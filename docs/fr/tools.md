# Les tools

[English](../en/tools.md) · **Français**

## Vue d'ensemble

| Tool | Arguments | Rend |
|---|---|---|
| `search_columns` | `text`, `catalog`, `schema`, `cursor` (tous facultatifs sauf `text`) | `{"tables": [{"catalog", "schema", "table", "comment"}], "matches": [{"catalog", "schema", "table", "column", "type", "comment"}], "schemas_searched": n, "not_searched": [{"catalog", "schema", "reason"}], "complete": bool, "next_cursor": token ou null}` : les tables et les colonnes qui contiennent chaque mot du texte. Quand rien ne porte tous les mots, une page de la passe par mot ajoute `"partial": true` et donne à chaque ligne ses `"words"`. `"hint"`, une phrase fixe pour l'agent, vient sur une telle page (suivre `next_cursor` tant qu'il y en a un) et sur une recherche complète qui n'a rien trouvé |
| `list_catalogs` | `offset` (facultatif, 0 par défaut) | `{"catalogs": [...], "next_offset": n ou null, "truncated": bool}` : une page des catalogs que Trino montre |
| `list_schemas` | `catalog`, `offset` (facultatif) | `{"schemas": [...], "next_offset": n ou null, "truncated": bool}` : une page des schémas de ce catalog |
| `list_tables` | `catalog`, `schema`, `offset` (facultatif) | `{"tables": [...], "next_offset": n ou null, "truncated": bool}` : une page des tables et vues de ce schéma |
| `describe_schema` | `catalog`, `schema`, `offset` (facultatif) | `{"catalog", "schema", "tables": [{"table", "type", "comment", "columns": [{"column", "type", "comment"}]}], "next_offset": n ou null, "shortened": bool}` : une page de tables entières avec leur type, leur commentaire et toutes leurs colonnes (nom, type, commentaire), `next_offset`, `shortened` ; avec un fournisseur de contexte, une `description` par table et `context_sources` (voir [Contexte d'un catalogue](catalog-context.md)) |
| `describe_table` | `catalog`, `schema`, `table`, `sample_rows` (0 à 20, première page seulement), `column_offset` (facultatif, 0 par défaut) | une page des colonnes, le plafond de lignes à partir de `column_offset`, avec types et commentaires ; `truncated` quand la page ne les contient pas toutes, `next_column_offset` où commence la page suivante (null sur la dernière), `columns_cut` quand la table a plus de `MCP_DESCRIBE_MAX_COLUMNS` colonnes ; un échantillon gouverné sur demande ; ce qu'ajoutent les descripteurs d'une extension, et `describer_errors` quand l'un a échoué ; avec un fournisseur de contexte, `table`, `column_context`, `terms`, `context_sources`, `context_truncated` et `undescribed_columns` (voir [Contexte d'un catalogue](catalog-context.md)) |
| `profile_table` | `catalog`, `schema`, `table`, `columns`, `mode`, `percent` (tous facultatifs sauf la table) | `{"table", "scope", "rows": n, "columns_cut": bool, "columns": [{"column", "type", "non_null", "null_fraction", "approx_distinct", "min", "max"}]}` : des agrégats des lignes que l'appelant peut lire, calculés par une seule requête sous son identité, donc masques et filtres de lignes s'appliquent comme dans `execute_query`. Les `columns` sont nommées comme `DESCRIBE` les liste, par défaut les `MCP_PROFILE_MAX_COLUMNS` premières ; `DESCRIBE` est lu jusqu'à `MCP_PROFILE_DESCRIBE_ROWS` lignes, et `"columns_cut": true` dit qu'il en listait plus, qu'on ne peut pas nommer. `mode` est l'un de `MCP_PROFILE_MODES` : `sample` (par défaut : les `MCP_PROFILE_SAMPLE_ROWS` premières lignes, `"scope": "first_rows"`, pas un tirage aléatoire), `bernoulli` (`TABLESAMPLE BERNOULLI (percent)`, `0 < percent <= 100`, `"scope": "random_sample"`, parcourt toutes les lignes pour tirer l'échantillon) ou `full` (toutes les lignes que l'appelant peut lire, `"scope": "visible_rows"`) ; un mode que le serveur ne permet pas est refusé avant toute requête, et la description ne nomme que les modes permis. La requête d'agrégats porte l'étiquette client `MCP_PROFILE_CLIENT_TAG` et tourne dans le plus petit de `MCP_PROFILE_TIMEOUT_SECONDS`, de `TRINO_QUERY_TIMEOUT_SECONDS` et du budget de l'appel posés au-dessus de 0 ; au-delà, elle est annulée dans Trino et la réponse est `"kind": "time_budget"`. Au plus `MCP_PROFILE_MAX_CONCURRENT` appels tournent à la fois ; au-delà, `"kind": "busy"`. `null_fraction` vaut null quand `rows` vaut 0. Un type sans ordre (`array`, `map`, `row`, `json`, tout type qu'on ne sait pas ordonné) n'a que son compte et `"skipped": ["distinct", "min", "max"]` ; `min` et `max` sont rendus comme les cellules d'`execute_query`, et un texte ou un binaire plus long que `MCP_PROFILE_VALUE_CHARS` (caractères, ou octets) est coupé dans la requête même, préfixe exact de la vraie valeur, avec `"cut": ["min"]`, `["max"]` ou les deux ; un type plus long que `MCP_PROFILE_TYPE_CHARS` est coupé, avec `"type"` dans `cut` |
| `explain_query` | `sql` | `{"valid": true}` : Trino a analysé une requête en lecture sous l'identité de l'appelant, sans l'exécuter ; `{"plan": "..."}` seulement une fois `EXPLAIN` dans `MCP_EXTRA_READ_COMMANDS` |
| `execute_query` | `sql`, `max_rows` (facultatif, inférieur au plafond du serveur) | `{"columns": [...], "rows": [...], "row_count": n, "truncated": bool}` |

Chaque tool passe par Trino sous le mode d'identité, les tools de liste compris : les métadonnées
sont des données, et Trino ne liste que ce que cette identité peut voir. Chaque description est
écrite pour un modèle et dit ce que rend le tool. Les descriptions de `describe_table` et
d'`execute_query` disent aussi qu'une valeur masquée ou une ligne absente est la policy d'accès, pas
une erreur à retenter.

## Le parcours d'un agent

Un agent suit ce parcours : `search_columns` pour trouver où vit la donnée,
`describe_schema` (ou `describe_table` pour des lignes d'exemple) pour connaître les tables
trouvées, puis `execute_query`. `list_catalogs`, `list_schemas` et `list_tables` servent à
parcourir, quand l'agent veut voir ce qu'il y a plutôt que trouver quelque chose ; les descriptions
des tools et `examples/agent.py` disent la même chose.

## Les listes

Les trois tools de liste rendent une page à la fois : une page compte `TRINO_MAX_ROWS` noms, triés
par nom, et `next_offset` dit où commence la page suivante, ou vaut `null` à la dernière. L'agent
rappelle le tool avec `offset=next_offset` pour lire la suite ; `truncated` est vrai exactement
quand `next_offset` n'est pas `null`. Les schémas et les tables sont paginés dans Trino (`ORDER BY`
et `OFFSET` sur `information_schema.schemata` et `information_schema.tables`) : c'est Trino qui
décide de ce que contient chaque page. Les catalogs sont lus par `SHOW CATALOGS`, qui marche même
sous une policy qui refuse tout `system`, en entier, au-delà du plafond de lignes, jusqu'à
`MCP_MAX_CATALOGS` (10000 par défaut), puis paginés par le serveur ; `system` ne compte pas dans
cette borne. Au-delà, `list_catalogs` rend une erreur, jamais une liste coupée. `list_tables` sur
un schéma inexistant ou invisible pour l'appelant rend `kind` `schema` (« not found or not
visible », la même réponse pour les deux), jamais une liste vide pour un schéma inexistant ou
caché. Un `offset` qui n'est pas un entier positif ou nul est refusé (`invalid_argument`) avant
Trino.

## describe_schema

`describe_schema` donne à l'agent, en un appel, le contexte d'un schéma avant d'écrire du SQL : ses
tables et vues, chacune avec son type, son commentaire et toutes ses colonnes. Il pagine par tables,
dans Trino (`ORDER BY` et `OFFSET` sur `information_schema.tables`) : une page compte au plus
`MCP_DESCRIBE_SCHEMA_TABLES` tables (20 par défaut, 100 au plus), toujours entières, tant que leurs colonnes ne
dépassent pas `MCP_DESCRIBE_SCHEMA_COLUMNS` au total (1000 par défaut). La première table d'une page
vient toujours entière, si large soit-elle ; une page qui compte moins de tables à cause de ce budget
le dit (`shortened: true`), et `next_offset` fait commencer la page suivante à la table laissée de
côté.

Les commentaires viennent de `system.metadata.table_comments` et les colonnes de
`system.jdbc.columns`, deux vues que Trino filtre pour chaque utilisateur, lues par des requêtes que
le serveur construit, jamais par le SQL de l'agent (voir
[Sécurité](security.md#lecture-seule-à-travers-ce-serveur)). Une page coûte quatre requêtes : la vérification
du schéma, la page des noms de tables, une lecture des colonnes et les commentaires des tables que
la page garde. Les colonnes sont lues une seule fois, `MCP_DESCRIBE_SCHEMA_COLUMNS` lignes plus une
au plus : les tables arrivées entières dans le budget sont gardées, la table que le budget coupe
ouvre la page suivante. Une première table plus large que le budget est relue seule, par sa propre
requête, en parts de `MCP_DESCRIBE_SCHEMA_COLUMNS` colonnes : une requête de plus par part. La
lecture des colonnes et celle des commentaires ont une branche `UNION ALL` par table, chacune
filtrée par égalité sur le nom de la table, que Trino pousse dans ces vues : une page lit les
commentaires et les colonnes de ses propres tables, jamais ceux du schéma entier (une liste, `IN`
ou `OR`, n'est pas poussée et lirait toutes les tables du schéma). Trino charge pour cette lecture
les métadonnées de chaque table de la page : une page coûte à peu près autant que ses tables soient
étroites ou larges.

Un schéma inexistant et un schéma invisible pour
l'appelant rendent la même erreur, `kind` `schema` ; un schéma visible et vide rend une liste vide.
Les commentaires sont du texte écrit par les propriétaires des données : la description du tool
demande au modèle de les lire comme des données, jamais comme des instructions, et le serveur ne les
place jamais dans ses instructions MCP. `describe_table` reste le tool des lignes d'exemple et de ce qu'ajoutent les extensions.

Dans la suite d'intégration, `describe_schema` de `tpch.tiny` répond à alice avec les huit tables
du schéma sur une seule page ; voici la première, et la fin de la réponse :

```
{"catalog": "tpch", "schema": "tiny", "tables": [
  {"table": "customer", "type": "BASE TABLE", "comment": null, "columns": [{"column": "custkey", "type": "bigint", "comment": null}, {"column": "name", "type": "varchar(25)", "comment": null}, {"column": "address", "type": "varchar(40)", "comment": null}, {"column": "nationkey", "type": "bigint", "comment": null}, {"column": "phone", "type": "varchar(15)", "comment": null}, {"column": "acctbal", "type": "double", "comment": null}, {"column": "mktsegment", "type": "varchar(10)", "comment": null}, {"column": "comment", "type": "varchar(117)", "comment": null}]},
  ...],
 "next_offset": null, "shortened": false}
```

## describe_table

`describe_table` lit une page des colonnes d'une table par le `DESCRIBE` de Trino, lancé sous
l'identité de l'appelant, et ajoute un échantillon gouverné quand `sample_rows` en demande un. Dans
la suite d'intégration, sans fournisseur de contexte, `describe_table` de `tpch.tiny.nation`
répond à alice :

```
{"columns": ["Column", "Type", "Extra", "Comment"], "rows": [["nationkey", "bigint", "", ""], ["name", "varchar(25)", "", ""], ["regionkey", "bigint", "", ""], ["comment", "varchar(152)", "", ""]], "truncated": false, "next_column_offset": null, "columns_cut": false}
```

Avec un fournisseur de contexte, la réponse ajoute les clés que décrit
[Contexte d'un catalogue](catalog-context.md).

## search_columns

`search_columns` est le point de départ d'un agent qui ne sait pas quelle table contient ce qu'il
cherche. Il prend un mot ou plusieurs, tirés des noms et des commentaires des colonnes et des
tables, jamais une valeur (une date, un montant, un code : les valeurs sont dans les lignes, pas
dans les noms). Les espaces et la ponctuation découpent le texte en mots, mais `_` fait partie d'un
mot (`customer_id` est un seul mot). Chaque mot est cherché sans tenir compte de la casse, abaissé
caractère par caractère comme le fait le `lower()` de Trino. Une colonne correspond quand chaque mot
apparaît dans son nom, son commentaire, le nom de sa table ou le commentaire de sa table, et qu'au
moins un mot apparaît dans son propre nom ou commentaire : « customer segment » trouve
`customers.segment`, et un mot présent seulement dans le nom d'une table ne liste pas toutes ses
colonnes. `tables` donne, avec leur commentaire, les tables dont le nom ou le commentaire contient
chaque mot et les tables qui portent une colonne listée : « archived 2019 » trouve une table
`transactions_2019` commentée « Archived transactions », sans ses colonnes, que `describe_table` lit
ensuite. La recherche porte sur tous les catalogs que l'appelant voit (jamais `system`), sur un
catalog ou sur un schéma. Un schéma nommé est vérifié d'abord : un schéma inexistant et un schéma
invisible pour l'appelant rendent la même erreur, `kind` `schema`, jamais une recherche vide dite
complète ; un catalog que Trino ne connaît pas ou refuse rend l'erreur de Trino.

Chaque mot entre
comme un littéral `LIKE` échappé : un mot ne contient ni apostrophe ni `%`, et son `_` est un
caractère ordinaire. Un mot plus court que `MCP_SEARCH_MIN_LENGTH` (3 par défaut) est ignoré, et un
texte sans aucun mot restant est refusé avec un message qui le dit, pour qu'une lettre ou deux ne
lisent pas toutes les colonnes. Un texte de plus de `MCP_SEARCH_MAX_WORDS` mots (8 par défaut), ou
dont un mot dépasse `MCP_SEARCH_MAX_WORD_LENGTH` caractères (64 par défaut), est refusé avant toute
requête, avec la borne dans le message : chaque mot ajoute quelques `LIKE` à chaque requête. La
recherche lit `system.metadata.table_comments` et `system.jdbc.columns`, que Trino filtre pour
chaque utilisateur, un schéma à la fois dans un ordre stable (les catalogs puis leurs schémas,
triés ; dans un schéma, ses tables puis ses colonnes). Un schéma coûte deux requêtes, ses tables
puis ses colonnes, qui lisent en tout trois fois `system.metadata.table_comments` et deux fois
`system.jdbc.columns` : la requête des tables lit les commentaires pour le texte propre des tables,
puis à nouveau pour les colonnes qu'une table porte, jointes au commentaire de leur table ; la
requête des colonnes lit chaque vue une fois. Sur Hive, la vue des commentaires est la plus
coûteuse, un appel au métastore par table. Chaque lecture filtre le catalog et le schéma par
égalité, ce que Trino pousse jusqu'aux vues : un catalog de milliers de tables répond schéma par
schéma. Elle tient dans le budget de l'appel (`MCP_CALL_TIMEOUT_SECONDS`) : avant chaque schéma,
elle garde `MCP_SEARCH_RESERVE_SECONDS` (2 par défaut) en réserve, chaque requête est arrêtée dans
Trino au temps qui reste, et la recherche rend alors ce qu'elle a fouillé plutôt que l'erreur de
l'appel. `tables` et `matches` s'arrêtent ensemble au plafond de lignes (`TRINO_MAX_ROWS`). La
réponse dit exactement ce qui a été fouillé : `schemas_searched`, `not_searched` avec une raison
pour chaque partie laissée (`time_budget`, `row_cap` ou le `kind` de l'erreur de Trino ; un `schema`
à `null` désigne un catalog entier), et `complete`, vrai seulement quand tout le périmètre a été
fouillé, sur tous les appels de la recherche. `next_cursor` reprend la même recherche là où elle
s'est arrêtée (ce schéma et la suite de son catalog, puis chaque catalog non ouvert), à
l'intérieur des tables ou des colonnes d'un schéma quand le plafond de lignes l'y a arrêtée.

Quand
une recherche de plusieurs mots est complète et qu'aucune page de sa chaîne de curseurs n'a rien qui
porte tous les mots, le même appel poursuit par une passe par mot : chaque mot seul, selon les mêmes
règles, sur le même périmètre, dans le même plafond de lignes, le même budget et les mêmes droits.
Ses lignes portent `words`, les mots qui ont trouvé chacune, les lignes trouvées par le plus de mots
d'abord, et la réponse porte `partial: true` ; une seule passe lit tous les mots, deux requêtes par
schéma comme la première, elle est donc bornée comme la recherche. `complete`, `not_searched`,
`schemas_searched` et `next_cursor` parlent alors de cette seule passe : si elle ne peut pas finir,
elle dit ce qu'elle a laissé et son curseur la reprend, jamais complète. Une page de la passe par
mot ajoute `hint`, une phrase fixe pour l'agent : tant que la page a un `next_cursor`, que la passe
n'est pas finie et qu'il faut rappeler avec lui ; sur sa dernière page, que ce sont des
correspondances partielles, choisir la bonne ou chercher à nouveau un seul mot de nom de colonne ou
de table, puis `describe_table`. Une recherche complète qui n'a rien trouvé du tout, et la
dernière page d'une passe par mot qui n'a rien trouvé sur aucune page, ajoutent l'autre `hint`
fixe : chercher à nouveau un seul mot de nom de colonne ou de
table, jamais une valeur, ou parcourir avec `list_tables` et `describe_schema`. Un agent ne doit pas
conclure d'une recherche vide que la donnée n'existe pas.

Un schéma, ou la liste des schémas d'un
catalog, qui prend plus de temps qu'une requête n'en a droit (`TRINO_QUERY_TIMEOUT_SECONDS`) alors
que l'appel a encore du temps est nommé (`time_budget`) et sauté : le reprendre arrêterait chaque
appel suivant au même endroit. De même pour celui qui prend plus de temps qu'un appel entier, quand
la limite est laissée à Trino (`TRINO_QUERY_TIMEOUT_SECONDS=0`) ou vaut celle de l'appel : un appel
qui manque de temps exactement là où il a repris, sans avoir avancé, le saute, si bien que chaque
appel avance. Un curseur est refusé (`invalid_argument`) pour un autre texte ou un autre périmètre,
pour un catalog que l'appelant ne voit pas, ou s'il a été modifié ; tout curseur rendu par le
serveur reprend, même pour un schéma Hive comme `2024_sales`, car le schéma d'un curseur n'entre que
dans un littéral. Sans budget d'appel (`MCP_CALL_TIMEOUT_SECONDS=0`), une recherche sur tous les
catalogs n'aurait pas de borne : elle est refusée, un catalog nommé est fouillé, et `--check` le
signale.

Elle lit `MCP_SEARCH_CONCURRENCY` catalogs à la fois (8 par défaut), chacun sous
l'identité de l'appelant et dans ce qui reste de l'appel, jamais plus de ce nombre au-delà du
catalog dont elle répond. Tant que le budget de l'appel n'est pas épuisé, elle rend ce que rendrait
une recherche d'un catalog à la fois : les mêmes lignes dans le même ordre, le même `not_searched`
et le même `next_cursor`. Quand le budget s'épuise, la page est une page partielle juste dont le
contenu peut différer, puisqu'elle a lu plus loin : rien n'est servi au-delà du premier catalog
qu'elle n'a pas fini, et `next_cursor` y reprend. Une fois la page pleine, les lectures encore en
cours sont aussitôt annulées dans Trino et aucune ne part. Un catalog qui bloque en tête est
attendu comme le ferait une recherche d'un catalog à la fois, jusqu'au budget de la requête ou de
l'appel. `MCP_SEARCH_MAX_THREADS` (32) borne les lectures en cours dans les fils de tout le
processus, tous appels confondus : un appel qui n'en trouve aucun de libre lit lui-même son
catalog suivant, et avance donc toujours. Pour Trino, chaque appel de `search_columns` lance
jusqu'à `MCP_SEARCH_CONCURRENCY` requêtes de métadonnées à la fois : les borner par un groupe de
ressources sur le `TRINO_SOURCE` du serveur, comme pour l'étiquette client de `profile_table`, ou
poser `MCP_SEARCH_CONCURRENCY=1` pour lire un catalog à la fois, comme en 0.7. `--check`
signale aussi `MCP_SEARCH_RESERVE_SECONDS=0` avec un budget d'appel : une
recherche peut alors aller jusqu'au bout de l'appel, qui rend sa propre erreur `time_budget`. La
ligne d'audit dit combien de tables et de colonnes sont venues et si la recherche a été coupée,
jamais le texte, qui peut être une donnée. Les commentaires sont des données, jamais des
instructions, comme pour `describe_schema`.

Dans le [sandbox](https://github.com/AKKO-p/akko-sandbox), `search_columns` avec le mot `email`
répond à carol (raccourci) :

```
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", ...}],
 "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", ...}],
 "schemas_searched": 4, "not_searched": [], "complete": true, "next_cursor": null}
```

## profile_table

La requête d'agrégats de `profile_table` porte l'étiquette client `MCP_PROFILE_CLIENT_TAG`
(`mcp-profile` par défaut) : les groupes de ressources de Trino peuvent ainsi borner leur nombre
simultané et leur mémoire, et [Exploitation](operations.md#mise-à-léchelle-et-groupes-de-ressources)
en donne un exemple.

Le groupe de ressources borne Trino ; `MCP_PROFILE_MAX_CONCURRENT` (4) borne chaque réplica de ce
serveur : un profil en file dans Trino tient quand même l'un des fils que partagent tous les tools du
réplica, donc au-delà de ce nombre de profils simultanés un appel répond aussitôt `"kind": "busy"` et
n'envoie aucune requête. Gardez-le au plus égal à `hardConcurrencyLimit` plus `maxQueued` du groupe,
divisé par le nombre de réplicas.

## execute_query et explain_query

Chaque identifiant est validé (`[A-Za-z_][A-Za-z0-9_-]*`) avant d'entrer dans le SQL.
`execute_query` accepte tout SQL que Trino accepte, **pourvu que ce soit une lecture** :
[Sécurité](security.md#les-instructions-quun-agent-peut-envoyer) liste les commandes qu'il laisse
passer et celles qu'il refuse.

`execute_query` n'est annoté `readOnlyHint: true` que tant que `TRINO_READ_ONLY` est vrai. Avec
`TRINO_READ_ONLY=false`, ses annotations disent qu'il peut écrire (`readOnlyHint: false`,
`destructiveHint: true`), et sa description aussi, pour que le MCP host demande la confirmation
qu'il sauterait pour un tool en lecture seule. Les huit autres tools ne font que lire, quel que
soit le réglage.

## Les bornes

Les résultats sont plafonnés à `TRINO_MAX_ROWS` (100 par défaut, 1 au moins) : une ligne de plus est
lue, si bien que `truncated` dit si la réponse a été coupée, et le reste de la requête est annulé
dans Trino. Avec `TRINO_QUERY_TIMEOUT_SECONDS` (60 s par défaut), une requête plus longue est annulée
dans Trino et l'agent en est averti. Ce budget est tenu par le serveur, pas posé comme propriété de
session : un moteur de policies qui interdit aux utilisateurs de poser des propriétés de session
refuserait sinon chaque requête. Avec `MCP_CALL_TIMEOUT_SECONDS` (120 s par défaut), un appel de
tool entier a un budget de temps, toutes ses requêtes comprises : au-delà, aucune requête ne
démarre, celle en cours est annulée (même quand le budget s'est épuisé avant que Trino ne l'ait
nommée), et l'agent reçoit `{"error": "the tool call exceeded its time budget…"}`, jamais ce que
l'appel avait rassemblé jusque-là, ni une réponse arrivée après l'échéance. Un plugin qui appelle
son propre backend borne cette requête avec `akko_mcp_trino.audit.time_allowed(timeout)` : son
propre délai, ou ce qui reste du budget de l'appel quand c'est plus court.

Les valeurs par défaut sont choisies pour un agent, pas pour un traitement par lots. Une question
d'agent devrait revenir en moins d'une minute ; une requête plus longue est le plus souvent un
parcours complet que l'agent doit restreindre, et l'erreur qu'il reçoit le dit. Deux minutes par
appel laissent la place à un tool qui lance deux ou trois requêtes, sans laisser le tour d'un agent
bloqué sur un seul tool. Posez l'une ou l'autre à `0` pour retirer la limite (la requête dure alors
ce que permet le `query.max-execution-time` de Trino) ; `--check` affiche les deux.

## Les erreurs

Une erreur revient à l'agent sous la forme `{"error": "...", "kind": "..."}`. `error` est le message entier de
Trino ; `kind` dit quoi en faire, et `identifier` nomme la colonne, la table, le schéma ou le
catalog que Trino n'a pas su résoudre, quand il le nomme. Une colonne que Trino n'a pas trouvée
(`COLUMN_NOT_FOUND`, nommée) dans `execute_query` ou `explain_query` ajoute `hint`, qui nomme la
table, ou chaque table, de la requête de l'agent sur laquelle la colonne était écrite
(`c.customer_id` nomme la table d'alias `c` ; une colonne sans qualificatif nomme toutes les tables
lues, sauf si la requête a une CTE ou une sous-requête) et conseille `describe_table` sur elle. Il
ne nomme que les tables que l'appelant voit : chaque candidate, quatre au plus, est cherchée dans
`information_schema.tables` de son catalog par égalité, au nom de l'appelant, chaque partie telle
qu'écrite (une partie entre guillemets peut contenir un point), dans le temps qui reste à l'appel
moins deux secondes, cinq secondes au plus, pour qu'une recherche lente ne fasse jamais perdre son
erreur à l'agent ; un nom sans catalog est cherché dans `TRINO_CATALOG`, et un nom sans schéma
(`FROM accounts`) n'a pas de conseil, une limite connue ; une table cachée, un refus, une erreur ou
moins de deux secondes restantes ne donnent aucun conseil pour elle ; une colonne ambiguë ou sans nom
n'a pas de conseil, ni une requête que l'analyseur ne sait pas lire, jamais une erreur de plus : le
nom vient de la requête, jamais de Trino, il ne montre donc rien que l'appelant ne pouvait déjà
nommer.

| `kind` | Sens |
|---|---|
| `syntax`, `column`, `table`, `schema`, `catalog`, `function`, `type` | Trino a refusé l'instruction : corriger cette partie |
| `permission` | Trino ou l'IdP a refusé le droit : le rapporter, ne pas réessayer |
| `invalid_argument` | le serveur a refusé un argument (un identifiant, un `offset`, un curseur, un texte de recherche hors bornes) avant d'atteindre Trino |
| `read_only` | l'instruction écrit et le serveur est en lecture seule |
| `time_budget` | l'appel a épuisé son budget de temps : restreindre la demande |
| `busy` | `profile_table` exécute déjà autant d'appels que ce serveur le permet (`MCP_PROFILE_MAX_CONCURRENT`) : rappeler dans quelques secondes, comme le dit son `hint` |
| `unavailable` | Trino n'a pas pu répondre (erreur interne, connexion perdue) : l'instruction n'est ni valide ni invalide, réessayer plus tard |
| `other` | toute autre erreur |


La ligne d'audit garde seulement le code de l'erreur (voir [Audit](operations.md#audit)),
inchangé.
