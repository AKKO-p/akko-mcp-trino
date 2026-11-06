"""The context a tool serves for one table: gated twice, merged, referenced, bounded."""

import json
import logging
import time
from dataclasses import replace
from types import MappingProxyType

import pytest

from akko_mcp_trino.audit import bind_call, reset_call
from akko_mcp_trino.auth import Principal
from akko_mcp_trino.context import (
    CLASSIFICATION,
    GLOSSARY,
    ColumnContext,
    Gathered,
    Join,
    Labelled,
    TableContext,
    TableRef,
    Term,
    TermRef,
)
from akko_mcp_trino.context.reply import ContextService
from akko_mcp_trino.context.text import NAME_CHARS
from akko_mcp_trino.context.visibility import Visibility
from akko_mcp_trino.identity import (
    current_subject,
    reset_current_principal,
    set_current_principal,
)
from tests.context.memory_provider import Memory, setup_for
from tests.context.test_text import _Reads
from tests.context.test_visibility import FakeTrino
from tests.test_core_alone import _config

ORDERS = TableRef("demo", "sales", "orders")
CUSTOMERS = TableRef("demo", "sales", "customers")
SERVED = {GLOSSARY: frozenset({"finance"}), CLASSIFICATION: frozenset({"pii"})}


def _columns(**columns):
    return MappingProxyType(columns)


def _trino(**over):
    values = {
        "schemas": {"demo": ["sales"]},
        "tables": {("demo", "sales"): ["orders", "customers"]},
        "columns": {("demo", "sales", "customers"): ["id", "name"]},
    }
    values.update(over)
    return FakeTrino(**values)


@pytest.fixture
def made():
    setups = []
    yield setups
    for setup in setups:
        setup.close()


def _service(made, *providers, trino=None, ttl=60.0, canary="", **settings):
    config = replace(_config(), **settings)
    setup = setup_for(config, *providers)
    made.append(setup)
    fake = trino if trino is not None else _trino()
    visibility = Visibility(
        fake,
        ttl_seconds=ttl,
        max_bytes=config.visibility_cache_bytes,
        max_names=config.visibility_max_names,
        canary=canary,
    )
    return ContextService(setup.chain, visibility, config, subject=current_subject), fake


def _context_of(out):
    return {k: out[k] for k in ("table", "column_context", "terms")}


def test_a_term_on_300_columns_is_defined_once(made):
    names = [f"c{n:03d}" for n in range(300)]
    columns = {n: ColumnContext(terms=("guid-amount",)) for n in names}
    term = Term("guid-amount", "Amount", "finance", definition="The gross amount, in euros.")
    provider = Memory(
        tables={ORDERS: TableContext(columns=_columns(**columns))},
        terms={"guid-amount": term},
        sets=SERVED,
    )
    service, _ = _service(made, provider)
    out = service.for_table(ORDERS, names, complete=True)
    assert out["terms"] == {
        "t1": {
            "name": "Amount",
            "glossary": "finance",
            "definition": "The gross amount, in euros.",
            "source": "catalog",
        }
    }
    assert all(
        out["column_context"][n]["terms"] == [{"name": "Amount", "ref": "t1"}] for n in names
    )
    assert json.dumps(out).count("The gross amount") == 1
    assert provider.term_calls == [("guid-amount",)], "read once"
    assert out["context_sources"] == {"catalog": "ok"}


def test_catalogue_keys_are_never_served(made):
    related = TermRef("guid-REL-456", "Net amount", "finance", "seeAlso")
    terms = {
        "guid-SECRET-123": Term("guid-SECRET-123", "Gross", "finance", relations=(related,)),
        "guid-REL-456": Term("guid-REL-456", "Net amount", "finance"),
    }
    provider = Memory(
        tables={
            ORDERS: TableContext(
                terms=("guid-SECRET-123",),
                columns=_columns(net=ColumnContext(terms=("guid-REL-456",))),
            )
        },
        terms=terms,
        sets=SERVED,
    )
    service, _ = _service(made, provider)
    out = service.for_table(ORDERS, ["net"], complete=True)
    text = json.dumps(out)
    assert "guid-" not in text and '"key"' not in text
    assert out["table"]["terms"] == [{"name": "Gross", "ref": "t1"}]
    assert out["column_context"]["net"]["terms"] == [{"name": "Net amount", "ref": "t2"}]


def test_terms_are_referenced_in_order_of_first_appearance_and_related_by_their_served_name(made):
    first = TermRef("k-b", "a name only the provider gives", "finance", "seeAlso")
    unattached = TermRef("k-hidden", "Hidden term", "finance", "isA")
    terms = {
        "k-a": Term("k-a", "Alpha", "finance", relations=(first, unattached)),
        "k-b": Term("k-b", "Beta", "finance", categories=("Ledger",)),
        "k-c": Term("k-c", "Gamma", "finance"),
    }
    provider = Memory(
        tables={
            ORDERS: TableContext(
                columns=_columns(
                    a=ColumnContext(terms=("k-c",)),
                    b=ColumnContext(terms=("k-a", "k-b")),
                )
            )
        },
        terms=terms,
        sets=SERVED,
    )
    service, _ = _service(made, provider)
    out = service.for_table(ORDERS, ["a", "b"], complete=True)
    assert [t["name"] for t in out["terms"].values()] == ["Gamma", "Alpha", "Beta"]
    assert list(out["terms"]) == ["t1", "t2", "t3"]
    assert out["terms"]["t2"]["related"] == [{"ref": "t3", "name": "Beta", "relation": "seeAlso"}]
    assert out["terms"]["t3"]["categories"] == ["Ledger"]
    text = json.dumps(out)
    assert "Hidden term" not in text and "only the provider" not in text


def test_the_first_pass_is_never_served(made):
    """A key the first pass kept is served only once its term is read in a served glossary."""
    terms = {
        "k-kept": Term("k-kept", "Kept", "finance"),
        "k-other": Term("k-other", "Elsewhere", "restricted"),
    }
    provider = Memory(
        tables={
            ORDERS: TableContext(
                terms=("k-unknown", "k-other", "k-kept"),
                columns=_columns(id=ColumnContext(terms=("k-unknown",))),
            )
        },
        terms=terms,
        sets=SERVED,
    )
    service, _ = _service(made, provider)
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert out["table"]["terms"] == [{"name": "Kept", "ref": "t1"}]
    assert out["column_context"] == {}
    assert list(out["terms"]) == ["t1"] and "Elsewhere" not in json.dumps(out)


def test_terms_are_asked_per_source_with_that_sources_kept_keys(made):
    a = Memory(
        "a",
        tables={ORDERS: TableContext(columns=_columns(id=ColumnContext(terms=("x",))))},
        terms={"x": Term("x", "Ex", "finance", relations=(TermRef("y", "Why", "finance"),))},
        sets=SERVED,
    )
    b = Memory(
        "b",
        tables={ORDERS: TableContext(columns=_columns(id=ColumnContext(terms=("y",))))},
        terms={"y": Term("y", "Why", "finance")},
        sets=SERVED,
    )
    service, _ = _service(made, a, b)
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert a.term_calls == [("x",)] and b.term_calls == [("y",)]
    assert "related" not in out["terms"]["t1"], "y is attached for b, not for a"
    assert out["column_context"]["id"]["terms"] == [
        {"name": "Ex", "ref": "t1"},
        {"name": "Why", "ref": "t2"},
    ]


def test_a_provider_whose_served_sets_fail_is_in_error_and_serves_nothing(made):
    broken = Memory(
        "broken",
        tables={ORDERS: TableContext(description="from broken")},
        sets=RuntimeError("no sets"),
    )
    good = Memory("good", tables={ORDERS: TableContext(description="from good")}, sets=SERVED)
    service, _ = _service(made, broken, good)
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert out["context_sources"] == {"broken": "error", "good": "ok"}
    assert out["table"]["description"] == "from good" and "from broken" not in json.dumps(out)


def _join_table():
    return TableContext(
        joins=(
            Join(("customer_id",), CUSTOMERS, ("id",)),
            Join(("customer_id",), CUSTOMERS, ("ID",)),
            Join(("customer_id",), TableRef("demo", "sales", "secret"), ("id",)),
        )
    )


def test_join_targets_are_proven_once_per_call_and_by_exact_columns(made):
    provider = Memory(tables={ORDERS: _join_table()}, sets=SERVED)
    service, fake = _service(made, provider, ttl=0.0)
    out = service.for_table(ORDERS, ["customer_id"], complete=True)
    assert out["table"]["joins"] == [
        {
            "columns": ["customer_id"],
            "table": "demo.sales.customers",
            "target_columns": ["id"],
            "source": "catalog",
        }
    ]
    assert fake.statements.count('DESCRIBE "demo"."sales"."customers"') == 1
    assert not any("secret" in sql for sql in fake.statements)


def test_the_described_table_proves_itself_and_its_columns_with_a_ttl_of_zero(made):
    table = TableContext(
        joins=(
            Join(("parent_id",), ORDERS, ("id",)),
            Join(("parent_id",), ORDERS, ("beyond_the_cap",)),
        )
    )
    provider = Memory(tables={ORDERS: table}, sets=SERVED)
    service, fake = _service(made, provider, ttl=0.0, context_require_select=True)
    out = service.for_table(ORDERS, ["id", "parent_id"], complete=True)
    assert [j["target_columns"] for j in out["table"]["joins"]] == [["id"]]
    assert fake.statements == [
        'EXPLAIN (TYPE VALIDATE) SELECT "id", "parent_id" FROM "demo"."sales"."orders"'
    ]


def test_the_response_budget_stops_the_context_and_lists_what_was_left(made):
    names = [f"c{n}" for n in range(6)]
    columns = {n: ColumnContext(description=f"{n} " + "x" * 90) for n in names}
    provider = Memory(
        tables={ORDERS: TableContext(description="orders", columns=_columns(**columns))},
        sets=SERVED,
    )
    service, _ = _service(made, provider, context_response_chars=400)
    out = service.for_table(ORDERS, names, complete=True)
    assert len(json.dumps(_context_of(out))) <= 400
    described = list(out["column_context"])
    assert described and described == names[: len(described)]
    assert out["context_truncated"] is True
    assert out["undescribed_columns"] == names[len(described) :]
    assert out["context_sources"] == {"catalog": "partial"}


def test_a_table_larger_than_the_budget_serves_nothing_and_says_so(made):
    provider = Memory(
        tables={
            ORDERS: TableContext(
                description="d" * 300, columns=_columns(id=ColumnContext(description="key"))
            )
        },
        sets=SERVED,
    )
    service, _ = _service(made, provider, context_response_chars=100)
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert _context_of(out) == {"table": {}, "column_context": {}, "terms": {}}
    assert out["context_truncated"] is True and out["undescribed_columns"] == ["id"]


def test_the_budget_counts_the_terms_a_column_brings(made):
    term = Term("k", "Amount", "finance", definition="y" * 300)
    provider = Memory(
        tables={
            ORDERS: TableContext(
                columns=_columns(
                    a=ColumnContext(description="first"),
                    b=ColumnContext(terms=("k",)),
                )
            )
        },
        terms={"k": term},
        sets=SERVED,
    )
    service, _ = _service(made, provider, context_response_chars=200)
    out = service.for_table(ORDERS, ["a", "b"], complete=True)
    assert list(out["column_context"]) == ["a"] and out["terms"] == {}
    assert out["undescribed_columns"] == ["b"]


def test_a_related_term_whose_column_was_left_out_is_not_named(made):
    terms = {
        "k-a": Term("k-a", "Alpha", "finance", relations=(TermRef("k-b", "Beta", "finance"),)),
        "k-b": Term("k-b", "Beta", "finance", definition="z" * 400),
    }
    provider = Memory(
        tables={
            ORDERS: TableContext(
                columns=_columns(a=ColumnContext(terms=("k-a",)), b=ColumnContext(terms=("k-b",)))
            )
        },
        terms=terms,
        sets=SERVED,
    )
    service, _ = _service(made, provider, context_response_chars=300)
    out = service.for_table(ORDERS, ["a", "b"], complete=True)
    assert list(out["terms"]) == ["t1"] and "related" not in out["terms"]["t1"]
    assert out["undescribed_columns"] == ["b"]


def test_at_most_the_column_bound_is_described(made):
    names = ["a", "b", "c"]
    columns = {n: ColumnContext(description=n) for n in names}
    provider = Memory(tables={ORDERS: TableContext(columns=_columns(**columns))}, sets=SERVED)
    service, _ = _service(made, provider, context_max_columns=2)
    out = service.for_table(ORDERS, names, complete=True)
    assert list(out["column_context"]) == ["a", "b"]
    assert out["context_truncated"] is True and out["undescribed_columns"] == ["c"]
    assert out["context_sources"] == {"catalog": "partial"}


def _huge() -> str:
    """A name of a million characters that may be read within its window only."""
    return _Reads("n" * 1_000_000, allowed=4 * NAME_CHARS + 1)


def test_a_term_name_past_its_bound_drops_the_term_and_other_huge_names_are_dropped(made):
    related = TermRef("k-big", "", "finance", _huge())
    terms = {
        "k-big": Term("k-big", _huge(), "finance"),
        "k-ok": Term(
            "k-ok",
            "Amount",
            "finance",
            status=_huge(),
            categories=(_huge(), "Ledger"),
            relations=(related, TermRef("k-net", "", "finance", _huge())),
        ),
        "k-net": Term("k-net", "Net", "finance"),
    }
    provider = Memory(
        tables={
            ORDERS: TableContext(
                terms=("k-big", "k-ok"),
                columns=_columns(
                    a=ColumnContext(terms=("k-big",)), b=ColumnContext(terms=("k-net",))
                ),
            )
        },
        terms=terms,
        sets=SERVED,
    )
    service, _ = _service(made, provider)
    out = service.for_table(ORDERS, ["a", "b"], complete=True)
    assert out["context_sources"] == {"catalog": "ok"}
    assert out["table"] == {"terms": [{"name": "Amount", "ref": "t1"}]}
    assert list(out["column_context"]) == ["b"], "a column whose only term is dropped"
    assert out["terms"]["t1"] == {
        "name": "Amount",
        "glossary": "finance",
        "categories": ["Ledger"],
        "related": [{"ref": "t2", "name": "Net"}],
        "source": "catalog",
    }
    assert [t["name"] for t in out["terms"].values()] == ["Amount", "Net"]


class _Counted(str):
    """A name that counts how many times it is read."""

    reads = 0

    def __iter__(self):
        type(self).reads += 1
        return str.__iter__(self)


def test_a_term_name_is_cleaned_once_per_answer(made):
    _Counted.reads = 0
    names = [f"c{n:03d}" for n in range(300)]
    columns = {n: ColumnContext(terms=("k",)) for n in names}
    provider = Memory(
        tables={ORDERS: TableContext(columns=_columns(**columns))},
        terms={"k": Term("k", _Counted("Amount"), "finance")},
        sets=SERVED,
    )
    service, _ = _service(made, provider)
    out = service.for_table(ORDERS, names, complete=True)
    assert len(out["column_context"]) == 300 and out["terms"]["t1"]["name"] == "Amount"
    assert _Counted.reads == 1


def test_a_cut_definition_says_so(made):
    term = Term("k", "Amount", "finance", definition="w" * 50, status="Approved")
    provider = Memory(tables={ORDERS: TableContext(terms=("k",))}, terms={"k": term}, sets=SERVED)
    service, _ = _service(made, provider, context_text_chars=10)
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert out["terms"]["t1"]["definition"] == "w" * 10
    assert out["terms"]["t1"]["cut"] == ["definition"]
    assert out["terms"]["t1"]["status"] == "Approved"


def test_an_incomplete_describe_says_partial_and_drops_the_grain(made):
    a = Memory("a", tables={ORDERS: TableContext(grain=("id",))}, sets=SERVED)
    b = Memory(
        "b", tables={ORDERS: TableContext(description="Orders", grain=("id", "day"))}, sets=SERVED
    )
    service, _ = _service(made, a, b)
    out = service.for_table(ORDERS, ["id", "day"], complete=False)
    assert "grain" not in out["table"] and "other_grains" not in out["table"]
    assert "grain" not in out["table"].get("sources", {})
    assert out["context_sources"] == {"a": "partial", "b": "partial"}
    whole = service.for_table(ORDERS, ["id", "day"], complete=True)
    assert whole["table"]["grain"] == ["id"] and whole["context_sources"]["a"] == "ok"


def test_not_found_and_not_mapped_are_said(made):
    known = Memory("known", tables={}, sets=SERVED)
    elsewhere = Memory("elsewhere", catalogs=("other",), sets=SERVED)
    service, _ = _service(made, known, elsewhere)
    out = service.for_table(ORDERS, ["id"], complete=False)
    assert out["context_sources"] == {"known": "not_found", "elsewhere": "not_mapped"}


def test_a_visible_canary_or_a_refused_select_serves_nothing_and_calls_no_provider(made):
    provider = Memory(tables={ORDERS: TableContext(description="d")}, sets=SERVED)
    service, _ = _service(
        made,
        provider,
        trino=_trino(canary=True, schemas={"demo": ["sales", "trap"]}),
        canary="demo.trap.canary",
    )
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert out == {
        "table": {},
        "column_context": {},
        "terms": {},
        "context_sources": {"catalog": "not_checked"},
        "context_truncated": False,
        "undescribed_columns": [],
    }
    fake = _trino()
    fake.fail = 1
    service, _ = _service(made, provider, trino=fake, context_require_select=True)
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert out["context_sources"] == {"catalog": "not_checked"}
    assert provider.calls == []


def test_the_deadline_keeps_the_reserve_of_the_call(made):
    service, _ = _service(made, Memory(), context_timeout_seconds=5.0, context_reserve_seconds=1.0)
    before = time.monotonic()
    assert before + 5.0 <= service.deadline() <= time.monotonic() + 5.0
    call, token = bind_call("describe_table", 3.0)
    try:
        assert service.deadline() <= time.monotonic() + 2.0
        call.deadline = time.monotonic() + 0.5
        assert service.deadline() is None
    finally:
        reset_call(token)


def test_less_than_the_reserve_left_calls_no_provider(made):
    provider = Memory(tables={ORDERS: TableContext(description="d")}, sets=SERVED)
    service, _ = _service(made, provider, context_reserve_seconds=5.0)
    call, token = bind_call("describe_table", 1.0)
    try:
        out = service.for_table(ORDERS, ["id"], complete=True)
    finally:
        reset_call(token)
    assert out["context_sources"] == {"catalog": "unavailable"}
    assert provider.calls == [] and out["table"] == {}


def test_a_chain_that_does_not_answer_in_time_says_unavailable(made, monkeypatch, caplog):
    provider = Memory(tables={ORDERS: TableContext(description="d")}, sets=SERVED)
    service, _ = _service(made, provider)
    chain = made[-1].chain

    def late(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(chain, "gather", late)
    assert service.for_table(ORDERS, ["id"], complete=True)["context_sources"] == {
        "catalog": "unavailable"
    }

    def broken(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(chain, "gather", broken)
    with caplog.at_level(logging.DEBUG, logger="akko_mcp_trino.context.reply"):
        out = service.for_table(ORDERS, ["id"], complete=True)
    assert out["context_sources"] == {"catalog": "error"}
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


def test_a_source_whose_terms_failed_after_its_tables_were_served_says_partial(made, monkeypatch):
    provider = Memory(
        tables={ORDERS: TableContext(description="d", terms=("k",))},
        terms={"k": Term("k", "K", "finance")},
        sets=SERVED,
    )
    service, _ = _service(made, provider)
    provider.term_failure = RuntimeError("down")
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert out["context_sources"] == {"catalog": "partial"}
    assert out["table"] == {"description": "d", "sources": {"description": "catalog"}}
    chain = made[-1].chain

    def late(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(chain, "terms", late)
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert out["context_sources"] == {"catalog": "partial"} and out["terms"] == {}


def test_a_source_that_still_served_cached_context_is_partial_never_unavailable(made, monkeypatch):
    provider = Memory(sets=SERVED)
    service, _ = _service(made, provider)
    chain = made[-1].chain
    cached = Gathered(
        tables=MappingProxyType({ORDERS: (("catalog", TableContext(description="cached")),)}),
        statuses=MappingProxyType({"catalog": "unavailable"}),
    )
    monkeypatch.setattr(chain, "gather", lambda *a, **k: cached)
    out = service.for_table(ORDERS, ["id"], complete=True)
    assert out["context_sources"] == {"catalog": "partial"}
    assert out["table"]["description"] == "cached"


def test_only_served_sets_reach_the_answer(made):
    table = TableContext(
        tags=(Labelled("PII", "pii"), Labelled("Secret", "restricted")),
        columns=_columns(email=ColumnContext(classifications=(Labelled("Secret", "restricted"),))),
    )
    service, _ = _service(made, Memory(tables={ORDERS: table}, sets=SERVED))
    out = service.for_table(ORDERS, ["email"], complete=True)
    assert out["table"]["tags"] == [{"name": "PII", "set": "pii", "source": "catalog"}]
    assert "Secret" not in json.dumps(out)


def test_the_key_of_the_proofs_is_the_effective_subject(made):
    provider = Memory(tables={ORDERS: TableContext(description="d")}, sets=SERVED)
    fake = _trino(schemas={"demo": ["sales", "trap"]})
    service, _ = _service(made, provider, trino=fake, canary="demo.trap.canary")
    for subject in ("carol", "carol", "dave"):
        token = set_current_principal(Principal(subject=subject))
        try:
            service.for_table(ORDERS, ["id"], complete=True)
        finally:
            reset_current_principal(token)
    canary_checks = [sql for sql in fake.statements if "canary" in sql]
    assert len(canary_checks) == 2, "one per subject, kept for the TTL"


# ---- for_tables: the short description of each table a listing just gave

REFUNDS = TableRef("demo", "sales", "refunds")


def test_for_tables_serves_one_cleaned_and_cut_description_per_table_and_its_source(made):
    first = Memory(
        "first",
        tables={ORDERS: TableContext(description="Orders\u202e of the shop", owner="bob")},
        sets=SERVED,
    )
    second = Memory(
        "second",
        tables={
            ORDERS: TableContext(description="Other words"),
            CUSTOMERS: TableContext(description="x" * 30),
        },
        sets=SERVED,
    )
    service, fake = _service(made, first, second, context_text_chars=20)
    out = service.for_tables([ORDERS, CUSTOMERS, REFUNDS], listed=True)
    assert out == {
        "descriptions": {
            ORDERS: {"text": "Orders of the shop", "source": "first"},
            CUSTOMERS: {"text": "x" * 20, "source": "second", "cut": ["text"]},
        },
        "context_sources": {"first": "ok", "second": "ok"},
    }
    assert first.calls == [(ORDERS, CUSTOMERS, REFUNDS)], "one read of the tables, by key"
    assert fake.statements == [], "Trino just listed them: nothing is asked of it"


def test_for_tables_without_time_left_calls_no_provider(made):
    provider = Memory(tables={ORDERS: TableContext(description="d")}, sets=SERVED)
    service, _ = _service(made, provider, context_reserve_seconds=5.0)
    call, token = bind_call("describe_schema", 1.0)
    try:
        out = service.for_tables([ORDERS], listed=True)
    finally:
        reset_call(token)
    assert out == {"descriptions": {}, "context_sources": {"catalog": "unavailable"}}
    assert provider.calls == []


def test_for_tables_says_the_chains_failure_by_status(made, monkeypatch, caplog):
    service, _ = _service(made, Memory(tables={ORDERS: TableContext(description="d")}))
    chain = made[-1].chain

    def late(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(chain, "gather", late)
    out = service.for_tables([ORDERS], listed=True)
    assert out == {"descriptions": {}, "context_sources": {"catalog": "unavailable"}}

    def broken(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(chain, "gather", broken)
    with caplog.at_level(logging.DEBUG, logger="akko_mcp_trino.context.reply"):
        out = service.for_tables([ORDERS], listed=True)
    assert out["context_sources"] == {"catalog": "error"}
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


def test_for_tables_a_source_whose_sets_fail_serves_nothing(made):
    broken = Memory(
        "broken",
        tables={ORDERS: TableContext(description="from broken")},
        sets=RuntimeError("no sets"),
    )
    good = Memory("good", tables={CUSTOMERS: TableContext(description="from good")}, sets=SERVED)
    service, _ = _service(made, broken, good)
    out = service.for_tables([ORDERS, CUSTOMERS], listed=True)
    assert out == {
        "descriptions": {CUSTOMERS: {"text": "from good", "source": "good"}},
        "context_sources": {"broken": "error", "good": "ok"},
    }


def test_for_tables_a_source_that_served_cached_context_is_partial(made, monkeypatch):
    service, _ = _service(made, Memory(sets=SERVED))
    cached = Gathered(
        tables=MappingProxyType({ORDERS: (("catalog", TableContext(description="cached")),)}),
        statuses=MappingProxyType({"catalog": "unavailable"}),
    )
    monkeypatch.setattr(made[-1].chain, "gather", lambda *a, **k: cached)
    out = service.for_tables([ORDERS], listed=True)
    assert out["context_sources"] == {"catalog": "partial"}
    assert out["descriptions"] == {ORDERS: {"text": "cached", "source": "catalog"}}


def test_for_tables_with_a_visible_canary_serves_nothing(made):
    provider = Memory(tables={ORDERS: TableContext(description="d")}, sets=SERVED)
    service, _ = _service(
        made,
        provider,
        trino=_trino(canary=True, schemas={"demo": ["sales", "trap"]}),
        canary="demo.trap.canary",
    )
    out = service.for_tables([ORDERS], listed=True)
    assert out == {"descriptions": {}, "context_sources": {"catalog": "not_checked"}}
    assert provider.calls == []


def test_for_tables_not_listed_reads_only_the_tables_trino_proves(made):
    """Without ``listed``, a table is read only once Trino proves it to the caller, by its
    schema's listing; the others are not named to Trino nor asked of the catalogue."""
    payroll = TableRef("demo", "sales", "payroll")
    provider = Memory(
        tables={ORDERS: TableContext(description="d"), payroll: TableContext(description="p")},
        sets=SERVED,
    )
    service, fake = _service(made, provider, ttl=0.0)
    out = service.for_tables([ORDERS, payroll], listed=False)
    assert out == {
        "descriptions": {ORDERS: {"text": "d", "source": "catalog"}},
        "context_sources": {"catalog": "ok"},
    }
    assert provider.calls == [(ORDERS,)] and "payroll" not in " ".join(fake.statements)
    assert service.for_tables([payroll], listed=False) == {
        "descriptions": {},
        "context_sources": {"catalog": "not_found"},
    }
    assert provider.calls == [(ORDERS,)]
    before = len(fake.statements)
    assert service.for_tables([], listed=False) == {"descriptions": {}}, "nothing asked"
    assert provider.calls == [(ORDERS,)] and len(fake.statements) == before


class _NeverRead(str):
    """A description that must never be read: no listing serves it."""

    def __iter__(self):
        raise AssertionError("a description that is not served was read")


def test_for_tables_reads_only_the_description_it_serves(made):
    first = Memory(
        "first",
        tables={
            ORDERS: TableContext(description="d"),
            CUSTOMERS: TableContext(description="\u200b"),
        },
        sets=SERVED,
    )
    second = Memory(
        "second",
        tables={
            ORDERS: TableContext(description=_NeverRead("other")),
            CUSTOMERS: TableContext(description="c"),
        },
        sets=SERVED,
    )
    service, _ = _service(made, first, second)
    out = service.for_tables([ORDERS, CUSTOMERS], listed=True)
    assert out == {
        "descriptions": {
            ORDERS: {"text": "d", "source": "first"},
            CUSTOMERS: {"text": "c", "source": "second"},
        },
        "context_sources": {"first": "ok", "second": "ok"},
    }


def test_the_readers_deadline_is_taken_after_its_proofs(made):
    """The proofs of a plugin's tables spend the call's time: the deadline is read after
    them, and none left calls no provider."""
    provider = Memory(tables={ORDERS: TableContext(description="d")}, sets=SERVED)
    fake = _trino()
    call, token = bind_call("plugin", 10.0)

    def slow(sql, **keywords):
        call.deadline = time.monotonic() + 0.1
        return fake(sql, **keywords)

    try:
        service, _ = _service(made, provider, trino=slow, context_reserve_seconds=0.5)
        out = service.for_tables([ORDERS], listed=False)
    finally:
        reset_call(token)
    assert out == {"descriptions": {}, "context_sources": {"catalog": "unavailable"}}
    assert provider.calls == [] and fake.statements, "proven first, then no time left"
