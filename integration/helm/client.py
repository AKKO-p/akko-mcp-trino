"""The MCP client of the chart's live proof, run in a pod of the release's namespace.

MODE=service mints tokens with the test key; MODE=jwt asks the test Keycloak for the
users' tokens. Then it checks the door and a few tool calls through the release's
Service. Prints one line per check and exits 1 if any failed.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request

import anyio
import httpx
import jwt
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

MODE = os.environ.get("MODE", "service")
URL = os.environ["MCP_URL"]
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)


def minted(audience: str, sub: str = "alice") -> str:
    """service mode: a token signed with the test key."""
    now = int(time.time())
    claims = {
        "iss": os.environ["ISSUER"],
        "aud": audience,
        "sub": sub,
        "iat": now,
        "exp": now + 300,
    }
    key = open("/keys/private.pem", encoding="ascii").read()
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


def issued(user: str, client: str) -> str:
    """jwt mode: a user's token from the test Keycloak (password grant)."""
    response = httpx.post(
        os.environ["TOKEN_URL"],
        data={
            "grant_type": "password",
            "client_id": client,
            "username": user,
            "password": os.environ["INTEGRATION_PASSWORD"],
        },
        verify="/ca/ca.crt",
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def status(bearer: str | None) -> int:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(URL, data=b"{}", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


async def calls(bearer: str, user: str, nations: str) -> None:
    """As one caller: who Trino sees, a read, a write."""
    client = create_mcp_http_client(headers={"Authorization": f"Bearer {bearer}"})
    async with streamable_http_client(URL, http_client=client) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            names = sorted(t.name for t in (await session.list_tools()).tools)
            check(f"{user}: tools/list", "execute_query" in names, ", ".join(names))
            result = await session.call_tool("execute_query", {"sql": "SELECT current_user"})
            check(f"{user}: Trino sees {user}", f'"{user}"' in text(result), text(result)[:160])
            result = await session.call_tool(
                "execute_query", {"sql": "SELECT count(*) FROM tiny.nation"}
            )
            check(
                f"{user}: reads its rows of tpch.tiny.nation",
                nations in text(result),
                text(result)[:160],
            )
            result = await session.call_tool("execute_query", {"sql": "DELETE FROM tiny.nation"})
            refused = result.is_error or '"kind": "read_only"' in text(result)
            check(f"{user}: a write is refused", bool(refused), text(result)[:160])


# A NetworkPolicy may take a few seconds to admit a new pod.
for attempt in range(30):
    try:
        first = status(None)
        break
    except urllib.error.URLError:
        if attempt == 29:
            raise
        time.sleep(2)
check("no token: 401", first == 401)
if MODE == "jwt":
    check(
        "a token issued for Trino, not for this server: 401",
        status(issued("alice", "trino-cli")) == 401,
    )
    # alice reads every nation; Trino's rules give bob a row filter (regionkey = 3).
    anyio.run(calls, issued("alice", "mcp-host"), "alice", "[[25]]")
    anyio.run(calls, issued("bob", "mcp-host"), "bob", "[[5]]")
else:
    check("a token for another audience: 401", status(minted("trino")) == 401)
    anyio.run(calls, minted(os.environ["AUDIENCE"]), "mcp-reader", "[[25]]")
sys.exit(1 if failed else 0)
