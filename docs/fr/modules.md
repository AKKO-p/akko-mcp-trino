# Modules

[English](../en/modules.md) · **Français**

Le cœur livre trois extensions à lui, chacune désactivée tant que `MCP_EXTENSIONS` ne la nomme
pas (séparées par des virgules) : `token-policy`, `fingerprint-audit` et `trino-tags`. Leur ordre
compte là où des extensions se rencontrent : un document public du même chemin est celui de la
dernière extension, le challenge d'un refus est celui de la première qui en donne un, et l'audit
est enveloppé dans cet ordre.

## token-policy

Un token vérifié doit aussi nommer un client admis et porter les portées
exigées. Le client est la première des revendications de `MCP_JWT_CLIENT_CLAIMS` que porte le
token, et ses portées la première de `MCP_JWT_SCOPE_CLAIMS` ; les deux sont comparés exactement,
casse comprise, sans joker. Un client étranger reçoit le 401 d'un token invalide
(`X-Reason: client_not_allowed`), et n'apprend jamais quelle portée l'aurait laissé entrer, car le
client est vérifié d'abord. Un token sans toutes les portées exigées reçoit 403
`insufficient_scope`, dont le `WWW-Authenticate` nomme les portées à demander (RFC 6750 §3.1), et
les métadonnées de ressource protégée ajoutent `scopes_supported`. Une requête sans token vérifié,
que `MCP_AUTH_REQUIRED=false` laisse passer le cœur, reçoit le 401 et le challenge du cœur, jamais
un 403. Chaque client et chaque portée doit être un scope-token OAuth (ASCII imprimable, sans
espace, sans guillemet, sans barre oblique inverse), sinon il refuse de démarrer ; sans aucune des
deux listes aussi : il n'imposerait rien. Les revendications sont lues à leur présence : la
première que porte le token est lue, quoi qu'elle contienne, si bien qu'un `scp` vide ne renvoie
jamais à `scope` ; ne listez que des revendications que l'identity provider pose lui-même, jamais
une qu'alimente un attribut modifiable par l'utilisateur.

Par exemple, la suite d'intégration (`integration/compose.yaml`, serveur `mcp-scoped`) le règle
ainsi :

```sh
MCP_EXTENSIONS=token-policy
MCP_ALLOWED_CLIENTS=mcp-host
MCP_REQUIRED_SCOPES=data:query
```

Un token du client `mcp-host` sans la portée `data:query` reçoit cette réponse :

```http
HTTP/1.1 403 Forbidden
X-Reason: insufficient_scope
WWW-Authenticate: Bearer resource_metadata="http://mcp-scoped:3000/.well-known/oauth-protected-resource/mcp", error="insufficient_scope", scope="data:query"

{"error":"insufficient_scope","message":"the token lacks a scope this server requires: ask the identity provider for a token with the scopes the WWW-Authenticate header names"}
```

et les métadonnées de ressource protégée nomment la portée :

```json
{
  "resource": "http://mcp-scoped:3000/mcp",
  "authorization_servers": ["https://keycloak:8443/realms/integration"],
  "bearer_methods_supported": ["header"],
  "scopes_supported": ["data:query"]
}
```

Voici les revendications, par identity provider :

- Keycloak : les défauts (`MCP_JWT_CLIENT_CLAIMS=azp,client_id`, `MCP_JWT_SCOPE_CLAIMS=scope`), testés par la suite d'intégration.
- Entra ID : `MCP_JWT_SCOPE_CLAIMS=scp` ; le client est dans `azp` ou `appid` selon la version du token, vérifiez donc l'un de vos tokens. Non testé en live.
- Okta : `MCP_JWT_SCOPE_CLAIMS=scp`, `MCP_JWT_CLIENT_CLAIMS=cid`. Non testé en live.

## fingerprint-audit

La ligne d'audit ajoute ce que l'appel a fait, après les champs du
cœur, dans cet ordre : `request_id`, `tool`, `subject`, `token_id` (le `jti` du token, jamais le
token), `ok`, `error`, `sql_sha256`, `tables`, `duration_ms`, `rows`, `truncated`,
`metadata_served` (les tables dont la description de catalogue est parvenue à l'appelant). Le
texte SQL n'a toujours aucun champ. `sql_sha256` est le SHA-256 de l'instruction envoyée à Trino
pour l'appelant ; un tool qui en envoie plusieurs garde la dernière qu'il a notée, celle qui a
répondu à l'agent (l'agrégat de `profile_table`, l'échantillon de `describe_table`). L'historique
de Trino garde le texte, et un moteur de politiques qui l'enregistre obtient la même empreinte :
c'est la jointure. Chaque liste garde `MCP_AUDIT_MAX_NAMES` noms ; au-delà, `tables_cut` ou
`metadata_served_cut`, juste après la liste, dit combien il y en avait de plus, chacun compté une
fois. Un champ qu'une autre extension ajoute à la ligne n'écrase jamais l'un de ceux-ci : il est
retiré et compté dans `extra_dropped`. Le serveur enveloppe l'audit dans l'ordre de
`MCP_EXTENSIONS`, chacune autour de la précédente : nommez `fingerprint-audit` avant toute autre
extension qui enveloppe l'audit.

Par exemple, la ligne d'un appel d'`execute_query` dans la suite d'intégration (le sujet et les
identifiants sont remplacés par des valeurs fictives ; l'ordre des champs et l'empreinte sont
ceux de l'exécution) :

```json
{
  "request_id": "req-docs-0001",
  "tool": "execute_query",
  "subject": "alice_user",
  "token_id": "jti-docs-0001",
  "ok": true,
  "error": "",
  "sql_sha256": "595c26467ad8ea2d0a0857b4bf5c094d6f6d6bfc8c88ebc289f03a523ce7faf5",
  "tables": ["tpch.tiny.region"],
  "duration_ms": 1675,
  "rows": 1,
  "truncated": false,
  "metadata_served": []
}
```

## trino-tags

Chaque requête d'un appel porte deux étiquettes client (`X-Trino-Client-Tags`) :
`request_id=<id>`, l'identifiant de requête envoyé par la périphérie ou créé par le serveur, et
`tool=<tool>`, chacune seulement quand elle est connue. Trino les garde avec la requête : son
historique, ses groupes de ressources et un moteur de politiques qui les enregistre voient quel
appel du serveur l'a envoyée. Le cœur ne garde l'identifiant de la périphérie que s'il est un
token simple, et le module ne garde un nom de tool que s'il en est un aussi, sinon `tool=other` :
aucune virgule ni aucun `=` ne peut ajouter une étiquette. L'étiquette du filtre
(`mcp-gate`) et celle du profil (`mcp-profile`) s'y ajoutent. Par exemple, un groupe de
ressources pour les requêtes que les agents écrivent eux-mêmes (les chiffres sont un exemple à
adapter, pas une mesure ; chaque sélecteur nomme un groupe feuille) :

```json
{
  "rootGroups": [
    {
      "name": "global",
      "softMemoryLimit": "80%",
      "hardConcurrencyLimit": 100,
      "maxQueued": 1000,
      "subGroups": [
        {"name": "agents", "softMemoryLimit": "30%", "hardConcurrencyLimit": 10, "maxQueued": 100},
        {"name": "other", "softMemoryLimit": "50%", "hardConcurrencyLimit": 90, "maxQueued": 900}
      ]
    }
  ],
  "selectors": [
    {"clientTags": ["tool=execute_query"], "group": "global.agents"},
    {"group": "global.other"}
  ]
}
```

Dans la suite d'intégration, un appel d'`execute_query` envoyé avec `X-Request-Id: req-docs-0001`
a lancé sa requête avec ces étiquettes client :

```text
X-Trino-Client-Tags: request_id=req-docs-0001,tool=execute_query
```

et `system.runtime.queries` de Trino la montre dans le groupe que nomme le sélecteur sur
`tool=execute_query` (`integration/trino/resource-groups.json`, avec les mêmes sélecteurs que
ci-dessus) :

```text
query_id                     resource_group_id    source
20261106_173554_00003_ppe7v  [global, agents]     trino-mcp
```

## Réglages

`MCP_ALLOWED_CLIENTS`, `MCP_REQUIRED_SCOPES` et `MCP_AUDIT_MAX_NAMES` sont dans
[Configuration](configuration.md#modules), et `MCP_JWT_CLIENT_CLAIMS` et `MCP_JWT_SCOPE_CLAIMS`
dans [son tableau d'identité](configuration.md#identité).
