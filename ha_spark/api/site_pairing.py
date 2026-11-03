"""Durable first-v4 site pairing, independent of per-boot bearer credentials.

Missing means not paired. Every other read failure is closed; a corrupt record
must never silently pair a different site. No paths or rejected values escape.
"""

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from ha_spark.config import Settings

PAIRING_FILE = "ha_spark_integration_site.json"


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
