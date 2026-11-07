"""A read the chain cuts at its deadline closes its connection, over a real socket.

The catalogue accepts connections and never answers. One source keeps its deadline itself,
with ``asyncio.timeout``, as the adapters do; another ignores it and is cut by the chain
alone. Either way the client closes the connection, so that no request is left pending at
the catalogue to be served once it recovers.
"""

import asyncio
import time

import httpx
import pytest

from akko_mcp_trino.context import CONTEXT_API, ContextChain, ProviderUnavailable, TableRef
from akko_mcp_trino.context import chain as chain_module
from akko_mcp_trino.testing.context_contract import _Silent, _ToSilent

from .test_chain import _settings, _tasks_left, _until

ORDERS = TableRef.of("demo", "crm", "orders")


class Socketed:
    """A source over HTTP that keeps its deadline with ``asyncio.timeout``."""

    name = "socketed"
    context_api = CONTEXT_API

    def __init__(self, port: int) -> None:
        self._client = httpx.AsyncClient(
            transport=_ToSilent(port), base_url="http://catalogue.test"
        )

    def maps(self, catalog):
        return catalog == "demo"

    def served_sets(self):
        return {}

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        try:
            async with asyncio.timeout(deadline - time.monotonic()):
                await self._client.get("/v1/table")
        except TimeoutError:
            raise ProviderUnavailable("the catalogue did not answer in time") from None
        return {}

    async def terms(self, keys, *, deadline):
        return {}

    def check_lines(self):
        return []

    async def aclose(self):
        await self._client.aclose()

    def credentials_stamp(self):
        return ""


class Heedless(Socketed):
    """A source that ignores its deadline: only the chain cuts it."""

    name = "heedless"

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        await self._client.get("/v1/table")
        return {}


@pytest.mark.parametrize("kind", [Socketed, Heedless])
def test_a_read_cut_at_its_deadline_closes_its_connection(loop, kind):
    silent = _Silent()
    source = kind(silent.port)
    chain = ContextChain([source], _settings(), loop=loop, clock=time.monotonic)
    try:
        deadline = time.monotonic() + 0.3
        got = chain.gather([ORDERS], deadline=deadline)
        assert time.monotonic() <= deadline + chain_module._GRACE_SECONDS
        assert got.statuses[source.name] == "unavailable"
        assert silent.asked() == 1
        # Closed by the time the chain answers; a short wait only for the socket to say so.
        _until(lambda: silent.open_connections() == 0, seconds=0.5)
        _until(lambda: _tasks_left(loop) == 0, seconds=0.5)
    finally:
        chain.close()
        silent.close()


def test_the_chain_ends_its_waits_within_its_grace():
    """The source's own deadline, then the chain's single cancellation, each given its wait."""
    assert 2 * chain_module._CANCEL_WAIT_SECONDS < chain_module._GRACE_SECONDS
