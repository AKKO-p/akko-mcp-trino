"""The README's "Run it" section must describe commands that actually exist.

Documentation that drifts from the code is worse than none: a reader trusts
it. So the README is parsed and its claims are checked against the module —
the entrypoint it names, the variables it lists, the defaults it states.
"""

from __future__ import annotations

import pathlib
import re

from akko_mcp_trino.config import Config

README = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_readme_names_the_real_entrypoint():
    assert "python -m akko_mcp_trino" in README
    assert (pathlib.Path(__file__).resolve().parents[1] / "akko_mcp_trino" / "__main__.py").exists()


def test_every_variable_the_readme_lists_is_read_by_config():
    listed = set(re.findall(r"`(TRINO_[A-Z_]+|MCP_[A-Z_]+)`", README))
    source = (
        pathlib.Path(__file__).resolve().parents[1] / "akko_mcp_trino" / "config.py"
    ).read_text(encoding="utf-8")
    read_by_code = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', source))
    unknown = sorted(v for v in listed if v not in read_by_code)
    assert unknown == [], f"README lists variables the code never reads: {unknown}"


def test_readme_defaults_match_the_code(monkeypatch):
    for name in (
        "MCP_PORT",
        "MCP_HEALTH_PORT",
        "MCP_TRANSPORT",
        "TRINO_MAX_ROWS",
        "TRINO_READ_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = Config.from_env()
    assert cfg.mcp_port == 3000 and "`3000`" in README
    assert cfg.health_port == 3001 and "`3001`" in README
    assert cfg.transport == "streamable-http" and "`streamable-http`" in README
    assert cfg.max_rows == 100 and "`100`" in README
    assert cfg.read_only is True


def test_package_version_matches_pyproject_and_changelog():
    import tomllib

    from akko_mcp_trino import __version__

    root = pathlib.Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__
    assert f"## [{__version__}]" in (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"version-{__version__}-blue" in README


def test_readme_states_the_safe_defaults_the_code_has(monkeypatch):
    """The defaults are the security posture: the README must say the ones the code has."""
    for name in (
        "TRINO_IDENTITY_MODE",
        "MCP_AUTH_ENABLED",
        "MCP_AUTH_REQUIRED",
        "TRINO_QUERY_TIMEOUT_SECONDS",
        "MCP_CALL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = Config.from_env()
    rows = {
        m.group(1): m.group(2)
        for m in re.finditer(r"^\| `([A-Z_]+)`[^\n]*\| `([^`]+)` \|$", README, re.M)
    }
    assert cfg.trino_identity_mode == rows["TRINO_IDENTITY_MODE"] == "jwt"
    assert cfg.auth_enabled is True and rows["MCP_AUTH_ENABLED"] == "true"
    assert cfg.auth_required is True and rows["MCP_AUTH_REQUIRED"] == "true"
    assert cfg.query_timeout_seconds == float(rows["TRINO_QUERY_TIMEOUT_SECONDS"]) == 60
    assert cfg.call_timeout_seconds == float(rows["MCP_CALL_TIMEOUT_SECONDS"]) == 120


def test_migration_guide_and_changelog_name_every_breaking_default():
    root = pathlib.Path(__file__).resolve().parents[1]
    guide = (root / "MIGRATING.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    for name in (
        "TRINO_IDENTITY_MODE",
        "MCP_AUTH_ENABLED",
        "MCP_AUTH_REQUIRED",
        "TRINO_QUERY_TIMEOUT_SECONDS",
        "MCP_CALL_TIMEOUT_SECONDS",
        "TRINO_TOKEN_EXCHANGE_AUDIENCE",
    ):
        assert name in guide and name in changelog, name
