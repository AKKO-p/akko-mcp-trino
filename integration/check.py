"""The live checks, inside the compose network: Keycloak, Trino and three servers.

Each check prints OK or FAIL; the exit code is 1 when one fails. Run by run.sh.

    python check.py
"""

import json
import os
import sys

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

KEYCLOAK = "https://keycloak:8443/realms/integration/protocol/openid-connect/token"
TRINO = "https://trino:8443/v1/statement"
CA = "/certs/ca.crt"
PASSWORD = os.environ["INTEGRATION_PASSWORD"]
JWT, IMPERSONATION, SERVICE = (
    "http://mcp-jwt:3000/mcp",
    "http://mcp-impersonation:3000/mcp",
    "http://mcp-service:3000/mcp",
)
PAGED = "http://mcp-paged:3000/mcp"  # the jwt server with pages of three rows
TPCH_TINY_TABLES = {
    "customer", "lineitem", "nation", "orders", "part", "partsupp", "region", "supplier",
}  # fmt: skip
NATIONS = "SELECT name FROM tpch.tiny.nation ORDER BY nationkey"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    RESULTS.append((name, ok, "" if ok else str(detail)[:300]))


def token(user: str, client: str) -> str:
    r = httpx.post(
        KEYCLOAK,
        data={
            "grant_type": "password",
            "client_id": client,
            "username": user,
            "password": PASSWORD,
        },
        verify=CA,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def session(url: str, bearer: str | None, extra: dict | None = None) -> dict:
    """initialize, list the tools, ask who Trino sees, read the nations, try a write."""
    headers = dict(extra or {})
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    http = create_mcp_http_client(headers=headers)
    async with streamable_http_client(url, http_client=http) as (read, write, *_):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            tools = {t.name: t.annotations for t in (await s.list_tools()).tools}

            async def run(sql: str) -> dict:
                res = await s.call_tool("execute_query", {"sql": sql})
                return json.loads(res.content[0].text)

            return {
                "instructions": init.instructions or "",
                "tools": tools,
                "who": await run("SELECT current_user"),
                "nations": await run(NATIONS),
                "write": await run("CREATE TABLE tpch.tiny.x AS SELECT 1 AS a"),
            }


def ask(url: str, bearer: str | None, extra: dict | None = None) -> dict:
    return anyio.run(session, url, bearer, extra)


async def tool_calls(url: str, bearer: str, fn) -> object:
    """Open a session as the bearer's owner and hand `fn` a `call(tool, **args) -> dict`."""
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {bearer}"})
    async with streamable_http_client(url, http_client=http) as (read, write, *_):
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def call(tool: str, **args: object) -> dict:
                res = await s.call_tool(tool, args)
                text = res.content[0].text if res.content else ""
                try:
                    return json.loads(text)
                except ValueError:  # a tool this server does not have answers in prose
                    return {"error": text}

            return await fn(call)


def use(url: str, bearer: str, fn) -> object:
    return anyio.run(tool_calls, url, bearer, fn)


async def walk(call, tool: str, key: str) -> tuple[list[str], int]:
    """A listing tool page after page, by next_offset, until it is null (at most 20 pages).

    Returns the table names it gave and the number of pages read.
    """
    names: list[str] = []
    pages, offset = 0, 0
    while offset is not None and pages < 20:
        page = await call(tool, catalog="tpch", schema="tiny", offset=offset)
        pages += 1
        names += [t if isinstance(t, str) else t[key] for t in page.get("tables", [])]
        offset = page.get("next_offset")
    return names, pages


async def discovery(call) -> dict:
    """The 0.7 tools, as one caller."""
    quote = "o'b\\c"

    async def search(text: str) -> dict:
        return await call("search_columns", text=text, catalog="tpch", schema="tiny")

    return {
        "search": await search("name"),
        "describe": await call("describe_schema", catalog="tpch", schema="tiny"),
        "wild": await search("na_e"),
        "quote": await search("o'brien\\x"),
        "words": await search("Nation, NAME"),
        "table": await search("nation"),
        "hidden_table": await search("supplier"),
        "empty": await search("zzqqzz"),
        "short": await search("ab %"),
        "quote_schema": await call("describe_schema", catalog="tpch", schema=quote),
        "column": await call("execute_query", sql="SELECT nosuch FROM tpch.tiny.nation"),
        "system": await call("execute_query", sql="SELECT * FROM system.jdbc.columns"),
        "tables": await walk(call, "list_tables", "table"),
        "described": await walk(call, "describe_schema", "table"),
    }


def trino_direct(bearer: str, user: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {bearer}"}
    if user:
        headers["X-Trino-User"] = user
    return httpx.post(TRINO, content="SELECT current_user", headers=headers, verify=CA, timeout=20)


def who(answer: dict) -> str:
    rows = answer["who"].get("rows") or [[answer["who"].get("error", "")]]
    return rows[0][0]


def main() -> int:
    alice, bob = token("alice", "mcp-host"), token("bob", "mcp-host")
    alice_for_trino = token("alice", "trino-cli")

    # 1. jwt, the default mode: each user, their own answer, through token exchange.
    a, b = ask(JWT, alice), ask(JWT, bob)
    check("jwt: Trino runs alice's query as alice", who(a) == "alice", a["who"])
    check("jwt: Trino runs bob's query as bob", who(b) == "bob", b["who"])
    check("jwt: alice reads the 25 nations", len(a["nations"].get("rows", [])) == 25, a["nations"])
    check(
        "jwt: bob reads the 5 nations his row filter leaves",
        len(b["nations"].get("rows", [])) == 5,
        b["nations"],
    )
    check(
        "jwt: a write is refused before Trino",
        "Read-only" in a["write"].get("error", ""),
        a["write"],
    )
    check(
        "jwt: execute_query is annotated read-only, not destructive",
        a["tools"]["execute_query"].read_only_hint is True
        and a["tools"]["execute_query"].destructive_hint is False,
        a["tools"]["execute_query"],
    )
    check("jwt: no warning in the instructions", a["instructions"] == "", a["instructions"])

    # 1b. the 0.7 tools, against the real tpch and the real policy.
    d_alice, d_bob = use(JWT, alice, discovery), use(JWT, bob, discovery)
    found = {(m["table"], m["column"]) for m in d_alice["search"].get("matches", [])}
    check(
        "discovery: alice finds tpch.tiny.nation.name with search_columns",
        ("nation", "name") in found and d_alice["search"].get("complete") is True,
        d_alice["search"],
    )
    check(
        "discovery: bob's search finds columns of nation only",
        {m["table"] for m in d_bob["search"].get("matches", [])} == {"nation"}
        and ("nation", "name") in {(m["table"], m["column"]) for m in d_bob["search"]["matches"]},
        d_bob["search"],
    )
    check(
        "discovery: describe_schema shows bob the table nation and no other",
        {t["table"] for t in d_bob["describe"].get("tables", [])} == {"nation"},
        d_bob["describe"],
    )
    check(
        "discovery: describe_schema shows alice the eight tables of tiny",
        {t["table"] for t in d_alice["describe"].get("tables", [])} == TPCH_TINY_TABLES
        and d_alice["describe"].get("next_offset") is None,
        d_alice["describe"],
    )
    check(
        "discovery: a search for na_e matches no column (the _ is literal)",
        d_alice["wild"].get("matches") == [] and "error" not in d_alice["wild"],
        d_alice["wild"],
    )
    check(
        "discovery: a text with a quote and a backslash answers without error, no match",
        d_alice["quote"].get("matches") == [] and "error" not in d_alice["quote"],
        d_alice["quote"],
    )
    check(
        "discovery: two words match a column by its name and its table's name",
        [(m["table"], m["column"]) for m in d_alice["words"].get("matches", [])]
        == [("nation", "name")]
        and d_alice["words"].get("complete") is True,
        d_alice["words"],
    )
    check(
        "discovery: a search lists the table whose name holds the word, for both users",
        [t["table"] for t in d_alice["table"].get("tables", [])] == ["nation"]
        and [t["table"] for t in d_bob["table"].get("tables", [])] == ["nation"],
        (d_alice["table"], d_bob["table"]),
    )
    check(
        "discovery: a table bob may not see is listed for alice only",
        [t["table"] for t in d_alice["hidden_table"].get("tables", [])] == ["supplier"]
        and d_bob["hidden_table"].get("tables") == []
        and d_bob["hidden_table"].get("matches") == [],
        (d_alice["hidden_table"], d_bob["hidden_table"]),
    )
    check(
        "discovery: a complete search with no result gives the hint",
        d_alice["empty"].get("complete") is True
        and d_alice["empty"].get("matches") == []
        and d_alice["empty"].get("tables") == []
        and "list_tables" in d_alice["empty"].get("hint", ""),
        d_alice["empty"],
    )
    check(
        "discovery: a text with no word long enough is refused and says so",
        d_alice["short"].get("kind") == "invalid_argument"
        and "no word" in d_alice["short"].get("error", ""),
        d_alice["short"],
    )
    check(
        "discovery: a schema name with a quote and a backslash is refused before SQL",
        d_alice["quote_schema"].get("kind") == "invalid_argument",
        d_alice["quote_schema"],
    )
    check(
        "discovery: an unknown column is explained (kind column, its identifier)",
        d_alice["column"].get("kind") == "column"
        and d_alice["column"].get("identifier") == "nosuch",
        d_alice["column"],
    )
    check(
        "discovery: the agent's own SQL on system.jdbc.columns stays refused (read_only)",
        d_alice["system"].get("kind") == "read_only",
        d_alice["system"],
    )
    paged = use(PAGED, alice, discovery)
    check(
        "discovery: list_tables walked by offset gives the eight tables in three pages, no duplicate",
        sorted(paged["tables"][0]) == sorted(TPCH_TINY_TABLES) and paged["tables"][1] == 3,
        paged["tables"],
    )
    check(
        "discovery: describe_schema walked by offset gives the eight tables in three pages, no duplicate",
        sorted(paged["described"][0]) == sorted(TPCH_TINY_TABLES)
        and paged["described"][1] == 3,
        paged["described"],
    )

    # 2. The door: no token, a forged one, one issued for Trino.
    no_token = httpx.post(JWT, json={}, timeout=10)
    check("door: no token, 401", no_token.status_code == 401, no_token.status_code)
    check(
        "door: the 401 says what to send",
        "requires a bearer token" in no_token.json().get("message", ""),
        no_token.text,
    )
    check(
        "door: the 401 points to the protected resource metadata",
        'resource_metadata="http://mcp-jwt:3000/.well-known/oauth-protected-resource/mcp"'
        in no_token.headers.get("www-authenticate", ""),
        no_token.headers,
    )
    meta = httpx.get("http://mcp-jwt:3000/.well-known/oauth-protected-resource/mcp", timeout=10)
    check(
        "door: the metadata names the server and its identity provider",
        meta.json().get("resource") == "http://mcp-jwt:3000/mcp"
        and meta.json().get("authorization_servers")
        == ["https://keycloak:8443/realms/integration"],
        meta.text,
    )
    forged = alice.rsplit(".", 1)[0] + ".Zm9yZ2VkLXNpZ25hdHVyZQ"
    r = httpx.post(JWT, json={}, headers={"Authorization": f"Bearer {forged}"}, timeout=10)
    check("door: a forged signature, 401", r.status_code == 401, r.status_code)
    r = httpx.post(JWT, json={}, headers={"Authorization": f"Bearer {alice_for_trino}"}, timeout=10)
    check("door: a token issued for Trino, 401 at the MCP server", r.status_code == 401, r.text)
    direct = trino_direct(alice_for_trino)
    check(
        "control: Trino itself accepts that token (refused for its audience, not as invalid)",
        direct.status_code == 200,
        direct.status_code,
    )
    passthrough = trino_direct(alice)
    check(
        "control: Trino refuses the token issued for the MCP server (no passthrough possible)",
        passthrough.status_code == 401,
        passthrough.status_code,
    )

    # 3. impersonation: the server's certificate, the caller's name from their token.
    a, b = ask(IMPERSONATION, alice), ask(IMPERSONATION, bob)
    check("impersonation: Trino runs alice's query as alice", who(a) == "alice", a["who"])
    check("impersonation: bob reads 5 nations", len(b["nations"].get("rows", [])) == 5, b)
    forged_header = ask(IMPERSONATION, alice, {"X-Trino-User": "bob"})
    check(
        "impersonation: a header naming another user changes nothing",
        who(forged_header) == "alice",
        forged_header["who"],
    )
    check(
        "impersonation: a write is refused before Trino",
        "Read-only" in a["write"].get("error", ""),
        a["write"],
    )
    r = trino_direct(alice_for_trino, user="bob")
    denied = (
        httpx.get(
            r.json()["nextUri"],
            headers={"Authorization": f"Bearer {alice_for_trino}"},
            verify=CA,
            timeout=20,
        )
        if r.status_code == 200 and "nextUri" in r.json()
        else r
    )
    check(
        "control: only the server's account may impersonate (alice cannot act as bob)",
        "cannot impersonate" in (r.text + denied.text),
        r.text,
    )
    r = httpx.post(IMPERSONATION, json={}, timeout=10)
    check("impersonation: no token, 401", r.status_code == 401, r.status_code)

    # 4. service mode with an open door, chosen explicitly, says so.
    s = ask(SERVICE, None)
    check("service: runs as its configured account", who(s) == "mcp-service", s["who"])
    check(
        "service: the initialization warns every host",
        "TRINO_IDENTITY_MODE=service" in s["instructions"]
        and "MCP_AUTH_ENABLED=false" in s["instructions"],
        s["instructions"],
    )

    for name, ok, detail in RESULTS:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
