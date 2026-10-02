# Examples

| File | What it shows |
|---|---|
| [`mcp.json`](mcp.json) | the host configuration for Cursor, Claude Desktop and VS Code |
| [`agent.py`](agent.py) | a complete agent on any OpenAI-compatible model (Mistral, through La Plateforme, OpenRouter or LiteLLM; or any other provider), driving the MCP tools with the user's token |

The server verifies the user's token at the door and runs every query as the
Trino identity its administrator configured: what the agent reads is what Trino
lets that identity read, never a write.
