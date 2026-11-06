"""The recorded answers hold together: what one recording names, the others agree with."""

import json
from pathlib import Path

import pytest

RECORDINGS = Path(__file__).parent / "recordings"
VERSIONS = sorted(p for p in RECORDINGS.iterdir() if p.is_dir() and p.name.startswith("atlas-"))
TERM = "AtlasGlossaryTerm"


def _named_terms(node, found: dict[str, set[str]]) -> None:
    """Every glossary term a recording names, by GUID: entities, headers and term reads."""
    if isinstance(node, list):
        for item in node:
            _named_terms(item, found)
        return
    if not isinstance(node, dict):
        return
    attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else {}
    qualified = node.get("qualifiedName") or attrs.get("qualifiedName")
    is_term = node.get("typeName") == TERM or "anchor" in node or "termGuid" in node
    guid = node.get("termGuid") or node.get("guid")
    if is_term and isinstance(qualified, str) and isinstance(guid, str):
        found.setdefault(guid, set()).add(qualified)
    for value in node.values():
        _named_terms(value, found)


@pytest.mark.parametrize("version", VERSIONS, ids=lambda p: p.name)
def test_every_term_named_is_in_its_glossary_listing(version):
    named: dict[str, set[str]] = {}
    listed: dict[str, set[str]] = {}
    for path in sorted(version.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        _named_terms(doc, named)
        if path.name.startswith("search-terms-of-glossary-"):
            glossary = doc["request"]["body"]["entityFilters"]["attributeValue"].lstrip("@")
            for entity in doc["response"]["body"].get("entities") or []:
                listed.setdefault(glossary, set()).add(entity["guid"])
    assert listed, "no glossary listing recorded"
    assert all(len(names) == 1 for names in named.values()), "a GUID names two terms"
    for guid, (qualified,) in named.items():
        glossary = qualified.rpartition("@")[2]
        if glossary in listed:
            assert guid in listed[glossary], f"{qualified} is missing from its glossary listing"


@pytest.mark.parametrize("version", VERSIONS, ids=lambda p: p.name)
def test_a_paging_client_finds_every_named_term(version):
    """Pages read as a client reads them: offset 0, then by the limit, until a short page."""
    named: dict[str, set[str]] = {}
    pages: dict[tuple[str, int], dict] = {}
    for path in sorted(version.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        _named_terms(doc, named)
        if path.name.startswith("search-terms-of-glossary-"):
            body = doc["request"]["body"]
            glossary = body["entityFilters"]["attributeValue"].lstrip("@")
            pages[(glossary, body["offset"])] = doc
    for glossary in {g for g, _ in pages}:
        limit = pages[(glossary, 0)]["request"]["body"]["limit"]
        seen: set[str] = set()
        offset = 0
        while True:
            assert (glossary, offset) in pages, f"no page at offset {offset} of {glossary}"
            page = pages[(glossary, offset)]
            assert page["request"]["body"]["limit"] == limit
            entities = page["response"]["body"].get("entities") or []
            seen |= {entity["guid"] for entity in entities}
            if len(entities) < limit:
                break
            offset += limit
        for guid, (qualified,) in named.items():
            if qualified.endswith("@" + glossary):
                assert guid in seen, f"a paging client never reads {qualified}"
