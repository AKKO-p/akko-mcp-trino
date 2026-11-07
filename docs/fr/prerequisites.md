# Prérequis

[English](../en/prerequisites.md) · **Français**

## Ce qu'il faut

| Il faut | Pourquoi | Remarques |
|---|---|---|
| Python 3.12 ou plus récent | l'exécution | `pip` et un environnement virtuel |
| Un coordinateur Trino joignable | le moteur | toute version récente ; https pour `jwt` et `impersonation`, et dès qu'un secret est envoyé |
| Un IdP OIDC avec un endpoint JWKS | la porte | Keycloak, Entra ID, Okta, Dex… Les tokens portent `iss`, un `aud` qui nomme ce serveur, `exp`, et la revendication qui nomme l'utilisateur (`sub` par défaut, `MCP_JWT_USER_CLAIM`) |
| `jwt` : l'échange de token auprès de l'IdP, et l'authentificateur JWT de Trino | chaque utilisateur lit avec ses propres droits | un client confidentiel pour ce serveur, autorisé à échanger un token émis pour lui contre un token dont l'audience est Trino ; `http-server.authentication.type=JWT` avec le JWKS de l'IdP et `required-audience` réglé sur cette audience |
| Ou `impersonation` : un compte pour ce serveur, et une règle d'impersonation | chaque utilisateur lit avec ses propres droits, sans échange de token | `TRINO_USER` avec `TRINO_PASSWORD` ou un certificat client ; une règle Trino qui laisse ce compte, et lui seul, agir au nom des utilisateurs |
| Ou `service` : une identité Trino unique | les droits de chaque appelant | `TRINO_USER`, et `TRINO_PASSWORD` ou un certificat client ; ne lui donner des droits de lecture que sur ce que tout appelant peut lire |
| Un moteur de policies qui décide pour Trino | la gouvernance | OPA (`opa.policy.uri`), Ranger, ou les règles par fichier de Trino |
| Un MCP host | le client | Cursor, Claude Desktop, VS Code, ou tout client construit sur le SDK `mcp` |

En option, un Prometheus pour lire `/metrics`.

## Compatibilité

| | Pris en charge | Testé |
|---|---|---|
| Python | 3.12, 3.13 | 3.12, 3.13 (CI) |
| Trino | 351 et plus récent ; https avec l'authentificateur JWT pour `jwt`, avec un authentificateur par mot de passe ou par certificat et des règles d'impersonation pour `impersonation`, avec OAuth2 pour `oauth2` (stdio) ; http (en local) ou https pour `service` | une version récente avec les authentificateurs JWT et par certificat, derrière OPA et derrière des règles par fichier, avec des row filters et des column masks |
| Moteurs de policies | OPA (`trino-opa`), Ranger (plugin Trino), contrôle d'accès par fichier de Trino | OPA et règles par fichier, avec des row filters et des column masks |
| Identity providers | tout IdP OIDC qui publie un JWKS (la porte) ; OAuth 2.0 Token Exchange, RFC 8693 (mode `jwt`) | Keycloak, échange de token standard |
| MCP | protocole négocié par le SDK officiel `mcp` 2.2 (révisions de la poignée de main d'initialisation de 2024-11-05 à 2025-11-25, et la révision sans état 2026-07-28) ; transports `streamable-http`, `sse` et `stdio` | les trois transports, client officiel du SDK à sa révision par défaut, en CI |
| MCP hosts | à distance : tout client qui envoie un en-tête bearer ; en local : tout MCP host stdio | le SDK Python, l'agent d'exemple |
| Modèles | tous, le serveur ne parle jamais à un modèle | Mistral Small 3.2 par OpenRouter, qui pilote les tools |

Les images que lance la suite d'intégration, Keycloak et Trino compris, sont épinglées dans
[`integration/compose.yaml`](../../integration/compose.yaml).

## Réseau

Le serveur écoute sur deux ports : `3000` pour MCP et `3001` pour la santé et les métriques
(`MCP_PORT`, `MCP_HEALTH_PORT`). Il joint l'endpoint JWKS de l'IdP, son endpoint de token en mode
`jwt` (en https seulement), et Trino. Un mot de passe, un token ou un certificat client ne
circule jamais en http simple.

## Kubernetes et OpenShift

En option. Une chart Helm est livrée dans [`charts/akko-mcp-trino/`](../../charts/akko-mcp-trino/),
et elle tourne aussi sous l'UID arbitraire d'OpenShift : voir
[Déploiement](deployment.md#kubernetes-et-openshift).
