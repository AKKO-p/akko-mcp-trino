"""The values a context provider returns: names, descriptions and labels, never data."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

GLOSSARY = "glossary"
CLASSIFICATION = "classification"
PROPERTY = "property"
OWNER = "owner"


def _empty() -> Mapping:
    return MappingProxyType({})


@dataclass(frozen=True)
class TableRef:
    """A table by its Trino names: lower case, always listed to the caller by Trino."""

    catalog: str
    schema: str
    table: str

    @classmethod
    def of(cls, catalog: str, schema: str, table: str) -> "TableRef":
        """Build a reference, folding the three names to lower case as Trino does."""
        return cls(catalog.lower(), schema.lower(), table.lower())


@dataclass(frozen=True)
class TermRef:
    """A pointer to a glossary term, with how it relates to the term it hangs from."""

    key: str
    name: str
    glossary: str
    relation: str = ""


@dataclass(frozen=True)
class Term:
    """A glossary term as the catalogue defines it."""

    key: str
    name: str
    glossary: str
    definition: str = ""
    status: str = ""
    categories: tuple[str, ...] = ()
    relations: tuple[TermRef, ...] = ()


@dataclass(frozen=True)
class Labelled:
    """A name taken from a set: a classification, a tag category or a property set."""

    name: str
    set: str


@dataclass(frozen=True)
class Join:
    """Columns of a table that join columns of another table."""

    columns: tuple[str, ...]
    target: TableRef
    target_columns: tuple[str, ...]


@dataclass(frozen=True)
class ColumnContext:
    """What the catalogue says about one column."""

    description: str = ""
    classifications: tuple[Labelled, ...] = ()
    terms: tuple[str, ...] = ()
    properties: Mapping[str, Labelled] = field(default_factory=_empty)


@dataclass(frozen=True)
class TableContext:
    """What the catalogue says about one table; its columns are keyed by lower-case name."""

    description: str = ""
    owner: str = ""
    tier: str = ""
    tags: tuple[Labelled, ...] = ()
    terms: tuple[str, ...] = ()
    properties: Mapping[str, Labelled] = field(default_factory=_empty)
    grain: tuple[str, ...] = ()
    joins: tuple[Join, ...] = ()
    columns: Mapping[str, ColumnContext] = field(default_factory=_empty)
