"""The vocabulary of the context interface: types, protocol, errors and statuses."""

import asyncio
import dataclasses
from types import MappingProxyType

from akko_mcp_trino import context
from akko_mcp_trino.context import errors, provider, status, types

DATA_VALUE_NAMES = {"values", "sample", "samples", "profile", "statistics", "top_values"}


def test_no_public_type_carries_a_data_value():
    public = (
        types.TableRef,
        types.TermRef,
        types.Term,
        types.Labelled,
        types.ColumnContext,
        types.TableContext,
        types.Join,
    )
    for cls in public:
        assert dataclasses.is_dataclass(cls)
        assert {f.name for f in dataclasses.fields(cls)} & DATA_VALUE_NAMES == set()


def test_a_table_ref_folds_trino_names_to_lower_case():
    assert types.TableRef.of("Demo", "CRM", "Orders") == types.TableRef("demo", "crm", "orders")


def test_the_status_of_an_error_never_carries_its_text():
    assert status.status_of(errors.ProviderRefused("401 secret=s3cr3t")) == "refused"
    assert status.status_of(errors.ProviderUnavailable("down")) == "unavailable"
    assert status.status_of(asyncio.TimeoutError()) == "unavailable"
    assert status.status_of(KeyError("x")) == "error"
    assert status.status_of(errors.ProviderError("plain")) == "error"


def test_the_context_api_is_one():
    assert context.CONTEXT_API == 1


def test_the_statuses_are_the_lower_case_names_of_the_spec():
    assert [
        status.OK,
        status.NOT_FOUND,
        status.NOT_MAPPED,
        status.PARTIAL,
        status.UNAVAILABLE,
        status.REFUSED,
        status.ERROR,
        status.NOT_CHECKED,
    ] == [
        "ok",
        "not_found",
        "not_mapped",
        "partial",
        "unavailable",
        "refused",
        "error",
        "not_checked",
    ]


def test_the_types_are_frozen_and_their_mappings_default_to_empty_read_only_views():
    table = types.TableContext()
    column = types.ColumnContext()
    assert isinstance(table.properties, MappingProxyType) and not table.properties
    assert isinstance(table.columns, MappingProxyType) and not table.columns
    assert isinstance(column.properties, MappingProxyType) and not column.properties
    assert table.joins == () and table.grain == () and table.description == ""
    try:
        table.owner = "x"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover
        raise AssertionError("TableContext must be frozen")
    join = types.Join(("a",), types.TableRef.of("c", "s", "t"), ("b",))
    assert join.target.table == "t"


def test_the_provider_kinds_are_the_four_allow_list_kinds():
    assert (types.GLOSSARY, types.CLASSIFICATION, types.PROPERTY, types.OWNER) == (
        "glossary",
        "classification",
        "property",
        "owner",
    )


def test_a_class_with_the_protocol_members_is_a_context_provider():
    class Stub:
        name = "stub"
        context_api = 1

        def maps(self, catalog):
            return True

        def served_sets(self):
            return {}

        async def describe(self, refs, *, deadline, on_behalf_of=None):
            return {}

        async def terms(self, keys, *, deadline):
            return {}

        def check_lines(self):
            return []

        async def aclose(self):
            return None

        def credentials_stamp(self):
            return ""

    assert isinstance(Stub(), provider.ContextProvider)
    assert not isinstance(object(), provider.ContextProvider)
