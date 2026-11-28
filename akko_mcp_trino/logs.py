"""The server's log lines: Python's text format, or one JSON object a line (``MCP_LOG_FORMAT``).

``text``, the default in 0.8, keeps the lines of earlier versions: ``LEVEL:logger:message``,
the audit's JSON as the message of ``mcp.audit``. ``json`` writes every line, the audit's
and the technical ones, as one JSON object with nothing before it: ``ts`` (UTC, ISO 8601
with milliseconds), ``level``, ``logger``, then ``msg`` for a technical line, or the fields of
the audit line at the top level (its own ``ts`` kept), and ``exc`` when an exception goes
with a technical line. Every line goes to stderr: over stdio, stdout is the protocol channel.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from .audit import utc_iso

TEXT_FORMAT = "%(levelname)s:%(name)s:%(message)s"


class JsonFormatter(logging.Formatter):
    """One JSON object a record, on one line, with nothing before it."""

    def format(self, record: logging.LogRecord) -> str:
        """The record as a JSON line: ``ts``, ``level``, ``logger``, then the audit's fields
        when the record carries them (``LoggingAudit``), else ``msg``."""
        line: dict[str, Any] = {
            "ts": utc_iso(record.created),
            "level": record.levelname,
            "logger": record.name,
        }
        audit = getattr(record, "audit", None)
        if isinstance(audit, dict):
            line.update(audit)
        else:
            line["msg"] = record.getMessage()
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, separators=(",", ":"), default=str)


def configure_logging(log_format: str, level: int = logging.INFO) -> None:
    """Send every log line to stderr in ``log_format`` (``text`` or ``json``), at ``level``;
    replaces the handlers the root logger had."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if log_format == "json" else logging.Formatter(TEXT_FORMAT)
    )
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
