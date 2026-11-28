# Politique de sécurité

[English](SECURITY.md) · **Français**

Ce serveur se place entre des agents IA et des données. Signalez toute faiblesse que vous trouvez.

## Versions prises en charge

| Version | Prise en charge |
|---|---|
| 0.8.x | oui : tous les correctifs, dans la dernière release 0.8 |
| 0.7.x | correctifs de sécurité jusqu'au 28 février 2027, puis non |
| 0.6.x et avant | non. Mettez à jour avec [MIGRATING.md](MIGRATING.md) (en anglais) ; jusqu'à la 0.5.x, le mode `jwt` transmettait le token de l'appelant à Trino, et les valeurs par défaut servaient des requêtes sans token. |

La durée de prise en charge d'une version mineure, et ce qu'une release peut changer, sont dans la
[politique de versions](docs/fr/versioning.md).

## Signaler une vulnérabilité

Signalez en privé, par l'un ou l'autre canal :

- un signalement privé sur GitHub : **Security**, puis **Report a vulnerability**
  ([security advisories](https://github.com/AKKO-p/akko-mcp-trino/security/advisories/new)) ;
- un courriel à **security@akko-ai.com**.

Donnez une description, la version et un moyen de reproduire. N'ouvrez pas d'issue publique, et
n'y mettez ni vrais tokens, ni vraies données, ni vrais noms de machines.

| Étape | Délai |
|---|---|
| Accusé de réception | 48 heures |
| Première évaluation (confirmé ou non, gravité) | 7 jours |
| Correctif publié, gravité critique ou haute | 30 jours |
| Correctif publié, autres gravités | 90 jours |

La divulgation est coordonnée : le correctif est publié d'abord, puis un avis de sécurité GitHub
décrit la faiblesse, les versions touchées et la version corrigée, avec un CVE s'il y a lieu, et
remercie l'auteur du signalement sauf s'il demande le contraire. Un signalement non corrigé sous
90 jours peut être divulgué par son auteur, après nous en avoir prévenus.

## Télémétrie

Le serveur n'envoie aucune télémétrie, ni à ses auteurs ni à personne. Il ne se connecte qu'à
Trino, au fournisseur d'identité configuré (ses clés, et le point de jeton pour le token exchange)
et aux fournisseurs de contexte configurés ; `/health`, `/ready` et `/metrics` sont servis
localement, sur le port de santé, à qui le joint. Une nuance : le SDK MCP sur lequel il repose
porte une instrumentation OpenTelemetry qui ne fait rien tant que l'environnement n'installe pas de
SDK OpenTelemetry (un opérateur qui en injecte un dans chaque pod, par exemple). Les spans du SDK
pour chaque message MCP partent alors vers l'exportateur que fixe l'environnement (`OTEL_*`) ; ils
ne portent aucun SQL.

## Vérifier une release

Chaque image et la chart Helm sont signées par cosign sans clé, par le workflow de release de ce
dépôt au tag de la release ; chaque image porte un SBOM signé et une provenance de construction
signée, et les paquets Python un bundle Sigstore chacun. Les commandes sont dans
[docs/fr/security.md](docs/fr/security.md#vérifier-une-release).

## Modèle de sécurité

**Qui Trino voit.** Par défaut (`jwt`), chaque requête s'exécute sous le nom de l'appelant
vérifié. Le serveur n'accepte qu'un token émis pour lui (son audience est `MCP_OIDC_AUDIENCE` ou
`MCP_RESOURCE_URL`, RFC 8707), refuse un token émis pour Trino, et obtient un token pour Trino par
OAuth 2.0 Token Exchange (RFC 8693) avec son propre client. Le token de l'appelant n'est jamais
transmis à Trino, comme l'exige la spécification d'autorisation de MCP. Sans token exchange,
`impersonation` exécute chaque requête au nom de l'appelant par le compte propre du serveur et une
règle d'impersonation réservée à ce compte. `service` exécute chaque requête sous une identité
unique ; ce n'est jamais un mode par défaut, et il est annoncé par un avertissement au démarrage,
dans `--check` et à chaque MCP host. Quel que soit le mode, le nom de l'appelant vient de son
token vérifié, jamais d'un en-tête qu'il a posé.

**La vérification du token.** Une requête sans token valide (absent, falsifié, expiré, ou émis
pour une autre audience) est refusée avec un `401` par défaut. Le serveur refuse de démarrer dans un
mode qui s'exécute au nom de l'appelant quand la vérification du token laisserait passer une telle
requête.

**Ce que couvre la lecture seule.** Le contrôle de lecture seule refuse toute écriture qui passe par
ce serveur : chaque instruction est analysée, et une écriture placée n'importe où dans son arbre
est refusée avant Trino, comme les table functions et le catalog `system`. Elle ne garantit pas les
droits de l'utilisateur dans Trino. En modes `jwt` et `impersonation`, Trino exécute la requête au
nom de l'utilisateur, avec les droits que sa policy lui donne : un utilisateur autorisé à écrire
peut toujours écrire par un autre client Trino, avec un token pour Trino que l'IdP lui délivre
directement. Le token que le serveur obtient par échange reste dans sa mémoire et n'est jamais
rendu à l'agent ni au MCP host. En mode `service`, il en va de même pour `TRINO_USER`, pour qui
détient son secret.

Pour une lecture seule de bout en bout, le moteur doit l'imposer lui aussi :

- donnez aux identités par lesquelles les agents atteignent Trino des règles en lecture seule dans
  le moteur de policies (OPA, Ranger, ou des règles par fichier comme
  `{"catalogs": [{"user": "mcp-reader", "catalog": ".*", "allow": "read-only"}]}`) ;
- en mode `jwt`, ne laissez que le client de ce serveur obtenir par échange des tokens pour
  l'audience de Trino ;
- en mode `impersonation`, limitez la règle d'impersonation aux utilisateurs pour qui les agents
  travaillent, et gardez le secret du compte comme celui de tout service account ;
- refusez aussi les table functions et le catalog `system` dans la policy : le contrôle du
  serveur est une deuxième ligne de défense, pas la seule.

**Les bornes.** Un plafond de lignes (100), un budget de temps par requête (60 s, annulée dans
Trino) et par appel de tool (120 s) s'appliquent par défaut ; `0` retire un budget de temps, et
c'est un choix à faire explicitement.

## Ce qui compte

Un signalement compte s'il permet à un appelant de :

- lire des données que son identité Trino ne peut pas lire (la sienne en modes `jwt` et
  `impersonation`, celle configurée en mode `service`) ;
- faire passer une écriture par un tool tant que `TRINO_READ_ONLY` est vrai ;
- atteindre Trino sous une identité que le mode ne permet pas ;
- faire accepter un token qui n'a pas été émis pour ce serveur ;
- faire envoyer à Trino par le serveur le token de l'appelant, ou tout token qu'il n'a pas obtenu
  pour Trino ;
- agir sans identité vérifiée tant que `MCP_AUTH_REQUIRED` est vrai ;
- extraire un token, un secret client ou un mot de passe d'un journal, d'un message d'erreur ou de
  `--check`.
