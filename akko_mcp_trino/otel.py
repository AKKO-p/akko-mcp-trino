"""OpenTelemetry: what the MCP SDK may emit, and the one setting that must not stop the server.

The server sends nothing of its own anywhere. The MCP SDK it is built on wraps every message
it receives in an OpenTelemetry span (``mcp.server._otel.OpenTelemetryMiddleware``, on by
default) through the ``opentelemetry-api`` package alone: without an OpenTelemetry SDK in the
environment, the API's default provider does nothing and no span leaves the process. An
environment that installs an SDK and configures it (an auto-instrumentation agent, the
``OTEL_*`` variables) turns those spans on, and they go wherever that SDK exports them; see
the operations guide for what they carry.

``OTEL_PYTHON_TRACER_PROVIDER`` names the tracer provider the API loads, by the name of its
entry point. When no installed package provides it (a platform that sets the OTel variables
on every pod, where this image has no SDK), the API raises at the SDK's first import and the
server would not start. ``drop_missing_tracer_provider`` removes such a variable from the
process before the SDK is imported, and returns the warning the server prints at start: the
server starts, and emits no span, which is what it would do without the variable.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import MutableMapping

TRACER_PROVIDER_VARIABLE = "OTEL_PYTHON_TRACER_PROVIDER"
_GROUP = "opentelemetry_tracer_provider"
# The longest value of the variable the warning repeats.
_SHOWN_CHARS = 100


def drop_missing_tracer_provider(environ: MutableMapping[str, str]) -> str:
    """Remove ``OTEL_PYTHON_TRACER_PROVIDER`` from ``environ`` when no installed package
    provides the tracer provider it names, and return the warning that says so; empty when
    the variable is unset or names an installed provider, which is then left alone."""
    name = environ.get(TRACER_PROVIDER_VARIABLE)
    if name is None:
        return ""
    if any(ep.name == name for ep in entry_points(group=_GROUP)):
        return ""
    del environ[TRACER_PROVIDER_VARIABLE]
    return (
        f"{TRACER_PROVIDER_VARIABLE}={name[:_SHOWN_CHARS]!r} names no installed tracer provider "
        "(no OpenTelemetry SDK provides it here): ignored, and the server emits no span"
    )
