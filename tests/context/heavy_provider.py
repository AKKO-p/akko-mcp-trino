"""A provider whose module loads the core's Trino client: the contract must refuse it.

It is not a test module; ``test_contract_http.py`` imports it only inside the test that
checks the contract catches it, so nothing else loads the Trino client through it.
"""

import akko_mcp_trino.trino_client  # noqa: F401 - the fault under test
from tests.context.test_contract_http import Recorded


class Heavy(Recorded):
    """The reference provider, from a module that loads the core's Trino side."""
