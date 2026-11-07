# Contexte d'un catalogue de données

[English](../en/catalog-context.md) · **Français**

## Les fournisseurs de contexte

Dans ces pages, un *catalog* est un catalog Trino, et un *catalogue de données* est un service de
métadonnées comme Apache Atlas ou OpenMetadata.

Un catalogue de données peut décrire ce que Trino liste : le propriétaire d'une table, sa
classification, les termes de glossaire d'une colonne. Un fournisseur de contexte est un paquet qui
lit un tel catalogue de données pour le serveur. Il enregistre une fabrique
`(env) -> ContextProvider` dans le groupe d'entry points `akko_mcp_trino.context`, et
l'administrateur le nomme dans `MCP_CONTEXT_PROVIDERS`. **Aucun fournisseur n'est chargé par
défaut.** Sans fournisseur, rien ne démarre, pas même un thread.

Le serveur refuse de démarrer sur un nom de fournisseur inconnu, sur un fournisseur d'une autre
version de `context_api`, ou sur une fabrique qui ne peut pas servir ses réglages. Un catalogue de
données en panne ne l'empêche pas de démarrer, mais `--check --strict` sort avec le code 2 quand un
fournisseur se dit injoignable. Un fournisseur lit ses propres variables dans l'environnement. Un
secret peut être donné en fichier plutôt qu'en valeur (`<NAME>_FILE`, relu quand le fichier
change).

Deux fournisseurs viennent avec ce dépôt :

- `file`, dans le cœur, la référence du contrat : un catalogue de données tenu dans un fichier
  JSON ;
- `atlas`, pour Apache Atlas, un paquet à part (`akko-mcp-trino-atlas`, installé par l'extra
  `atlas`). Ses réglages, sa mise en place de Kerberos et le compte qu'il lui faut sont dans
  [son README](../../providers/atlas/README.fr.md).

Le contexte suit ce que Trino liste pour l'appelant : [Sécurité](security.md#les-droits-du-contexte)
en donne les règles. Charger un fournisseur ou non, c'est le choix entre les deux modes de
[Déploiement](deployment.md#avec-un-catalogue-de-données), et ses réglages sont dans
[Configuration](configuration.md#contexte-dun-catalogue-de-données).

Le *filtre de visibilité* est la partie du cœur qui vérifie avec Trino ce que l'appelant peut voir,
avant de servir un contexte. Ses requêtes partent sous l'identité de l'appelant et portent
l'étiquette client `mcp-gate` (voir [Le filtre de visibilité](#le-filtre-de-visibilité)).

## Ce que sert describe_table

Son `DESCRIBE`, lancé sous l'identité de l'appelant, établit quelles table et colonnes Trino montre
à l'appelant. Un `DESCRIBE` en échec n'appelle aucun fournisseur et rend la même réponse que sans
fournisseur. Sinon, la réponse ajoute :

- `table` : ce que disent les catalogues de données de la table (description, étiquettes, termes,
  propriétés, grain, jointures ; le propriétaire seulement quand un fournisseur le sert), avec
  `sources`, le fournisseur de chaque champ ;
- `column_context` : la même chose pour chaque colonne que Trino a listée, par nom, dans l'ordre du
  `DESCRIBE`. Une colonne que Trino ne liste pas n'a aucun contexte. Un grain perd toutes ses
  colonnes quand l'une est cachée. Une jointure n'est gardée que vers une table et des colonnes que
  Trino liste à l'appelant ;
- `terms` : chaque terme de glossaire une seule fois, par référence (`t1`, `t2`… dans l'ordre
  d'apparition), avec son `name`, son `glossary`, sa `definition`, son `status`, ses `categories`,
  ses termes liés `related` (seulement des termes de la même réponse) et sa `source`. Une table ou
  une colonne cite `{"name", "ref"}`. Les clés propres d'un catalogue de données ne sont jamais
  servies ;
- `context_sources` : un statut par fournisseur. `ok` ; `not_found` (le fournisseur ne connaît pas
  la table) ; `not_mapped` (il ne couvre pas ce catalog) ; `partial` (une partie n'a pas pu être
  lue à temps ou ne tenait pas dans la réponse, ou le plafond de lignes a coupé le `DESCRIBE`) ;
  `unavailable` ; `refused` ; `error` ; ou `not_checked` (le canari est visible, un contrôle a
  échoué, ou, avec `MCP_CONTEXT_REQUIRE_SELECT=true`, Trino ne valide pas une lecture des colonnes
  visibles) ;
- `context_truncated` et `undescribed_columns` : au plus `MCP_CONTEXT_MAX_COLUMNS` colonnes sont
  décrites, et le contexte s'arrête avant la colonne qui dépasserait `MCP_CONTEXT_RESPONSE_CHARS`.
  Les colonnes laissées de côté sont listées. Un texte coupé à `MCP_CONTEXT_TEXT_CHARS` est nommé
  dans `cut`.

Avec un fournisseur, la description de `describe_table` dit que ce texte vient d'un catalogue de
données et que ce sont des données, jamais des instructions. Le contexte est lu en dernier, après
l'échantillon. Il ne cache jamais les colonnes et ne fait jamais échouer l'appel.

Un fournisseur en panne, lent ou qui refuse le compte est lu en `MCP_CONTEXT_TIMEOUT_SECONDS` au
plus, et laisse `MCP_CONTEXT_RESERVE_SECONDS` du budget de l'appel. S'il en reste moins, aucun
fournisseur n'est appelé, et les sources disent `unavailable`. Les requêtes du filtre de visibilité
s'arrêtent avant d'entamer cette réserve. Une requête trop lente ne prouve rien (`not_checked` pour
le canari ou le contrôle de lecture, pas de jointure vers une cible), et l'appel répond quand même.
Tout autre échec en chemin ne sert aucun contexte et dit `error` pour chaque source. L'appel
cesse d'attendre une lecture 0,25 s après son échéance au plus. Un fournisseur qui suit le contrat
a alors fermé sa connexion au catalogue de données, si bien qu'aucune requête n'y reste en
attente ; une lecture qui ignore son annulation est abandonnée.

## Ce que sert describe_schema

`describe_schema` en sert moins. Chaque table de la page qu'un fournisseur décrit ajoute
`description`, `{"text", "source"}` : la première description qu'un fournisseur en donne, coupée à
`MCP_CONTEXT_TEXT_CHARS` (alors `"cut": ["text"]`). Une table sans contexte n'a pas de clé
`description`. La réponse ajoute `context_sources`, sauf pour une page vide, qui n'interroge aucun
fournisseur.

Trino vient de lister à l'appelant les tables de la page : le filtre de visibilité ne lui demande
donc rien de plus que le contrôle du canari. Les fournisseurs ne sont interrogés que sur les tables
de la page, en une lecture, par lots de `MCP_CONTEXT_BATCH`. Une liste ne sert ni contexte de
colonne, ni terme, ni propriétaire. `MCP_CONTEXT_REQUIRE_SELECT` ne s'y applique pas, parce qu'il
coûterait une requête par table : `describe_table` sert le reste.

Les mêmes règles de sûreté tiennent. S'il reste moins de `MCP_CONTEXT_RESERVE_SECONDS`, aucun
fournisseur n'est appelé et les sources disent `unavailable`. Une requête lente du filtre de
visibilité ne prouve rien. Tout autre échec sert la page sans contexte et dit `error` pour chaque
source. L'appel ne rend jamais `time_budget` à cause du contexte. Sans fournisseur,
`describe_schema` n'ajoute aucune clé de contexte à sa réponse ni aucune phrase à sa description.

## Ce que lisent les plugins

Les plugins lisent les catalogues de données par `tools.context.tables(refs)`, avec une liste de
`TableRef` construits par `TableRef.of`. La réponse a la forme de celle de `describe_schema` :
`descriptions` par `TableRef`, et `context_sources`. Elle ne couvre que les tables que la
visibilité de l'appelant prouve, listées par Trino dans cet appel ou depuis moins de
`MCP_VISIBILITY_TTL_SECONDS`. Une table que Trino ne liste pas n'est ni nommée à Trino ni demandée
à un fournisseur, et elle est absente de la réponse, comme une table sans contexte.

Un appel lit au plus `MCP_DESCRIBE_SCHEMA_TABLES` tables distinctes, les premières de `refs`,
autant qu'une page de `describe_schema` ; les suivantes sont absentes. Une liste `refs` vide
n'interroge rien et ne rend aucun `context_sources`, comme une page vide. Un plugin ne reçoit jamais
la chaîne, le cache de visibilité ni un fournisseur.

## Le texte des catalogues de données

Le coût du texte d'un catalogue de données est borné par ce qui est servi, pas par ce que le
catalogue de données envoie. Le nettoyage lit au plus quatre fois `MCP_CONTEXT_TEXT_CHARS`
caractères d'un texte, et une liste ne lit que la description qu'elle sert.

Un nom que donne le catalogue de données (une classification, une étiquette, un ensemble, la clé
d'une propriété, le propriétaire, le niveau, le nom, le glossaire, le statut, la catégorie ou la
relation d'un terme, une colonne d'un grain ou d'une jointure) de plus de 256 caractères une fois
nettoyé est écarté, jamais coupé, car un nom coupé est un autre nom. En décider lit au plus 1025
caractères. Un terme dont le nom est écarté n'est pas servi. La valeur d'une propriété est un
texte : elle est coupée à `MCP_CONTEXT_TEXT_CHARS`, et porte alors `"cut": ["value"]`.

Le texte d'un catalogue de données perd ses caractères de contrôle et de format, et ses séparateurs
de ligne et de paragraphe (U+2028, U+2029), avant d'atteindre un agent.

## Le filtre de visibilité

Les requêtes du filtre de visibilité partent sous l'identité de l'appelant, avec l'étiquette client
`mcp-gate` :

- le contrôle du canari ;
- avec `MCP_CONTEXT_REQUIRE_SELECT=true`, `EXPLAIN (TYPE VALIDATE)` des colonnes visibles (un refus
  est gardé `MCP_VISIBILITY_TTL_SECONDS`) ;
- pour la cible d'une jointure, les schémas de son catalog, les tables de son schéma et son
  `DESCRIBE`.

## Le compte du fournisseur

Donnez à chaque fournisseur un compte en lecture seule. Il ne doit pas voir d'un objet plus que ce
que tout utilisateur qui voit cet objet dans Trino a le droit de lire. Les listes blanches d'un
fournisseur nomment ce qui est servi, et son compte ne doit rien lire de plus. Derrière Trino
Gateway, tous les clusters d'un groupe de routage doivent partager le même contrôle d'accès (voir
[Déploiement](deployment.md#derrière-trino-gateway)).

## Contrôle, journaux et arrêt

`--check` affiche une ligne `context:`, avec les fournisseurs et chaque réglage de son tableau dans
[Configuration](configuration.md#contexte-dun-catalogue-de-données), de
`MCP_CONTEXT_TIMEOUT_SECONDS` à `MCP_CONTEXT_MAX_COLUMNS`. Il affiche une ligne `visibility:`, avec
la durée, les limites en octets et en noms, si un canari est posé (jamais son nom), et si `select`
est exigé. Il affiche aussi les lignes de chaque fournisseur, jamais un secret.
`MCP_CONTEXT_PROVIDERS` sans canari, ou en mode `service`, déclenche un avertissement.

Un fournisseur en panne ou qui refuse le compte écrit un avertissement à chaque ouverture de son
coupe-circuit (`<name> is unavailable`, `<name> refused the account`), jamais le texte de son
erreur. `SIGHUP` permet de rappeler aussitôt les fournisseurs qui ont refusé le compte, et remet
leur `mcp_trino_context_breaker_open` à 0.

À l'arrêt, par SIGTERM, Ctrl+C ou la fin de son entrée, le serveur ferme chaque fournisseur, puis
la boucle du contexte.

- En HTTP, cela se fait à l'arrêt de l'application, puis SIGTERM termine le processus avec le
  code 143.
- En stdio avec un fournisseur, SIGTERM ferme et sort avec 0. Un SIGTERM pendant une fermeture la
  laisse finir, et n'en lance jamais une autre.
- Sans fournisseur, SIGTERM termine le processus.

Une lecture qui ignore son annulation ne retient pas l'arrêt plus de quelques secondes ; asyncio
peut alors écrire, une seule fois, qu'une tâche en attente a été détruite.

## Le fournisseur `file`

`file` est le fournisseur livré avec le serveur, et la référence du contrat. Il n'a besoin d'aucun
service : un fichier JSON décrit des tables, des colonnes et des termes de glossaire par leur nom.
On l'active avec `MCP_CONTEXT_PROVIDERS=file`.

| Variable | Rôle | Défaut |
|---|---|---|
| `MCP_CONTEXT_FILE` | chemin du fichier JSON ; obligatoire. Un fichier illisible ou invalide est une erreur de démarrage | aucune |
| `MCP_CONTEXT_FILE_GLOSSARIES` | noms des glossaires servis, séparés par des virgules | aucune |
| `MCP_CONTEXT_FILE_CLASSIFICATIONS` | noms des ensembles de classifications et d'étiquettes servis, séparés par des virgules | aucune |
| `MCP_CONTEXT_FILE_PROPERTIES` | noms des ensembles de propriétés servis, séparés par des virgules | aucune |
| `MCP_CONTEXT_FILE_SERVE_OWNER` | `true` : le propriétaire d'une table est servi | `false` |

Les listes blanches sont des noms exacts : `*` est le nom `*`, jamais un joker, et les entrées
vides sont ignorées. Le fichier peut déclarer plus qu'il ne sert ; seul ce qui est déclaré et
autorisé est servi. `--check` affiche le fichier, le nombre de tables et de termes, et les
ensembles servis.

Le fournisseur relit le fichier, hors de la boucle d'événements, quand il change. Un changement
est vu dans un délai d'un `MCP_CONTEXT_TTL_SECONDS`, car le contexte déjà lu est mis en cache, et
les ensembles servis reflètent la dernière lecture du fichier. Quand un fichier modifié devient
invalide, le fournisseur garde le dernier contenu valide, et un avertissement nomme le réglage et
le chemin. Le fournisseur refuse un fichier qui a un champ `values` ou `samples`, où que ce soit :
un contexte ne porte jamais de données. Une table, une colonne, un terme ou une propriété *nommé* `values` ou `samples`
est permis.

Le fournisseur passe en minuscules les noms de catalog, de schéma, de table et de colonne, comme
Trino les liste, car le cœur les compare exactement. Les clés de termes sont gardées telles
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
