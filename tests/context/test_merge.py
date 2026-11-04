"""The merge rule: first provider wins a single field, multi-valued fields are a union."""

from types import MappingProxyType

from akko_mcp_trino.context import ColumnContext, Join, Labelled, TableContext, TableRef
from akko_mcp_trino.context.merge import merge_column, merge_table

BIG = 1000


def test_the_first_provider_with_a_description_wins_and_says_so():
    merged = merge_table(
        [
            ("x", TableContext()),
            ("a", TableContext(description="a")),
            ("b", TableContext(description="b")),
        ],
        text_chars=BIG,
    )
    assert merged["description"] == "a"
    assert merged["sources"] == {"description": "a"}


def test_another_description_is_kept_with_its_source():
    merged = merge_table(
        [
            ("a", TableContext(description="one")),
            ("b", TableContext(description="two")),
            ("c", TableContext(description="two")),
            ("d", TableContext(description="one")),
        ],
        text_chars=BIG,
    )
    assert merged["other_descriptions"] == [{"text": "two", "source": "b"}]


def test_single_valued_fields_come_from_the_first_provider_that_has_one():
    merged = merge_table(
        [
            ("a", TableContext(owner="ann")),
            ("b", TableContext(owner="bob", tier="gold")),
        ],
        text_chars=BIG,
    )
    assert (merged["owner"], merged["tier"]) == ("ann", "gold")
    assert merged["sources"] == {"owner": "a", "tier": "b"}


def test_multi_valued_fields_are_a_union_without_duplicates():
    target = TableRef("c", "s", "t")
    first = TableContext(
        tags=(Labelled("pii", "cat"),),
        terms=("k1",),
        properties=MappingProxyType({"k": Labelled("v", "set")}),
        grain=("id",),
        joins=(Join(("a",), target, ("b",)),),
    )
    second = TableContext(
        tags=(Labelled("pii", "cat"), Labelled("hr", "cat")),
        terms=("k1", "k2"),
        properties=MappingProxyType({"k": Labelled("other", "set"), "j": Labelled("w", "set")}),
        grain=("id", "day"),
        joins=(Join(("a",), target, ("b",)), Join(("x",), target, ("y",))),
    )
    merged = merge_table([("a", first), ("b", second)], text_chars=BIG)
    assert merged["tags"] == [
        {"name": "pii", "set": "cat", "source": "a"},
        {"name": "hr", "set": "cat", "source": "b"},
    ]
    assert merged["terms"] == [{"key": "k1", "source": "a"}, {"key": "k2", "source": "b"}]
    assert merged["properties"] == {
        "set.k": {"value": "v", "source": "a"},
        "set.j": {"value": "w", "source": "b"},
    }
    assert merged["grain"] == ["id", "day"]
    assert merged["joins"] == [
        {"columns": ["a"], "table": "c.s.t", "target_columns": ["b"], "source": "a"},
        {"columns": ["x"], "table": "c.s.t", "target_columns": ["y"], "source": "b"},
    ]


def test_empty_fields_are_omitted():
    assert merge_table([("a", TableContext())], text_chars=BIG) == {}
    assert merge_column([], text_chars=BIG) == {}


def test_a_column_merges_classifications_and_terms_with_sources():
    merged = merge_column(
        [
            ("a", ColumnContext(description="d", classifications=(Labelled("pii", "cls"),))),
            ("b", ColumnContext(classifications=(Labelled("pii", "cls"),), terms=("k",))),
        ],
        text_chars=BIG,
    )
    assert merged == {
        "description": "d",
        "sources": {"description": "a"},
        "classifications": [{"name": "pii", "set": "cls", "source": "a"}],
        "terms": [{"key": "k", "source": "b"}],
    }


def test_descriptions_are_cleaned_and_cut_fields_are_listed():
    merged = merge_table(
        [
            ("a", TableContext(description="ab‮cdef", owner="o" * 10)),
            ("b", TableContext(description="x" * 10)),
            ("c", TableContext(description="y" * 3)),
        ],
        text_chars=4,
    )
    assert merged["description"] == "abcd"
    assert merged["owner"] == "oooo"
    assert merged["other_descriptions"] == [
        {"text": "xxxx", "source": "b"},
        {"text": "yyy", "source": "c"},
    ]
    assert merged["cut"] == ["description", "owner", "other_descriptions"]


def test_a_description_empty_after_cleaning_is_ignored():
    merged = merge_column(
        [("a", ColumnContext(description="​")), ("b", ColumnContext(description="ok"))],
        text_chars=BIG,
    )
    assert merged["sources"] == {"description": "b"}
