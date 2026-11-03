"""Local-authority setup for guard tests that inject a fake charge writer."""
import pytest

from custom_components.ha_spark.coordinator import SparkCoordinator


async def local_authority(coordinator: SparkCoordinator, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use a verified Optimise store instead of the legacy display mirror."""
    await coordinator.authority.async_load()
    assert await coordinator.authority.async_initialise()
    state = coordinator.authority.state
    assert state is not None
    assert await coordinator.authority.async_save(state.model_copy(update={
        "committed_mode": "optimise",
    }))
    monkeypatch.setattr(coordinator.control, "owned", lambda: True)
