"""Modules of the core: extensions it ships, each loaded only when ``MCP_EXTENSIONS`` names it.

* ``token-policy`` (`token_policy`): a verified token must also name an allowed client and
  carry the required scopes;
* ``fingerprint-audit`` (`fingerprint_audit`): the audit line adds what the call did, joined
  to Trino by the SHA-256 of its statement;
* ``trino-tags`` (`trino_tags`): every query of a call carries its request id and its tool as
  client tags.

Each is an `akko_mcp_trino.extensions.Extension`, registered under the entry-point group
``akko_mcp_trino.extensions`` by this package. None is loaded by default.
"""
