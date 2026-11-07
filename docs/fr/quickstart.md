# Démarrage rapide

[English](../en/quickstart.md) · **Français**

## Lancer le sandbox

Le [sandbox](https://github.com/AKKO-p/akko-sandbox) fait tourner Keycloak, Trino, OPA et
akko-mcp-trino sur votre machine, avec trois utilisateurs de démonstration. Il vous faut Docker
avec Compose v2, environ 2,5 Go de mémoire libre, et les ports `8080`, `3000` et `6274` libres.

```bash
git clone https://github.com/AKKO-p/akko-sandbox.git
cd akko-sandbox
docker compose up -d --wait
./demo.sh
```

`demo.sh` envoie la même question pour deux utilisateurs par le client MCP officiel, montre que
Trino exécute chaque requête sous le nom de l'utilisateur dont il a reçu le token, et montre les
refus : un troisième utilisateur refusé par la policy, une écriture refusée par le serveur avant
Trino, et des requêtes sans token valide refusées en `401`.

| Utilisateur | Voit |
|---|---|
| `alice_admin` | toutes les lignes, toutes les colonnes en clair |
| `carol_analyst` | les clients européens seulement, adresses e-mail masquées |
| `dave_viewer` | tout sauf la table des clients et la colonne `balance` des comptes |

## Obtenir le token d'un utilisateur

```bash
./token.sh carol_analyst
```

`token.sh` demande à Keycloak le token d'accès de `carol_analyst`, émis pour le serveur MCP, et
l'affiche. Il vaut une heure. Collez-le à la place de `<le token d'accès de l'utilisateur>`.

## Brancher un MCP host

Pour **Cursor**, mettez ceci dans `~/.cursor/mcp.json`
([`examples/cursor.json`](../../examples/cursor.json)) :

```json
{
  "mcpServers": {
    "trino": {
      "url": "http://localhost:3000/mcp",
      "headers": {
        "Authorization": "Bearer <le token d'accès de l'utilisateur>"
      }
    }
  }
}
```

Claude Desktop rejoint un serveur HTTP par un pont local :
[Brancher un MCP host](hosts.md#1-essayer-en-cinq-minutes--le-sandbox) donne son entrée, et celles
des autres MCP hosts.

## Poser une question

*« Quelle colonne contient l'adresse e-mail des clients ? »* Le modèle appelle `search_columns`
avec un mot comme `email`, et le serveur répond, pour carol :

```
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", ...}],
 "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", ...}],
 "schemas_searched": 4, "not_searched": [], "complete": true, "next_cursor": null}
```

*« Liste nos clients avec leur e-mail et leur pays. »* Avec le token de carol, cinq clients
européens reviennent, leurs adresses masquées (`***@example.com`). Avec
`./token.sh alice_admin`, les dix, en clair. Avec `./token.sh dave_viewer`, la recherche ne trouve
rien et dit qu'elle est complète : la policy lui cache la table, elle n'est donc pas à lui.

Pour changer d'utilisateur, collez un autre token et redémarrez le serveur dans le MCP host.

## Vérifier sans MCP host

[`examples/check_host_config.py`](../../examples/check_host_config.py) lit le même fichier que le
MCP host, joint le serveur comme le ferait le MCP host, liste les tools et appelle
`search_columns`. Lancez-le depuis un clone de ce dépôt :

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python3 -m venv .venv && . .venv/bin/activate
pip install akko-mcp-trino==0.7.2
python3 examples/check_host_config.py ~/.cursor/mcp.json --search email
```

```
entry: trino (streamable HTTP)
server: trino-mcp 0.7.2
8 tools: search_columns, list_catalogs, list_schemas, list_tables, describe_schema, describe_table, explain_query, execute_query
search_columns('email'):
{"tables": [...], "matches": [...], "complete": true, "next_cursor": null}
```

C'est la sortie de la 0.7.2, qui servait 8 tools ; à partir de la 0.8, la liste ajoute
`profile_table`.

## Arrêter

```bash
docker compose down -v
```

Lisez ensuite [Déploiement](deployment.md) pour lancer le serveur sur votre propre Trino, et
[Identité](identity.md) pour choisir comment il rejoint Trino.
