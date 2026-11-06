"""search_columns: the tables and columns that hold every word of a text, within the call's time.

The text is cut into words (whitespace and punctuation split them; ``_`` belongs to a word).
A column matches when every word appears in its name, its comment, its table's name or its
table's comment, and one word at least in its own name or comment: a word found only in a
table's name or comment does not list all its columns. A table matches when every word
appears in its name or its comment, or when it holds a column that matches. The search reads
``system.metadata.table_comments`` and ``system.jdbc.columns``, which Trino filters for each
user, one schema at a time, in a stable order: the catalogs the caller sees (never
``system``), sorted, and in each its schemas, sorted; in a schema its tables first, then its
columns. A schema costs two statements, each reading both views of that schema (a table
holds a matching column, a column's table holds a word). Each is filtered by equality on
catalog and schema, which Trino pushes down to the views, so it loads that schema only, and
a catalog of many thousand tables answers schema by schema instead of not at all.

When a search of several words is complete and nothing holds every word on any page of its
chain of cursors, the same call goes on with a per-word pass over the same scope: each row a
word alone finds (the same rules, one word for all), marked with the words that found it,
within the same row cap, time budget and rights. One pass reads all the words, two
statements per schema as the first pass; ``partial`` says the page is of that pass, and
``complete``, ``not_searched`` and the cursor then speak of it.

The search never claims more than it did. Before each schema it checks what is left of the
call's budget, keeping a reserve so that it ends with an answer, not with the call's own
error; each query is bounded by what is left. It stops on the row cap too. Either way the
answer says what was not searched and why, and a cursor resumes the search exactly there,
inside a schema's tables or columns if the row cap stopped it there. ``complete`` is true
only when a search, over the calls of one chain of cursors, read everything in its scope.

Each word enters the statement as one escaped ``LIKE`` literal (``like_contains``): a word
holds no quote, and its ``_`` is a character, never a wildcard. The audit keeps how many
tables and columns came and whether the search was cut, never the text, which may be a
value.
"""

import base64
import binascii
import contextvars
import hashlib
import json
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

from .audit import CallTimeout, current_call, error_code, note
from .errors import INVALID_ARGUMENT, TIME_BUDGET, VIEW_REFUSED, explain_error, metadata_view_error
from .sql_guard import (
    JDBC_COLUMNS,
    TABLE_COMMENTS,
    governed_catalog,
    sql_literal,
    validate_identifier,
)
from .trino_client import Cancellation, every_row

# Why a part of the scope was not searched, besides an error's kind.
ROW_CAP = "row_cap"
# The two reads of a schema, in this order: its matching tables, then its matching columns.
TABLES = "tables"
COLUMNS = "columns"
_PHASES = (TABLES, COLUMNS)
# The two passes of a search: every word together, then, when that found nothing, each
# word alone (its rows marked with the words that found them).
EVERY_WORD = "every_word"
PER_WORD = "per_word"
_MODES = (EVERY_WORD, PER_WORD)
# What a complete search that found nothing says, so that an agent tries another way
# rather than conclude that the data does not exist.
HINT = (
    "Nothing matched these words: search again with one word from a column or table name "
    "(never a value such as a date, an amount or a code), or browse with list_tables and "
    "describe_schema."
)
# What a page of the per-word pass that has more to read says, so that an agent never takes
# it for the end.
PER_WORD_MORE_HINT = (
    "No table or column holds every word together. This per-word search is not finished: "
    "call search_columns again with the same text and cursor=next_cursor."
)
# What the last page of a per-word pass that found rows says: they hold some of the words.
PER_WORD_HINT = (
    "No table or column holds every word together, so these are partial matches, each found "
    "by the words it lists: pick the right one, or search again with a single word from a "
    "column or table name, then call describe_table on the table."
)
_WORD = re.compile(r"\w+")
# What a cursor may hold: a schema name of this many characters at most, a row offset Trino
# reads as a bigint.
MAX_NAME_LENGTH = 1024
MAX_ROW_OFFSET = 2**63 - 1
_INVALID_CURSOR = (
    "Invalid cursor: pass back next_cursor unchanged, or start the search again without a cursor"
)


def trino_lower(text: str) -> str:
    """``text`` lower-cased as Trino's ``lower()`` does: one code point at a time, without
    the rules of context (a final sigma) or mappings to several code points (``İ``) that
    ``str.lower`` applies, so that a word matches the names Trino lower-cases."""
    return "".join(ch.lower()[0] for ch in text)


def like_contains(text: str) -> str:
    """The ``LIKE`` literal that matches ``text`` anywhere, lower-cased, written with
    ``ESCAPE '!'``: ``!``, ``%`` and ``_`` are escaped, so each stands for itself."""
    escaped = trino_lower(text).replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return sql_literal(f"%{escaped}%")


def search_words(text: str, min_length: int) -> list[str]:
    """The words of ``text`` the search looks for, lower-cased, each once, in the order given.

    A word is a run of letters, digits and ``_``: whitespace and punctuation split them, so
    a word holds no quote and no ``%``, and ``customer_id`` is one word. A word shorter than
    ``min_length`` is dropped."""
    found = _WORD.findall(trino_lower(text))
    return list(dict.fromkeys(w for w in found if len(w) >= min_length))


def every_word(words: list[str], fields: tuple[str, ...]) -> str:
    """The condition that each word appears, ignoring case, in one of ``fields`` at least:
    one group of ORs per word, the groups joined by AND."""
    return " AND ".join(
        "(" + " OR ".join(f"lower({f}) LIKE {pattern} ESCAPE '!'" for f in fields) + ")"
        for pattern in map(like_contains, words)
    )


def any_word(words: list[str], fields: tuple[str, ...]) -> str:
    """The condition that one word at least appears, ignoring case, in one of ``fields``."""
    return (
        "("
        + " OR ".join(
            f"lower({f}) LIKE {pattern} ESCAPE '!'"
            for pattern in map(like_contains, words)
            for f in fields
        )
        + ")"
    )


def query_digest(words: list[str], catalog: str, schema: str) -> str:
    """What ties a cursor to its search: a digest of the words and of the catalog and schema
    it was given (empty when not given), already folded.

    It guards against a mistake, a cursor kept for another search, not against forgery: an
    agent can compute it, and gains nothing by it, since Trino decides what any search reads."""
    data = json.dumps([words, catalog, schema]).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass(frozen=True)
class Cursor:
    """Where a search resumes: in ``catalog``, from ``schema`` (empty: its first schema), at
    its ``phase`` (its tables, or its columns once its tables are read), after the first
    ``row_offset`` matching rows of that phase. ``query`` ties it to its search, ``gaps``
    says an earlier call of the search left a part of it not searched, and ``found`` that an
    earlier call found a table or a column. ``mode`` is the pass it resumes: every word
    together, or each word alone once that found nothing."""

    catalog: str
    schema: str
    row_offset: int
    query: str = ""
    gaps: bool = False
    phase: str = TABLES
    found: bool = False
    mode: str = EVERY_WORD


def encode_cursor(cursor: Cursor) -> str:
    """The cursor as an opaque token for the agent: url-safe base64 of a JSON object."""
    data = {
        "c": cursor.catalog,
        "s": cursor.schema,
        "p": cursor.phase,
        "o": cursor.row_offset,
        "q": cursor.query,
        "g": cursor.gaps,
        "f": cursor.found,
        "m": cursor.mode,
    }
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def decode_cursor(token: str) -> Cursor:
    """The cursor ``token`` encodes, every field checked; anything else raises ValueError.

    The catalog passes ``governed_catalog`` (never ``system``); the search then requires it
    to be one the caller sees. The schema, empty for a catalog's first, comes from Trino's
    own listing and only ever enters a string literal: any lower-case name without NUL, up to
    ``MAX_NAME_LENGTH`` characters, so every cursor the server gives resumes, a Hive schema
    such as ``2024_sales`` included. The mode is ``every_word`` or ``per_word``, the phase
    ``tables`` or ``columns``, the offset a
    whole number from 0 to ``MAX_ROW_OFFSET``."""
    try:
        data = json.loads(base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True))
        if not isinstance(data, dict):
            raise ValueError(_INVALID_CURSOR)
        catalog, schema, offset = data["c"], data["s"], data["o"]
        query, gaps, phase, found = data["q"], data["g"], data["p"], data["f"]
        mode = data["m"]
        if not (isinstance(catalog, str) and isinstance(schema, str) and isinstance(query, str)):
            raise ValueError(_INVALID_CURSOR)
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError(_INVALID_CURSOR)
        if not 0 <= offset <= MAX_ROW_OFFSET or not isinstance(gaps, bool):
            raise ValueError(_INVALID_CURSOR)
        if not isinstance(found, bool):
            raise ValueError(_INVALID_CURSOR)
        if phase not in _PHASES or mode not in _MODES:
            raise ValueError(_INVALID_CURSOR)
        if len(schema) > MAX_NAME_LENGTH or "\0" in schema or schema != schema.lower():
            raise ValueError(_INVALID_CURSOR)
        governed_catalog(catalog)
    except (ValueError, KeyError, binascii.Error) as exc:
        raise ValueError(_INVALID_CURSOR) from exc
    return Cursor(
        catalog, schema, offset, query=query, gaps=gaps, phase=phase, found=found, mode=mode
    )


class _Abandoned(Exception):
    """A catalog read at the same time as others that the pass no longer needs."""


def _not_searched(catalog: str, schema: Optional[str], reason: str) -> dict:
    """One part of the scope not searched: a schema, or a whole catalog (schema None)."""
    return {"catalog": catalog, "schema": schema, "reason": reason}


def search(
    ctx: Any,
    text: str,
    catalog: Optional[str],
    schema: Optional[str],
    cursor: Optional[str],
    *,
    min_length: int,
    max_words: int,
    max_word_length: int,
    reserve_seconds: float,
    max_catalogs: int,
    concurrency: int = 1,
    slots: Optional[threading.Semaphore] = None,
) -> dict:
    """Search the tables and columns that hold every word of ``text``, for the verified caller.

    ``ctx`` is the `ToolContext`: its ``run`` queries Trino, its ``page_size`` (the row cap)
    bounds the tables and columns of one answer together, its ``visible_catalogs`` lists the
    catalogs. With
    ``catalog``, only its schemas are searched; with ``schema`` too, only that one, once
    ``schema_visible`` says the caller sees it: a schema that does not exist and one the
    policy hides answer the same error of kind ``schema``, never an empty search taken as
    complete, and a catalog Trino refuses or does not know raises its error. A text
    without a word of ``min_length`` characters, a schema without its catalog, every catalog
    without a call budget, more than ``max_words`` words, a word longer than
    ``max_word_length``, and a cursor that is not this search's raise ValueError before Trino
    is asked anything. ``reserve_seconds`` of the call's budget are kept so the search
    ends with an answer. A refused metadata view answers its error, which names what to
    allow. A complete search that found nothing, over every call of its chain of cursors,
    adds ``hint``; with several words, it goes on with the per-word pass, whose pages carry
    ``partial`` and say what that pass searched. ``concurrency`` catalogs are read at once,
    each as the caller and within the call, in threads that take one of ``slots`` each (the
    reads in flight of every call of the process); with none free, the call reads the
    catalog itself. Unless the call's budget runs out, the answer and the cursor are those
    of a search of one catalog at a time."""
    words = search_words(text, min_length)
    if not words:
        raise ValueError(
            f"Search text has no word of {min_length} characters or more (shorter words are "
            "ignored): give at least one, so the search does not read every column"
        )
    if len(words) > max_words:
        raise ValueError(
            f"Search text has {len(words)} words: give {max_words} at most, the most precise ones"
        )
    longest = max(words, key=len)
    if len(longest) > max_word_length:
        raise ValueError(
            f"Search text has a word of {len(longest)} characters: a word is "
            f"{max_word_length} characters at most"
        )
    if schema and not catalog:
        raise ValueError("A schema is searched within its catalog: name the catalog too")
    # Trino folds an unquoted name to lower case: names are compared and answered folded.
    only_catalog = governed_catalog(catalog).lower() if catalog else ""
    only_schema = validate_identifier(schema, "schema").lower() if schema else ""
    call = current_call()
    if not only_catalog and (call is None or call.time_left() is None):
        raise ValueError(
            "This server sets no time budget for a call, so a search of every catalog would "
            "have no bound: name a catalog"
        )
    digest = query_digest(words, only_catalog, only_schema)
    start = decode_cursor(cursor) if cursor else None
    if start is not None and (
        start.query != digest
        or (only_catalog and start.catalog != only_catalog)
        or (only_schema and start.schema != only_schema)
    ):
        raise ValueError(_INVALID_CURSOR)

    def time_remains() -> bool:
        """Whether the call still has time beyond the reserve: after a query stopped by a
        plain TimeoutError, true means its own budget (``TRINO_QUERY_TIMEOUT_SECONDS``)
        stopped it, not what was left of the call."""
        try:
            allowed()
        except TimeoutError:
            return False
        return True

    def allowed() -> float:
        """Seconds the next query may take (0: no budget of the call), or a plain
        TimeoutError when only the reserve is left."""
        left = call.time_left() if call is not None else None
        if left is None:
            return 0.0
        left -= reserve_seconds
        if left <= 0:
            raise TimeoutError("no time left in the call for the search")
        return left

    def stoppable(cancel: Optional[Cancellation]) -> dict:
        """The keyword that lets ``cancel`` stop a read in Trino, when there is one."""
        return {"cancel": cancel} if cancel is not None else {}

    def schemas_of(cat: str, first: str, cancel: Optional[Cancellation] = None) -> list[str]:
        """The schemas of ``cat`` from ``first`` on, sorted, without information_schema."""
        sql = (
            f"SELECT schema_name FROM {cat}.information_schema.schemata "
            f"WHERE schema_name >= {sql_literal(first)} "
            "AND schema_name <> 'information_schema' ORDER BY schema_name"
        )
        rows = every_row(lambda s: ctx.run(s, timeout=allowed(), **stoppable(cancel)), sql)
        return [str(r[0]) for r in rows]

    if only_schema and not ctx.schema_visible(only_catalog, only_schema, timeout=allowed()):
        # system.jdbc.columns filtered on a schema the caller does not see answers no row and
        # no error: without this check the search would say it searched it, completely.
        note(error="schema_not_found")
        return {
            "error": f"Schema {catalog}.{schema} not found or not visible",
            "kind": "schema",
        }

    # A column: every word in its four fields, one word at least in its own two.
    column_match = (
        every_word(words, ("c.column_name", "c.remarks", "c.table_name", "t.comment"))
        + " AND "
        + any_word(words, ("c.column_name", "c.remarks"))
    )
    patterns = [like_contains(w) for w in words]
    # The per-word pass: a column with one word at least in its own name or comment, which
    # is every column that word alone finds; each read says which words found each row.
    own = [
        f"(lower(c.column_name) LIKE {p} ESCAPE '!' OR lower(c.remarks) LIKE {p} ESCAPE '!')"
        for p in patterns
    ]
    any_own = any_word(words, ("c.column_name", "c.remarks"))

    def matching_columns(select: str, cat_: str, sch_: str) -> str:
        """``select`` over the matching columns of one schema, each joined to its table's
        comment; both views filtered by equality on the catalog and the schema."""
        return (
            f"SELECT {select} FROM {JDBC_COLUMNS} c LEFT JOIN {TABLE_COMMENTS} t "
            f"ON t.catalog_name = {cat_} AND t.schema_name = {sch_} "
            "AND t.table_name = c.table_name "
            f"WHERE c.table_cat = {cat_} AND c.table_schem = {sch_} AND {column_match}"
        )

    def every_word_sql(phase: str, cat_: str, sch_: str) -> str:
        """The statement of one phase of the search of every word."""
        if phase == TABLES:
            # A table whose own text holds every word, or that holds a matching column.
            holding = matching_columns("c.table_name", cat_, sch_)
            return (
                f"SELECT s.table_name, s.comment FROM {TABLE_COMMENTS} s "
                f"WHERE s.catalog_name = {cat_} AND s.schema_name = {sch_} "
                f"AND ({every_word(words, ('s.table_name', 's.comment'))} "
                f"OR s.table_name IN ({holding})) ORDER BY s.table_name"
            )
        return (
            matching_columns("c.table_name, c.column_name, c.type_name, c.remarks", cat_, sch_)
            + " ORDER BY c.table_name, c.ordinal_position"
        )

    def hits(flags: list[str]) -> str:
        """How many words found a row: the rows most words found come first, so a row that
        holds several words of the question is not lost behind pages of a common word."""
        return "(" + " + ".join(f"CAST({f} AS integer)" for f in flags) + ")"

    def per_word_sql(phase: str, cat_: str, sch_: str) -> str:
        """The statement of one phase of the per-word pass: one flag per word, true when
        that word alone finds the row, the rows most words found first, then in the order
        of the first pass (stable, so a cursor's offset holds)."""
        if phase == TABLES:
            # A table whose own text holds a word, or that holds a column a word finds: the
            # columns are read once, grouped by table, one flag per word.
            table_flags = [
                f"coalesce(lower(s.table_name) LIKE {p} ESCAPE '!' "
                f"OR lower(s.comment) LIKE {p} ESCAPE '!' OR h.w{i}, false)"
                for i, p in enumerate(patterns)
            ]
            flags = ", ".join(table_flags)
            held = ", ".join(f"bool_or(coalesce({o}, false)) AS w{i}" for i, o in enumerate(own))
            return (
                f"SELECT s.table_name, s.comment, {flags} FROM {TABLE_COMMENTS} s "
                f"LEFT JOIN (SELECT c.table_name, {held} FROM {JDBC_COLUMNS} c "
                f"WHERE c.table_cat = {cat_} AND c.table_schem = {sch_} AND {any_own} "
                "GROUP BY c.table_name) h ON h.table_name = s.table_name "
                f"WHERE s.catalog_name = {cat_} AND s.schema_name = {sch_} "
                f"AND ({any_word(words, ('s.table_name', 's.comment'))} "
                f"OR h.table_name IS NOT NULL) ORDER BY {hits(table_flags)} DESC, s.table_name"
            )
        column_flags = [f"coalesce({o}, false)" for o in own]
        return (
            f"SELECT c.table_name, c.column_name, c.type_name, c.remarks, "
            f"{', '.join(column_flags)} "
            f"FROM {JDBC_COLUMNS} c WHERE c.table_cat = {cat_} AND c.table_schem = {sch_} "
            f"AND {any_own} ORDER BY {hits(column_flags)} DESC, c.table_name, c.ordinal_position"
        )

    def read(
        mode: str,
        phase: str,
        cat: str,
        sch: str,
        offset: int,
        room: int,
        cancel: Optional[Cancellation] = None,
    ) -> dict:
        """The matching tables, or columns, of one schema from row ``offset``, ``room`` of
        them at most, for the search of every word or for its per-word pass. Every view
        read is filtered by equality on the catalog and the schema, the joined ones too,
        so Trino reads that schema only."""
        cat_, sch_ = sql_literal(cat), sql_literal(sch)
        sql = (every_word_sql if mode == EVERY_WORD else per_word_sql)(phase, cat_, sch_)
        return ctx.run(
            f"{sql} OFFSET {offset} LIMIT {room + 1}",
            max_rows=room,
            timeout=allowed(),
            **stoppable(cancel),
        )

    def found_by(flags: list) -> list[str]:
        """The words whose flag is true, in the order given."""
        return [w for w, hit in zip(words, flags) if hit]

    def stalled(begin: Optional[Cursor], cat: str, sch: str, phase: str, offset: int) -> bool:
        """Whether a stop here is exactly where this call resumed, so it moved nothing: with
        every read bounded by the call (no query timeout of its own, or one as long as the
        call), resuming here again would stop every later call at the same place."""
        return begin is not None and (cat, sch, phase, offset) == (
            begin.catalog,
            begin.schema,
            begin.phase,
            begin.row_offset,
        )

    def walk(
        mode: str,
        begin: Optional[Cursor],
        cat: str,
        used: int,
        cancelled: Optional[Cancellation],
    ) -> list[tuple]:
        """One catalog of a pass, from ``begin`` when it is that catalog's, as the events
        the pass then replays in order: ``used`` rows already taken before it (0 when the
        catalog is read at the same time as others, which then take theirs first).

        A read asks for as many rows as the page has room for after ``used``: read alone,
        that is the room the replay finds, and read at the same time as others, it is
        more, of which the replay takes the first rows. The order of each read is total,
        so its first rows are the rows a read of less room answers. A walk stops where the
        pass would: on the row cap, on a stop for time or on a refused view."""
        events: list[tuple] = []
        at = begin if begin is not None and cat == begin.catalog else None
        first = at.schema if at is not None else ""
        here = (first, *((at.phase, at.row_offset) if at is not None else (TABLES, 0)))

        def ask() -> None:
            """Before a statement: none starts once the pass no longer needs this catalog."""
            if cancelled is not None and cancelled.is_set():
                raise _Abandoned

        try:
            governed_catalog(cat)
        except ValueError:  # a name the statement cannot carry is named, never queried
            return [("gap", _not_searched(cat, None, INVALID_ARGUMENT))]
        schemas = [only_schema]
        if not only_schema:
            events.append(("open", here))
            if used >= ctx.page_size:
                return events
            ask()
            try:
                schemas = schemas_of(cat, first, cancelled)
            except CallTimeout:
                raise
            except TimeoutError:
                # Slower than one query may be, or than a whole call: resuming would not help.
                if time_remains() or stalled(begin, cat, *here):
                    return [*events, ("gap", _not_searched(cat, None, TIME_BUDGET))]
                return [*events, ("stop", (*here, TIME_BUDGET))]
            except Exception as e:  # noqa: BLE001 - this catalog only, said in the answer
                return [*events, ("gap", _not_searched(cat, None, explain_error(e)["kind"]))]
        for sch in schemas:
            phase, offset = (
                (at.phase, at.row_offset) if at is not None and sch == first else (TABLES, 0)
            )
            skipped = False
            for phase in _PHASES[_PHASES.index(phase) :]:
                events.append(("before", sch, phase, offset))
                room = ctx.page_size - used
                if room <= 0:
                    return events
                ask()
                try:
                    res = read(mode, phase, cat, sch, offset, room, cancelled)
                except CallTimeout:
                    raise
                except TimeoutError:
                    # Slower than one query may be, or than a whole call: named and skipped,
                    # or every resumed call would stop here again. Stopped by what was left
                    # of the call after some progress: resumed here.
                    if time_remains() or stalled(begin, cat, sch, phase, offset):
                        events.append(("gap", _not_searched(cat, sch, TIME_BUDGET)))
                        skipped = True
                        break
                    return [*events, ("stop", (sch, phase, offset, TIME_BUDGET))]
                except Exception as e:  # noqa: BLE001 - this schema only, said in the answer
                    out = metadata_view_error(e)
                    if out["kind"] in VIEW_REFUSED:  # a view itself: nothing is searched
                        return [*events, ("error", out, error_code(e))]
                    events.append(("gap", _not_searched(cat, sch, out["kind"])))
                    skipped = True
                    break
                rows = []
                for row in res["rows"][:room]:
                    where = {"catalog": cat, "schema": sch, "table": row[0]}
                    if phase == TABLES:
                        entry = {**where, "comment": row[1]}
                    else:
                        entry = {**where, "column": row[1], "type": row[2], "comment": row[3]}
                    if mode == PER_WORD:
                        entry["words"] = found_by(row[2:] if phase == TABLES else row[4:])
                    rows.append(entry)
                more = bool(res.get("truncated")) or len(res["rows"]) > room
                events.append(("rows", sch, phase, offset, rows, more))
                used += len(rows)
                if more:
                    return events
                offset = 0
            if not skipped:
                events.append(("searched",))
        return events

    def scan(mode: str, begin: Optional[Cursor]) -> dict:
        """One pass over the scope, from ``begin`` (None: from the start), within the row
        cap and the call's time: what it found, what it skipped, where it stopped. A
        refused metadata view answers its error, as ``{"error"}``.

        The catalogs are walked one at a time, or ``concurrency`` at once; either way their
        events are replayed in the order of the catalogs, against the row cap the pass has
        left, so the answer and the cursor are those of the walk one at a time: what a
        catalog after the stop found is never served, and resuming reads it again."""
        tables: list[dict] = []
        matches: list[dict] = []
        gaps: list[dict] = []
        searched = 0
        catalogs = all_catalogs
        if begin is not None:
            catalogs = [c for c in catalogs if c >= begin.catalog]
        # Where the pass stopped: the catalog's index, the schema ("" for the catalog's
        # first), the phase and the row offset in it, and why.
        stop: Optional[tuple[int, str, str, int, str]] = None

        def replay(index: int, events: list[tuple]) -> Optional[dict]:
            """The events of the catalog at ``index``, taken in order until the pass
            stops; a refused view's error, or None."""
            nonlocal stop, searched
            for event in events:
                kind = event[0]
                used = len(tables) + len(matches)
                if kind == "gap":
                    gaps.append(event[1])
                elif kind == "open" and used >= ctx.page_size:
                    stop = (index, *event[1], ROW_CAP)
                elif kind == "before" and used >= ctx.page_size:
                    stop = (index, *event[1:], ROW_CAP)
                elif kind == "rows":
                    _, sch, phase, offset, rows, more = event
                    room = ctx.page_size - used
                    taken = rows[:room]
                    (tables if phase == TABLES else matches).extend(taken)
                    if more or len(rows) > room:
                        stop = (index, sch, phase, offset + len(taken), ROW_CAP)
                elif kind == "stop":
                    stop = (index, *event[1])
                elif kind == "error":
                    note(error=event[2])
                    return {"error": event[1]}
                elif kind == "searched":
                    searched += 1
                if stop is not None:
                    return None
            return None

        if concurrency <= 1 or len(catalogs) <= 1:
            for index, cat in enumerate(catalogs):
                failed = replay(index, walk(mode, begin, cat, len(tables) + len(matches), None))
                if failed is not None:
                    return failed
                if stop is not None:
                    break
        else:
            # A window of ``concurrency`` catalogs past the one replayed: each walked in a
            # thread that takes one of ``slots`` while it runs; with none free, the call
            # walks its next catalog itself, as one at a time, so it always advances.
            cancelled = Cancellation()
            free = slots if slots is not None else threading.BoundedSemaphore(concurrency)
            pool = ThreadPoolExecutor(
                max_workers=min(concurrency, len(catalogs)), thread_name_prefix="mcp-search"
            )
            futures: dict[int, Future] = {}
            ahead = 0  # the catalogs before this one are in a thread or read

            def walk_alone(cat: str) -> Optional[list[tuple]]:
                """The walk of ``cat`` read at the same time as others; None once the pass
                no longer needs it."""
                try:
                    return walk(mode, begin, cat, 0, cancelled)
                except _Abandoned:
                    return None

            def fill(end: int) -> None:
                """Start the catalogs up to ``end``, while a slot is free."""
                nonlocal ahead
                while ahead < min(end, len(catalogs)) and free.acquire(blocking=False):
                    # Each walk runs as the caller, within the call: the context of this call.
                    futures[ahead] = pool.submit(
                        contextvars.copy_context().run, walk_alone, catalogs[ahead]
                    )
                    # Its slot is given back once it ends, or once it is cancelled unstarted.
                    futures[ahead].add_done_callback(lambda _: free.release())
                    ahead += 1

            try:
                fill(concurrency)
                for index, cat in enumerate(catalogs):
                    if index in futures:
                        events = futures.pop(index).result() or []
                    else:
                        events = walk(mode, begin, cat, len(tables) + len(matches), None)
                        ahead = index + 1
                    failed = replay(index, events)
                    if failed is not None:
                        return failed
                    if stop is not None:
                        break
                    fill(index + 1 + concurrency)
            finally:
                # The pass needs nothing more: the reads in flight are cancelled in Trino at
                # once, none starts, and no thread outlives the call.
                cancelled.cancel()
                pool.shutdown(wait=True, cancel_futures=True)
        return {
            "tables": tables,
            "matches": matches,
            "gaps": gaps,
            "searched": searched,
            "stop": stop,
            "catalogs": catalogs,
        }

    def answer(mode: str, part: dict, begin: Optional[Cursor], searched: int) -> dict:
        """The reply for a pass that ``begin`` resumed (None: started in this call), with
        the schemas this call finished in that pass, the cursor that resumes it, and the
        hint: on a page of the per-word pass with more to read, the one that sends to
        next_cursor."""
        earlier_gaps = begin is not None and begin.gaps
        earlier_found = begin is not None and begin.found
        tables, matches, gaps, stop = part["tables"], part["matches"], part["gaps"], part["stop"]
        not_searched = list(gaps)
        next_cursor = None
        if stop is not None:
            catalogs = part["catalogs"]
            index, sch, phase, offset, reason = stop
            # The schema it stopped in (from there on in its catalog), or the catalog it did
            # not open, then every catalog after it, never opened.
            not_searched.append(_not_searched(catalogs[index], sch or None, reason))
            not_searched += [_not_searched(c, None, reason) for c in catalogs[index + 1 :]]
            resume = Cursor(
                catalogs[index],
                sch,
                offset,
                query=digest,
                gaps=bool(gaps) or earlier_gaps,
                phase=phase,
                found=bool(tables or matches) or earlier_found,
                mode=mode,
            )
            next_cursor = encode_cursor(resume)
        complete = not gaps and stop is None and not earlier_gaps
        note(rows=len(tables) + len(matches), truncated=not complete)
        out: dict[str, Any] = {
            "tables": tables,
            "matches": matches,
            "schemas_searched": searched,
            "not_searched": not_searched,
            "complete": complete,
            "next_cursor": next_cursor,
        }
        if mode == PER_WORD:
            out["partial"] = True
            if next_cursor is not None:
                out["hint"] = PER_WORD_MORE_HINT
            elif tables or matches or earlier_found:
                out["hint"] = PER_WORD_HINT
            else:  # nothing found by the pass, complete or with a part skipped
                out["hint"] = HINT
        elif complete and not tables and not matches and not earlier_found:
            out["hint"] = HINT
        return out

    all_catalogs = (
        [only_catalog] if only_catalog else [c.lower() for c in ctx.visible_catalogs(max_catalogs)]
    )
    if start is not None and start.catalog not in all_catalogs:
        raise ValueError(_INVALID_CURSOR)  # hidden now, or never shown: not this search's
    if start is not None and start.mode == PER_WORD:
        part = scan(PER_WORD, start)
        if "error" in part:
            return part["error"]
        return answer(PER_WORD, part, start, part["searched"])
    part = scan(EVERY_WORD, start)
    if "error" in part:
        return part["error"]
    whole = answer(EVERY_WORD, part, start, part["searched"])
    if len(words) < 2 or "hint" not in whole:
        return whole
    # Complete, and nothing holds every word on any page: each word alone, in the time and
    # the row cap left, from the start of the scope.
    per_word = scan(PER_WORD, None)
    if "error" in per_word:
        return per_word["error"]
    # The page speaks of the per-word pass: its schemas only, never the first pass's twice.
    return answer(PER_WORD, per_word, None, per_word["searched"])
