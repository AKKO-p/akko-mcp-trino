# The base image, pinned by the digest of its multi-platform index, the same in both stages;
# Dependabot moves the tag and the digest. Its Python is the one of the locks (requirements/*.txt):
# scripts/lock.sh reads it from here, and tests/test_locks.py checks that they agree.
FROM python:3.12.16-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY akko_mcp_trino ./akko_mcp_trino
COPY providers/atlas ./providers/atlas
RUN pip install --no-cache-dir build && python -m build --wheel \
 && python -m build --wheel --outdir dist providers/atlas

FROM python:3.12.16-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
LABEL org.opencontainers.image.title="akko-mcp-trino" \
      org.opencontainers.image.description="Read-only MCP server for Trino" \
      org.opencontainers.image.source="https://github.com/AKKO-p/akko-mcp-trino" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN useradd --create-home --uid 10001 mcp
COPY requirements/image.txt /tmp/requirements/
COPY --from=build /src/dist/*.whl /tmp/
# Every dependency comes from the lock, at its exact version and with its hashes; the package is
# installed without dependencies, and pip check fails the build if one is missing from the lock.
# The Atlas provider is installed after the check, without dependencies, and is inactive: nothing
# is loaded unless MCP_CONTEXT_PROVIDERS names it. It enters the lock with the release that
# publishes both, as it requires that release of the core; its only other dependency, httpx,
# comes with the core.
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements/image.txt \
 && pip install --no-cache-dir --no-deps /tmp/akko_mcp_trino-*.whl \
 && pip check \
 && pip install --no-cache-dir --no-deps /tmp/akko_mcp_trino_atlas-*.whl \
 && rm -r /tmp/*.whl /tmp/requirements
USER mcp
EXPOSE 3000 3001
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"MCP_HEALTH_PORT\",\"3001\")}/health')"
CMD ["akko-mcp-trino"]
