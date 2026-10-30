"""search_columns: the tables and columns that hold every word of a text, within the call's time.

The text is cut into words (whitespace and punctuation split them). A column matches when
every word appears in its name, its comment, its table's name or its table's comment; a
table matches when every word appears in its name or its comment. The search reads
``system.metadata.table_comments`` and ``system.jdbc.columns``, which Trino filters for each
user, one schema at a time, in a stable order: the catalogs the caller sees (never
``system``), sorted, and in each its schemas, sorted; in a schema its tables first, then its
columns. Each query is filtered by equality on catalog and schema, which Trino pushes down
to the views, so it loads that schema only, and a catalog of many thousand tables answers
schema by schema instead of not at all.

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
import hashlib
import json
import re
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
from .trino_client import every_row

# Why a part of the scope was not searched, besides an error's kind.
ROW_CAP = "row_cap"
# The two reads of a schema, in this order: its matching tables, then its matching columns.
TABLES = "tables"
COLUMNS = "columns"
_PHASES = (TABLES, COLUMNS)
# What a complete search that found nothing says, so that an agent tries another way
# rather than conclude that the data does not exist.
HINT = (
    "Nothing matched every word. Try fewer words or other words (a synonym, a column or "
    "table name), or browse with list_tables and describe_schema."
)
_WORD = re.compile(r"\w+")
# What a cursor may hold: a schema name of this many characters at most, a row offset Trino
# reads as a bigint.
MAX_NAME_LENGTH = 1024
MAX_ROW_OFFSET = 2**63 - 1
_INVALID_CURSOR = (
    "Invalid cursor: pass back next_cursor unchanged, or start the search again without a cursor"
)


def like_contains(text: str) -> str:
    """The ``LIKE`` literal that matches ``text`` anywhere, lower-cased, written with
    ``ESCAPE '!'``: ``!``, ``%`` and ``_`` are escaped, so each stands for itself."""
    escaped = text.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return sql_literal(f"%{escaped}%")


def search_words(text: str, min_length: int) -> list[str]:
    """The words of ``text`` the search looks for, lower-cased, each once, in the order given.

    A word is a run of letters, digits and ``_``: whitespace and punctuation split them, so
    a word holds no quote and no ``%``. A word shorter than ``min_length`` is dropped."""
    return list(dict.fromkeys(w for w in _WORD.findall(text.lower()) if len(w) >= min_length))


def every_word(words: list[str], fields: tuple[str, ...]) -> str:
    """The condition that each word appears, ignoring case, in one of ``fields`` at least:
    one group of ORs per word, the groups joined by AND."""
    return " AND ".join(
        "(" + " OR ".join(f"lower({f}) LIKE {pattern} ESCAPE '!'" for f in fields) + ")"
        for pattern in map(like_contains, words)
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
    ``row_offset`` matching rows of that phase. ``query`` ties it to its search, and
    ``gaps`` says an earlier call of the search left a part of it not searched."""

    catalog: str
    schema: str
    row_offset: int
    query: str = ""
    gaps: bool = False
    phase: str = TABLES


def encode_cursor(cursor: Cursor) -> str:
    """The cursor as an opaque token for the agent: url-safe base64 of a JSON object."""
    data = {
        "c": cursor.catalog,
        "s": cursor.schema,
        "p": cursor.phase,
        "o": cursor.row_offset,
        "q": cursor.query,
        "g": cursor.gaps,
    }
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def decode_cursor(token: str) -> Cursor:
    """The cursor ``token`` encodes, every field checked; anything else raises ValueError.

    The catalog passes ``governed_catalog`` (never ``system``); the search then requires it
    to be one the caller sees. The schema, empty for a catalog's first, comes from Trino's
    own listing and only ever enters a string literal: any lower-case name without NUL, up to
    ``MAX_NAME_LENGTH`` characters, so every cursor the server gives resumes, a Hive schema
    such as ``2024_sales`` included. The phase is ``tables`` or ``columns``, the offset a
    whole number from 0 to ``MAX_ROW_OFFSET``."""
    try:
        data = json.loads(base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True))
        if not isinstance(data, dict):
            raise ValueError(_INVALID_CURSOR)
        catalog, schema, offset = data["c"], data["s"], data["o"]
        query, gaps, phase = data["q"], data["g"], data["p"]
        if not (isinstance(catalog, str) and isinstance(schema, str) and isinstance(query, str)):
            raise ValueError(_INVALID_CURSOR)
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError(_INVALID_CURSOR)
        if not 0 <= offset <= MAX_ROW_OFFSET or not isinstance(gaps, bool):
            raise ValueError(_INVALID_CURSOR)
        if phase not in _PHASES:
            raise ValueError(_INVALID_CURSOR)
        if len(schema) > MAX_NAME_LENGTH or "\0" in schema or schema != schema.lower():
            raise ValueError(_INVALID_CURSOR)
        governed_catalog(catalog)
    except (ValueError, KeyError, binascii.Error) as exc:
        raise ValueError(_INVALID_CURSOR) from exc
    return Cursor(catalog, schema, offset, query=query, gaps=gaps, phase=phase)


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
    reserve_seconds: float,
    max_catalogs: int,
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
    without a call budget, and a cursor that is not this search's raise ValueError before
    Trino is asked anything. ``reserve_seconds`` of the call's budget are kept so the search
    ends with an answer. A refused metadata view answers its error, which names what to
    allow. A complete search that found nothing adds ``hint``."""
    words = search_words(text, min_length)
    if not words:
        raise ValueError(
            f"Search text has no word of {min_length} characters or more (shorter words are "
            "ignored): give at least one, so the search does not read every column"
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

    def stalled(cat: str, sch: str, phase: str, offset: int) -> bool:
        """Whether a stop here is exactly where this call resumed, so it moved nothing: with
        every read bounded by the call (no query timeout of its own, or one as long as the
        call), resuming here again would stop every later call at the same place."""
        return start is not None and (cat, sch, phase, offset) == (
            start.catalog,
            start.schema,
            start.phase,
            start.row_offset,
        )

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

    def schemas_of(cat: str, first: str) -> list[str]:
        """The schemas of ``cat`` from ``first`` on, sorted, without information_schema."""
        sql = (
            f"SELECT schema_name FROM {cat}.information_schema.schemata "
            f"WHERE schema_name >= {sql_literal(first)} "
            "AND schema_name <> 'information_schema' ORDER BY schema_name"
        )
        return [str(r[0]) for r in every_row(lambda s: ctx.run(s, timeout=allowed()), sql)]

    if only_schema and not ctx.schema_visible(only_catalog, only_schema, timeout=allowed()):
        # system.jdbc.columns filtered on a schema the caller does not see answers no row and
        # no error: without this check the search would say it searched it, completely.
        note(error="schema_not_found")
        return {
            "error": f"Schema {catalog}.{schema} not found or not visible",
            "kind": "schema",
        }

    table_words = every_word(words, ("table_name", "comment"))
    column_words = every_word(words, ("c.column_name", "c.remarks", "c.table_name", "t.comment"))

    def read(phase: str, cat: str, sch: str, offset: int, room: int) -> dict:
        """The matching tables, or columns, of one schema from row ``offset``, ``room`` of
        them at most. Both views are filtered by equality on the catalog and the schema,
        the join's side too, so Trino reads that schema only."""
        cat_, sch_ = sql_literal(cat), sql_literal(sch)
        if phase == TABLES:
            sql = (
                f"SELECT table_name, comment FROM {TABLE_COMMENTS} "
                f"WHERE catalog_name = {cat_} AND schema_name = {sch_} AND {table_words} "
                "ORDER BY table_name"
            )
        else:
            sql = (
                "SELECT c.table_name, c.column_name, c.type_name, c.remarks "
                f"FROM {JDBC_COLUMNS} c LEFT JOIN {TABLE_COMMENTS} t "
                f"ON t.catalog_name = {cat_} AND t.schema_name = {sch_} "
                "AND t.table_name = c.table_name "
                f"WHERE c.table_cat = {cat_} AND c.table_schem = {sch_} AND {column_words} "
                "ORDER BY c.table_name, c.ordinal_position"
            )
        return ctx.run(f"{sql} OFFSET {offset} LIMIT {room + 1}", max_rows=room, timeout=allowed())

    catalogs = (
        [only_catalog] if only_catalog else [c.lower() for c in ctx.visible_catalogs(max_catalogs)]
    )
    if start is not None:
        if start.catalog not in catalogs:  # hidden now, or never shown: not this search's
            raise ValueError(_INVALID_CURSOR)
        catalogs = [c for c in catalogs if c >= start.catalog]
    tables: list[dict] = []
    matches: list[dict] = []
    gaps: list[dict] = []
    searched = 0
    # Where the search stopped: the catalog's index, the schema ("" for the catalog's
    # first), the phase and the row offset in it, and why.
    stop: Optional[tuple[int, str, str, int, str]] = None
    for index, cat in enumerate(catalogs):
        at = start if start is not None and cat == start.catalog else None
        first = at.schema if at is not None else ""
        # Before its schemas are read, the search of this catalog resumes where it was.
        here = (index, first, *((at.phase, at.row_offset) if at is not None else (TABLES, 0)))
        try:
            governed_catalog(cat)
        except ValueError:  # a name the statement cannot carry is named, never queried
            gaps.append(_not_searched(cat, None, INVALID_ARGUMENT))
            continue
        schemas = [only_schema]
        if not only_schema:
            if len(tables) + len(matches) >= ctx.page_size:
                stop = (*here, ROW_CAP)
                break
            try:
                schemas = schemas_of(cat, first)
            except CallTimeout:
                raise
            except TimeoutError:
                # Slower than one query may be, or than a whole call: resuming would not help.
                if time_remains() or stalled(cat, *here[1:]):
                    gaps.append(_not_searched(cat, None, TIME_BUDGET))
                    continue
                stop = (*here, TIME_BUDGET)
                break
            except Exception as e:  # noqa: BLE001 - this catalog only, said in the answer
                gaps.append(_not_searched(cat, None, explain_error(e)["kind"]))
                continue
        for sch in schemas:
            phase, offset = (
                (at.phase, at.row_offset) if at is not None and sch == first else (TABLES, 0)
            )
            skipped = False
            for phase in _PHASES[_PHASES.index(phase) :]:
                room = ctx.page_size - len(tables) - len(matches)
                if room <= 0:
                    stop = (index, sch, phase, offset, ROW_CAP)
                    break
                try:
                    res = read(phase, cat, sch, offset, room)
                except CallTimeout:
                    raise
                except TimeoutError:
                    # Slower than one query may be, or than a whole call: named and skipped,
                    # or every resumed call would stop here again. Stopped by what was left
                    # of the call after some progress: resumed here.
                    if time_remains() or stalled(cat, sch, phase, offset):
                        gaps.append(_not_searched(cat, sch, TIME_BUDGET))
                        skipped = True
                        break
                    stop = (index, sch, phase, offset, TIME_BUDGET)
                    break
                except Exception as e:  # noqa: BLE001 - this schema only, said in the answer
                    out = metadata_view_error(e)
                    if out["kind"] in VIEW_REFUSED:  # a view itself: nothing can be searched
                        note(error=error_code(e))
                        return out
                    gaps.append(_not_searched(cat, sch, out["kind"]))
                    skipped = True
                    break
                rows = res["rows"][:room]
                if phase == TABLES:
                    tables += [
                        {"catalog": cat, "schema": sch, "table": table, "comment": comment}
                        for table, comment in rows
                    ]
                else:
                    matches += [
                        {
                            "catalog": cat,
                            "schema": sch,
                            "table": table,
                            "column": column,
                            "type": kind,
                            "comment": comment,
                        }
                        for table, column, kind, comment in rows
                    ]
                if res.get("truncated") or len(res["rows"]) > room:
                    stop = (index, sch, phase, offset + len(rows), ROW_CAP)
                    break
                offset = 0
            if stop is not None:
                break
            if not skipped:
                searched += 1
        if stop is not None:
            break
    earlier_gaps = start is not None and start.gaps
    not_searched = list(gaps)
    next_cursor = None
    if stop is not None:
        index, sch, phase, offset, reason = stop
        # The schema it stopped in (from there on in its catalog), or the catalog it did not
        # open, then every catalog after it, never opened.
        not_searched.append(_not_searched(catalogs[index], sch or None, reason))
        not_searched += [_not_searched(c, None, reason) for c in catalogs[index + 1 :]]
        resume = Cursor(
            catalogs[index],
            sch,
            offset,
            query=digest,
            gaps=bool(gaps) or earlier_gaps,
            phase=phase,
        )
        next_cursor = encode_cursor(resume)
    complete = not gaps and stop is None and not earlier_gaps
    note(rows=len(tables) + len(matches), truncated=not complete)
    answer = {
        "tables": tables,
        "matches": matches,
        "schemas_searched": searched,
        "not_searched": not_searched,
        "complete": complete,
        "next_cursor": next_cursor,
    }
    if complete and not tables and not matches:
        answer["hint"] = HINT
    return answer
