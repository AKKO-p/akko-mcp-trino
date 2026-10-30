# akko-mcp-trino

[English](README.md) · **Français**

**Donnez à vos agents IA un accès en lecture seule à Trino, avec les droits de la personne pour
qui ils travaillent, et rien de plus.**

akko-mcp-trino est un serveur [MCP](https://modelcontextprotocol.io) pour Trino, sous licence
Apache 2.0. Il permet à un agent de lister les catalogues, les schémas et les tables, de décrire
une table, d'expliquer une requête et de l'exécuter. Il refuse toute écriture qu'on lui envoie :
chaque requête est analysée, et une écriture placée n'importe où dans la requête est refusée avant
d'atteindre Trino.

Par défaut, chaque requête s'exécute sous le nom de l'utilisateur pour qui l'agent travaille. Le
serveur vérifie le jeton de l'utilisateur, émis pour ce serveur, puis obtient du fournisseur
d'identité un jeton distinct destiné à Trino, par OAuth 2.0 Token Exchange (RFC 8693). Le jeton de
l'utilisateur ne quitte jamais le serveur. Le contrôle d'accès de Trino (OPA, Ranger ou les règles
de Trino) décide ensuite de ce que cet utilisateur peut lire. Les résultats sont bornés en lignes
et en durée, et chaque appel laisse une ligne d'audit.

> **Vous venez de la 0.5 ?** La 0.6.0 change les valeurs par défaut. Un appelant vérifié est
> exigé, et le mode `jwt` échange le jeton de l'appelant au lieu de le transmettre. Le guide
> [MIGRATING.md](MIGRATING.md) (en anglais) décrit chaque étape.

Ce document est un résumé en français. Le [README en anglais](README.md) fait référence pour
chaque réglage ; la [documentation en français](https://akko-ai.com/docs/coeur-open-source/)
reprend tout en détail.

## Pourquoi

Un agent qui lit un entrepôt de données a besoin d'une surface petite et prévisible : quelques
outils qu'un modèle comprend, des réponses qui ne s'emballent jamais, et la garantie qu'une
injection de prompt ne transforme pas, à travers ce serveur, une question en `DROP TABLE`. Ce
serveur est cette surface, et rien d'autre. Il ne décide pas des droits : Trino décide pour chaque
personne avec les règles qu'il a déjà, ou, en mode `service`, pour l'identité unique que vous lui
donnez.

## Points forts

- **Les droits de la personne, sûrs par défaut.** En mode `jwt`, le mode par défaut, le serveur
  vérifie le jeton de l'appelant (émis pour ce serveur, jamais pour Trino), l'échange auprès du
  fournisseur d'identité contre un jeton dont l'audience est Trino, et Trino exécute la requête
  sous le nom de cet utilisateur. Le jeton de l'appelant n'est jamais transmis. Une requête sans
  jeton valide est refusée en `401`.
- **Un repli sans échange de jeton.** En mode `impersonation`, le serveur s'authentifie auprès de
  Trino avec son propre compte (mot de passe ou certificat client), et Trino exécute la requête
  sous le nom de l'appelant vérifié, selon une règle d'impersonation réservée à ce compte.
- **Une identité unique, seulement si on la nomme.** En mode `service`, toutes les requêtes
  s'exécutent sous `TRINO_USER`. Ce mode n'est jamais choisi par défaut, et le serveur le signale
  au démarrage et à chaque hôte dans ses instructions d'initialisation.
- **Lecture seule par construction, à travers ce serveur.** La requête est analysée en arbre et
  refusée si une écriture y apparaît, y compris dans une CTE ou une sous-requête. Les fonctions de
  table et le catalogue `system` sont refusés aussi. Ce qu'un utilisateur peut faire dans Trino
  par d'autres moyens relève des règles de Trino : la section suivante le détaille.
- **Borné par défaut.** Un plafond de 100 lignes avec l'indicateur `truncated`, un délai de 60 s
  par requête annulé dans Trino, un délai de 120 s par appel d'outil.
- **Une ligne d'audit par appel**, sans jamais le jeton ni le texte SQL.
- **Six outils, pas un de plus**, annotés selon ce qu'ils peuvent vraiment faire.
- **Extensible sans fork**, par des paquets Python nommés au démarrage.

## Ce que couvre la lecture seule

La garde refuse toute écriture qui passe par ce serveur. Elle ne garantit pas les droits de
l'utilisateur dans Trino. En modes `jwt` et `impersonation`, Trino exécute la requête sous le nom
de l'utilisateur, avec les droits que sa politique lui donne. Un utilisateur autorisé à écrire
peut donc toujours écrire par un autre client Trino, avec un jeton pour Trino que le fournisseur
d'identité lui délivre directement. Le jeton obtenu par échange reste dans la mémoire du serveur et
n'est jamais rendu à l'agent ni à l'hôte.

Pour une lecture seule de bout en bout, le moteur doit tenir la ligne lui aussi. Donnez des règles
en lecture seule aux identités par lesquelles les agents atteignent Trino (OPA, Ranger ou règles
de fichier), réservez l'échange vers l'audience de Trino au client de ce serveur, limitez la règle
d'impersonation aux utilisateurs pour qui les agents travaillent, et refusez aussi les fonctions
de table et le catalogue `system` dans la politique. La garde reste une seconde ligne.

## Installer

```bash
pip install akko-mcp-trino        # ou : pipx install akko-mcp-trino
uvx akko-mcp-trino --version      # sans installation, comme le font la plupart des hôtes MCP
```

En conteneur, pour `linux/amd64` et `linux/arm64` :

```bash
docker run --rm akkoai/akko-mcp-trino:0.6 akko-mcp-trino --version
```

Il faut Python 3.12 ou plus récent pour l'installation par pip.

## Démarrer

| Mode | Identité dans Trino | Usage |
|---|---|---|
| `jwt` (par défaut) | l'appelant vérifié, par échange de jeton | fournisseur compatible avec OAuth 2.0 Token Exchange |
| `impersonation` | l'appelant vérifié, via le compte du serveur | fournisseur sans échange de jeton |
| `oauth2` | l'utilisateur local, vérifié par `SELECT current_user` | connexion navigateur native de Trino, en `stdio` uniquement |
| `service` | `TRINO_USER`, identique pour tous | identité unique explicitement choisie, signalée au démarrage |

### Chaque personne avec ses droits (`jwt`, par défaut)

```bash
export TRINO_HOST=trino.example.internal
export TRINO_PORT=8443
export TRINO_HTTP_SCHEME=https         # obligatoire : un jeton ne circule jamais en http
export TRINO_CATALOG=iceberg

# La porte : des jetons émis pour ce serveur, vérifiés avec le JWKS du fournisseur
export MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs
export MCP_OIDC_ISSUER=https://idp.example.com/realms/data
export MCP_OIDC_AUDIENCE=akko-mcp       # l'audience des jetons émis pour ce serveur

# Un jeton pour Trino, par échange (RFC 8693), jamais celui de l'appelant
export TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token
export TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp
export TRINO_TOKEN_EXCHANGE_CLIENT_SECRET=...
export TRINO_TOKEN_EXCHANGE_AUDIENCE=trino

python -m akko_mcp_trino
```

Côté fournisseur, ce serveur a un client confidentiel autorisé à échanger des jetons, et
l'audience de Trino lui est accessible. Avec Keycloak 26, activez *Standard token exchange* sur ce
client et ajoutez-lui un mappeur d'audience vers le client Trino. Les clients des hôtes MCP
placent ce serveur dans l'audience de leurs jetons. Le dossier [`integration/`](integration/)
contient un tel realm, testé à chaque pull request.

Côté Trino : `http-server.authentication.type=JWT`, `http-server.authentication.jwt.key-file`
réglé sur l'URL JWKS du fournisseur, `http-server.authentication.jwt.principal-field` sur la même
revendication que celle lue par le serveur (`preferred_username` ou `sub`), et `required-audience`
sur l'audience de Trino. Deux personnes qui posent la même question obtiennent alors les réponses
que leurs droits permettent. Un jeton dont l'audience est celle de Trino est refusé par le
serveur.

### Sans échange de jeton (`impersonation`)

```bash
export TRINO_IDENTITY_MODE=impersonation
export TRINO_USER=mcp-service
export TRINO_CLIENT_CERT=/certs/mcp-service.crt   # mTLS ; ou TRINO_PASSWORD en https
export TRINO_CLIENT_KEY=/certs/mcp-service.key
```

Trino authentifie ce compte et l'autorise, lui seul, à agir au nom des utilisateurs, par exemple
avec la règle `{"impersonation": [{"original_user": "mcp-service", "new_user": ".*", "allow": true}]}`
à restreindre aux utilisateurs voulus. Le nom de l'appelant vient toujours de son jeton vérifié,
jamais d'un en-tête.

### Connexion navigateur native (`oauth2`, en stdio uniquement)

Avec Trino OAuth2 configuré, utilisez l'[exemple de lancement](README.md#native-browser-login-oauth2-stdio-only).
Le serveur n'envoie aucun nom à Trino : Trino exécute chaque requête au nom de la personne
connectée, et `TRINO_USER` est ignoré. HTTPS et stdio sont obligatoires. Le navigateur s'ouvre,
l'URL va uniquement sur stderr, et `SELECT current_user` doit réussir avant de servir ; son résultat
nomme l'utilisateur dans l'admission et l'audit. `MCP_AUTH_REQUIRED=false` ne désactive jamais cette
connexion ; `true` exige un `MCP_USER_TOKEN` vérifié en plus. Le pilote gère le cache en mémoire
(ou `keyring` facultatif) et la réauthentification. `--check` ne se connecte pas.

### Une identité unique (`service`, seulement si on la nomme)

Réglez `TRINO_IDENTITY_MODE=service`, `TRINO_USER=mcp-reader` et, si Trino demande un mot de
passe, `TRINO_PASSWORD` (en https seulement). Donnez à cette identité les droits que tout appelant
du serveur peut avoir, en lecture seule, pas plus. Le serveur l'annonce par un avertissement.

### Vérifier

```bash
akko-mcp-trino --check           # la configuration effective, les avertissements, jamais un secret ; code 2 si un réglage bloque
curl -s localhost:3001/health    # {"status":"ok"}
curl -s localhost:3001/ready     # {"status":"ready"} quand Trino répond
curl -s localhost:3001/metrics   # métriques Prometheus
```

## Brancher un hôte MCP

L'hôte envoie le jeton d'accès de l'utilisateur, émis pour ce serveur ; le serveur le vérifie et
nomme l'appelant dans la ligne d'audit. Cursor, Claude Desktop, VS Code (`mcp.json`) :

```json
{
  "mcpServers": {
    "trino": {
      "url": "http://localhost:3000/mcp",
      "headers": {"Authorization": "Bearer <le jeton d'accès de l'utilisateur>"}
    }
  }
}
```

`streamable-http` (`/mcp`) est le transport par défaut. Claude Desktop, Cursor et Mistral Vibe
peuvent aussi lancer le serveur eux-mêmes en `stdio` : l'appelant est alors `MCP_USER_TOKEN`,
vérifié comme un en-tête, ou l'identité confirmée par Trino en mode `oauth2`.
Le [README](README.md#local-hosts-over-stdio) donne la configuration.

## Les outils

| Outil | Arguments | Rend |
|---|---|---|
| `list_catalogs` | `offset` (facultatif, 0 par défaut) | une page des catalogues que Trino montre, `next_offset`, `truncated` |
| `list_schemas` | `catalog`, `offset` (facultatif) | une page des schémas de ce catalogue, `next_offset`, `truncated` |
| `list_tables` | `catalog`, `schema`, `offset` (facultatif) | une page des tables et vues de ce schéma, `next_offset`, `truncated` |
| `describe_table` | `catalog`, `schema`, `table`, `sample_rows` (0 à 20) | colonnes, types, commentaires, échantillon gouverné sur demande |
| `explain_query` | `sql` | le plan de Trino pour une requête en lecture, sans l'exécuter |
| `execute_query` | `sql`, `max_rows` (facultatif) | colonnes, lignes, nombre de lignes, `truncated` |

Chaque outil passe par Trino sous l'identité du mode choisi, les outils de liste compris : Trino ne
liste que ce que cette identité peut voir. Une valeur masquée ou une ligne absente est la politique
d'accès, pas une erreur à retenter, et les descriptions des outils le disent au modèle. Avec
`TRINO_READ_ONLY=false`, `execute_query` est annoté comme pouvant écrire, pour que l'hôte demande
la confirmation qu'il sauterait pour un outil en lecture seule.

Les trois outils de liste rendent une page à la fois : une page compte `TRINO_MAX_ROWS` noms, triés
par nom, et `next_offset` dit où commence la page suivante, ou vaut `null` à la dernière. L'agent
rappelle l'outil avec `offset=next_offset` pour lire la suite ; `truncated` est vrai exactement
quand `next_offset` n'est pas `null`. Les schémas et les tables sont paginés dans Trino (`ORDER BY`
et `OFFSET` sur `information_schema`). Les catalogues sont lus en entier par `SHOW CATALOGS`,
au-delà du plafond de lignes, jusqu'à `MCP_MAX_CATALOGS` (10000 par défaut), puis paginés par le
serveur, `system` non compté ; au-delà de cette borne, `list_catalogs` rend une erreur, jamais une
liste coupée. `list_tables` sur un schéma inexistant ou invisible pour l'appelant rend `kind`
`schema` (« not found or not visible », la même réponse pour les deux), jamais une liste vide. Un
`offset` qui n'est pas un entier positif ou nul est refusé (`invalid_argument`) avant Trino.

Une erreur revient sous la forme `{"error": "...", "kind": "..."}`. `error` est le message entier de
Trino ; `kind` dit quoi en faire, et `identifier` nomme la colonne, la table, le schéma ou le
catalogue que Trino n'a pas su résoudre, quand il le nomme.

| `kind` | Sens |
|---|---|
| `syntax`, `column`, `table`, `schema`, `catalog`, `function`, `type` | Trino a refusé l'instruction : corriger cette partie |
| `permission` | Trino ou le fournisseur d'identité a refusé le droit : le rapporter, ne pas réessayer |
| `invalid_argument` | le serveur a refusé un argument (identifiant, `offset`) avant d'atteindre Trino |
| `read_only` | l'instruction écrit et le serveur est en lecture seule |
| `time_budget` | l'appel a épuisé son budget de temps : restreindre la demande |
| `unavailable` | Trino n'a pas pu répondre (erreur interne, connexion perdue) : l'instruction n'est ni valide ni invalide, réessayer plus tard |
| `other` | toute autre erreur |

La ligne d'audit garde seulement le code de l'erreur, inchangé.

## Configuration, extensions, exploitation

Toutes les variables, leurs valeurs par défaut, les points d'extension (identités, gardes, audit,
outils), les refus, l'audit et les métriques sont décrits dans le [README](README.md) et dans la
[documentation en français](https://akko-ai.com/docs/coeur-open-source/).

## Contribuer

Les issues et les pull requests sont bienvenues : voir [CONTRIBUTING.md](CONTRIBUTING.md). Une
vulnérabilité se signale à security@akko-ai.com, selon [SECURITY.md](SECURITY.md). Si vous arrivez à
faire passer une écriture à travers la garde, ouvrez une issue : c'est exactement ce que nous
cherchons.

## À propos

akko-mcp-trino est construit et maintenu par [AKKO](https://akko-ai.com), un projet français qui
travaille sur l'accès gouverné des agents IA aux données des organisations, sur place, avec les
moteurs et les fournisseurs d'identité qu'elles ont déjà.

## Licence

Apache 2.0, voir [LICENSE](LICENSE) et [NOTICE](NOTICE).
