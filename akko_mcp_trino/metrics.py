"""Prometheus metrics, injectable with an isolated registry so they are testable.

Counts Trino queries, errors and latency; exposed on /metrics by the health app.
A dedicated `CollectorRegistry` avoids global state shared across instances and tests.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.queries = Counter(
            "mcp_trino_queries_total", "Trino queries executed", registry=self.registry
        )
        self.errors = Counter(
            "mcp_trino_query_errors_total", "Trino queries that failed", registry=self.registry
        )
        self.duration = Histogram(
            "mcp_trino_query_duration_seconds", "Trino query latency", registry=self.registry
        )

    def render(self) -> tuple[bytes, str]:
        """Return (Prometheus exposition body, content-type)."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
