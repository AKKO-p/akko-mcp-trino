# Configuration

[English](../en/configuration.md) · **Français**

Tout se lit dans l'environnement. Rien n'est écrit en dur. Une extension lit ses propres variables
dans le même environnement. `akko-mcp-trino --check` affiche la configuration effective, jamais un
secret.

## Trino

| Variable | Rôle | Défaut |
|---|---|---|
| `TRINO_HOST`, `TRINO_PORT` | le coordinateur | `localhost`, `8080` |
| `TRINO_USER` | le compte Trino propre au serveur : chaque requête s'exécute sous ce compte en mode `service` ; en mode `impersonation`, il s'authentifie et agit au nom de l'appelant ; ignoré en mode `oauth2` | `trino` |
| `TRINO_PASSWORD` | son mot de passe, envoyé par authentification Basic ; refusé en `http` ; `--check` dit si un mot de passe est posé, jamais lequel | aucun |
| `TRINO_CLIENT_CERT`, `TRINO_CLIENT_KEY` | un certificat client et sa clé (chemins PEM) avec lesquels le compte s'authentifie à la place (mTLS) ; https seulement | aucun |
| `TRINO_CATALOG` | catalog de session, où un nom sans catalog est lu ; à régler sur un catalog de données (avec `system`, de tels noms sont refusés) | `system` |
| `TRINO_READ_ONLY` | refuser les écritures dans `execute_query` | `true` |
| `MCP_EXTRA_READ_COMMANDS` | commandes de lecture que la liste blanche refuse, ouvertes une à une, séparées par des virgules : `EXPLAIN` (et le plan dans `explain_query`), `SHOW STATS`, `SHOW CREATE`, `SHOW GRANTS`, `SHOW ROLES` (avec `SHOW CURRENT ROLES` et `SHOW ROLE GRANTS`), `SHOW SESSION`. Trino y répond au-delà d'un row filter ou d'un column mask : n'en ouvrir une que là où aucun row filter ni aucun column mask ne doit tenir ; annoncé à chaque démarrage et à chaque MCP host ; un nom inconnu empêche le démarrage | aucune |
| `TRINO_SOURCE` | la source que Trino enregistre pour chaque requête (`X-Trino-Source`) | le nom du serveur |
| `TRINO_HTTP_SCHEME` | `http` ou `https` ; un mot de passe ne circule jamais en `http` | `http` |
| `TRINO_VERIFY` | vérification TLS : `true`, `false`, ou le chemin du paquet d'autorités avec lequel la chaîne TLS de Trino est vérifiée | `true` |
| `TRINO_REQUEST_TIMEOUT_SECONDS` | délai par requête HTTP du client Trino | `30` |
| `TRINO_IDENTITY_MODE` | qui est le serveur pour Trino : `jwt` (l'appelant, par échange de token), `impersonation` (l'appelant, au nom duquel agit `TRINO_USER`), `oauth2` (connexion navigateur native, en stdio seulement, vérifiée par la première requête qui n'est pas de la découverte) ou `service` (`TRINO_USER`, annoncé par un avertissement) ; tous sauf `service` exigent https ; `jwt` et `impersonation` exigent aussi un token MCP valide ; une extension peut en ajouter d'autres, et un mode inconnu empêche le démarrage | `jwt` |
| `TRINO_TOKEN_EXCHANGE_URL` | `jwt` : l'endpoint de token de l'IdP (https seulement) | aucune (obligatoire en `jwt`) |
| `TRINO_TOKEN_EXCHANGE_CLIENT_ID`, `TRINO_TOKEN_EXCHANGE_CLIENT_SECRET` | `jwt` : le client confidentiel de ce serveur auprès de l'IdP, envoyé par authentification Basic ; `--check` n'affiche jamais le secret | aucun (obligatoires en `jwt`) |
| `TRINO_TOKEN_EXCHANGE_AUDIENCE` | `jwt` : l'audience du token demandé pour Trino, celle que nomme le `required-audience` de Trino ; jamais une audience que ce serveur accepte | aucune (obligatoire en `jwt`) |
| `TRINO_TOKEN_EXCHANGE_SCOPE` | `jwt` : les portées à demander avec, séparées par des espaces | aucune |
| `TRINO_TOKEN_EXCHANGE_VERIFY` | vérification TLS de l'endpoint de token : `true`, `false`, ou le chemin d'un paquet d'autorités | `true` |

## Bornes et budgets de temps

| Variable | Rôle | Défaut |
|---|---|---|
| `TRINO_MAX_ROWS` | plafond de résultat, 1 au moins ; une ligne de plus est lue pour dire si la réponse a été coupée (`truncated`) ; c'est aussi la taille d'une page de liste | `100` |
| `TRINO_QUERY_TIMEOUT_SECONDS` | budget de temps d'une requête : au-delà, le serveur annule la requête dans Trino ; `0` laisse la limite à Trino | `60` |
| `MCP_CALL_TIMEOUT_SECONDS` | budget de temps d'un appel de tool, toutes ses requêtes comprises : au-delà, aucune requête ne démarre, celle en cours est annulée et l'agent reçoit une erreur ; `0` n'en pose aucun | `120` |

## Listes et recherche

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_MAX_CATALOGS` | le nombre maximal de catalogs que `list_catalogs` lit avant de les paginer, `system` à part, 1 au moins ; au-delà, le tool rend une erreur, jamais une liste coupée | `10000` |
| `MCP_DESCRIBE_SCHEMA_TABLES` | le nombre maximal de tables d'une page de `describe_schema`, de 1 à 100 : une page lit chacune de ses tables par sa propre branche, donc son coût grandit avec la page | `20` |
| `MCP_DESCRIBE_SCHEMA_COLUMNS` | le nombre maximal de colonnes qu'une page de `describe_schema` contient en tout, 1 au moins ; la première table d'une page vient entière si large soit-elle, et une page raccourcie pour ce budget le dit | `1000` |
| `MCP_DESCRIBE_MAX_COLUMNS` | colonnes d'une table que `describe_table` lit au plus, page par page, de 1 à 100000 ; au-delà, la réponse dit `columns_cut` | `5000` |
| `MCP_SEARCH_MIN_LENGTH` | le mot le plus court que `search_columns` cherche, 1 au moins ; les mots plus courts sont ignorés, et un texte où il n'en reste aucun est refusé | `3` |
| `MCP_SEARCH_MAX_WORDS` | le nombre maximal de mots d'un texte de `search_columns`, 1 au moins ; au-delà, la recherche est refusée avant Trino | `8` |
| `MCP_SEARCH_MAX_WORD_LENGTH` | le mot le plus long que prend `search_columns`, pas en dessous de `MCP_SEARCH_MIN_LENGTH` ; un mot plus long est refusé avant Trino | `64` |
| `MCP_SEARCH_CONCURRENCY` | catalogs qu'un appel de `search_columns` lit à la fois, de 1 à 32 : autant de requêtes de métadonnées en cours dans Trino pour cet appel. Le monter au-delà de 8 ajoute surtout de la charge de métadonnées sur Trino, pour peu de gain. | `8` |
| `MCP_SEARCH_MAX_THREADS` | lectures de `search_columns` en cours dans les fils d'un processus, tous appels confondus, de 1 à 256 ; un appel qui n'en trouve aucune de libre lit lui-même son catalog suivant | `32` |
| `MCP_SEARCH_RESERVE_SECONDS` | les secondes du budget de l'appel que `search_columns` garde en réserve, pour rendre ce qu'elle a fouillé avant la fin de l'appel ; 0 ou plus, en dessous de `MCP_CALL_TIMEOUT_SECONDS` quand il est posé ; 0 est signalé | `2` |

## Identité

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_AUTH_ENABLED` | vérifier les tokens bearer des appelants (la porte) | `true` |
| `MCP_AUTH_REQUIRED` | refuser en `401` une requête sans token valide ; `jwt` et `impersonation` refusent de démarrer sans ; en `oauth2`, une porte MCP de plus, jamais un moyen de désactiver la connexion à Trino | `true` |
| `MCP_JWKS_URL` | l'endpoint JWKS de l'IdP | aucune (obligatoire quand l'authentification est active) |
| `MCP_OIDC_ISSUER` | l'émetteur qu'un token doit porter | aucun |
| `MCP_OIDC_AUDIENCE` | l'audience qu'un token doit porter : celle que l'IdP donne aux tokens émis pour ce serveur (RFC 8707) ; jamais celle de Trino | aucune (celle-ci ou `MCP_RESOURCE_URL` est obligatoire quand l'authentification est active) |
| `MCP_RESOURCE_URL` | l'URL canonique de ce serveur, comme `https://mcp.example.com/mcp` : acceptée comme audience, nommée dans les métadonnées de ressource protégée que le serveur publie alors (RFC 9728) et dans le challenge d'un `401` | aucune |
| `MCP_JWT_LEEWAY_SECONDS` | tolérance sur `exp`/`nbf`/`iat` pour le décalage d'horloge entre l'émetteur et ce serveur | `30` |
| `MCP_JWT_USER_CLAIM` | la revendication du token d'un appelant qui nomme l'utilisateur, tel que les règles d'accès le connaissent. `sub` est le standard OpenID Connect : l'identifiant stable et unique que l'émetteur donne à l'utilisateur. Un émetteur dont le `sub` est un identifiant opaque que les règles ne connaissent pas (Keycloak donne un UUID ; Entra ID, Okta et Dex un identifiant opaque aussi) en nomme une autre : `preferred_username`, `upn`, `uid`... Un token qui ne la porte pas est refusé, jamais lu dans une autre revendication | `sub` |
| `MCP_JWT_ALGORITHMS` | les algorithmes de signature que peut utiliser le token d'un appelant, séparés par des virgules, parmi `RS256`, `RS384`, `RS512`, `PS256`, `PS384`, `PS512`, `ES256`, `ES384`, `ES512` (par exemple `RS256,RS512` pour un IdP qui signe avec l'un ou l'autre) ; un algorithme à secret partagé (`HS256`...) ou `none` empêche le démarrage | `RS256` |
| `MCP_JWT_CLIENT_CLAIMS`, `MCP_JWT_SCOPE_CLAIMS` | les revendications qui nomment l'application qui a demandé le token, et celles qui portent ses portées, séparées par des virgules, dans l'ordre : la première que porte le token l'emporte. Une revendication de portées est une chaîne séparée par des espaces ou une liste de chaînes ; toute autre valeur ne donne aucune portée. Keycloak : les défauts ; Entra ID : `azp` ou `appid` selon la version du token, et `scp` ; Okta : `cid` et `scp` (voir [Modules](modules.md)) | `azp,client_id`, `scope` |
| `MCP_ALLOWED_HOSTS` | filtre de l'en-tête Host (la protection contre le DNS rebinding du SDK) : des entrées `host` ou `host:port`, séparées par des virgules, `host:*` pour tout port (le seul joker) ; une fois le filtre actif, une requête qui porte un en-tête `Origin` est refusée (403) ; vide, il sert tout Host | aucun (désactivé) |

L'authentification activée sans URL JWKS, ou sans audience propre à ce serveur, empêche le
démarrage : une couche d'authentification qui ne peut rien vérifier ne doit pas faire semblant. De
même pour une porte qui exige un token qu'elle ne peut pas vérifier
(`MCP_AUTH_REQUIRED=true` avec `MCP_AUTH_ENABLED=false`).

## Service

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http`, `sse` ou `stdio` | `streamable-http` |
| `MCP_PORT`, `MCP_HEALTH_PORT` | ports d'écoute | `3000`, `3001` |
| `MCP_SERVER_NAME` | nom annoncé aux MCP hosts | `trino-mcp` |
| `MCP_USER_TOKEN` | stdio seulement : le token d'accès de l'utilisateur, vérifié comme un en-tête bearer | aucun (obligatoire en mode strict) |

## Plugins et extensions

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_TOOL_PLUGINS` | noms, séparés par des virgules, des fonctions d'enregistrement installées dans le groupe d'entrée `akko_mcp_trino.tools` | aucun |
| `MCP_EXTENSIONS` | noms, séparés par des virgules, des extensions installées dans le groupe d'entrée `akko_mcp_trino.extensions` | aucune |

## Modules

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_ALLOWED_CLIENTS` | `token-policy` : les clients qu'un token peut nommer, séparés par des virgules ou des espaces | aucun : tout client |
| `MCP_REQUIRED_SCOPES` | `token-policy` : les portées qu'un token doit toutes porter, séparées par des virgules ou des espaces | aucune |
| `MCP_AUDIT_MAX_NAMES` | les noms que garde chaque liste de l'enregistrement d'audit d'un appel (ses tables, les tables dont il a servi la description), de 1 à 100000 ; les autres sont comptés, une fois chacun, jamais écrits | `200` |

## profile_table

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_PROFILE_MAX_COLUMNS`, `MCP_PROFILE_SAMPLE_ROWS` | colonnes que `profile_table` profile au plus en un appel (de 1 à 500), et lignes que lit son mode `sample`, 1 au moins | `50`, `100000` |
| `MCP_PROFILE_MODES` | modes que `profile_table` permet, séparés par des virgules, parmi `sample`, `bernoulli` et `full` ; vide ou nom inconnu : refus de démarrer | `sample,bernoulli,full` |
| `MCP_PROFILE_VALUE_CHARS` | caractères d'un `min` ou d'un `max` texte qu'il sert, ou octets d'un binaire ; Trino en lit un de plus et pas davantage, et au-delà la valeur est coupée ; 1 au moins | `200` |
| `MCP_PROFILE_TYPE_CHARS` | caractères du type d'une colonne qu'il sert, de 1 à 100000 ; un type plus long est coupé, avec `"type"` dans `cut` ; `MCP_PROFILE_VALUE_CHARS` ne coupe jamais un type | `500` |
| `MCP_PROFILE_DESCRIBE_ROWS` | lignes de son `DESCRIBE` qu'il lit, au-delà du plafond de lignes, 1 au moins ; une colonne au-delà ne peut pas être nommée | `5000` |
| `MCP_PROFILE_CLIENT_TAG` | étiquette client de sa requête d'agrégats (pas de son `DESCRIBE`), pour un groupe de ressources de Trino : 1 à 100 lettres, chiffres et `_ . : = / -` | `mcp-profile` |
| `MCP_PROFILE_TIMEOUT_SECONDS` | budget de temps de sa requête d'agrégats, au-delà duquel elle est annulée dans Trino ; le budget qui s'applique est le plus petit de celui-ci, de `TRINO_QUERY_TIMEOUT_SECONDS` et du budget de l'appel, chacun compté seulement au-dessus de 0, et `--check` l'affiche ; 0 laisse le plus petit des deux autres ; au-dessus de 0, au plus `TRINO_QUERY_TIMEOUT_SECONDS` et `MCP_CALL_TIMEOUT_SECONDS` quand ils sont au-dessus de 0 | `0` |
| `MCP_PROFILE_MAX_CONCURRENT` | appels de `profile_table` qu'un processus du serveur exécute à la fois, de 1 à 64 ; au-delà, un appel répond aussitôt `"kind": "busy"` et n'envoie aucune requête | `4` |

## Contexte d'un catalogue

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_CONTEXT_PROVIDERS` | noms, séparés par des virgules, des fournisseurs installés dans le groupe d'entrée `akko_mcp_trino.context` ; un nom inconnu empêche le démarrage | aucun |
| `MCP_CONTEXT_TIMEOUT_SECONDS` | budget de temps d'une lecture de contexte, plus de 0 | `5` |
| `MCP_CONTEXT_RESERVE_SECONDS` | secondes du budget d'un appel gardées en réserve pour que le contexte finisse avant l'appel ; 0,3 ou plus quand un fournisseur est chargé (le serveur refuse moins), 0 ou plus sinon | `0.5` |
| `MCP_CONTEXT_MAX_CONCURRENCY` | lectures de contexte simultanées au plus par fournisseur, 1 au moins | `32` |
| `MCP_CONTEXT_RETRY_AFTER_SECONDS` | délai avant de rappeler une source en panne, 0 ou plus | `30` |
| `MCP_CONTEXT_REFUSED_RETRY_SECONDS` | délai avant de rappeler une source qui a refusé le compte (`SIGHUP` le lève aussitôt), 0 ou plus | `3600` |
| `MCP_CONTEXT_BATCH` | tables par lecture d'une source, 1 au moins | `50` |
| `MCP_CONTEXT_TTL_SECONDS` | durée de conservation d'une lecture, 0 ou plus | `900` |
| `MCP_CONTEXT_CACHE_BYTES`, `MCP_CONTEXT_TERMS_CACHE_BYTES` | borne des caches des tables et des termes de glossaire, 1 au moins ; chaque borne compte la longueur de la forme JSON des entrées gardées, et le processus en tient environ trois à quatre fois plus en mémoire, dimensionner le pod en conséquence | `134217728`, `16777216` |
| `MCP_VISIBILITY_TTL_SECONDS` | durée de conservation de ce que Trino a listé pour un appelant, 0 ou plus | `60` |
| `MCP_VISIBILITY_CACHE_BYTES`, `MCP_VISIBILITY_MAX_NAMES` | borne de ce cache (chaque entrée compte sa clé comme sa valeur ; mesuré avec des chaînes neuves, le processus en tient de 3,6 à 8 fois plus en mémoire, dimensionner le pod pour huit fois plus), et noms gardés au plus pour un appelant, 1 au moins | `33554432`, `10000` |
| `MCP_CONTEXT_REQUIRE_SELECT` | `true` : contexte servi seulement pour les tables que l'appelant peut lire | `false` |
| `MCP_CONTEXT_CANARY` | une table `catalog.schema.table` qu'aucun appelant de ce serveur ne doit voir : le contexte n'est servi à un appelant que tant que Trino la lui cache ; son absence est signalée quand un fournisseur est chargé | aucune |
| `MCP_CONTEXT_TEXT_CHARS`, `MCP_CONTEXT_RESPONSE_CHARS`, `MCP_CONTEXT_MAX_COLUMNS` | le texte le plus long d'un champ de contexte, le contexte le plus long d'une réponse, et le nombre maximal de colonnes décrites dans une table, 1 au moins | `500`, `60000`, `500` |
| `MCP_VISIBILITY_ENABLED` | supprimé en 0.8 : le filtre par les droits de Trino ne se désactive pas, et `false` refuse de démarrer | aucune |

Les variables propres au fournisseur `file` sont dans
[Contexte d'un catalogue](catalog-context.md#le-fournisseur-file).
