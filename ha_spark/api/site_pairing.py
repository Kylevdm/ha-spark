"""Durable first-v4 site pairing, independent of per-boot bearer credentials.

Missing means not paired. Every other read failure is closed; a corrupt record
must never silently pair a different site. No paths or rejected values escape.

Replacement binds the accepted topology to the old site before atomically
replacing the pairing file (the commit point). A crash before commit keeps the
old site's revision floor; after commit, for_site clears only that floor while
keeping all accepted limits. Read-back verifies both records. No replacement
failure unlinks the pairing file. AppState's intake lock serialises replacement,
topology acceptance and ownership handover; this synchronous durable operation
must run under that lock in the daemon. No authority record is written here.
"""

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import UUID

from ha_spark.config import Settings
from ha_spark.energy.topology_record import (
    _durable_write,
    load_topology_record,
    save_topology_record,
)

PAIRING_FILE = "ha_spark_integration_site.json"
SITE_NOT_PAIRED = "site_not_paired"
SITE_NOT_PAIRED_TEXT = (
    "The integration was re-added and this site is not paired. "
    "Use ha-spark pair --replace to replace the pairing."
)
ReplaceReason = Literal[
    "invalid_site_id", "same_site", "pairing_untrusted", "pairing_mismatch",
    "handshake_pending", "transition_pending", "mode_not_observe", "pairing_save_failed",
]
REPLACE_TEXT: dict[ReplaceReason, str] = {
    "invalid_site_id": "Both site ids must be canonical UUIDs.",
    "same_site": "The new site must differ from the paired site.",
    "pairing_untrusted": "The stored pairing cannot be trusted; repair it before replacing it.",
    "pairing_mismatch": "The stored pairing does not match the expected old site.",
    "handshake_pending": "An ownership handshake is pending; pairing was not replaced.",
    "transition_pending": "A mode transition is pending; pairing was not replaced.",
    "mode_not_observe": "Enter Observe before replacing the pairing.",
    "pairing_save_failed": "The pairing replacement could not be verified; nothing may write.",
}


def replacement_refusal(settings: Settings, from_site: str, to_site: str) -> ReplaceReason | None:
    """Read-only identity checks, before the daemon checks authority admission."""
    try:
        if str(UUID(from_site)) != from_site or str(UUID(to_site)) != to_site:
            return "invalid_site_id"
    except Exception:  # Untrusted ids must never expose parser exceptions.
        return "invalid_site_id"
    if from_site == to_site:
        return "same_site"
    paired, trusted = load_pairing(settings)
    if not trusted:
        return "pairing_untrusted"
    if paired != from_site:
        return "pairing_mismatch"
    return None


def replace_pairing(settings: Settings, from_site: str, to_site: str) -> ReplaceReason | None:
    """Bind the old floor, then commit the new pairing; never grant authority."""
    refusal = replacement_refusal(settings, from_site, to_site)
    if refusal is not None:
        return refusal
    try:
        loaded = load_topology_record(settings)
        if loaded.site_id is None and loaded.revision is not None and loaded.topology is not None:
            supported = None
            if loaded.supported_revision is not None and loaded.supported_topology is not None:
                supported = (loaded.supported_revision, loaded.supported_topology)
            save_topology_record(
                settings, loaded.revision, loaded.topology, supported, site_id=from_site,
            )
            bound = load_topology_record(settings)
            if bound != replace(loaded, site_id=from_site):
                return "pairing_save_failed"
            loaded = bound
        path = Path(settings.db_path).parent / PAIRING_FILE
        _durable_write(path, json.dumps({"site_id": to_site}).encode())
        verified = load_topology_record(settings)
        # The bound record carries the old floor; an unbound one would floor the new site.
        if load_pairing(settings) != (to_site, True) or verified != loaded:
            return "pairing_save_failed"
    except OSError:
        return "pairing_save_failed"
    return None


def load_pairing(settings: Settings) -> tuple[str | None, bool]:
    """Return the canonical paired site and whether the record can be trusted."""
    path = Path(settings.db_path).parent / PAIRING_FILE
    try:
        with path.open("rb") as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise ValueError
        body = json.loads(raw)
        if (not isinstance(body, dict) or set(body) != {"site_id"}
                or not isinstance(body["site_id"], str)):
            raise ValueError
        site = str(UUID(body["site_id"]))
        if site != body["site_id"]:
            raise ValueError
        return site, True
    except FileNotFoundError:
        # A dangling link isn't an unpaired installation.
        return None, not os.path.lexists(path)
    except (OSError, ValueError, TypeError, RecursionError):
        return None, False


def save_pairing(settings: Settings, site_id: str) -> bool:
    """Atomic 0600 replacement and independent reload before accepting a snapshot."""
    path = Path(settings.db_path).parent / PAIRING_FILE
    name: str | None = None
    replaced = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{PAIRING_FILE}.", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps({"site_id": site_id}).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        name, replaced = None, True
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if load_pairing(settings) == (site_id, True):
            return True
    except OSError:
        pass
    finally:
        if name is not None:
            try:
                Path(name).unlink(missing_ok=True)
            except OSError:
                pass
    if replaced:
        # A rejected snapshot must not leave its site paired.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return False
