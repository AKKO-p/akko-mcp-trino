# Exploitation

[English](../en/operations.md) · **Français**

## Mise à l'échelle et groupes de ressources

La requête d'agrégats de [`profile_table`](tools.md#profile_table) porte l'étiquette client `MCP_PROFILE_CLIENT_TAG`
(`mcp-profile` par défaut) : les groupes de ressources de Trino peuvent ainsi borner leur nombre
simultané et leur mémoire, par exemple avec le gestionnaire de groupes de ressources par fichier (les
chiffres sont un exemple à adapter, pas une mesure). Chaque sélecteur nomme un groupe feuille : les
requêtes sans l'étiquette ont donc besoin de leur propre groupe (ici `global.other`), sinon Trino les
refuse :

```json
{
  "rootGroups": [
    {
      "name": "global",
      "softMemoryLimit": "80%",
      "hardConcurrencyLimit": 100,
      "maxQueued": 1000,
      "subGroups": [
        {"name": "profile", "softMemoryLimit": "10%", "hardConcurrencyLimit": 2, "maxQueued": 20},
        {"name": "other", "softMemoryLimit": "70%", "hardConcurrencyLimit": 98, "maxQueued": 980}
      ]
    }
  ],
  "selectors": [
    {"clientTags": ["mcp-profile"], "group": "global.profile"},
    {"group": "global.other"}
  ]
}
```

Le groupe de ressources borne Trino ; `MCP_PROFILE_MAX_CONCURRENT` (4) borne chaque réplica de ce
serveur : un profil en file dans Trino tient quand même l'un des fils que partagent tous les tools du
réplica, donc au-delà de ce nombre de profils simultanés un appel répond aussitôt `"kind": "busy"` et
n'envoie aucune requête. Gardez-le au plus égal à `hardConcurrencyLimit` plus `maxQueued` du groupe,
divisé par le nombre de réplicas.

Pour Trino, chaque appel de `search_columns` lance jusqu'à `MCP_SEARCH_CONCURRENCY` requêtes de
métadonnées à la fois : bornez-les par un groupe de ressources sur le `TRINO_SOURCE` du serveur,
comme pour l'étiquette client de `profile_table`, ou posez `MCP_SEARCH_CONCURRENCY=1` pour lire un
catalog à la fois, comme en 0.7. Le module [`trino-tags`](modules.md#trino-tags) étiquette chaque
requête avec son tool, pour un groupe de ressources des requêtes que les agents écrivent
eux-mêmes.

## Refus

Chaque refus porte `X-Reason` et l'identifiant de requête, jamais le token. Le cœur refuse en
`401 unauthenticated` une requête sans token valide (absent, falsifié, expiré, ou émis pour une
autre audience, celle de Trino comprise), et dit dans le corps ce qu'il faut envoyer :

```json
{"error": "unauthenticated", "message": "this server requires a bearer token issued for it by the identity provider: send Authorization: Bearer <access token>"}
```

Quand `MCP_RESOURCE_URL` est posé, le serveur publie ses métadonnées de ressource protégée à
`/.well-known/oauth-protected-resource` (et sous le chemin de l'URL, comme
`/.well-known/oauth-protected-resource/mcp`), qui nomment la ressource et `MCP_OIDC_ISSUER` comme
serveur d'autorisation, et le `401` porte `WWW-Authenticate: Bearer resource_metadata="…"`, comme
le demande la spécification d'autorisation de MCP. Les gardes d'une extension ajoutent leurs
propres raisons ; ses documents et ses challenges passent avant ceux du cœur.

## Avertissements

Une configuration qui laisse quelque chose ouvert est dite trois fois : par une ligne `WARNING` au
démarrage, dans `--check`, et à chaque MCP host dans les `instructions` de l'initialisation. Le
cœur signale le mode `service` et une porte qui n'exige pas de token valide.

## Audit

Chaque réponse de l'endpoint MCP porte `X-Request-Id`. C'est celui qu'a envoyé la passerelle
devant le serveur, quand c'est un identifiant simple (lettres, chiffres, `.`, `_`, `:`, `-`, 128
au plus), ou celui que le serveur a créé. Un tool call hors d'une requête HTTP (stdio) reçoit son
propre identifiant. Chaque tool call, ceux des plugins compris, écrit une ligne JSON dans le logger
`mcp.audit`, indexée par cet identifiant :

```json
{"request_id":"edge-42","tool":"execute_query","subject":"alice","ok":false,"error":"USER_ERROR:PERMISSION_DENIED","duration_ms":84}
```

Avec la configuration de journalisation par défaut, la ligne sort préfixée par
`INFO:mcp.audit:` ; un collecteur qui attend du JSON pur retire ce préfixe.

| Champ | Sens |
|---|---|
| `request_id`, `tool`, `subject` | quel appel, quel tool, pour qui ; la ligne n'a aucun champ qui pourrait contenir le bearer ou le texte SQL |
| `ok`, `error` | le résultat ; `error` est un code, jamais un message, parce qu'un message de Trino peut citer une valeur lue dans une ligne (`Cannot cast '<value>' to INT`) : le type et le nom de l'erreur de Trino pour une erreur Trino (`USER_ERROR:INVALID_CAST_ARGUMENT`), la classe de toute autre exception (`TimeoutError`, `CallTimeout` au-delà du budget de l'appel), le code qu'un tool donne à son propre refus (`read_only`, `invalid_argument`) ou un plugin avec `note(error=...)`, sinon `tool_error`. L'agent, lui, lit le message entier |
| `duration_ms` | la durée de l'appel |

Ce qu'a fait un appel (le SHA-256 de son SQL, les tables, les lignes, si elles ont été coupées)
est gardé dans son enregistrement pendant qu'il tourne (`akko_mcp_trino.audit.note`) ; la ligne du
cœur ne l'écrit pas, une extension peut le faire (`wrap_audit`), comme `fingerprint-audit`. Chaque
liste de l'enregistrement garde `MCP_AUDIT_MAX_NAMES` noms (200 par défaut) et compte les autres,
une fois chacun, à coût constant : une recherche sur mille catalogs n'écrit jamais une ligne sans
borne.

## Santé et métriques

`/health`, sur le port de santé, ne touche jamais Trino : une sonde de vie ne peut pas tuer le
serveur parce que Trino est lent. `/ready` interroge le mode d'identité : en mode `jwt`, le
`/v1/info` public de Trino ; en modes `impersonation` et `service`, un `SELECT 1` borné sous
`TRINO_USER`, au nom de personne d'autre. `/metrics` expose `mcp_trino_queries_total`,
`mcp_trino_query_errors_total` et `mcp_trino_query_duration_seconds`. Avec des fournisseurs de
contexte chargés, il expose aussi `mcp_trino_context_calls_total`,
`mcp_trino_context_errors_total` (par statut), `mcp_trino_context_call_duration_seconds`,
`mcp_trino_context_cache_lookups_total` (trouvé ou non), `mcp_trino_context_cache_bytes` et
`mcp_trino_context_breaker_open`, chacune par fournisseur.

## Montée de version

**Depuis la 0.7.0.** Les 0.7.1 et 0.7.2 sont une version de sécurité. `execute_query` refuse
`SHOW STATS`, `EXPLAIN`, `SHOW CREATE` et les autres lectures qui peuvent révéler des informations
au-delà d'un filtre de lignes ou d'un masque, et l'utilisateur est lu dans `sub`. Avec Keycloak,
posez `MCP_JWT_USER_CLAIM=preferred_username` avant la mise à jour. Voir
[MIGRATING.md](../../MIGRATING.md) (en anglais).

**Depuis la 0.6.** La 0.7.0 ne retire et ne renomme rien : elle ajoute `search_columns` et
`describe_schema`, pagine les listes et donne à chaque erreur un `kind`. Laissez la policy du
moteur lire `system.jdbc` et `system.metadata`, que lisent les deux nouveaux tools (la 0.6
conseillait de refuser tout `system`). Gardez `MCP_CALL_TIMEOUT_SECONDS` à 0 ou au-dessus de
`MCP_SEARCH_RESERVE_SECONDS` (2 par défaut), sinon le serveur refuse de démarrer. Une liste
`truncated` n'est plus la fin : lisez la suite avec `next_offset`. Pour les auteurs d'extensions,
une fonction utilitaire change de signature, et en mode `oauth2` les gardes tournent à chaque
requête qui n'est pas de la découverte, et non plus une fois au démarrage.

**Depuis la 0.5.** La 0.6.0 a changé les valeurs par défaut : un appelant vérifié est exigé, et le
mode `jwt` échange le token de l'appelant au lieu de le transmettre. Le guide
[MIGRATING.md](../../MIGRATING.md) décrit chaque étape des deux.
