"""Endpoints de santé K8s — /health (liveness process) + /ready (dépendance Trino).

P1 : comportement identique. /health ne sonde PAS Trino (anti-restart sur hoquet) ;
/ready fait un `SELECT 1` borné. (P2 ajoutera /metrics.)
"""
from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .trino_client import TrinoClient


def build_health_app(client: TrinoClient, metrics: Any = None) -> Starlette:
    async def health(_request):
        return JSONResponse({"status": "ok"})

    async def ready(_request):
        try:
            client.query("SELECT 1")
            return JSONResponse({"status": "ready"})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)

    routes = [Route("/health", health), Route("/ready", ready)]
    if metrics is not None:
        async def metrics_route(_request):
            body, content_type = metrics.render()
            return Response(body, media_type=content_type)
        routes.append(Route("/metrics", metrics_route))

    return Starlette(routes=routes)
