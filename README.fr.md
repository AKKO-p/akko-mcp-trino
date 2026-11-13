<p align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/AKKO-p/akko-mcp-trino/ci.yml?branch=main&label=ci" alt="CI">
  <img src="https://img.shields.io/badge/coverage-100%25-brightgreen" alt="Coverage">
  <img src="https://img.shields.io/badge/version-0.8.0-blue" alt="Version">
  <img src="https://img.shields.io/badge/python-3.12%20%7C%203.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/MCP-2024--11--05%20to%202026--07--28%20%C2%B7%20streamable--http%20%7C%20sse%20%7C%20stdio-blue" alt="MCP">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License">
  <img src="https://img.shields.io/badge/code%20style-ruff-000000" alt="Ruff">
  <img src="https://img.shields.io/badge/status-beta-yellow" alt="Status">
</p>

# akko-mcp-trino

[English](https://github.com/AKKO-p/akko-mcp-trino/blob/main/README.md) · **Français**

**Donnez à vos agents IA un accès en lecture seule à Trino, et rien de plus.**

akko-mcp-trino est un serveur [MCP](https://modelcontextprotocol.io) pour Trino. Un agent peut s'en
servir pour trouver des tables et des colonnes, décrire un schéma ou une table, lister les
catalogs, les schémas et les tables, et expliquer ou exécuter une requête. Le serveur analyse
chaque instruction et refuse toute écriture avant qu'elle n'atteigne Trino. Par défaut, chaque
requête s'exécute au nom de l'utilisateur pour qui l'agent travaille. Le serveur vérifie le token
de l'utilisateur, qui doit être émis pour ce serveur. Il obtient ensuite de l'identity provider un
token distinct pour Trino (OAuth 2.0 Token Exchange). Le token de l'utilisateur ne quitte
jamais le serveur. Le contrôle d'accès de Trino (OPA, Ranger ou les règles de Trino) décide ensuite
de ce que cet utilisateur peut lire. Les résultats sont limités en lignes et en durée, et chaque
appel laisse une ligne d'audit.

> **Vous venez d'une 0.7.x ?** La 0.8.0 ajoute un neuvième tool, `profile_table`, et le contexte
> d'un catalogue de données, éteint tant que `MCP_CONTEXT_PROVIDERS` ne nomme pas de fournisseur.
> Sans rien activer, trois changements se voient : `search_columns` lit 8 catalogs à la fois
> (`MCP_SEARCH_CONCURRENCY=1` pour la charge de la 0.7), un token dont l'`azp` est présent mais vide
> ne nomme aucun client, et le serveur n'écrit le journal d'`httpx` qu'à partir de `WARNING`. Depuis
> la 0.7.0, la 0.7.1 et la 0.7.2 sont des versions de sécurité : `execute_query`
> refuse `SHOW STATS`, `EXPLAIN`, `SHOW CREATE` et les autres lectures listées dans
> [Sécurité](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/security.md#les-instructions-quun-agent-peut-envoyer), et l'utilisateur est lu dans
> `sub`. Avec Keycloak, posez `MCP_JWT_USER_CLAIM=preferred_username` avant la mise à jour. Voir
> [MIGRATING.md](https://github.com/AKKO-p/akko-mcp-trino/blob/main/MIGRATING.md#from-07-to-08) (en anglais), et [Montée de version](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/operations.md#montée-de-version)
> pour les autres versions.

## Comprendre en cinq étapes

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-1-probleme-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-1-probleme-light.svg" alt="Carol, analyste, demande la liste des clients. Dans un montage d'illustration avec un service account aux droits larges, Trino voit mcp-svc et renvoie 10 clients, e-mails en clair. Avec akko-mcp-trino dans son mode jwt par défaut, Trino voit carol_analyst et renvoie 5 clients européens, e-mails masqués, comme dans le sandbox.">
</picture>

Un agent branché sur un service account aux droits larges arrive dans Trino sous ce compte, et
les règles écrites pour Carol ne s'appliquent plus. Le panneau de gauche est une illustration :
la policy du sandbox refuserait un tel compte. akko-mcp-trino fait arriver l'identité de Carol
jusqu'à Trino, et les règles que Trino a déjà font le reste.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-2-qui-voit-quoi-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-2-qui-voit-quoi-light.svg" alt="La même requête lancée par trois utilisateurs du sandbox, par le même client MCP. alice_admin reçoit les 10 clients en clair. carol_analyst reçoit les 5 clients européens, e-mails masqués. dave_viewer est refusé par Trino : Access Denied: Cannot select from columns [country, full_name, id, email] in table or view demo.crm.customers.">
</picture>

La même requête, lancée par les trois utilisateurs du sandbox, par le même client MCP et le
même serveur. Trino applique à chacun la policy OPA du sandbox ; le serveur ne recopie aucune
règle.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-3-pas-a-pas-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-3-pas-a-pas-light.svg" alt="Un vrai essai de Mistral en 0.7.0, en six étapes. Il demande quel segment de clients détient le plus gros solde total, cherche avec quatre mots et reçoit des résultats mot par mot, fait une jointure sur une colonne qui n'existe pas, lit le conseil du serveur, lit les colonnes de customers avec describe_table, corrige la jointure et obtient business, 17620.75.">
</picture>

Un vrai essai, tool call par tool call : Mistral en 0.7.0 cherche le segment qui détient le
plus gros solde. La recherche trouve les mots un par un, l'erreur sur une mauvaise jointure nomme
la table à lire, et l'agent corrige sa jointure. Chaque réponse de tool montrée est celle du serveur,
rejouée dans le sandbox.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-4-modes-identite-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-4-modes-identite-light.svg" alt="Trois questions choisissent le mode d'identité. Un échange de tokens donne jwt, une règle d'impersonation dans Trino donne impersonation, un serveur sur le poste d'une seule personne avec la connexion OAuth2 de Trino donne oauth2, sinon service. Les quatre modes suivent la même grille (MCP host, serveur, Trino), et service, un seul compte pour tous, est en orange.">
</picture>

Quatre modes d'identité, du plus sûr au plus simple. `jwt` est le mode par défaut. `service`
donne le même compte à tous les appelants, et ne s'active que si vous le nommez.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-5-mesures-dark.svg">
  <img src="https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/comprendre-5-mesures-light.svg" alt="Mistral répond juste dans 62,5 % des cas en 0.6.1 et dans 70 % des cas en 0.7.0, Claude à 93,3 % dans les deux versions. Mistral passe de 5,8 à 3,0 tool calls par question, Claude de 8,5 à 4,9. Mistral varie plus d'un run à l'autre que l'écart entre les versions, et les tokens d'entrée augmentent d'environ 41 %.">
</picture>

Les cinq mêmes questions métier, posées à Mistral et à Claude, en 0.6.1 puis en 0.7.0. Mistral
varie plus d'un run à l'autre que l'écart entre les versions, et la 0.7.0 envoie environ 41 % de
tokens d'entrée en plus par question, parce que ses descriptions de tools sont plus longues.
L'outil de mesure et ses rapports sont dans [akko-sandbox](https://github.com/AKKO-p/akko-sandbox/tree/main/agent/reports).

La version interactive de ces cinq étapes est sur
[akko-ai.com/docs/comprendre](https://akko-ai.com/docs/comprendre/) (aussi
[en anglais](https://akko-ai.com/en/docs/understand/)).

## Essayer en cinq minutes

Le sandbox lance Keycloak, Trino, OPA et le serveur sur votre machine, avec les trois
utilisateurs ci-dessus :

```bash
git clone https://github.com/AKKO-p/akko-sandbox.git
cd akko-sandbox
docker compose up -d --wait
./demo.sh
```

Le [démarrage rapide](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/quickstart.md) continue avec le token d'un utilisateur, un MCP host et une
première question.

## Pourquoi

Un agent qui lit un lakehouse a besoin d'une surface petite et prévisible : quelques tools dont un
modèle comprend les descriptions, des réponses toujours bornées, et la garantie qu'une prompt
injection ne transforme pas, à travers ce serveur, une question en `DROP TABLE`. Ce serveur est
cette surface, et rien d'autre. Il ne décide pas des droits : Trino décide pour chaque utilisateur
avec les règles qu'il a déjà, ou, en mode `service`, pour l'identité unique que vous lui donnez.

## Fonctionnalités

- **Lecture seule par construction, à travers ce serveur.** Le SQL est analysé en arbre et refusé si une écriture apparaît n'importe où dans l'arbre, CTE et sous-requêtes comprises. Les table functions (`TABLE(...)`, comme le `system.query` d'un connecteur JDBC) et le catalog `system` sont refusés aussi, y compris par un nom sans catalog quand le catalog de session est `system`. Ce qu'un utilisateur peut faire dans Trino par d'autres moyens relève des règles de Trino : voir [Sécurité](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/security.md#lecture-seule-à-travers-ce-serveur).
- **Les droits de chaque utilisateur, par défaut.** En mode `jwt`, le mode par défaut, le serveur vérifie le token de l'appelant (émis pour ce serveur, jamais pour Trino), l'échange auprès de l'identity provider contre un token dont l'audience est Trino (RFC 8693), et Trino exécute la requête au nom de cet utilisateur. Le token de l'appelant n'est jamais transmis. Une requête sans token valide est refusée avec un `401`.
- **Un fallback sans token exchange.** En mode `impersonation`, le serveur s'authentifie auprès de Trino avec son propre compte (mot de passe ou certificat client), et Trino exécute la requête au nom de l'appelant vérifié, selon une règle d'impersonation réservée à ce compte.
- **Une identité unique, seulement si on la nomme.** En mode `service`, chaque requête s'exécute sous `TRINO_USER`. Ce mode n'est jamais choisi par défaut, et le serveur le dit au démarrage et à chaque MCP host dans ses instructions d'initialisation.
- **Des limites par défaut.** Un plafond de lignes avec l'indicateur `truncated` (100), un budget de temps par requête, annulée dans Trino (60 s), et un budget de temps par appel de tool (120 s).
- **Une ligne d'audit par appel.** Elle est indexée par `X-Request-Id` et ne contient jamais le token ni le texte SQL : qui a appelé quel tool, avec quel résultat, pendant combien de temps.
- **Neuf tools**, annotés selon ce qu'ils peuvent vraiment faire : `execute_query` n'est annoté en lecture seule que tant que `TRINO_READ_ONLY` est vrai.
- **La vérification du token.** Les bearer tokens sont vérifiés avec le JWKS de n'importe quel identity provider OIDC (issuer, audience liée à ce serveur, expiration, tolérance d'horloge), un token émis pour Trino est refusé, les métadonnées de ressource protégée sont publiées (RFC 9728), et l'en-tête Host est filtré.
- **Le contexte d'un catalogue de données, éteint par défaut.** Avec `MCP_CONTEXT_PROVIDERS`, `describe_table` et `describe_schema` ajoutent ce qu'un catalogue de données sait d'une table (descriptions, termes de glossaire, classifications, propriétaire), seulement pour les tables que Trino liste à l'appelant, et au-delà des descriptions seulement ce que nomment des listes blanches. Des fournisseurs pour Apache Atlas (`akko-mcp-trino-atlas`, installé par l'extra `atlas`, ou `atlas-kerberos` pour Kerberos) et pour un fichier JSON : voir [Contexte d'un catalogue de données](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/catalog-context.md).
- **Extensible sans fork.** Les plugins de tools et les extensions sont des paquets Python nommés au démarrage : modes d'identité, gardes, champs d'audit, routes, tools. Une extension peut seulement refuser ce que le cœur autorise, jamais l'élargir.
- **N'importe quel MCP host, n'importe quel modèle.** Cursor, Claude Desktop, VS Code et le SDK Python ; et tout modèle, Mistral compris, par l'agent d'exemple.
- **Les tests.** 100 % de couverture des lignes et des branches, dont des suites en processus sur le vrai SDK pour les trois transports, une exécution réelle contre Keycloak et Trino en CI ([`integration/`](https://github.com/AKKO-p/akko-mcp-trino/tree/main/integration)), et un contrôle que le paquet n'importe aucun code propre à un fournisseur.

## Documentation

Ces pages décrivent `main`. La documentation d'une version publiée se trouve à son tag.

| Page | Contenu |
|---|---|
| [Prérequis](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/prerequisites.md) | ce qu'il faut, et les versions prises en charge et testées |
| [Démarrage rapide](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/quickstart.md) | le sandbox, le token d'un utilisateur, un MCP host, une première question |
| [Déploiement](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/deployment.md) | installer et lancer, le conteneur, Helm sur Kubernetes et OpenShift, Trino Gateway, avec ou sans catalogue de données |
| [Identité](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/identity.md) | les quatre modes d'identité, le claim de l'utilisateur, la vérification du token |
| [Brancher un MCP host](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/hosts.md) | Claude Desktop, Cursor, tout MCP host en stdio, le SDK Python, un agent |
| [Contexte d'un catalogue de données](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/catalog-context.md) | ce que sert un fournisseur de contexte, ses limites, le fournisseur `file` |
| [Modules](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/modules.md) | `token-policy`, `fingerprint-audit` et `trino-tags`, avec des exemples réels |
| [Développeurs](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/developers.md) | l'architecture, les plugins de tools, les extensions, écrire un fournisseur de contexte |
| [Les tools](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/tools.md) | les neuf tools, leurs arguments, leurs réponses, leurs erreurs |
| [Configuration](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/configuration.md) | chaque variable, par thème, avec sa valeur par défaut et ses limites |
| [Sécurité](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/security.md) | ce que couvre la lecture seule, Trino d'abord, les instructions qu'un agent peut envoyer |
| [Exploitation](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/operations.md) | groupes de ressources, refus, avertissements, audit, santé, métriques, montées de version |
| [Notes de conception](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/design-notes.md) | pourquoi le serveur est construit ainsi, et quelques questions |

La documentation existe aussi [en anglais](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/index.md). Les fichiers de référence restent
à la racine, en anglais : [`CHANGELOG.md`](https://github.com/AKKO-p/akko-mcp-trino/blob/main/CHANGELOG.md), [`MIGRATING.md`](https://github.com/AKKO-p/akko-mcp-trino/blob/main/MIGRATING.md),
[`SECURITY.md`](https://github.com/AKKO-p/akko-mcp-trino/blob/main/SECURITY.md) (aussi [en français](https://github.com/AKKO-p/akko-mcp-trino/blob/main/SECURITY.fr.md)) et les
[exemples](https://github.com/AKKO-p/akko-mcp-trino/tree/main/examples). Le guide du site est sur
[akko-ai.com/docs/coeur-open-source](https://akko-ai.com/docs/coeur-open-source/).

## Compatibilité

| | Pris en charge |
|---|---|
| Python | 3.12, 3.13 |
| Trino | voir [Prérequis](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/prerequisites.md#compatibilité) |
| Moteurs de policies | OPA (`trino-opa`), Ranger (plugin Trino), contrôle d'accès par fichier de Trino |
| Identity providers | tout identity provider OIDC qui publie un JWKS ; OAuth 2.0 Token Exchange, RFC 8693, pour le mode `jwt` par défaut |
| MCP | transports `streamable-http`, `sse` et `stdio`, par le SDK officiel `mcp` |
| Modèles | tous : le serveur ne parle jamais à un modèle |

[Prérequis](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/fr/prerequisites.md#compatibilité) dit ce que chaque mode demande à Trino et ce qui
est testé. Les images que lance la suite d'intégration sont épinglées dans
[`integration/compose.yaml`](https://github.com/AKKO-p/akko-mcp-trino/blob/main/integration/compose.yaml).

## Contribuer

Les contributions sont bienvenues. Voir [CONTRIBUTING.fr.md](https://github.com/AKKO-p/akko-mcp-trino/blob/main/CONTRIBUTING.fr.md) pour les règles
de base (les tests d'abord, 100 % de couverture, un code neutre vis-à-vis des produits, refuser en
cas de doute), et [SECURITY.fr.md](https://github.com/AKKO-p/akko-mcp-trino/blob/main/SECURITY.fr.md) pour signaler une vulnérabilité en privé. Les
changements sont suivis dans [CHANGELOG.md](https://github.com/AKKO-p/akko-mcp-trino/blob/main/CHANGELOG.md).

## À propos d'AKKO

akko-mcp-trino est construit et maintenu par [AKKO](https://akko-ai.com), un projet français qui
travaille sur l'accès gouverné des agents IA aux données des organisations, là où elles sont, avec
les moteurs et les identity providers qu'elles ont déjà.

AKKO le maintient ([contact@akko-ai.com](mailto:contact@akko-ai.com)), et les issues sont sur
[github.com/AKKO-p/akko-mcp-trino/issues](https://github.com/AKKO-p/akko-mcp-trino/issues).

## Licence

Apache 2.0. Voir [LICENSE](https://github.com/AKKO-p/akko-mcp-trino/blob/main/LICENSE) et [NOTICE](https://github.com/AKKO-p/akko-mcp-trino/blob/main/NOTICE).
