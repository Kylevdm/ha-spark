"""The integration's half of the add-on/integration wire contract (#324).

Pure Python with no Home Assistant imports: this module holds the `api_version`
the integration speaks and, as later tickets land, the site snapshot it sends
and the plan summary it parses. `api_version` is an integer, separate from the
release version. A receiver may ignore an unknown field only if ignoring it keeps
every meaning and safety obligation it already has. A field the receiver must act
on, a change of ownership or source of truth, or a change to what an absent field
means always bumps `api_version`. An optional field is not compatible by that fact
alone.

The add-on's matching constant is `ha_spark/api/contract.py`. Both suites
compare their constant with `tests/fixtures/contract/contract.json`, so neither
half can move a contract version alone.
"""

from typing import Final

API_VERSION: Final = 1

# Every `api_version` this integration can send and accept replies to.
SUPPORTED_API_VERSIONS: Final = frozenset({1})
