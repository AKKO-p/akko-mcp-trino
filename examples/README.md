# Examples

| File | What it shows |
|---|---|
| [`mcp.json`](mcp.json) | the host configuration for Cursor and the hosts that read the same `mcpServers` format, over HTTP |
| [`cursor.json`](cursor.json), [`claude-desktop.json`](claude-desktop.json) | Cursor and Claude Desktop over HTTP with the user's token; Claude Desktop through the `mcp-remote` bridge |
| [`cursor-oauth2.json`](cursor-oauth2.json), [`claude-desktop-oauth2.json`](claude-desktop-oauth2.json) | the same hosts launching the server over stdio, signed in by Trino's own browser login (`oauth2`) |
| [`check_host_config.py`](check_host_config.py) | checks one of these files without the host: starts or reaches the server as the host would, lists the tools, calls `search_columns` |
| [`mcp-stdio.json`](mcp-stdio.json) | Claude Desktop, Cursor or Mistral Vibe launching the server over stdio (`uvx akko-mcp-trino==0.7.2`), the user's token in `MCP_USER_TOKEN` |
| [`agent.py`](agent.py) | a complete agent on any OpenAI-compatible model (Mistral, through La Plateforme, OpenRouter or LiteLLM; or any other provider), driving the MCP tools with the user's token: `search_columns` to find where the data lives, `describe_schema` to learn it, `execute_query` to read it |

[The guide](../docs/guides/connect-claude-desktop-cursor.md) walks through each of them.

The server verifies the user's token at the door (its audience must be the
server's own) and, by default, runs every query as that user, with a token for
Trino it obtains by token exchange: what the agent reads is what Trino lets that
user read, and nothing the agent sends through the server can write.
