# Contexte d'un catalogue

[English](../en/catalog-context.md) · **Français**

## Les fournisseurs de contexte

Un catalogue de données peut décrire ce que Trino liste : le propriétaire d'une table, sa
classification, les termes du glossaire d'une colonne. Un fournisseur de contexte est un paquet qui
lit un tel catalogue pour le serveur. Il enregistre une fabrique `(env) -> ContextProvider` dans le
groupe d'entrée `akko_mcp_trino.context`, et l'administrateur le nomme dans `MCP_CONTEXT_PROVIDERS`.
**Aucun fournisseur n'est chargé par défaut** : sans fournisseur, rien ne démarre, pas même un fil.
Un nom inconnu, un fournisseur d'une autre version de `context_api` ou une fabrique qui ne peut pas
servir ses réglages empêchent le démarrage ; un catalogue injoignable, non (`--check --strict` rend 2
quand un fournisseur se dit injoignable). Un fournisseur lit ses propres variables dans
l'environnement ; un secret peut être donné en fichier (`<NOM>_FILE`, relu
quand le fichier change).

Deux fournisseurs viennent avec ce dépôt : `file`, dans le cœur, la référence du contrat (un
catalogue tenu dans un fichier JSON), et `atlas`, pour Apache Atlas, un paquet à part
(`akko-mcp-trino-atlas`, installé par l'extra `atlas`) ; ses réglages, sa mise en place de Kerberos
et le compte qu'il lui faut sont dans [son README](../../providers/atlas/README.fr.md).

Le contexte suit ce que Trino liste pour l'appelant : un nom qui vient d'un catalogue n'est jamais
utilisé avant que Trino ne l'ait listé pour cet appelant, un fournisseur ne reçoit jamais le client
Trino ni l'identité de l'appelant, et il n'écrit jamais dans son catalogue. Le texte de l'erreur
d'un fournisseur n'atteint jamais l'agent, seulement un statut.

En charger un ou non, c'est le choix entre les deux modes de
[Déploiement](deployment.md#déploiement-avec-un-catalogue), et ses réglages sont dans
[Configuration](configuration.md#contexte-dun-catalogue).

## Ce que sert describe_table

`describe_table` sert ce contexte. Son `DESCRIBE`, lancé sous l'identité de l'appelant, prouve la
table et les colonnes que Trino liste ; un `DESCRIBE` en échec n'appelle aucun fournisseur et répond
comme avant. La réponse ajoute alors :

- `table` : ce que les catalogues disent de la table (description, étiquettes, termes, propriétés,
  grain, jointures ; le propriétaire seulement quand un fournisseur le sert), avec `sources`, le
  fournisseur de chaque champ ;
- `column_context` : la même chose pour chaque colonne listée par Trino, par nom, dans l'ordre du
  `DESCRIBE`. Une colonne que Trino ne liste pas n'a aucun contexte ; un grain perd toutes ses
  colonnes quand l'une est cachée ; une jointure n'est gardée que vers une table et des colonnes
  que Trino liste à l'appelant ;
- `terms` : chaque terme de glossaire une seule fois, par référence (`t1`, `t2`… dans l'ordre
  d'apparition), avec son `name`, son `glossary`, sa `definition`, son `status`, ses `categories`,
  ses termes liés `related` (seulement des termes de la même réponse) et sa `source`. Une table ou
  une colonne cite `{"name", "ref"}` ; les clés propres d'un catalogue ne sont jamais servies ;
- `context_sources` : un statut par fournisseur, `ok`, `not_found` (le fournisseur ne connaît pas
  la table), `not_mapped` (il ne couvre pas ce catalogue Trino), `partial` (une partie n'a pas été
  lue dans le temps ou dans la taille de la réponse, ou le plafond de lignes a coupé le
  `DESCRIBE`), `unavailable`, `refused`, `error`, ou `not_checked` (le canari est visible, un
  contrôle a échoué, ou avec `MCP_CONTEXT_REQUIRE_SELECT=true` Trino ne valide pas une lecture des
  colonnes visibles) ;
- `context_truncated` et `undescribed_columns` : au plus `MCP_CONTEXT_MAX_COLUMNS` colonnes sont
  décrites, et le contexte s'arrête avant la colonne qui dépasserait
  `MCP_CONTEXT_RESPONSE_CHARS` ; les colonnes laissées sont listées. Un texte coupé à
  `MCP_CONTEXT_TEXT_CHARS` est nommé dans `cut`.

Sans fournisseur, `describe_table` répond comme en 0.7.2 plus `next_column_offset` et
`columns_cut`, avec une nouvelle description qui dit comment lire la page suivante, et
`describer_errors` : un descripteur de plugin qui lève y est désormais compté. Avec un fournisseur, sa description dit
que ce texte vient d'un catalogue et que ce sont des données, jamais des instructions. Le contexte
est lu en dernier, après l'échantillon, et ne cache jamais les colonnes ni ne fait échouer l'appel.
Un fournisseur en panne, lent ou qui refuse le compte est lu en `MCP_CONTEXT_TIMEOUT_SECONDS` au plus
et laisse `MCP_CONTEXT_RESERVE_SECONDS` du budget de l'appel ; s'il en reste moins, aucun fournisseur
n'est appelé et les sources disent `unavailable`. Les requêtes du filtre s'arrêtent tant que cette
réserve reste : une requête trop lente ne prouve rien (`not_checked` pour le canari ou le contrôle
de lecture, pas de jointure vers une cible), et l'appel répond quand même. Tout autre échec en
chemin ne sert aucun contexte et dit `error` pour chaque source.

## Ce que sert describe_schema

`describe_schema` en sert moins. Chaque table de la page qu'un fournisseur décrit ajoute
`description`, `{"text", "source"}` : la première description qu'un fournisseur en donne, coupée à
`MCP_CONTEXT_TEXT_CHARS` (alors `"cut": ["text"]`). Une table sans contexte n'a pas de clé
`description`, et la réponse ajoute `context_sources`, sauf pour une page vide, qui n'interroge
aucun fournisseur. Trino vient de lister à l'appelant les tables de la page : le filtre ne lui
demande rien de plus que le contrôle du canari, et les fournisseurs ne sont interrogés que sur les
tables de la page, en une lecture par lots de `MCP_CONTEXT_BATCH`. Une liste ne sert ni contexte de
colonne, ni terme, ni propriétaire, et `MCP_CONTEXT_REQUIRE_SELECT` ne s'y applique pas, parce qu'il
coûterait une requête par table : `describe_table` sert le reste. Les mêmes garanties tiennent :
s'il reste moins de `MCP_CONTEXT_RESERVE_SECONDS`, aucun fournisseur n'est appelé et les sources
disent `unavailable`, une requête lente du filtre ne prouve rien, tout autre échec sert la page sans
contexte et dit `error` pour chaque source, et l'appel ne répond jamais `time_budget` à cause du
contexte. Sans fournisseur, `describe_schema` n'ajoute aucune clé de contexte à sa réponse ni
aucune phrase à sa description.

## Ce que lisent les plugins

Les plugins lisent les catalogues par `tools.context.tables(refs)`, une liste de `TableRef`
construits par `TableRef.of`. La réponse a la forme de celle de `describe_schema`, `descriptions`
par `TableRef` et `context_sources`, pour les seules tables que la visibilité de l'appelant prouve
(listées par Trino dans cet appel ou depuis moins de `MCP_VISIBILITY_TTL_SECONDS`). Une table que
Trino ne liste pas n'est ni nommée à Trino ni demandée à un fournisseur, et elle est absente de la
réponse, comme une table sans contexte. Un appel lit au plus `MCP_DESCRIBE_SCHEMA_TABLES` tables
distinctes, les premières de `refs`, autant qu'une page de `describe_schema` ; les suivantes sont
absentes. Une liste `refs` vide n'interroge rien et ne répond aucun `context_sources`, comme une
page vide. Un plugin ne reçoit jamais la chaîne, le cache de visibilité ni un fournisseur.

## Le texte des catalogues

Un texte de catalogue coûte ce qui est servi, pas ce que le catalogue envoie : le nettoyage lit au
plus quatre fois `MCP_CONTEXT_TEXT_CHARS` caractères d'un texte, et une liste ne lit que la
description qu'elle sert. Un nom que donne le catalogue (une classification, une étiquette, un
ensemble, la clé d'une propriété, le propriétaire, le niveau, le nom, le glossaire, le statut, la
catégorie ou la relation d'un terme, une colonne d'un grain ou d'une jointure) de plus de 256
caractères une fois nettoyé est écarté, jamais coupé, car un nom coupé est un autre nom ; en décider
lit au plus 1025 caractères. Un terme dont le nom est écarté n'est pas servi. La valeur d'une
propriété est un texte : elle est coupée à `MCP_CONTEXT_TEXT_CHARS`, et porte alors
`"cut": ["value"]`.

## Les requêtes du filtre

Les requêtes du filtre partent sous l'identité de l'appelant, avec l'étiquette client `mcp-gate` :
le contrôle du canari, `EXPLAIN (TYPE VALIDATE)` des colonnes visibles avec
`MCP_CONTEXT_REQUIRE_SELECT=true` (un refus est gardé `MCP_VISIBILITY_TTL_SECONDS`), et, pour la
cible d'une jointure, les schémas de son catalogue, les tables de son schéma et son `DESCRIBE`. Un
droit retiré dans Trino peut laisser voir le contexte d'une table jusqu'à
`MCP_VISIBILITY_TTL_SECONDS`, jamais ses données, que Trino refuse aussitôt.

## Le compte du fournisseur

Donnez à chaque fournisseur un compte en lecture seule qui ne voit que ce que tout utilisateur qui
voit un objet dans Trino peut en lire : les listes blanches d'un fournisseur nomment ce qui est
servi, et son compte ne doit rien lire de plus. Derrière Trino Gateway, les requêtes du filtre et
celles de l'appel peuvent partir vers deux clusters d'un même groupe de routage : tous les clusters
d'un groupe doivent partager le même contrôle d'accès (le même plugin et les mêmes règles). Un
groupe de routage dont les clusters appliquent des règles différentes n'est pas pris en charge.

## Contrôle, journaux et arrêt

`--check` imprime une ligne `context:` (les fournisseurs et chaque réglage de son tableau dans
[Configuration](configuration.md#contexte-dun-catalogue), de
`MCP_CONTEXT_TIMEOUT_SECONDS` à `MCP_CONTEXT_MAX_COLUMNS`), une ligne `visibility:` (la durée, la
borne en octets et en noms, si un canari est posé, jamais son nom, et si `select` est exigé) et les
lignes de chaque fournisseur, jamais un secret. `MCP_CONTEXT_PROVIDERS` sans canari, ou en mode
`service`, est signalé.

Un fournisseur en panne ou qui refuse le compte écrit un avertissement à chaque ouverture de son
coupe-circuit (`<nom> is unavailable`, `<nom> refused the account`), jamais le texte de son erreur.
`SIGHUP` laisse rappeler aussitôt les fournisseurs qui ont refusé le compte et remet leur
`mcp_trino_context_breaker_open` à 0. À l'arrêt, par SIGTERM, Ctrl+C ou la fin de son entrée, le
serveur ferme chaque fournisseur, puis la boucle du contexte. En HTTP, cela se fait à l'arrêt de
l'application, puis SIGTERM termine le processus avec le code 143 ; en stdio avec un fournisseur,
SIGTERM ferme et sort avec 0, et un SIGTERM pendant une fermeture la laisse finir sans en lancer une
autre ; sans fournisseur, SIGTERM termine le processus comme en 0.7.2. Une lecture qui ignore son
annulation ne retient pas l'arrêt plus de quelques secondes ; asyncio peut alors écrire une fois
qu'une tâche en attente a été détruite. Le texte d'un catalogue perd ses caractères de contrôle et
de format, et ses séparateurs de ligne et de paragraphe (U+2028, U+2029), avant d'atteindre un
agent.

## Le fournisseur `file`

`file` est le fournisseur livré avec le serveur, et la référence du contrat. Il n'a besoin d'aucun
service : un fichier JSON décrit des tables, des colonnes et des termes de glossaire par leur nom.
On l'active avec `MCP_CONTEXT_PROVIDERS=file`.

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_CONTEXT_FILE` | chemin du fichier JSON ; obligatoire, un fichier illisible ou invalide empêche le démarrage | aucune |
| `MCP_CONTEXT_FILE_GLOSSARIES` | noms des glossaires servis, séparés par des virgules | aucune |
| `MCP_CONTEXT_FILE_CLASSIFICATIONS` | noms des ensembles de classifications et d'étiquettes servis | aucune |
| `MCP_CONTEXT_FILE_PROPERTIES` | noms des ensembles de propriétés servis | aucune |
| `MCP_CONTEXT_FILE_SERVE_OWNER` | `true` : le propriétaire d'une table est servi | `false` |

Les listes autorisées sont des noms exacts : `*` est le nom `*`, jamais un joker, et les entrées
vides sont ignorées. Le fichier peut déclarer plus qu'il ne sert ; seul ce qui est déclaré et
autorisé est servi. `--check` affiche le fichier, le nombre de tables et de termes, et les
ensembles servis.

Le fichier est relu, hors de la boucle d'événements, quand il change : un changement est vu dans
un délai d'un `MCP_CONTEXT_TTL_SECONDS`, car le contexte déjà lu est mis en cache, et les
ensembles servis reflètent la dernière lecture du fichier. Un fichier modifié qui
devient invalide garde le dernier contenu valide, et un avertissement nomme le réglage et le
chemin. Un champ `values` ou `samples`, où que ce soit, refuse le fichier : un contexte ne porte
jamais de données. Une table, une colonne, un terme ou une propriété *nommé* `values` ou `samples`
est permis.

Le fournisseur passe en minuscules les noms de catalogue, de schéma, de table et de colonne, comme
Trino les liste, car le cœur les compare exactement ; les clés de termes sont gardées telles
qu'écrites. Un fichier qui contient deux fois la même table, ou la même colonne, une fois passé en
minuscules est refusé au lieu d'être fusionné.

```json
{
  "version": 1,
  "catalogs": ["demo"],
  "tables": [
    {
      "catalog": "demo", "schema": "sales", "table": "orders",
      "description": "One row per order", "owner": "sales-team", "tier": "gold",
      "tags": [{"name": "internal", "set": "visibility"}],
      "terms": ["order"],
      "properties": {"retention": {"value": "7y", "set": "policy"}},
      "grain": ["order_id"],
      "joins": [{"columns": ["customer_id"],
                 "target": {"catalog": "demo", "schema": "sales", "table": "customers"},
                 "target_columns": ["id"]}],
      "columns": {
        "order_id": {"description": "The order", "terms": ["order"],
                     "classifications": [{"name": "key", "set": "visibility"}],
                     "properties": {"unit": {"value": "none", "set": "policy"}}}
      }
    }
  ],
  "terms": {
    "order": {"name": "Order", "glossary": "sales", "definition": "A purchase",
              "status": "approved", "categories": ["core"],
              "relations": [{"key": "line", "name": "Line", "glossary": "sales",
                             "relation": "part"}]}
  }
}
```
