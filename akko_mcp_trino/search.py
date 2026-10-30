"""search_columns: the columns whose name or comment contains a text, within the call's time.

The search reads ``system.jdbc.columns``, which Trino filters for each user, one schema at a
time, in a stable order: the catalogs the caller sees (never ``system``), sorted, and in each
its schemas, sorted. A query filtered by catalog and schema loads that schema only, so a
catalog of many thousand tables answers schema by schema instead of not at all.

The search never claims more than it did. Before each schema it checks what is left of the
call's budget, keeping a reserve so that it ends with an answer, not with the call's own
error; each query is bounded by what is left. It stops on the row cap too. Either way the
answer says what was not searched and why, and a cursor resumes the search exactly there,
inside a schema if the row cap stopped it there. ``complete`` is true only when a search,
over the calls of one chain of cursors, read everything in its scope.

The agent's text enters the statement as one escaped ``LIKE`` literal (``like_contains``):
its quotes cannot end the literal, and its ``%``, ``_`` and ``!`` are characters, never
wildcards. The audit keeps how many matches came and whether the search was cut, never the
text, which may be a value.
"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from .audit import CallTimeout, current_call, error_code, note
from .errors import INVALID_ARGUMENT, TIME_BUDGET, VIEW_REFUSED, explain_error, metadata_view_error
from .sql_guard import JDBC_COLUMNS, governed_catalog, sql_literal, validate_identifier
from .trino_client import every_row

# Why a part of the scope was not searched, besides an error's kind.
ROW_CAP = "row_cap"
# What a cursor may hold: a schema name of this many characters at most, a row offset Trino
# reads as a bigint.
MAX_NAME_LENGTH = 1024
MAX_ROW_OFFSET = 2**63 - 1
_INVALID_CURSOR = "Invalid cursor: pass back next_cursor unchanged"


def like_contains(text: str) -> str:
    """The ``LIKE`` literal that matches ``text`` anywhere, lower-cased, written with
    ``ESCAPE '!'``: ``!``, ``%`` and ``_`` are escaped, so each stands for itself."""
    escaped = text.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return sql_literal(f"%{escaped}%")


def query_digest(text: str, catalog: str, schema: str) -> str:
    """What ties a cursor to its search: a digest of the lower-cased text and of the catalog
    and schema it was given (empty when not given), already folded.

    It guards against a mistake, a cursor kept for another search, not against forgery: an
    agent can compute it, and gains nothing by it, since Trino decides what any search reads."""
    data = json.dumps([text.lower(), catalog, schema]).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass(frozen=True)
class Cursor:
    """Where a search resumes: in ``catalog``, from ``schema`` (empty: its first schema),
    after its first ``row_offset`` matching rows. ``query`` ties it to its search, and
    ``gaps`` says an earlier call of the search left a part of it not searched."""

    catalog: str
    schema: str
    row_offset: int
    query: str = ""
    gaps: bool = False


def encode_cursor(cursor: Cursor) -> str:
    """The cursor as an opaque token for the agent: url-safe base64 of a JSON object."""
    data = {
        "c": cursor.catalog,
        "s": cursor.schema,
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
    such as ``2024_sales`` included. The offset is a whole number from 0 to ``MAX_ROW_OFFSET``."""
    try:
        data = json.loads(base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True))
        if not isinstance(data, dict):
            raise ValueError(_INVALID_CURSOR)
        catalog, schema, offset = data["c"], data["s"], data["o"]
        query, gaps = data["q"], data["g"]
        if not (isinstance(catalog, str) and isinstance(schema, str) and isinstance(query, str)):
            raise ValueError(_INVALID_CURSOR)
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError(_INVALID_CURSOR)
        if not 0 <= offset <= MAX_ROW_OFFSET or not isinstance(gaps, bool):
            raise ValueError(_INVALID_CURSOR)
        if len(schema) > MAX_NAME_LENGTH or "\0" in schema or schema != schema.lower():
            raise ValueError(_INVALID_CURSOR)
        governed_catalog(catalog)
    except (ValueError, KeyError, binascii.Error) as exc:
        raise ValueError(_INVALID_CURSOR) from exc
    return Cursor(catalog, schema, offset, query=query, gaps=gaps)


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
    """Search the columns whose name or comment contains ``text``, for the verified caller.

    ``ctx`` is the `ToolContext`: its ``run`` queries Trino, its ``page_size`` (the row cap)
    bounds the matches of one answer, its ``visible_catalogs`` lists the catalogs. With
    ``catalog``, only its schemas are searched; with ``schema`` too, only that one. A text
    shorter than ``min_length``, a schema without its catalog, every catalog without a call
    budget, and a cursor that is not this search's raise ValueError before Trino is asked
    anything. ``reserve_seconds`` of the call's budget are kept so the search ends with an
    answer. A refused metadata view answers its error, which names what to allow."""
    wanted = text.strip()
    if len(wanted) < min_length:
        raise ValueError(
            f"Search text too short: give at least {min_length} characters, "
            "so the search does not read every column"
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
    digest = query_digest(wanted, only_catalog, only_schema)
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

    def schemas_of(cat: str, first: str) -> list[str]:
        """The schemas of ``cat`` from ``first`` on, sorted, without information_schema."""
        sql = (
            f"SELECT schema_name FROM {cat}.information_schema.schemata "
            f"WHERE schema_name >= {sql_literal(first)} "
            "AND schema_name <> 'information_schema' ORDER BY schema_name"
        )
        return [str(r[0]) for r in every_row(lambda s: ctx.run(s, timeout=allowed()), sql)]

    pattern = like_contains(wanted)

    def columns_of(cat: str, sch: str, offset: int, room: int) -> dict:
        """The matching columns of one schema from row ``offset``, ``room`` of them at most."""
        return ctx.run(
            "SELECT table_name, column_name, type_name, remarks "
            f"FROM {JDBC_COLUMNS} WHERE table_cat = {sql_literal(cat)} "
            f"AND table_schem = {sql_literal(sch)} "
            f"AND (lower(column_name) LIKE {pattern} ESCAPE '!' "
            f"OR lower(remarks) LIKE {pattern} ESCAPE '!') "
            f"ORDER BY table_name, ordinal_position OFFSET {offset} LIMIT {room + 1}",
            max_rows=room,
            timeout=allowed(),
        )

    catalogs = (
        [only_catalog] if only_catalog else [c.lower() for c in ctx.visible_catalogs(max_catalogs)]
    )
    if start is not None:
        if start.catalog not in catalogs:  # hidden now, or never shown: not this search's
            raise ValueError(_INVALID_CURSOR)
        catalogs = [c for c in catalogs if c >= start.catalog]
    matches: list[dict] = []
    gaps: list[dict] = []
    searched = 0
    # Where the search stopped: the catalog's index, the schema ("" for the catalog's
    # first), the row offset in it, and why.
    stop: Optional[tuple[int, str, int, str]] = None
    for index, cat in enumerate(catalogs):
        at = start if start is not None and cat == start.catalog else None
        first = at.schema if at is not None else ""
        # Before its schemas are read, the search of this catalog resumes where it was.
        here = (index, first, at.row_offset if at is not None else 0)
        try:
            governed_catalog(cat)
        except ValueError:  # a name the statement cannot carry is named, never queried
            gaps.append(_not_searched(cat, None, INVALID_ARGUMENT))
            continue
        schemas = [only_schema]
        if not only_schema:
            if len(matches) >= ctx.page_size:
                stop = (*here, ROW_CAP)
                break
            try:
                schemas = schemas_of(cat, first)
            except CallTimeout:
                raise
            except TimeoutError:
                if time_remains():  # slower than one query may be: resuming would not help
                    gaps.append(_not_searched(cat, None, TIME_BUDGET))
                    continue
                stop = (*here, TIME_BUDGET)
                break
            except Exception as e:  # noqa: BLE001 - this catalog only, said in the answer
                gaps.append(_not_searched(cat, None, explain_error(e)["kind"]))
                continue
        for sch in schemas:
            offset = at.row_offset if at is not None and sch == first else 0
            room = ctx.page_size - len(matches)
            if room <= 0:
                stop = (index, sch, offset, ROW_CAP)
                break
            try:
                res = columns_of(cat, sch, offset, room)
            except CallTimeout:
                raise
            except TimeoutError:
                # Slower than one query may be: named and skipped, or every resumed call would
                # stop here again. Stopped by what was left of the call: resumed here.
                if time_remains():
                    gaps.append(_not_searched(cat, sch, TIME_BUDGET))
                    continue
                stop = (index, sch, offset, TIME_BUDGET)
                break
            except Exception as e:  # noqa: BLE001 - this schema only, said in the answer
                out = metadata_view_error(e)
                if out["kind"] in VIEW_REFUSED:  # the view itself: nothing can be searched
                    note(error=error_code(e))
                    return out
                gaps.append(_not_searched(cat, sch, out["kind"]))
                continue
            rows = res["rows"][:room]
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
                stop = (index, sch, offset + len(rows), ROW_CAP)
                break
            searched += 1
        if stop is not None:
            break
    earlier_gaps = start is not None and start.gaps
    not_searched = list(gaps)
    next_cursor = None
    if stop is not None:
        index, sch, offset, reason = stop
        # The schema it stopped in (from there on in its catalog), or the catalog it did not
        # open, then every catalog after it, never opened.
        not_searched.append(_not_searched(catalogs[index], sch or None, reason))
        not_searched += [_not_searched(c, None, reason) for c in catalogs[index + 1 :]]
        resume = Cursor(catalogs[index], sch, offset, query=digest, gaps=bool(gaps) or earlier_gaps)
        next_cursor = encode_cursor(resume)
    complete = not gaps and stop is None and not earlier_gaps
    note(rows=len(matches), truncated=not complete)
    return {
        "matches": matches,
        "schemas_searched": searched,
        "not_searched": not_searched,
        "complete": complete,
        "next_cursor": next_cursor,
    }
