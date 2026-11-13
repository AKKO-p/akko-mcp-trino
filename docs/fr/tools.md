# Les tools

[English](../en/tools.md) · **Français**

## Vue d'ensemble

| Tool | Arguments | Rend |
|---|---|---|
| `search_columns` | `text`, `catalog`, `schema`, `cursor` (tous facultatifs sauf `text`) | les tables et les colonnes qui portent chaque mot du texte ; voir [search_columns](#search_columns) |
| `list_catalogs` | `offset` (facultatif, 0 par défaut) | `{"catalogs": [...], "next_offset": n ou null, "truncated": bool}` : une page des catalogs que Trino montre |
| `list_schemas` | `catalog`, `offset` (facultatif) | `{"schemas": [...], "next_offset": n ou null, "truncated": bool}` : une page des schémas de ce catalog |
| `list_tables` | `catalog`, `schema`, `offset` (facultatif) | `{"tables": [...], "next_offset": n ou null, "truncated": bool}` : une page des tables et vues de ce schéma |
| `describe_schema` | `catalog`, `schema`, `offset` (facultatif) | une page de tables, chacune entière, avec leurs commentaires et leurs colonnes ; voir [describe_schema](#describe_schema) |
| `describe_table` | `catalog`, `schema`, `table`, `sample_rows` (de 0 à 20, première page seulement), `column_offset` (facultatif, 0 par défaut) | une page des colonnes, avec types et commentaires, et un échantillon gouverné sur demande ; voir [describe_table](#describe_table) |
| `profile_table` | `catalog`, `schema`, `table`, `columns`, `mode`, `percent` (tous facultatifs sauf la table) | des agrégats des lignes que l'appelant peut lire ; voir [profile_table](#profile_table) |
| `explain_query` | `sql` | `{"valid": true}` : Trino a analysé une instruction en lecture sous l'identité de l'appelant, sans l'exécuter ; `{"plan": "..."}` seulement quand `EXPLAIN` figure dans `MCP_EXTRA_READ_COMMANDS` |
| `execute_query` | `sql`, `max_rows` (facultatif, inférieur au plafond du serveur) | `{"columns": [...], "rows": [...], "row_count": n, "truncated": bool}`, plus `truncated_reason` et `cut_columns` quand ils s'appliquent ; voir [Les bornes](#les-bornes) |

Chaque tool passe par Trino sous le mode d'identité, les tools de liste compris : les métadonnées
sont des données, et Trino ne liste que ce que cette identité peut voir. Chaque description est
écrite pour un modèle et dit ce que rend le tool. Les descriptions de `describe_table`, de
`profile_table` et d'`execute_query` disent aussi qu'une valeur masquée ou une ligne absente est la
policy d'accès, pas une erreur à retenter.

## Le parcours d'un agent

On indique aux agents d'appeler d'abord `search_columns`, pour trouver où vit la donnée. Ils
appellent ensuite `describe_schema` (ou `describe_table` pour des lignes d'exemple) pour connaître
les tables trouvées, et enfin `execute_query`. `list_catalogs`, `list_schemas` et `list_tables`
servent à parcourir, quand l'agent veut voir ce qu'il y a plutôt que trouver quelque chose. Les
descriptions des tools et `examples/agent.py` disent la même chose.

## Les listes

Les trois tools de liste rendent une page à la fois. Une page compte `TRINO_MAX_ROWS` noms, triés
par nom, et `next_offset` dit où commence la page suivante, ou vaut `null` à la dernière page.
L'agent rappelle le tool avec `offset=next_offset` pour lire la suite. `truncated` est vrai
exactement quand `next_offset` n'est pas `null`.

Les schémas et les tables sont paginés dans Trino (`ORDER BY` et `OFFSET` sur
`information_schema.schemata` et `information_schema.tables`) : c'est Trino qui décide de ce que
contient chaque page. Les catalogs sont lus par `SHOW CATALOGS`, qui marche même quand la policy
refuse tout `system`. Le serveur lit la liste entière, sans tenir compte du plafond de lignes,
jusqu'à `MCP_MAX_CATALOGS` (10 000 par défaut), puis la pagine lui-même. `system` ne compte pas
dans cette limite. Au-delà de la limite, `list_catalogs` rend une erreur plutôt qu'une liste
coupée.

`list_tables` sur un schéma inexistant, ou que l'appelant ne peut pas voir, rend une erreur de
`kind` `schema` (« not found or not visible », la même réponse pour les deux), jamais une liste
vide. Le serveur refuse un `offset` qui n'est pas un entier positif ou nul (`invalid_argument`)
avant d'appeler Trino.

## describe_schema

`describe_schema` donne à l'agent, en un appel, le contexte d'un schéma avant qu'il écrive du SQL :
ses tables et vues, chacune avec son type, son commentaire et toutes ses colonnes (nom, type,
commentaire). Il rend `{"catalog", "schema", "tables": [{"table", "type", "comment", "columns":
[{"column", "type", "comment"}]}], "next_offset": n ou null, "shortened": bool}`. Avec un
fournisseur de contexte, il ajoute une `description` par table et `context_sources` (voir
[Contexte d'un catalogue de données](catalog-context.md)).

Il pagine par tables, dans Trino (`ORDER BY` et `OFFSET` sur `information_schema.tables`). Une page
compte au plus `MCP_DESCRIBE_SCHEMA_TABLES` tables (20 par défaut, 100 au plus), toujours
entières, tant que leurs colonnes ne dépassent pas `MCP_DESCRIBE_SCHEMA_COLUMNS` au total (1 000
par défaut). La première table d'une page vient toujours entière, si large soit-elle. Une page qui
compte moins de tables à cause du budget de colonnes le dit (`shortened: true`), et `next_offset`
fait commencer la page suivante à la table laissée de côté.

Les commentaires viennent de `system.metadata.table_comments` et les colonnes de
`system.jdbc.columns`. Trino filtre ces deux vues de son catalog `system` pour chaque utilisateur.
Le serveur les lit par des requêtes qu'il construit lui-même, jamais par le SQL de l'agent (voir
[Sécurité](security.md#lecture-seule-à-travers-ce-serveur)).

Une page coûte quatre requêtes : la vérification du schéma, la page des noms de tables, une
lecture des colonnes et les commentaires des tables que la page garde. Les colonnes sont lues une
seule fois, au plus `MCP_DESCRIBE_SCHEMA_COLUMNS` lignes plus une. Les tables arrivées entières
dans le budget sont gardées, et la table que le budget coupe ouvre la page suivante. Une première
table plus large que le budget est relue seule, en parts de `MCP_DESCRIBE_SCHEMA_COLUMNS`
colonnes, une requête par part.

Les lectures des colonnes et des commentaires ont une branche `UNION ALL` par table, chacune
filtrée par égalité sur le nom de la table, que Trino pousse dans ces vues. Une page lit donc les
commentaires et les colonnes de ses propres tables, jamais ceux du schéma entier. Une liste (`IN`
ou `OR`) n'est pas poussée et lirait toutes les tables du schéma. Trino charge pour cette lecture
les métadonnées de chaque table de la page : une page coûte à peu près autant, que ses tables
soient étroites ou larges.

Un schéma inexistant et un schéma que l'appelant ne peut pas voir rendent la même erreur, de
`kind` `schema`. Un schéma visible et vide rend une liste vide. Les commentaires sont du texte
qu'ont écrit les propriétaires des données : la description du tool demande au modèle de les lire
comme des données, jamais comme des instructions, et le serveur ne les place jamais dans ses
propres instructions MCP.

Dans la suite d'intégration, `describe_schema` sur `tpch.tiny` rend, pour Alice, les huit tables
du schéma sur une seule page. Voici la première table, et la fin de la réponse :

```
{"catalog": "tpch", "schema": "tiny", "tables": [
  {"table": "customer", "type": "BASE TABLE", "comment": null, "columns": [{"column": "custkey", "type": "bigint", "comment": null}, {"column": "name", "type": "varchar(25)", "comment": null}, {"column": "address", "type": "varchar(40)", "comment": null}, {"column": "nationkey", "type": "bigint", "comment": null}, {"column": "phone", "type": "varchar(15)", "comment": null}, {"column": "acctbal", "type": "double", "comment": null}, {"column": "mktsegment", "type": "varchar(10)", "comment": null}, {"column": "comment", "type": "varchar(117)", "comment": null}]},
  ...],
 "next_offset": null, "shortened": false}
```

## describe_table

`describe_table` lit une page des colonnes d'une table par le `DESCRIBE` de Trino, lancé sous
l'identité de l'appelant. Une page compte au plus `TRINO_MAX_ROWS` colonnes, à partir de
`column_offset`, avec leurs types et leurs commentaires.

- `truncated` est vrai quand la page ne contient pas toutes les colonnes, et `next_column_offset`
  dit où commence la page suivante (`null` à la dernière page).
- `columns_cut` est vrai quand la table a plus de `MCP_DESCRIBE_MAX_COLUMNS` colonnes.
- `sample_rows` ajoute un échantillon gouverné, sur la première page seulement.
- Les descripteurs d'un plugin de tools ajoutent leurs propres clés, et `describer_errors` compte
  ceux qui ont échoué.
- Avec un fournisseur de contexte, la réponse ajoute `table`, `column_context`, `terms`,
  `context_sources`, `context_truncated` et `undescribed_columns` (voir
  [Contexte d'un catalogue de données](catalog-context.md)).

`describe_table` reste le tool des lignes d'exemple et de ce qu'ajoutent les extensions. Dans la
suite d'intégration, sans fournisseur de contexte, `describe_table` sur `tpch.tiny.nation` rend,
pour Alice :

```
{"columns": ["Column", "Type", "Extra", "Comment"], "rows": [["nationkey", "bigint", "", ""], ["name", "varchar(25)", "", ""], ["regionkey", "bigint", "", ""], ["comment", "varchar(152)", "", ""]], "truncated": false, "next_column_offset": null, "columns_cut": false}
```

## search_columns

`search_columns` est le point de départ d'un agent qui ne sait pas quelle table contient ce qu'il
cherche. Il rend :

```
{"tables": [{"catalog", "schema", "table", "comment"}],
 "matches": [{"catalog", "schema", "table", "column", "type", "comment"}],
 "schemas_searched": n, "not_searched": [{"catalog", "schema", "reason"}],
 "complete": bool, "next_cursor": token or null}
```

### La correspondance

Le texte est un ou plusieurs mots tirés des noms et des commentaires des colonnes et des tables,
jamais une valeur. Les dates, les montants et les codes sont dans les lignes, pas dans les noms.
Les espaces et la ponctuation découpent le texte en mots, et `_` fait partie d'un mot
(`customer_id` est un seul mot). Chaque mot est cherché sans tenir compte de la casse, mis en
minuscules point de code par point de code, comme le fait le `lower()` de Trino.

Une colonne correspond quand chaque mot apparaît dans son nom, son commentaire, le nom de sa table
ou le commentaire de sa table, et qu'au moins un mot apparaît dans son propre nom ou commentaire.
Ainsi, « customer segment » trouve `customers.segment`, et un mot présent seulement dans le nom
d'une table ne liste pas toutes ses colonnes. `tables` donne, avec leur commentaire, les tables
dont le nom ou le commentaire porte chaque mot, et les tables qui portent une colonne listée. Par
exemple, « archived 2019 » trouve une table `transactions_2019` commentée « Archived
transactions », sans ses colonnes, que `describe_table` lit ensuite.

La recherche porte sur tous les catalogs que l'appelant voit (jamais `system`), sur un catalog ou
sur un schéma. Un schéma nommé est vérifié d'abord : un schéma inexistant et un schéma que
l'appelant ne peut pas voir rendent la même erreur, de `kind` `schema`, jamais une recherche vide
dite complète. Un catalog que Trino ne connaît pas ou refuse rend l'erreur de Trino.

### Les limites du texte

Chaque mot devient un littéral `LIKE` échappé : un mot ne contient ni apostrophe ni `%`, et son `_`
est un caractère ordinaire. Un mot plus court que `MCP_SEARCH_MIN_LENGTH` (3 par défaut) est
ignoré. Un texte sans aucun mot restant est refusé, avec un message qui le dit : une lettre ou deux
ne lisent donc jamais toutes les colonnes. Un texte de plus de `MCP_SEARCH_MAX_WORDS` mots (8 par
défaut), ou dont un mot dépasse `MCP_SEARCH_MAX_WORD_LENGTH` caractères (64 par défaut), est
refusé avant d'appeler Trino, et le message donne la limite. Chaque mot ajoute quelques prédicats
`LIKE` à chaque requête.

### Le coût dans Trino

La recherche lit `system.metadata.table_comments` et `system.jdbc.columns`, que Trino filtre pour
chaque utilisateur. Elle lit un schéma à la fois, dans un ordre stable : les catalogs, puis leurs
schémas, triés ; dans un schéma, ses tables, puis ses colonnes. Un schéma coûte deux requêtes, une
pour ses tables et une pour ses colonnes. Ensemble, elles lisent trois fois
`system.metadata.table_comments` et deux fois `system.jdbc.columns`. La requête des tables lit les
commentaires pour le texte propre des tables, puis à nouveau pour les colonnes qu'une table porte,
jointes au commentaire de leur table. La requête des colonnes lit chaque vue une fois. Sur Hive, la
vue des commentaires est la plus coûteuse, avec un appel au metastore par table. Chaque lecture
filtre le catalog et le schéma par égalité, ce que Trino pousse jusqu'aux vues : un catalog de
milliers de tables est donc parcouru un schéma à la fois.

La recherche lit jusqu'à `MCP_SEARCH_CONCURRENCY` catalogs à la fois (8 par défaut), chacun sous
l'identité de l'appelant et dans ce qui reste de l'appel. Elle ne lit jamais plus de catalogs que
ce nombre en avance sur celui dont elle rend les résultats. Tant que le budget de l'appel n'est pas
épuisé, la réponse est celle d'une recherche d'un catalog à la fois : les mêmes lignes dans le même
ordre, le même `not_searched` et le même `next_cursor`. Quand le budget s'épuise, la page est une
page partielle juste dont le contenu peut différer, puisque la recherche a lu plus loin. Rien
n'est rendu au-delà du premier catalog qu'elle n'a pas fini, et `next_cursor` y reprend. Une fois
la page pleine, les lectures encore en cours sont aussitôt annulées dans Trino, et aucune nouvelle
lecture ne part. Un catalog qui bloque en tête est attendu, comme le ferait une recherche d'un
catalog à la fois, jusqu'au budget de la requête ou de l'appel. `MCP_SEARCH_MAX_THREADS` (32) limite
les lectures en cours dans les threads de tout le processus, tous appels confondus. Un appel qui
ne trouve aucun thread libre lit lui-même son catalog suivant, et avance donc toujours.
[Exploitation](operations.md#mise-à-léchelle-et-groupes-de-ressources) montre comment limiter ces
requêtes dans Trino.

### Le budget de temps

La recherche tient dans le budget de l'appel (`MCP_CALL_TIMEOUT_SECONDS`). Avant chaque schéma,
elle garde `MCP_SEARCH_RESERVE_SECONDS` (2 par défaut) en réserve, et chaque requête est arrêtée
dans Trino au temps qui reste. La recherche rend alors ce qu'elle a déjà parcouru, au lieu
d'échouer sur une erreur de budget de temps. `tables` et `matches` s'arrêtent ensemble au plafond
de lignes (`TRINO_MAX_ROWS`).

Un schéma, ou la liste des schémas d'un catalog, qui prend plus de temps qu'une requête n'en a
droit (`TRINO_QUERY_TIMEOUT_SECONDS`) alors que l'appel a encore du temps est signalé
(`time_budget`) et sauté : le reprendre arrêterait chaque appel suivant au même endroit. Il en va de
même d'un schéma qui prend plus de temps qu'un appel entier, quand la limite est laissée à Trino
(`TRINO_QUERY_TIMEOUT_SECONDS=0`) ou vaut celle de l'appel. Un appel qui manque de temps exactement
là où il a repris, sans avancer, saute ce schéma : chaque appel avance donc.

Sans budget d'appel (`MCP_CALL_TIMEOUT_SECONDS=0`), une recherche sur tous les catalogs n'aurait
pas de limite : elle est refusée, un catalog nommé est quand même parcouru, et `--check` le
signale. `--check` signale aussi `MCP_SEARCH_RESERVE_SECONDS=0` avec un budget d'appel : une
recherche peut alors aller jusqu'au bout de l'appel, qui rend sa propre erreur `time_budget`.

### Ce que dit la réponse

La réponse dit exactement ce qui a été parcouru :

- `schemas_searched` ;
- `not_searched`, avec une raison pour chaque partie laissée de côté (`time_budget`, `row_cap`, ou
  le `kind` de l'erreur de Trino) ; un `schema` à `null` désigne un catalog entier ;
- `complete`, vrai seulement quand tout le périmètre a été parcouru, sur tous les appels de la
  recherche.

`next_cursor` reprend la même recherche là où elle s'est arrêtée : ce schéma et la suite de son
catalog, puis chaque catalog pas encore ouvert. Il reprend à l'intérieur des tables ou des colonnes
d'un schéma quand le plafond de lignes y a arrêté la recherche. Le serveur refuse un curseur
(`invalid_argument`) pour un autre texte ou un autre périmètre, pour un catalog que l'appelant ne
voit pas, ou s'il a été modifié. Tout curseur rendu par le serveur reprend, même pour un schéma
Hive comme `2024_sales`, car le schéma d'un curseur n'entre jamais que dans un littéral.

### La passe par mot

Quand une recherche de plusieurs mots est complète et qu'aucune page de sa chaîne de curseurs ne
porte tous les mots, le même appel poursuit par une passe par mot. Chaque mot est cherché seul,
selon les mêmes règles, sur le même périmètre, dans le même plafond de lignes, le même budget de
temps et les mêmes droits. Ses lignes portent `words`, les mots qui ont trouvé chacune, et les
lignes trouvées par le plus de mots viennent d'abord. La réponse porte `partial: true`. Une seule
passe lit tous les mots, avec deux requêtes par schéma comme avant : elle est donc limitée comme la
recherche. `complete`, `not_searched`, `schemas_searched` et `next_cursor` parlent alors de cette
seule passe. Si elle ne peut pas finir, elle dit ce qu'elle a laissé de côté et son curseur la
reprend ; elle n'est jamais dite complète.

Une page de la passe par mot ajoute `hint`, une phrase fixe pour l'agent. Tant que la page a un
`next_cursor`, le hint dit que la passe n'est pas finie et qu'il faut rappeler avec lui. Sur la
dernière page, il dit que ce sont des correspondances partielles, et qu'il faut choisir la bonne ou
chercher à nouveau un seul mot de nom de colonne ou de table, puis appeler `describe_table`. Une
recherche complète qui n'a rien trouvé du tout, et la dernière page d'une passe par mot qui n'a
rien trouvé sur aucune page, ajoutent l'autre hint fixe : chercher à nouveau un seul mot de nom de
colonne ou de table, jamais une valeur, ou parcourir avec `list_tables` et `describe_schema`. Un
agent ne doit pas conclure d'une recherche vide que la donnée n'existe pas.

La ligne d'audit dit combien de tables et de colonnes sont revenues et si la recherche a été
coupée, jamais le texte, qui peut être une donnée. Les commentaires sont des données, jamais des
instructions, comme pour `describe_schema`.

Dans le [sandbox](https://github.com/AKKO-p/akko-sandbox), `search_columns` avec le mot `email`
rend, pour Carol (raccourci) :

```
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", ...}],
 "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", ...}],
 "schemas_searched": 4, "not_searched": [], "complete": true, "next_cursor": null}
```

## profile_table

`profile_table` rend `{"table", "scope", "rows": n, "columns_cut": bool, "columns": [{"column",
"type", "non_null", "null_fraction", "approx_distinct", "min", "max"}]}`. Ce sont des agrégats des
lignes que l'appelant peut lire, calculés par une seule requête sous son identité : column masks et
row filters s'appliquent donc comme dans `execute_query`.

- **Les colonnes.** Les `columns` sont nommées comme `DESCRIBE` les liste ; par défaut, les
  `MCP_PROFILE_MAX_COLUMNS` premières. `DESCRIBE` est lu jusqu'à `MCP_PROFILE_DESCRIBE_ROWS`
  lignes, et `"columns_cut": true` dit qu'il listait plus de colonnes, qu'on ne peut pas nommer.
- **Les modes.** `mode` est l'un de `MCP_PROFILE_MODES`. `sample`, le mode par défaut, lit les
  `MCP_PROFILE_SAMPLE_ROWS` premières lignes (`"scope": "first_rows"`, pas un tirage aléatoire).
  `bernoulli` utilise `TABLESAMPLE BERNOULLI (percent)`, avec `0 < percent <= 100`
  (`"scope": "random_sample"`), et parcourt toutes les lignes pour tirer l'échantillon. `full` lit
  toutes les lignes que l'appelant peut lire (`"scope": "visible_rows"`). Un mode que le serveur ne
  permet pas est refusé avant toute requête, et la description ne nomme que les modes permis.
- **Le budget de temps.** La requête d'agrégats porte l'étiquette client
  `MCP_PROFILE_CLIENT_TAG`. Elle tourne dans la plus petite valeur non nulle parmi
  `MCP_PROFILE_TIMEOUT_SECONDS`, `TRINO_QUERY_TIMEOUT_SECONDS` et le budget de l'appel. Au-delà,
  Trino annule la requête, et la réponse est `"kind": "time_budget"`.
- **La concurrence.** Au plus `MCP_PROFILE_MAX_CONCURRENT` appels tournent à la fois dans un
  processus. Au-delà, un appel rend aussitôt `"kind": "busy"`.
  [Exploitation](operations.md#mise-à-léchelle-et-groupes-de-ressources) montre comment le
  dimensionner.
- **Les types sans ordre.** `array`, `map`, `row`, `json` et tout type qu'on ne sait pas ordonné
  n'ont que leur compte, avec `"skipped": ["distinct", "min", "max"]`. `null_fraction` vaut `null`
  quand `rows` vaut 0.
- **Les valeurs coupées.** `min` et `max` sont rendus comme les cellules d'`execute_query`. Un texte
  ou un binaire plus long que `MCP_PROFILE_VALUE_CHARS` (caractères, ou octets) est coupé dans la
  requête même, en un préfixe exact de la vraie valeur, avec `"cut": ["min"]`, `["max"]` ou les
  deux. Un type plus long que `MCP_PROFILE_TYPE_CHARS` est coupé, avec `"type"` dans `cut`.

## execute_query et explain_query

`execute_query` accepte tout SQL que Trino accepte, pourvu que ce soit une lecture. Les
identifiants sont validés d'abord. [Sécurité](security.md#les-instructions-quun-agent-peut-envoyer)
liste les instructions que le serveur laisse passer et celles qu'il refuse.

`execute_query` n'est annoté `readOnlyHint: true` que tant que `TRINO_READ_ONLY` est vrai. Avec
`TRINO_READ_ONLY=false`, ses annotations disent qu'il peut écrire (`readOnlyHint: false`,
`destructiveHint: true`), et sa description aussi, pour que le MCP host demande la confirmation
qu'il sauterait pour un tool en lecture seule. Les huit autres tools ne font que lire, quel que
soit le réglage.

## Les bornes

Les résultats sont plafonnés à `TRINO_MAX_ROWS` (100 par défaut, au moins 1). Le serveur lit une
ligne de plus, si bien que `truncated` dit si la réponse a été coupée, et le reste de la requête
est annulé dans Trino.

Une réponse d'`execute_query` est aussi bornée en taille, quoi que contiennent les lignes. Deux
clés, absentes sauf si elles s'appliquent, disent ce qui a été coupé :

- **Une cellule** de plus de `MCP_MAX_CELL_CHARS` caractères (4000 par défaut) est coupée. Un texte
  est coupé à ce nombre de caractères ; une valeur binaire, rendue en hexadécimal, à ce nombre de
  chiffres ; toute autre valeur (un tableau, une map, une row, un nombre, une date) dont le texte
  JSON est plus long est remplacée par ce texte coupé. `cut_columns` n'apparaît que si une cellule
  des lignes rendues a été coupée, et liste les noms de leurs colonnes, dans l'ordre de `columns`,
  chacun une fois.
- **La réponse** entière reste dans `MCP_MAX_RESULT_BYTES` octets (1 Mio par défaut), mesurés sur
  le JSON que le tool rend. Les lignes s'ajoutent dans l'ordre tant que cela tient ; la première
  qui ne tiendrait pas, et celles d'après, sont écartées. `row_count` dit combien sont rendues,
  `truncated` est vrai et `truncated_reason` vaut `"bytes"`. Quand même la première ligne ne tient
  pas, la réponse n'a aucune ligne, `truncated` vrai et la même raison. Une instruction dont les
  noms de colonnes dépassent à eux seuls la borne est refusée (`invalid_argument`).
- `truncated_reason` n'est présent que si `truncated` est vrai : `"rows"` quand le plafond de
  lignes (`TRINO_MAX_ROWS` ou `max_rows`) a coupé la réponse, `"bytes"` quand la borne de taille
  l'a fait, y compris quand les deux s'appliquaient. Pour lire la suite, choisissez moins de
  colonnes ou des valeurs plus courtes, ajoutez `LIMIT`, ou agrégez.

La ligne d'audit de l'appel note les lignes rendues et `truncated`, et la raison, `rows` ou
`bytes`, dans `truncated_reason` avec le module `fingerprint-audit` ; jamais les données ni
l'instruction.

Les commentaires que Trino porte sur les tables et les colonnes, rendus par `describe_table`,
`describe_schema` et `search_columns`, sont du texte qu'ont écrit les propriétaires des données.
Ils sont débarrassés des caractères de contrôle, de format (contrôles bidirectionnels et
caractères de largeur nulle compris) et de séparation, et coupés à `MCP_COMMENT_CHARS` caractères
(1000 par défaut), comme le texte d'un catalogue. Une entrée dont le commentaire a été coupé dit
`"cut": ["comment"]` ; `describe_table`, dont les rows sont des listes, dit
`"comments_cut": [<noms des colonnes>]` sur la réponse.

Avec `TRINO_QUERY_TIMEOUT_SECONDS` (60 s par défaut), le serveur annule dans Trino une requête plus
longue et prévient l'agent. Le serveur tient ce budget lui-même au lieu de poser une propriété de
session : un moteur de policies qui interdit aux utilisateurs de poser des propriétés de session
refuserait sinon chaque requête.

Avec `MCP_CALL_TIMEOUT_SECONDS` (120 s par défaut), un appel de tool entier a un budget de temps,
toutes ses requêtes comprises. Au-delà, aucune requête ne démarre, et la requête en cours est
annulée, même quand le budget s'est épuisé avant que Trino ne l'ait nommée. L'agent reçoit
`{"error": "the tool call exceeded its time budget…"}`, jamais ce que l'appel avait rassemblé
jusque-là, et jamais une réponse arrivée après l'échéance. Un plugin qui appelle son propre backend
limite cette requête avec `akko_mcp_trino.audit.time_allowed(timeout)`, qui rend son propre délai,
ou ce qui reste du budget de l'appel quand c'est plus court.

Les valeurs par défaut sont choisies pour un agent, pas pour un traitement par lots. Une question
d'agent devrait revenir en moins d'une minute. Une requête plus longue est le plus souvent un
parcours complet que l'agent doit restreindre, et l'erreur qu'il reçoit le dit. Deux minutes par
appel laissent la place à un tool qui lance deux ou trois requêtes, sans bloquer le tour d'un agent
sur un seul tool. Posez l'une ou l'autre valeur à `0` pour retirer la limite ; la requête dure
alors ce que permet le `query.max-execution-time` de Trino. `--check` affiche les deux.

## Les erreurs

Une erreur revient à l'agent sous la forme `{"error": "...", "kind": "..."}`. `error` est le
message entier de Trino. Un refus de droit de Trino est une erreur ordinaire, pas un plantage.
`kind` dit quoi en faire, et `identifier` nomme la colonne, la table, le schéma ou le catalog que
Trino n'a pas su résoudre, quand Trino le nomme.

| `kind` | Sens |
|---|---|
| `syntax`, `column`, `table`, `schema`, `catalog`, `function`, `type` | Trino a refusé l'instruction : corriger cette partie |
| `permission` | Trino ou l'identity provider a refusé le droit : le rapporter, ne pas réessayer |
| `invalid_argument` | le serveur a refusé un argument (un identifiant, un `offset`, un curseur, un texte de recherche hors limites, une instruction trop longue ou trop imbriquée) avant d'appeler Trino |
| `read_only` | l'instruction écrit et le serveur est en lecture seule |
| `time_budget` | l'appel a épuisé son budget de temps, ou son instruction n'a pu être contrôlée dans `MCP_SQL_PARSE_SECONDS` : restreindre ou simplifier la demande |
| `busy` | `profile_table` exécute déjà autant d'appels que ce serveur le permet (`MCP_PROFILE_MAX_CONCURRENT`), ou une instruction du même appelant est encore en cours de contrôle : rappeler dans quelques secondes |
| `unavailable` | Trino n'a pas pu répondre (erreur interne, connexion perdue), ou le processus qui contrôlait l'instruction a échoué : cela ne dit rien de la validité de l'instruction, réessayer plus tard |
| `other` | toute autre erreur |

Quand Trino ne trouve pas une colonne (`COLUMN_NOT_FOUND`, avec son nom) dans `execute_query` ou
`explain_query`, l'erreur ajoute un `hint`. Le hint nomme la table sur laquelle la colonne était
écrite et conseille d'appeler `describe_table` sur elle.

- Une colonne qualifiée (`c.customer_id`) nomme la table d'alias `c`.
- Une colonne non qualifiée nomme toutes les tables que lit l'instruction, sauf si l'instruction a
  une CTE ou une sous-requête.
- Le hint ne nomme que des tables que l'appelant voit. Chaque candidate (quatre au plus) est
  cherchée dans `information_schema.tables` de son catalog par égalité, sous l'identité de
  l'appelant, chaque partie telle qu'écrite (une partie entre guillemets peut contenir un point).
- La recherche dispose du temps qui reste à l'appel moins deux secondes, et de cinq secondes au
  plus. Une recherche lente ne fait jamais perdre son erreur à l'agent.
- Un nom de table sans catalog est cherché dans `TRINO_CATALOG`. Un nom sans schéma
  (`FROM accounts`) n'a pas de hint. C'est une limite connue.
- Aucun hint n'est donné pour une table cachée, une recherche refusée ou en échec, moins de deux
  secondes restantes, une colonne ambiguë ou sans nom, ou une instruction que l'analyseur ne sait
  pas lire. Un hint absent n'ajoute jamais d'erreur.

Le nom de la table vient de l'instruction de l'agent, jamais de Trino : le hint ne montre donc rien
que l'appelant ne pouvait déjà nommer. La ligne d'audit garde seulement le code de l'erreur (voir
[Audit](operations.md#audit)).
