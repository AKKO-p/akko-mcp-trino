"""The published contract, run on a provider over HTTP, and the faults it catches.

``Recorded`` is written the way an adapter is: it reads a small catalogue over HTTP,
keeps only the sets its settings allow, folds names as Trino lists them, respects the
deadline and turns every failure into a status-bearing error without the catalogue's
text. The variants below each break one rule, and the contract must catch them.
"""

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

import httpx
import pytest

from akko_mcp_trino.context import (
    CLASSIFICATION,
    CONTEXT_API,
    GLOSSARY,
    OWNER,
    PROPERTY,
    ColumnContext,
    Join,
    Labelled,
    ProviderError,
    ProviderRefused,
    ProviderUnavailable,
    TableContext,
    TableRef,
    Term,
    TermRef,
)
from akko_mcp_trino.testing import ContextContract, ContractHarness, ContractNames

log = logging.getLogger(__name__)

SECRET = "contract-token-9d41b7c2"

TRAP = TableRef("warehouse", "o'brien.été", "l'année fiscale.v2")

# The catalogue's recorded answers, by (catalog, schema, table) as Trino lists them.
TABLES: dict[tuple[str, str, str], dict[str, Any]] = {
    ("warehouse", "ledger", "invoices"): {
        "status": "ACTIVE",
        "description": "One row per invoice",
        "owner": "billing-team",
        "tags": [
            {"name": "confidential", "set": "sensitivity"},
            {"name": "draft", "set": "internal-only"},
        ],
        "terms": [
            {"key": "t-invoice", "glossary": "finance"},
            {"key": "t-staff", "glossary": "hr"},
        ],
        "properties": {
            "keep": {"value": "10y", "set": "retention"},
            "budget": {"value": "high", "set": "cost"},
        },
        "grain": ["Invoice_Id"],
        "joins": [
            {
                "columns": ["Payment_Ref"],
                "target": {"catalog": "warehouse", "schema": "ledger", "table": "payments"},
                "target_columns": ["Payment_Ref"],
            }
        ],
        "columns": {
            "Invoice_Id": {},
            "Invoice_Total": {
                "description": "Amount due, taxes included",
                "terms": [{"key": "t-invoice", "glossary": "finance"}],
                "classifications": [{"name": "financial", "set": "sensitivity"}],
                "properties": {"unit": {"value": "eur", "set": "retention"}},
            },
            "Payment_Ref": {},
        },
    },
    ("warehouse", "ledger", "payments"): {
        "status": "ACTIVE",
        "description": "One row per payment",
        "columns": {"Payment_Ref": {}},
        "partitionColumns": ["Booking_Month"],
    },
    ("warehouse", "ledger", "refunds"): {"status": "DELETED", "description": "Refunds"},
    (TRAP.catalog, TRAP.schema, TRAP.table): {
        "status": "ACTIVE",
        "description": "A name to quote",
        "columns": {"Période": {}},
    },
}

TERMS: dict[str, dict[str, Any]] = {
    "t-invoice": {
        "name": "Invoice",
        "glossary": "finance",
        "definition": "A bill sent to a customer",
        "relations": [
            {"key": "t-staff", "name": "Staff", "glossary": "hr", "relation": "related"},
            {"key": "t-payment", "name": "Payment", "glossary": "finance", "relation": "part"},
        ],
    },
    "t-staff": {"name": "Staff", "glossary": "hr"},
}

NAMES = ContractNames(
    catalog="warehouse",
    unmapped_catalog="lakeside",
    described=TableRef("warehouse", "ledger", "invoices"),
    partitioned=TableRef("warehouse", "ledger", "payments"),
    partition_column="booking_month",
    dropped=TableRef("warehouse", "ledger", "refunds"),
    unknown=TableRef("warehouse", "ledger", "ghosts"),
    trap=TRAP,
    term_column="invoice_total",
    foreign_glossary="hr",
)

ENV = {
    "REC_TOKEN": SECRET,
    "REC_CATALOGS": "warehouse",
    "REC_GLOSSARIES": "finance",
    "REC_CLASSIFICATIONS": "sensitivity",
    "REC_PROPERTIES": "retention",
}


def replay(request: httpx.Request) -> httpx.Response:
    """The catalogue as recorded: a wrong token is refused, an unknown name is a 404."""
    if request.headers.get("authorization") != f"Bearer {SECRET}":
        return httpx.Response(401)
    params = request.url.params
    if request.url.path == "/v1/table":
        found = TABLES.get((params["catalog"], params["schema"], params["table"]))
    else:
        found = TERMS.get(params["key"])
    return httpx.Response(200, json=found) if found else httpx.Response(404)


def _items(env: Mapping[str, str], name: str) -> frozenset[str]:
    return frozenset(part.strip() for part in env.get(name, "").split(",") if part.strip())


class Recorded:
    """A provider over the recorded catalogue, as an adapter writes one."""

    name = "recorded"
    context_api = CONTEXT_API
    fold = True

    def __init__(self, env: Mapping[str, str], transport: httpx.AsyncBaseTransport) -> None:
        self._catalogs = _items(env, "REC_CATALOGS")
        self._sets = {
            GLOSSARY: _items(env, "REC_GLOSSARIES"),
            CLASSIFICATION: _items(env, "REC_CLASSIFICATIONS"),
            PROPERTY: _items(env, "REC_PROPERTIES"),
            OWNER: frozenset({"owner"}),
        }
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url="http://catalogue.test",
            headers={"authorization": f"Bearer {env['REC_TOKEN']}"},
        )

    def maps(self, catalog: str) -> bool:
        return catalog in self._catalogs

    def served_sets(self) -> Mapping[str, frozenset[str]]:
        return self._sets

    async def describe(
        self, refs: Sequence[TableRef], *, deadline: float, on_behalf_of: None = None
    ) -> Mapping[TableRef, TableContext]:
        mine = [ref for ref in refs if self.maps(ref.catalog)]
        found = await self._all(
            [
                self._get("/v1/table", {"catalog": r.catalog, "schema": r.schema, "table": r.table})
                for r in mine
            ],
            deadline,
        )
        return {
            ref: self._table(spec)
            for ref, spec in zip(mine, found)
            if spec and spec.get("status") != "DELETED"
        }

    async def terms(self, keys: Sequence[str], *, deadline: float) -> Mapping[str, Term]:
        unique = list(dict.fromkeys(keys))
        found = await self._all([self._get("/v1/term", {"key": key}) for key in unique], deadline)
        served = self._sets[GLOSSARY]
        return {
            key: Term(
                key=key,
                name=spec["name"],
                glossary=spec["glossary"],
                definition=spec.get("definition", ""),
                relations=tuple(
                    TermRef(r["key"], r["name"], r["glossary"], r["relation"])
                    for r in spec.get("relations", [])
                    if r["glossary"] in served
                ),
            )
            for key, spec in zip(unique, found)
            if spec and spec["glossary"] in served
        }

    def check_lines(self) -> list[str]:
        return [f"recorded: http://catalogue.test; glossaries: {sorted(self._sets[GLOSSARY])}"]

    async def aclose(self) -> None:
        await self._client.aclose()

    def credentials_stamp(self) -> str:
        return ""

    # -- reading the catalogue

    async def _all(self, calls: list, deadline: float) -> list:
        try:
            async with asyncio.timeout(deadline - time.monotonic()):
                return await asyncio.gather(*calls)
        except TimeoutError:
            raise ProviderUnavailable("the catalogue did not answer in time") from None

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        response = await self._client.get(path, params=params)
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise ProviderRefused("the catalogue refused the account")
        if response.status_code != 200:
            log.warning("recorded: the catalogue answered %s", response.status_code)
            raise self.failure(response)
        try:
            return response.json()
        except ValueError:
            raise ProviderUnavailable("the catalogue answered no JSON") from None

    def failure(self, response: httpx.Response) -> ProviderError:
        return ProviderUnavailable(f"the catalogue answered {response.status_code}")

    def _name(self, text: str) -> str:
        return text.lower() if self.fold else text

    def _labels(self, specs: list, kind: str) -> tuple[Labelled, ...]:
        return tuple(Labelled(s["name"], s["set"]) for s in specs if s["set"] in self._sets[kind])

    def _properties(self, specs: dict) -> Mapping[str, Labelled]:
        return MappingProxyType(
            {
                key: Labelled(s["value"], s["set"])
                for key, s in specs.items()
                if s["set"] in self._sets[PROPERTY]
            }
        )

    def _keys(self, specs: list) -> tuple[str, ...]:
        return tuple(s["key"] for s in specs if s["glossary"] in self._sets[GLOSSARY])

    def _column(self, spec: dict) -> ColumnContext:
        return ColumnContext(
            description=spec.get("description", ""),
            classifications=self._labels(spec.get("classifications", []), CLASSIFICATION),
            terms=self._keys(spec.get("terms", [])),
            properties=self._properties(spec.get("properties", {})),
        )

    def _table(self, spec: dict) -> TableContext:
        columns = {self._name(n): self._column(c) for n, c in spec.get("columns", {}).items()}
        for name in spec.get("partitionColumns", []):
            columns.setdefault(self._name(name), ColumnContext())
        return TableContext(
            description=spec.get("description", ""),
            owner=spec.get("owner", ""),
            tags=self._labels(spec.get("tags", []), CLASSIFICATION),
            terms=self._keys(spec.get("terms", [])),
            properties=self._properties(spec.get("properties", {})),
            grain=tuple(self._name(n) for n in spec.get("grain", [])),
            joins=tuple(
                Join(
                    tuple(self._name(c) for c in j["columns"]),
                    TableRef.of(**j["target"]),
                    tuple(self._name(c) for c in j["target_columns"]),
                )
                for j in spec.get("joins", [])
            ),
            columns=MappingProxyType(columns),
        )


def harness_of(kind: type[Recorded]) -> ContractHarness:
    return ContractHarness(
        build=lambda env, transport: kind(env, transport),
        replay=replay,
        env=ENV,
        secret=SECRET,
        names=NAMES,
    )


class TestRecordedProviderContract(ContextContract):
    @pytest.fixture
    def harness(self):
        return harness_of(Recorded)


# ---- the faults the contract catches


class Leaky(Recorded):
    """Puts the catalogue's error body in its exception."""

    def failure(self, response: httpx.Response) -> ProviderError:
        return ProviderError(response.text)


class Chatty(Recorded):
    """Prints its token and the tables it was asked about."""

    def check_lines(self) -> list[str]:
        return [f"recorded: token {SECRET}"]


class Logging(Recorded):
    """Logs the names it looks up."""

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        log.info("looking up %s", [ref.table for ref in refs])
        return await super().describe(refs, deadline=deadline)


class Unfolded(Recorded):
    """Keeps the catalogue's case for column names."""

    fold = False


class Swallowing(Recorded):
    """Answers nothing when the catalogue refuses it, instead of saying so."""

    async def _get(self, path, params):
        try:
            return await super()._get(path, params)
        except ProviderRefused:
            return None


class Eager(Recorded):
    """Asks the catalogue about every catalog, mapped or not."""

    def maps(self, catalog: str) -> bool:
        return catalog != NAMES.unmapped_catalog

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        await self._get("/v1/table", {"catalog": "any", "schema": "any", "table": "any"})
        return await super().describe(refs, deadline=deadline)


class Ungated(Recorded):
    """Serves every set the catalogue holds, whatever its settings allow."""

    def __init__(self, env, transport):
        super().__init__(env, transport)
        self._sets = {
            GLOSSARY: frozenset({"finance", "hr"}),
            CLASSIFICATION: frozenset({"sensitivity", "internal-only"}),
            PROPERTY: frozenset({"retention", "cost"}),
            OWNER: frozenset({"owner"}),
        }


class Valued(Recorded):
    """Hands back a subclass of a core type that carries a sample of the data."""

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        found = await super().describe(refs, deadline=deadline)
        return {ref: _WithValues(**vars(context)) for ref, context in found.items()}


class _WithValues(TableContext):
    pass


@pytest.mark.parametrize(
    ("kind", "check", "says"),
    [
        (Leaky, "test_logs_and_check_lines_never_hold_the_secret_or_an_object_name", "secret"),
        (Chatty, "test_logs_and_check_lines_never_hold_the_secret_or_an_object_name", "secret"),
        (Logging, "test_logs_and_check_lines_never_hold_the_secret_or_an_object_name", "name"),
        (Unfolded, "test_returned_names_are_folded_as_trino_lists_them", "folded"),
        (Swallowing, "test_a_401_raises_refused_and_is_not_retried", "ProviderRefused"),
        (Eager, "test_an_unmapped_catalog_sends_no_request", "unmapped"),
        (Ungated, "test_served_sets_reflect_the_configuration", "configuration"),
        (Ungated, "test_nothing_outside_served_sets_is_returned", "served"),
        (Valued, "test_no_type_carries_a_data_value", "core types"),
    ],
)
def test_the_contract_catches_a_provider_that_breaks_a_rule(kind, check, says):
    with pytest.raises(pytest.fail.Exception, match=says) as failed:
        getattr(ContextContract(), check)(harness_of(kind))
    assert SECRET not in str(failed.value)


@pytest.mark.parametrize(
    ("names", "says"),
    [
        (dict(described=TableRef("warehouse", "Ledger", "invoices")), "folded"),
        (dict(unmapped_catalog="warehouse"), "unmapped"),
        (dict(trap=TableRef("warehouse", "ledger", "plain")), "trap"),
    ],
)
def test_the_contract_refuses_names_that_cannot_test_anything(names, says):
    harness = harness_of(Recorded)
    harness.names = ContractNames(**{**vars(NAMES), **names})
    with pytest.raises(pytest.fail.Exception, match=says):
        ContextContract().test_describe_returns_the_context_partition_columns_included(harness)


def test_the_contract_refuses_a_secret_too_short_to_be_found():
    harness = harness_of(Recorded)
    harness.secret = "s"
    with pytest.raises(pytest.fail.Exception, match="secret"):
        ContextContract().test_trap_names_are_looked_up_as_trino_lists_them(harness)
