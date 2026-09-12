#!/usr/bin/env bash
# Keeps the package product-neutral: no import of a product layer, no vendor
# or product value hardcoded. A comment explaining the boundary is fine; only
# real couplings are refused. Runs in CI on every push.
set -euo pipefail
cd "$(dirname "$0")"
fail=0

# 1. No import of a product layer.
if grep -rnE --include=*.py --exclude-dir=__pycache__ "^\s*(from|import)\s+akko(\.|\s|$)" akko_mcp_trino/ 2>/dev/null; then
  echo "FAIL: akko_mcp_trino/ imports a product layer; the package must stand alone." >&2
  fail=1
fi

# 2. No product or vendor value hardcoded (hosts, catalogs, model runtimes).
FORBIDDEN='akko-trino|akko_ai|akko-ai\.com|harbor\.akko|iceberg|ollama|mcp-trino'
if grep -rniE --include=*.py --exclude-dir=__pycache__ "$FORBIDDEN" akko_mcp_trino/ 2>/dev/null; then
  echo "FAIL: akko_mcp_trino/ contains a hardcoded product or vendor value; inject it through the environment." >&2
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "OK: akko_mcp_trino/ is product-neutral."
fi
exit "$fail"
