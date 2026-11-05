"""Fixtures shared by the context tests."""

import pytest

from akko_mcp_trino.context.loop import ContextLoop


@pytest.fixture
def loop():
    started = ContextLoop()
    started.start()
    yield started
    started.close()
