"""The live checks of the ldap scenario, inside the compose network.

Trino authenticates by password against the directory; the server runs in impersonation
mode with its own directory account; the callers' tokens come from Keycloak, federated on
the same directory. Each check prints OK or FAIL; the exit code is 1 when one fails. Run
by run.sh, and in a pod by integration/helm/openshift-ldap.sh.
"""

import json
import os
import sys
import time

import anyio
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

CA = "/certs/ca.crt"
ISSUER = "https://keycloak:8443/realms/ldap"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
# The server: its compose service, or its Service in a cluster (integration/helm/openshift-ldap.sh).
MCP = os.environ.get("MCP_URL", "http://mcp:3000/mcp")
TRINO = "https://trino:8443/v1/statement"
NATIONS = "SELECT name FROM tpch.tiny.nation ORDER BY nationkey"
PASSWORDS = {
    "mcp-server": os.environ["MCP_SERVER_PASSWORD"],
    "alice": os.environ["ALICE_PASSWORD"],
    "bob": os.environ["BOB_PASSWORD"],
}
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: object = "", shown: bool = False) -> None:
    """Records a check; its detail is printed when it fails, or always when ``shown``."""
    RESULTS.append((name, ok, str(detail)[:400] if shown or not ok else ""))


def token(user: str, password: str | None = None) -> httpx.Response:
    """The user's token from Keycloak (password grant): Keycloak checks the password by a
    bind to the directory."""
    return httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "mcp-host",
            "username": user,
            "password": PASSWORDS[user] if password is None else password,
        },
        verify=CA,
        timeout=20,
    )


def forged(user: str) -> str:
    """A token with the right issuer, audience, user and key id (Keycloak's own signing key, read
    from its JWKS), signed by a key Keycloak never had: refused for its signature alone."""
    jwks = httpx.get(f"{ISSUER}/protocol/openid-connect/certs", verify=CA, timeout=20).json()
    kid = next(k["kid"] for k in jwks["keys"] if k.get("use") == "sig" and k.get("alg") == "RS256")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "mcp-trino",
        "preferred_username": user,
        "sub": user,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


async def session(bearer: str, extra: dict | None = None) -> dict:
    """As the bearer's owner: who Trino sees, the nations, the hidden column, a table out of
    reach, a write, and a change of the session's user."""
    headers = {"Authorization": f"Bearer {bearer}", **(extra or {})}
    http = create_mcp_http_client(headers=headers)
    async with streamable_http_client(MCP, http_client=http) as (read, write, *_):
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def run(sql: str) -> dict:
                res = await s.call_tool("execute_query", {"sql": sql})
                return json.loads(res.content[0].text)

            return {
                "who": await run("SELECT current_user"),
                "nations": await run(NATIONS),
                "comment": await run("SELECT comment FROM tpch.tiny.nation"),
                "orders": await run("SELECT count(*) FROM tpch.tiny.orders"),
                "write": await run("CREATE TABLE tpch.tiny.x AS SELECT 1 AS a"),
                "insert": await run("INSERT INTO tpch.tiny.nation VALUES (99, 'X', 0, 'x')"),
                "authorization": await run("SET SESSION AUTHORIZATION 'bob'"),
            }


def ask(bearer: str, extra: dict | None = None) -> dict:
    return anyio.run(session, bearer, extra)


def who(answer: dict) -> str:
    rows = answer["who"].get("rows") or [[answer["who"].get("error", "")]]
    return rows[0][0]


def trino(user: str, sql: str, as_user: str | None = None, password: str | None = None):
    """Trino directly, as ``user`` with their directory password, possibly asking to run as
    ``as_user``: (HTTP status, rows or the error message)."""
    auth = (user, PASSWORDS[user] if password is None else password)
    headers = {"X-Trino-User": as_user} if as_user else {}
    r = httpx.post(TRINO, content=sql, headers=headers, auth=auth, verify=CA, timeout=20)
    if r.status_code != 200:
        return r.status_code, r.text.strip()[:200]
    body, rows = r.json(), []
    while True:
        rows += body.get("data") or []
        if "error" in body:
            return 200, body["error"].get("message", "")
        if "nextUri" not in body:
            return 200, rows
        body = httpx.get(body["nextUri"], auth=auth, verify=CA, timeout=20).json()


def main() -> int:
    # 1. The door: a verified token, or 401.
    def door(bearer: str | None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        return httpx.post(MCP, json={}, headers=headers, timeout=20)

    bare = door(None)
    check(
        "without a token, the server answers 401 and says what to send",
        bare.status_code == 401 and "requires a bearer token" in bare.json().get("message", ""),
        (bare.status_code, bare.text),
    )
    for what, bearer in (
        ("with the identity provider's key id, signed by another key", forged("bob")),
        ("that is not a token", "not-a-token"),
    ):
        r = door(bearer)
        check(
            f"a token {what} is refused with 401",
            r.status_code == 401 and r.headers.get("x-reason") == "unauthenticated",
            (r.status_code, r.text),
            shown=True,
        )

    # 2. The identity provider checks the password against the directory.
    wrong = token("alice", "not-" + PASSWORDS["alice"])
    check(
        "Keycloak refuses alice with a wrong password (checked by the directory)",
        wrong.status_code in (400, 401) and "invalid_grant" in wrong.text,
        (wrong.status_code, wrong.text[:200]),
    )
    tokens = {}
    for user in ("alice", "bob"):
        r = token(user)
        check(
            f"Keycloak issues {user}'s token, the password checked by the directory",
            r.status_code == 200,
            (r.status_code, r.text[:200]),
        )
        tokens[user] = r.json().get("access_token", "")
    claims = jwt.decode(tokens["alice"], options={"verify_signature": False})
    check(
        "alice's token names her by her directory uid, for the server's audience",
        claims.get("preferred_username") == "alice" and "mcp-trino" in claims.get("aud", []),
        {k: claims.get(k) for k in ("preferred_username", "aud", "iss")},
    )

    # 3. Through the server: each caller as Trino sees them, with their own rights.
    a, b = ask(tokens["alice"]), ask(tokens["bob"])
    check("Trino runs alice's query as alice (SELECT current_user)", who(a) == "alice", a["who"])
    check("Trino runs bob's query as bob (SELECT current_user)", who(b) == "bob", b["who"])
    check("alice reads the 25 nations", len(a["nations"].get("rows", [])) == 25, a["nations"])
    check(
        "bob reads the 5 nations his row filter leaves",
        len(b["nations"].get("rows", [])) == 5,
        b["nations"],
    )
    check(
        "alice reads the column comment; Trino refuses it to bob",
        len(a["comment"].get("rows", [])) == 25 and b["comment"].get("kind") == "permission",
        (a["comment"].get("kind"), b["comment"]),
    )
    check(
        "alice counts the orders; Trino refuses them to bob",
        a["orders"].get("rows") == [[15000]] and b["orders"].get("kind") == "permission",
        (a["orders"], b["orders"]),
    )

    # 4. No caller becomes another.
    spoof = ask(tokens["alice"], {"X-Trino-User": "bob", "X-Forwarded-User": "bob"})
    check(
        "alice's token with X-Trino-User: bob sent to the server: Trino still sees alice",
        who(spoof) == "alice" and len(spoof["nations"].get("rows", [])) == 25,
        spoof["who"],
    )
    for name, answer in (("alice", a), ("bob", b)):
        check(
            f"SET SESSION AUTHORIZATION is refused to {name} before Trino",
            answer["authorization"].get("kind") == "read_only"
            and answer["authorization"].get("error", "").startswith("Read-only mode"),
            answer["authorization"],
        )

    # 5. Nothing is written.
    for name, answer in (("alice", a), ("bob", b)):
        check(
            f"a CREATE TABLE and an INSERT by {name} are refused before Trino",
            "Read-only" in answer["write"].get("error", "")
            and "Read-only" in answer["insert"].get("error", ""),
            (answer["write"], answer["insert"]),
        )

    # 6. Impersonation in Trino: the server's account only, for the listed users only.
    status, rows = trino("mcp-server", "SELECT current_user", as_user="alice")
    check(
        "the server's account may run a query as alice (the impersonation rule)",
        status == 200 and rows == [["alice"]],
        (status, rows),
    )
    status, rows = trino("mcp-server", "SELECT current_user", as_user="carol")
    check(
        "the server's account may not run a query as a user the rule does not list",
        rows == "Access Denied: User mcp-server cannot impersonate user carol",
        (status, rows),
        shown=True,
    )
    status, rows = trino("mcp-server", NATIONS)
    check(
        "the server's account alone reads no data",
        rows == "Access Denied: Cannot select from table tpch.tiny.nation",
        (status, rows),
        shown=True,
    )
    for user, target in (("alice", "bob"), ("bob", "alice"), ("bob", "mcp-server")):
        status, rows = trino(user, "SELECT current_user", as_user=target)
        check(
            f"{user}, signed in to Trino with a directory password, "
            f"may not run a query as {target}",
            rows == f"Access Denied: User {user} cannot impersonate user {target}",
            (status, rows),
            shown=True,
        )
    status, rows = trino("mcp-server", "SELECT 1", password="not-the-password")
    check("Trino refuses the server's account with a wrong password", status == 401, (status, rows))

    # 7. Trino's HTTP port: closed in this scenario (http-server.http.enabled=false). Open, Trino
    # refuses an unauthenticated statement over HTTP (403) unless allow-insecure-over-http is set.
    try:
        r = httpx.post(
            "http://trino:8080/v1/statement",
            content="SELECT current_user",
            headers={"X-Trino-User": "alice"},
            timeout=10,
        )
        outcome, refused = f"{r.status_code} {r.text[:100]}", r.status_code in (401, 403)
    except httpx.ConnectError as exc:
        outcome, refused = f"no listener: {exc}", True
    check(
        "Trino's HTTP port runs nothing as alice without her password",
        refused,
        outcome,
        shown=True,
    )

    for name, ok, detail in RESULTS:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
    failed = sum(not ok for _, ok, _ in RESULTS)
    print(f"  {len(RESULTS) - failed} of {len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
