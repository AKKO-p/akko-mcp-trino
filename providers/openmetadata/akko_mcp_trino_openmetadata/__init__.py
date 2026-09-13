"""OpenMetadata context provider for akko-mcp-trino.

Reads what the catalogue knows about a table — description, owners, tier,
tags, primary key, foreign keys, column descriptions and classifications such
as ``PII.Sensitive`` — and hands it to the server's ``describe_table`` and
``explain_table``. It only ever reads metadata with a bot token; the data
itself still flows through Trino under the user's identity.

Registered as the context provider ``openmetadata`` (entry point
``akko_mcp_trino.context``):

    pip install akko-mcp-trino-openmetadata
    export MCP_CONTEXT_PROVIDERS=openmetadata,trino-comments
    export OPENMETADATA_URL=http://openmetadata:8585
    export OPENMETADATA_TOKEN=<bot token>
    export OPENMETADATA_SERVICE=<the database service name of your Trino in OpenMetadata>

Nothing known, a refused token, or a catalogue that is down all mean "nothing
known": the agent gets Trino alone, never an error.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

import httpx
from akko_mcp_trino.context import ColumnContext, Join, TableContext

__version__ = "0.1.0"

log = logging.getLogger(__name__)

_FIELDS = "description,owners,tags,columns,tableConstraints"
_TIER_PREFIX = "Tier."


class OpenMetadataContext:
    """A ``ContextProvider`` backed by the OpenMetadata REST API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        service: str,
        *,
        timeout: float = 5.0,
        table_ttl_seconds: float = 30.0,
        http: Optional[httpx.Client] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Talk to ``base_url`` with a bot ``token``; ``service`` prefixes every table FQN."""
        self._base = base_url.rstrip("/")
        self._service = service
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._ttl = table_ttl_seconds
        self._clock = clock
        self._tables: dict[str, tuple[float, Optional[dict]]] = {}
        self._lock = threading.Lock()

    def _fetch(self, catalog: str, schema: str, table: str) -> Optional[dict]:
        """The table document, cached briefly so one describe does not mean N calls."""
        key = f"{catalog}.{schema}.{table}"
        now = self._clock()
        with self._lock:
            hit = self._tables.get(key)
            if hit and hit[0] > now:
                return hit[1]
        fqn = quote(f"{self._service}.{key}", safe="")
        doc: Optional[dict] = None
        try:
            r = self._http.get(
                f"{self._base}/api/v1/tables/name/{fqn}",
                params={"fields": _FIELDS},
                headers=self._headers,
            )
            if r.status_code == 200:
                doc = r.json()
            elif r.status_code in (401, 403):
                log.warning("openmetadata refused the bot token (%s)", r.status_code)
            elif r.status_code != 404:
                log.info("openmetadata answered %s for %s", r.status_code, key)
        except Exception as exc:  # noqa: BLE001 - a catalogue down is "nothing known"
            log.info("openmetadata unreachable: %s", type(exc).__name__)
        with self._lock:
            self._tables[key] = (now + self._ttl, doc)
        return doc

    def table(self, catalog: str, schema: str, table: str) -> Optional[TableContext]:
        """Description, owners, tier, tags, primary key as grain, foreign keys as joins."""
        doc = self._fetch(catalog, schema, table)
        if not doc:
            return None
        tags = [str(t.get("tagFQN", "")) for t in doc.get("tags", []) if t.get("tagFQN")]
        tier = next((t[len(_TIER_PREFIX) :] for t in tags if t.startswith(_TIER_PREFIX)), "")
        grain: list[str] = []
        joins: list[Join] = []
        for c in doc.get("tableConstraints") or []:
            if c.get("constraintType") == "PRIMARY_KEY":
                grain = [str(x) for x in c.get("columns", [])]
            elif c.get("constraintType") == "FOREIGN_KEY":
                referred = [str(x) for x in c.get("referredColumns", [])]
                targets = {self._strip_service(x.rsplit(".", 1)[0]) for x in referred}
                joins.append(
                    Join(
                        columns=[str(x) for x in c.get("columns", [])],
                        target=next(iter(targets), ""),
                        target_columns=[x.rsplit(".", 1)[-1] for x in referred],
                    )
                )
        ctx = TableContext(
            description=str(doc.get("description") or ""),
            owner=", ".join(
                str(o.get("displayName") or o.get("name") or "") for o in doc.get("owners") or []
            ),
            tier=tier,
            grain=grain,
            joins=joins,
            tags=[t for t in tags if not t.startswith(_TIER_PREFIX)],
        )
        return ctx if ctx.as_dict() else None

    def column(self, catalog: str, schema: str, table: str, column: str) -> Optional[ColumnContext]:
        """Description and classification tags of one column."""
        doc = self._fetch(catalog, schema, table)
        if not doc:
            return None
        for c in doc.get("columns") or []:
            if c.get("name") == column:
                ctx = ColumnContext(
                    description=str(c.get("description") or ""),
                    classification=[
                        str(t.get("tagFQN", "")) for t in c.get("tags", []) if t.get("tagFQN")
                    ],
                )
                return ctx if ctx.as_dict() else None
        return None

    def _strip_service(self, fqn: str) -> str:
        prefix = self._service + "."
        return fqn[len(prefix) :] if fqn.startswith(prefix) else fqn


def from_env(env: Mapping[str, str], query: Callable[[str], dict]) -> Any:
    """Entry-point factory: build the provider from ``OPENMETADATA_*`` settings."""
    url, token, service = (
        env.get("OPENMETADATA_URL", ""),
        env.get("OPENMETADATA_TOKEN", ""),
        env.get("OPENMETADATA_SERVICE", ""),
    )
    if not (url and token and service):
        raise ValueError(
            "context provider 'openmetadata' needs OPENMETADATA_URL, OPENMETADATA_TOKEN "
            "and OPENMETADATA_SERVICE"
        )
    return OpenMetadataContext(
        url, token, service, timeout=float(env.get("OPENMETADATA_TIMEOUT_SECONDS", "5"))
    )
