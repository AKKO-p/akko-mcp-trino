"""The per-caller visibility cache: what Trino lists, asked without a catalogue's names."""

import logging
import re
from dataclasses import replace

import pytest

from akko_mcp_trino.config import Config
from akko_mcp_trino.context.types import TableRef
from akko_mcp_trino.context.visibility import Visibility, VisibilityKey, key_for


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


_IDENT = r'"((?:[^"]|"")+)"'
_TEXT = r"'((?:[^']|'')+)'"


def _name(quoted):
    return quoted.replace('""', '"').replace("''", "'")


class FakeTrino:
    """A ``run`` that records each statement and answers from what the caller sees."""

    def __init__(
        self, catalogs=("demo", "system"), schemas=None, tables=None, canary=False, columns=None
    ):
        self.catalogs = list(catalogs)
        self.schemas = schemas if schemas is not None else {"demo": ["crm", "sales"]}
        self.tables = (
            tables
            if tables is not None
            else {("demo", "crm"): ["customers", "secret_target"], ("demo", "sales"): ["orders"]}
        )
        self.canary = canary
        self.columns = columns if columns is not None else {}
        self.statements = []
        self.limits = []
        self.fail = 0

    def __call__(self, sql, *, row_limit=0, **_):
        self.statements.append(sql)
        self.limits.append(row_limit)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("Trino is unreachable")
        if sql == "SHOW CATALOGS":
            return self._rows(self.catalogs, row_limit)
        schemata = f"SELECT schema_name FROM {_IDENT}\\.information_schema\\.schemata"
        found = re.fullmatch(schemata, sql)
        if found:
            return self._rows(self.schemas.get(_name(found[1]), []), row_limit)
        found = re.fullmatch(
            f"SELECT table_name FROM {_IDENT}\\.information_schema\\.tables "
            f"WHERE table_schema = {_TEXT} AND table_name = {_TEXT}",
            sql,
        )
        if found:
            return self._rows([_name(found[3])] if self.canary else [], row_limit)
        found = re.fullmatch(
            f"SELECT table_name FROM {_IDENT}\\.information_schema\\.tables "
            f"WHERE table_schema = {_TEXT}",
            sql,
        )
        if found:
            catalog, schema = _name(found[1]), _name(found[2])
            return self._rows(self.tables.get((catalog, schema), []), row_limit)
        found = re.fullmatch(f"DESCRIBE {_IDENT}\\.{_IDENT}\\.{_IDENT}", sql)
        if found:
            ref = tuple(_name(found[n]) for n in (1, 2, 3))
            return self._rows(self.columns.get(ref, []), row_limit)
        if sql.startswith("EXPLAIN (TYPE VALIDATE) SELECT "):
            return self._rows(["true"], row_limit)
        raise AssertionError(f"unexpected statement: {sql}")

    @staticmethod
    def _rows(names, row_limit):
        cut = row_limit > 0 and len(names) > row_limit
        kept = names[:row_limit] if cut else names
        return {"rows": [[n] for n in kept], "truncated": cut}


BOUND = 150
KEY = VisibilityKey(mode="jwt", subject="alice", roles=(), catalog="demo")


def _visibility(fake, *, clock=None, ttl=60.0, max_names=100, canary="", max_bytes=1 << 20):
    return Visibility(
        fake,
        ttl_seconds=ttl,
        max_bytes=max_bytes,
        max_names=max_names,
        canary=canary,
        clock=clock or Clock(),
    )


def test_the_key_names_the_mode_the_subject_and_the_catalog_and_no_role(monkeypatch):
    monkeypatch.setenv("TRINO_HOST", "h")
    monkeypatch.setenv("TRINO_IDENTITY_MODE", "impersonation")
    config = Config.from_env()
    assert key_for(config, "demo", "alice") == VisibilityKey("impersonation", "alice", (), "demo")


def test_catalogs_and_schemas_are_listed_without_any_name_filter():
    fake = FakeTrino()
    vis = _visibility(fake, max_names=100)
    assert vis.catalogs(KEY) == frozenset({"demo", "system"})
    assert vis.schemas(KEY) == frozenset({"crm", "sales"})
    assert fake.statements == [
        "SHOW CATALOGS",
        'SELECT schema_name FROM "demo".information_schema.schemata',
    ]
    assert fake.limits == [101, 101], "one name past the bound tells a list was cut"


def test_a_join_target_is_proven_without_its_name_in_any_statement():
    fake = FakeTrino()
    vis = _visibility(fake)
    assert vis.proven(KEY, TableRef("demo", "crm", "secret_target"))
    assert not any("secret_target" in sql for sql in fake.statements)
    assert fake.statements[-1].endswith("WHERE table_schema = 'crm'")


def test_a_proof_is_cached_for_the_caller():
    fake = FakeTrino()
    vis = _visibility(fake)
    assert vis.proven(KEY, TableRef("demo", "crm", "customers"))
    asked = len(fake.statements)
    assert vis.proven(KEY, TableRef("demo", "crm", "secret_target"))
    assert not vis.proven(KEY, TableRef("demo", "crm", "absent"))
    assert len(fake.statements) == asked


def test_a_catalog_the_caller_does_not_list_is_never_queried():
    fake = FakeTrino()
    vis = _visibility(fake)
    assert not vis.proven(KEY, TableRef("hidden", "crm", "customers"))
    assert vis.schemas(replace(KEY, catalog="hidden")) == frozenset()
    assert not any("hidden" in sql for sql in fake.statements)


def test_a_schema_the_caller_does_not_list_is_never_queried():
    fake = FakeTrino()
    vis = _visibility(fake)
    assert not vis.proven(KEY, TableRef("demo", "hr", "salaries"))
    assert vis.tables(KEY, "hr") == frozenset()
    assert not any("hr" in sql or "salaries" in sql for sql in fake.statements)


def test_a_join_target_in_another_listed_catalog_is_proven_there():
    fake = FakeTrino(
        catalogs=["demo", "lake"],
        schemas={"demo": ["crm"], "lake": ["raw"]},
        tables={("lake", "raw"): ["events"]},
    )
    vis = _visibility(fake)
    assert vis.proven(KEY, TableRef("lake", "raw", "events"))
    assert fake.statements[-1] == (
        "SELECT table_name FROM \"lake\".information_schema.tables WHERE table_schema = 'raw'"
    )


def test_names_are_quoted_in_the_statements():
    odd = 'we"ird'
    fake = FakeTrino(catalogs=[odd], schemas={odd: ["o'brien"]}, tables={(odd, "o'brien"): ["t"]})
    vis = _visibility(fake)
    assert vis.proven(KEY, TableRef(odd, "o'brien", "t"))
    assert fake.statements[1:] == [
        'SELECT schema_name FROM "we""ird".information_schema.schemata',
        'SELECT table_name FROM "we""ird".information_schema.tables '
        "WHERE table_schema = 'o''brien'",
    ]


def test_two_roles_or_two_subjects_share_no_entry():
    fake = FakeTrino()
    vis = _visibility(fake)
    ref = TableRef("demo", "crm", "customers")
    others = [
        KEY,
        replace(KEY, subject="bob"),
        replace(KEY, roles=("analyst",)),
        replace(KEY, roles=("auditor",)),
        replace(KEY, mode="service"),
    ]
    for key in others:
        before = len(fake.statements)
        assert vis.proven(key, ref)
        assert len(fake.statements) == before + 3, key


def test_a_table_just_described_is_visible_whatever_an_older_cache_says():
    fake = FakeTrino()
    vis = _visibility(fake)
    granted = TableRef("demo", "crm", "granted_now")
    assert not vis.proven(KEY, granted), "the cached list predates the grant"
    asked = len(fake.statements)
    vis.listed(KEY, [granted])
    assert vis.proven(KEY, granted)
    assert len(fake.statements) == asked
    assert not vis.proven(replace(KEY, subject="bob"), granted), "listed for alice only"


def test_the_cache_expires_after_its_ttl_and_zero_disables_it():
    clock = Clock()
    fake = FakeTrino()
    vis = _visibility(fake, clock=clock, ttl=60.0)
    ref = TableRef("demo", "crm", "customers")
    vis.proven(KEY, ref)
    asked = len(fake.statements)
    clock.now = 100.0
    vis.proven(KEY, ref)
    assert len(fake.statements) == 2 * asked
    off = FakeTrino()
    vis = _visibility(off, clock=clock, ttl=0.0)
    vis.proven(KEY, ref)
    vis.proven(KEY, ref)
    assert len(off.statements) == 2 * asked


def test_an_entry_counts_its_key_as_well_as_its_value():
    ref = TableRef("demo", "crm", "customers")
    counted = {}
    for width in (1, 300):
        vis = _visibility(FakeTrino())
        columns = [f"column_{n:03d}" for n in range(width)]
        vis.listed(KEY, [ref])
        before = vis.bytes
        assert vis.selectable(KEY, ref, columns, listed=columns)
        counted[width] = vis.bytes - before
    assert counted[1] > len("column_000")
    assert counted[300] > 300 * len("column_000"), "a wide entry is not counted as its value"


def test_the_cache_is_bounded_in_bytes_and_an_evicted_proof_is_read_again():
    fake = FakeTrino()
    vis = _visibility(fake, max_bytes=BOUND)
    assert vis.proven(KEY, TableRef("demo", "crm", "customers"))
    assert vis.bytes <= BOUND
    assert fake.statements.count("SHOW CATALOGS") == 1
    # The lists of catalogs and schemas were evicted to keep the tables of crm.
    assert vis.proven(KEY, TableRef("demo", "sales", "orders"))
    assert fake.statements.count("SHOW CATALOGS") == 2, "read again, never assumed"
    assert not vis.proven(KEY, TableRef("demo", "crm", "absent"))
    assert not vis.proven(KEY, TableRef("demo", "hr", "salaries"))
    assert vis.bytes <= BOUND


def test_a_mixed_case_reference_is_not_proven_and_never_queried():
    fake = FakeTrino()
    vis = _visibility(fake)
    assert not vis.proven(KEY, TableRef("demo", "CRM", "Customers"))
    assert not any("CRM" in sql or "Customers" in sql for sql in fake.statements)


@pytest.mark.parametrize("warm", [0, 1, 2])
def test_a_trino_error_is_never_cached_and_proves_nothing(warm, caplog):
    fake = FakeTrino()
    vis = _visibility(fake)
    ref = TableRef("demo", "crm", "customers")
    # The error strikes the first, the second or the third statement of the proof.
    if warm > 0:
        vis.catalogs(KEY)
    if warm > 1:
        vis.schemas(KEY)
    fake.fail = 1
    with caplog.at_level(logging.DEBUG, logger="akko_mcp_trino.context.visibility"):
        assert not vis.proven(KEY, ref)
    assert "alice" not in caplog.text and "unreachable" not in caplog.text
    assert "customers" not in caplog.text
    assert vis.proven(KEY, ref), "the failed read was not kept"


def test_a_listing_that_fails_raises_and_is_read_again():
    fake = FakeTrino()
    vis = _visibility(fake)
    fake.fail = 1
    with pytest.raises(RuntimeError):
        vis.catalogs(KEY)
    assert vis.catalogs(KEY) == frozenset({"demo", "system"})


def test_a_schema_above_the_name_bound_proves_nothing_and_is_no_error():
    fake = FakeTrino(tables={("demo", "crm"): ["a", "b", "c"]})
    vis = _visibility(fake, max_names=2)
    assert vis.tables(KEY, "crm") is None
    assert not vis.proven(KEY, TableRef("demo", "crm", "a"))
    assert fake.limits[-1] == 3
    asked = len(fake.statements)
    assert vis.tables(KEY, "crm") is None
    assert len(fake.statements) == asked, "a known excess is kept like any answer"


def test_a_cut_list_of_catalogs_or_schemas_keeps_only_what_trino_returned():
    fake = FakeTrino(catalogs=["demo", "lake", "other"])
    vis = _visibility(fake, max_names=1)
    assert vis.catalogs(KEY) == frozenset({"demo"})


def test_every_statement_goes_through_the_run_it_was_given():
    seen = []

    def run(sql, **kw):
        seen.append(sql)
        return FakeTrino(canary=False)(sql, **kw)

    vis = _visibility(run, canary="demo.crm.canary_t")
    ref = TableRef("demo", "crm", "customers")
    vis.proven(KEY, ref)
    vis.canary_clear(KEY)
    vis.selectable(KEY, ref, ["id"], listed=["id"])
    assert len(seen) == 5
    assert seen[-1].startswith("EXPLAIN (TYPE VALIDATE) SELECT ")


def test_without_a_canary_nothing_is_checked():
    fake = FakeTrino()
    vis = _visibility(fake, canary="")
    assert vis.canary_clear(KEY)
    assert fake.statements == []


def test_a_hidden_canary_clears_the_caller_once_per_ttl():
    clock = Clock()
    fake = FakeTrino(canary=False)
    vis = _visibility(fake, clock=clock, canary="demo.crm.canary_t")
    assert vis.canary_clear(KEY)
    assert vis.canary_clear(replace(KEY, catalog="lake")), "one check per caller"
    assert fake.statements == [
        "SHOW CATALOGS",
        'SELECT table_name FROM "demo".information_schema.tables '
        "WHERE table_schema = 'crm' AND table_name = 'canary_t'",
    ]
    clock.now = 100.0
    assert vis.canary_clear(KEY)
    assert len(fake.statements) == 4


def test_a_canary_in_a_catalog_the_caller_does_not_list_is_clear_without_a_query():
    fake = FakeTrino(canary=True)
    vis = _visibility(fake, canary="vault.crm.canary_t")
    assert vis.canary_clear(KEY)
    assert fake.statements == ["SHOW CATALOGS"]


def test_a_visible_canary_stops_the_context_and_logs_no_user_name(caplog):
    fake = FakeTrino(canary=True)
    vis = _visibility(fake, canary="demo.crm.canary_t")
    with caplog.at_level(logging.DEBUG, logger="akko_mcp_trino.context.visibility"):
        assert not vis.canary_clear(KEY)
        assert not vis.canary_clear(KEY)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "said once per expiry of the cache"
    assert "alice" not in caplog.text
    assert len(fake.statements) == 2


def test_a_mixed_case_canary_is_read_as_trino_folds_it():
    fake = FakeTrino(catalogs=["demo"], canary=True)
    vis = _visibility(fake, canary="Demo.Secret.Canary")
    assert not vis.canary_clear(KEY)
    assert fake.statements[-1] == (
        'SELECT table_name FROM "demo".information_schema.tables '
        "WHERE table_schema = 'secret' AND table_name = 'canary'"
    )


def test_a_canary_check_that_fails_is_not_clear_and_not_cached(caplog):
    fake = FakeTrino(canary=False)
    vis = _visibility(fake, canary="demo.crm.canary_t")
    fake.fail = 1
    with caplog.at_level(logging.DEBUG, logger="akko_mcp_trino.context.visibility"):
        assert not vis.canary_clear(KEY)
    assert "alice" not in caplog.text and "unreachable" not in caplog.text
    assert vis.canary_clear(KEY)


def test_selectable_quotes_columns_and_validates_only_the_visible_ones():
    fake = FakeTrino()
    vis = _visibility(fake)
    ref = TableRef("demo", "crm", "customers")
    vis.listed(KEY, [ref])
    described = ["id", 'odd"name']
    asked = ["id", "catalogue_only", 'odd"name']
    assert vis.selectable(KEY, ref, asked, listed=described)
    assert fake.statements == [
        'EXPLAIN (TYPE VALIDATE) SELECT "id", "odd""name" FROM "demo"."crm"."customers"'
    ]
    assert vis.selectable(KEY, ref, ["id", 'odd"name'], listed=described)
    assert len(fake.statements) == 1, "cached under the columns validated"
    assert not any("catalogue_only" in sql or "*" in sql for sql in fake.statements)


def test_selectable_asks_nothing_when_no_column_was_listed():
    fake = FakeTrino()
    vis = _visibility(fake)
    ref = TableRef("demo", "crm", "customers")
    vis.listed(KEY, [ref])
    assert not vis.selectable(KEY, ref, ["catalogue_only"], listed=["id"])
    assert fake.statements == []


def test_selectable_asks_nothing_for_an_unproven_table_or_no_column():
    fake = FakeTrino()
    vis = _visibility(fake)
    hidden = TableRef("demo", "hr", "salaries")
    assert not vis.selectable(KEY, hidden, ["amount"], listed=["amount"])
    assert not any("salaries" in sql for sql in fake.statements)
    ref = TableRef("demo", "crm", "customers")
    vis.listed(KEY, [ref])
    asked = len(fake.statements)
    assert not vis.selectable(KEY, ref, [], listed=["id"])
    assert len(fake.statements) == asked


def test_a_refused_select_is_not_selectable_and_not_cached():
    fake = FakeTrino()
    vis = _visibility(fake)
    ref = TableRef("demo", "crm", "customers")
    vis.listed(KEY, [ref])
    fake.fail = 1
    assert not vis.selectable(KEY, ref, ["id"], listed=["id"])
    assert vis.selectable(KEY, ref, ["id"], listed=["id"])
    assert len(fake.statements) == 2


def test_columns_runs_describe_only_for_a_proven_table(caplog):
    odd = TableRef("demo", 'c"rm', 'cu"st')
    fake = FakeTrino(
        schemas={"demo": ['c"rm']},
        tables={("demo", 'c"rm'): ['cu"st']},
        columns={("demo", 'c"rm', 'cu"st'): ["id", "Name"], ("demo", 'c"rm', "absent"): ["x"]},
    )
    vis = _visibility(fake)
    assert vis.columns(KEY, TableRef("demo", 'c"rm', "absent")) is None
    assert vis.columns(KEY, TableRef("hidden", "crm", "customers")) is None
    assert not any(sql.startswith("DESCRIBE") for sql in fake.statements)
    assert vis.columns(KEY, odd) == frozenset({"id", "Name"})
    assert fake.statements[-1] == 'DESCRIBE "demo"."c""rm"."cu""st"'
    asked = len(fake.statements)
    assert vis.columns(replace(KEY, catalog="other"), odd) == frozenset({"id", "Name"})
    assert len(fake.statements) == asked, "kept per caller"
    assert vis.columns(replace(KEY, subject="bob"), odd) == frozenset({"id", "Name"})
    assert fake.statements.count('DESCRIBE "demo"."c""rm"."cu""st"') == 2, "not across callers"


def test_columns_of_a_table_wider_than_the_bound_prove_nothing():
    ref = TableRef("demo", "crm", "customers")
    fake = FakeTrino(columns={("demo", "crm", "customers"): ["a", "b", "c"]})
    vis = _visibility(fake, max_names=2)
    vis.listed(KEY, [ref])
    assert vis.columns(KEY, ref) is None
    assert fake.limits[-1] == 3


def test_columns_never_caches_an_error(caplog):
    ref = TableRef("demo", "crm", "customers")
    fake = FakeTrino(columns={("demo", "crm", "customers"): ["id"]})
    vis = _visibility(fake)
    vis.listed(KEY, [ref])
    fake.fail = 1
    with caplog.at_level(logging.DEBUG, logger="akko_mcp_trino.context.visibility"):
        assert vis.columns(KEY, ref) is None
    assert "RuntimeError" in caplog.text
    assert "alice" not in caplog.text and "unreachable" not in caplog.text
    assert "customers" not in caplog.text
    assert vis.columns(KEY, ref) == frozenset({"id"}), "the failed read was not kept"


def test_columns_counts_its_key_in_the_bound():
    counted = {}
    for width in (1, 300):
        ref = TableRef("demo", "crm", "t" * width)
        vis = _visibility(FakeTrino(columns={("demo", "crm", "t" * width): ["id"]}))
        vis.listed(KEY, [ref])
        before = vis.bytes
        assert vis.columns(KEY, ref) == frozenset({"id"})
        counted[width] = vis.bytes - before
    assert counted[300] - counted[1] >= 299, "the key of the entry is counted"
    ref = TableRef("demo", "crm", "customers")
    vis = _visibility(FakeTrino(columns={("demo", "crm", "customers"): ["id"]}), max_bytes=BOUND)
    assert vis.columns(KEY, ref) == frozenset({"id"})
    assert vis.bytes <= BOUND


class _Denied(Exception):
    """A Trino user error that refuses access, as the Trino client raises it."""

    error_name = "PERMISSION_DENIED"
    error_type = "USER_ERROR"


def test_a_select_trino_denies_is_kept_as_refused_for_one_ttl():
    clock = Clock()
    fake = FakeTrino()
    vis = _visibility(fake, clock=clock, ttl=60.0)
    ref = TableRef("demo", "crm", "customers")
    vis.listed(KEY, [ref])
    denied = fake.__call__

    def refuse(sql, **kw):
        if sql.startswith("EXPLAIN"):
            fake.statements.append(sql)
            raise _Denied("Access Denied: Cannot select from columns [id]")
        return denied(sql, **kw)

    vis._run = refuse
    assert not vis.selectable(KEY, ref, ["id"], listed=["id"])
    assert not vis.selectable(KEY, ref, ["id"], listed=["id"])
    assert sum(sql.startswith("EXPLAIN") for sql in fake.statements) == 1, "kept as refused"
    assert vis.selectable(replace(KEY, subject="bob"), ref, ["id"], listed=["id"]) is False
    vis._run = fake
    clock.now = 100.0
    vis.listed(KEY, [ref])
    assert vis.selectable(KEY, ref, ["id"], listed=["id"]), "asked again after the TTL"


def test_a_select_on_the_table_just_described_needs_no_other_proof():
    fake = FakeTrino()
    vis = _visibility(fake, ttl=0.0)
    ref = TableRef("demo", "hr", "salaries")
    assert vis.selectable(KEY, ref, ["amount"], listed=["amount"], described=True)
    assert fake.statements == [
        'EXPLAIN (TYPE VALIDATE) SELECT "amount" FROM "demo"."hr"."salaries"'
    ]
    assert not vis.selectable(KEY, ref, ["other"], listed=["amount"], described=True)
    assert len(fake.statements) == 1
