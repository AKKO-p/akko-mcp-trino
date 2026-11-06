"""An Atlas made of the recorded answers, for ``httpx.MockTransport``; never the network.

A bulk read is answered from the recording of its entity type: the recorded entities
whose qualified name was asked, and the entities they refer to, as Atlas answers a
smaller or larger batch. A type recorded with an error (Atlas 2.4 does not know
``iceberg_table``) answers that error for any batch. A glossary term is answered from its
recorded read by GUID.
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

RECORDINGS = Path(__file__).parent / "recordings"
VERSIONS = ("atlas-2.1", "atlas-2.4")
BULK = "/api/atlas/v2/entity/bulk/uniqueAttribute/type/"
TERM = "/api/atlas/v2/glossary/term/"
ATLAS = "http://atlas-a:21000"
# The trap table of the recordings, by Trino schema and table: a quote, a dot, a space, accents.
TRAP = ("o'brien", "été.l'année fiscale.v2")


def term_not_found(guid: str) -> httpx.Response:
    """Atlas's answer to a term read by a GUID it does not hold.

    Constructed in the format of the recorded 404 answers (``errorCode``,
    ``errorMessage``), with the code and message of Atlas's ``INSTANCE_GUID_NOT_FOUND``.
    To be replaced by the recording of that answer from the target Atlas.
    """
    return httpx.Response(
        404,
        json={
            "errorCode": "ATLAS-404-00-005",
            "errorMessage": f"Given instance guid {guid} is invalid/not found",
        },
    )


def recording(version: str, name: str) -> dict[str, Any]:
    """One recorded exchange, a fresh copy each time."""
    path = RECORDINGS / version / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def asked(request: httpx.Request) -> list[str]:
    """The qualified names a bulk read asks for, in order."""
    return [v for k, v in request.url.params.multi_items() if k.endswith(":qualifiedName")]


def _referred_guids(entity: dict[str, Any]) -> set[str]:
    relations = entity.get("relationshipAttributes") or {}
    return {
        header["guid"]
        for key in ("columns", "partitionKeys")
        for header in relations.get(key) or []
        if isinstance(header, dict)
    }


class FakeAtlas:
    """Answers the bulk reads of one recorded version; records every request it gets.

    A qualified name is matched exactly, case included, as Atlas's unique attribute
    lookup does.
    """

    def __init__(self, version: str) -> None:
        self.bulk: dict[str, dict[str, Any]] = {}
        for path in sorted((RECORDINGS / version).glob("bulk-*.json")):
            if not path.stem.endswith("-unknown-only"):
                self.bulk[path.stem.removeprefix("bulk-")] = recording(version, path.stem)
        self.terms: dict[str, dict[str, Any]] = {}
        for path in sorted((RECORDINGS / version).glob("glossary-term-*.json")):
            body = recording(version, path.stem)["response"]["body"]
            self.terms[body["guid"]] = body
        self.requests: list[httpx.Request] = []

    def entities(self, type_name: str) -> list[dict[str, Any]]:
        """The recorded entities of a type, to edit before a test replays them."""
        return self.bulk[type_name]["response"]["body"]["entities"]

    def referred(self, type_name: str) -> dict[str, dict[str, Any]]:
        """The recorded referred entities of a type, to edit before a test replays them."""
        return self.bulk[type_name]["response"]["body"]["referredEntities"]

    def entity(self, type_name: str, qualified_name: str) -> dict[str, Any]:
        """One recorded entity of a type, by qualified name."""
        (found,) = (
            e
            for e in self.entities(type_name)
            if e["attributes"]["qualifiedName"] == qualified_name
        )
        return found

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one request as the recorded Atlas did."""
        self.requests.append(request)
        path = request.url.path
        if path.startswith(TERM):
            guid = path.removeprefix(TERM)
            term = self.terms.get(guid)
            if term is None:
                return term_not_found(guid)
            return httpx.Response(200, json=copy.deepcopy(term))
        if not path.startswith(BULK):
            return httpx.Response(404, json={"errorCode": "ATLAS-404-00-005"})
        exchange = self.bulk.get(path.removeprefix(BULK))
        if exchange is None:
            return httpx.Response(400, json={"errorCode": "ATLAS-400-00-014"})
        response = exchange["response"]
        if response["status"] != 200:
            return httpx.Response(response["status"], json=response["body"])
        names = asked(request)
        wanted = set(names)
        body = copy.deepcopy(response["body"])
        entities = [e for e in body["entities"] if e["attributes"]["qualifiedName"] in wanted]
        guids = set().union(*map(_referred_guids, entities)) if entities else set()
        referred = {g: e for g, e in body["referredEntities"].items() if g in guids}
        answer = {"entities": entities, "referredEntities": referred} if entities else {}
        return httpx.Response(200, json=answer)


def transport(handler: Callable[[httpx.Request], Any]) -> httpx.MockTransport:
    """A transport that answers with ``handler``, sync or async."""
    return httpx.MockTransport(handler)
