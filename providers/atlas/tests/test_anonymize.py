"""The script that makes recorded answers neutral stops on what it does not know."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

RECORDINGS = Path(__file__).parent / "recordings"
_spec = importlib.util.spec_from_file_location("anonymize", RECORDINGS / "anonymize.py")
assert _spec is not None and _spec.loader is not None
anonymize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(anonymize)

# The 2.4 recordings already carry neutral names: they are kept as they are.
NAMES = {
    "db": {"ledger": "ledger", "o'brien.été": "o'brien.été"},
    "table": {n: n for n in ("invoices", "payments", "refunds", "l'année fiscale.v2", "ghosts")},
    "cluster": {"primary": "primary"},
}


def _table_read() -> dict:
    return json.loads((RECORDINGS / "atlas-2.4" / "bulk-hive_table.json").read_text("utf-8"))


def _run(doc: dict, tmp_path: Path) -> dict:
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    (raw / "read.json").write_text(json.dumps(doc), encoding="utf-8")
    anonymize.run(raw, out, copy.deepcopy(NAMES))
    return json.loads((out / "read.json").read_text(encoding="utf-8"))


def _invoices(doc: dict) -> dict:
    return doc["response"]["body"]["entities"][0]["attributes"]


def test_an_unknown_key_stops_the_script_without_naming_it(tmp_path):
    doc = _table_read()
    _invoices(doc)["internalRiskOwner"] = "someone"
    with pytest.raises(SystemExit) as stopped:
        _run(doc, tmp_path)
    assert "internalRiskOwner" not in str(stopped.value) and "someone" not in str(stopped.value)


def test_table_parameters_and_statistics_are_dropped(tmp_path):
    doc = _table_read()
    _invoices(doc)["parameters"] = {
        "transient_lastDdlTime": "1790294400",
        "numRows": "48213",
        "totalSize": "9182736",
    }
    _invoices(doc)["numFiles"] = 17
    text = json.dumps(_run(doc, tmp_path))
    out = json.loads(text)
    assert _invoices(out)["parameters"] == {} and _invoices(out)["numFiles"] is None
    assert not any(v in text for v in ("1790294400", "48213", "9182736", "numRows"))


def test_a_numeric_string_outside_a_known_field_stops_the_script(tmp_path):
    doc = _table_read()
    _invoices(doc)["tableType"] = "48213"
    with pytest.raises(SystemExit):
        _run(doc, tmp_path)


def test_a_timestamp_in_seconds_or_milliseconds_is_rewritten(tmp_path):
    doc = _table_read()
    _invoices(doc)["createTime"] = 1790294400
    _invoices(doc)["lastAccessTime"] = 1790294400123
    out = _invoices(_run(doc, tmp_path))
    assert (out["createTime"], out["lastAccessTime"]) == (1_700_000_000, 1_700_000_000_000)


def test_a_business_attribute_named_in_drop_is_left_out(tmp_path):
    doc = _table_read()
    entity = doc["response"]["body"]["entities"][0]
    entity["businessAttributes"] = {"stewardship": {"approvalStatus": "APPROVED", "family": "x"}}
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    (raw / "read.json").write_text(json.dumps(doc), encoding="utf-8")
    names = {**copy.deepcopy(NAMES), "drop": ["family"]}
    anonymize.run(raw, out, names)
    got = json.loads((out / "read.json").read_text(encoding="utf-8"))
    (kept,) = got["response"]["body"]["entities"][0]["businessAttributes"].values()
    assert len(kept) == 1 and "family" not in json.dumps(got)
