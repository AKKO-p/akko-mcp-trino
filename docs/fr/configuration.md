# Configuration

[English](../en/configuration.md) · **Français**

Le serveur lit chaque réglage dans l'environnement ; rien n'est écrit en dur. Une extension lit
ses propres variables dans le même environnement. `akko-mcp-trino --check` affiche la
configuration effective, jamais un secret. Une « erreur de démarrage » ci-dessous veut dire que le
serveur refuse de démarrer et dit pourquoi.

## Trino

| Variable | Rôle | Défaut |
|---|---|---|
| `TRINO_HOST`, `TRINO_PORT` | le coordinateur | `localhost`, `8080` |
| `TRINO_USER` | le compte Trino propre au serveur. En mode `service`, chaque requête s'exécute sous ce compte ; en mode `impersonation`, il s'authentifie et agit au nom de l'appelant ; ignoré en mode `oauth2` | `trino` |
| `TRINO_PASSWORD` | son mot de passe, envoyé par authentification Basic ; refusé en `http`. `--check` dit si un mot de passe est posé, jamais lequel | aucun |
| `TRINO_CLIENT_CERT`, `TRINO_CLIENT_KEY` | un certificat client et sa clé (chemins PEM) avec lesquels le compte s'authentifie à la place (mTLS) ; https seulement | aucun |
| `TRINO_CATALOG` | le catalog de session, où un nom sans catalog est lu. Réglez-le sur un catalog qui contient vos données, comme `iceberg` : avec `system`, de tels noms sont refusés | `system` |
| `TRINO_READ_ONLY` | refuser les écritures dans `execute_query` | `true` |
| `MCP_EXTRA_READ_COMMANDS` | commandes de lecture refusées par défaut, ouvertes une à une, séparées par des virgules : `EXPLAIN` (ouvre aussi le plan dans `explain_query`), `SHOW STATS`, `SHOW CREATE`, `SHOW GRANTS`, `SHOW ROLES` (avec `SHOW CURRENT ROLES` et `SHOW ROLE GRANTS`), `SHOW SESSION`. Lisez [Sécurité](security.md#les-instructions-quun-agent-peut-envoyer) avant d'en ouvrir une. Annoncé à chaque démarrage et à chaque MCP host ; un nom inconnu est une erreur de démarrage | aucune |
| `TRINO_SOURCE` | la source que Trino enregistre pour chaque requête (`X-Trino-Source`) | le nom du serveur |
| `TRINO_HTTP_SCHEME` | `http` ou `https` ; un mot de passe ne circule jamais en `http` | `http` |
| `TRINO_VERIFY` | vérification TLS : `true`, `false`, ou le chemin d'un paquet d'autorités avec lequel la chaîne TLS de Trino est vérifiée | `true` |
| `TRINO_REQUEST_TIMEOUT_SECONDS` | délai d'une requête HTTP du client Trino | `30` |
| `TRINO_IDENTITY_MODE` | qui est le serveur pour Trino : `jwt`, `impersonation`, `oauth2` ou `service` (voir [Identité](identity.md)). Une extension peut en ajouter d'autres ; un mode inconnu est une erreur de démarrage | `jwt` |
| `TRINO_TOKEN_EXCHANGE_URL` | `jwt` : l'endpoint de token de l'identity provider (https seulement) | aucune (obligatoire en `jwt`) |
| `TRINO_TOKEN_EXCHANGE_CLIENT_ID`, `TRINO_TOKEN_EXCHANGE_CLIENT_SECRET` | `jwt` : le client confidentiel de ce serveur auprès de l'identity provider, envoyé par authentification Basic. `--check` n'affiche jamais le secret | aucun (obligatoires en `jwt`) |
| `TRINO_TOKEN_EXCHANGE_AUDIENCE` | `jwt` : l'audience du token demandé pour Trino, celle que nomme le `required-audience` de Trino ; jamais une audience que ce serveur accepte | aucune (obligatoire en `jwt`) |
| `TRINO_TOKEN_EXCHANGE_SCOPE` | `jwt` : les portées à demander avec ce token, séparées par des espaces | aucune |
| `TRINO_TOKEN_EXCHANGE_VERIFY` | vérification TLS de l'endpoint de token : `true`, `false`, ou le chemin d'un paquet d'autorités | `true` |

## Bornes et budgets de temps

| Variable | Rôle | Défaut |
|---|---|---|
| `TRINO_MAX_ROWS` | plafond de résultat, au moins 1. Le serveur lit une ligne de plus pour dire si la réponse a été coupée (`truncated`). C'est aussi la taille d'une page de liste | `100` |
| `TRINO_QUERY_TIMEOUT_SECONDS` | budget de temps d'une requête : au-delà, le serveur annule la requête dans Trino. `0` laisse la limite à Trino | `60` |
| `MCP_CALL_TIMEOUT_SECONDS` | budget de temps d'un appel de tool, toutes ses requêtes comprises. Au-delà, aucune requête ne démarre, la requête en cours est annulée et l'agent reçoit une erreur. `0` ne pose aucun budget | `120` |

## Listes et recherche

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_MAX_CATALOGS` | nombre maximal de catalogs que `list_catalogs` lit avant de les paginer, `system` à part ; au moins 1. Au-delà, le tool rend une erreur, jamais une liste coupée | `10000` |
| `MCP_DESCRIBE_SCHEMA_TABLES` | nombre maximal de tables d'une page de `describe_schema`, de 1 à 100. Une page lit chacune de ses tables par sa propre branche, donc son coût grandit avec la page | `20` |
| `MCP_DESCRIBE_SCHEMA_COLUMNS` | nombre maximal de colonnes d'une page de `describe_schema`, au moins 1. La première table d'une page vient entière si large soit-elle, et une page raccourcie pour ce budget le dit | `1000` |
| `MCP_DESCRIBE_MAX_COLUMNS` | nombre maximal de colonnes d'une table que `describe_table` lit, page par page, de 1 à 100 000. Au-delà, la réponse dit `columns_cut` | `5000` |
| `MCP_SEARCH_MIN_LENGTH` | le mot le plus court que cherche `search_columns`, au moins 1. Les mots plus courts sont ignorés, et un texte où il n'en reste aucun est refusé | `3` |
| `MCP_SEARCH_MAX_WORDS` | nombre maximal de mots d'un texte de `search_columns`, au moins 1. Au-delà, la recherche est refusée avant d'appeler Trino | `8` |
| `MCP_SEARCH_MAX_WORD_LENGTH` | le mot le plus long qu'accepte `search_columns`, pas en dessous de `MCP_SEARCH_MIN_LENGTH`. Un mot plus long est refusé avant d'appeler Trino | `64` |
| `MCP_SEARCH_CONCURRENCY` | catalogs qu'un appel de `search_columns` lit à la fois, de 1 à 32 : autant de requêtes de métadonnées en cours dans Trino pour cet appel. Le monter au-delà de 8 ajoute surtout de la charge de métadonnées sur Trino, pour peu de gain. | `8` |
| `MCP_SEARCH_MAX_THREADS` | nombre maximal de lectures de `search_columns` en cours dans les threads d'un processus, tous appels confondus, de 1 à 256. Un appel qui ne trouve aucun thread libre lit lui-même son catalog suivant | `32` |
| `MCP_SEARCH_RESERVE_SECONDS` | secondes du budget de l'appel que `search_columns` garde en réserve, pour rendre ce qu'il a parcouru avant la fin de l'appel. 0 ou plus, en dessous de `MCP_CALL_TIMEOUT_SECONDS` quand il est posé. `0` déclenche un avertissement | `2` |

## Identité

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_AUTH_ENABLED` | vérifier les bearer tokens des appelants (la vérification du token) | `true` |
| `MCP_AUTH_REQUIRED` | refuser avec un `401` une requête sans token valide. En modes `jwt` et `impersonation`, le serveur refuse de démarrer sans lui. En mode `oauth2`, ajoute un contrôle de token MCP et ne désactive jamais la connexion à Trino | `true` |
| `MCP_JWKS_URL` | l'endpoint JWKS de l'identity provider | aucune (obligatoire quand l'authentification est active) |
| `MCP_OIDC_ISSUER` | l'issuer qu'un token doit porter | aucun (obligatoire quand l'authentification est active ; vide, chaque token est refusé) |
| `MCP_OIDC_AUDIENCE` | l'audience qu'un token doit porter : celle que l'identity provider donne aux tokens émis pour ce serveur (RFC 8707) ; jamais celle de Trino | aucune (celle-ci ou `MCP_RESOURCE_URL` est obligatoire quand l'authentification est active) |
| `MCP_RESOURCE_URL` | l'URL canonique de ce serveur, comme `https://mcp.example.com/mcp`. Elle est acceptée comme audience, et nommée dans les métadonnées de ressource protégée que le serveur publie alors (RFC 9728) et dans le challenge d'un `401` | aucune |
| `MCP_JWT_LEEWAY_SECONDS` | tolérance sur `exp`, `nbf` et `iat` pour le décalage d'horloge entre l'issuer et ce serveur | `30` |
| `MCP_JWT_USER_CLAIM` | le claim qui nomme l'utilisateur, tel que vos règles d'accès le connaissent. Keycloak, Entra ID, Okta et Dex mettent un identifiant opaque dans `sub` : nommez un autre claim, comme `preferred_username`, `upn` ou `uid`. Un token sans ce claim est refusé ; aucun autre claim n'est essayé | `sub` |
| `MCP_JWT_ALGORITHMS` | les algorithmes de signature que peut utiliser le token d'un appelant, séparés par des virgules, parmi `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES384` et `ES512` (par exemple `RS256,RS512` pour un identity provider qui signe avec l'un ou l'autre). Un algorithme à secret partagé (comme `HS256`) ou `none` est une erreur de démarrage | `RS256` |
| `MCP_JWT_CLIENT_CLAIMS`, `MCP_JWT_SCOPE_CLAIMS` | les claims qui nomment l'application qui a demandé le token, et ceux qui portent ses portées, séparés par des virgules, dans l'ordre : le premier que porte le token l'emporte. Un claim de portées est une chaîne séparée par des espaces ou une liste de chaînes ; toute autre valeur ne donne aucune portée. Keycloak : les défauts ; Entra ID : `azp` ou `appid` selon la version du token, et `scp` ; Okta : `cid` et `scp` (voir [Modules](modules.md)) | `azp,client_id`, `scope` |
| `MCP_ALLOWED_HOSTS` | filtre de l'en-tête Host (la protection contre le DNS rebinding du SDK) : des entrées `host` ou `host:port`, séparées par des virgules, `host:*` pour tout port (le seul joker). Une fois le filtre actif, une requête qui porte un en-tête `Origin` est refusée (403). Vide, il sert tout Host | aucun (désactivé) |

[Identité](identity.md#la-vérification-du-token) dit quelles combinaisons empêchent le serveur de
démarrer.

## Exécution du serveur

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http`, `sse` ou `stdio` | `streamable-http` |
| `MCP_PORT`, `MCP_HEALTH_PORT` | ports d'écoute, transports HTTP seulement | `3000`, `3001` |
| `MCP_SERVER_NAME` | nom annoncé aux MCP hosts | `trino-mcp` |
| `MCP_USER_TOKEN` | stdio seulement : le token d'accès de l'utilisateur, vérifié comme un en-tête bearer | aucun (obligatoire quand `MCP_AUTH_REQUIRED=true`) |

## Audit

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_AUDIT_MAX_NAMES` | les noms que garde chaque liste de l'enregistrement d'audit d'un appel (ses tables, les tables dont il a servi la description), de 1 à 100 000. Les autres sont comptés, une fois chacun, jamais écrits | `200` |

## Plugins et extensions

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_TOOL_PLUGINS` | noms, séparés par des virgules, des fonctions d'enregistrement installées dans le groupe d'entry points `akko_mcp_trino.tools` | aucun |
| `MCP_EXTENSIONS` | noms, séparés par des virgules, des extensions installées dans le groupe d'entry points `akko_mcp_trino.extensions`, les [modules](modules.md) compris | aucune |

## Modules

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_ALLOWED_CLIENTS` | `token-policy` : les clients qu'un token peut nommer, séparés par des virgules ou des espaces | aucun : tout client |
| `MCP_REQUIRED_SCOPES` | `token-policy` : les portées qu'un token doit toutes porter, séparées par des virgules ou des espaces | aucune |

## Profilage

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_PROFILE_MAX_COLUMNS`, `MCP_PROFILE_SAMPLE_ROWS` | nombre maximal de colonnes que `profile_table` profile en un appel (de 1 à 500), et lignes que lit son mode `sample` (au moins 1) | `50`, `100000` |
| `MCP_PROFILE_MODES` | les modes que `profile_table` permet, séparés par des virgules, parmi `sample`, `bernoulli` et `full`. Vide ou un nom inconnu est une erreur de démarrage | `sample,bernoulli,full` |
| `MCP_PROFILE_VALUE_CHARS` | les caractères d'un `min` ou d'un `max` texte qu'il rend, ou les octets d'un binaire. Trino en lit un de plus et pas davantage, et une valeur plus longue est coupée. Au moins 1 | `200` |
| `MCP_PROFILE_TYPE_CHARS` | les caractères du type d'une colonne qu'il rend, de 1 à 100 000. Un type plus long est coupé, avec `"type"` dans `cut`. `MCP_PROFILE_VALUE_CHARS` ne coupe jamais un type | `500` |
| `MCP_PROFILE_DESCRIBE_ROWS` | les lignes de son `DESCRIBE` qu'il lit, au-delà du plafond de lignes, au moins 1. Une colonne au-delà ne peut pas être nommée | `5000` |
| `MCP_PROFILE_CLIENT_TAG` | l'étiquette client de sa requête d'agrégats (pas de son `DESCRIBE`), pour un groupe de ressources de Trino : de 1 à 100 lettres, chiffres et `_ . : = / -` | `mcp-profile` |
| `MCP_PROFILE_TIMEOUT_SECONDS` | budget de temps de la requête d'agrégats ; Trino annule la requête quand il est épuisé. Le budget effectif est la plus petite valeur non nulle parmi ce réglage, `TRINO_QUERY_TIMEOUT_SECONDS` et `MCP_CALL_TIMEOUT_SECONDS`. `0` veut dire « prendre les deux autres ». Une valeur non nulle ne doit dépasser aucun des deux quand ils sont posés. `--check` affiche le budget effectif | `0` |
| `MCP_PROFILE_MAX_CONCURRENT` | les appels de `profile_table` qu'un processus du serveur exécute à la fois, de 1 à 64. Au-delà, un appel rend aussitôt `"kind": "busy"` et n'envoie aucune requête | `4` |

## Contexte d'un catalogue de données

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_CONTEXT_PROVIDERS` | noms, séparés par des virgules, des fournisseurs installés dans le groupe d'entry points `akko_mcp_trino.context` ; un nom inconnu est une erreur de démarrage | aucun |
| `MCP_CONTEXT_TIMEOUT_SECONDS` | budget de temps d'une lecture de contexte, strictement positif | `5` |
| `MCP_CONTEXT_RESERVE_SECONDS` | secondes du budget d'un appel gardées en réserve, pour que le contexte finisse avant l'appel. Au moins 0,3 quand un fournisseur est chargé (le serveur refuse moins), 0 ou plus sinon | `0.5` |
| `MCP_CONTEXT_MAX_CONCURRENCY` | nombre maximal de lectures de contexte en cours pour un fournisseur, au moins 1 | `32` |
| `MCP_CONTEXT_RETRY_AFTER_SECONDS` | délai avant de rappeler une source en panne, 0 ou plus | `30` |
| `MCP_CONTEXT_REFUSED_RETRY_SECONDS` | délai avant de rappeler une source qui a refusé le compte (`SIGHUP` le lève aussitôt), 0 ou plus | `3600` |
| `MCP_CONTEXT_BATCH` | les tables que contient une lecture d'une source, au moins 1 | `50` |
| `MCP_CONTEXT_TTL_SECONDS` | durée de conservation d'une lecture de contexte, 0 ou plus | `900` |
| `MCP_CONTEXT_CACHE_BYTES`, `MCP_CONTEXT_TERMS_CACHE_BYTES` | les limites des caches des tables et des termes de glossaire, au moins 1. Chaque limite compte la longueur de la forme JSON des entrées gardées. Le processus en tient environ trois à quatre fois plus en mémoire : dimensionnez le pod en conséquence | `134217728`, `16777216` |
| `MCP_VISIBILITY_TTL_SECONDS` | durée de conservation de ce que Trino a listé pour un appelant, 0 ou plus | `60` |
| `MCP_VISIBILITY_CACHE_BYTES`, `MCP_VISIBILITY_MAX_NAMES` | la limite de ce cache, et le nombre maximal de noms gardés pour un appelant, au moins 1. Chaque entrée compte sa clé comme sa valeur. Mesuré avec des chaînes neuves, le processus en tient de 3,6 à 8 fois plus en mémoire : dimensionnez le pod pour huit fois plus | `33554432`, `10000` |
| `MCP_CONTEXT_REQUIRE_SELECT` | `true` : `describe_table` ne sert le contexte que si Trino valide un SELECT des colonnes qu'il liste. `describe_schema` et les plugins ne sont pas concernés (voir [Contexte d'un catalogue de données](catalog-context.md)) | `false` |
| `MCP_CONTEXT_CANARY` | une table, `catalog.schema.table`, qu'aucun appelant de ce serveur ne doit voir : le contexte n'est servi à un appelant que tant que Trino la lui cache. Son absence déclenche un avertissement quand un fournisseur est chargé | aucune |
| `MCP_CONTEXT_TEXT_CHARS`, `MCP_CONTEXT_RESPONSE_CHARS`, `MCP_CONTEXT_MAX_COLUMNS` | le texte le plus long d'un champ de contexte, le contexte le plus long d'une réponse, et le nombre maximal de colonnes décrites dans une table, au moins 1 | `500`, `60000`, `500` |
| `MCP_VISIBILITY_ENABLED` | supprimé en 0.8 : le filtre par les droits de Trino ne se désactive pas, et `false` est une erreur de démarrage | aucune |

Les variables propres au fournisseur `file` sont dans
[Contexte d'un catalogue de données](catalog-context.md#le-fournisseur-file).
