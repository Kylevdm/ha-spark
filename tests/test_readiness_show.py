"""The CLI evaluation is read-only, even on a fresh install."""
from ha_spark.cli import main
from ha_spark.config import Settings


def test_readiness_show_does_not_create_database(tmp_path, monkeypatch, capsys):
    settings = Settings(db_path=str(tmp_path / "missing.db"))
    monkeypatch.setattr("ha_spark.cli.load_settings", lambda **kwargs: settings)
    assert main(["readiness", "show"]) == 0
    text = capsys.readouterr().out
    assert "insufficient_evidence" in text
    assert "too few qualifying days" in text
    assert "cost_delta" in text
    assert "excluded_days" in text
    assert not list(tmp_path.iterdir())
