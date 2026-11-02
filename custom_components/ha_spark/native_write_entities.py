"""Shared entity refusal, fixed-code errors and caller-context audit logging.

Names, hosts and rejected values never enter logs or exceptions. Readings remain
available on a read-only device; availability is not permission to write.
"""
import logging

from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .commands import IntentResult
from .const import DOMAIN
from .native_entities import NativeEntity

_LOGGER = logging.getLogger(__name__)


class WriteEntity(NativeEntity):
    _attr_entity_category = EntityCategory.CONFIG
    control = "charge"
    frame = "holding_43141"

    def words(self) -> list[int] | None:
        reading = self.coordinator.native.reading(self.inverter, self.frame)
        return list(reading.registers) if reading and reading.registers is not None else None

    @property
    def available(self) -> bool:
        return super().available and self.words() is not None

    def audit(self, outcome: str) -> None:
        context = self._context
        caller = (context.user_id or ("automation" if context.parent_id else "system")
                  if context else "system")
        state = self.coordinator.authority.state
        # Topology ids are owner input too: log the opaque gateway/unit key only.
        _LOGGER.info("Native entity write key=%s mode=%s outcome=%s caller=%s",
                     self.unique_id, state.committed_mode if state else "unknown", outcome, caller)

    def error(self, key: str, *, failed: bool = False) -> None:
        self.audit(key)
        cls = HomeAssistantError if failed else ServiceValidationError
        state = self.coordinator.authority.state
        placeholders: dict[str, str] | None = (
            {"mode": state.committed_mode if state else "unknown"}
            if key == "entity_write_mode" else None)
        raise cls(translation_domain=DOMAIN, translation_key=key,
                  translation_placeholders=placeholders) from None

    def epoch(self) -> int:
        state = self.coordinator.authority.state
        if state is None:
            self.error("authority_unknown")
        assert state is not None
        reason = self.coordinator.writes._refusal(self.inverter, state.epoch,
                                                entity_control=self.control)
        if reason:
            if state.transition is not None:
                self.error("transition_in_progress")
            self.refused(reason)
        return state.epoch

    def refused(self, reason: str) -> None:
        state = self.coordinator.authority.state
        if reason == "not_available" and state is not None and state.transition is not None:
            self.error("transition_in_progress")
        if reason == "mode":
            self.error("protection_owned" if self.control == "power" else "entity_write_mode")
        self.error(reason if reason in ("authority_unknown", "stale_epoch", "read_only", "limits",
                                       "untrusted_soc", "grid_charge_not_permitted",
                                       "not_available", "invalid_intent") else "read_only")

    def result(self, result: IntentResult) -> None:
        if result.outcome == "refused":
            self.refused(result.reason or "not_available")
        if result.outcome not in ("applied", "unchanged"):
            self.error("entity_write_unverified" if result.outcome == "unverified"
                       else "entity_write_failed", failed=True)
        self.audit(result.outcome)
        self.coordinator.async_update_listeners()
