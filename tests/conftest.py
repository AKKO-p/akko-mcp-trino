"""Every parse pool a test builds is closed after it, so no worker outlives its test. The
pool the tools share when they are given none stays: the suite starts its workers once."""

import pytest

from akko_mcp_trino import parse_pool


@pytest.fixture(autouse=True)
def _close_parse_pools():
    before = set(parse_pool._POOLS)
    yield
    for pool in set(parse_pool._POOLS) - before - set(parse_pool._SHARED.values()):
        pool.close()
