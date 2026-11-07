"""The examples must run against the SDK the package pins.

An example that fails at its first import teaches nothing, and nothing else in
the suite imports it. So every `from mcp... import name` in the examples and in
the documentation's Python snippets is checked against the installed SDK.
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
    for page in sorted((ROOT / "docs").glob("*/*.md")):
        text = page.read_text(encoding="utf-8")
        found += [
            (f"{page.relative_to(ROOT)} snippet {i}", block)
            for i, block in enumerate(re.findall(r"```python\n(.*?)```", text, re.S))
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
# VS Code (code.visualstudio.com/docs/copilot/reference/mcp-configuration) names
# the object servers, and every entry names its type.
_HOST_KEYS = {
    "claude-desktop": [{"command", "args", "env"}],
    "cursor": [{"type", "command", "args", "env", "envFile"}, {"url", "headers", "auth"}],
    "mcp": [{"command", "args", "env"}, {"url", "headers"}],
    "vscode": [{"type", "command", "args", "env", "envFile"}, {"type", "url", "headers"}],
}
# The keys of a VS Code input that prompts for a string.
_VSCODE_INPUT_KEYS = {"type", "id", "description", "default", "password"}
# A block of the pages that connect a host may be for either host.
_HOST_KEYS["guide"] = _HOST_KEYS["claude-desktop"] + _HOST_KEYS["cursor"]


def _entries(config: dict) -> dict:
    """The servers of a host file: mcpServers, or servers for VS Code."""
    return config["mcpServers"] if "mcpServers" in config else config["servers"]


def _configs() -> list[tuple[str, dict]]:
    found = [
        (p.name, json.loads(p.read_text(encoding="utf-8")))
        for p in sorted((ROOT / "examples").glob("*.json"))
    ]
    for language in ("en", "fr"):
        for page in ("hosts", "quickstart"):
            text = (ROOT / "docs" / language / f"{page}.md").read_text(encoding="utf-8")
            found += [
                (f"guide {language}/{page} json {i}", json.loads(block))
                for i, block in enumerate(re.findall(r"```json\n(.*?)```", text, re.S))
            ]
    return found


def test_every_host_config_uses_only_keys_its_host_documents():
    wrong = []
    for name, config in _configs():
        host = next((h for h in ("claude-desktop", "cursor", "guide") if name.startswith(h)), "mcp")
        if "servers" in config:
            host = "vscode"
        for server, cfg in _entries(config).items():
            if not any(set(cfg) <= keys for keys in _HOST_KEYS[host]):
                wrong.append(f"{name}: {server}: {sorted(cfg)}")
            if host == "cursor" and "command" in cfg and cfg.get("type") != "stdio":
                wrong.append(f"{name}: {server}: a Cursor stdio entry names type stdio")
            if host == "vscode" and cfg.get("type") != ("http" if "url" in cfg else "stdio"):
                wrong.append(f"{name}: {server}: a VS Code entry names its type")
    assert wrong == []


def test_every_vscode_input_is_declared_and_typed_hidden():
    """Each ${input:ID} of a VS Code file is declared in its inputs, as a hidden prompt: the
    token is typed once, never shown and never written to the file."""
    wrong = []
    seen = 0
    for name, config in _configs():
        if "servers" not in config:
            continue
        inputs = {i["id"]: i for i in config.get("inputs", [])}
        for i in inputs.values():
            if set(i) - _VSCODE_INPUT_KEYS or i["type"] != "promptString" or not i.get("password"):
                wrong.append(f"{name}: input {i['id']}: {i}")
        for ref in re.findall(r"\$\{input:([^}]+)\}", json.dumps(config["servers"])):
            seen += 1
            if ref not in inputs:
                wrong.append(f"{name}: ${{input:{ref}}} is not declared")
    assert wrong == [] and seen > 0


def test_host_configs_set_only_variables_the_server_reads_and_pin_their_versions():
    """Every launch of the server is pinned to this version, every bridge to one release:
    a host file copied today must start the same thing tomorrow."""
    from akko_mcp_trino import __version__

    source = (ROOT / "akko_mcp_trino" / "config.py").read_text(encoding="utf-8")
    read_by_code = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', source))
    # The package, or the image under any registry and namespace, with or without a tag.
    server = re.compile(r"^(?:[^\s/]+/)*akko-mcp-trino(?:[=:@].*)?$")
    pinned = re.compile(
        rf"^(?:akko-mcp-trino=={re.escape(__version__)}|(?:[^\s/]+/)+akko-mcp-trino:{re.escape(__version__)})$"
    )
    wrong = []
    for name, config in _configs():
        for cfg in _entries(config).values():
            args = cfg.get("args", [])
            wrong += [
                f"{name}: {a} is not pinned"
                for a in args
                if a.startswith("mcp-remote") and not re.fullmatch(r"mcp-remote@\d+\.\d+\.\d+", a)
            ]
            launched = [a for a in args if server.match(a)]
            wrong += [
                f"{name}: {a} is not pinned to {__version__}"
                for a in launched
                if not pinned.match(a)
            ]
            if not launched:
                continue
            names = set(cfg.get("env", {}))
            names |= {a.split("=", 1)[0] for a in args if re.match(r"^[A-Z_]+=", a)}
            wrong += [f"{name}: {v}" for v in sorted(names - read_by_code)]
    assert wrong == []
