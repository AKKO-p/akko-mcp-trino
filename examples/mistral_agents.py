"""A Mistral agent that reads Trino through the MCP tools, as the user whose token it holds.

The MCP client runs here, in this process, with the official mistralai SDK: it
reaches the server with the user's token in a header, lists the tools and runs
each tool call the model asks for. Mistral's Conversations API receives the
question, the tool descriptions and the tool results; it never receives the
token, and it never reaches the server.

    pip install "mistralai[agents]==3.7.0"
    export MISTRAL_API_KEY=...
    export MCP_URL=http://localhost:3000/mcp
    export USER_TOKEN=<the user's access token>
    python examples/mistral_agents.py "Which customers do we have, with their e-mail and country?"

MISTRAL_MODEL chooses the model (default mistral-medium-latest).
"""

from __future__ import annotations

import asyncio
import os
import sys

from mistralai.client import Mistral
from mistralai.client.models import FunctionCallEntry, FunctionResultEntry
from mistralai.extra.mcp.streamable_http import (
    MCPClientStreamableHTTP,
    StreamableHTTPServerParams,
)
from mistralai.extra.run.context import RunContext


async def main(question: str) -> None:
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    server = MCPClientStreamableHTTP(
        StreamableHTTPServerParams(
            url=os.environ.get("MCP_URL", "http://localhost:3000/mcp"),
            headers={"Authorization": f"Bearer {os.environ['USER_TOKEN']}"},
        )
    )
    model = os.environ.get("MISTRAL_MODEL", "mistral-medium-latest")
    async with RunContext(model=model) as run_ctx:
        await run_ctx.register_mcp_client(server)
        result = await client.beta.conversations.run_async(run_ctx=run_ctx, inputs=question)
    for entry in result.output_entries:
        if isinstance(entry, FunctionCallEntry):
            print(f"tool call: {entry.name} {entry.arguments}")
        elif isinstance(entry, FunctionResultEntry):
            print(f"tool result: {entry.result[:300]}")
    print(result.output_as_text)


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "Which catalogs can I see?"))
