"""Remove from a catalogue's description what Trino or the allow list does not let through.

The gate runs on what each provider says, before the merge. A column is kept
only under the exact name Trino lists to the caller. A grain loses all its
columns when one of them is hidden: a shorter grain would lie about
uniqueness. A join is kept only from visible columns to a table proven
visible. A term, a classification, a tag or a property is kept only when its
set is served; the owner only when the owner is served. A kind missing from
``served`` serves nothing.

The inputs are never changed: cached contexts are shared between callers.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType

from .types import (
    CLASSIFICATION,
    GLOSSARY,
    OWNER,
    PROPERTY,
    ColumnContext,
    Labelled,
    TableContext,
    TableRef,
    Term,
)


def _labels(labels: tuple[Labelled, ...], sets: frozenset[str]) -> tuple[Labelled, ...]:
    return tuple(label for label in labels if label.set in sets)


def _properties(properties: Mapping[str, Labelled], sets: frozenset[str]) -> Mapping:
    return MappingProxyType({k: v for k, v in properties.items() if v.set in sets})


def _column(
    column: ColumnContext,
    classifications: frozenset[str],
    properties: frozenset[str],
    keep_term: Callable[[str], bool],
) -> ColumnContext:
    return replace(
        column,
        classifications=_labels(column.classifications, classifications),
        terms=tuple(key for key in column.terms if keep_term(key)),
        properties=_properties(column.properties, properties),
    )


def filter_table(
    ctx: TableContext,
    *,
    visible_columns: Sequence[str],
    served: Mapping[str, frozenset[str]],
    join_proven: Callable[[TableRef], bool],
    terms: Mapping[str, Term] | None = None,
) -> TableContext:
    """Return ``ctx`` without what the caller may not see or the allow list does not serve.

    ``visible_columns`` are the names Trino lists to the caller; a context column
    is kept only under one of them, never after folding its case. ``served`` gives
    the sets of each kind that the provider declares and the configuration allows.
    ``join_proven`` is asked only for the target of a join from visible columns.
    Without ``terms``, term keys are kept while a glossary is served, until the
    terms are read; with them, a key is kept only when its term was read and its
    glossary is served.
    """
    visible = frozenset(visible_columns)
    glossaries = served.get(GLOSSARY, frozenset())
    classifications = served.get(CLASSIFICATION, frozenset())
    properties = served.get(PROPERTY, frozenset())

    def keep_term(key: str) -> bool:
        if not glossaries:
            return False
        if terms is None:
            return True
        term = terms.get(key)
        return term is not None and term.glossary in glossaries

    grain = ctx.grain if all(name in visible for name in ctx.grain) else ()
    joins = tuple(
        join
        for join in ctx.joins
        if all(name in visible for name in join.columns) and join_proven(join.target)
    )
    columns = {
        name: _column(column, classifications, properties, keep_term)
        for name, column in ctx.columns.items()
        if name in visible
    }
    return replace(
        ctx,
        owner=ctx.owner if served.get(OWNER) else "",
        tags=_labels(ctx.tags, classifications),
        terms=tuple(key for key in ctx.terms if keep_term(key)),
        properties=_properties(ctx.properties, properties),
        grain=grain,
        joins=joins,
        columns=MappingProxyType(columns),
    )


def filter_term(term: Term, *, served_glossaries: frozenset[str], attached: frozenset[str]) -> Term:
    """Return ``term`` naming only the related terms the caller may be told of.

    A related term is kept when its key is in ``attached`` (terms attached to an
    object proven visible in the same answer) and its glossary is served. The
    categories are kept, by name.
    """
    relations = tuple(
        ref for ref in term.relations if ref.key in attached and ref.glossary in served_glossaries
    )
    return replace(term, relations=relations)
