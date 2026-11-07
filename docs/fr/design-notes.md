# Notes de conception

[English](../en/design-notes.md) · **Français**

## Pourquoi

Le [README](https://github.com/AKKO-p/akko-mcp-trino/blob/main/README.fr.md#pourquoi) dit pourquoi
ce serveur existe : un agent a besoin d'une surface petite et prévisible devant Trino, et Trino
continue de décider des droits.

## Notes

**Le middleware est en ASGI pur, pas `BaseHTTPMiddleware`.** `BaseHTTPMiddleware` exécute le reste
de l'application dans une tâche à part et casse la propagation des ContextVar : l'appelant vérifié
n'atteindrait jamais les tools.

**Le montage ne dépend pas du transport.** Un seul chemin de code construit l'application du
transport et y monte le middleware, quel que soit le transport. Un test le vérifie pour chaque
transport, parce que le middleware a un temps vécu sur un seul chemin de code, que le transport par
défaut ne prenait jamais.

**Chaque tool passe par le mode d'identité, pas seulement `execute_query`.** Lister les catalogs,
les schémas et les tables, c'est lire des métadonnées, et les métadonnées sont des données. Un test
parcourt chaque tool pour vérifier qu'il atteint Trino de la même façon.

**Le contrôle de lecture seule se fait sur l'arbre, n'importe où dans l'arbre.** Un contrôle du
premier mot-clé laisse passer `WITH w AS (DELETE FROM t) SELECT 1`. Un contrôle du nœud racine
aussi, jusqu'à ce qu'un test adverse contre un vrai Trino le prenne en défaut. Le contrôle parcourt
désormais tout l'arbre.

**Les table functions sont refusées.** `TABLE(pg.system.query(query => '...'))` s'analyse comme un
simple `SELECT`, et pourtant elle envoie du SQL brut à la source sous le compte propre du
connecteur : le moteur n'applique alors ni row filters ni column masks. Le contrôle de lecture
seule refuse toute table function et le catalog `system`, qui montre le SQL des autres utilisateurs
dans `system.runtime.queries`. La policy du moteur doit les refuser aussi, `system.jdbc` et
`system.metadata` à part : le contrôle du serveur est une deuxième ligne de défense, pas la seule.

**Pas de token passthrough.** La spécification d'autorisation de MCP interdit de transmettre à un
autre service un token émis pour un service : cet autre service pourrait faire confiance à un token
qui ne lui était pas destiné (le « confused deputy »). Le serveur n'accepte que les tokens dont
l'audience est la sienne, refuse un token émis pour Trino, et obtient pour Trino un token à lui par
OAuth 2.0 Token Exchange (RFC 8693). Là où l'identity provider ne sait pas échanger de tokens,
`impersonation` est le fallback explicite ; il n'est jamais choisi en silence.
[MIGRATING.md](../../MIGRATING.md) décrit les versions qui transmettaient le token de l'appelant.

**L'identité vérifiée, jamais celle que nomme l'appelant.** Le nom de l'appelant vient toujours de
son token vérifié. Un appelant qui envoie un en-tête nommant un utilisateur Trino ne change rien :
le serveur construit sa propre requête vers Trino. Un mot de passe, un token ou un certificat client
ne circule jamais en http simple, et un mode d'identité que le cœur ne connaît pas empêche le
démarrage plutôt que de se replier sur un autre.

**Sûr par défaut.** Par défaut, chaque requête s'exécute au nom de l'appelant, et une requête sans
token valide est refusée. Le mode `service` et une vérification du token coupée restent possibles,
choisis par leur nom, et annoncés par des avertissements. Les limites en lignes et en temps ont
aussi des valeurs par défaut : l'absence de limite était un choix que personne n'avait fait.

**Une extension peut seulement refuser.** Ses gardes tournent autour de la vérification du token,
jamais à sa place, et le contrôle de lecture seule s'applique quoi qu'elles rendent.

**Le serveur ne décide pas des droits.** Voir [Trino d'abord](security.md#trino-dabord).

## Questions

### Et si le catalogue de données a déjà son propre serveur MCP ?

Le [premier mode de déploiement](deployment.md#avec-un-catalogue-de-données) fait tourner le
serveur à côté : rien dans ce serveur ne le bloque ni ne le remplace.

### Un utilisateur peut-il encore écrire dans Trino ?

Le contrôle de lecture seule couvre ce qui passe par ce serveur. Il ne garantit pas ce qu'un
utilisateur peut faire dans Trino par d'autres moyens : si la policy le laisse écrire, il peut
toujours écrire par un autre client Trino. [Sécurité](security.md#lecture-seule-à-travers-ce-serveur)
dit comment la faire imposer aussi par le moteur.

### Pourquoi pas une identité partagée ?

En mode `service`, chaque requête s'exécute sous un seul compte Trino, quelle que soit la personne
qui demande. Tout le monde lit alors ce que lit ce compte, les row filters et les column masks de
Trino ne distinguent plus personne, et l'audit nomme le compte au lieu de la personne. C'est pour
cela que `service` n'est jamais le mode par défaut : le serveur l'annonce au démarrage et à chaque
MCP host, et le mode ne convient qu'à des données que tout appelant a de toute façon le droit de
lire.

### Le serveur parle-t-il à un modèle ?

Non. Le serveur ne parle jamais à un modèle, donc tout modèle convient. Dans
l'[agent d'exemple](hosts.md#depuis-un-agent), le modèle ne voit jamais non plus le token de
l'utilisateur : l'agent l'envoie au serveur à chaque tool call.
