"""Merge what several context providers say about one table or column, keeping the sources."""

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .text import clean_name, clean_text
from .types import ColumnContext, TableContext

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


def _plains(texts: Iterable[str]) -> tuple[str, ...]:
    """The cleaned names that are not empty once cleaned, nor dropped for their length."""
    return tuple(t for t in map(clean_name, texts) if t)


def _labels(entries, field: str) -> list[dict]:
    def cleaned(context) -> list[tuple[str, str]]:
        pairs = ((clean_name(i.name), clean_name(i.set)) for i in getattr(context, field))
        return [(name, label_set) for name, label_set in pairs if name and label_set]

    return _union(
        entries,
        cleaned,
        lambda item: item,
        lambda item, source: {"name": item[0], "set": item[1], "source": source},
    )


def _terms(entries) -> list[dict]:
    return _union(
        entries,
        lambda c: c.terms,
        lambda key: key,
        lambda key, source: {"key": key, "source": source},
    )


def _properties(entries, text_chars: int) -> dict:
    """Properties keyed ``<set>.<key>``: the first value stays, differing ones are listed.

    The set and the key are names, dropped past their bound; a value is a text, cut at
    ``text_chars`` with ``"cut": ["value"]``.
    """
    merged: dict[str, dict] = {}
    for source, context in entries:
        for name, labelled in context.properties.items():
            label_set, prop = clean_name(labelled.set), clean_name(name)
            value, cut = clean_text(labelled.name, text_chars)
            if not (label_set and prop and value):
                continue
            shown: dict = {"value": value, "source": source}
            if cut:
                shown["cut"] = ["value"]
            key = f"{label_set}.{prop}"
            entry = merged.setdefault(key, shown)
            if value != entry["value"]:
                others = entry.setdefault("other_values", [])
                if all(o["value"] != value for o in others):
                    others.append(shown)
    return merged


def _texts(entries, text_chars: int, merged: dict, singles: Sequence[str]) -> None:
    """Fill the single-valued fields, ``sources``, ``other_descriptions`` and ``cut``."""
    sources: dict[str, str] = {}
    cut: list[str] = []
    others: list[dict] = []
    winner = ""
    for source, context in entries:
        for field in ("description", *singles):
            # The owner and the tier are names: dropped past their bound, never cut.
            if field == "description":
                text, was_cut = clean_text(context.description, text_chars)
            else:
                text, was_cut = clean_name(getattr(context, field)), False
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
    _put(merged, "properties", _properties(entries, text_chars))
    return merged


def _grain(entries, merged: dict) -> None:
    """The grain is one value: the first provider with a whole grain wins, others are listed."""
    others: list[dict] = []
    for source, context in entries:
        columns = [clean_name(c) for c in context.grain]
        if not columns or not all(columns):
            continue
        if "grain" not in merged:
            merged["grain"] = columns
            merged.setdefault("sources", {})["grain"] = source
        elif columns != merged["grain"] and all(o["columns"] != columns for o in others):
            others.append({"columns": columns, "source": source})
    if others:
        merged["other_grains"] = others


def _joins(entries) -> list[dict]:
    def cleaned(context) -> list[tuple[tuple[str, ...], str, tuple[str, ...]]]:
        found = []
        for join in context.joins:
            target = join.target
            parts = _plains((target.catalog, target.schema, target.table))
            columns, target_columns = _plains(join.columns), _plains(join.target_columns)
            paired = (
                len(columns) == len(join.columns) == len(join.target_columns) == len(target_columns)
            )
            if len(parts) == 3 and columns and paired:
                found.append((columns, ".".join(parts), target_columns))
        return found

    return _union(
        entries,
        cleaned,
        lambda j: j,
        lambda j, source: {
            "columns": list(j[0]),
            "table": j[1],
            "target_columns": list(j[2]),
            "source": source,
        },
    )


def merge_table(entries: Sequence[tuple[str, TableContext]], *, text_chars: int) -> dict:
    """Merge the table contexts of several providers, in the order of ``entries``."""
    merged: dict = {}
    _texts(entries, text_chars, merged, _SINGLE)
    _put(merged, "tags", _labels(entries, "tags"))
    _put(merged, "terms", _terms(entries))
    _put(merged, "properties", _properties(entries, text_chars))
    _grain(entries, merged)
    _put(merged, "joins", _joins(entries))
    return merged
