# Exploitation

[English](../en/operations.md) · **Français**

## Mise à l'échelle et groupes de ressources

La requête d'agrégats de [`profile_table`](tools.md#profile_table) porte l'étiquette client
`MCP_PROFILE_CLIENT_TAG` (`mcp-profile` par défaut). Les groupes de ressources de Trino peuvent
donc borner le nombre de ces requêtes en cours et la mémoire qu'elles prennent. Voici un exemple
pour le gestionnaire de groupes de ressources par fichier. Les chiffres sont un point de départ,
pas une mesure. Chaque sélecteur nomme un groupe feuille : les requêtes sans l'étiquette ont donc
besoin de leur propre groupe (ici `global.other`), sinon Trino les rejette :

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

Le groupe de ressources borne Trino, et `MCP_PROFILE_MAX_CONCURRENT` (4) borne chaque réplica de ce
serveur. Un profil en file dans Trino tient quand même l'un des threads que partagent tous les
tools du réplica. Au-delà de ce nombre de profils simultanés, un appel rend donc aussitôt
`"kind": "busy"` et n'envoie aucune requête. Gardez-le au plus égal à `hardConcurrencyLimit` plus
`maxQueued` du groupe, divisé par le nombre de réplicas.

Chaque appel de `search_columns` lance jusqu'à `MCP_SEARCH_CONCURRENCY` requêtes de métadonnées à
la fois dans Trino. Bornez-les par un groupe de ressources sur le `TRINO_SOURCE` du serveur, comme
pour l'étiquette client de `profile_table`, ou posez `MCP_SEARCH_CONCURRENCY=1` pour lire un
catalog à la fois. Le module [`trino-tags`](modules.md#trino-tags) étiquette chaque requête avec
son tool, pour qu'un groupe de ressources cible les requêtes que les agents écrivent eux-mêmes.

## Refus

Chaque refus porte `X-Reason` et l'identifiant de requête, jamais le token. Le cœur refuse avec un
`401 unauthenticated` une requête sans token valide (absent, falsifié, expiré, ou émis pour une
autre audience, celle de Trino comprise), et dit dans le corps ce qu'il faut envoyer :

```json
{"error": "unauthenticated", "message": "this server requires a bearer token issued for it by the identity provider: send Authorization: Bearer <access token>"}
```

Quand `MCP_RESOURCE_URL` est posé, le serveur publie ses métadonnées de ressource protégée à
`/.well-known/oauth-protected-resource`, et sous le chemin de l'URL, comme
`/.well-known/oauth-protected-resource/mcp`. Ces métadonnées nomment la ressource et
`MCP_OIDC_ISSUER` comme serveur d'autorisation, et le `401` porte
`WWW-Authenticate: Bearer resource_metadata="…"`, comme le demande la spécification d'autorisation
de MCP. Les gardes d'une extension ajoutent leurs propres raisons, et ses documents et ses
challenges passent avant ceux du cœur.

## Avertissements

Une configuration qui laisse quelque chose ouvert est signalée à trois endroits : une ligne
`WARNING` au démarrage, `--check`, et les `instructions` de l'initialisation MCP, envoyées à chaque
MCP host. Le cœur avertit dans ces cas, avec ce texte :

| Condition | Avertissement |
|---|---|
| `TRINO_IDENTITY_MODE=service` | `TRINO_IDENTITY_MODE=service: every query runs as the Trino user '<TRINO_USER>', whoever calls; Trino cannot tell callers apart` |
| `MCP_AUTH_ENABLED=false` | `MCP_AUTH_ENABLED=false: no caller is verified; anyone who reaches this server queries Trino` |
| `MCP_AUTH_REQUIRED=false`, avec l'authentification active | `MCP_AUTH_REQUIRED=false: a request without a valid token is served too` |
| `MCP_SEARCH_RESERVE_SECONDS=0` avec un budget d'appel | `MCP_SEARCH_RESERVE_SECONDS=0: a search of search_columns may run to the end of the call's budget, and the call then answers its time_budget error with nothing it found` |
| `MCP_CALL_TIMEOUT_SECONDS=0` | `MCP_CALL_TIMEOUT_SECONDS=0: a tool call has no time budget, so search_columns refuses to search every catalog and searches a named catalog only` |
| `MCP_EXTRA_READ_COMMANDS` nomme des commandes | l'avertissement nomme les commandes qu'il ouvre ; [Sécurité](security.md#les-instructions-quun-agent-peut-envoyer) dit ce qu'elles sont |
| `MCP_DESCRIBE_MAX_COLUMNS` au-dessus de `MCP_PROFILE_DESCRIBE_ROWS` | `MCP_DESCRIBE_MAX_COLUMNS=<n> is above MCP_PROFILE_DESCRIBE_ROWS=<n>: describe_table shows columns profile_table cannot name` |
| `full` dans `MCP_PROFILE_MODES` sans aucun budget de temps | `MCP_PROFILE_MODES allows full while MCP_PROFILE_TIMEOUT_SECONDS, TRINO_QUERY_TIMEOUT_SECONDS and MCP_CALL_TIMEOUT_SECONDS are 0: a full profile reads every row it may read with no time limit` |
| un fournisseur de contexte sans `MCP_CONTEXT_CANARY` | `MCP_CONTEXT_CANARY is not set: nothing checks that Trino filters its listings, and context follows what Trino lists` |
| un fournisseur de contexte en mode `service` | `context visibility is the service account's` |

## Audit

Chaque réponse de l'endpoint MCP porte `X-Request-Id`. C'est celui qu'a envoyé la passerelle placée
devant le serveur, quand c'est un identifiant simple (lettres, chiffres, `.`, `_`, `:`, `-`, 128
caractères au plus), ou celui que le serveur a créé. Un tool call hors d'une requête HTTP (stdio)
reçoit son propre identifiant. Chaque tool call, ceux des plugins compris, écrit une ligne JSON dans
le logger `mcp.audit`, indexée par cet identifiant :

```json
{"request_id":"edge-42","tool":"execute_query","subject":"alice","ok":false,"error":"USER_ERROR:PERMISSION_DENIED","duration_ms":84}
```

Avec la configuration de journalisation par défaut, la ligne sort préfixée par `INFO:mcp.audit:`.
Un collecteur qui attend du JSON pur doit retirer ce préfixe.

| Champ | Sens |
|---|---|
| `request_id`, `tool`, `subject` | quel appel, quel tool, pour qui ; la ligne n'a aucun champ qui pourrait contenir le bearer ou le texte SQL |
| `ok`, `error` | le résultat ; `error` est un code, jamais un message, parce qu'un message de Trino peut citer une valeur lue dans une ligne (`Cannot cast '<value>' to INT`) : le type et le nom de l'erreur de Trino pour une erreur Trino (`USER_ERROR:INVALID_CAST_ARGUMENT`), la classe de toute autre exception (`TimeoutError`, `CallTimeout` au-delà du budget de l'appel), le code qu'un tool donne à son propre refus (`read_only`, `invalid_argument`, `sql_too_long`, `sql_too_nested`, `parse_deadline`, `parse_no_worker`, `parse_busy`, `parse_failed`) ou un plugin avec `note(error=...)`, sinon `tool_error`. L'agent, lui, lit le message entier |
| `duration_ms` | la durée de l'appel |

Ce qu'a fait un appel (le SHA-256 de son SQL, les tables, les lignes, si elles ont été coupées) est
gardé dans son enregistrement pendant qu'il tourne (`akko_mcp_trino.audit.note`). La ligne du cœur
ne l'écrit pas ; une extension peut le faire (`wrap_audit`), comme
[`fingerprint-audit`](modules.md#fingerprint-audit). Chaque liste de l'enregistrement garde
`MCP_AUDIT_MAX_NAMES` noms (200 par défaut) et compte les autres, une fois chacun, à coût constant :
une recherche sur mille catalogs n'écrit jamais une ligne sans limite.

## Santé et métriques

Ces endpoints n'existent que sur les transports HTTP, sur le port de santé.

- `/health` ne touche jamais Trino : une sonde de vie ne peut pas tuer le serveur parce que Trino
  est lent.
- Ce que vérifie `/ready` dépend du mode d'identité : en mode `jwt`, le `/v1/info` public de
  Trino ; en modes `impersonation` et `service`, un `SELECT 1` borné sous `TRINO_USER`, au nom de
  personne d'autre. Quand le contrôle échoue, `/ready` rend un `503` avec
  `{"status": "error", "detail": ...}`.
- `/metrics` expose `mcp_trino_queries_total`, `mcp_trino_query_errors_total`,
  `mcp_trino_query_duration_seconds` et `mcp_trino_sql_refusals_total`, les instructions refusées
  avant Trino par `reason` : `too_long`, `too_nested`, `caller_busy`, `no_worker`, `deadline` (une
  analyse coupée par le délai) ou `worker_failed` ; jamais une instruction ni un nom. Avec des fournisseurs de contexte chargés, il expose aussi,
  par fournisseur, `mcp_trino_context_calls_total`, `mcp_trino_context_errors_total` (par statut),
  `mcp_trino_context_call_duration_seconds`, `mcp_trino_context_cache_lookups_total` (trouvé ou
  non), `mcp_trino_context_cache_bytes` et `mcp_trino_context_breaker_open`. Un appel que
  l'outil cesse d'attendre est compté comme s'il avait fini, chaque source lue étant `partial` ou
  `unavailable` ; un arrêt du serveur pendant une lecture en compte ainsi quelques-uns.

## Montée de version

[MIGRATING.md](../../MIGRATING.md) (en anglais) donne chaque étape. En bref :

**De la 0.7 à la 0.8.** [MIGRATING.md](../../MIGRATING.md#from-07-to-08) liste les changements, dont
les modules que `MCP_EXTENSIONS` nomme désormais un par un.

**Depuis la 0.7.0.** La 0.7.1 et la 0.7.2 sont des versions de sécurité. `execute_query` refuse
`SHOW STATS`, `EXPLAIN`, `SHOW CREATE` et les autres lectures listées dans
[Sécurité](security.md#les-instructions-quun-agent-peut-envoyer), et l'utilisateur est lu dans
`sub`. Avec Keycloak, posez `MCP_JWT_USER_CLAIM=preferred_username` avant la mise à jour.

**Depuis la 0.6.** La 0.7.0 ne retire et ne renomme rien : elle ajoute `search_columns` et
`describe_schema`, pagine les listes et donne à chaque erreur un `kind`. Laissez la policy du
moteur lire `system.jdbc` et `system.metadata`, que lisent les deux nouveaux tools (la 0.6
conseillait de refuser tout `system`). Gardez `MCP_CALL_TIMEOUT_SECONDS` à 0 ou au-dessus de
`MCP_SEARCH_RESERVE_SECONDS` (2 par défaut), sinon le serveur refuse de démarrer. Une liste
`truncated` n'est plus la fin : lisez la suite avec `next_offset`. Pour les auteurs d'extensions,
une fonction utilitaire change de signature, et en mode `oauth2` les gardes tournent à chaque
requête qui n'est pas de la découverte, et non plus une fois au démarrage.

**Depuis la 0.5.** La 0.6.0 a changé les valeurs par défaut : un appelant vérifié est exigé, et le
mode `jwt` échange le token de l'appelant au lieu de le transmettre.
