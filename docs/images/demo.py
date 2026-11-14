"""The animation at the top of the READMEs: one question, two users, two answers.

It runs against akko-sandbox (https://github.com/AKKO-p/akko-sandbox) and reuses its client,
demo/demo.py: each user gets a token from the sandbox's Keycloak, the official MCP client sends
it to the sandbox's akko-mcp-trino, and Trino runs the query as that user. Only the two answers
are printed. demo.tape records it.
"""

import json
import sys

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

sys.path.insert(0, "/sandbox/demo")
from demo import MCP_URL, QUESTION, paint, token  # noqa: E402  the sandbox's own client

USERS = [
    ("Alice", "alice_admin", "32", "nothing hidden"),
    ("Carol", "carol_analyst", "33", "Europe only, e-mails masked"),
]


async def ask(user: str) -> tuple[str, list]:
    """Who Trino sees, and the answer to the question, for one user, through MCP."""
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token(user)}"})
    async with streamable_http_client(MCP_URL, http_client=http) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def run(sql: str) -> dict:
                result = await session.call_tool("execute_query", {"sql": sql})
                return json.loads(result.content[0].text)

            who = (await run("SELECT current_user"))["rows"][0][0]
            return who, (await run(QUESTION))["rows"]


def main() -> None:
    print(paint("1", "Alice and Carol ask the same question through the same MCP server"))
    print("  " + paint("36", QUESTION))
    for label, user, color, note in USERS:
        who, rows = anyio.run(ask, user)
        print()
        head = f"Trino runs it as {paint('1', who)}: {len(rows)} rows, {note}"
        print(f"{paint('1;' + color, label)}  {head}")
        for name, email, country in rows:
            print(f"  {name:<15} {paint(color, f'{email:<26}')} {country}")
    print()
    print(paint("1", "Same server, same question: Trino applied each user's rules."))


if __name__ == "__main__":
    main()
