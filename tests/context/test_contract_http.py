"""The published contract, run on a provider over HTTP, and the faults it catches.

``Recorded`` is written the way an adapter is: it reads a small catalogue over HTTP,
keeps only the sets its settings allow, folds names as Trino lists them, respects the
deadline and turns every failure into a status-bearing error without the catalogue's
text. The variants below each break one rule, and the contract must catch them.
"""

import asyncio
import json
import logging
import time
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

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

# A quote, a backslash and an accent: escaped in JSON, encoded in a URL, it must still be found.
SECRET = 'contract-"token\\é-9d41b7c2'

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

# How the secret looks once escaped or encoded: none may appear in a failure message.
SECRET_FORMS = (
    SECRET,
    json.dumps(SECRET)[1:-1],
    quote(SECRET, safe=""),
)

ENV = {
    "REC_TOKEN": SECRET,
    "REC_CATALOGS": "warehouse",
    "REC_GLOSSARIES": "finance",
    "REC_CLASSIFICATIONS": "sensitivity",
    "REC_PROPERTIES": "retention",
    "REC_SERVE_OWNER": "true",
}


def replay(request: httpx.Request) -> httpx.Response:
    """The catalogue as recorded: a wrong token is refused, an unknown name is a 404."""
    if request.headers.get("authorization") != f"Bearer {quote(SECRET, safe='')}":
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
    deleted = "DELETED"
    partitions = "partitionColumns"

    def __init__(self, env: Mapping[str, str], transport: httpx.AsyncBaseTransport) -> None:
        token = env.get("REC_TOKEN", "")
        if not token:
            raise ValueError("REC_TOKEN is not set")
        owner = env.get("REC_SERVE_OWNER", "false")
        if owner not in ("true", "false"):
            raise ValueError("REC_SERVE_OWNER must be true or false")
        self._catalogs = _items(env, "REC_CATALOGS")
        self._sets = {
            GLOSSARY: _items(env, "REC_GLOSSARIES"),
            CLASSIFICATION: _items(env, "REC_CLASSIFICATIONS"),
            PROPERTY: _items(env, "REC_PROPERTIES"),
            OWNER: frozenset({"owner"}) if owner == "true" else frozenset(),
        }
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url="http://catalogue.test",
            headers={"authorization": f"Bearer {quote(token, safe='')}"},
        )

    def maps(self, catalog: str) -> bool:
        return catalog in self._catalogs

    def served_sets(self) -> Mapping[str, frozenset[str]]:
        return self._sets

    async def describe(
        self, refs: Sequence[TableRef], *, deadline: float, on_behalf_of: None = None
    ) -> Mapping[TableRef, TableContext]:
        mine = [ref for ref in refs if self.maps(ref.catalog)]
        found = await self._all([self._get("/v1/table", self._params(r)) for r in mine], deadline)
        return {
            ref: self._table(spec)
            for ref, spec in zip(mine, found)
            if spec and spec.get("status") != self.deleted
        }

    def _params(self, ref: TableRef) -> dict[str, str]:
        return {"catalog": ref.catalog, "schema": ref.schema, "table": ref.table}

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
        return self._json(response)

    def _json(self, response: httpx.Response) -> Any:
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
        for name in spec.get(self.partitions, []):
            columns.setdefault(self._name(name), ColumnContext())
        return TableContext(
            description=spec.get("description", ""),
            owner=spec.get("owner", "") if self._sets[OWNER] else "",
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


class Echoing(Recorded):
    """Puts a token it refuses in its ValueError."""

    def __init__(self, env, transport):
        token = env.get("REC_TOKEN", "")
        if token != token.strip() or token.startswith('"'):
            raise ValueError(f"REC_TOKEN {token!r} has stray characters around it")
        super().__init__(env, transport)


class Typing(Recorded):
    """Puts a token it cannot use in a TypeError."""

    def __init__(self, env, transport):
        token = env.get("REC_TOKEN", "")
        if token != token.strip() or token.startswith('"'):
            raise TypeError(f"REC_TOKEN {token!r} is not a usable token")
        super().__init__(env, transport)


class Chatty(Recorded):
    """Prints its token."""

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


class Remembering(Recorded):
    """Keeps the text of its last error and prints it in ``check_lines``."""

    last = ""

    def failure(self, response: httpx.Response) -> ProviderError:
        self.last = response.text
        return super().failure(response)

    def check_lines(self) -> list[str]:
        return super().check_lines() + [f"recorded: last error {self.last}"]


class Lingering(Recorded):
    """Starts a request it never waits for, nor cancels."""

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        self.pending = asyncio.create_task(self._get("/v1/table", self._params(refs[0])))
        return await super().describe(refs, deadline=deadline)


class Late(Recorded):
    """Takes its time before saying the catalogue is late."""

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        try:
            return await super().describe(refs, deadline=deadline)
        except ProviderUnavailable:
            await asyncio.sleep(1.0)
            raise


class Retrying(Recorded):
    """Sends a refused request again."""

    async def _get(self, path, params):
        try:
            return await super()._get(path, params)
        except ProviderRefused:
            return await super()._get(path, params)


class Resetting(Recorded):
    """Forgets what it remembered of its catalogue on reset(), at once and without a request."""

    def reset(self) -> None:
        self.forgotten = True


class AsyncReset(Recorded):
    """Writes reset() as a coroutine function: the server's signal handler never awaits it."""

    async def reset(self) -> None:
        self.forgotten = True


class RaisingReset(Recorded):
    """Fails in reset()."""

    def reset(self) -> None:
        raise RuntimeError("cannot forget")


class SlowReset(Recorded):
    """Blocks in reset()."""

    def reset(self) -> None:
        time.sleep(0.5)


class AnsweringReset(Recorded):
    """Returns something from reset()."""

    def reset(self) -> bool:
        return True


class AskingReset(Recorded):
    """Asks its catalogue from reset(), on the loop the provider runs on."""

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        self.loop = asyncio.get_running_loop()
        return await super().describe(refs, deadline=deadline)

    def reset(self) -> None:
        call = self._get("/v1/table", self._params(NAMES.described))
        asyncio.run_coroutine_threadsafe(call, self.loop).result(5)


class Resurrecting(Recorded):
    """Ignores that the catalogue deleted an entity."""

    deleted = "never"


class Quoting(Recorded):
    """Quotes names the SQL way before asking the catalogue, which never finds the trap."""

    def _params(self, ref: TableRef) -> dict[str, str]:
        return {key: value.replace("'", "''") for key, value in super()._params(ref).items()}


class Raw(Recorded):
    """Lets a body that is not JSON through as a ``ValueError``."""

    def _json(self, response: httpx.Response) -> Any:
        return response.json()


class Unpartitioned(Recorded):
    """Forgets the partition columns."""

    partitions = "none"


class Unfiltered(Recorded):
    """Says honestly what it serves, then forgets to filter tags and properties."""

    def _labels(self, specs: list, kind: str) -> tuple[Labelled, ...]:
        return tuple(Labelled(s["name"], s["set"]) for s in specs)

    def _properties(self, specs: dict) -> Mapping[str, Labelled]:
        return MappingProxyType({key: Labelled(s["value"], s["set"]) for key, s in specs.items()})


class OwnerAlways(Recorded):
    """Serves the owner whatever its settings say."""

    def __init__(self, env, transport):
        super().__init__(env, transport)
        self._sets[OWNER] = frozenset({"owner"})


class Sticky(Recorded):
    """Takes an allow list left empty for "everything" and serves every set it holds."""

    def _labels(self, specs: list, kind: str) -> tuple[Labelled, ...]:
        if self._sets[kind]:
            return super()._labels(specs, kind)
        return tuple(Labelled(s["name"], s["set"]) for s in specs)

    def _keys(self, specs: list) -> tuple[str, ...]:
        if self._sets[GLOSSARY]:
            return super()._keys(specs)
        return tuple(s["key"] for s in specs)


class Ownerless(Recorded):
    """A catalogue with no notion of an owner: the provider never declares one."""

    def __init__(self, env, transport):
        super().__init__(env, transport)
        self._sets[OWNER] = frozenset()


LOGS = "test_logs_and_check_lines_never_hold_the_secret_or_an_object_name"
OWNER_CHECK = "test_the_owner_follows_the_configuration"
REFLECT = "test_served_sets_reflect_the_configuration"
RESET = "test_a_reset_hook_is_synchronous_quick_and_quiet"


@pytest.mark.parametrize(
    ("kind", "check", "says"),
    [
        (Leaky, LOGS, "an exception, its causes included, must never hold the secret"),
        (
            Echoing,
            "test_a_factory_refusal_never_holds_the_secret",
            "a factory's exception must never hold the secret",
        ),
        (
            Typing,
            "test_a_factory_refusal_never_holds_the_secret",
            "a factory's exception must never hold the secret",
        ),
        (Chatty, LOGS, r"the logs and check_lines\(\) must never hold the secret"),
        (Remembering, LOGS, r"the logs and check_lines\(\) must never hold the secret"),
        (Logging, LOGS, "must never hold an object name"),
        (
            Unfolded,
            "test_returned_names_are_folded_as_trino_lists_them",
            r"lower case\): \d+ are not",
        ),
        (
            Swallowing,
            "test_a_401_raises_refused_and_is_not_retried",
            "a 401 must raise ProviderRefused",
        ),
        (Retrying, "test_a_401_raises_refused_and_is_not_retried", "must not be sent again"),
        (Eager, "test_an_unmapped_catalog_sends_no_request", "neither returned nor asked for"),
        (Ungated, REFLECT, "only when the configuration names it"),
        (Sticky, REFLECT, "without its allow lists, a provider must still describe"),
        (OwnerAlways, OWNER_CHECK, "the owner must not be served once"),
        (
            Unfiltered,
            "test_nothing_outside_served_sets_is_returned",
            "a tag or a classification must belong",
        ),
        (Valued, "test_no_type_carries_a_data_value", "exactly the core types"),
        (AsyncReset, RESET, "a plain function, not a coroutine function"),
        (RaisingReset, RESET, "reset\\(\\) must not raise"),
        (SlowReset, RESET, "reset\\(\\) must return within"),
        (AnsweringReset, RESET, "reset\\(\\) must return None"),
        (AskingReset, RESET, "reset\\(\\) must send no request"),
        (
            Resurrecting,
            "test_a_deleted_entity_is_never_returned",
            "a deleted entity must never be returned",
        ),
        (Quoting, "test_trap_names_are_looked_up_as_trino_lists_them", "trap table must be found"),
        (
            Unpartitioned,
            "test_describe_returns_the_context_partition_columns_included",
            "a partition column must come",
        ),
        (
            Raw,
            "test_a_timeout_or_bad_json_raises_unavailable",
            "'bad-json' must raise ProviderUnavailable",
        ),
        (
            Lingering,
            "test_a_slow_catalogue_returns_before_the_deadline_plus_margin_and_closes_its_connection",
            "must be cancelled, not left open",
        ),
        (
            Late,
            "test_a_slow_catalogue_returns_before_the_deadline_plus_margin_and_closes_its_connection",
            "past its deadline",
        ),
        (
            Lingering,
            "test_a_read_past_its_deadline_closes_its_connection",
            "must close its connection: 1 of 2 left open",
        ),
        (
            Lingering,
            "test_a_read_the_chain_cuts_at_its_deadline_closes_its_connection",
            "must close its connection: 1 of 2 left open",
        ),
    ],
)
def test_the_contract_catches_a_provider_that_breaks_a_rule(kind, check, says):
    with pytest.raises(pytest.fail.Exception, match=says) as failed:
        getattr(ContextContract(), check)(harness_of(kind))
    assert not any(form in str(failed.value) for form in SECRET_FORMS)


def test_the_contract_catches_a_provider_whose_module_loads_the_trino_client():
    from tests.context.heavy_provider import Heavy

    with pytest.raises(pytest.fail.Exception, match="loads what an adapter must not"):
        ContextContract().test_the_adapter_imports_neither_the_trino_client_nor_the_identity(
            harness_of(Heavy)
        )


@pytest.mark.parametrize(
    ("names", "says"),
    [
        (dict(described=TableRef("warehouse", "Ledger", "invoices")), "as TableRef.of does"),
        (dict(unmapped_catalog="warehouse"), "the unmapped catalog differs"),
        (dict(trap=TableRef("warehouse", "ledger", "plain")), "the trap name must hold"),
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
    with pytest.raises(pytest.fail.Exception, match="8 characters"):
        ContextContract().test_trap_names_are_looked_up_as_trino_lists_them(harness)


def test_a_reset_hook_that_keeps_the_rules_passes_and_none_is_skipped():
    contract = ContextContract()
    getattr(contract, RESET)(harness_of(Resetting))
    with pytest.raises(pytest.skip.Exception, match="no reset"):
        getattr(contract, RESET)(harness_of(Recorded))


def test_a_provider_without_an_owner_passes_and_says_why_the_owner_is_not_checked():
    contract, harness = ContextContract(), harness_of(Ownerless)
    contract.test_served_sets_reflect_the_configuration(harness)
    with pytest.raises(pytest.skip.Exception, match="declares no owner"):
        getattr(contract, OWNER_CHECK)(harness)
