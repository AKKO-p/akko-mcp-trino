"""Names are quoted, never filtered: the metadata tools against a real Trino.

Run by ``integration/names/run.sh``, with the host name of a disposable Trino that has an
Iceberg catalog ``lake`` and a memory catalog ``mem``. It creates the tables, calls the
tools in process as the service account, and exits 1 on the first check that fails.
"""

import json
import sys
import time
from dataclasses import replace

import trino

from akko_mcp_trino.config import Config
from akko_mcp_trino.tools import register_query_tools
from akko_mcp_trino.trino_client import TrinoClient

HOST = sys.argv[1]
SETUP = [
    "CREATE SCHEMA lake.sales",
    "CREATE TABLE lake.sales.orders (id bigint, amount double)",
    "INSERT INTO lake.sales.orders VALUES (1, 10.5)",
    "INSERT INTO lake.sales.orders VALUES (2, 20.0)",
    'CREATE SCHEMA lake."Ventes_Été"',
    'CREATE TABLE lake."Ventes_Été"."Données_Café" ("Montant" double, id bigint)',
    'INSERT INTO lake."Ventes_Été"."Données_Café" VALUES (3.5, 1)',
    'CREATE SCHEMA lake."2024_sales"',
    'CREATE TABLE lake."2024_sales"."we""ird t" (x bigint)',
    'CREATE SCHEMA mem."Mémoire"',
    'CREATE TABLE mem."Mémoire"."Été" (v varchar)',
    'INSERT INTO mem."Mémoire"."Été" VALUES (\'ok\')',
]
failed = 0


def sql(statement: str) -> list:
    conn = trino.dbapi.connect(host=HOST, port=8080, user="admin", http_scheme="http")
    cur = conn.cursor()
    cur.execute(statement)
    return cur.fetchall()


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {detail}"))
    failed += not ok


class Tools:
    """Captures the tools so the proof calls them directly."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, annotations=None, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def main() -> int:
    for _ in range(60):
        try:
            sql("SELECT 1")
            break
        except Exception:  # noqa: BLE001 - Trino still starting
            time.sleep(2)
    print("Trino", sql("SELECT node_version FROM system.runtime.nodes")[0][0])
    for statement in SETUP:
        sql(statement)

    config = replace(
        Config.from_env(),
        trino_host=HOST,
        trino_port=8080,
        trino_http_scheme="http",
        trino_user="admin",
        trino_identity_mode="service",
        trino_catalog="lake",
    )
    mcp = Tools()
    register_query_tools(mcp, TrinoClient(config), read_only=True, max_rows=100, max_catalogs=100)
    t = mcp.tools

    def call(tool: str, *args: object, **kw: object) -> dict:
        return json.loads(t[tool](*args, **kw))

    out = call("describe_table", "lake", "sales", "orders$snapshots", 1)
    names = [r[0] for r in out.get("rows", [])]
    check("describe_table on orders$snapshots", "snapshot_id" in names, out)
    check("with one sample row", len(out.get("sample", {}).get("rows", [])) == 1, out)
    out = call("profile_table", "lake", "sales", "orders$snapshots", ["snapshot_id"], "full")
    snapshots = sql('SELECT count(*) FROM lake.sales."orders$snapshots"')[0][0]
    check("profile_table on orders$snapshots", out.get("rows") == snapshots, out)
    out = call("describe_table", "LAKE", "Ventes_Été", "Données_Café$snapshots")
    check("describe_table on a mixed-case accented $snapshots", "rows" in out, out)
    out = call("list_tables", "lake", "Ventes_Été")
    check("list_tables on a mixed-case accented schema", out.get("tables") == ["données_café"], out)
    out = call("list_tables", "lake", "ventes_été")
    check("list_tables on the same schema folded", out.get("tables") == ["données_café"], out)
    out = call("describe_table", "lake", "Ventes_Été", "Données_Café", 1)
    check(
        "describe_table on an accented table, Trino folds the quoted name",
        [r[0] for r in out.get("rows", [])] == ["montant", "id"]
        and out.get("sample", {}).get("rows") == [[3.5, 1]],
        out,
    )
    out = call("describe_schema", "lake", "Ventes_Été")
    check(
        "describe_schema on an accented schema",
        out.get("schema") == "ventes_été" and out["tables"][0]["table"] == "données_café",
        out,
    )
    out = call("list_tables", "lake", "2024_sales")
    check("list_tables on a schema that starts with a digit", len(out.get("tables", [])) == 1, out)
    out = call("describe_table", "lake", "2024_sales", 'we"ird t')
    check("describe_table, a quote in a name", out.get("rows") == [["x", "bigint", "", ""]], out)
    out = call("search_columns", "montant", "lake", "Ventes_Été")
    check("search_columns in an accented schema", len(out.get("matches", [])) == 1, out)
    out = call("search_columns", "montant", "lake")
    whole = out.get("complete") is True and len(out.get("matches", [])) == 1
    check("search_columns in the whole catalog", whole, out)
    out = call("describe_table", "mem", "Mémoire", "Été", 1)
    check("describe_table in a memory catalog", out.get("sample", {}).get("rows") == [["ok"]], out)
    out = call("describe_table", "lake", "sales", 'orders"; DROP TABLE lake.sales.orders; --')
    check("a hostile name is one name Trino does not find", out.get("kind") == "table", out)
    out = call("list_tables", "lake", "sales' OR '1'='1")
    check("a hostile schema is one literal, not found", out.get("kind") == "schema", out)
    out = call("list_tables", "lake", "sa\nles")
    check("a name with a line break is refused", out.get("kind") == "invalid_argument", out)
    out = call("describe_table", "SYSTEM", "runtime", "queries")
    check("the system catalog is refused", "system catalog" in out.get("error", ""), out)
    check("the table is still there", sql("SELECT count(*) FROM lake.sales.orders") == [[2]])
    print("FAILED" if failed else "PASSED", f"{failed} check(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
