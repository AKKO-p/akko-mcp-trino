"""Métriques Prometheus — vendor-neutre, injectable (registry isolé → testable).

Compte les requêtes Trino, les erreurs et la latence. Exposées via /metrics (cf health).
Un `CollectorRegistry` dédié évite l'état global partagé entre instances/tests.
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
            "mcp_trino_queries_total", "Requêtes Trino exécutées", registry=self.registry
        )
        self.errors = Counter(
            "mcp_trino_query_errors_total", "Requêtes Trino en erreur", registry=self.registry
        )
        self.duration = Histogram(
            "mcp_trino_query_duration_seconds", "Latence des requêtes Trino", registry=self.registry
        )

    def render(self) -> tuple[bytes, str]:
        """Renvoie (corps exposition Prometheus, content-type)."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
