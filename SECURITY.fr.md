# Politique de sécurité

[English](SECURITY.md) · **Français**

Ce serveur se place entre des agents IA et des données. Prenez toute faiblesse au sérieux ; nous
le faisons.

## Versions prises en charge

| Version | Prise en charge |
|---|---|
| 0.7.x | oui |
| 0.6.x | oui |
| 0.5.x et avant | non : le mode `jwt` transmet le token de l'appelant à Trino, et les valeurs par défaut servent des requêtes sans token. Mettez à jour avec [MIGRATING.md](MIGRATING.md) (en anglais). |

## Signaler une vulnérabilité

Écrivez à **security@akko-ai.com** avec une description, la version et un moyen de reproduire.
N'ouvrez pas d'issue publique. Vous recevrez un accusé de réception sous trois jours ouvrés, et un
correctif ou un plan de mitigation sous trente.

Merci de ne mettre dans un signalement ni vrais tokens, ni vraies données, ni vrais noms de machines.

## Modèle de sécurité

**Qui Trino voit.** Par défaut (`jwt`), chaque requête s'exécute sous le nom de l'appelant
vérifié. Le serveur n'accepte qu'un token émis pour lui (son audience est `MCP_OIDC_AUDIENCE` ou
`MCP_RESOURCE_URL`, RFC 8707), refuse un token émis pour Trino, et obtient un token pour Trino par
OAuth 2.0 Token Exchange (RFC 8693) avec son propre client. Le token de l'appelant n'est jamais
transmis, comme l'exige la spécification d'autorisation de MCP. Sans échange de token,
`impersonation` exécute chaque requête au nom de l'appelant par le compte propre du serveur et une
règle d'impersonation réservée à ce compte. `service` exécute chaque requête sous une identité
unique ; ce n'est jamais un mode par défaut, et il est annoncé par un avertissement au démarrage,
dans `--check` et à chaque MCP host. Quel que soit le mode, le nom de l'appelant vient de son
token vérifié, jamais d'un en-tête qu'il a écrit.

**La porte.** Une requête sans token valide (absent, falsifié, expiré, ou émis pour une autre
audience) est refusée en `401` par défaut. Les modes qui s'exécutent au nom de l'appelant refusent
de démarrer derrière une porte qui laisserait passer une telle requête.

**Ce que couvre la lecture seule.** La garde de lecture seule refuse toute écriture qui passe par
ce serveur : chaque instruction est analysée, et une écriture placée n'importe où dans son arbre
est refusée avant Trino, comme les table functions et le catalog `system`. Elle ne garantit pas les
droits de l'utilisateur dans Trino. En modes `jwt` et `impersonation`, Trino exécute la requête au
nom de l'utilisateur, avec les droits que sa policy lui donne : un utilisateur autorisé à écrire
peut toujours écrire par un autre client Trino, avec un token pour Trino que l'IdP lui délivre
directement. Le token que le serveur obtient par échange reste dans sa mémoire et n'est jamais
rendu à l'agent ni au MCP host. En mode `service`, il en va de même pour `TRINO_USER`, pour qui
détient son secret.

Pour une lecture seule de bout en bout, le moteur doit tenir la ligne lui aussi :

- donnez aux identités par lesquelles les agents atteignent Trino des règles en lecture seule dans
  le moteur de policies (OPA, Ranger, ou des règles par fichier comme
  `{"catalogs": [{"user": "mcp-reader", "catalog": ".*", "allow": "read-only"}]}`) ;
- en mode `jwt`, ne laissez que le client de ce serveur obtenir par échange des tokens pour
  l'audience de Trino ;
- en mode `impersonation`, limitez la règle d'impersonation aux utilisateurs pour qui les agents
  travaillent, et gardez le secret du compte comme celui de tout compte de service ;
- refusez aussi les table functions et le catalog `system` dans la policy : la garde est une
  seconde ligne, pas la seule.

**Les bornes.** Un plafond de lignes (100), un budget de temps par requête (60 s, annulée dans
Trino) et par appel de tool (120 s) s'appliquent par défaut ; `0` retire un budget de temps, et
c'est un choix à faire explicitement.

## Ce qui compte

Tout ce qui permet à un appelant de lire des données que son identité Trino ne pourrait pas lire
(la sienne en modes `jwt` et `impersonation`, celle configurée en mode `service`), de faire passer
une écriture par un tool tant que `TRINO_READ_ONLY` est vrai, d'atteindre Trino sous une autre
identité que celle que le mode permet, de faire accepter un token qui n'a pas été émis pour ce
serveur, de faire envoyer à Trino par le serveur le token de l'appelant ou un token qu'il n'a pas
obtenu pour Trino, d'agir sans identité vérifiée tant que `MCP_AUTH_REQUIRED` est vrai, ou
d'extraire un token, un secret client ou un mot de passe d'un journal, d'un message d'erreur ou de
`--check`.
