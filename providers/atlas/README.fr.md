# akko-mcp-trino-atlas

Fournisseur de contexte Apache Atlas pour [akko-mcp-trino](https://github.com/AKKO-p/akko-mcp-trino).
Il lit ce qu'Atlas sait des tables que Trino liste pour l'appelant (descriptions,
classifications, termes de glossaire, métadonnées métier) et le donne à `describe_table` et
`describe_schema`, à travers le filtre du serveur par les droits de Trino. Il lit Atlas avec
un compte à lui, par l'API REST v2 seulement, et n'y écrit jamais.

*Version anglaise : [README.md](README.md).*

> **État.** Le paquet, son point d'entrée et le contrôle de ses réglages sont en place ; la
> lecture des tables et des termes de glossaire arrive dans les prochains commits de la 0.8.

## Installation

```bash
pip install "akko-mcp-trino[atlas]"     # le cœur avec ce fournisseur et Kerberos
export MCP_CONTEXT_PROVIDERS=atlas
```

Le fournisseur s'enregistre sous le groupe de points d'entrée `akko_mcp_trino.context`, sous le
nom `atlas`. Rien n'est chargé tant que `MCP_CONTEXT_PROVIDERS` ne le nomme pas ; l'image de
conteneur d'akko-mcp-trino l'embarque, inactif.

## Réglages

Le serveur refuse de démarrer quand le fournisseur n'a pas d'adresse ou pas de moyen de
s'authentifier. Aucun message ne cite une adresse ni un secret.

| Variable | Rôle | Défaut |
|---|---|---|
| `ATLAS_URL` | adresse d'Atlas, `http` ou `https` ; plusieurs, séparées par des virgules, pour un Atlas en haute disponibilité. Jamais avec des identifiants dedans. | obligatoire |
| `ATLAS_AUTH` | `basic` (un utilisateur et un mot de passe, ou un jeton Knox) ou `kerberos` | `basic` |
| `ATLAS_USER`, `ATLAS_PASSWORD` ou `ATLAS_PASSWORD_FILE` | authentification Basic, avec un compte en lecture seule | vide |
| `ATLAS_KNOX_TOKEN` ou `ATLAS_KNOX_TOKEN_FILE` | un jeton Knox, pour un Atlas derrière une topologie Knox avec le rôle `ATLAS` ; pas avec `ATLAS_USER` | vide |
| `ATLAS_PRINCIPAL`, `ATLAS_KEYTAB` | Kerberos (SPNEGO), avec `ATLAS_AUTH=kerberos` | vide |

Un secret donné par fichier (`*_FILE`) est relu quand le fichier change.

## Versions d'Atlas

Les tests rejouent des réponses enregistrées sur Atlas 2.1 et Atlas 2.4, dans
`tests/recordings/atlas-2.1` et `tests/recordings/atlas-2.4`. Chaque nom et chaque texte y a été
remplacé par un nom neutre, structure gardée, par `tests/recordings/anonymize.py` ; sa docstring
dit comment enregistrer et anonymiser une nouvelle version.

## Licence

Licence Apache 2.0, comme akko-mcp-trino.
