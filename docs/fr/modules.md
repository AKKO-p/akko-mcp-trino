# Modules

[English](../en/modules.md) · **Français**

Le cœur livre trois modules : `token-policy`, `fingerprint-audit` et `trino-tags`. Chacun reste
désactivé tant que `MCP_EXTENSIONS` ne le nomme pas (séparés par des virgules). Dans le code, un
module est une extension (`akko_mcp_trino.extensions.Extension`), chargée comme toute autre.

L'ordre dans `MCP_EXTENSIONS` compte quand deux modules ou extensions touchent la même chose :

- si deux publient un document au même chemin, le dernier l'emporte ;
- pour un refus, le premier qui rend un challenge l'emporte ;
- les enveloppes d'audit s'appliquent dans cet ordre.

## token-policy

Un token vérifié doit aussi nommer un client admis et porter les portées exigées. Le client est le
premier des claims de `MCP_JWT_CLIENT_CLAIMS` que porte le token, et ses portées le premier de
`MCP_JWT_SCOPE_CLAIMS`. Les deux sont comparés exactement, casse comprise, sans joker.

- Un client non admis reçoit le 401 d'un token invalide (`X-Reason: client_not_allowed`). Il
  n'apprend jamais quelle portée l'aurait laissé entrer, car le client est vérifié d'abord.
- Un token sans toutes les portées exigées reçoit un 403 `insufficient_scope`. Son
  `WWW-Authenticate` nomme les portées à demander (RFC 6750 §3.1), et les métadonnées de ressource
  protégée ajoutent `scopes_supported`.
- Une requête sans token vérifié, que le cœur laisse passer parce que `MCP_AUTH_REQUIRED=false`,
  reçoit le 401 et le challenge du cœur, jamais un 403.

Chaque client et chaque portée doit être un scope-token OAuth (ASCII imprimable, sans espace, sans
guillemet, sans barre oblique inverse) ; sinon, le serveur refuse de démarrer. Il refuse aussi de
démarrer quand aucune des deux listes n'est posée, puisque le module n'imposerait rien. C'est la
présence d'un claim qui compte : le premier que porte le token est lu, quoi qu'il contienne, si
bien qu'un `scp` vide ne se rabat jamais sur `scope`. Ne listez que des claims que l'identity
provider pose lui-même. Ne listez jamais un claim rempli à partir d'un attribut que l'utilisateur
peut modifier.

Par exemple, la suite d'intégration (`integration/compose.yaml`, serveur `mcp-scoped`) pose :

```sh
MCP_EXTENSIONS=token-policy
MCP_ALLOWED_CLIENTS=mcp-host
MCP_REQUIRED_SCOPES=data:query
```

Un token du client `mcp-host` sans la portée `data:query` reçoit cette réponse :

```http
HTTP/1.1 403 Forbidden
X-Reason: insufficient_scope
WWW-Authenticate: Bearer resource_metadata="http://mcp-scoped:3000/.well-known/oauth-protected-resource/mcp", error="insufficient_scope", scope="data:query"

{"error":"insufficient_scope","message":"the token lacks a scope this server requires: ask the identity provider for a token with the scopes the WWW-Authenticate header names"}
```

Les métadonnées de ressource protégée nomment la portée :

```json
{
  "resource": "http://mcp-scoped:3000/mcp",
  "authorization_servers": ["https://keycloak:8443/realms/integration"],
  "bearer_methods_supported": ["header"],
  "scopes_supported": ["data:query"]
}
```

Voici les claims, par identity provider :

- Keycloak : les défauts (`MCP_JWT_CLIENT_CLAIMS=azp,client_id`, `MCP_JWT_SCOPE_CLAIMS=scope`), testés par la suite d'intégration.
- Entra ID : `MCP_JWT_SCOPE_CLAIMS=scp` ; le client est dans `azp` ou `appid` selon la version du token, vérifiez donc l'un de vos tokens. Non testé en conditions réelles.
- Okta : `MCP_JWT_SCOPE_CLAIMS=scp`, `MCP_JWT_CLIENT_CLAIMS=cid`. Non testé en conditions réelles.

## fingerprint-audit

Ce module ajoute à la ligne d'audit ce que l'appel a fait, après les champs du cœur, dans cet
ordre : `request_id`, `tool`, `subject`, `token_id` (le `jti` du token, jamais le token), `ok`,
`error`, `sql_sha256`, `tables`, `duration_ms`, `rows`, `truncated`, `metadata_served` (les tables
dont la description de catalogue de données est parvenue à l'appelant). Le texte SQL n'a toujours
aucun champ. Quand une réponse a été coupée, `truncated_reason` (`rows` ou `bytes`) suit
`metadata_served` ; il est absent sinon.

`sql_sha256` est le SHA-256 de l'instruction envoyée à Trino pour l'appelant. Un tool qui en envoie
plusieurs garde la dernière qu'il a notée, celle qui a répondu à l'agent (l'agrégat de
`profile_table`, l'échantillon de `describe_table`). L'historique de Trino garde le texte, et un
moteur de policies qui l'enregistre obtient la même empreinte : servez-vous de l'empreinte pour les
joindre.

Chaque liste garde `MCP_AUDIT_MAX_NAMES` noms. Au-delà, `tables_cut` ou `metadata_served_cut`,
juste après la liste, dit combien il y en avait de plus, chacun compté une fois. Un champ qu'une
autre extension ajoute à la ligne n'écrase jamais l'un de ceux-ci : il est retiré et compté dans
`extra_dropped`. Le serveur enveloppe l'audit dans l'ordre de `MCP_EXTENSIONS`, chaque enveloppe
autour de la précédente : nommez donc `fingerprint-audit` avant toute autre extension qui enveloppe
l'audit.

Voici la ligne d'un appel d'`execute_query` dans la suite d'intégration. Le sujet et les
identifiants sont remplacés par des valeurs fictives ; l'ordre des champs et l'empreinte sont ceux
de l'exécution :

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

Chaque requête d'un appel porte deux étiquettes client (`X-Trino-Client-Tags`), chacune seulement
quand elle est connue : `request_id=<id>`, l'identifiant de requête envoyé par la passerelle placée
devant le serveur, ou créé par le serveur, et `tool=<tool>`. Trino les garde avec la requête : son
historique, ses groupes de ressources et un moteur de policies qui les enregistre voient quel appel
du serveur l'a envoyée.

Le cœur ne garde un identifiant de requête reçu que s'il est un identifiant simple, et le module ne
garde un nom de tool que s'il en est un aussi, sinon `tool=other`. Aucune virgule ni aucun `=` ne
peut donc ajouter une étiquette. L'étiquette du filtre de visibilité (`mcp-gate`) et celle du
profil (`mcp-profile`) s'y ajoutent.

Voici un groupe de ressources pour les requêtes que les agents écrivent eux-mêmes. Les chiffres
sont un point de départ, pas une mesure, et chaque sélecteur nomme un groupe feuille :

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
a lancé sa requête avec ces étiquettes client :

```text
X-Trino-Client-Tags: request_id=req-docs-0001,tool=execute_query
```

Le `system.runtime.queries` de Trino montre la requête dans le groupe que nomme le sélecteur sur
`tool=execute_query` (`integration/trino/resource-groups.json`, avec les mêmes sélecteurs que
ci-dessus) :

```text
query_id                     resource_group_id    source
20261106_173554_00003_ppe7v  [global, agents]     trino-mcp
```

## Réglages

`MCP_ALLOWED_CLIENTS` et `MCP_REQUIRED_SCOPES` sont dans [Configuration](configuration.md#modules),
`MCP_AUDIT_MAX_NAMES` dans [son tableau d'audit](configuration.md#audit), et `MCP_JWT_CLIENT_CLAIMS`
et `MCP_JWT_SCOPE_CLAIMS` dans [son tableau d'identité](configuration.md#identité).
