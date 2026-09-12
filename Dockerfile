FROM python:3.14-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY akko_mcp_trino ./akko_mcp_trino
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.14-slim
LABEL org.opencontainers.image.title="akko-mcp-trino" \
      org.opencontainers.image.description="Governed MCP server for Trino" \
      org.opencontainers.image.source="https://github.com/AKKO-p/akko-mcp-trino" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN useradd --create-home --uid 10001 mcp
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER mcp
EXPOSE 3000 3001
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"MCP_HEALTH_PORT\",\"3001\")}/health')"
CMD ["akko-mcp-trino"]
