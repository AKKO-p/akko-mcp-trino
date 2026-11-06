"""fingerprint-audit: the audit line adds what the call did, joined to Trino by SHA-256.

The core writes one line per tool call: who, which tool, the outcome, how long. This module
adds what the call did, from the record its tools fill with ``note(...)``: the token id
(``jti``, never the token), the SHA-256 of the statement sent to Trino for the caller, the
tables it names, the rows returned and whether they were cut, and the tables whose catalogue
description reached the caller. The SQL text itself still has no field to live in.

A tool that sends several statements for the caller keeps the core's rule: the digest is
that of the last statement it noted, the one that answered the agent (the aggregate of
``profile_table``, the sample of ``describe_table``). Each list keeps ``MCP_AUDIT_MAX_NAMES``
names; past them, ``tables_cut`` and ``metadata_served_cut`` say how many more there were.

Trino keeps the query text in its history, and a policy engine that records it hashes to the
same digest: that is the join, with the request id and the tool on each query when
``trino-tags`` is loaded too.

An extension's field (``AuditEvent.extra``) never overwrites a field of the fingerprint: one
of the same name is dropped and counted in ``extra_dropped``. The server wraps the audit in
the order of ``MCP_EXTENSIONS``, each around the one before: name ``fingerprint-audit`` before
any other extension that wraps the audit, so that one sees the core's event and hands it on,
and the fingerprint is added last, whatever that one rebuilds.
"""

from dataclasses import dataclass, field, fields
from typing import Any, Mapping, Optional

from ..audit import AuditEvent
from ..config import Config
from ..extensions import Extension

# The fields of the line an extension's field may never take.
FINGERPRINT_FIELDS = frozenset(
    {
        "request_id",
        "tool",
        "subject",
        "token_id",
        "ok",
        "error",
        "sql_sha256",
        "tables",
        "tables_cut",
        "duration_ms",
        "rows",
        "truncated",
        "metadata_served",
        "metadata_served_cut",
        "extra_dropped",
    }
)


@dataclass(frozen=True)
class FingerprintEvent(AuditEvent):
    """One tool call with its fingerprint: which token id, which statement (its SHA-256),
    which tables, how many rows, which catalogue descriptions served."""

    token_id: str = ""
    sql_sha256: str = ""
    tables: list[str] = field(default_factory=list)
    tables_cut: int = 0
    rows: Optional[int] = None
    truncated: bool = False
    metadata_served: list[str] = field(default_factory=list)
    metadata_served_cut: int = 0

    def as_dict(self) -> dict:
        """The line in the order of the fields it joins; a ``_cut`` count only when a list
        was cut."""
        line: dict[str, Any] = {
            "request_id": self.request_id,
            "tool": self.tool,
            "subject": self.subject,
            "token_id": self.token_id,
            "ok": self.ok,
            "error": self.error,
            "sql_sha256": self.sql_sha256,
            "tables": list(self.tables),
        }
        if self.tables_cut:
            line["tables_cut"] = self.tables_cut
        line.update(
            duration_ms=self.duration_ms,
            rows=self.rows,
            truncated=self.truncated,
            metadata_served=list(self.metadata_served),
        )
        if self.metadata_served_cut:
            line["metadata_served_cut"] = self.metadata_served_cut
        # An extension's field never overwrites a field of the fingerprint: dropped, counted.
        kept = {k: v for k, v in self.extra.items() if k not in FINGERPRINT_FIELDS}
        if len(kept) < len(self.extra):
            kept["extra_dropped"] = len(self.extra) - len(kept)
        return {**line, **kept}


class FingerprintAudit:
    """An audit sink that adds the fingerprint of the call to each event, then hands it to
    ``inner``."""

    def __init__(self, inner: Any) -> None:
        """Write through ``inner``, the sink the server would write to otherwise."""
        self.inner = inner

    def record(self, event: AuditEvent) -> None:
        """Add the fingerprint fields, then record through the wrapped sink."""
        call, principal = event.call, event.principal
        base = {f.name: getattr(event, f.name) for f in fields(AuditEvent)}
        self.inner.record(
            FingerprintEvent(
                **base,
                token_id=principal.token_id if principal is not None else "",
                sql_sha256=call.sql_sha256 if call is not None else "",
                tables=list(call.tables) if call is not None else [],
                tables_cut=call.tables_cut if call is not None else 0,
                rows=call.rows if call is not None else None,
                truncated=call.truncated if call is not None else False,
                metadata_served=list(call.metadata_served) if call is not None else [],
                metadata_served_cut=call.metadata_served_cut if call is not None else 0,
            )
        )


class FingerprintAuditExtension(Extension):
    """The audit line with the fingerprint of each call."""

    name = "fingerprint-audit"

    def __init__(self, config: Config) -> None:
        """Remember the bound of the line's lists, for ``--check``."""
        self._max_names = config.audit_max_names

    def check_lines(self) -> list[str]:
        """What the line adds, and the bound of its lists."""
        return [
            "fingerprint-audit: the audit line adds token_id, sql_sha256, tables, rows, "
            f"truncated and metadata_served, up to {self._max_names} names a list"
        ]

    def wrap_audit(self, audit: Any) -> Any:
        """The server's sink, with the fingerprint added to each line."""
        return FingerprintAudit(audit)


def create(config: Config, env: Mapping[str, str]) -> FingerprintAuditExtension:
    """The module; it reads no setting of its own."""
    return FingerprintAuditExtension(config)
