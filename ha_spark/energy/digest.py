"""Format and deliver the once-daily morning digest of the current plan."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

from ha_spark.config import Settings
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.sources import parse_time
from ha_spark.ha.rest import HomeAssistantRest, notify
from ha_spark.logging import get_logger

log = get_logger(__name__)


def format_digest(plan: ChargePlan, settings: Settings, now: datetime) -> tuple[str, str]:
    """Return a short title and phone-readable description of the current plan."""
    local_now = now.astimezone(load_timezone(settings.timezone))
    title = f"ha-spark morning plan — {local_now.day} {local_now:%B}"
    intent = plan.charge_intent
    window = f"{intent.window_start:%H:%M} to {intent.window_end:%H:%M}"

    if not plan.soc.ok:
        if settings.proactive_mode == "on":
            charge_line = (
                "The SoC reading is untrusted. ha-spark will not program a charge tonight. "
                f"Reason: {plan.soc.reason}."
            )
        else:
            charge_line = (
                "This describes what ha-spark would do: it would not program a charge tonight "
                f"because the SoC reading is untrusted. Reason: {plan.soc.reason}."
            )
    elif plan.required_kwh <= 0:
        charge_line = (
            "No grid charge is needed tonight."
            if settings.proactive_mode == "on"
            else "This describes what ha-spark would do: no grid charge is needed tonight."
        )
    elif settings.proactive_mode == "on":
        charge_line = (
            f"Tonight ha-spark charges from {window}, targeting "
            f"{intent.target_soc_pct:.0f}% SoC."
        )
    else:
        charge_line = (
            "This describes what ha-spark would do: charge tonight "
            f"from {window}, targeting {intent.target_soc_pct:.0f}% SoC."
        )
    lines = [charge_line]

    for reservation in plan.reservations:
        lines.append(
            f"Reserve {reservation.energy_kwh:.1f} kWh for {reservation.name}: "
            f"{reservation.reason}"
        )

    if plan.planned_cost is None:
        lines.append("Expected cost is unavailable for this plan.")
    elif plan.baseline_cost is None:
        lines.append(f"Expected cost: £{plan.planned_cost:.2f}.")
    else:
        lines.append(
            f"Expected cost: £{plan.planned_cost:.2f} "
            f"(£{plan.baseline_cost:.2f} without the battery)."
        )

    return title, "\n".join(lines)


def _sent_path(settings: Settings) -> Path:
    return Path(settings.db_path).parent / "ha_spark_digest.json"


def _load_sent_date(settings: Settings) -> date | None:
    path = _sent_path(settings)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload["last_sent_date"]
        return date.fromisoformat(value)
    except (OSError, ValueError, TypeError, KeyError):
        log.warning("Reading morning digest state failed; treating today as unsent")
        return None


def _save_sent_date(settings: Settings, sent_date: date) -> None:
    path = _sent_path(settings)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps({"last_sent_date": sent_date.isoformat()}), encoding="utf-8"
        )
        os.replace(tmp_path, path)
    except OSError:
        log.warning("Caching morning digest state failed")
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def digest_due(settings: Settings, now: datetime) -> bool:
    """Whether a digest attempt is due on this household-clock day."""
    if not settings.notify_service:
        return False
    local_now = now.astimezone(load_timezone(settings.timezone))
    if local_now.time().replace(tzinfo=None) < parse_time(settings.digest_time):
        return False
    return _load_sent_date(settings) != local_now.date()


async def run_digest_tick(
    settings: Settings, plan: ChargePlan, now: datetime, rest: HomeAssistantRest
) -> None:
    """Send the current plan once per household-clock day after ``digest_time``."""
    service = settings.notify_service
    if not digest_due(settings, now):
        return

    tz = load_timezone(settings.timezone)
    local_now = now.astimezone(tz)
    title, message = format_digest(plan, settings, now)
    try:
        await notify(rest, service, title, message)
    except Exception:  # noqa: BLE001 - notification failures never affect planning
        log.warning("Morning digest notify failed; will retry next tick")
        return
    _save_sent_date(settings, local_now.date())
