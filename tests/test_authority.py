"""Authority persistence is independent of legacy write gates and never repairs on read."""

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ha_spark.authority import AuthorityState, AuthorityStore, load_authority, resolve_data_dir
from ha_spark.config import Settings


def settings_for(path: Path, **kwargs) -> Settings:
    return Settings(
        _env_file=None, ha_url="http://ha.test", ha_token="test", db_path=str(path / "ha.db"),
        **kwargs,
    )


def test_first_boot_store_precedes_marker_and_ignores_legacy(tmp_path, monkeypatch):
    settings = settings_for(
        tmp_path, proactive_mode="on",
        devices=[{"id": "inverter", "driver": "solis", "control": "ha_spark"}],
    )
    store = AuthorityStore(settings)
    replaced = []
    real_replace = os.replace

    def replace(src, dst):
        replaced.append(Path(dst).name)
        if Path(dst) == store.marker_path:
            assert store.store_path.exists()
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    result = store.initialise()
    assert result.status == "known"
    assert result.state.committed_mode == "observe"
    assert result.state.epoch == 1
    assert result.state.requester_kind == "system"
    assert replaced == [store.store_path.name, store.marker_path.name]
    assert load_authority(settings) == result
    assert store.initialise() == result


@pytest.mark.parametrize("existing", [False, True])
def test_read_only_uninitialised_writes_nothing(tmp_path, existing):
    directory = tmp_path / "data"
    if existing:
        directory.mkdir()
        (directory / "unrelated").write_text("keep")
    before = sorted(tmp_path.rglob("*"))
    assert load_authority(settings_for(directory)).status == "uninitialised"
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("damage", [
    "missing_store", "missing_marker", "corrupt_store", "invalid_schema", "zero_epoch",
    "unreadable_store", "corrupt_marker", "swapped_store", "swapped_marker",
])
def test_damaged_pair_is_unknown_and_never_reinitialised(tmp_path, monkeypatch, damage):
    settings = settings_for(tmp_path)
    store = AuthorityStore(settings)
    store.initialise()
    if damage == "missing_store":
        store.store_path.unlink()
    elif damage == "missing_marker":
        store.marker_path.unlink()
    elif damage == "corrupt_store":
        store.store_path.write_text("{")
    elif damage in {"invalid_schema", "zero_epoch", "swapped_store"}:
        data = json.loads(store.store_path.read_text())
        if damage == "invalid_schema":
            del data["committed_mode"]
        elif damage == "zero_epoch":
            data["epoch"] = 0
        else:
            data["store_id"] = "00000000-0000-4000-8000-000000000000"
        store.store_path.write_text(json.dumps(data))
    elif damage == "corrupt_marker":
        store.marker_path.write_text("{}")
    elif damage == "swapped_marker":
        data = json.loads(store.marker_path.read_text())
        data["store_id"] = "00000000-0000-4000-8000-000000000000"
        store.marker_path.write_text(json.dumps(data))
    else:
        real_read = Path.read_text

        def read(path, *args, **kwargs):
            if path == store.store_path:
                raise PermissionError("unreadable")
            return real_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    result = load_authority(settings)
    assert result.status == "authority_unknown"
    assert result.state is None
    assert store.initialise() == result
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("failure", ["file_fsync", "replace", "directory_fsync"])
def test_failed_save_raises_without_advancing_memory(tmp_path, monkeypatch, failure):
    store = AuthorityStore(settings_for(tmp_path))
    old = store.initialise().state
    new = old.model_copy(update={"committed_mode": "optimise", "epoch": 2})
    calls = 0
    real_fsync = os.fsync

    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == (1 if failure == "file_fsync" else 2):
            raise OSError("injected failure")
        real_fsync(fd)

    def replace(src, dst):
        raise OSError("injected failure")

    if failure == "replace":
        monkeypatch.setattr(os, "replace", replace)
    else:
        monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="injected failure"):
        store.save(new)
    assert store.state is old
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([
        store.store_path.name, store.marker_path.name,
    ])


def test_save_roundtrips_all_state_and_validates_candidates(tmp_path):
    settings = settings_for(tmp_path)
    store = AuthorityStore(settings)
    old = store.initialise().state
    payload = old.model_dump(mode="json")
    now = datetime.now(UTC).isoformat()
    payload.update(
        committed_mode="protect", epoch=2, last_changed_at=now, requester_kind="cli",
        transition={"target": "optimise", "started_at": now,
                    "steps": ["handover"], "failing_actions": ["readback"]},
        holds=[{"cause": "ev", "started_at": now}],
        fallback={"active": True, "causes": ["soc_integrity"]},
        readiness_notice={"notified": True},
    )
    new = AuthorityState.model_validate(payload)
    store.save(new)
    assert store.state == new
    assert load_authority(settings).state == new
    with pytest.raises(ValidationError):
        store.save(new.model_copy(update={"epoch": 0}))
    assert store.state == new


def test_durable_save_fsyncs_file_before_replace_then_directory(tmp_path, monkeypatch):
    store = AuthorityStore(settings_for(tmp_path))
    old = store.initialise().state
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    def replace(src, dst):
        assert Path(src).parent == Path(dst).parent == tmp_path
        # The complete new payload is visible before replacement.
        assert json.loads(Path(src).read_text())["epoch"] == 2
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    store.save(old.model_copy(update={"epoch": 2}))
    assert events == ["file", "replace", "directory"]


def test_interrupted_initialisation_does_not_grant_or_retry(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    store = AuthorityStore(settings)
    real_replace = os.replace

    def replace(src, dst):
        if Path(dst) == store.marker_path:
            raise OSError("marker failed")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError, match="marker failed"):
        store.initialise()
    assert store.state is None
    assert load_authority(settings).status == "authority_unknown"
    assert AuthorityStore(settings).initialise().status == "authority_unknown"


def test_canonical_paths(tmp_path):
    standalone = settings_for(tmp_path / "configured")
    assert resolve_data_dir(standalone) == tmp_path / "configured"
    addon = Settings(_env_file=None, ha_url="", ha_token="", db_path="elsewhere/ha.db")
    assert resolve_data_dir(addon) == Path("/data")
    assert AuthorityStore(addon).store_path.parent == Path("/data")
    assert AuthorityStore(addon).marker_path.parent == Path("/data")
    store = AuthorityStore(standalone)
    assert store.initialise().state.committed_mode == "observe"
    assert store.store_path.parent == resolve_data_dir(standalone)
    assert store.marker_path.parent == resolve_data_dir(standalone)
