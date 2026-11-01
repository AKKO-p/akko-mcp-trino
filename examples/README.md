# Examples

| File | What it shows |
|---|---|
| [`mcp.json`](mcp.json) | the host configuration for Cursor, Claude Desktop and VS Code, over HTTP |
| [`mcp-stdio.json`](mcp-stdio.json) | the same hosts launching the server themselves over stdio (`uvx akko-mcp-trino`), the user's token in `MCP_USER_TOKEN` |
| [`agent.py`](agent.py) | a complete agent on any OpenAI-compatible model (Mistral, through La Plateforme, OpenRouter or LiteLLM; or any other provider), driving the MCP tools with the user's token: `search_columns` to find where the data lives, `describe_schema` to learn it, `execute_query` to read it |

The server verifies the user's token at the door (its audience must be the
server's own) and, by default, runs every query as that user, with a token for
Trino it obtains by token exchange: what the agent reads is what Trino lets that
user read, and nothing the agent sends through the server can write.
