"""The examples must run against the SDK the package pins.

An example that fails at its first import teaches nothing, and nothing else in
the suite imports it. So every `from mcp... import name` in the examples and in
the README's Python snippets is checked against the installed SDK.
"""

from __future__ import annotations

import ast
import importlib
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
