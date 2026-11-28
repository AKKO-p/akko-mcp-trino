"""Kubernetes health endpoints: /health (process liveness) and /ready (Trino reachable).

/health deliberately does NOT probe Trino: a liveness check that depends on a
downstream would restart every pod the moment Trino hiccups, turning a partial
outage into a total one. /ready checks Trino the way the identity mode can (a bounded
`SELECT 1` in the core's `service` mode). /metrics is exposed when a metrics registry is supplied.

The health port answers anyone who reaches it, without a token: a failed /ready says why
in a short code (``readiness_code``), never the exception, whose text names the address of
Trino and what its driver said. The exception goes to the server's log, once each time the
reason changes, and back to ready. The probe's queries stay out of the query metrics; the
probes have their own counter, by result (``mcp_trino_ready_checks_total``).
"""

from __future__ import annotations

import logging
import re
import ssl
from typing import Any, Iterator

import httpx
import trino.exceptions
import urllib3.exceptions
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .trino_client import TrinoClient, TrinoNotReady

log = logging.getLogger(__name__)

READY = "ready"
# Every code a failed /ready may answer, and the label of the probes' counter.
READINESS_CODES = (
    "trino_starting",
    "trino_unavailable",
    "trino_auth_failed",
    "trino_permission_denied",
    "trino_query_failed",
    "trino_http_error",
    "trino_tls_failed",
    "trino_timeout",
    "trino_unreachable",
    "identity_refused",
    "trino_error",
)
_HTTP_STATUS = re.compile(r"error (\d{3})")
_DENIED = frozenset({"PERMISSION_DENIED", "ACCESS_DENIED"})


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and the exceptions it was raised from or during, at most ten deep."""
    seen: BaseException | None = exc
    for _ in range(10):
        if seen is None:
            return
        yield seen
        seen = seen.__cause__ or seen.__context__


def readiness_code(exc: BaseException) -> str:
    """Why Trino is not ready, in one of ``READINESS_CODES``: what Trino answered when it
    answered, else what kept the probe from reaching it (TLS, a timeout, the network)."""
    if isinstance(exc, TrinoNotReady) and exc.code in READINESS_CODES:
        return exc.code
    if isinstance(exc, PermissionError):
        return "identity_refused"
    if isinstance(exc, trino.exceptions.TrinoAuthError):
        return "trino_auth_failed"
    if isinstance(exc, trino.exceptions.HttpError):
        found = _HTTP_STATUS.match(str(exc))
        status = int(found.group(1)) if found else 0
        if status == 401:
            return "trino_auth_failed"
        if status == 403:
            return "trino_permission_denied"
        if status in (502, 503, 504):
            return "trino_unavailable"
        return "trino_http_error"
    if isinstance(exc, trino.exceptions.TrinoQueryError):
        return "trino_permission_denied" if exc.error_name in _DENIED else "trino_query_failed"
    chain = list(_chain(exc))
    if any(isinstance(e, (ssl.SSLError, urllib3.exceptions.SSLError)) for e in chain):
        return "trino_tls_failed"
    # urllib3 derives a failed connection (a name that does not resolve, a refusal) from its
    # connect timeout: it is no timeout.
    if any(isinstance(e, urllib3.exceptions.NewConnectionError) for e in chain):
        return "trino_unreachable"
    if any(
        isinstance(e, (TimeoutError, urllib3.exceptions.TimeoutError, httpx.TimeoutException))
        for e in chain
    ):
        return "trino_timeout"
    if any(
        isinstance(
            e,
            (
                trino.exceptions.TrinoConnectionError,
                urllib3.exceptions.HTTPError,
                httpx.TransportError,
                OSError,
            ),
        )
        for e in chain
    ):
        return "trino_unreachable"
    return "trino_error"


def build_health_app(client: TrinoClient, metrics: Any = None) -> Starlette:
    """The health app: ``/health`` never touches Trino, ``/ready`` checks Trino
    (``TrinoClient.ping``) and answers a code when it is not ready, ``/metrics`` when
    metrics are given.
    """
    last = [READY]

    def observe(result: str, exc: BaseException | None = None) -> None:
        if metrics is not None:
            metrics.ready_checks.labels(result=result).inc()
        if result == last[0]:
            return
        if exc is not None:
            log.warning("not ready: %s (%s: %s)", result, type(exc).__name__, exc)
        else:
            log.info("ready again after %s", last[0])
        last[0] = result

    async def health(_request):
        return JSONResponse({"status": "ok"})

    async def ready(_request):
        try:
            client.ping()
        except Exception as exc:  # noqa: BLE001 - any failure is a reason not to be ready
            code = readiness_code(exc)
            observe(code, exc)
            return JSONResponse({"status": "error", "reason": code}, status_code=503)
        observe(READY)
        return JSONResponse({"status": READY})

    routes = [Route("/health", health), Route("/ready", ready)]
    if metrics is not None:

        async def metrics_route(_request):
            body, content_type = metrics.render()
            return Response(body, media_type=content_type)

        routes.append(Route("/metrics", metrics_route))

    return Starlette(routes=routes)
