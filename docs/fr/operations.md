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
| `TRINO_VERIFY=false` (ou `0`, `no`) en https | `TRINO_VERIFY=false: Trino's certificate is not verified, so the server cannot know whom it sends its credentials to; set TRINO_VERIFY=true, or the path of a CA bundle` |
| `MCP_JWKS_URL` hors https, avec la vérification des tokens active | `MCP_JWKS_URL=<url>: the keys that verify tokens are read over plain http, so anyone on the path could substitute them and mint accepted tokens; set an https MCP_JWKS_URL` |
| `TRINO_TOKEN_EXCHANGE_VERIFY=false` (ou `0`, `no`) en mode `jwt` | `TRINO_TOKEN_EXCHANGE_VERIFY=false: the token endpoint's certificate is not verified, so the server cannot know whom it sends its client secret and the callers' tokens to; set TRINO_TOKEN_EXCHANGE_VERIFY=true, or the path of a CA bundle` |
| aucun tool de `discovery` exposé | `MCP_TOOLSETS leaves out every discovery tool: the agent writes its queries without listing or describing the tables it may read; add discovery` |
| `MCP_SEARCH_RESERVE_SECONDS=0` avec un budget d'appel | `MCP_SEARCH_RESERVE_SECONDS=0: a search of search_columns may run to the end of the call's budget, and the call then answers its time_budget error with nothing it found` |
| `MCP_CALL_TIMEOUT_SECONDS=0` | `MCP_CALL_TIMEOUT_SECONDS=0: a tool call has no time budget, so search_columns refuses to search every catalog and searches a named catalog only` |
| `MCP_EXTRA_READ_COMMANDS` nomme des commandes | l'avertissement nomme les commandes qu'il ouvre ; [Sécurité](security.md#les-instructions-quun-agent-peut-envoyer) dit ce qu'elles sont |
| `MCP_DESCRIBE_MAX_COLUMNS` au-dessus de `MCP_PROFILE_DESCRIBE_ROWS` | `MCP_DESCRIBE_MAX_COLUMNS=<n> is above MCP_PROFILE_DESCRIBE_ROWS=<n>: describe_table shows columns profile_table cannot name` |
| `full` dans `MCP_PROFILE_MODES` sans aucun budget de temps | `MCP_PROFILE_MODES allows full while MCP_PROFILE_TIMEOUT_SECONDS, TRINO_QUERY_TIMEOUT_SECONDS and MCP_CALL_TIMEOUT_SECONDS are 0: a full profile reads every row it may read with no time limit` |
| un fournisseur de contexte sans `MCP_CONTEXT_CANARY` | `MCP_CONTEXT_CANARY is not set: nothing checks that Trino filters its listings, and context follows what Trino lists` |
| un fournisseur de contexte en mode `service` | `context visibility is the service account's` |

## Audit et journaux

Le serveur écrit deux sortes de lignes sur stderr : l'audit, dans le logger `mcp.audit`, et le
journal technique de ses autres loggers. En stdio, stderr est ce que garde l'hôte ; dans un pod,
c'est ce que lit le collecteur du cluster. Le serveur n'envoie lui-même aucune ligne nulle part :
les acheminer, les conserver et les protéger revient à la plateforme (voir
[Envoyer les lignes à un SIEM](#envoyer-les-lignes-à-un-siem)).

### Format des journaux

`MCP_LOG_FORMAT` choisit le format de chaque ligne.

- `text`, le défaut en 0.8, garde les lignes des versions précédentes : `NIVEAU:logger:message`.
  La ligne d'audit est le message de `mcp.audit` ; elle sort donc sous la forme
  `INFO:mcp.audit:{...}`.
- `json` écrit chaque ligne comme un objet JSON, sans rien devant : `ts` (UTC, ISO 8601 à la
  milliseconde), `level`, `logger`, puis `msg` pour une ligne technique, ou les champs de l'audit
  au premier niveau. Les lignes d'uvicorn prennent le même format. Une exception qui accompagne
  une ligne technique est dans `exc`, sur la même ligne.

Chaque ligne d'audit porte son propre `ts` dans les deux formats : une ligne gardée sans son
collecteur dit encore quand elle a été écrite. Une version mineure ultérieure pourra faire de
`json` le défaut en HTTP ; [MIGRATING.md](../../MIGRATING.md) le dira d'abord.

### Un appel de tool

Chaque appel de tool, ceux des plugins compris, écrit une ligne. En voici une réelle, en `json`,
en mode `service` derrière une vérification du token, avec `fingerprint-audit` et `trino-tags`,
mise sur plusieurs lignes pour la lecture (le serveur l'écrit sur une seule) ; les champs après
`query_ids` sont ceux du module :

```json
{
  "ts": "2026-11-28T16:15:18.261Z",
  "level": "INFO",
  "logger": "mcp.audit",
  "event": "tool_call",
  "request_id": "preuve-087-appel",
  "tool": "execute_query",
  "subject": "alice",
  "client_id": "agent-essai",
  "mcp_client": "mcp/0.1.0",
  "client_ip": "198.51.100.7",
  "identity_mode": "service",
  "trino_user": "A12345",
  "ok": true,
  "error": "",
  "duration_ms": 1278,
  "query_ids": ["20261128_161517_00302_qyb4k"],
  "token_id": "jti-essai-1",
  "sql_sha256": "047c266c14197de8cb2a77b806c37d62a5f3dd8459328cd67a2c8691b79433f2",
  "tables": ["tpch.tiny.nation"],
  "rows": 3,
  "truncated": false,
  "metadata_served": []
}
```

| Champ | Sens |
|---|---|
| `ts`, `event` | quand la ligne a été écrite (la fin de l'appel), et `tool_call` |
| `request_id` | le `X-Request-Id` envoyé par la passerelle placée devant le serveur, quand c'est un identifiant simple (lettres, chiffres, `.`, `_`, `:`, `-`, 128 caractères au plus), ou celui que le serveur a créé ; chaque réponse de l'endpoint MCP le renvoie. Un appel en stdio reçoit le sien |
| `tool` | le tool appelé |
| `subject` | l'appelant vérifié, d'après `MCP_JWT_USER_CLAIM` ; vide quand aucun token n'a été vérifié (`MCP_AUTH_ENABLED=false`, stdio sans `MCP_USER_TOKEN`) |
| `client_id` | l'application à qui le token a été délivré, le premier claim de `MCP_JWT_CLIENT_CLAIMS` que porte le token (`azp`, puis `client_id`) ; vérifié avec le token, vide sans token |
| `mcp_client` | `nom/version` du client MCP tel qu'il s'est déclaré à l'initialisation (`clientInfo`), coupés à 64 et 32 caractères, en ASCII imprimable seulement. Déclaré, jamais vérifié : n'importe quel client peut écrire n'importe quel nom. Pour savoir quelle application détient le token, lisez `client_id` |
| `client_ip` | l'adresse du client : celle du pair, ou derrière `MCP_TRUSTED_PROXIES`, celle que donne leur `X-Forwarded-For` (voir [L'adresse du client](#ladresse-du-client)) ; vide en stdio |
| `identity_mode` | `TRINO_IDENTITY_MODE` : comment le serveur joint Trino |
| `trino_user` | l'utilisateur que Trino a vu pour les requêtes de l'appel : `TRINO_USER` en `service`, l'appelant en `jwt` et `impersonation`, l'utilisateur que Trino a nommé en `oauth2` ; vide quand l'appel n'a lancé aucune requête |
| `ok`, `error` | le résultat ; `error` est un code, jamais un message, parce qu'un message de Trino peut citer une valeur lue dans une ligne (`Cannot cast '<value>' to INT`) : le type et le nom de l'erreur de Trino pour une erreur Trino (`USER_ERROR:INVALID_CAST_ARGUMENT`), la classe de toute autre exception (`TimeoutError`, `CallTimeout` au-delà du budget de l'appel), le code qu'un tool donne à son propre refus (`read_only`, `invalid_argument`, `sql_too_long`, `sql_too_nested`, `parse_deadline`, `parse_no_worker`, `parse_busy`, `parse_failed`, `result_columns_too_long`) ou un plugin avec `note(error=...)`, sinon `tool_error`. L'agent, lui, lit le message entier |
| `duration_ms` | la durée de l'appel |
| `query_ids` | les identifiants que Trino a donnés aux requêtes de l'appel, dans l'ordre, celui d'une requête en échec compris : la clé de l'historique de Trino (`system.runtime.queries`, `/v1/query`, les event listeners). Jusqu'à `MCP_AUDIT_MAX_NAMES` ; au-delà, `query_ids_cut` dit combien il y en avait de plus |

La ligne n'a aucun champ qui pourrait contenir le bearer ou le texte SQL. Ce qu'a fait un appel
(le SHA-256 de son SQL, les tables, les lignes, si elles ont été coupées) est gardé dans son
enregistrement pendant qu'il tourne (`akko_mcp_trino.audit.note`) ;
[`fingerprint-audit`](modules.md#fingerprint-audit) l'ajoute à la ligne, après les champs du cœur.
Chaque liste de l'enregistrement garde `MCP_AUDIT_MAX_NAMES` noms (200 par défaut) et compte les
autres, une fois chacun, à coût constant : une recherche sur mille catalogs n'écrit jamais une
ligne sans limite. Un champ qu'ajoute une extension ne prend jamais la place d'un champ du cœur :
il est retiré et compté dans `extra_dropped`.

### Une requête refusée

Chaque requête refusée avant qu'un tool ne tourne écrit une ligne, avec `"event":"refused"`, et
compte dans `mcp_trino_request_refusals_total{reason}`. Des lignes réelles, une requête sans token
et un token signé par une autre clé :

```json
{"ts":"2026-11-28T16:15:18.547Z","level":"INFO","logger":"mcp.audit","event":"refused","request_id":"preuve-087-sans-jeton","reason":"missing_token","status":401,"client_ip":"172.19.0.9","subject":"","client_id":""}
{"ts":"2026-11-28T16:15:18.552Z","level":"INFO","logger":"mcp.audit","event":"refused","request_id":"preuve-087-forge","reason":"bad_signature","status":401,"client_ip":"172.19.0.9","subject":"","client_id":""}
```

| Champ | Sens |
|---|---|
| `ts`, `event` | quand, et `refused` |
| `request_id` | comme pour un appel ; le client l'a reçu dans `X-Request-Id` |
| `reason` | pourquoi, un code stable du tableau ci-dessous |
| `status` | le statut HTTP de la réponse ; `0` en stdio |
| `client_ip` | comme pour un appel |
| `subject`, `client_id` | ceux d'un token vérifié (un refus après le token, par une extension ou par le transport) ; vides sinon. Un token que le serveur n'a pas pu vérifier ne nomme personne, et aucun de ses claims n'est écrit |

| Raison | Statut | Quand |
|---|---|---|
| `missing_token` | 401 | aucun bearer, avec `MCP_AUTH_REQUIRED=true` |
| `invalid_token` | 401 | un bearer qui ne se lit pas comme un JWT, ou tout autre échec de la vérification |
| `bad_signature` | 401 | signé pour une clé du JWKS, mais la signature ne correspond pas |
| `unknown_key` | 401 | le JWKS a été lu, et aucune de ses clés ne correspond au `kid` du token (ou le token n'en nomme aucun) |
| `jwks_unreachable` | 401 | la dernière lecture du JWKS a échoué, et le serveur ne détient aucune clé pour le `kid` du token |
| `wrong_algorithm` | 401 | un algorithme hors de `MCP_JWT_ALGORITHMS`, ou autre que celui que déclare sa clé |
| `wrong_audience` | 401 | aucune audience que ce serveur accepte, ou l'audience des tokens de Trino (`TRINO_TOKEN_EXCHANGE_AUDIENCE`) |
| `wrong_issuer` | 401 | un autre issuer que `MCP_OIDC_ISSUER` |
| `token_expired`, `token_not_yet_valid` | 401 | après `exp`, ou avant `nbf` ou `iat`, au-delà de `MCP_JWT_LEEWAY_SECONDS` |
| `missing_claim` | 401 | pas d'`exp`, d'`iss` ou d'`aud` |
| `no_user_claim` | 401 | pas de `MCP_JWT_USER_CLAIM` utilisable |
| la raison d'une extension | la sienne | `client_not_allowed`, `insufficient_scope` avec [`token-policy`](modules.md#token-policy) ; une raison qui n'est pas un mot court en minuscules est écrite et comptée comme `extension_refused` |
| `host_not_allowed` | 421 | un `Host` hors de `MCP_ALLOWED_HOSTS` |
| `origin_not_allowed` | 403 | un en-tête `Origin` alors que `MCP_ALLOWED_HOSTS` est posé |
| `payload_too_large` | 413 | un corps au-delà de la limite du SDK |
| `session_invalid` | 404 | une session MCP que le serveur ne connaît pas (celle d'un autre réplica, ou expirée) |
| `not_found` | 404 | un chemin que le serveur ne sert pas |
| `bad_request`, `method_not_allowed`, `not_acceptable`, `unsupported_media_type`, `conflict` | 400, 405, 406, 415, 409 | ce que refuse le transport MCP : un corps illisible, une méthode, un `Accept` ou un `Content-Type` qu'il ne sert pas |

Les refus du middleware portent aussi `X-Reason` pour le client (`unauthenticated` pour ceux du
cœur, la raison de l'extension sinon) ; la `reason` de l'audit est plus fine. Un tool qui refuse
une instruction (`read_only`) ou que Trino refuse (`USER_ERROR:PERMISSION_DENIED`) est un appel de
tool avec `"ok":false`, pas un refus. En stdio, un `MCP_USER_TOKEN` absent ou invalide et le refus
d'une extension à l'admission sont écrits avec `status` 0 avant que le serveur ne s'arrête ; en
mode `oauth2`, une admission refusée sur un appel est écrite aussi. Les requêtes au port de santé
(`/health`, `/ready`, `/metrics`) ne sont pas auditées.

### L'adresse du client

Sans `MCP_TRUSTED_PROXIES`, `client_ip` est l'adresse du pair de la connexion TCP, quoi que dise
`X-Forwarded-For` : un en-tête écrit par un client n'est jamais pris pour une adresse. Derrière un
ingress, un routeur ou une passerelle, posez `MCP_TRUSTED_PROXIES` à leurs adresses, en CIDR
(chart : `server.trustedProxies`). Le serveur lit alors `X-Forwarded-For` depuis la droite, saute
les proxys de confiance et garde la première adresse qui n'en est pas un ; une entrée illisible
arrête la lecture au dernier proxy de confiance.

### Rejoindre l'historique de Trino

| Mode | Jointure |
|---|---|
| tous les modes | `query_ids` de la ligne = `query_id` dans l'historique de Trino ; `trino_user` = le `user` de Trino ; `source` = `MCP_SERVER_NAME` ou `TRINO_SOURCE` |
| avec `trino-tags` | chaque requête porte `request_id=<id>` et `tool=<tool>` dans ses client tags : le `request_id` de la ligne est dans les `clientTags` de Trino |
| avec `fingerprint-audit` | `sql_sha256` = le SHA-256 du texte que garde Trino |

Ce que Trino a gardé de l'appel ci-dessus, et d'un second appel de la même requête que Trino a
refusé (`/v1/query`) :

```text
20261128_161517_00302_qyb4k user=A12345 source=trino-mcp clientTags=['request_id=preuve-087-appel', 'tool=execute_query'] FINISHED
20261128_161518_00303_qyb4k user=A12345 source=trino-mcp clientTags=['request_id=preuve-087-appel', 'tool=execute_query'] FAILED PERMISSION_DENIED
```

En mode `service`, Trino voit `TRINO_USER` pour tous les appelants : le `subject` et le
`client_id` de la ligne sont la seule trace de qui a appelé. Gardez les lignes d'audit aussi
longtemps que l'historique de Trino.

### Envoyer les lignes à un SIEM

- Posez `MCP_LOG_FORMAT=json` : chaque ligne est un objet JSON, qu'un collecteur (Fluent Bit,
  Vector, l'OpenTelemetry Collector, celui de la plateforme) lit sans analyseur. Dirigez le logger
  `mcp.audit`, ou les lignes qui ont un champ `event`, vers l'index d'audit.
- `ts` est l'horloge du serveur, en UTC. Synchronisez les nœuds sur une source de référence
  documentée (NTP) : une piste d'audit ne vaut pas mieux que son horloge.
- L'intégrité des lignes (stockage à écriture unique, signé ou haché) et leur durée de
  conservation reviennent à la plateforme, selon la politique de l'organisation : le serveur écrit
  sur stderr et ne garde rien. Un collecteur qui perd des lignes les perd en silence ; surveillez
  ses propres métriques.
- Alertez sur le débit de `mcp_trino_request_refusals_total` par raison : une rafale de
  `bad_signature` ou de `wrong_audience` est un token forgé ou présenté au mauvais serveur, une
  rafale de `client_not_allowed` une application qui ne devrait pas appeler.
- Ces lignes répondent à ce que les textes demandent d'une piste d'audit : qui, quand, d'où, quoi,
  avec quel résultat, et les tentatives d'accès en échec. Le règlement (UE) 2022/2554 (DORA), par
  le règlement délégué (UE) 2024/1774 de la Commission, article 12, demande la journalisation des
  événements, des accès logiques et des identités, et une horloge synchronisée sur une référence
  documentée ([EUR-Lex](https://eur-lex.europa.eu/eli/reg_del/2024/1774/oj)) ; l'OWASP
  [MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)
  et l'OWASP [MCP Top 10](https://owasp.org/www-project-mcp-top-10/) (MCP08, absence d'audit et de
  télémétrie) demandent de journaliser chaque appel de tool avec son utilisateur et son heure. La
  conformité est celle du déploiement, pas d'un composant.

### Profils

| Réglage | Test (un poste, stdio, `service`) | Production (HTTP, la chart) |
|---|---|---|
| `MCP_LOG_FORMAT` | `text`, stderr gardé par l'hôte | `json`, lu par le collecteur du cluster |
| `MCP_EXTENSIONS` | `fingerprint-audit,trino-tags` conseillés | `token-policy,fingerprint-audit,trino-tags` (`token-policy` quand le fournisseur d'identité donne des scopes) |
| `MCP_TRUSTED_PROXIES` | aucun | le contrôleur d'ingress ou le routeur ; la bordure pose `X-Request-Id` |
| identité | `service`, annoncé au démarrage | `jwt` ou `impersonation`, porte stricte ; `service` seulement pour un compte technique nommé |
| port de santé | aucun (stdio) | `networkPolicy.enabled: true`, `/metrics` ouvert à Prometheus seul |
| OpenTelemetry | aucun | aucun, ou un SDK que la plateforme installe exprès (voir plus bas) |

Dans la chart, le profil de production s'écrit :

```yaml
server:
  logFormat: json
  extensions: [token-policy, fingerprint-audit, trino-tags]
  trustedProxies: ["10.128.0.0/14"]   # les pods du routeur ou du contrôleur d'ingress
networkPolicy:
  enabled: true
```

### Télémétrie et OpenTelemetry

Le serveur n'envoie rien à son éditeur ni à aucun tiers : il parle à Trino, au fournisseur
d'identité (JWKS, échange de token) et aux fournisseurs de contexte qu'on lui a configurés, rien
d'autre.

Le SDK MCP sur lequel il repose enveloppe chaque message reçu dans un span OpenTelemetry, par le
seul paquet `opentelemetry-api`. Sans SDK OpenTelemetry installé dans l'environnement, l'API ne
fait rien et aucun span ne sort du processus : c'est l'image telle qu'elle est publiée. Quand la
plateforme installe et configure un SDK (un agent d'auto-instrumentation et ses variables
`OTEL_*`), ces spans partent vers l'exportateur qu'elle configure. Ils portent `mcp.method.name`,
`mcp.protocol.version`, `jsonrpc.request.id` et, pour un appel de tool, `gen_ai.operation.name`
(`execute_tool`) et `gen_ai.tool.name` ; ni SQL ni token. Une exception que le SDK n'attend pas
met son message dans le statut du span, ce que l'audit ne fait jamais : n'installez un SDK que là
où ces messages peuvent aller. Une instrumentation des bibliothèques HTTP clientes, si la
plateforme en ajoute une, trace aussi les requêtes vers Trino.

Un `OTEL_PYTHON_TRACER_PROVIDER` qui nomme un fournisseur qu'aucun paquet installé ne fournit
(une plateforme qui pose les variables OpenTelemetry sur tous les pods) arrêterait le SDK dès son
premier import. Le serveur retire cette variable avant, le dit au démarrage et dans `--check`, et
n'émet aucun span.

## Santé et métriques

Ces endpoints n'existent que sur les transports HTTP, sur le port de santé.

- `/health` ne touche jamais Trino : une sonde de vie ne peut pas tuer le serveur parce que Trino
  est lent.
- Ce que vérifie `/ready` dépend du mode d'identité : en mode `jwt`, le `/v1/info` public de
  Trino ; en modes `impersonation` et `service`, un `SELECT 1` borné sous `TRINO_USER`, au nom de
  personne d'autre. Les requêtes de la sonde ne sont comptées ni dans `mcp_trino_queries_total`,
  ni dans ses voisines. Quand le contrôle échoue, `/ready` rend un `503` avec
  `{"status": "error", "reason": "<code>"}`, jamais l'exception, dont le texte nomme l'adresse de
  Trino : `trino_starting`, `trino_unavailable` (un HTTP 502, 503 ou 504, ou `/v1/info` qui ne
  répond pas 200), `trino_auth_failed`, `trino_permission_denied`, `trino_query_failed`,
  `trino_http_error`, `trino_tls_failed`, `trino_timeout`, `trino_unreachable` (un nom qui ne se
  résout pas, une connexion refusée), `identity_refused` ou `trino_error`. L'exception va au
  journal du serveur, une fois à chaque changement de raison, comme le retour à l'état prêt :

  ```json
  {"ts":"2026-11-28T16:15:19.126Z","level":"WARNING","logger":"akko_mcp_trino.health","msg":"not ready: trino_unreachable (TrinoConnectionError: failed to execute: HTTPConnectionPool(host='trino-absent', port=8080): Max retries exceeded with url: /v1/statement (Caused by NameResolutionError(\"HTTPConnection(host='trino-absent', port=8080): Failed to resolve 'trino-absent' ([Errno -2] Name or service not known)\")))"}
  ```
- `/metrics` expose `mcp_trino_queries_total`, `mcp_trino_query_errors_total`,
  `mcp_trino_query_duration_seconds` et `mcp_trino_sql_refusals_total`, les instructions refusées
  avant Trino par `reason` : `too_long`, `too_nested`, `caller_busy`, `no_worker`, `deadline` (une
  analyse coupée par le délai) ou `worker_failed` ; jamais une instruction ni un nom. Il expose
  aussi `mcp_trino_request_refusals_total`, les requêtes refusées avant qu'un tool ne tourne, par
  la `reason` de leur [ligne d'audit](#une-requête-refusée), et `mcp_trino_ready_checks_total`,
  les sondes de disponibilité, par `result` (`ready` ou le code de la raison). Avec des fournisseurs de contexte chargés, il expose aussi,
  par fournisseur, `mcp_trino_context_calls_total`, `mcp_trino_context_errors_total` (par statut),
  `mcp_trino_context_call_duration_seconds`, `mcp_trino_context_cache_lookups_total` (trouvé ou
  non), `mcp_trino_context_cache_bytes` et `mcp_trino_context_breaker_open`. Un appel que
  l'outil cesse d'attendre est compté comme s'il avait fini, chaque source lue étant `partial` ou
  `unavailable` ; un arrêt du serveur pendant une lecture en compte ainsi quelques-uns. Avec la
  vérification des tokens, il expose `mcp_trino_jwks_fetches_total` (par `result`, `ok` ou
  `failed`) et `mcp_trino_jwks_up`, à 0 après une lecture du JWKS en échec
  ([Sécurité](security.md#authentification)).

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
