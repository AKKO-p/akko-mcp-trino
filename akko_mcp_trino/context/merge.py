"""Merge what several context providers say about one table or column, keeping the sources."""

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .text import clean_text
from .types import ColumnContext, Join, TableContext

_SINGLE = ("owner", "tier")


def _union(
    entries: Sequence[tuple[str, Any]],
    values: Callable[[Any], Iterable],
    key: Callable[[Any], Any],
    shape: Callable[[Any, str], dict],
) -> list[dict]:
    """Union of the multi-valued items of every entry, first source kept, no duplicate."""
    seen: set = set()
    merged: list[dict] = []
    for source, context in entries:
        for item in values(context):
            k = key(item)
            if k not in seen:
                seen.add(k)
                merged.append(shape(item, source))
    return merged


def _plain(text: str) -> str:
    """A name from the catalogue without control or invisible characters; never cut."""
    return clean_text(text, len(text))[0]


def _labels(entries, field: str) -> list[dict]:
    return _union(
        entries,
        lambda c: [(_plain(i.name), _plain(i.set)) for i in getattr(c, field)],
        lambda item: item,
        lambda item, source: {"name": item[0], "set": item[1], "source": source},
    )


def _terms(entries) -> list[dict]:
    return _union(
        entries,
        lambda c: [_plain(t) for t in c.terms],
        lambda key: key,
        lambda key, source: {"key": key, "source": source},
    )


def _properties(entries) -> dict:
    """Properties keyed ``<set>.<key>``: the first value stays, differing ones are listed."""
    merged: dict[str, dict] = {}
    for source, context in entries:
        for name, labelled in context.properties.items():
            key = f"{_plain(labelled.set)}.{_plain(name)}"
            value = _plain(labelled.name)
            entry = merged.setdefault(key, {"value": value, "source": source})
            if value != entry["value"]:
                others = entry.setdefault("other_values", [])
                if all(o["value"] != value for o in others):
                    others.append({"value": value, "source": source})
    return merged


def _texts(entries, text_chars: int, merged: dict, singles: Sequence[str]) -> None:
    """Fill the single-valued fields, ``sources``, ``other_descriptions`` and ``cut``."""
    sources: dict[str, str] = {}
    cut: list[str] = []
    others: list[dict] = []
    winner = ""
    for source, context in entries:
        for field in ("description", *singles):
            text, was_cut = clean_text(getattr(context, field), text_chars)
            if not text:
                continue
            if field not in sources:
                sources[field] = source
                merged[field] = text
                if field == "description":
                    winner = text
                if was_cut:
                    cut.append(field)
            elif field == "description" and text != winner:
                if all(o["text"] != text for o in others):
                    others.append({"text": text, "source": source})
                    if was_cut and "other_descriptions" not in cut:
                        cut.append("other_descriptions")
    if sources:
        merged["sources"] = sources
    if others:
        merged["other_descriptions"] = others
    if cut:
        merged["cut"] = cut


def _put(merged: dict, name: str, value) -> None:
    if value:
        merged[name] = value


def merge_column(entries: Sequence[tuple[str, ColumnContext]], *, text_chars: int) -> dict:
    """Merge the column contexts of several providers, in the order of ``entries``."""
    merged: dict = {}
    _texts(entries, text_chars, merged, ())
    _put(merged, "classifications", _labels(entries, "classifications"))
    _put(merged, "terms", _terms(entries))
    _put(merged, "properties", _properties(entries))
    return merged


def _grain(entries) -> list[str]:
    return list(dict.fromkeys(_plain(c) for _, context in entries for c in context.grain))


def _joins(entries) -> list[dict]:
    def shape(join: Join, source: str) -> dict:
        target = join.target
        return {
            "columns": [_plain(c) for c in join.columns],
            "table": _plain(f"{target.catalog}.{target.schema}.{target.table}"),
            "target_columns": [_plain(c) for c in join.target_columns],
            "source": source,
        }

    return _union(
        entries,
        lambda c: c.joins,
        lambda j: (j.columns, j.target, j.target_columns),
        shape,
    )


def merge_table(entries: Sequence[tuple[str, TableContext]], *, text_chars: int) -> dict:
    """Merge the table contexts of several providers, in the order of ``entries``."""
    merged: dict = {}
    _texts(entries, text_chars, merged, _SINGLE)
    _put(merged, "tags", _labels(entries, "tags"))
    _put(merged, "terms", _terms(entries))
    _put(merged, "properties", _properties(entries))
    _put(merged, "grain", _grain(entries))
    _put(merged, "joins", _joins(entries))
    return merged
