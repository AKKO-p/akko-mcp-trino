"""The Host header filter, decided by configuration and never by the SDK.

The SDK ships DNS rebinding protection for servers a browser could reach on
localhost, and enables it on its own whenever the app is built without an
explicit setting. That default is right for a desktop tool and wrong for a
network service: the pod is reached as ``akko-mcp-trino.akko.svc`` in a
cluster and as a public name on the internet, and the SDK would answer 421
Misdirected Request to both. The identity guard, not the Host header, is
what protects this server; the filter is opt-in through ``MCP_ALLOWED_HOSTS``.
"""

from __future__ import annotations

from mcp.server.transport_security import TransportSecuritySettings


def parse_allowed_hosts(value: str) -> list[str]:
    """``MCP_ALLOWED_HOSTS`` as a list: comma-separated, blanks ignored."""
    return [h.strip() for h in value.split(",") if h.strip()]


def transport_security_for(allowed_hosts: str) -> TransportSecuritySettings:
    """The settings the transport app is built with: filter off when no host
    is listed, on for exactly the listed hosts otherwise."""
    hosts = parse_allowed_hosts(allowed_hosts)
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts)
