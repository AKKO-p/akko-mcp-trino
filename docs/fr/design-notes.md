# Notes de conception

[English](../en/design-notes.md) · **Français**

## Pourquoi

Un agent qui lit un entrepôt de données a besoin d'une surface petite et prévisible : quelques
tools dont un modèle comprend les descriptions, des réponses qui ne s'emballent jamais, et la
garantie qu'une prompt injection ne transforme pas, à travers ce serveur, une question en
`DROP TABLE`. Ce serveur est cette surface, et rien d'autre. Il ne décide pas des droits : Trino
décide pour chaque utilisateur avec les règles qu'il a déjà, ou, en mode `service`, pour l'identité
unique que vous lui donnez.

## Notes

**La garde est un middleware ASGI pur, pas `BaseHTTPMiddleware`.** Ce dernier exécute la suite
dans une tâche à part et casse la propagation des ContextVar : l'appelant vérifié n'atteindrait
jamais les tools.

**Le montage ne dépend pas du transport.** Un seul chemin de code construit l'application du
transport et y monte la garde, quel que soit le transport. La garde n'a longtemps vécu que sur la
branche que le transport par défaut ne prenait jamais ; un test la vérifie désormais pour les deux.

**Chaque tool passe par le mode d'identité, pas seulement `execute_query`.** Lister les catalogs,
les schémas et les tables, c'est lire des métadonnées ; un test parcourt chaque tool pour vérifier
qu'il atteint Trino de la même façon.

**La lecture seule se décide sur l'arbre, n'importe où dans l'arbre.** Un contrôle du premier
mot-clé laisse passer `WITH w AS (DELETE FROM t) SELECT 1`. Un contrôle du nœud racine aussi,
jusqu'à ce qu'une preuve adverse en conditions réelles le prenne en défaut. La garde parcourt
désormais tout l'arbre.

**Les table functions sont refusées.** `TABLE(pg.system.query(query => '...'))` s'analyse comme un
simple `SELECT`, et pourtant elle envoie du SQL brut à la source sous le compte propre du
connecteur : le moteur n'applique alors ni row filters ni column masks. La garde refuse toute
table function et le catalog `system` (qui montre le SQL des autres utilisateurs dans
`system.runtime.queries`). La policy du moteur doit les refuser aussi, `system.jdbc` et
`system.metadata` à part ; la garde est une seconde ligne, pas la seule.

**Pas de token passthrough.** Jusqu'à la 0.5, le mode `jwt` envoyait à Trino le token de
l'appelant, une fois vérifié. La spécification d'autorisation de MCP l'interdit : un token émis
pour un service ne doit pas être transmis à un autre, sinon cet autre service peut faire confiance
à un token qui ne lui était pas destiné (le « confused deputy »). Depuis la 0.6, le serveur
n'accepte que les tokens dont l'audience est la sienne, refuse un token émis pour Trino, et obtient
pour Trino un token à lui par OAuth 2.0 Token Exchange (RFC 8693). Là où l'IdP ne sait pas
échanger de tokens, `impersonation` est le repli explicite ; il n'est jamais choisi en silence.

**L'identité vérifiée, jamais celle que nomme l'appelant.** Le nom de l'appelant vient toujours de
son token vérifié. Un appelant qui envoie un en-tête nommant un utilisateur Trino ne change rien :
le serveur construit sa propre requête vers Trino. Un mot de passe, un token ou un certificat
client ne circule jamais en http simple, et un mode d'identité que le cœur ne connaît pas empêche
le démarrage plutôt que de se replier sur un autre.

**Sûr par défaut.** Jusqu'à la 0.5, les valeurs par défaut exécutaient chaque requête sous
`TRINO_USER` et servaient une requête sans token. Depuis la 0.6, la valeur par défaut exécute chaque
requête sous le nom de l'appelant et refuse une requête sans token valide ; le mode `service` et une
porte ouverte restent possibles, choisis par leur nom, et annoncés par des avertissements. Les
bornes en lignes et en temps ont aussi des valeurs par défaut : l'absence de borne était un choix
que personne n'avait fait.

**Une extension peut seulement refuser.** Ses gardes tournent autour de la vérification du token,
jamais à sa place, et la garde de lecture seule s'applique quoi qu'elles répondent.

**Le serveur ne décide pas des droits.** Trino en décide, pour chaque appelant ou pour l'identité
configurée. Mettre de la policy dans le serveur dupliquerait, et finirait par contredire, ce que le
moteur impose déjà à tous les autres clients.

## Questions

### Le catalogue a déjà son propre serveur MCP

Une équipe qui fait déjà tourner le serveur MCP de son catalogue le garde, à côté : rien dans ce
serveur ne le bloque ni ne le remplace. C'est le premier mode de
[Déploiement](deployment.md#déploiement-avec-un-catalogue), et il convient quand les métadonnées du
catalogue ne sont pas sensibles. Quand les métadonnées doivent suivre les droits sur les données,
un fournisseur de contexte ne les sert que pour les tables et les colonnes que Trino liste à
l'appelant.

### Un utilisateur peut-il encore écrire dans Trino ?

La garde de lecture seule couvre ce qui passe par ce serveur. Elle ne garantit pas ce qu'un
utilisateur peut faire dans Trino par d'autres moyens : si la policy le laisse écrire, il peut
toujours écrire par un autre client Trino. [Sécurité](security.md#lecture-seule-à-travers-ce-serveur)
dit comment faire tenir la ligne au moteur lui aussi.

### Pourquoi pas une identité partagée ?

En mode `service`, chaque requête s'exécute sous un seul compte Trino, quelle que soit la personne
qui demande. Tout le monde lit alors ce que lit ce compte, les row filters et les column masks de
Trino ne distinguent plus personne, et l'audit nomme le compte au lieu de la personne. C'est pour
cela que `service` n'est jamais le mode par défaut : le serveur l'annonce au démarrage et à chaque
MCP host, et il ne convient qu'à des données que tout appelant a de toute façon le droit de lire.

### Le serveur parle-t-il à un modèle ?

Non. Le serveur ne parle jamais à un modèle, donc tout modèle convient. Le modèle d'un agent ne voit
jamais non plus le token de l'utilisateur : le serveur le reçoit à chaque tool call et le vérifie.
