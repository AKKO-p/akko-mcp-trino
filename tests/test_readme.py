"""The README's "Run it" section must describe commands that actually exist.

Documentation that drifts from the code is worse than none: a reader trusts
it. So the README is parsed and its claims are checked against the module —
the entrypoint it names, the variables it lists, the defaults it states.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

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
    # a provider reads its own settings from the environment it is given
    provider = (
        pathlib.Path(__file__).resolve().parents[1] / "akko_mcp_trino" / "context" / "file.py"
    ).read_text(encoding="utf-8")
    read_by_code |= set(re.findall(r'"(MCP_[A-Z_]+)"', provider))
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


def test_readme_defaults_of_the_file_provider_match_the_code(tmp_path):
    from akko_mcp_trino.context import CLASSIFICATION, GLOSSARY, OWNER, PROPERTY
    from akko_mcp_trino.context.file import from_env

    section = README[README.index("### The file provider") :]
    section = section[: section.index("\n### ", 4)]
    rows = {
        m.group(1): m.group(2)
        for m in re.finditer(r"^\| `(MCP_CONTEXT_FILE[A-Z_]*)` \|.*\| ([^|]+) \|$", section, re.M)
    }
    assert set(rows) == {
        "MCP_CONTEXT_FILE",
        "MCP_CONTEXT_FILE_GLOSSARIES",
        "MCP_CONTEXT_FILE_CLASSIFICATIONS",
        "MCP_CONTEXT_FILE_PROPERTIES",
        "MCP_CONTEXT_FILE_SERVE_OWNER",
    }
    path = tmp_path / "context.json"
    # What the file declares, so that an allow list has something to let through.
    table = {
        "catalog": "demo",
        "schema": "sales",
        "table": "orders",
        "owner": "sales-team",
        "terms": ["t1", "t2"],
        "tags": [{"name": "x", "set": "a"}, {"name": "y", "set": "*"}],
        "properties": {"p": {"value": "v", "set": "a"}, "q": {"value": "v", "set": "*"}},
    }
    terms = {
        "t1": {"name": "T1", "glossary": "a"},
        "t2": {"name": "T2", "glossary": "*"},
    }
    document = {"version": 1, "catalogs": ["demo"], "tables": [table], "terms": terms}
    path.write_text(json.dumps(document), encoding="utf-8")
    sets = from_env({"MCP_CONTEXT_FILE": str(path)}).served_sets()
    assert rows["MCP_CONTEXT_FILE"].strip() == "none"
    with pytest.raises(ValueError, match="MCP_CONTEXT_FILE is not set"):
        from_env({})
    for name, kind in (
        ("MCP_CONTEXT_FILE_GLOSSARIES", GLOSSARY),
        ("MCP_CONTEXT_FILE_CLASSIFICATIONS", CLASSIFICATION),
        ("MCP_CONTEXT_FILE_PROPERTIES", PROPERTY),
    ):
        assert rows[name].strip() == "none" and sets[kind] == frozenset(), name
        # the README says: exact names, `*` is a name, empty entries are ignored
        named = from_env({"MCP_CONTEXT_FILE": str(path), name: "a, ,*"}).served_sets()
        assert named[kind] == frozenset({"a", "*"}), name
    assert rows["MCP_CONTEXT_FILE_SERVE_OWNER"].strip() == "`false`"
    assert sets[OWNER] == frozenset()
    served = from_env({"MCP_CONTEXT_FILE": str(path), "MCP_CONTEXT_FILE_SERVE_OWNER": "true"})
    assert served.served_sets()[OWNER] != frozenset()


def _resource_groups(path: pathlib.Path) -> list[dict]:
    """The Trino resource group examples of a README: its JSON blocks with rootGroups."""
    blocks = re.findall(r"```json\n(.*?)```", path.read_text(encoding="utf-8"), re.S)
    return [json.loads(b) for b in blocks if '"rootGroups"' in b]


def _leaves(groups: list[dict], prefix: str = "") -> tuple[set[str], set[str]]:
    """The paths of every group, and of the leaf groups, as a selector names them."""
    every, leaves = set(), set()
    for group in groups:
        path = f"{prefix}{group['name']}"
        every.add(path)
        if group.get("subGroups"):
            more, deeper = _leaves(group["subGroups"], f"{path}.")
            every |= more
            leaves |= deeper
        else:
            leaves.add(path)
    return every, leaves


@pytest.mark.parametrize("readme", ["README.md", "README.fr.md"])
def test_the_resource_group_example_selects_only_leaf_groups(readme):
    """Trino refuses a query its selector puts in a group that has sub-groups: a selector
    must name a leaf, so the queries without the tag need a group of their own."""
    (example,) = _resource_groups(pathlib.Path(__file__).resolve().parents[1] / readme)
    every, leaves = _leaves(example["rootGroups"])
    selected = [s["group"] for s in example["selectors"]]
    assert selected and all(g in every for g in selected), "a selector names no group"
    assert [g for g in selected if g not in leaves] == [], "a selector names a non-leaf group"
    assert not example["selectors"][-1].get("clientTags"), "a last selector catches the rest"


@pytest.mark.parametrize(
    "path, word",
    [
        ("README.md", "Nine tools"),
        ("README.fr.md", "Neuf tools"),
        ("DOCKERHUB.md", "Nine tools"),
        ("akko_mcp_trino/__init__.py", "Nine tools"),
    ],
)
def test_the_public_texts_count_the_nine_tools(path, word):
    text = (pathlib.Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    assert word in text
    assert not re.search(r"\b(eight|huit) tools\b", text, re.I), "a count from 0.7"
