from ha_agent.ha.models import EntityState, StateChangedEvent
from ha_agent.ha.state_cache import StateCache


def _state(entity_id: str, state: str) -> EntityState:
    return EntityState(entity_id=entity_id, state=state)


async def test_apply_update_and_removal() -> None:
    cache = StateCache()
    await cache.on_state_changed(
        StateChangedEvent(entity_id="light.kitchen", new_state=_state("light.kitchen", "on"))
    )
    assert cache.get("light.kitchen") is not None
    assert cache.get("light.kitchen").state == "on"  # type: ignore[union-attr]

    # Update.
    await cache.on_state_changed(
        StateChangedEvent(
            entity_id="light.kitchen",
            old_state=_state("light.kitchen", "on"),
            new_state=_state("light.kitchen", "off"),
        )
    )
    assert cache.get("light.kitchen").state == "off"  # type: ignore[union-attr]

    # Removal (new_state is None).
    await cache.on_state_changed(StateChangedEvent(entity_id="light.kitchen", old_state=None))
    assert cache.get("light.kitchen") is None


async def test_by_domain_filtering() -> None:
    cache = StateCache()
    for eid in ("light.a", "light.b", "sensor.x"):
        await cache.on_state_changed(StateChangedEvent(entity_id=eid, new_state=_state(eid, "1")))
    assert [s.entity_id for s in cache.by_domain("light")] == ["light.a", "light.b"]
    assert len(cache) == 3
