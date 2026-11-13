"""Prometheus metrics, injectable with an isolated registry so they are testable.

Counts Trino queries, errors and latency, and the reads of the context sources;
exposed on /metrics by the health app. A context label names a source, a status or
a cache result, never an object.
A dedicated `CollectorRegistry` avoids global state shared across instances and tests.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class Metrics:
    """Query and context counters, latency histograms and gauges on an isolated registry."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        """Create the collectors on ``registry``, or on a fresh one."""
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
        self.context_calls = Counter(
            "mcp_trino_context_calls_total",
            "Calls to a context source",
            ["provider"],
            registry=self.registry,
        )
        self.context_errors = Counter(
            "mcp_trino_context_errors_total",
            "Context reads that did not end well, by status",
            ["provider", "status"],
            registry=self.registry,
        )
        self.context_duration = Histogram(
            "mcp_trino_context_call_duration_seconds",
            "Context source call latency",
            ["provider"],
            registry=self.registry,
        )
        self.context_cache = Counter(
            "mcp_trino_context_cache_lookups_total",
            "Context cache lookups, by result (hit or miss)",
            ["provider", "result"],
            registry=self.registry,
        )
        self.context_cache_bytes = Gauge(
            "mcp_trino_context_cache_bytes",
            "Length of the JSON form of the entries a context source caches "
            "(memory is about 3 to 4 times that)",
            ["provider"],
            registry=self.registry,
        )
        self.context_breaker_open = Gauge(
            "mcp_trino_context_breaker_open",
            "1 while the breaker of a context source is open",
            ["provider"],
            registry=self.registry,
        )

        self.sql_refusals = Counter(
            "mcp_trino_sql_refusals_total",
            "Agent statements refused before Trino by a bound or a parse worker, by reason "
            "(too_long, too_nested, caller_busy, no_worker, deadline: a parse cut by the "
            "deadline, worker_failed)",
            ["reason"],
            registry=self.registry,
        )

    def render(self) -> tuple[bytes, str]:
        """Return (Prometheus exposition body, content-type)."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
