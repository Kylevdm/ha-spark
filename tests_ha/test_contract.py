"""The integration's `api_version` equals the shared contract fixture's (#473).

The fixture is the contract both suites read, so neither half can move a
contract version alone.
"""

import json
from pathlib import Path

from custom_components.ha_spark.wire import API_VERSION, SUPPORTED_API_VERSIONS

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "contract"


def test_api_version_matches_the_shared_fixture() -> None:
    contract = json.loads((CONTRACT_DIR / "contract.json").read_text(encoding="utf-8"))
    assert API_VERSION == contract["api_version"]


def test_api_version_is_in_an_immutable_supported_set() -> None:
    assert API_VERSION in SUPPORTED_API_VERSIONS
    assert isinstance(SUPPORTED_API_VERSIONS, frozenset)
