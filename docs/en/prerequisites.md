# Prerequisites

**English** · [Français](../fr/prerequisites.md)

## What you need

| You need | Why | Notes |
|---|---|---|
| Python 3.12 or 3.13 | to run the server | `pip` and a virtual environment; the container image needs neither |
| A Trino coordinator the server can reach | the query engine | Trino 351 or later; https for `jwt`, `impersonation` and `oauth2`, and whenever a secret is sent |
| An OIDC identity provider (IdP) with a JWKS endpoint | to verify the caller's token | Keycloak, Entra ID, Okta, Dex and others. Tokens must carry `iss`, an `aud` that names this server, `exp`, and the claim that names the user (`sub` by default, `MCP_JWT_USER_CLAIM`) |
| `jwt`: token exchange at the IdP, and Trino's JWT authenticator | each user reads with their own rights | a confidential client for this server, allowed to exchange a token issued for it for one whose audience is Trino; `http-server.authentication.type=JWT` with the IdP's JWKS and `required-audience` set to that audience |
| Or `impersonation`: an account for this server, and an impersonation rule | each user reads with their own rights, without token exchange | `TRINO_USER` with `TRINO_PASSWORD` or a client certificate; a Trino rule that lets this account, and only this account, impersonate users |
| Or `service`: one Trino identity | every caller gets the rights of this identity | `TRINO_USER`, with `TRINO_PASSWORD` or a client certificate, or no credential against a local coordinator; give it read rights only on data every caller may read |
| A policy engine that decides for Trino | access control | OPA (`opa.policy.uri`), Ranger, or Trino's file-based rules |
| An MCP host | the client | Cursor, Claude Desktop, VS Code, or any client built on the `mcp` SDK |

Optional: a Prometheus server to scrape `/metrics`.

## Compatibility

| | Supported | Tested |
|---|---|---|
| Python | 3.12, 3.13 | 3.12, 3.13 (CI) |
| Trino | 351 and later; https with the JWT authenticator for `jwt`, with a password or certificate authenticator and impersonation rules for `impersonation`, with OAuth2 for `oauth2` (stdio); http (local) or https for `service` | a recent release with the JWT and certificate authenticators, behind OPA and behind file-based rules, with row filters and column masks |
| Policy engines | OPA (`trino-opa`), Ranger (Trino plugin), Trino file-based access control | OPA and file-based rules, with row filters and column masks |
| Identity providers | any OIDC provider that publishes a JWKS; OAuth 2.0 Token Exchange, RFC 8693 (`jwt` mode) | Keycloak, standard token exchange |
| MCP | protocol negotiated by the official `mcp` SDK 2.2 (initialize handshake revisions 2024-11-05 to 2025-11-25, and the stateless 2026-07-28 revision); transports `streamable-http`, `sse` and `stdio` | all three transports, official SDK client at its default revision, in CI |
| MCP hosts | remote: any host that sends a bearer header; local: any stdio host | the Python SDK, the example agent |
| Models | any: the server never talks to a model | not tested in CI; the sandbox's [agent reports](https://github.com/AKKO-p/akko-sandbox/tree/main/agent/reports) measure models that drive the tools |

The images the integration suite runs, Keycloak and Trino included, are pinned in
[`integration/compose.yaml`](../../integration/compose.yaml).

## Network

Over the HTTP transports (`streamable-http`, `sse`), the server listens on two ports: `3000` for
MCP and `3001` for health and metrics (`MCP_PORT`, `MCP_HEALTH_PORT`). Over `stdio`, it opens no
port and serves no health endpoint. The server reaches the IdP's JWKS endpoint, the IdP's token
endpoint in `jwt` mode (https only), and Trino. It never sends a password, a token or a client
certificate over plain http.

## Kubernetes and OpenShift

These are optional. A Helm chart ships in
[`charts/akko-mcp-trino/`](../../charts/akko-mcp-trino/), and it also runs under OpenShift's
arbitrary UID. See [Deployment](deployment.md#kubernetes-and-openshift).
