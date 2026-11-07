"""The documentation exists in English and in French, page for page, and its links resolve.

`docs/en` and `docs/fr` hold the same pages, with the same headings in the same order and the
same commands. Every relative link and every anchor of the documentation points at something
that exists. The READMEs and the Docker Hub page are read on PyPI and Docker Hub too, where a
relative link breaks: they link with absolute URLs only.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EN, FR = ROOT / "docs" / "en", ROOT / "docs" / "fr"
PAGES = sorted(p.name for p in EN.glob("*.md"))
# The pages a reader follows from the READMEs, in both languages.
LINKED = [*EN.glob("*.md"), *FR.glob("*.md")] + [
    ROOT / name
    for name in (
        "CONTRIBUTING.md",
        "CONTRIBUTING.fr.md",
        "SECURITY.md",
        "SECURITY.fr.md",
        "examples/README.md",
        "docs/guides/connect-claude-desktop-cursor.md",
    )
]


def _prose(text: str) -> str:
    """The text outside fenced code blocks, a fence indented in a list included."""
    kept, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            kept.append(line)
    return "\n".join(kept)


def headings(text: str) -> list[str]:
    return [line for line in _prose(text).splitlines() if re.match(r"#{1,6} ", line)]


def _slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces as hyphens."""
    title = heading.lstrip("#").strip().lower()
    return re.sub(r"[^\w\- ]", "", title).replace(" ", "-")


def anchors(path: pathlib.Path) -> set[str]:
    seen: dict[str, int] = {}
    found = set()
    for heading in headings(path.read_text(encoding="utf-8")):
        slug = _slug(heading)
        found.add(slug if slug not in seen else f"{slug}-{seen[slug]}")
        seen[slug] = seen.get(slug, 0) + 1
    return found


def _links(text: str) -> list[str]:
    prose = re.sub(r"`[^`\n]*`", "", _prose(text))
    return re.findall(r"\]\(([^)\s]+)\)", prose) + re.findall(r'(?:src|srcset)="([^"]+)"', prose)


def commands(text: str) -> list[str]:
    """The lines of the bash and text blocks, comments left out."""
    lines = []
    for language, block in re.findall(r"^```(bash|text)\n(.*?)^```", text, re.M | re.S):
        for line in block.splitlines():
            if language == "bash":
                line = re.sub(r"(^|\s+)#\s.*$", "", line)
            if line.strip():
                lines.append(line.rstrip())
    return lines


def test_the_two_trees_hold_the_same_pages():
    assert PAGES and PAGES == sorted(p.name for p in FR.glob("*.md"))
    assert {"index.md", "quickstart.md", "configuration.md", "security.md"} <= set(PAGES)


@pytest.mark.parametrize("page", PAGES)
def test_each_page_has_the_same_headings_and_commands_in_both_languages(page):
    en = (EN / page).read_text(encoding="utf-8")
    fr = (FR / page).read_text(encoding="utf-8")
    assert [h.split()[0] for h in headings(en)] == [h.split()[0] for h in headings(fr)]
    assert commands(en) == commands(fr)
    assert f"**English** · [Français](../fr/{page})" in en
    assert f"[English](../en/{page}) · **Français**" in fr


def test_the_readmes_mirror_each_other_heading_for_heading():
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    fr = (ROOT / "README.fr.md").read_text(encoding="utf-8")
    assert [h.split()[0] for h in headings(en)] == [h.split()[0] for h in headings(fr)]
    assert commands(en) == commands(fr)
    for page in PAGES:
        if page != "index.md":
            assert f"/docs/en/{page})" in en and f"/docs/fr/{page})" in fr, page


@pytest.mark.parametrize("path", LINKED, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_and_anchor_resolves(path):
    broken = []
    for link in _links(path.read_text(encoding="utf-8")):
        if re.match(r"(https?|mailto):", link):
            continue
        target, _, anchor = link.partition("#")
        resolved = (path.parent / target).resolve() if target else path
        if not resolved.exists():
            broken.append(link)
        elif anchor and resolved.suffix == ".md" and anchor not in anchors(resolved):
            broken.append(link)
    assert broken == []


@pytest.mark.parametrize("name", ["README.md", "README.fr.md", "DOCKERHUB.md"])
def test_the_pages_read_outside_github_link_with_absolute_urls(name):
    links = _links((ROOT / name).read_text(encoding="utf-8"))
    assert links
    assert [link for link in links if not re.match(r"(https://|mailto:)", link)] == []


def test_the_absolute_links_of_the_readmes_name_files_that_exist():
    base = "https://github.com/AKKO-p/akko-mcp-trino/"
    broken = []
    for name in ("README.md", "README.fr.md", "DOCKERHUB.md"):
        for link in _links((ROOT / name).read_text(encoding="utf-8")):
            if not link.startswith(base) or link.endswith("#readme") or "/issues" in link:
                continue
            path, _, anchor = link.removeprefix(base).partition("#")
            target = ROOT / re.sub(r"^(blob|tree)/main/", "", path)
            if not target.exists() or (anchor and anchor not in anchors(target)):
                broken.append(f"{name}: {link}")
    assert broken == []


@pytest.mark.parametrize("language", ["en", "fr"])
def test_the_stdio_entry_of_the_hosts_page_is_the_example_file(language):
    """The entry the page shows is the one examples/mcp-stdio.json holds, byte for byte once
    parsed, so that a fix to one cannot leave the other behind."""
    import json

    page = (ROOT / "docs" / language / "hosts.md").read_text(encoding="utf-8")
    blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", page, re.S)]
    example = json.loads((ROOT / "examples" / "mcp-stdio.json").read_text(encoding="utf-8"))
    assert example in blocks


@pytest.mark.parametrize("language", ["en", "fr"])
def test_the_vscode_file_of_the_hosts_page_is_the_example_file(language):
    """The .vscode/mcp.json the page shows is examples/vscode-mcp.json, byte for byte once
    parsed."""
    import json

    page = (ROOT / "docs" / language / "hosts.md").read_text(encoding="utf-8")
    blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", page, re.S)]
    example = json.loads((ROOT / "examples" / "vscode-mcp.json").read_text(encoding="utf-8"))
    assert example in blocks


@pytest.mark.parametrize("language", ["en", "fr"])
def test_the_claude_code_stdio_command_launches_the_stdio_entry(language):
    """`claude mcp add ... -- uvx ...` on the page sets the variables of examples/mcp-stdio.json,
    in the same order and with the same values, and runs the same command."""
    import json
    import shlex

    page = (ROOT / "docs" / language / "hosts.md").read_text(encoding="utf-8")
    [command] = [
        b for b in re.findall(r"```bash\n(.*?)```", page, re.S) if "--env MCP_TRANSPORT" in b
    ]
    words = shlex.split(command.replace("\\\n", " "))
    cut = words.index("--")
    env = [words[i + 1].split("=", 1) for i in range(cut) if words[i] == "--env"]
    entry = json.loads((ROOT / "examples" / "mcp-stdio.json").read_text(encoding="utf-8"))
    entry = entry["mcpServers"]["trino"]
    assert words[:4] == ["claude", "mcp", "add", "trino"]
    assert [tuple(e) for e in env] == list(entry["env"].items())
    assert words[cut + 1 :] == [entry["command"], *entry["args"]]


@pytest.mark.parametrize("language", ["en", "fr"])
def test_the_mistral_agent_of_the_hosts_page_is_the_example_file(language):
    """The code and the commands the page shows are those of examples/mistral_agents.py."""
    page = (ROOT / "docs" / language / "hosts.md").read_text(encoding="utf-8")
    example = (ROOT / "examples" / "mistral_agents.py").read_text(encoding="utf-8")
    [code] = [b for b in re.findall(r"```python\n(.*?)```", page, re.S) if "RunContext" in b]
    [run] = [b for b in re.findall(r"```bash\n(.*?)```", page, re.S) if "mistral_agents" in b]
    assert code in example
    assert all(f"    {line}" in example.splitlines() for line in run.splitlines())


# The one sentence that says what SHOW STATS or EXPLAIN can show, in each language.
_ALLOWED = {
    "en": "Some reads, such as `SHOW STATS` or `EXPLAIN`, can show facts beyond a row filter or a "
    "column mask.",
    "fr": "Certaines lectures, comme `SHOW STATS` ou `EXPLAIN`, peuvent révéler des informations "
    "au-delà d'un filtre de lignes ou d'un masque de colonne.",
}
_SUBJECT = {
    "en": re.compile(r"beyond a row filter|statistics of the query|past a row filter", re.I),
    "fr": re.compile(
        r"au-delà d'un filtre de lignes|au-delà d'un row filter|statistiques de la requête", re.I
    ),
}


@pytest.mark.parametrize("language", ["en", "fr"])
def test_only_the_security_page_says_what_extra_reads_can_show(language):
    readme = "README.md" if language == "en" else "README.fr.md"
    texts = {readme: (ROOT / readme).read_text(encoding="utf-8")}
    texts |= {
        str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "docs" / language).glob("*.md"))
    }
    flat = {
        name: " ".join(text.replace("\u202f", " ").replace("\u00a0", " ").split())
        for name, text in texts.items()
    }
    found = {name: len(_SUBJECT[language].findall(t)) for name, t in flat.items()}
    assert {name: n for name, n in found.items() if n} == {f"docs/{language}/security.md": 1}
    assert _ALLOWED[language] in flat[f"docs/{language}/security.md"]
