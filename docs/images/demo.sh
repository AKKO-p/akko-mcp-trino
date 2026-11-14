#!/usr/bin/env bash
# Runs demo.py against a running akko-sandbox, in the akko-mcp-trino image (the official MCP
# SDK is in it). SANDBOX is the sandbox's clone, its compose project the default akko-sandbox
# unless SANDBOX_PROJECT says otherwise.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
sandbox="${SANDBOX:?the path of an akko-sandbox clone, started with docker compose up -d --wait}"
exec docker run --rm -t --memory 256m \
  --network "${SANDBOX_PROJECT:-akko-sandbox}_default" \
  -e KEYCLOAK_URL=http://keycloak:8080 -e MCP_URL=http://mcp:3000/mcp \
  -v "$sandbox/demo:/sandbox/demo:ro" -v "$here/demo.py:/demo.py:ro" \
  --entrypoint python "${MCP_IMAGE:-akkoai/akko-mcp-trino:0.8.1}" /demo.py
