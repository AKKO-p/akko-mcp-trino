# Sécurité

[English](../en/security.md) · **Français**

## Lecture seule à travers ce serveur

Le contrôle de lecture seule couvre **ce qui passe par ce serveur**. Le serveur refuse chaque
instruction qu'envoie un agent si une écriture y apparaît, où que ce soit, avant que Trino ne la
voie. Ce contrôle ne garantit pas ce qu'un utilisateur peut faire dans Trino par d'autres moyens.

En modes `jwt` et `impersonation`, Trino exécute chaque requête au nom de l'utilisateur, avec les
droits que sa policy lui donne. Si cette policy laisse l'utilisateur écrire, il peut toujours
écrire par un autre client Trino (la CLI, un notebook, un outil de BI), avec un token pour Trino
que l'identity provider lui délivre directement. Le token que le serveur obtient par échange reste
en mémoire et n'est jamais rendu à l'agent ni au MCP host. Cela ne change rien aux droits de
l'utilisateur dans Trino. En mode `service`, il en va de même pour `TRINO_USER`, pour qui détient
son secret.

Pour une lecture seule de bout en bout, le moteur doit l'imposer lui aussi :

- **`service`** : ne donnez à `TRINO_USER` que des droits de lecture. Avec le contrôle d'accès par
  fichier, utilisez `{"catalogs": [{"user": "mcp-reader", "catalog": ".*", "allow": "read-only"}]}` ;
  avec OPA ou Ranger, une policy qui donne à cette identité `SELECT` et rien d'autre.
- **`impersonation`** : limitez la règle d'impersonation aux utilisateurs pour qui les agents
  peuvent travailler. Si ces utilisateurs peuvent écrire ailleurs, ne donnez au compte du serveur
  aucun droit propre au-delà de `SELECT 1`.
- **`jwt`** : ne laissez que le client de ce serveur obtenir par échange des tokens pour l'audience
  de Trino, pour qu'un token détenu par le MCP host de l'agent n'ouvre jamais Trino. Dans le moteur
  de policies, donnez aux utilisateurs pour qui travaillent les agents des règles en lecture seule
  sur les catalogs que ces agents peuvent atteindre. Trino applique les mêmes règles à une requête
  quel que soit le client qui l'envoie : la policy est donc l'endroit où les droits d'un
  utilisateur deviennent en lecture seule pour tous les chemins, celui-ci compris.

Le contrôle du serveur est une deuxième ligne de défense, pas la seule. La policy du moteur doit
aussi refuser les table functions et le catalog `system` (voir [Notes de conception](design-notes.md)),
**sauf** la lecture de `system.jdbc` et `system.metadata`. Trino les filtre pour chaque
utilisateur, comme il filtre `information_schema` : un utilisateur n'y voit que les tables et les
colonnes qu'il peut voir ailleurs. `describe_schema` et `search_columns` lisent
`system.jdbc.columns` et `system.metadata.table_comments` par des requêtes que le serveur
construit. Une policy qui les refuse fait rendre à ces tools une erreur qui le dit, et aucune autre
voie n'est essayée. Le SQL de l'agent reste refusé sur tout `system`, ces vues comprises.

Posez aussi `hide-inaccessible-columns=true` dans le `config.properties` de Trino. Sans cela, un
`SELECT *` sur une table dont la policy cache une colonne est refusé, et le refus nomme cette
colonne. Avec cela, la colonne cachée disparaît du résultat et n'est nommée nulle part.

## Trino d'abord

**Le serveur ne décide pas des droits.** Trino en décide, pour chaque appelant ou pour l'identité
configurée. Mettre des règles d'accès dans le serveur dupliquerait, et finirait par contredire, ce
que le moteur impose déjà à tous les autres clients.

En mode `service`, Trino décide pour l'identité unique que vous donnez au serveur. Donnez à cette
identité des droits en lecture seule, et seulement sur les données que tout appelant peut lire
(voir [Identité](identity.md#une-identité-pour-tous-les-appelants-service-seulement-si-on-la-nomme)).

## Les instructions qu'un agent peut envoyer

Un nom de catalogue, de schéma ou de table donné à un tool est pris tel qu'il est écrit : tout
texte de 1 à 1024 caractères sans caractère de contrôle, comme `orders$snapshots`, `2024_sales`
ou `données`. Il n'entre dans le SQL que comme identifiant entre guillemets doubles, chaque
guillemet double doublé, ou comme littéral de chaîne. Un nom qui porte un caractère de contrôle,
un contrôle bidirectionnel (U+061C, U+200E, U+200F, U+202A à U+202E, U+2066 à U+2069) ou un
séparateur de ligne ou de paragraphe (U+2028, U+2029) est refusé avant que Trino ne soit
interrogé, et le refus le montre échappé ; l'antiliant et le liant sans chasse, dont certaines
écritures ont besoin, sont acceptés.
`execute_query` accepte tout SQL que Trino accepte, **pourvu que ce soit une lecture**.

- **Les commandes passent par une liste blanche** : `SELECT`, `DESCRIBE` d'une table,
  `SHOW CATALOGS`, `SHOW SCHEMAS`, `SHOW TABLES`, `SHOW COLUMNS` et `SHOW FUNCTIONS`. Toute autre
  commande est refusée, y compris une commande qu'une version future de Trino ajouterait.
- Certaines lectures, comme `SHOW STATS` ou `EXPLAIN`, peuvent révéler des informations au-delà
  d'un filtre de lignes ou d'un masque de colonne. Elles sont refusées, sauf si l'administrateur
  les nomme dans `MCP_EXTRA_READ_COMMANDS` (`EXPLAIN`, `SHOW STATS`, `SHOW CREATE`,
  `SHOW GRANTS`, `SHOW ROLES`, `SHOW SESSION`).
- **Une instruction est bornée avant d'être analysée, puis analysée dans un délai.** Une
  instruction de plus de `MCP_MAX_SQL_CHARS` caractères (100000 par défaut), ou dont les
  parenthèses et crochets s'imbriquent sur plus de `MCP_MAX_SQL_NESTING` niveaux (32 par défaut),
  est refusée avant que l'analyseur la voie. Une instruction dans ces bornes est analysée une
  fois, dans l'un des `MCP_SQL_PARSE_WORKERS` processus d'analyse (2 par défaut), jamais dans le
  processus du serveur, et chaque contrôle lit cette analyse unique. Un processus qui n'a pas
  répondu dans `MCP_SQL_PARSE_SECONDS` (2 par défaut) est tué et remplacé, et l'appel est refusé
  (`time_budget`) ; de même pour un appel qui ne trouve aucun processus libre dans ce délai, et un
  processus en échec fait refuser son appel. Un SQL très imbriqué ou répété n'occupe donc plus le
  serveur. Un appelant fait analyser une instruction à la fois, si bien qu'il n'occupe qu'un
  processus au plus : un second appel pendant l'analyse du premier attend son tour, et il est
  refusé (`busy`) si son tour n'est pas venu dans `MCP_SQL_PARSE_SECONDS`. Les appels sans
  appelant vérifié comptent pour le compte Trino configuré et partagent ce même tour. Un appel
  attend au plus deux fois `MCP_SQL_PARSE_SECONDS` : son tour et un processus, puis sa réponse. Les processus démarrent au premier
  usage, sous Linux, macOS et Windows, n'écrivent nulle part (leurs flux standard sont le
  périphérique nul, et le protocole passe par des tubes qui leur sont propres), ne reçoivent
  aucun réglage du serveur et s'arrêtent avec lui. Le risque qui reste : un appelant authentifié peut occuper un processus pendant le
  délai, une fois à la fois, si bien qu'autant d'appelants qu'il y a de processus peuvent retarder
  les autres ; des quotas par utilisateur sont prévus.
- **Ce qu'un agent lit est borné, et un commentaire est un texte non fiable.** Une réponse
  d'`execute_query` contient au plus `MCP_MAX_RESULT_BYTES` octets de JSON (1 Mio par défaut) et
  une cellule au plus `MCP_MAX_CELL_CHARS` caractères (4000 par défaut), quel que soit son type :
  une cellule énorme ou un résultat large ne peut pas inonder le contexte d'un agent, et la
  réponse dit ce qu'elle a coupé (`truncated_reason`, `cut_columns`). Un commentaire que Trino
  porte sur une table ou une colonne, dans `describe_table`, `describe_schema` et
  `search_columns`, est débarrassé des caractères de contrôle, bidirectionnels et de largeur nulle
  et coupé à `MCP_COMMENT_CHARS` caractères (1000 par défaut), comme le texte d'un catalogue, et
  l'entrée dit quand il l'a été. Les commentaires restent des données : les descriptions de ces trois
  tools demandent au modèle de les lire comme des données, jamais comme des instructions.
- **Les valeurs des lignes sont des données, et leurs caractères cachés peuvent être retirés.**
  Les lignes d'`execute_query`, le `sample` de `describe_table` et les `min` et `max` de
  `profile_table` contiennent ce qui a été écrit dans les tables, parfois par des personnes
  extérieures à l'organisation (un formulaire, un ticket, un avis). Les descriptions de ces trois
  tools demandent au modèle de les lire comme des données, jamais comme des instructions. Avec
  `MCP_CLEAN_CELLS=true`, le serveur retire en plus de leurs valeurs texte les caractères qu'il
  retire des commentaires : les caractères de largeur nulle, les contrôles bidirectionnels, les
  caractères d'étiquette Unicode (U+E0001 et U+E0020 à U+E007F, les caractères attribués du bloc
  U+E0000 à U+E007F), qui peuvent porter un texte qu'un lecteur humain ne voit pas, et les autres
  caractères de contrôle, de format et de séparation ; le saut de ligne et la tabulation restent.
  La réponse dit ce qui a été nettoyé (`cleaned_columns`, `"cleaned"` dans `profile_table`).
  L'option est désactivée par défaut, pour trois raisons. Une requête doit rendre ce que la base
  contient. Certains de ces caractères sont légitimes : l'antiliant sans chasse (ZWNJ) du persan
  et de l'arabe, le liant sans chasse (ZWJ) des séquences d'emoji, les marques bidirectionnelles, les retours
  chariot ; les retirer change la valeur, si bien qu'un filtre sur une valeur nettoyée peut ne rien
  trouver. Enfin, seul le texte caché disparaît : un texte visible rédigé comme une instruction
  reste, de même que les caractères des autres catégories Unicode, comme les sélecteurs de
  variante et les points de code non attribués. L'option réduit la place laissée aux instructions cachées ; elle ne neutralise pas
  l'injection de prompt. La ligne d'audit et les métriques sont les mêmes avec ou sans elle.
- **Les écritures sont refusées n'importe où dans l'arbre.** L'instruction est analysée en arbre.
  Elle est refusée si elle contient plus d'une instruction, ou si un `INSERT`, `UPDATE`, `DELETE`,
  `MERGE`, `CREATE`, `DROP`, `ALTER`, `GRANT`, `CALL` ou `SET` apparaît n'importe où dans l'arbre,
  y compris dans une CTE, une sous-requête ou l'instruction qu'un `EXPLAIN` explique.
  `EXPLAIN ANALYZE` est refusé, parce qu'il exécute ce qu'il explique, de même qu'un `EXPLAIN`
  d'un `EXPLAIN`.
- **Les table functions et `system` sont refusés**, de même que tout `SHOW` qui nomme `system`.
  Les tools de métadonnées refusent aussi `system` comme nom de catalog, et `list_catalogs` ne le
  liste pas.
- **Un nom sans catalog** est lu dans le catalog de session (`TRINO_CATALOG`, `system` par
  défaut). Quand c'est `system`, une table sans catalog est refusée (une CTE n'est pas une table).
  Seuls `SHOW CATALOGS`, et un `SHOW SCHEMAS`, `SHOW TABLES` ou `SHOW COLUMNS` dont le `FROM` donne
  le nom complet, catalog compris, passent.

Réglez donc `TRINO_CATALOG` sur un catalog qui contient des données, jamais `system`. La policy du moteur doit aussi refuser
`system`, sauf la lecture de `system.jdbc` et `system.metadata` (voir
[Lecture seule à travers ce serveur](#lecture-seule-à-travers-ce-serveur)) ; le contrôle du
serveur est une deuxième ligne de défense, pas la seule.

## Les droits du contexte

Le contexte d'un catalogue de données suit ce que Trino liste pour l'appelant. Un nom qui vient
d'un catalogue de données n'est jamais utilisé avant que Trino ne l'ait listé pour cet appelant.
Un fournisseur ne reçoit jamais le client Trino ni l'identité de l'appelant, et il n'écrit jamais
dans son catalogue de données. Le texte de l'erreur d'un fournisseur n'atteint jamais l'agent,
seulement un statut. Un droit retiré dans Trino peut laisser voir le contexte d'une table jusqu'à
`MCP_VISIBILITY_TTL_SECONDS`, jamais ses données, que Trino refuse aussitôt.
[Déploiement](deployment.md#avec-un-catalogue-de-données) dit quelles listes blanches ouvrent quoi.

## Authentification

Le serveur vérifie le token de chaque appelant avec les clés publiées par l'identity provider
(JWKS), ainsi que son issuer, son audience et son expiration
([Identité](identity.md#la-vérification-du-token)). La lecture du JWKS est le seul appel réseau de ce
contrôle, et l'en-tête `kid` d'un token choisit la clé cherchée ; les lectures sont donc bornées,
quoi que disent les tokens :

- Un token est vérifié dans un thread de travail : un identity provider lent ou injoignable ne
  retient jamais les autres requêtes, y compris celles qui n'ont pas de token.
- Une lecture du JWKS dure au plus `MCP_JWKS_TIMEOUT_SECONDS` (5 par défaut). Les clés sont
  gardées `MCP_JWKS_CACHE_SECONDS` (300).
- Le JWKS est relu au plus une fois toutes les `MCP_JWKS_MIN_REFRESH_SECONDS` (30), lecture en
  échec comprise. Un token qui nomme une clé que le serveur ne détient pas est refusé sans
  lecture quand la dernière lecture est plus récente. Une seule lecture tourne à la fois, et
  personne ne l'attend.
- Une lecture en échec garde les clés déjà détenues : les tokens signés par une clé connue restent
  acceptés tant que l'identity provider est injoignable, et un token signé par une nouvelle clé
  l'est dès qu'une lecture réussit.

Une lecture en échec est journalisée en avertissement, avec le seul type de l'erreur. `/metrics`
compte les lectures dans `mcp_trino_jwks_fetches_total` (par `result`, `ok` ou `failed`) et met
`mcp_trino_jwks_up` à 0 après une lecture en échec. Placez une alerte dessus. `/ready` ne vérifie
pas le JWKS : les clés détenues continuent de vérifier les tokens pendant une panne de l'identity
provider, et retirer tous les pods du service transformerait cette panne en panne totale.

Un réglage booléen vaut `true` ou `false`, rien d'autre ([Configuration](configuration.md)) : une
faute de frappe dans `MCP_AUTH_ENABLED`, `MCP_AUTH_REQUIRED` ou `TRINO_READ_ONLY` arrête le serveur
au démarrage, et ne coupe jamais le contrôle.

## Audit

Chaque appel de tool écrit une ligne d'audit, indexée par `X-Request-Id`, comme chaque requête
refusée avant qu'un tool ne tourne : pas de token, un token refusé (avec la raison : expiré, autre
audience, mauvaise signature…), un client ou un scope qu'une extension refuse, un hôte, un corps
ou une session que le transport refuse. Chaque ligne dit quand (`ts`, UTC), depuis quelle adresse
et, pour un appel, qui a appelé, sous quelle identité Trino a exécuté ses requêtes et avec quels
identifiants de requête. Aucune ligne ne contient le token, un claim d'un token non
vérifié, ni le texte SQL. Le serveur n'envoie de télémétrie à personne.
[Exploitation](operations.md#audit-et-journaux) donne chaque champ et chaque raison.

## Signaler une vulnérabilité

Écrivez à **security@akko-ai.com**, comme le dit [SECURITY.fr.md](../../SECURITY.fr.md). N'ouvrez
pas d'issue publique.
