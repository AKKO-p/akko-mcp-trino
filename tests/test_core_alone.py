"""The core alone serves the basics, and nothing more slips into it unnoticed.

The core is what anyone may read and run: nine SQL tools, the read-only guard
on the SQL tree, four Trino identities (the caller, by a token for Trino
obtained by token exchange; the caller, impersonated by the server's own
account; the local user by native OAuth2; or, when named, the one identity the
administrator configures),
bounds on rows and time, one audit line per call. Every function beyond that
lives in an extension and is loaded only when ``MCP_EXTENSIONS`` names it.
These tests list what the core holds; they fail the day something is added
without being listed here.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import fields

import pytest

from akko_mcp_trino.audit import AuditEvent
from akko_mcp_trino.config import Config
from akko_mcp_trino.server import build_server
from akko_mcp_trino.trino_client import resolve_identity
from tests.tool_doubles import FakeMCP, core_config

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "akko_mcp_trino"

BASIC_TOOLS = {
    "list_catalogs",
    "list_schemas",
    "list_tables",
    "describe_schema",
    "describe_table",
    "explain_query",
    "execute_query",
    "search_columns",
    "profile_table",
}

CORE_MODULES = {
    "__init__",
    "__main__",
    "app",
    "audit",
    "auth",
    "config",
    "context/__init__",
    "context/caches",
    "context/chain",
    "context/errors",
    "context/file",
    "context/gate",
    "context/loading",
    "context/loop",
    "context/merge",
    "context/provider",
    "context/reply",
    "context/secrets",
    "context/status",
    "context/text",
    "context/types",
    "context/visibility",
    "errors",
    "extensions",
    "health",
    "identity",
    "metrics",
    "middleware",
    "modules/__init__",
    "modules/fingerprint_audit",
    "modules/token_policy",
    "modules/trino_tags",
    "plugins",
    "search",
    "server",
    "sql_guard",
    "testing/__init__",
    "testing/context_contract",
    "testing/fake_trino",
    "token_exchange",
    "tools",
    "transport_security",
    "trino_client",
}

# The sub-packages of the core, named as ``akko_mcp_trino.<package>``.
CORE_PACKAGES = {m.split("/")[0] for m in CORE_MODULES if "/" in m}

# The entry-point groups the core reads: its tool plugins and its extensions.
CORE_GROUPS = {"akko_mcp_trino.tools", "akko_mcp_trino.extensions", "akko_mcp_trino.context"}


_config = core_config


def _modules() -> list[pathlib.Path]:
    """Every module of the core, sub-packages included, without bytecode caches."""
    return sorted(p for p in CORE.rglob("*.py") if "__pycache__" not in p.parts)


def _source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _modules())


def test_the_core_serves_nine_tools():
    mcp, _ = build_server(_config(), mcp_factory=lambda name, **_: FakeMCP())
    assert set(mcp.tools) == BASIC_TOOLS


def test_the_core_ships_its_listed_modules_and_no_other():
    assert {p.relative_to(CORE).with_suffix("").as_posix() for p in _modules()} == CORE_MODULES


def test_every_setting_the_core_reads_is_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    read = set(re.findall(r'environ\.get\("([A-Z_]+)"', _source()))
    assert read, "the core reads its settings from the environment"
    assert {name for name in read if f"`{name}`" not in readme} == set()


def test_the_core_identity_modes_are_exchange_impersonation_oauth2_or_one_named():
    """Without an extension, a query runs as the caller, with a token for Trino obtained by
    token exchange (``jwt``, the default) or impersonated by the server's account
    (``impersonation``), by browser login (``oauth2``, stdio only), or, when named,
    as ``TRINO_USER`` (``service``); a mode the core
    does not know refuses to start rather than fall back to any of them."""
    caller = dict(auth_enabled=True, auth_required=True)
    jwt = _config(
        trino_identity_mode="jwt",
        token_exchange_url="https://idp/token",
        token_exchange_client_id="mcp",
        token_exchange_client_secret="s",
        token_exchange_audience="trino",
        **caller,
    )
    imp = _config(trino_identity_mode="impersonation", trino_password="s", **caller)
    oauth2 = _config(trino_identity_mode="oauth2", transport="stdio")
    assert type(resolve_identity(_config())).__name__ == "ServiceIdentity"
    assert type(resolve_identity(jwt)).__name__ == "JwtIdentity"
    assert type(resolve_identity(imp)).__name__ == "ImpersonationIdentity"
    assert type(resolve_identity(oauth2)).__name__ == "OAuth2Identity"
    assert Config.__dataclass_fields__["trino_identity_mode"].default == "jwt"
    for mode in ("per-user", "token", "magic"):
        with pytest.raises(
            ValueError, match="expected one of \\('impersonation', 'jwt', 'oauth2', 'service'\\)"
        ):
            resolve_identity(_config(trino_identity_mode=mode))
    assert "trino_identity_mode" in {f.name for f in fields(Config)}


def test_the_core_sends_trino_its_accounts_credential_or_an_exchanged_or_oauth2_token():
    assert set(re.findall(r"trino\.auth\.(\w+)", _source())) == {
        "BasicAuthentication",
        "CertificateAuthentication",
        "CompositeRedirectHandler",
        "JWTAuthentication",
        "OAuth2Authentication",
    }


def test_the_core_never_sends_trino_the_callers_own_token():
    """Token passthrough is forbidden by the MCP authorization specification: the token
    Trino receives in ``jwt`` mode is the one ``trino_token`` obtains by exchange."""
    source = (CORE / "trino_client.py").read_text(encoding="utf-8")
    assert "JWTAuthentication(self.trino_token(bearer))" in source
    assert "JWTAuthentication(bearer)" not in source
    assert "return bearer" not in source


def test_the_core_audit_line_has_its_six_fields_and_trino_gets_only_its_two_tags():
    line = AuditEvent(request_id="r", tool="t", subject="s", ok=True).as_dict()
    assert set(line) == {"request_id", "tool", "subject", "ok", "error", "duration_ms"}
    # Only the trino-tags module, loaded when MCP_EXTENSIONS names it, makes the tags of a call.
    makers = {
        p.relative_to(CORE).as_posix()
        for p in _modules()
        if 'f"request_id=' in p.read_text(encoding="utf-8")
        or 'f"tool=' in p.read_text(encoding="utf-8")
    }
    assert makers == {"modules/trino_tags.py"}
    # The client tags of the core: one marks the statements of the filter by Trino's rights,
    # the other, by default, the aggregate query of profile_table (MCP_PROFILE_CLIENT_TAG).
    assert set(re.findall(r'^[A-Z_]*TAG = "([^"]*)"', _source(), re.M)) == {
        "mcp-gate",
        "mcp-profile",
    }
    assert "query_tags(GATE_TAG)" in (CORE / "trino_client.py").read_text(encoding="utf-8")


def test_without_an_extension_no_public_document_is_served_and_no_challenge_is_sent():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from akko_mcp_trino.app import build_asgi_app

    class _MCP:
        def streamable_http_app(self, *, transport_security=None):
            return Starlette()

    app = build_asgi_app(
        _config(auth_required=True),
        _MCP(),
        auth_provider=type("A", (), {"verify": lambda s, h: None})(),
    )
    client = TestClient(app)
    for path in ("/.well-known/any-document", "/.well-known/any-document/mcp"):
        assert client.get(path).status_code == 401
    refused = client.post("/mcp")
    assert refused.status_code == 401 and "www-authenticate" not in refused.headers


def test_the_core_reads_only_its_own_entry_point_groups():
    source = _source()
    named = set(re.findall(r"akko_mcp_trino\.[a-z_]+", source))
    groups = {
        name
        for name in named
        if name.removeprefix("akko_mcp_trino.") not in CORE_MODULES | CORE_PACKAGES
    }
    assert groups <= CORE_GROUPS


@pytest.mark.parametrize("package", ["context", "testing"])
def test_the_package_an_adapter_imports_loads_neither_the_trino_client_nor_the_identity(package):
    """A provider and its contract test import these packages; they must not pull the core's
    Trino side."""
    import subprocess
    import sys

    modules = sorted(
        [f"akko_mcp_trino.{package}"]
        + [
            f"akko_mcp_trino.{package}.{p.stem}"
            for p in (CORE / package).glob("*.py")
            if p.stem != "__init__"
        ]
    )
    code = (
        "import importlib, sys\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "loaded = [m for m in ('akko_mcp_trino.trino_client', 'akko_mcp_trino.identity')"
        " if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert done.returncode == 0, done.stderr


def test_the_wheel_finds_every_sub_package_of_the_core():
    """Packages are found, never listed: a sub-package added later cannot miss the wheel."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]
    assert project["setuptools"]["packages"] == {
        "find": {"include": ["akko_mcp_trino", "akko_mcp_trino.*"]}
    }
    assert CORE_PACKAGES == {"context", "modules", "testing"}


def test_an_adapter_finds_what_it_needs_in_the_context_package():
    import akko_mcp_trino.context as context
    from akko_mcp_trino.context import secrets, status

    for name in (
        "OK",
        "NOT_FOUND",
        "NOT_MAPPED",
        "PARTIAL",
        "UNAVAILABLE",
        "REFUSED",
        "ERROR",
        "NOT_CHECKED",
    ):
        assert getattr(context, name) == getattr(status, name) and name in context.__all__
    assert context.secret_setting is secrets.secret_setting
    assert context.FileSecret is secrets.FileSecret


def test_adapters_install_the_contract_by_an_extra_with_ranges():
    """An adapter's own pins must not clash with ours: the extra it installs gives ranges."""
    import tomllib

    import akko_mcp_trino.testing as testing

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["optional-dependencies"]["contract"] == ["pytest>=8,<10"]
    for text in (
        testing.__doc__ or "",
        (ROOT / "README.md").read_text(encoding="utf-8"),
        (ROOT / "README.fr.md").read_text(encoding="utf-8"),
    ):
        assert 'pip install "akko-mcp-trino[contract]"' in text


def test_the_sdist_carries_every_test_package():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include tests *.py" in manifest
