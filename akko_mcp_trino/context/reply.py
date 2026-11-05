"""What a tool serves from the catalogues for a table, under Trino's rights.

A call's own ``DESCRIBE`` proves its table and the columns it lists, whatever the
visibility cache holds. Before any provider is asked, the table is recorded as
listed, the canary is checked and, with ``MCP_CONTEXT_REQUIRE_SELECT``, a read of
the visible columns is validated; a failure there serves nothing (``not_checked``).

Each provider's context goes through the gate twice. The first pass, before the
terms are read, only says which glossary keys survive; it is never served. The
terms of those keys are read, each source for its own keys, then the second pass
keeps a key only when its term was read in a served glossary. What is left is
merged, its terms are given references (``t1``, ``t2``… in order of first
appearance; a catalogue's key is never served) and the whole is bounded by
``MCP_CONTEXT_MAX_COLUMNS`` and ``MCP_CONTEXT_RESPONSE_CHARS``.

A provider never makes the tool fail: a deadline left too short, a chain that
does not answer in time or fails says so in ``context_sources``, one status per
provider.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..audit import current_call, note
from ..config import Config
from .chain import ContextChain
from .gate import filter_table, filter_term
from .merge import merge_column, merge_table
from .status import ERROR, NOT_CHECKED, OK, PARTIAL, REFUSED, UNAVAILABLE
from .text import clean_text
from .types import GLOSSARY, TableContext, TableRef, Term
from .visibility import Visibility, VisibilityKey, key_for

_log = logging.getLogger(__name__)

_FAILED = frozenset({UNAVAILABLE, REFUSED, ERROR})
_NOT_WELL = _FAILED | {PARTIAL}

# A term of the answer: its source and its key in that source's catalogue.
_TermId = tuple[str, str]


def _failure(exc: BaseException) -> str:
    """The status of a chain call that raised: a timeout is ``unavailable``."""
    if isinstance(exc, TimeoutError):
        return UNAVAILABLE
    _log.error("the context read failed with %s", type(exc).__name__)
    return ERROR


def _plain(text: str) -> str:
    return clean_text(text, len(text))[0]


def _entry_size(name: str, value: Any) -> int:
    """The length an entry adds to a JSON object: its name, its value and two separators."""
    return len(json.dumps(name)) + len(json.dumps(value)) + 4


def _term_ids(entry: Mapping[str, Any]) -> list[_TermId]:
    return [(item["source"], item["key"]) for item in entry.get("terms", ())]


def _kept_keys(ctx: TableContext) -> list[str]:
    """The glossary keys of a table and its columns, once each, in order."""
    keys = list(ctx.terms)
    for column in ctx.columns.values():
        keys.extend(column.terms)
    return list(dict.fromkeys(keys))


class _Terms:
    """The terms of one answer, rendered by reference, with their related terms."""

    def __init__(self, read: Mapping[_TermId, Term], text_chars: int) -> None:
        self._read = read
        self._text_chars = text_chars

    def name(self, term_id: _TermId) -> str:
        return _plain(self._read[term_id].name)

    def render(self, term_id: _TermId, ref_of: Callable[[_TermId], str | None]) -> dict:
        """The term for the agent; a related term is named only when ``ref_of`` gives it a
        reference, which it gives only to a term read for this answer."""
        source, _ = term_id
        term = self._read[term_id]
        out: dict[str, Any] = {"name": self.name(term_id), "glossary": _plain(term.glossary)}
        definition, cut = clean_text(term.definition, self._text_chars)
        if definition:
            out["definition"] = definition
        status = _plain(term.status)
        if status:
            out["status"] = status
        categories = [c for c in map(_plain, term.categories) if c]
        if categories:
            out["categories"] = categories
        related = []
        for relation in term.relations:
            target = (source, relation.key)
            ref = ref_of(target)
            if ref is None:
                continue
            shown: dict[str, str] = {"ref": ref, "name": self.name(target)}
            kind = _plain(relation.relation)
            if kind:
                shown["relation"] = kind
            related.append(shown)
        if related:
            out["related"] = related
        out["source"] = source
        if cut:
            out["cut"] = ["definition"]
        return out


def _cite(entry: Mapping[str, Any], terms: _Terms, ref_of: Callable[[_TermId], str]) -> dict:
    """``entry`` with each term cited by name and reference, never by its catalogue key."""
    out = dict(entry)
    if "terms" in out:
        out["terms"] = [{"name": terms.name(t), "ref": ref_of(t)} for t in _term_ids(entry)]
    return out


class ContextService:
    """The catalogue context of the tables a call has just listed or described."""

    def __init__(
        self,
        chain: ContextChain,
        visibility: Visibility,
        config: Config,
        *,
        subject: Callable[[], str | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Read through ``chain``, prove through ``visibility``, bound by ``config``.

        ``subject`` returns the caller Trino runs the call's statements as, the verified
        one (the impersonated caller in ``impersonation``), or ``None`` without one; the
        proofs are kept under it. It is given, never imported: a provider's package loads
        nothing of the caller's identity.
        """
        self._chain = chain
        self._visibility = visibility
        self._config = config
        self._subject = subject
        self._clock = clock

    def deadline(self) -> float | None:
        """Return the instant the context of this call must be read by, or ``None``.

        It is ``MCP_CONTEXT_TIMEOUT_SECONDS`` from now, shortened to what is left of the
        call's budget minus ``MCP_CONTEXT_RESERVE_SECONDS``; ``None`` when nothing is left.
        """
        seconds = self._config.context_timeout_seconds
        call = current_call()
        left = call.time_left() if call is not None else None
        if left is not None:
            seconds = min(seconds, left - self._config.context_reserve_seconds)
        if seconds <= 0:
            return None
        return self._clock() + seconds

    def for_table(self, ref: TableRef, visible_columns: Sequence[str], *, complete: bool) -> dict:
        """Return the context of ``ref``, whose ``DESCRIBE`` just listed ``visible_columns``.

        ``complete`` is false when the row cap cut the ``DESCRIBE``: the columns past it are
        not proven, the grain is not served and a source that knows the table is ``partial``.
        The answer has ``table``, ``column_context``, ``terms``, ``context_sources``,
        ``context_truncated`` and ``undescribed_columns``.
        """
        visible = tuple(dict.fromkeys(visible_columns))
        key = key_for(self._config, ref.catalog, self._subject() or "")
        self._visibility.listed(key, [ref])
        if not self._visibility.canary_clear(key):
            return self._nothing(NOT_CHECKED)
        if self._config.context_require_select and not self._visibility.selectable(
            key, ref, visible, listed=visible, described=True
        ):
            return self._nothing(NOT_CHECKED)
        deadline = self.deadline()
        if deadline is None:
            return self._nothing(UNAVAILABLE)
        try:
            gathered = self._chain.gather([ref], deadline=deadline)
        except Exception as exc:
            return self._nothing(_failure(exc))
        statuses = dict(gathered.statuses)
        join_proven = self._join_check(key, ref, visible)

        contexts: dict[str, TableContext] = {}
        served: dict[str, Mapping[str, frozenset[str]]] = {}
        kept: dict[str, list[str]] = {}
        for name, ctx in gathered.tables.get(ref, ()):
            sets = self._chain.served_sets(name)
            if sets is None:
                statuses[name] = ERROR
                continue
            first = filter_table(
                ctx, visible_columns=visible, served=sets, join_proven=join_proven, terms=None
            )
            contexts[name], served[name] = ctx, sets
            keys = _kept_keys(first)
            if keys:
                kept[name] = keys
        read, term_statuses = self._read_terms(kept, deadline)

        final: list[tuple[str, TableContext]] = []
        terms: dict[_TermId, Term] = {}
        for name, ctx in contexts.items():
            glossaries = served[name].get(GLOSSARY, frozenset()) - {""}
            attached = frozenset(kept.get(name, ()))
            mine = {
                term_key: filter_term(term, served_glossaries=glossaries, attached=attached)
                for (source, term_key), term in read.items()
                if source == name
            }
            second = filter_table(
                ctx,
                visible_columns=visible,
                served=served[name],
                join_proven=join_proven,
                terms=mine,
            )
            final.append((name, second))
            terms.update({(name, term_key): term for term_key, term in mine.items()})

        out = self._render(final, visible, terms, complete)
        cut = out["context_truncated"] or not complete
        sources: dict[str, str] = {}
        for name in self._chain.names:
            status = statuses.get(name, UNAVAILABLE)
            if name in contexts and (status in _FAILED or term_statuses.get(name) in _NOT_WELL):
                status = PARTIAL
            if status == OK and cut:
                status = PARTIAL
            sources[name] = status
        out["context_sources"] = sources
        if out["table"] or out["column_context"] or out["terms"]:
            note(metadata_served=[f"{ref.catalog}.{ref.schema}.{ref.table}"])
        return out

    def _nothing(self, status: str) -> dict:
        return {
            "table": {},
            "column_context": {},
            "terms": {},
            "context_sources": dict.fromkeys(self._chain.names, status),
            "context_truncated": False,
            "undescribed_columns": [],
        }

    def _join_check(
        self, key: VisibilityKey, ref: TableRef, visible: Sequence[str]
    ) -> Callable[[TableRef, tuple[str, ...]], bool]:
        """Whether a join target and its columns are proven, each answer kept for the call.

        The described table is proven by its own ``DESCRIBE``; another target by the
        visibility cache, its columns read once per call.
        """
        own = frozenset(visible)
        columns: dict[TableRef, frozenset[str] | None] = {ref: own}
        answers: dict[tuple[TableRef, tuple[str, ...]], bool] = {}

        def proven(target: TableRef, target_columns: tuple[str, ...]) -> bool:
            asked = (target, target_columns)
            if asked not in answers:
                if target not in columns:
                    columns[target] = self._visibility.columns(key, target)
                listed = columns[target]
                answers[asked] = listed is not None and all(c in listed for c in target_columns)
            return answers[asked]

        return proven

    def _read_terms(
        self, kept: Mapping[str, Sequence[str]], deadline: float
    ) -> tuple[Mapping[_TermId, Term], Mapping[str, str]]:
        if not kept:
            return {}, {}
        try:
            result = self._chain.terms(kept, deadline=deadline)
        except Exception as exc:
            return {}, dict.fromkeys(kept, _failure(exc))
        return result.terms, result.statuses

    def _render(
        self,
        final: Sequence[tuple[str, TableContext]],
        visible: Sequence[str],
        read: Mapping[_TermId, Term],
        complete: bool,
    ) -> dict:
        """Merge, cite the terms by reference and keep what the bounds allow, in order."""
        text_chars = self._config.context_text_chars
        table = merge_table(final, text_chars=text_chars)
        if not complete:
            table.pop("grain", None)
            table.pop("other_grains", None)
            sources = table.get("sources", {})
            sources.pop("grain", None)
            if not sources:
                table.pop("sources", None)
        columns: list[tuple[str, dict]] = []
        for column in visible:
            entries = [(name, ctx.columns[column]) for name, ctx in final if column in ctx.columns]
            merged = merge_column(entries, text_chars=text_chars) if entries else {}
            if merged:
                columns.append((column, merged))
        left_out = [name for name, _ in columns[self._config.context_max_columns :]]
        columns = columns[: self._config.context_max_columns]

        terms = _Terms(read, text_chars)
        order: dict[_TermId, None] = {}
        for entry in (table, *(c for _, c in columns)):
            order.update(dict.fromkeys(_term_ids(entry)))
        # Sizes are measured with the longest reference and every related term the
        # answer may name: the answer served is never longer than what was measured.
        widest = f"t{len(order)}"

        def any_ref(term_id: _TermId) -> str | None:
            return widest if term_id in order else None

        def widest_ref(term_id: _TermId) -> str:
            return widest

        total = len(json.dumps({"table": {}, "column_context": {}, "terms": {}}))
        taken: set[_TermId] = set()

        def fits(entry: dict, size: int) -> bool:
            nonlocal total
            new = [t for t in dict.fromkeys(_term_ids(entry)) if t not in taken]
            size += sum(_entry_size(widest, terms.render(t, any_ref)) for t in new)
            if total + size > self._config.context_response_chars:
                return False
            total += size
            taken.update(new)
            return True

        keep_table = not table or fits(table, len(json.dumps(_cite(table, terms, widest_ref))) - 2)
        described: list[tuple[str, dict]] = []
        undescribed: list[str] = []
        for name, entry in columns:
            cited = _cite(entry, terms, widest_ref)
            if keep_table and not undescribed and fits(entry, _entry_size(name, cited)):
                described.append((name, entry))
            else:
                undescribed.append(name)
        if not keep_table:
            table = {}

        refs: dict[_TermId, str] = {}
        for entry in (table, *(c for _, c in described)):
            for term_id in _term_ids(entry):
                refs.setdefault(term_id, f"t{len(refs) + 1}")
        return {
            "table": _cite(table, terms, refs.__getitem__),
            "column_context": {
                name: _cite(entry, terms, refs.__getitem__) for name, entry in described
            },
            "terms": {ref: terms.render(term_id, refs.get) for term_id, ref in refs.items()},
            "context_truncated": bool(undescribed or left_out),
            "undescribed_columns": undescribed + left_out,
        }
