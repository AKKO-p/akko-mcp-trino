"""The gate: what Trino does not list and what the allow list does not name is removed."""

from types import MappingProxyType

import pytest

from akko_mcp_trino.context import (
    CLASSIFICATION,
    GLOSSARY,
    OWNER,
    PROPERTY,
    ColumnContext,
    Join,
    Labelled,
    TableContext,
    TableRef,
    Term,
    TermRef,
)
from akko_mcp_trino.context.gate import filter_table, filter_term

ORDERS = TableRef("c", "s", "orders")
HIDDEN = TableRef("c", "s", "hidden")

SERVED = {
    GLOSSARY: frozenset({"finance"}),
    CLASSIFICATION: frozenset({"pii"}),
    PROPERTY: frozenset({"quality"}),
    OWNER: frozenset({"owner"}),
}


# What the fake Trino lists to the caller: each proven table with its columns.
LISTED = {ORDERS: frozenset({"id", "amount"})}


def _proven(listed):
    def proven(target: TableRef, columns: tuple[str, ...]) -> bool:
        return target in listed and all(name in listed[target] for name in columns)

    return proven


def _gate(ctx: TableContext, visible=("id", "amount"), served=None, terms=None):
    return filter_table(
        ctx,
        visible_columns=visible,
        served=SERVED if served is None else served,
        join_proven=_proven(LISTED),
        terms=terms,
    )


def _columns(**columns: ColumnContext):
    return MappingProxyType(columns)


def test_a_column_trino_does_not_list_loses_its_context():
    ctx = TableContext(
        columns=_columns(
            id=ColumnContext(description="key"),
            secret=ColumnContext(description="hidden"),
        )
    )
    assert set(_gate(ctx).columns) == {"id"}


def test_a_context_column_is_matched_exactly_never_after_folding():
    ctx = TableContext(columns=MappingProxyType({"Id": ColumnContext(description="key")}))
    assert dict(_gate(ctx).columns) == {}


def test_a_grain_with_one_invisible_column_is_emptied():
    assert _gate(TableContext(grain=("id", "secret"))).grain == ()


def test_a_grain_of_visible_columns_is_kept():
    assert _gate(TableContext(grain=("id", "amount"))).grain == ("id", "amount")


def test_a_join_to_an_unproven_table_is_removed():
    join = Join(("id",), HIDDEN, ("id",))
    assert _gate(TableContext(joins=(join,))).joins == ()


def test_a_join_naming_a_hidden_column_of_the_target_is_removed():
    join = Join(("id",), ORDERS, ("secret",))
    assert _gate(TableContext(joins=(join,))).joins == ()


def test_a_join_from_an_invisible_column_is_removed():
    join = Join(("secret",), ORDERS, ("id",))
    assert _gate(TableContext(joins=(join,))).joins == ()


def test_a_join_from_visible_columns_to_a_proven_table_is_kept():
    join = Join(("id",), ORDERS, ("id",))
    asked: list[tuple[TableRef, tuple[str, ...]]] = []

    def proven(target: TableRef, columns: tuple[str, ...]) -> bool:
        asked.append((target, columns))
        return True

    gated = filter_table(
        TableContext(joins=(join,)),
        visible_columns=("id",),
        served=SERVED,
        join_proven=proven,
        terms=None,
    )
    assert gated.joins == (join,)
    assert asked == [(ORDERS, ("id",))]


def test_the_proof_is_not_asked_for_a_join_from_an_invisible_column():
    asked: list[TableRef] = []
    filter_table(
        TableContext(joins=(Join(("secret",), HIDDEN, ("id",)),)),
        visible_columns=("id",),
        served=SERVED,
        join_proven=lambda target, columns: asked.append(target) or True,
        terms=None,
    )
    assert asked == []


def test_a_term_of_a_glossary_outside_the_allow_list_is_removed():
    ctx = TableContext(
        terms=("t-fin", "t-hr"),
        columns=_columns(id=ColumnContext(terms=("t-hr", "t-fin"))),
    )
    terms = {
        "t-fin": Term("t-fin", "Revenue", "finance"),
        "t-hr": Term("t-hr", "Salary", "hr"),
    }
    gated = _gate(ctx, terms=terms)
    assert gated.terms == ("t-fin",)
    assert gated.columns["id"].terms == ("t-fin",)


def test_a_term_key_the_provider_could_not_read_is_removed():
    ctx = TableContext(terms=("t-gone",), columns=_columns(id=ColumnContext(terms=("t-gone",))))
    gated = _gate(ctx, terms={})
    assert gated.terms == ()
    assert gated.columns["id"].terms == ()


def test_no_term_key_is_kept_when_no_glossary_is_served():
    ctx = TableContext(terms=("t-fin",), columns=_columns(id=ColumnContext(terms=("t-fin",))))
    gated = _gate(ctx, served={**SERVED, GLOSSARY: frozenset()})
    assert gated.terms == ()
    assert gated.columns["id"].terms == ()


def test_a_term_read_under_another_key_is_treated_as_unknown():
    ctx = TableContext(terms=("t-fin",), columns=_columns(id=ColumnContext(terms=("t-fin",))))
    gated = _gate(ctx, terms={"t-fin": Term("t-other", "Revenue", "finance")})
    assert gated.terms == ()
    assert gated.columns["id"].terms == ()


def test_the_pass_is_always_named():
    with pytest.raises(TypeError):
        filter_table(
            TableContext(),
            visible_columns=(),
            served=SERVED,
            join_proven=_proven(LISTED),
        )


def test_term_keys_wait_for_their_terms_when_a_glossary_is_served():
    ctx = TableContext(terms=("t-fin",), columns=_columns(id=ColumnContext(terms=("t-fin",))))
    gated = _gate(ctx)
    assert gated.terms == ("t-fin",)
    assert gated.columns["id"].terms == ("t-fin",)


def test_the_core_removes_what_a_provider_serves_outside_its_declared_sets():
    ctx = TableContext(
        tags=(Labelled("PII", "pii"), Labelled("Secret", "restricted")),
        properties=MappingProxyType(
            {"score": Labelled("high", "quality"), "budget": Labelled("1M", "finance")}
        ),
        columns=_columns(
            id=ColumnContext(
                classifications=(Labelled("PII", "pii"), Labelled("Secret", "restricted")),
                properties=MappingProxyType(
                    {"score": Labelled("high", "quality"), "budget": Labelled("1M", "finance")}
                ),
            )
        ),
    )
    gated = _gate(ctx)
    assert gated.tags == (Labelled("PII", "pii"),)
    assert dict(gated.properties) == {"score": Labelled("high", "quality")}
    column = gated.columns["id"]
    assert column.classifications == (Labelled("PII", "pii"),)
    assert dict(column.properties) == {"score": Labelled("high", "quality")}


def test_nothing_is_served_from_a_kind_the_allow_list_does_not_name():
    ctx = TableContext(
        owner="ann",
        tags=(Labelled("PII", "pii"),),
        terms=("t-fin",),
        properties=MappingProxyType({"score": Labelled("high", "quality")}),
        columns=_columns(
            id=ColumnContext(description="key", classifications=(Labelled("PII", "pii"),))
        ),
    )
    gated = _gate(ctx, served={})
    assert gated == TableContext(columns=_columns(id=ColumnContext(description="key")))


def test_an_empty_set_name_serves_nothing():
    ctx = TableContext(
        owner="ann",
        tags=(Labelled("PII", ""),),
        terms=("t-blank",),
        properties=MappingProxyType({"score": Labelled("high", "")}),
        columns=_columns(id=ColumnContext(classifications=(Labelled("PII", ""),))),
    )
    blank = frozenset({""})
    served = {GLOSSARY: blank, CLASSIFICATION: blank, PROPERTY: blank, OWNER: blank}
    gated = _gate(ctx, served=served, terms={"t-blank": Term("t-blank", "Blank", "")})
    assert gated == TableContext(columns=_columns(id=ColumnContext()))
    assert _gate(TableContext(terms=("t-blank",)), served=served).terms == ()


def test_a_set_that_differs_only_by_case_is_removed():
    ctx = TableContext(
        tags=(Labelled("PII", "PII"),),
        terms=("t-fin",),
        properties=MappingProxyType({"score": Labelled("high", "Quality")}),
    )
    gated = _gate(ctx, terms={"t-fin": Term("t-fin", "Revenue", "Finance")})
    assert gated.tags == ()
    assert gated.terms == ()
    assert dict(gated.properties) == {}


def test_the_owner_is_not_served_unless_declared():
    ctx = TableContext(owner="ann")
    assert _gate(ctx, served={**SERVED, OWNER: frozenset()}).owner == ""
    assert _gate(ctx).owner == "ann"


def test_descriptions_and_the_tier_of_a_visible_table_are_kept():
    ctx = TableContext(
        description="all orders",
        tier="gold",
        columns=_columns(id=ColumnContext(description="key")),
    )
    gated = _gate(ctx)
    assert gated.description == "all orders"
    assert gated.tier == "gold"
    assert gated.columns["id"].description == "key"


def test_the_input_is_never_changed():
    columns = {
        "id": ColumnContext(
            classifications=(Labelled("Secret", "restricted"),),
            terms=("t-hr",),
            properties=MappingProxyType({"budget": Labelled("1M", "finance")}),
        ),
        "secret": ColumnContext(description="hidden"),
    }
    ctx = TableContext(
        owner="ann",
        tags=(Labelled("Secret", "restricted"),),
        terms=("t-hr",),
        properties=MappingProxyType({"budget": Labelled("1M", "finance")}),
        grain=("id", "secret"),
        joins=(Join(("id",), HIDDEN, ("id",)),),
        columns=MappingProxyType(columns),
    )
    before = repr(ctx)
    gated = _gate(ctx, served={}, terms={"t-hr": Term("t-hr", "Salary", "hr")})
    assert repr(ctx) == before
    assert gated is not ctx
    assert set(ctx.columns) == {"id", "secret"}


def test_the_gated_mappings_are_read_only():
    ctx = TableContext(
        properties=MappingProxyType({"score": Labelled("high", "quality")}),
        columns=_columns(id=ColumnContext()),
    )
    gated = _gate(ctx)
    assert isinstance(gated.columns, MappingProxyType)
    assert isinstance(gated.properties, MappingProxyType)
    assert isinstance(gated.columns["id"].properties, MappingProxyType)


def test_a_related_term_attached_only_to_hidden_tables_is_never_named():
    term = Term(
        "t-fin",
        "Revenue",
        "finance",
        relations=(
            TermRef("t-net", "Net revenue", "finance", "see_also"),
            TermRef("t-bonus", "Bonus pool", "finance", "synonym"),
        ),
    )
    served = filter_term(
        term, served_glossaries=frozenset({"finance"}), attached=frozenset({"t-net"})
    )
    assert served.relations == (TermRef("t-net", "Net revenue", "finance", "see_also"),)
    assert "Bonus pool" not in repr(served)


def test_a_related_term_of_a_glossary_outside_the_allow_list_is_never_named():
    term = Term("t-fin", "Revenue", "finance", relations=(TermRef("t-hr", "Salary", "hr"),))
    served = filter_term(
        term, served_glossaries=frozenset({"finance"}), attached=frozenset({"t-hr"})
    )
    assert served.relations == ()


def test_a_served_term_keeps_its_definition_status_and_category_names():
    term = Term(
        "t-fin",
        "Revenue",
        "finance",
        definition="Money in",
        status="Approved",
        categories=("Sales",),
    )
    before = repr(term)
    served = filter_term(term, served_glossaries=frozenset({"finance"}), attached=frozenset())
    assert served == term
    assert repr(term) == before
