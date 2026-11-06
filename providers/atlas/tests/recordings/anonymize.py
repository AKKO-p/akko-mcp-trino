"""Turn answers recorded from a real Atlas into neutral recordings, structure kept.

Record with reads only (``GET``, and ``POST /api/atlas/v2/search/basic``), with an account
that may only read, and keep the raw answers off the repository. Then::

    python anonymize.py RAW_DIR OUT_DIR [--names NAMES.json]

``RAW_DIR`` holds one JSON file per exchange, ``{"request": {"method", "path", "query",
"body"}, "response": {"status", "content_type", "body"}}``, where ``query`` is a list of
``[name, value]`` pairs. Each file is written to ``OUT_DIR`` under the same name, with:

- every name a catalogue gives (database, table, column, glossary, term, category,
  classification, business metadata and its attributes, cluster, user) replaced by a
  neutral one, the same everywhere it appears, qualified names included;
- every free text (descriptions, comments, examples, attribute values) replaced by a
  short text naming the field and the object it belongs to;
- every GUID replaced by a sequential one, every timestamp by a fixed one, the server's
  build revision by zeros and its version cut to ``major.minor.patch``.

Neutral names are given in order of first appearance (``schema_01``, ``table_01``,
``Term 01``...). ``NAMES.json`` may choose some of them, for instance so that the tables
the tests use carry the names of the contract: ``{"<role>": {"<real name>": "<neutral
name>"}, "values": {"<real value>": "<neutral value>"}}``, with the roles ``db``,
``table``, ``column``, ``glossary``, ``term``, ``category``, ``classification``,
``bm_type``, ``bm_attr``, ``user`` and ``cluster``. That file holds real names: keep it
out of the repository.

Any string the script does not know how to treat stops it, naming the field but never
the value: nothing passes through unexamined.
"""

import argparse
import json
import re
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
AUTO = {
    "db": "schema_{:02d}",
    "table": "table_{:02d}",
    "column": "column_{:02d}",
    "glossary": "glossary_{:02d}",
    "term": "Term {:02d}",
    "category": "Category {:02d}",
    "classification": "class_{:02d}",
    "bm_type": "set_{:02d}",
    "bm_attr": "attribute_{:02d}",
    "user": "user_{:02d}",
    "cluster": "cluster_{:02d}",
}
# The kind of object an Atlas type name stands for.
KINDS = (
    (re.compile(r"_db$"), "db"),
    (re.compile(r"_column$"), "column"),
    (re.compile(r"_(table|view)$"), "table"),
    (re.compile(r"^AtlasGlossaryTerm$"), "term"),
    (re.compile(r"^AtlasGlossaryCategory$"), "category"),
    (re.compile(r"^AtlasGlossary$"), "glossary"),
)
# Lists of related terms in a term, and of the objects they hold.
TERM_LISTS = {
    "seeAlso",
    "synonyms",
    "antonyms",
    "preferredTerms",
    "preferredToTerms",
    "replacementTerms",
    "replacedBy",
    "translationTerms",
    "translatedTerms",
    "isA",
    "classifies",
    "validValues",
    "validValuesFor",
    "terms",
    "meanings",
}
FREE_TEXT = {
    "description",
    "comment",
    "userDescription",
    "shortDescription",
    "longDescription",
    "examples",
    "usage",
    "abbreviation",
    "source",
    "expression",
    "viewOriginalText",
    "viewExpandedText",
    "displayName",
}
# The message of a 404 on a read by unique attribute: the qualified name in it is replaced.
MISSING = re.compile(r"Instance (\S+) with unique attribute \{qualifiedName=(.+)\} does not exist")
# Values Atlas itself writes: kept as they are.
STRUCTURAL = [
    re.compile(p)
    for p in (
        r"^$",
        r"^(hive|iceberg|hdfs|spark|impala|kafka|hbase|aws|avro|rdbms)_[a-z_]+$",
        r"^Atlas[A-Za-z]+$",
        r"^__[A-Za-z_]+$",
        r"^(ACTIVE|DELETED|PURGED|SHELL)$",
        r"^(GET|POST)$",
        r"^(BASIC|DSL|FULL_TEXT|GREMLIN|ATTRIBUTE|RELATIONSHIP)$",
        r"^(MANAGED_TABLE|EXTERNAL_TABLE|VIRTUAL_VIEW|MATERIALIZED_VIEW|ICEBERG)$",
        r"^(ONE_TO_TWO|TWO_TO_ONE|BOTH|NONE)$",
        r"^(contains|endsWith|startsWith|eq|neq|=|!=|lt|gt|lte|gte|AND|OR)$",
        r"^(application/json|text/html|text/plain)$",
        r"^(qualifiedName|name|__guid|__typeName)$",
        r"^attr_\d+:qualifiedName$",
        r"^(tinyint|smallint|int|integer|bigint|boolean|float|double|date|timestamp|string"
        r"|binary|varchar\(\d+\)|char\(\d+\)|decimal\(\d+,\s*\d+\)|(array|map|struct)<.*>)$",
        r"^apache-atlas$",
        r"^jersey-servlet$",
        r"^Metadata Management and Data Governance Platform over Hadoop$",
        r"^Full authentication is required to access this resource$",
        r"^ATLAS-\d{3}-\d{2}-[0-9A-F]{3}$",
        r"^Type ENTITY with name [a-z_]+ does not exist$",
        r"^\d+$",
    )
]


class Anonymizer:
    """Rewrites recordings with neutral names, consistent across every file it reads."""

    def __init__(self, chosen: dict[str, dict[str, str]]) -> None:
        """Start from the neutral names ``chosen`` (by role, then real name)."""
        self.values: dict[str, str] = dict(chosen.get("values", {}))
        self.names: dict[str, dict[str, str]] = {role: dict(chosen.get(role, {})) for role in AUTO}
        for role, names in self.names.items():
            if len(set(names.values())) != len(names):
                raise SystemExit(f"two real {role} names get the same neutral name")
        self.guids: dict[str, str] = {}

    # Names --------------------------------------------------------------------------------

    def name(self, role: str, real: str) -> str:
        """The neutral name of ``real`` in ``role``, given on first sight."""
        if not real:
            return real
        names = self.names[role]
        if real not in names:
            taken = set(names.values())
            n = len(names) + 1
            while AUTO[role].format(n) in taken:
                n += 1
            names[real] = AUTO[role].format(n)
        return names[real]

    def qualified(self, kind: str, real: str, where: str) -> str:
        """A qualified name of an object of ``kind``, each part replaced."""
        if kind == "glossary":
            return self.name("glossary", real)
        left, at, right = real.rpartition("@")
        if not at:
            raise SystemExit(f"{where}: a qualified name without '@'")
        if kind in ("term", "category"):
            parts = left.split(".") if kind == "category" else [left]
            names = [self.name(kind, p) for p in parts]
            return ".".join(names) + "@" + self.name("glossary", right)
        roles = {"db": ["db"], "table": ["db", "table"], "column": ["db", "table", "column"]}
        wanted = roles[kind]
        parts = self.split(left, wanted, where)
        names = [self.name(role, p) for role, p in zip(wanted, parts)]
        return ".".join(names) + "@" + self.name("cluster", right)

    def split(self, dotted: str, roles: list[str], where: str) -> list[str]:
        """Split ``dotted`` into one part per role. A name may hold a dot: then the split
        whose parts but the last are names already known in their roles is taken."""
        pieces = dotted.split(".")
        if len(pieces) == len(roles):
            return pieces
        splits = []
        for cuts in combinations(range(1, len(pieces)), len(roles) - 1):
            bounds = (0, *cuts, len(pieces))
            parts = [".".join(pieces[a:b]) for a, b in zip(bounds, bounds[1:])]
            if all(p in self.names[r] for r, p in zip(roles[:-1], parts[:-1])):
                splits.append(parts)
        if len(splits) != 1:
            raise SystemExit(f"{where}: a qualified name that splits {len(splits)} ways")
        return splits[0]

    def guid(self, real: str) -> str:
        """A sequential GUID in place of ``real``, the same everywhere."""
        if real not in self.guids:
            self.guids[real] = f"00000000-0000-4000-8000-{len(self.guids) + 1:012d}"
        return self.guids[real]

    def guids_in(self, text: str) -> str:
        """``text`` with every GUID it holds replaced."""
        return UUID.sub(lambda m: self.guid(m.group(0)), text)

    # Walking ------------------------------------------------------------------------------

    def exchange(self, doc: dict[str, Any], where: str) -> dict[str, Any]:
        """One recorded exchange, request and response."""
        request, response = doc["request"], doc["response"]
        path = request["path"]
        kind = next(
            (k for p, k in KINDS for t in re.findall(r"/type/([^/?]+)", path) if p.search(t)),
            "table",
        )
        query = [
            [
                name,
                self.qualified(kind, value, f"{where}: query")
                if name.endswith(":qualifiedName")
                else self.leaf(name, value, {}, where),
            ]
            for name, value in request["query"]
        ]
        return {
            "request": {
                "method": request["method"],
                "path": self.guids_in(path),
                "query": query,
                "body": self.walk(request["body"], {}, f"{where}: request"),
            },
            "response": {
                "status": response["status"],
                "content_type": response["content_type"],
                "body": self.walk(response["body"], {}, f"{where}: response"),
            },
        }

    def walk(self, node: Any, ctx: dict[str, Any], where: str) -> Any:
        """Rewrite ``node``; ``ctx`` says what kind of object holds it and its label."""
        if isinstance(node, list):
            return [self.walk(item, ctx, where) for item in node]
        if isinstance(node, dict):
            return self.mapping(node, ctx, where)
        if isinstance(node, str):
            return self.leaf(ctx.get("key", ""), node, ctx, where)
        if isinstance(node, int) and not isinstance(node, bool) and 10**12 < node < 4 * 10**12:
            return 1_700_000_000_000
        return node

    def mapping(self, node: dict[str, Any], ctx: dict[str, Any], where: str) -> dict:
        """A JSON object: find what it describes, then rewrite its keys and values."""
        parent = ctx.get("key", "")
        kind = self.kind_of(node, parent, ctx)
        label = self.label(node, kind, ctx, where)
        out: dict[str, Any] = {}
        for key, value in node.items():
            inner = {"key": key, "kind": kind, "label": label, "parent": parent}
            if parent == "referredEntities":
                out[self.guid(key)] = self.walk(value, {"key": "", "label": label}, where)
            elif parent == "businessAttributes":
                attrs = {"key": "bm", "label": label}
                out[self.name("bm_type", key)] = self.attributes(value, attrs, where)
            elif key == "additionalAttributes" and isinstance(value, dict):
                out[key] = self.attributes(value, {"key": "bm", "label": label}, where)
            else:
                out[key] = self.walk(value, inner, f"{where}.{key}")
        return out

    def attributes(self, attrs: Any, ctx: dict[str, Any], where: str) -> Any:
        """Business attributes: names neutral, values by what they hold."""
        if not isinstance(attrs, dict):
            return self.walk(attrs, ctx, where)
        out = {}
        for key, value in attrs.items():
            neutral = self.name("bm_attr", key)
            if isinstance(value, str) and value in self.values:
                out[neutral] = self.values[value]
            elif isinstance(value, str) and key in ("steward", "owner"):
                out[neutral] = self.name("user", value)
            elif isinstance(value, str):
                out[neutral] = f"{neutral} of {ctx['label']}" if value else value
            else:
                out[neutral] = self.walk(value, {"key": ""}, f"{where}.{neutral}")
        return out

    def kind_of(self, node: dict[str, Any], parent: str, ctx: dict[str, Any]) -> str:
        """The kind of object a JSON object describes, or an empty string."""
        if parent == "classifications":
            return "classification"
        type_name = node.get("typeName")
        if isinstance(type_name, str):
            for pattern, kind in KINDS:
                if pattern.search(type_name):
                    return kind
        if parent in TERM_LISTS or parent == "glossaryTermHeader":
            return "term"
        if parent == "categories":
            return "category"
        if "anchor" in node or "glossaryTermHeader" in node:
            return "term"
        if parent in ("attributes", "entityFilters"):
            return str(ctx.get("kind", ""))
        return ""

    def label(self, node: dict[str, Any], kind: str, ctx: dict[str, Any], where: str) -> str:
        """The neutral name free texts of this object are written after."""
        attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else node
        for key in ("qualifiedName", "displayText", "name"):
            value = attrs.get(key)
            if kind and isinstance(value, str) and value:
                if key == "qualifiedName":
                    neutral = self.qualified(kind, value, where)
                    return neutral.rpartition("@")[0] or neutral
                return self.name(kind, value)
        if kind == "classification" and isinstance(node.get("typeName"), str):
            return self.name("classification", node["typeName"])
        return ctx.get("label", "the object")

    def leaf(self, key: str, value: str, ctx: dict[str, Any], where: str) -> str:
        """One string, by the field it is in."""
        kind = ctx.get("kind", "")
        if value in self.values:
            return self.values[value]
        if UUID.fullmatch(value) or key.endswith("Guid") or key == "guid":
            return self.guids_in(value)
        if key == "Version":
            return ".".join(value.split(".")[:3])
        if key == "Revision":
            return "0" * 40
        if key == "path" or (key == "url" and value.startswith("/api/atlas/")):
            return self.guids_in(value)
        if key == "typeName" and kind == "classification":
            return self.name("classification", value)
        if key in ("classificationNames",):
            return self.name("classification", value)
        if key == "meaningNames":
            return self.name("term", value)
        if key == "termName":
            return self.qualified("term", value, where)
        if key == "attributeValue" and value.startswith("@"):
            return "@" + self.name("glossary", value[1:])
        if key == "attributeValue" and "@" in value and kind:
            return self.qualified(kind, value, where)
        missing = MISSING.fullmatch(value) if key == "errorMessage" else None
        if missing:
            found = [k for p, k in KINDS if p.search(missing.group(1))]
            neutral = self.qualified(found[0] if found else "table", missing.group(2), where)
            start, end = missing.span(2)
            return value[:start] + neutral + value[end:]
        if key in ("createdBy", "updatedBy", "owner", "steward"):
            return self.name("user", value)
        if key in ("qualifiedName",) and kind:
            return self.qualified(kind, value, where)
        if key in ("name", "displayText") and kind:
            return self.name(kind, value)
        if key in FREE_TEXT:
            return f"{key} of {ctx.get('label', 'the object')}" if value else value
        if any(p.fullmatch(value) for p in STRUCTURAL):
            return value
        raise SystemExit(f"{where}: no rule for the field {key!r} (value not shown)")


def run(raw: Path, out: Path, chosen: dict[str, dict[str, str]]) -> Anonymizer:
    """Rewrite every recording of ``raw`` into ``out``; returns the anonymizer for checks."""
    anonymizer = Anonymizer(chosen)
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(raw.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        neutral = anonymizer.exchange(doc, path.name)
        text = json.dumps(neutral, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        (out / path.name).write_text(text, encoding="utf-8")
    return anonymizer


def main(argv: list[str]) -> int:
    """Command line entry: ``RAW_DIR OUT_DIR [--names NAMES.json]``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("raw", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--names", help="JSON file of chosen neutral names (keep it private)")
    args = parser.parse_args(argv)
    chosen = json.loads(Path(args.names).read_text(encoding="utf-8")) if args.names else {}
    anonymizer = run(args.raw, args.out, chosen)
    counts = {role: len(names) for role, names in anonymizer.names.items() if names}
    print(f"{len(list(args.out.glob('*.json')))} recordings written; names by role: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
