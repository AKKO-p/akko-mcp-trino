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
