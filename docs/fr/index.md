# Documentation d'akko-mcp-trino

[English](../en/index.md) · **Français**

akko-mcp-trino est un serveur MCP pour Trino. Un agent y lit avec les droits de l'utilisateur pour
qui il travaille, et toute écriture est refusée avant Trino. Commencez par le
[démarrage rapide](quickstart.md), puis ouvrez la page dont vous avez besoin.

## Apprendre

| Page | Contenu |
|---|---|
| [Prérequis](prerequisites.md) | ce qu'il faut, et les versions prises en charge et testées |
| [Démarrage rapide](quickstart.md) | le sandbox, le token d'un utilisateur, un MCP host, une première question |

## Faire

| Page | Contenu |
|---|---|
| [Déploiement](deployment.md) | installer et lancer, les transports, le conteneur, Helm sur Kubernetes et OpenShift, Trino Gateway, avec ou sans catalogue |
| [Identité](identity.md) | les quatre modes d'identité, la revendication de l'utilisateur, la porte, le trajet d'une requête |
| [Brancher un MCP host](hosts.md) | Claude Desktop, Cursor, tout MCP host en stdio, le SDK Python, un agent |
| [Contexte d'un catalogue](catalog-context.md) | ce que sert un fournisseur de contexte, ses bornes, le fournisseur `file` |
| [Modules](modules.md) | `token-policy`, `fingerprint-audit` et `trino-tags`, avec des exemples réels |

## Consulter

| Page | Contenu |
|---|---|
| [Les tools](tools.md) | les neuf tools, leurs arguments, leurs réponses, leurs erreurs |
| [Configuration](configuration.md) | chaque variable, par thème, avec sa valeur par défaut et ses bornes |
| [Exploitation](operations.md) | groupes de ressources, refus, avertissements, audit, santé, métriques, montées de version |

## Comprendre

| Page | Contenu |
|---|---|
| [Sécurité](security.md) | ce que couvre la lecture seule, Trino d'abord, les commandes qu'un agent peut envoyer |
| [Développeurs](developers.md) | l'architecture, les plugins de tools, les extensions, écrire un fournisseur de contexte |
| [Notes de conception](design-notes.md) | pourquoi le serveur est construit ainsi, et quelques questions |

## Fichiers de référence

Ces fichiers restent à la racine du dépôt : [`examples/`](../../examples/),
[`CHANGELOG.md`](../../CHANGELOG.md) et [`MIGRATING.md`](../../MIGRATING.md), en anglais,
[`CONTRIBUTING.fr.md`](../../CONTRIBUTING.fr.md), [`SECURITY.fr.md`](../../SECURITY.fr.md), le
[README](../../charts/akko-mcp-trino/README.md) de la chart Helm (en anglais) et le
[README](../../providers/atlas/README.fr.md) du fournisseur Atlas.

La version interactive des cinq étapes du README est sur
[akko-ai.com/docs/comprendre](https://akko-ai.com/docs/comprendre/), et le fonctionnement de bout
en bout sur [akko-ai.com/docs/comment-ca-marche](https://akko-ai.com/docs/comment-ca-marche/).
