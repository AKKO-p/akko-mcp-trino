"""A minimal agent: any OpenAI-compatible model, the MCP tools, the user's identity.

Works unchanged with Mistral (La Plateforme, or through OpenRouter or LiteLLM),
and with any other provider that speaks the OpenAI chat-completions API with
tool calling. The model never sees a token; the MCP server receives the user's
JWT on every tool call, and Trino answers as that user.

    export LLM_BASE_URL=https://api.mistral.ai/v1
    export LLM_API_KEY=...
    export LLM_MODEL=mistral-small-latest
    export MCP_URL=http://localhost:3000/sse
    export USER_TOKEN=<the user's access token>
    export AGENT_KEY=<optional X-Agent-Key>
    python examples/agent.py "Give me three customer e-mails and their country"

Dependencies: pip install akko-mcp-trino openai
"""

from __future__ import annotations

import json
import os
import sys

import anyio
from mcp import ClientSession
from mcp.client.sse import sse_client
from openai import OpenAI

SYSTEM = (
    "You are a data assistant. Use the tools to answer from Trino. "
    "Explore with list_catalogs, list_schemas, list_tables and describe_table "
    "before writing SQL. Values may come back masked (for example ***@domain) or "
    "rows may be missing: that is the data governance policy applied to the current "
    "user, not an error. Report what the tools return, verbatim, and never repeat "
    "a query that already succeeded. Answer with the data you retrieved, nothing else."
)


def _openai_tools(mcp_tools) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema,
            },
        }
        for t in mcp_tools
    ]


async def run(question: str) -> str:
    llm = OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"])
    model = os.environ.get("LLM_MODEL", "mistral-small-latest")
    headers = {"Authorization": f"Bearer {os.environ['USER_TOKEN']}"}
    if os.environ.get("AGENT_KEY"):
        headers["X-Agent-Key"] = os.environ["AGENT_KEY"]

    async with sse_client(
        os.environ.get("MCP_URL", "http://localhost:3000/sse"), headers=headers
    ) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = _openai_tools((await session.list_tools()).tools)
            messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": question},
            ]
            for _ in range(12):  # bounded: an agent that loops forever is a bug, not a feature
                reply = llm.chat.completions.create(model=model, messages=messages, tools=tools)
                msg = reply.choices[0].message
                if not msg.tool_calls:
                    return msg.content or ""
                messages.append(msg)
                for call in msg.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    result = await session.call_tool(call.function.name, args)
                    print(f"  → {call.function.name}({args})", file=sys.stderr)
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": result.content[0].text}
                    )
            return "The agent did not converge within the step budget."


if __name__ == "__main__":
    print(anyio.run(run, " ".join(sys.argv[1:]) or "Which catalogs can I see?"))
