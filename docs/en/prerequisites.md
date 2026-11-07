# Prerequisites

**English** · [Français](../fr/prerequisites.md)

## What you need

| You need | Why | Notes |
|---|---|---|
| Python 3.12 or later | runtime | `pip` and a virtual environment |
| A reachable Trino coordinator | the engine | any recent version; https for `jwt` and `impersonation`, and whenever a secret is sent |
| An OIDC provider with a JWKS endpoint | the door | Keycloak, Entra ID, Okta, Dex… Tokens must carry `iss`, an `aud` naming this server, `exp`, and the claim that names the user (`sub` by default, `MCP_JWT_USER_CLAIM`) |
| `jwt`: token exchange at the provider, and Trino's JWT authenticator | each user reads with their own rights | a confidential client for this server, allowed to exchange a token issued for it for one whose audience is Trino; `http-server.authentication.type=JWT` with the provider's JWKS and `required-audience` set to that audience |
| Or `impersonation`: an account for this server, and an impersonation rule | each user reads with their own rights, without token exchange | `TRINO_USER` with `TRINO_PASSWORD` or a client certificate; a Trino rule letting that account, and only it, impersonate users |
| Or `service`: one Trino identity | the rights of every caller | `TRINO_USER`, and `TRINO_PASSWORD` or a client certificate; give it read rights only on what every caller may read |
| A policy engine deciding for Trino | the governance | OPA (`opa.policy.uri`), Ranger, or Trino file-based rules |
| An MCP host | the client | Cursor, Claude Desktop, VS Code, or any client built on the `mcp` SDK |

Optional: a Prometheus to scrape `/metrics`.

## Compatibility

| | Supported | Tested |
|---|---|---|
| Python | 3.12, 3.13 | 3.12, 3.13 (CI) |
| Trino | 351 and later; https with the JWT authenticator for `jwt`, with a password or certificate authenticator and impersonation rules for `impersonation`, with OAuth2 for `oauth2` (stdio); http (local) or https for `service` | a recent release with the JWT and certificate authenticators, behind OPA and behind file-based rules, with row filters and column masks |
| Policy engines | OPA (`trino-opa`), Ranger (Trino plugin), Trino file-based access control | OPA and file-based rules, with row filters and column masks |
| Identity providers | any OIDC provider publishing a JWKS (the door); OAuth 2.0 Token Exchange, RFC 8693 (`jwt` mode) | Keycloak, standard token exchange |
| MCP | protocol negotiated by the official `mcp` SDK 2.2 (initialize handshake revisions 2024-11-05 to 2025-11-25, and the stateless 2026-07-28 revision); transports `streamable-http`, `sse` and `stdio` | all three transports, official SDK client at its default revision, in CI |
| MCP hosts | remote: anything that sends a bearer header; local: any stdio host | Python SDK, the example agent |
| Models | any, the server never talks to a model | Mistral Small 3.2 through OpenRouter, driving the tools |

The images the integration suite runs, Keycloak and Trino included, are pinned in
[`integration/compose.yaml`](../../integration/compose.yaml).

## Network

The server listens on two ports: `3000` for MCP and `3001` for health and metrics
(`MCP_PORT`, `MCP_HEALTH_PORT`). It reaches the provider's JWKS endpoint, its token endpoint in
`jwt` mode (https only), and Trino. A password, a token or a client certificate is never sent
over plain http.

## Kubernetes and OpenShift

Optional. A Helm chart ships in [`charts/akko-mcp-trino/`](../../charts/akko-mcp-trino/), and it
runs under OpenShift's arbitrary UID too: see
[Deployment](deployment.md#kubernetes-and-openshift).
