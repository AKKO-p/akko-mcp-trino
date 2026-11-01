"""The examples must run against the SDK the package pins.

An example that fails at its first import teaches nothing, and nothing else in
the suite imports it. So every `from mcp... import name` in the examples and in
the README's Python snippets is checked against the installed SDK.
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _sources() -> list[tuple[str, str]]:
    found = [
        (str(p.relative_to(ROOT)), p.read_text(encoding="utf-8"))
        for p in sorted((ROOT / "examples").glob("*.py"))
    ]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    found += [
        (f"README.md snippet {i}", block)
        for i, block in enumerate(re.findall(r"```python\n(.*?)```", readme, re.S))
    ]
    return found


def test_examples_import_only_what_the_sdk_has():
    missing = []
    for where, source in _sources():
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] == "mcp"
            ):
                module = importlib.import_module(node.module)
                missing += [
                    f"{where}: {node.module}.{a.name}"
                    for a in node.names
                    if not hasattr(module, a.name)
                ]
    assert missing == []


def test_the_example_agent_follows_the_path_the_tools_describe():
    """search_columns first when the data's place is unknown, then describe_schema, then
    execute_query: the example teaches the same path as the tool descriptions."""
    source = (ROOT / "examples" / "agent.py").read_text(encoding="utf-8")
    path = [source.find(tool) for tool in ("search_columns", "describe_schema", "execute_query")]
    assert -1 not in path and path == sorted(path), path


# The keys each host documents for an entry under mcpServers: Claude Desktop
# (modelcontextprotocol.io, "Connect to local MCP servers") starts local servers
# only; Cursor (cursor.com/docs/context/mcp) takes stdio or a url with headers.
_HOST_KEYS = {
    "claude-desktop": [{"command", "args", "env"}],
    "cursor": [{"type", "command", "args", "env", "envFile"}, {"url", "headers", "auth"}],
    "mcp": [{"command", "args", "env"}, {"url", "headers"}],
}
# A block of the guide may be for either host.
_HOST_KEYS["guide"] = _HOST_KEYS["claude-desktop"] + _HOST_KEYS["cursor"]


def _configs() -> list[tuple[str, dict]]:
    found = [
        (p.name, json.loads(p.read_text(encoding="utf-8")))
        for p in sorted((ROOT / "examples").glob("*.json"))
    ]
    guide = (ROOT / "docs" / "guides" / "connect-claude-desktop-cursor.md").read_text(
        encoding="utf-8"
    )
    found += [
        (f"guide json {i}", json.loads(block))
        for i, block in enumerate(re.findall(r"```json\n(.*?)```", guide, re.S))
    ]
    return found


def test_every_host_config_uses_only_keys_its_host_documents():
    wrong = []
    for name, config in _configs():
        host = next((h for h in ("claude-desktop", "cursor", "guide") if name.startswith(h)), "mcp")
        for server, cfg in config["mcpServers"].items():
            if not any(set(cfg) <= keys for keys in _HOST_KEYS[host]):
                wrong.append(f"{name}: {server}: {sorted(cfg)}")
            if host == "cursor" and "command" in cfg and cfg.get("type") != "stdio":
                wrong.append(f"{name}: {server}: a Cursor stdio entry names type stdio")
    assert wrong == []


def test_host_configs_set_only_variables_the_server_reads_and_pin_their_versions():
    """Every launch of the server is pinned to this version, every bridge to one release:
    a host file copied today must start the same thing tomorrow."""
    from akko_mcp_trino import __version__

    source = (ROOT / "akko_mcp_trino" / "config.py").read_text(encoding="utf-8")
    read_by_code = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', source))
    pinned = ("akko-mcp-trino==" + __version__, "akkoai/akko-mcp-trino:" + __version__)
    wrong = []
    for name, config in _configs():
        for cfg in config["mcpServers"].values():
            args = cfg.get("args", [])
            wrong += [
                f"{name}: {a} is not pinned"
                for a in args
                if a.startswith("mcp-remote") and not re.fullmatch(r"mcp-remote@\d+\.\d+\.\d+", a)
            ]
            launched = [
                a for a in args if a.startswith(("akko-mcp-trino", "akkoai/akko-mcp-trino"))
            ]
            wrong += [
                f"{name}: {a} is not pinned to {__version__}" for a in launched if a not in pinned
            ]
            if not launched:
                continue
            names = set(cfg.get("env", {}))
            names |= {a.split("=", 1)[0] for a in args if re.match(r"^[A-Z_]+=", a)}
            wrong += [f"{name}: {v}" for v in sorted(names - read_by_code)]
    assert wrong == []
