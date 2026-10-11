# akko-mcp-trino

[English](README.md) · **Français**

**Donnez à vos agents IA un accès en lecture seule à Trino, avec les droits de la personne pour
qui ils travaillent, et rien de plus.**

akko-mcp-trino est un serveur [MCP](https://modelcontextprotocol.io) pour Trino, sous licence
Apache 2.0. Il permet à un agent de lister les catalogues, les schémas et les tables, de décrire
une table, d'expliquer une requête et de l'exécuter. Il garantit que rien de ce que l'agent envoie
ne peut écrire : chaque requête est analysée, et une écriture placée n'importe où dans la requête
est refusée avant d'atteindre Trino.

Chaque requête s'exécute sous l'une de deux identités. La première est celle que l'administrateur
configure, la même pour tous les appels. La seconde est celle de l'utilisateur pour qui l'agent
travaille, dont Trino vérifie lui-même le jeton. Dans les deux cas, le contrôle d'accès de Trino
(OPA, Ranger ou les règles de Trino) décide de ce que l'agent peut lire. Les résultats sont bornés
en lignes et en durée, et chaque appel laisse une ligne d'audit.

Ce document est un résumé en français. Le [README en anglais](README.md) fait référence pour
chaque réglage ; la [documentation en français](https://akko-ai.com/docs/coeur-open-source/)
reprend tout en détail.

## Pourquoi

Un agent qui lit un entrepôt de données a besoin d'une surface petite et prévisible : quelques
outils qu'un modèle comprend, des réponses qui ne s'emballent jamais, et la garantie qu'une
injection de prompt ne transforme pas une question en `DROP TABLE`. Ce serveur est cette surface,
et rien d'autre. Il ne décide pas des droits : en mode `jwt`, Trino décide pour chaque personne
avec les règles qu'il a déjà ; en mode `service`, il décide pour l'identité unique que vous lui
donnez.

## Points forts

- **Les droits de la personne, de bout en bout.** En mode `jwt`, le jeton vérifié de l'utilisateur
  part vers Trino, qui le vérifie à son tour et exécute la requête sous son nom. Ranger ou OPA
  décident pour la personne, pas pour un compte de service, sans mot de passe. Une requête sans
  jeton vérifié est refusée, jamais exécutée sous un autre nom.
- **Ou une identité configurée par l'administrateur.** En mode `service`, le mode par défaut, toutes
  les requêtes s'exécutent sous `TRINO_USER`.
- **Lecture seule par construction.** La requête est analysée en arbre et refusée si une écriture
  y apparaît, y compris dans une CTE ou une sous-requête. Les fonctions de table et le catalogue
  `system` sont refusés aussi.
- **Borné.** Un plafond de lignes avec l'indicateur `truncated`, un délai par requête annulé dans
  Trino, un délai par appel d'outil.
- **Une ligne d'audit par appel**, sans jamais le jeton ni le texte SQL.
- **Six outils, pas un de plus.**
- **Extensible sans fork**, par des paquets Python nommés au démarrage.

## Installer

```bash
pip install akko-mcp-trino        # ou : pipx install akko-mcp-trino
uvx akko-mcp-trino --version      # sans installation, comme le font la plupart des hôtes MCP
```

En conteneur, pour `linux/amd64` et `linux/arm64` :

```bash
docker run --rm akkoai/akko-mcp-trino:0.5 akko-mcp-trino --version
```

Il faut Python 3.12 ou plus récent pour l'installation par pip.

## Démarrer

### Chaque personne avec ses droits (`jwt`)

Quand Trino fait confiance au même fournisseur d'identité, laissez-le décider pour chaque personne :

```bash
export TRINO_HOST=trino.example.internal
export TRINO_PORT=8443
export TRINO_HTTP_SCHEME=https         # obligatoire : un jeton ne circule jamais en http
export TRINO_CATALOG=iceberg
export TRINO_IDENTITY_MODE=jwt         # le jeton vérifié de l'appelant part vers Trino

export MCP_AUTH_ENABLED=true
export MCP_AUTH_REQUIRED=true          # refuse toute requête sans identité vérifiée
export MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs
export MCP_OIDC_ISSUER=https://idp.example.com/realms/data
export MCP_OIDC_AUDIENCE=data-platform

python -m akko_mcp_trino
```

Côté Trino : `http-server.authentication.type=JWT`, `http-server.authentication.jwt.key-file`
réglé sur l'URL JWKS du fournisseur, et `http-server.authentication.jwt.principal-field` sur la
même revendication que celle lue par le serveur (`preferred_username` ou `sub`). Deux personnes
qui posent la même question obtiennent alors les réponses que leurs droits permettent.

### Une identité configurée (`service`)

Remplacez `TRINO_IDENTITY_MODE=jwt` par `TRINO_USER=mcp-reader` et, si Trino demande un mot de
passe, `TRINO_PASSWORD` (en https seulement). Donnez à cette identité les droits que tout appelant
du serveur peut avoir, pas plus.

### Vérifier

```bash
akko-mcp-trino --check           # la configuration effective, jamais un secret ; code 2 si un réglage bloque
curl -s localhost:3001/health    # {"status":"ok"}
curl -s localhost:3001/ready     # {"status":"ready"} quand Trino répond
curl -s localhost:3001/metrics   # métriques Prometheus
```

## Brancher un hôte MCP

Avec l'authentification activée, l'hôte envoie le jeton d'accès de l'utilisateur ; le serveur le
vérifie et nomme l'appelant dans la ligne d'audit. Cursor, Claude Desktop, VS Code (`mcp.json`) :

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
vérifié comme un en-tête. Le [README](README.md#local-hosts-over-stdio) donne la configuration.

## Les outils

| Outil | Arguments | Rend |
|---|---|---|
| `list_catalogs` | aucun | les catalogues que Trino montre |
| `list_schemas` | `catalog` | les schémas de ce catalogue |
| `list_tables` | `catalog`, `schema` | les tables et vues de ce schéma |
| `describe_table` | `catalog`, `schema`, `table`, `sample_rows` (0 à 20) | colonnes, types, commentaires, échantillon gouverné sur demande |
| `explain_query` | `sql` | le plan de Trino pour une requête en lecture, sans l'exécuter |
| `execute_query` | `sql`, `max_rows` (facultatif) | colonnes, lignes, nombre de lignes, `truncated` |

Chaque outil passe par Trino sous l'identité du mode choisi, les outils de liste compris : Trino ne
liste que ce que cette identité peut voir. Une valeur masquée ou une ligne absente est la politique
d'accès, pas une erreur à retenter, et les descriptions des outils le disent au modèle.

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
