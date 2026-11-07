# Sécurité

[English](../en/security.md) · **Français**

## Lecture seule à travers ce serveur

La garde de lecture seule couvre **ce qui passe par ce serveur** : chaque instruction qu'envoie un
agent est refusée si une écriture y apparaît, où que ce soit, avant que Trino ne la voie. Elle ne
garantit pas ce qu'un utilisateur peut faire dans Trino par d'autres moyens.

En modes `jwt` et `impersonation`, Trino exécute chaque requête sous le nom de l'utilisateur, avec
les droits que sa policy lui donne. Si cette policy laisse l'utilisateur écrire, il peut toujours
écrire par un autre client Trino (la CLI, un notebook, un outil de BI), avec un token pour Trino
que l'IdP lui délivre directement. Le token que le serveur obtient par échange reste en mémoire et
n'est jamais rendu à l'agent ni au MCP host, mais les droits de l'utilisateur dans Trino sont ce
qu'ils sont. En mode `service`, il en va de même pour `TRINO_USER`, pour qui détient son secret.

Pour une lecture seule de bout en bout, le moteur doit tenir la ligne lui aussi :

- **`service`** : ne donnez à `TRINO_USER` que des droits de lecture. Avec le contrôle d'accès par
  fichier, `{"catalogs": [{"user": "mcp-reader", "catalog": ".*", "allow": "read-only"}]}` ;
  avec OPA ou Ranger, une policy qui donne à cette identité `SELECT` et rien d'autre.
- **`impersonation`** : limitez la règle d'impersonation aux utilisateurs pour qui les agents
  peuvent travailler, et, si ces utilisateurs peuvent écrire ailleurs, ne donnez au compte du
  serveur aucun droit propre au-delà de `SELECT 1`.
- **`jwt`** : ne laissez que le client de ce serveur obtenir par échange des tokens pour
  l'audience de Trino, pour qu'un token que détient le MCP host de l'agent n'ouvre jamais Trino ;
  et donnez aux utilisateurs que les agents atteignent des règles en lecture seule sur les catalogs
  que les agents peuvent atteindre, dans le moteur de policies. Trino applique les mêmes règles à une
  requête quel que soit le client qui l'envoie : la policy est donc l'endroit où les droits d'un
  utilisateur sont en lecture seule pour tous les chemins, celui-ci compris.

La garde reste une seconde ligne, pas la seule : la policy du moteur doit refuser aussi les table
functions et le catalog `system` (voir [Notes de conception](design-notes.md)), **sauf** la lecture
de `system.jdbc` et `system.metadata`, que Trino filtre pour chaque utilisateur comme il filtre
`information_schema` : un utilisateur n'y voit que les tables et colonnes qu'il peut voir ailleurs.
`describe_schema` lit `system.jdbc.columns` et `system.metadata.table_comments`, et
`search_columns` aussi, par des requêtes que le serveur construit ; une policy qui les refuse leur
fait rendre une erreur qui le dit, sans autre voie essayée. Le SQL de l'agent reste refusé sur tout
`system`, ces vues comprises.

Posez aussi `hide-inaccessible-columns=true` dans le `config.properties` de Trino. Sans cela, un
`SELECT *` sur une table dont la policy masque une colonne est refusé, et le refus nomme cette
colonne ; avec cela, la colonne cachée disparaît du résultat et n'est nommée nulle part.

## Trino d'abord

**Le serveur ne décide pas des droits.** Trino en décide, pour chaque appelant ou pour l'identité
configurée. Mettre de la policy dans le serveur dupliquerait, et finirait par contredire, ce que le
moteur impose déjà à tous les autres clients.

En mode `service`, Trino décide pour l'identité unique que vous donnez au serveur : donnez-lui les
droits que tout appelant peut avoir, pas plus, et en lecture seule (voir
[Identité](identity.md#une-identité-pour-tous-les-appelants-service-seulement-si-on-la-nomme)).

## Les instructions qu'un agent peut envoyer

Chaque identifiant est validé (`[A-Za-z_][A-Za-z0-9_-]*`) avant d'entrer dans le SQL.
`execute_query` accepte tout SQL que Trino accepte, **pourvu que ce soit une lecture**. Les
commandes passent par une liste blanche : `SELECT`, `DESCRIBE` d'une table, et `SHOW CATALOGS`,
`SHOW SCHEMAS`, `SHOW TABLES`, `SHOW COLUMNS` et `SHOW FUNCTIONS`. Toute autre commande est refusée,
y compris une commande qu'une version future de Trino ajouterait. Certaines lectures, comme `SHOW STATS` ou `EXPLAIN`,
peuvent révéler des informations au-delà d'un filtre de lignes ou d'un masque de colonne. Elles sont
refusées, sauf si l'administrateur les nomme dans `MCP_EXTRA_READ_COMMANDS` (`EXPLAIN`, `SHOW STATS`,
`SHOW CREATE`, `SHOW GRANTS`, `SHOW ROLES`, `SHOW SESSION`). Une réponse ne porte que des colonnes et
des lignes, jamais les statistiques de la requête calculées par Trino. La requête
est analysée en arbre et refusée si elle contient plus d'une instruction, ou si un `INSERT`,
`UPDATE`, `DELETE`, `MERGE`, `CREATE`, `DROP`, `ALTER`, `GRANT`, `CALL` ou `SET` apparaît n'importe
où dans l'arbre, y compris dans une CTE, une sous-requête ou l'instruction qu'un `EXPLAIN` explique.
`EXPLAIN ANALYZE` est refusé : il exécute ce qu'il explique. Les table functions et le catalog
`system` sont refusés, de même que tout `SHOW` qui nomme `system`. Un nom sans catalog est lu dans
le catalog de session (`TRINO_CATALOG`, `system` par défaut) ; quand c'est `system`, une table
sans catalog est refusée (une CTE n'est pas une table), et seuls `SHOW CATALOGS` et un `SHOW
SCHEMAS`, `SHOW TABLES` ou `SHOW COLUMNS` dont le `FROM` donne le nom complet, catalog compris,
passent. Les tools de métadonnées refusent aussi `system` comme nom de catalog, et
`list_catalogs` ne le liste pas. Réglez donc `TRINO_CATALOG` sur un catalog de données ; la
policy du moteur doit refuser `system` elle aussi, sauf la lecture de `system.jdbc` et
`system.metadata` (voir
[Lecture seule à travers ce serveur](#lecture-seule-à-travers-ce-serveur)), la garde étant une
seconde ligne.

## Les droits du contexte

Le contexte d'un catalogue suit ce que Trino liste pour l'appelant : un nom qui vient d'un
catalogue n'est jamais utilisé avant que Trino ne l'ait listé pour cet appelant, un fournisseur ne
reçoit jamais le client Trino ni l'identité de l'appelant, et il n'écrit jamais dans son catalogue.
Le texte de l'erreur d'un fournisseur n'atteint jamais l'agent, seulement un statut. Un droit
retiré dans Trino peut laisser voir le contexte d'une table jusqu'à `MCP_VISIBILITY_TTL_SECONDS`,
jamais ses données, que Trino refuse aussitôt.
[Déploiement](deployment.md#déploiement-avec-un-catalogue) dit quelles listes blanches ouvrent
quoi.

## Audit

Chaque appel de tool écrit une ligne d'audit, indexée par `X-Request-Id`, jamais le token, jamais
le texte SQL : qui a appelé quel tool, avec quel résultat, pendant combien de temps.
[Exploitation](operations.md#audit) en donne les champs.

## Signaler une vulnérabilité

Écrivez à **security@akko-ai.com**, comme le dit [SECURITY.fr.md](../../SECURITY.fr.md). N'ouvrez
pas d'issue publique.
