"""Readiness thresholds are bounded settings, aligned with the add-on options."""
import pytest
from pydantic import ValidationError

from ha_spark.config import Settings


def test_readiness_defaults():
    settings = Settings()
    assert settings.readiness_window_days == 21
    assert settings.readiness_mae_improvement_pct == 10
    assert settings.readiness_max_cost_delta == 0.0
    assert settings.readiness_min_guard_coverage_pct == 90


@pytest.mark.parametrize("key,value", [
    ("readiness_window_days", 0), ("readiness_window_days", -1),
    ("readiness_window_days", 1.5),
    ("readiness_mae_improvement_pct", -1), ("readiness_mae_improvement_pct", 101),
    ("readiness_min_guard_coverage_pct", -1), ("readiness_min_guard_coverage_pct", 101),
    ("readiness_max_cost_delta", float("nan")),
    ("readiness_max_cost_delta", float("inf")),
])
def test_readiness_bounds(key, value):
    with pytest.raises(ValidationError):
        Settings(**{key: value})
