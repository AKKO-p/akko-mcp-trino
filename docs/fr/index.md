# Documentation d'akko-mcp-trino

[English](../en/index.md) · **Français**

akko-mcp-trino est un serveur MCP pour Trino. Par ce serveur, un agent lit les données avec les
droits de l'utilisateur pour qui il travaille, et le serveur refuse toute écriture avant qu'elle
n'atteigne Trino. Commencez par le [démarrage rapide](quickstart.md), puis ouvrez la page dont
vous avez besoin.

Ces pages décrivent `main`. La documentation d'une version publiée se trouve à son tag.

## Apprendre

| Page | Contenu |
|---|---|
| [Prérequis](prerequisites.md) | ce qu'il faut, et les versions prises en charge et testées |
| [Démarrage rapide](quickstart.md) | le sandbox, le token d'un utilisateur, un MCP host, une première question |

## Faire

| Page | Contenu |
|---|---|
| [Déploiement](deployment.md) | installer et lancer, les transports, le conteneur, Helm sur Kubernetes et OpenShift, Trino Gateway, avec ou sans catalogue de données, avec ou sans modules |
| [Identité](identity.md) | les quatre modes d'identité, le claim de l'utilisateur, la vérification du token, le trajet d'une requête |
| [Brancher un MCP host](hosts.md) | Claude Desktop, Cursor, Claude Code, VS Code, tout MCP host en stdio, le SDK Python, deux agents (dont un sur l'API Agents de Mistral) |
| [Contexte d'un catalogue de données](catalog-context.md) | ce que sert un fournisseur de contexte, ses limites, le fournisseur `file` |
| [Modules](modules.md) | `token-policy`, `fingerprint-audit` et `trino-tags`, avec des exemples réels |
| [Développeurs](developers.md) | l'architecture, les plugins de tools, les extensions, écrire un fournisseur de contexte |

## Consulter

| Page | Contenu |
|---|---|
| [Les tools](tools.md) | les neuf tools, leurs arguments, leurs réponses, leurs erreurs |
| [Configuration](configuration.md) | chaque variable, par thème, avec sa valeur par défaut et ses limites |
| [Sécurité](security.md) | ce que couvre la lecture seule, Trino d'abord, les instructions qu'un agent peut envoyer |
| [Exploitation](operations.md) | groupes de ressources, refus, avertissements, audit, santé, métriques, montées de version |

## Comprendre

| Page | Contenu |
|---|---|
| [Notes de conception](design-notes.md) | pourquoi le serveur est construit ainsi, et quelques questions |

## Contribuer

[`CONTRIBUTING.fr.md`](../../CONTRIBUTING.fr.md) donne les règles de base, les contrôles et les
verrous. [Développeurs](developers.md#contribuer-à-la-documentation) donne les termes qu'emploient
ces pages.

## Fichiers de référence

Ces fichiers restent à la racine du dépôt : [`examples/`](../../examples/),
[`CHANGELOG.md`](../../CHANGELOG.md) et [`MIGRATING.md`](../../MIGRATING.md), en anglais,
[`SECURITY.fr.md`](../../SECURITY.fr.md), le [README](../../charts/akko-mcp-trino/README.md) de
la chart Helm (en anglais) et le [README](../../providers/atlas/README.fr.md) du fournisseur Atlas.

La version interactive des cinq étapes du README est sur
[akko-ai.com/docs/comprendre](https://akko-ai.com/docs/comprendre/). Le fonctionnement de bout en
bout est décrit sur [akko-ai.com/docs/comment-ca-marche](https://akko-ai.com/docs/comment-ca-marche/),
et le guide du site sur [akko-ai.com/docs/coeur-open-source](https://akko-ai.com/docs/coeur-open-source/).
