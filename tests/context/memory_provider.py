"""A context provider held in memory, and the setup the tools receive with it.

It is not a test module: the tests of the context service and of ``describe_table`` share it.
"""

from akko_mcp_trino.context import (
    CONTEXT_API,
    ContextChain,
    ContextLoop,
    ContextSettings,
    ContextSetup,
)


class Memory:
    """A catalogue in memory: tables by reference, terms by key, the sets it serves."""

    context_api = CONTEXT_API

    def __init__(self, name="catalog", *, tables=None, terms=None, sets=None, catalogs=("demo",)):
        self.name = name
        self.tables = dict(tables or {})
        self.glossary = dict(terms or {})
        self.sets = sets if sets is not None else {}
        self.catalogs = set(catalogs)
        self.calls = []
        self.term_calls = []
        self.term_failure = None

    def maps(self, catalog):
        return catalog in self.catalogs

    def served_sets(self):
        if isinstance(self.sets, Exception):
            raise self.sets
        return self.sets

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        self.calls.append(tuple(refs))
        return {ref: self.tables[ref] for ref in refs if ref in self.tables}

    async def terms(self, keys, *, deadline):
        self.term_calls.append(tuple(keys))
        if self.term_failure is not None:
            raise self.term_failure
        return {key: self.glossary[key] for key in keys if key in self.glossary}

    def check_lines(self):
        return []

    async def aclose(self):
        return None

    def credentials_stamp(self):
        return ""


def setup_for(config, *providers):
    """A started context setup over ``providers``; the caller closes it."""
    loop = ContextLoop()
    loop.start()
    chain = ContextChain(providers, ContextSettings.from_config(config), loop=loop)
    return ContextSetup(chain, config, loop)
