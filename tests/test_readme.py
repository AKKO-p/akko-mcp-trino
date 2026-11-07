"""The README and the documentation must describe commands and settings that actually exist.

Documentation that drifts from the code is worse than none: a reader trusts
it. So the README and the pages of docs/en and docs/fr are parsed and their claims are
checked against the module: the entrypoint they name, the variables they list, the
defaults they state.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from akko_mcp_trino.config import Config

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
LANGUAGES = ["en", "fr"]


def doc(language: str, page: str) -> str:
    """One page of the documentation, in one language."""
    return (ROOT / "docs" / language / f"{page}.md").read_text(encoding="utf-8")


def docs(language: str) -> str:
    """Every page of the documentation in one language, one after the other."""
    pages = sorted((ROOT / "docs" / language).glob("*.md"))
    return "\n".join(p.read_text(encoding="utf-8") for p in pages)


@pytest.mark.parametrize("language", LANGUAGES)
def test_readme_names_the_real_entrypoint(language):
    assert "python -m akko_mcp_trino" in doc(language, "deployment")
    assert (pathlib.Path(__file__).resolve().parents[1] / "akko_mcp_trino" / "__main__.py").exists()


def test_every_variable_the_readme_lists_is_read_by_config():
    text = README + docs("en") + docs("fr")
    listed = set(re.findall(r"`(TRINO_[A-Z_]+|MCP_[A-Z_]+)`", text))
    source = (
        pathlib.Path(__file__).resolve().parents[1] / "akko_mcp_trino" / "config.py"
    ).read_text(encoding="utf-8")
    read_by_code = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', source))
    # a provider reads its own settings from the environment it is given
    provider = (
        pathlib.Path(__file__).resolve().parents[1] / "akko_mcp_trino" / "context" / "file.py"
    ).read_text(encoding="utf-8")
    read_by_code |= set(re.findall(r'"(MCP_[A-Z_]+)"', provider))
    # a module of the core reads its own settings from the environment it is given
    for module in (pathlib.Path(__file__).resolve().parents[1] / "akko_mcp_trino" / "modules").glob(
        "*.py"
    ):
        read_by_code |= set(re.findall(r'"(MCP_[A-Z_]+)"', module.read_text(encoding="utf-8")))
    unknown = sorted(v for v in listed if v not in read_by_code)
    assert unknown == [], f"README lists variables the code never reads: {unknown}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_readme_defaults_match_the_code(monkeypatch, language):
    configuration = doc(language, "configuration")
    for name in (
        "MCP_PORT",
        "MCP_HEALTH_PORT",
        "MCP_TRANSPORT",
        "TRINO_MAX_ROWS",
        "TRINO_READ_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = Config.from_env()
    assert cfg.mcp_port == 3000 and "`3000`" in configuration
    assert cfg.health_port == 3001 and "`3001`" in configuration
    assert cfg.transport == "streamable-http" and "`streamable-http`" in configuration
    assert cfg.max_rows == 100 and "`100`" in configuration
    assert cfg.read_only is True


def test_package_version_matches_pyproject_and_changelog():
    import tomllib

    from akko_mcp_trino import __version__

    root = pathlib.Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__
    assert f"## [{__version__}]" in (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"version-{__version__}-blue" in README


@pytest.mark.parametrize("language", LANGUAGES)
def test_readme_states_the_safe_defaults_the_code_has(monkeypatch, language):
    """The defaults are the security posture: the configuration page must say the ones the
    code has, in both languages."""
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
        for m in re.finditer(
            r"^\| `([A-Z_]+)`[^\n]*\| `([^`]+)` \|$", doc(language, "configuration"), re.M
        )
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


@pytest.mark.parametrize(
    "language, heading, none",
    [("en", "## The file provider", "none"), ("fr", "## Le fournisseur `file`", "aucune")],
)
def test_readme_defaults_of_the_file_provider_match_the_code(tmp_path, language, heading, none):
    from akko_mcp_trino.context import CLASSIFICATION, GLOSSARY, OWNER, PROPERTY
    from akko_mcp_trino.context.file import from_env

    page = doc(language, "catalog-context")
    section = page[page.index(heading) :]
    section = section[: section.index("\n```", 4)]
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
    assert rows["MCP_CONTEXT_FILE"].strip() == none
    with pytest.raises(ValueError, match="MCP_CONTEXT_FILE is not set"):
        from_env({})
    for name, kind in (
        ("MCP_CONTEXT_FILE_GLOSSARIES", GLOSSARY),
        ("MCP_CONTEXT_FILE_CLASSIFICATIONS", CLASSIFICATION),
        ("MCP_CONTEXT_FILE_PROPERTIES", PROPERTY),
    ):
        assert rows[name].strip() == none and sets[kind] == frozenset(), name
        # the README says: exact names, `*` is a name, empty entries are ignored
        named = from_env({"MCP_CONTEXT_FILE": str(path), name: "a, ,*"}).served_sets()
        assert named[kind] == frozenset({"a", "*"}), name
    assert rows["MCP_CONTEXT_FILE_SERVE_OWNER"].strip() == "`false`"
    assert sets[OWNER] == frozenset()
    served = from_env({"MCP_CONTEXT_FILE": str(path), "MCP_CONTEXT_FILE_SERVE_OWNER": "true"})
    assert served.served_sets()[OWNER] != frozenset()


def _resource_groups(text: str) -> list[dict]:
    """The Trino resource group examples of a text: its JSON blocks with rootGroups."""
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
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


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_resource_group_example_selects_only_leaf_groups(language):
    """Trino refuses a query its selector puts in a group that has sub-groups: a selector
    must name a leaf, so the queries without the tag need a group of their own."""
    examples = _resource_groups(docs(language))
    assert len(examples) == 2, "profile_table's tag and trino-tags' tool tag"
    for example in examples:
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
        ("docs/en/index.md", "nine tools"),
        ("docs/fr/index.md", "neuf tools"),
        ("docs/en/tools.md", "eight other tools"),
        ("docs/fr/tools.md", "huit autres tools"),
        ("DOCKERHUB.md", "Nine tools"),
        ("akko_mcp_trino/__init__.py", "Nine tools"),
    ],
)
def test_the_public_texts_count_the_nine_tools(path, word):
    text = (pathlib.Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    assert word in text
    assert not re.search(r"\b(eight|huit) tools\b", text, re.I), "a count from 0.7"


_RIGHTS = {
    "docs/en/deployment.md": [
        "## With a data catalog",
        "Descriptions follow what Trino lists to the caller. With "
        "`MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table` also asks Trino to confirm that the "
        "caller may select the columns it lists. `describe_schema` keeps serving the descriptions "
        "of the tables Trino lists.",
        "The provider serves glossary terms, classifications and business metadata only when "
        "their glossary, classification or set is named in an allow list. The allow lists are "
        "empty by default. The owner is served only when it is configured (for Atlas: "
        "`ATLAS_GLOSSARIES`, `ATLAS_CLASSIFICATIONS`, `ATLAS_BUSINESS_METADATA`, empty means none, "
        "and `ATLAS_OWNER_ATTRIBUTE`).",
    ],
    "docs/en/configuration.md": [
        "Raising it past 8 mostly adds metadata load on Trino, with little gain.",
    ],
    "docs/fr/deployment.md": [
        "## Avec un catalogue de données",
        "Les descriptions suivent ce que Trino liste à l'appelant. Avec "
        "`MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table` demande en plus à Trino de confirmer "
        "que l'appelant peut faire un SELECT des colonnes qu'il liste. `describe_schema` continue "
        "de servir les descriptions des tables que Trino liste.",
        "Le fournisseur ne sert les termes de glossaire, les classifications et les métadonnées "
        "métier que si leur glossaire, leur classification ou leur ensemble est nommé dans une "
        "liste blanche. Les listes blanches sont vides par défaut. Le propriétaire n'est servi "
        "que s'il est configuré (pour Atlas : `ATLAS_GLOSSARIES`, `ATLAS_CLASSIFICATIONS`, "
        "`ATLAS_BUSINESS_METADATA`, vide veut dire aucun, et `ATLAS_OWNER_ATTRIBUTE`).",
    ],
    "docs/fr/configuration.md": [
        "Le monter au-delà de 8 ajoute surtout de la charge de métadonnées sur Trino, pour peu "
        "de gain.",
    ],
    "providers/atlas/README.md": [
        "Descriptions follow what Trino lists to the caller. With "
        "`MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table` also requires that Trino validates a "
        "select by the caller of the columns it lists, while `describe_schema` keeps serving the "
        "descriptions of the tables Trino lists.",
        "It serves glossary terms, classifications and business metadata only when their "
        "glossary, classification or set is named in `ATLAS_GLOSSARIES`, "
        "`ATLAS_CLASSIFICATIONS` or `ATLAS_BUSINESS_METADATA`, closed by default (empty means "
        "none), and the owner only when `ATLAS_OWNER_ATTRIBUTE` is set.",
    ],
    "providers/atlas/README.fr.md": [
        "Les descriptions suivent ce que Trino liste à l'appelant. Avec "
        "`MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table` exige en plus que Trino valide un "
        "SELECT par l'appelant des colonnes qu'il liste, tandis que `describe_schema` continue de "
        "servir les descriptions des tables que Trino liste.",
        "Il ne sert les termes de glossaire, les classifications et les métadonnées métier "
        "que si leur glossaire, leur classification ou leur ensemble est nommé dans "
        "`ATLAS_GLOSSARIES`, `ATLAS_CLASSIFICATIONS` ou `ATLAS_BUSINESS_METADATA`, fermées par "
        "défaut (vide veut dire aucun), et le propriétaire seulement si "
        "`ATLAS_OWNER_ATTRIBUTE` est posé.",
    ],
}


@pytest.mark.parametrize("readme", list(_RIGHTS))
def test_the_readme_says_what_the_context_of_a_catalogue_serves_and_under_which_rights(readme):
    """What the context serves, sentence by sentence as the Atlas provider serves it: the
    descriptions of what Trino lists to the caller; terms, classifications and business
    metadata of the sets named only; the owner only when its attribute is set."""
    text = (pathlib.Path(__file__).resolve().parents[1] / readme).read_text(encoding="utf-8")
    flat = " ".join(text.replace("\u202f", " ").replace("\u00a0", " ").split())
    for sentence in _RIGHTS[readme]:
        assert sentence in flat, sentence
    assert "what its allow lists name" not in flat and "nomment ses listes blanches" not in flat
    assert "lets the caller read" not in flat and "laisse lire à l'appelant" not in flat
    assert "only of those the caller may also select" not in flat
    assert "seulement de celles que l'appelant peut aussi" not in flat


# What a real run of the integration suite showed, quoted by the modules page of both
# languages, one per module.
_MODULE_EXAMPLES = [
    "MCP_REQUIRED_SCOPES=data:query",
    'WWW-Authenticate: Bearer resource_metadata="http://mcp-scoped:3000/.well-known/'
    'oauth-protected-resource/mcp", error="insufficient_scope", scope="data:query"',
    '"scopes_supported": ["data:query"]',
    '"token_id": "jti-docs-0001",',
    '"sql_sha256": "595c26467ad8ea2d0a0857b4bf5c094d6f6d6bfc8c88ebc289f03a523ce7faf5",',
    "X-Trino-Client-Tags: request_id=req-docs-0001,tool=execute_query",
    "20261106_173554_00003_ppe7v  [global, agents]     trino-mcp",
    "`integration/compose.yaml`",
]


@pytest.mark.parametrize("language", LANGUAGES)
def test_each_readme_quotes_a_real_example_of_each_module(language):
    text = doc(language, "modules")
    assert [e for e in _MODULE_EXAMPLES if e not in text] == []
    assert text.count("Not tested live") + text.count("Non testé en conditions réelles") == 2, (
        "Entra ID, Okta"
    )


def _sections(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    parts = re.split(r"^(#{1,6} .+)$", text, flags=re.M)
    return text, list(zip(parts[1::2], parts[2::2], strict=True))


def _commands(section):
    """The lines of the bash and text blocks of a section, comments left out."""
    lines = []
    for language, block in re.findall(r"^```(bash|text)\n(.*?)^```", section, re.M | re.S):
        for line in block.splitlines():
            line = re.sub(r"\s+#\s.*$", "", line) if language == "bash" else line
            if line.strip():
                lines.append(line.rstrip())
    return lines


def test_the_contributor_guide_exists_in_both_languages_section_for_section():
    en_text, en = _sections("CONTRIBUTING.md")
    fr_text, fr = _sections("CONTRIBUTING.fr.md")
    assert "**English** · [Français](CONTRIBUTING.fr.md)" in en_text
    assert "[English](CONTRIBUTING.md) · **Français**" in fr_text
    assert [h.split()[0] for h, _ in en] == [h.split()[0] for h, _ in fr]
    assert len(en) >= 8
    for (_, en_body), (_, fr_body) in zip(en, fr, strict=True):
        assert _commands(en_body) == _commands(fr_body)
    titles = dict(zip([h for h, _ in en], [h for h, _ in fr], strict=True))
    assert titles["## Dependencies and locks"] == "## Dépendances et verrous"
    dependencies = dict(fr)["## Dépendances et verrous"]
    assert "bash scripts/lock.sh check" in _commands(dependencies)
    readme_fr = (ROOT / "README.fr.md").read_text(encoding="utf-8")
    assert "# Dépendances et verrous" not in readme_fr
    assert "/CONTRIBUTING.fr.md)" in readme_fr and "/CONTRIBUTING.md)" not in readme_fr
