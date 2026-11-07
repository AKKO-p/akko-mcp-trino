# Démarrage rapide

[English](../en/quickstart.md) · **Français**

## Lancer le sandbox

Le [sandbox](https://github.com/AKKO-p/akko-sandbox) fait tourner Keycloak, Trino, OPA et
akko-mcp-trino sur votre machine, avec trois utilisateurs de démonstration. Il vous faut Docker
avec Compose v2, environ 2,5 Go de mémoire libre, et les ports 8080, 3000 et 6274 disponibles.

```bash
git clone https://github.com/AKKO-p/akko-sandbox.git
cd akko-sandbox
docker compose up -d --wait
./demo.sh
```

`demo.sh` pose la même question au nom de deux utilisateurs différents, par le client MCP
officiel. Il montre que Trino exécute chaque requête au nom de l'utilisateur dont il a reçu le
token. Il montre ensuite les refus : un troisième utilisateur refusé par la policy, une écriture
refusée par le serveur avant d'atteindre Trino, et des requêtes sans token valide refusées avec un
`401`.

| Utilisateur | Login | Voit |
|---|---|---|
| Alice | `alice_admin` | toutes les lignes et toutes les colonnes, sans masque |
| Carol | `carol_analyst` | les clients européens seulement, avec des adresses e-mail masquées |
| Dave | `dave_viewer` | tout sauf la table des clients et la colonne `balance` des comptes |

## Obtenir le token d'un utilisateur

```bash
./token.sh carol_analyst
```

`token.sh` demande à Keycloak le token d'accès de Carol, émis pour le serveur MCP, et l'affiche. Le
token est valable une heure. Collez-le à la place de `<le token d'accès de l'utilisateur>`.

## Brancher un MCP host

Pour **Cursor**, mettez ceci dans `~/.cursor/mcp.json`
([`examples/cursor.json`](../../examples/cursor.json)) :

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

Claude Desktop joint un serveur HTTP par un pont local.
[Brancher un MCP host](hosts.md#1-essayer-en-cinq-minutes-le-sandbox) donne son entrée et celles
des autres MCP hosts.

## Poser une question

Demandez *« Quelle colonne contient l'adresse e-mail des clients ? »* Le modèle appelle
`search_columns` avec un mot comme `email`, et le serveur rend, pour Carol :

```
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", ...}],
 "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", ...}],
 "schemas_searched": 4, "not_searched": [], "complete": true, "next_cursor": null}
```

Demandez ensuite *« Liste nos clients avec leur e-mail et leur pays. »* Avec le token de Carol,
cinq clients européens reviennent, avec des adresses masquées (`***@example.com`). Avec
`./token.sh alice_admin`, les dix reviennent, sans masque. Avec `./token.sh dave_viewer`, la
recherche ne trouve rien et se dit complète, parce que la policy cache la table à Dave.

Pour changer d'utilisateur, collez un autre token et relancez le serveur dans le MCP host. Pour
vérifier une configuration sans MCP host, voir
[Brancher un MCP host](hosts.md#vérifier-une-configuration-sans-le-mcp-host).

## Arrêter

```bash
docker compose down -v
```

Lisez ensuite [Déploiement](deployment.md) pour lancer le serveur sur votre propre Trino, et
[Identité](identity.md) pour choisir comment il joint Trino.
