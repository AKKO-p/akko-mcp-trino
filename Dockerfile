# The base image, pinned by the digest of its multi-platform index, the same in both stages;
# Dependabot moves the tag and the digest. Its Python is the one of the locks (requirements/*.txt):
# scripts/lock.sh reads it from here, and tests/test_locks.py checks that they agree.
FROM python:3.12.16-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY akko_mcp_trino ./akko_mcp_trino
COPY providers/atlas ./providers/atlas
COPY requirements/build.txt requirements/kerberos.txt ./requirements/
# The build tools come from their lock, wheels only, with their hashes, and build without
# isolation, so that no build backend is fetched unlocked.
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements/build.txt \
 && python -m build --wheel --no-isolation \
 && python -m build --wheel --no-isolation --outdir dist providers/atlas
# The kerberos extra of the Atlas provider, as wheels, from its lock and with its hashes: gssapi and
# krb5 publish no Linux wheel and are built here from their sdists, by the locked Cython and
# without isolation, against the system's MIT Kerberos; every other wheel is the lock's own. The
# final stage reads them through a mount, so none of this stage enters the image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev libkrb5-dev \
 && pip wheel --no-cache-dir --no-deps --no-build-isolation --require-hashes \
      -r requirements/kerberos.txt -w /src/kerberos

FROM python:3.12.16-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
LABEL org.opencontainers.image.title="akko-mcp-trino" \
      org.opencontainers.image.description="Read-only MCP server for Trino" \
      org.opencontainers.image.source="https://github.com/AKKO-p/akko-mcp-trino" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN useradd --create-home --uid 10001 mcp
# The MIT Kerberos libraries that gssapi and krb5 load, for ATLAS_AUTH=kerberos.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgssapi-krb5-2 \
 && rm -rf /var/lib/apt/lists/*
COPY requirements/image.txt /tmp/requirements/
COPY --from=build /src/dist/*.whl /tmp/
# Every dependency comes from the lock, wheels only, at its exact version and with its hashes; the
# core and the Atlas provider are installed without dependencies. The kerberos extra comes from
# the wheels of the build stage only (--no-index), checked there against its lock. pip check fails
# the build if the lock misses a dependency that one of them or a locked package declares (not
# one an extra brings, such as the CLI of mcp[cli]). The Atlas provider is inactive: nothing is
# loaded unless MCP_CONTEXT_PROVIDERS names it.
RUN --mount=type=bind,from=build,source=/src/kerberos,target=/tmp/kerberos \
    pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements/image.txt \
 && pip install --no-cache-dir --no-deps /tmp/akko_mcp_trino-*.whl /tmp/akko_mcp_trino_atlas-*.whl \
 && pip install --no-cache-dir --no-index --find-links /tmp/kerberos "akko-mcp-trino-atlas[kerberos]" \
 && pip check \
 && rm -r /tmp/*.whl /tmp/requirements
USER mcp
EXPOSE 3000 3001
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"MCP_HEALTH_PORT\",\"3001\")}/health')"
CMD ["akko-mcp-trino"]
