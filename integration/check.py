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
