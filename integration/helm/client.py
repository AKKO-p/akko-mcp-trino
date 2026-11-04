"""The MCP client of the chart's live proof, run in a pod of the release's namespace.

Mints tokens with the test key, then checks the door and a few tool calls through the
release's Service. Prints one line per check and exits 1 if any failed.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request

import anyio
import jwt
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

URL = os.environ["MCP_URL"]
ISSUER = os.environ["ISSUER"]
AUDIENCE = os.environ["AUDIENCE"]
KEY = open("/keys/private.pem", encoding="ascii").read()
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)


def token(audience: str, sub: str = "alice") -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": audience, "sub": sub, "iat": now, "exp": now + 300}
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test"})


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


async def session_checks() -> None:
    headers = {"Authorization": f"Bearer {token(AUDIENCE)}"}
    client = create_mcp_http_client(headers=headers)
    async with streamable_http_client(URL, http_client=client) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            names = sorted(t.name for t in (await session.list_tools()).tools)
            check("tools/list", "execute_query" in names, ", ".join(names))
            result = await session.call_tool(
                "execute_query", {"sql": "SELECT name FROM tiny.nation ORDER BY name LIMIT 3"}
            )
            check("execute_query reads tpch", not result.is_error and "ALGERIA" in text(result), text(result)[:160])
            result = await session.call_tool("execute_query", {"sql": "SELECT current_user"})
            check("the query runs as the service account", "mcp-reader" in text(result), text(result)[:160])
            result = await session.call_tool("execute_query", {"sql": "DELETE FROM tiny.nation"})
            refused = result.is_error or '"kind": "read_only"' in text(result)
            check("a write is refused", bool(refused), text(result)[:160])


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
check("a token for another audience: 401", status(token("trino")) == 401)
anyio.run(session_checks)
sys.exit(1 if failed else 0)
