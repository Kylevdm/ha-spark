"""The add-on's half of the add-on/integration wire contract (#324).

`API_VERSION` is the `api_version` the add-on puts on every plan summary and
accepts on every site snapshot. It is an integer, separate from the release
version, and bumps only for breaking contract changes: additive fields are
ignored by receivers that don't know them.

The integration's matching constant is `custom_components/ha_spark/wire.py`.
Both suites compare their constant with `tests/fixtures/contract/contract.json`,
so neither half can move a contract version alone.
"""

from typing import Final

API_VERSION: Final = 1
