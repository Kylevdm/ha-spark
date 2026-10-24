"""Packaging contracts for the two-product release (#473, spec #472).

One `vX.Y.Z` tag ships the add-on and the companion integration, so the
`config.yaml` `version`, the integration manifest's `version` and
`pyproject.toml`'s `version` must stay in lockstep; the Supervisor store must
see no add-on config file outside `ha_spark_addon/`; HACS must not offer the
default branch; and both test suites read one `api_version` from the shared
contract fixture. The integration is read as files here: this suite never
imports `custom_components`, because it runs without Home Assistant.
"""

import ast
import json
import os
import tomllib
from pathlib import Path

import pytest
import yaml

from ha_spark.api.contract import API_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = "ha_spark_addon"
MANIFEST_PATH = REPO_ROOT / "custom_components" / "ha_spark" / "manifest.json"
HACS_PATH = REPO_ROOT / "hacs.json"
CONTRACT_PATH = REPO_ROOT / "tests" / "fixtures" / "contract" / "contract.json"
CONFIG_NAMES = frozenset({"config.yaml", "config.yml", "config.json"})
IGNORED_DIRS = frozenset({"__pycache__"})


def _release_versions(root: Path = REPO_ROOT) -> dict[str, str]:
    """Read the three places one release version must agree with itself."""
    addon = yaml.safe_load((root / ADDON_DIR / "config.yaml").read_text("utf-8"))
    manifest = json.loads(
        (root / "custom_components" / "ha_spark" / "manifest.json").read_text("utf-8")
    )
    pyproject = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    return {
        "ha_spark_addon/config.yaml": str(addon["version"]),
        "manifest.json": str(manifest["version"]),
        "pyproject.toml": str(pyproject["project"]["version"]),
    }


def stray_addon_config_files(root: Path) -> list[Path]:
    """Find add-on config files outside `ha_spark_addon/` (the #252 gotcha).

    Hidden directories (`.git`, a `.venv` whose site-packages holds such files)
    and caches are skipped, as is the add-on directory itself. Point it at a
    `tmp_path` tree to plant files and check what it catches.
    """
    stray: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and name not in IGNORED_DIRS and name != ADDON_DIR
        )
        stray.extend(
            Path(dirpath) / name for name in sorted(filenames) if name in CONFIG_NAMES
        )
    return stray


def _in_lockstep(root: Path = REPO_ROOT) -> bool:
    return len(set(_release_versions(root).values())) == 1


def test_release_versions_stay_in_lockstep() -> None:
    assert _in_lockstep(), _release_versions()
    assert _release_versions()["manifest.json"] == "0.25.0"


@pytest.mark.parametrize("stale", ["addon", "manifest", "project"])
def test_lockstep_check_fails_on_one_mismatched_version(tmp_path: Path, stale: str) -> None:
    versions = dict.fromkeys(["addon", "manifest", "project"], "0.25.0")
    versions[stale] = "0.24.0"
    (tmp_path / ADDON_DIR).mkdir()
    (tmp_path / ADDON_DIR / "config.yaml").write_text(f"version: {versions['addon']}\n")
    (tmp_path / "custom_components" / "ha_spark").mkdir(parents=True)
    (tmp_path / "custom_components" / "ha_spark" / "manifest.json").write_text(
        json.dumps({"version": versions["manifest"]})
    )
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{versions["project"]}"\n')
    assert not _in_lockstep(tmp_path)


def test_no_addon_config_outside_the_addon_directory() -> None:
    """A config file anywhere else would make the Supervisor store it as an add-on."""
    assert stray_addon_config_files(REPO_ROOT) == []


def test_stray_addon_config_walk_skips_the_addon_and_hidden_dirs(tmp_path: Path) -> None:
    (tmp_path / ADDON_DIR).mkdir()
    (tmp_path / ADDON_DIR / "config.yaml").write_text("version: '0.25.0'", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "config.yml").write_text("{}", encoding="utf-8")
    (tmp_path / ".venv" / "site-packages").mkdir(parents=True)
    (tmp_path / ".venv" / "site-packages" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "config.yaml").write_text("{}", encoding="utf-8")
    found = [path.relative_to(tmp_path).as_posix() for path in stray_addon_config_files(tmp_path)]
    assert found == ["docs/config.yml"]


def test_hacs_json_hides_the_default_branch() -> None:
    hacs = json.loads(HACS_PATH.read_text(encoding="utf-8"))
    assert hacs["name"] == "ha-spark"
    assert hacs["hide_default_branch"] is True
    assert hacs["homeassistant"] == "2026.10.3"


def test_contract_fixture_declares_the_addon_api_version() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert API_VERSION == contract["api_version"]


def test_wheel_still_packages_only_the_addon_package() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["ha_spark"]


def test_manifest_stays_a_bare_custom_integration() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["domain"] == "ha_spark"
    assert manifest["name"] == "ha-spark"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_polling"
    assert manifest["requirements"] == []
    assert "single_config_entry" not in manifest


def test_integration_imports_nothing_from_the_addon_package() -> None:
    """The two halves stay separate: the integration never imports `ha_spark`."""
    sources = sorted((REPO_ROOT / "custom_components" / "ha_spark").rglob("*.py"))
    assert sources, "the integration has no Python sources to check"
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module or ""]
            else:
                continue  # a relative import stays inside the integration
            assert not any(
                module == "ha_spark" or module.startswith("ha_spark.")
                for module in modules
            ), f"{path} imports {modules}"
