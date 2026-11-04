"""The file provider: a catalogue kept in one JSON file, the reference for the contract."""

import asyncio
import json
import logging
import os
import threading
import time
from importlib.metadata import entry_points

import pytest

from akko_mcp_trino.context import (
    CLASSIFICATION,
    CONTEXT_API,
    GLOSSARY,
    OWNER,
    PROPERTY,
    ContextProvider,
    Labelled,
    TableRef,
)
from akko_mcp_trino.context import file as file_module
from akko_mcp_trino.context.errors import ProviderUnavailable
from akko_mcp_trino.context.file import ENV_FILE, FileProvider, from_env
from akko_mcp_trino.context.loading import ENTRY_POINT_GROUP, load_providers
from tests.test_core_alone import _config

ORDERS = TableRef("demo", "sales", "orders")

DOC = {
    "version": 1,
    "catalogs": ["Demo"],
    "tables": [
        {
            "catalog": "Demo",
            "schema": "Sales",
            "table": "Orders",
            "description": "One row per order",
            "owner": "sales-team",
            "tier": "gold",
            "tags": [{"name": "internal", "set": "visibility"}, {"name": "pii", "set": "other"}],
            "terms": ["t-order", "t-hidden"],
            "properties": {
                "retention": {"value": "7y", "set": "policy"},
                "cost": {"value": "high", "set": "finance"},
            },
            "grain": ["Order_Id"],
            "joins": [
                {
                    "columns": ["Customer_Id"],
                    "target": {"catalog": "demo", "schema": "sales", "table": "Customers"},
                    "target_columns": ["Id"],
                }
            ],
            "columns": {
                "Order_Id": {
                    "description": "The order",
                    "classifications": [{"name": "key", "set": "visibility"}],
                    "terms": ["t-order"],
                    "properties": {"unit": {"value": "none", "set": "policy"}},
                },
                "Customer_Id": {},
            },
        }
    ],
    "terms": {
        "t-order": {
            "name": "Order",
            "glossary": "sales",
            "definition": "A purchase",
            "status": "approved",
            "categories": ["core"],
            "relations": [
                {"key": "t-hidden", "name": "Secret", "glossary": "hr", "relation": "related"},
                {"key": "t-line", "name": "Line", "glossary": "sales", "relation": "part"},
            ],
        },
        "t-hidden": {"name": "Secret", "glossary": "hr"},
    },
}


def _write(path, doc, mtime=None):
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    if mtime is not None:
        os.utime(path, ns=(mtime, mtime))
    return str(path)


def _env(path, **extra):
    return {
        ENV_FILE: str(path),
        "MCP_CONTEXT_FILE_GLOSSARIES": "sales",
        "MCP_CONTEXT_FILE_CLASSIFICATIONS": "visibility",
        "MCP_CONTEXT_FILE_PROPERTIES": "policy",
        **extra,
    }


def _provider(tmp_path, doc=DOC, **extra):
    return from_env(_env(_write(tmp_path / "ctx.json", doc), **extra))


def _later():
    return time.monotonic() + 5


def _describe(provider, refs=(ORDERS,), deadline=None):
    return asyncio.run(provider.describe(list(refs), deadline=deadline or _later()))


def _terms(provider, keys, deadline=None):
    return asyncio.run(provider.terms(list(keys), deadline=deadline or _later()))


# ---- the contract


def test_the_provider_speaks_the_contract(tmp_path):
    provider = _provider(tmp_path)
    assert isinstance(provider, ContextProvider)
    assert provider.name == "file" and provider.context_api == CONTEXT_API
    assert provider.credentials_stamp() == ""
    asyncio.run(provider.aclose())


def test_the_entry_point_is_registered_and_loads(tmp_path):
    found = {ep.name: ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)}
    assert found["file"] == "akko_mcp_trino.context.file:from_env"
    path = _write(tmp_path / "ctx.json", DOC)
    got = load_providers(_config(context_providers="file"), {ENV_FILE: path})
    assert [p.name for p in got] == ["file"]


# ---- the settings


def test_the_file_is_required(tmp_path):
    with pytest.raises(ValueError, match="MCP_CONTEXT_FILE is not set"):
        from_env({})


def test_a_missing_or_unreadable_file_refuses_to_start(tmp_path):
    with pytest.raises(ValueError, match=r"MCP_CONTEXT_FILE: cannot read .*FileNotFoundError"):
        from_env({ENV_FILE: str(tmp_path / "nope.json")})
    path = tmp_path / "bad.json"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ValueError, match=r"cannot read .*UnicodeDecodeError"):
        from_env({ENV_FILE: str(path)})


def test_the_owner_switch_is_a_boolean(tmp_path):
    with pytest.raises(ValueError, match="MCP_CONTEXT_FILE_SERVE_OWNER"):
        _provider(tmp_path, MCP_CONTEXT_FILE_SERVE_OWNER="maybe")
    assert _provider(tmp_path, MCP_CONTEXT_FILE_SERVE_OWNER="TRUE")
    assert _provider(tmp_path, MCP_CONTEXT_FILE_SERVE_OWNER="false")


# ---- what is served


def test_the_file_serves_only_the_declared_sets(tmp_path):
    provider = _provider(tmp_path)
    assert provider.served_sets() == {
        GLOSSARY: frozenset({"sales"}),
        CLASSIFICATION: frozenset({"visibility"}),
        PROPERTY: frozenset({"policy"}),
        OWNER: frozenset(),
    }
    wide = _provider(
        tmp_path,
        MCP_CONTEXT_FILE_GLOSSARIES="sales, hr ,,nothing-declared",
        MCP_CONTEXT_FILE_CLASSIFICATIONS="visibility,other",
        MCP_CONTEXT_FILE_PROPERTIES="policy,finance",
        MCP_CONTEXT_FILE_SERVE_OWNER="true",
    )
    assert wide.served_sets() == {
        GLOSSARY: frozenset({"sales", "hr"}),
        CLASSIFICATION: frozenset({"visibility", "other"}),
        PROPERTY: frozenset({"policy", "finance"}),
        OWNER: frozenset({"owner"}),
    }


def test_nothing_is_served_by_default(tmp_path):
    provider = from_env({ENV_FILE: _write(tmp_path / "ctx.json", DOC)})
    assert all(not sets for sets in provider.served_sets().values())
    ctx = _describe(provider)[ORDERS]
    assert ctx.description == "One row per order" and ctx.grain == ("order_id",)
    assert ctx.owner == "" and ctx.tags == () and ctx.terms == () and not ctx.properties


def test_a_star_is_not_a_wildcard(tmp_path, caplog):
    doc = json.loads(json.dumps(DOC))
    doc["terms"]["t-star"] = {"name": "Star", "glossary": "*"}
    doc["tables"][0]["terms"].append("t-star")
    provider = _provider(tmp_path, doc, MCP_CONTEXT_FILE_GLOSSARIES="*")
    assert provider.served_sets()[GLOSSARY] == frozenset({"*"})
    assert _describe(provider)[ORDERS].terms == ("t-star",)
    assert set(_terms(provider, ["t-order", "t-star"])) == {"t-star"}
    assert any("'*'" in line or "glossaries: *" in line for line in provider.check_lines())
    only_star = _provider(tmp_path, DOC, MCP_CONTEXT_FILE_GLOSSARIES="*")
    assert only_star.served_sets()[GLOSSARY] == frozenset()


def test_describe_returns_the_table_with_names_folded_and_only_served_things(tmp_path):
    provider = _provider(tmp_path, MCP_CONTEXT_FILE_SERVE_OWNER="true")
    ctx = _describe(provider)[ORDERS]
    assert ctx.description == "One row per order"
    assert ctx.owner == "sales-team" and ctx.tier == "gold"
    assert ctx.tags == (Labelled("internal", "visibility"),)
    assert ctx.terms == ("t-order",)
    assert dict(ctx.properties) == {"retention": Labelled("7y", "policy")}
    assert ctx.grain == ("order_id",)
    (join,) = ctx.joins
    assert join.columns == ("customer_id",)
    assert join.target == TableRef("demo", "sales", "customers")
    assert join.target_columns == ("id",)
    assert set(ctx.columns) == {"order_id", "customer_id"}
    column = ctx.columns["order_id"]
    assert column.description == "The order"
    assert column.classifications == (Labelled("key", "visibility"),)
    assert column.terms == ("t-order",)
    assert dict(column.properties) == {"unit": Labelled("none", "policy")}
    assert ctx.columns["customer_id"].description == ""


def test_the_owner_is_not_served_unless_asked(tmp_path):
    assert _describe(_provider(tmp_path))[ORDERS].owner == ""


def test_describe_answers_only_for_the_tables_it_holds(tmp_path):
    provider = _provider(tmp_path)
    other = TableRef("demo", "sales", "nothing")
    assert set(_describe(provider, [ORDERS, other])) == {ORDERS}
    assert _describe(provider, []) == {}


def test_terms_are_read_by_key_and_filtered_to_the_served_glossaries(tmp_path):
    provider = _provider(tmp_path)
    got = _terms(provider, ["t-order", "t-hidden", "unknown"])
    assert set(got) == {"t-order"}
    term = got["t-order"]
    assert (term.name, term.glossary, term.definition, term.status) == (
        "Order",
        "sales",
        "A purchase",
        "approved",
    )
    assert term.categories == ("core",)
    assert [(r.key, r.glossary, r.relation) for r in term.relations] == [
        ("t-line", "sales", "part")
    ]
    assert _terms(provider, []) == {}


def test_an_unlisted_catalog_is_not_mapped(tmp_path):
    provider = _provider(tmp_path)
    assert provider.maps("demo") and provider.maps("DEMO")
    assert not provider.maps("other")
    doc = json.loads(json.dumps(DOC))
    del doc["catalogs"]
    assert not _provider(tmp_path, doc).maps("demo")


def test_check_lines_name_the_file_and_the_sets_never_the_content(tmp_path):
    provider = _provider(tmp_path, MCP_CONTEXT_FILE_SERVE_OWNER="true")
    text = "\n".join(provider.check_lines())
    assert "file:" in text and "ctx.json" in text
    assert "glossaries: sales" in text and "classifications: visibility" in text
    assert "properties: policy" in text and "owner: served" in text
    assert "One row per order" not in text and "7y" not in text
    assert "owner: not served" in "\n".join(_provider(tmp_path).check_lines())
    empty = from_env({ENV_FILE: _write(tmp_path / "e.json", DOC)})
    assert "glossaries: none" in "\n".join(empty.check_lines())


# ---- the format


def test_a_file_with_values_is_refused(tmp_path):
    for key in ("values", "samples", "Values"):
        doc = json.loads(json.dumps(DOC))
        doc["tables"][0]["columns"]["Order_Id"][key] = ["a"]
        with pytest.raises(ValueError, match="MCP_CONTEXT_FILE: .*carries data"):
            _provider(tmp_path, doc)
    doc = json.loads(json.dumps(DOC))
    doc["terms"]["t-order"]["relations"][0]["samples"] = []
    with pytest.raises(ValueError, match="carries data"):
        _provider(tmp_path, doc)
    doc = json.loads(json.dumps(DOC))
    doc["tables"][0]["extra"] = [{"x": {"values": 1}}]
    with pytest.raises(ValueError, match="carries data"):
        _provider(tmp_path, doc)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=2),
        lambda d: d.pop("version"),
        lambda d: d.update(catalogs="demo"),
        lambda d: d.update(catalogs=[1]),
        lambda d: d.update(tables={}),
        lambda d: d["tables"].append("x"),
        lambda d: d["tables"][0].pop("schema"),
        lambda d: d["tables"][0].update(table=3),
        lambda d: d["tables"][0].update(description=3),
        lambda d: d["tables"][0].update(tags=["x"]),
        lambda d: d["tables"][0].update(tags=[{"name": "a"}]),
        lambda d: d["tables"][0].update(terms="t-order"),
        lambda d: d["tables"][0].update(properties=[]),
        lambda d: d["tables"][0].update(properties={"k": {"value": "v"}}),
        lambda d: d["tables"][0].update(grain="id"),
        lambda d: d["tables"][0].update(joins={}),
        lambda d: d["tables"][0]["joins"][0].pop("target"),
        lambda d: d["tables"][0]["joins"][0].update(columns=[]),
        lambda d: d["tables"][0]["joins"][0].update(target_columns=["a", "b"]),
        lambda d: d["tables"][0].update(columns=[]),
        lambda d: d["tables"][0]["columns"].update(Z=[]),
        lambda d: d.update(terms=[]),
        lambda d: d["terms"].update(k={"name": "n"}),
        lambda d: d["terms"].update(k={"name": "n", "glossary": "g", "categories": "c"}),
        lambda d: d["terms"].update(k={"name": "n", "glossary": "g", "relations": ["x"]}),
        lambda d: d["terms"]["t-order"]["relations"][0].pop("key"),
    ],
)
def test_a_malformed_file_is_refused_without_its_text(tmp_path, mutate):
    doc = json.loads(json.dumps(DOC))
    mutate(doc)
    with pytest.raises(ValueError, match="MCP_CONTEXT_FILE: .* is not a valid context file"):
        _provider(tmp_path, doc)


def test_a_file_that_is_not_json_or_not_an_object_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not a valid context file"):
        _provider(tmp_path, "{not json")
    with pytest.raises(ValueError, match="not a valid context file"):
        _provider(tmp_path, "[]")


def test_the_same_table_twice_once_folded_is_refused(tmp_path):
    doc = json.loads(json.dumps(DOC))
    twin = json.loads(json.dumps(doc["tables"][0]))
    twin["table"] = "ORDERS"
    doc["tables"].append(twin)
    with pytest.raises(ValueError, match="same table twice"):
        _provider(tmp_path, doc)


def test_the_same_column_twice_once_folded_is_refused(tmp_path):
    doc = json.loads(json.dumps(DOC))
    doc["tables"][0]["columns"]["ORDER_ID"] = {}
    with pytest.raises(ValueError, match="same column twice"):
        _provider(tmp_path, doc)


def test_optional_fields_may_be_left_out(tmp_path):
    doc = {
        "version": 1,
        "tables": [{"catalog": "a", "schema": "b", "table": "c"}],
        "terms": {"k": {"name": "K", "glossary": "g"}},
    }
    provider = _provider(tmp_path, doc, MCP_CONTEXT_FILE_GLOSSARIES="g")
    ctx = _describe(provider, [TableRef("a", "b", "c")])[TableRef("a", "b", "c")]
    assert ctx.description == "" and ctx.columns == {} and ctx.joins == ()
    assert _terms(provider, ["k"])["k"].relations == ()


# ---- reading again


def test_the_file_is_read_again_when_it_changes(tmp_path):
    path = tmp_path / "ctx.json"
    provider = from_env(_env(_write(path, DOC, 1_000_000_000)))
    assert _describe(provider)[ORDERS].description == "One row per order"
    doc = json.loads(json.dumps(DOC))
    doc["tables"][0]["description"] = "Now different"
    _write(path, doc, 2_000_000_000)
    assert _describe(provider)[ORDERS].description == "Now different"
    doc["terms"]["t-order"]["definition"] = "Changed"
    _write(path, doc, 3_000_000_000)
    assert _terms(provider, ["t-order"])["t-order"].definition == "Changed"


def test_an_unchanged_file_is_not_parsed_again(tmp_path, monkeypatch):
    path = tmp_path / "ctx.json"
    provider = from_env(_env(_write(path, DOC, 1_000_000_000)))
    calls = []
    real = file_module._parse
    monkeypatch.setattr(file_module, "_parse", lambda *a: calls.append(1) or real(*a))
    _describe(provider)
    _describe(provider)
    assert calls == []


def test_served_sets_follow_the_file_when_it_changes(tmp_path):
    path = tmp_path / "ctx.json"
    provider = from_env(_env(_write(path, DOC, 1_000_000_000)))
    assert provider.served_sets()[GLOSSARY] == frozenset({"sales"})
    doc = json.loads(json.dumps(DOC))
    doc["terms"]["t-order"]["glossary"] = "hr"
    _write(path, doc, 2_000_000_000)
    _describe(provider)  # the file is read again by a read, never by the sets themselves
    assert provider.served_sets()[GLOSSARY] == frozenset()


def test_a_changed_file_that_becomes_invalid_keeps_the_last_good_one(tmp_path, caplog):
    path = tmp_path / "ctx.json"
    provider = from_env(_env(_write(path, DOC, 1_000_000_000)))
    bad = json.loads(json.dumps(DOC))
    bad["tables"][0]["columns"]["Order_Id"]["values"] = ["secret-value"]
    with caplog.at_level(logging.WARNING):
        _write(path, bad, 2_000_000_000)
        assert _describe(provider)[ORDERS].description == "One row per order"
        assert _describe(provider)[ORDERS].description == "One row per order"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "MCP_CONTEXT_FILE" in text and str(path) in text
    assert "secret-value" not in text and "values" not in text.replace("last good", "")
    _write(path, DOC, 3_000_000_000)  # fixed: served again
    assert _describe(provider)[ORDERS].description == "One row per order"


def test_a_file_that_goes_missing_keeps_the_last_good_one(tmp_path, caplog):
    path = tmp_path / "ctx.json"
    provider = from_env(_env(_write(path, DOC, 1_000_000_000)))
    path.unlink()
    with caplog.at_level(logging.WARNING):
        assert _describe(provider)[ORDERS].description == "One row per order"
        assert _describe(provider)[ORDERS].description == "One row per order"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert provider.maps("demo")


# ---- deadline and loop


def test_a_deadline_already_passed_is_unavailable(tmp_path):
    provider = _provider(tmp_path)
    past = time.monotonic() - 1
    with pytest.raises(ProviderUnavailable):
        _describe(provider, deadline=past)
    with pytest.raises(ProviderUnavailable):
        _terms(provider, ["t-order"], deadline=past)


def test_the_file_is_read_off_the_loop(tmp_path, monkeypatch):
    path = tmp_path / "ctx.json"
    provider = from_env(_env(_write(path, DOC, 1_000_000_000)))
    threads = []
    real = file_module._parse
    monkeypatch.setattr(
        file_module, "_parse", lambda *a: threads.append(threading.get_ident()) or real(*a)
    )
    _write(path, DOC, 2_000_000_000)

    async def run():
        await provider.describe([ORDERS], deadline=_later())
        return threading.get_ident()

    loop_thread = asyncio.run(run())
    assert threads and loop_thread not in threads


def test_file_provider_is_a_class():
    assert FileProvider.name == "file"
