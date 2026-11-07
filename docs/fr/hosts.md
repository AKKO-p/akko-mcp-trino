# Brancher un MCP host

[English](../en/hosts.md) · **Français**

Le MCP host envoie le token d'accès de l'utilisateur, émis pour ce serveur. Le serveur le vérifie
et nomme l'appelant dans la ligne d'audit. Cette page branche deux MCP hosts, Claude Desktop et
Cursor, puis tout MCP host en stdio, le SDK Python et un agent. Il y a trois chemins :

| Chemin | Transport | La requête s'exécute au nom de | Pour |
|---|---|---|---|
| [1. Essayer en cinq minutes](#1-essayer-en-cinq-minutes-le-sandbox) | streamable HTTP, `http://localhost:3000/mcp` | l'utilisateur du sandbox dont vous collez le token | un premier essai sur votre portable |
| [2. Votre propre Trino depuis votre poste](#2-votre-propre-trino-depuis-votre-poste-stdio-et-oauth2) | stdio, le MCP host lance le serveur | vous, connecté par la page de connexion de Trino (`oauth2`) | une personne, un Trino avec OAuth2 |
| [3. Un serveur partagé pour une équipe](#3-un-serveur-partagé-pour-une-équipe-streamable-http-et-un-token-par-utilisateur) | streamable HTTP en https | chaque utilisateur, d'après son propre bearer token (`jwt`, le mode par défaut) | une équipe, un seul serveur |

> **Ce que nous avons vérifié, et ce que nous n'avons pas vérifié.** Nous avons exécuté chaque
> bloc JSON des trois chemins, avec les seuls emplacements à remplacer changés, par le SDK Python
> MCP officiel. Il a lancé chaque entrée stdio, ou envoyé l'`url` et les `headers` de chaque entrée
> HTTP, contre le sandbox et akko-mcp-trino 0.7.0 depuis PyPI et Docker Hub. Il a listé les 8 tools
> de la 0.7 et appelé `search_columns`. Les versions épinglées ont été montées en 0.7.2 ensuite, et
> les neuf tools listés plus bas sont ceux de `main` : ni l'un ni l'autre ne faisait partie de cet
> essai. Les clés de chaque bloc sont celles que documentent les MCP hosts, et les tests du dépôt
> le vérifient. Nous n'avons pas piloté les interfaces de Claude Desktop et de Cursor : les noms
> des menus viennent de leur documentation. L'[annexe](#annexe-ce-qui-a-été-vérifié) donne le
> détail.

## Où se trouve la configuration

| MCP host | Fichier | À savoir |
|---|---|---|
| Claude Desktop, macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` | s'ouvre depuis **Settings > Developer > Edit Config** ; quittez complètement Claude Desktop et relancez-le après chaque changement |
| Claude Desktop, Windows | `%APPDATA%\Claude\claude_desktop_config.json` | idem |
| Claude Desktop, Linux | `~/.config/Claude/claude_desktop_config.json` | idem |
| Cursor, tous les projets | `~/.cursor/mcp.json`, dans votre dossier personnel (`%USERPROFILE%\.cursor\mcp.json` sous Windows) | |
| Cursor, un seul projet | `.cursor/mcp.json` à la racine du projet | |

Les deux fichiers contiennent un objet `mcpServers`, avec une entrée par serveur. Les exemples
l'appellent `trino`. Si votre fichier déclare déjà d'autres serveurs, ajoutez l'entrée à côté.

**Comment chaque MCP host joint le serveur.** Cursor se connecte lui-même à un serveur streamable
HTTP, avec une entrée `url` et `headers`. Le fichier de Claude Desktop, lui, ne lance que des
serveurs locaux, avec une entrée `command`, `args` et `env`. Ses connecteurs distants, ajoutés
dans **Customize > Connectors**, se connectent depuis le cloud d'Anthropic et non depuis votre
ordinateur ([Claude Help Center](https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp)).
Ils ne peuvent donc pas joindre un serveur sur `localhost` ou sur un réseau privé, sauf si votre
organisation utilise les [MCP tunnels](https://claude.com/docs/connectors/mcp-tunnels/overview).
Leurs en-têtes portent un seul identifiant, partagé par tous. Pour connecter chaque personne,
il faut OAuth et un serveur joignable depuis internet
([documentation de Claude](https://claude.com/docs/connectors/custom)). Pour un serveur HTTP, cette
page fait donc lancer à Claude Desktop un petit pont local,
[`mcp-remote`](https://github.com/punkpeye/mcp-remote), qui transmet vers l'URL du serveur avec
votre en-tête. `mcp-remote` 0.15.2 demande Node.js 20.18.1 ou plus récent (`node --version`), et
Claude Desktop prend le Node.js installé sur le système.

## 1. Essayer en cinq minutes : le sandbox

Lancez le sandbox et obtenez le token de Carol comme dans le [démarrage rapide](quickstart.md),
puis collez le token à la place de `<le token d'accès de l'utilisateur>`.

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

Pour **Claude Desktop**, mettez ceci dans `claude_desktop_config.json`
([`examples/claude-desktop.json`](../../examples/claude-desktop.json)) :

```json
{
  "mcpServers": {
    "trino": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.15.2",
        "http://localhost:3000/mcp",
        "--header",
        "Authorization:${AUTH_HEADER}"
      ],
      "env": {
        "AUTH_HEADER": "Bearer <le token d'accès de l'utilisateur>"
      }
    }
  }
}
```

La valeur de l'en-tête va dans `env`, et `args` porte `Authorization:${AUTH_HEADER}` sans espace.
`mcp-remote` remplace lui-même `${AUTH_HEADER}`, et sa documentation prévient que Claude Desktop
sous Windows et Cursor abîment les espaces dans `args`.

### Vérifier que ça marche

Dans Cursor, le serveur est listé dans **Customize**, ses tools sous *Available Tools*. Dans Claude
Desktop, ouvrez le menu **+** de la zone de saisie (*Add files, connectors, and more*), puis
**Connectors**, **Manage connectors**, et choisissez `trino`. Vous devez voir neuf tools :
`search_columns`, `list_catalogs`, `list_schemas`, `list_tables`, `describe_schema`,
`describe_table`, `profile_table`, `explain_query` et `execute_query`. Les versions 0.7 en ont 8,
sans `profile_table`. Posez ensuite les deux questions du
[démarrage rapide](quickstart.md#poser-une-question).

Pour changer d'utilisateur, collez un autre token et relancez le serveur. Dans Cursor,
désactivez-le puis réactivez-le dans **Customize** ; dans Claude Desktop, relancez l'application.

## 2. Votre propre Trino depuis votre poste : stdio et `oauth2`

Ici, le MCP host lance le serveur lui-même, sur votre ordinateur, et le serveur vous connecte à
Trino par la page de connexion de Trino. Il n'y a rien à déployer ni aucun token à coller. Il vous
faut :

- un Trino en https, avec son authentificateur OAuth2 configuré
  (`http-server.authentication.type` contient `OAUTH2`) ; ce chemin a été vérifié dans le
  [sandbox](https://github.com/AKKO-p/akko-sandbox), avec ses images épinglées ;
- [uv](https://docs.astral.sh/uv/), dont la commande `uvx` télécharge et lance akko-mcp-trino
  depuis PyPI, ou Docker (voir plus bas).

Pour **Cursor**, l'entrée de `~/.cursor/mcp.json` a cette forme
([`examples/cursor-oauth2.json`](../../examples/cursor-oauth2.json)) :

```json
{
  "mcpServers": {
    "trino": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "akko-mcp-trino==0.7.2"
      ],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "TRINO_IDENTITY_MODE": "oauth2",
        "TRINO_HOST": "trino.example.internal",
        "TRINO_PORT": "8443",
        "TRINO_HTTP_SCHEME": "https",
        "TRINO_CATALOG": "iceberg",
        "MCP_AUTH_ENABLED": "false",
        "MCP_AUTH_REQUIRED": "false"
      }
    }
  }
}
```

Pour **Claude Desktop**, c'est la même entrée sans `"type": "stdio"`
([`examples/claude-desktop-oauth2.json`](../../examples/claude-desktop-oauth2.json)).

Remplacez `TRINO_HOST`, `TRINO_PORT` et `TRINO_CATALOG` par les vôtres. Si le certificat de Trino
est signé par l'autorité de votre organisation, ajoutez `"TRINO_VERIFY": "/path/to/ca.pem"`.
`MCP_AUTH_ENABLED` et `MCP_AUTH_REQUIRED` valent `false` parce que ce chemin n'a pas de token MCP :
c'est Trino qui vous authentifie, et chaque requête s'exécute au nom de l'utilisateur que Trino
nomme.

Le serveur le signale à son démarrage, dans le journal du MCP host et dans les instructions qu'il
donne au MCP host à l'initialisation :

```
WARNING:akko_mcp_trino.__main__:MCP_AUTH_ENABLED=false: no caller is verified; anyone who reaches this server queries Trino
```

Ici, c'est attendu. En stdio, seul le processus du MCP host qui a lancé le serveur sur votre
ordinateur peut lui parler, et c'est la page de connexion de Trino qui dit qui vous êtes : aucune
requête ne part avant votre connexion, et chacune s'exécute à votre nom. Le même réglage sur un
serveur que d'autres joignent par le réseau laisserait n'importe qui interroger Trino à travers
lui. N'y coupez jamais la vérification du token, et gardez `oauth2` pour le stdio, ce que le
serveur impose.

**Avec Docker au lieu d'uv**, passez les mêmes variables à `docker run` (pour Cursor, ajoutez
`"type": "stdio"` comme plus haut) :

```json
{
  "mcpServers": {
    "trino": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "MCP_TRANSPORT=stdio",
        "-e", "TRINO_IDENTITY_MODE=oauth2",
        "-e", "TRINO_HOST=trino.example.internal",
        "-e", "TRINO_PORT=8443",
        "-e", "TRINO_HTTP_SCHEME=https",
        "-e", "TRINO_CATALOG=iceberg",
        "-e", "MCP_AUTH_ENABLED=false",
        "-e", "MCP_AUTH_REQUIRED=false",
        "akkoai/akko-mcp-trino:0.7.2"
      ]
    }
  }
}
```

Dans un conteneur, le serveur ne peut pas ouvrir votre navigateur : lisez l'URL de connexion dans
le journal du MCP host (voir ci-dessous). Pour `TRINO_VERIFY`, montez aussi l'autorité dans le
conteneur (`"-v", "/path/to/ca.pem:/ca.pem:ro", "-e", "TRINO_VERIFY=/ca.pem"`).

### Ce qui se passe à la première question

Le démarrage du serveur et la liste des tools ne déclenchent aucune connexion : le MCP host affiche
tout de suite les tools. Le premier tool call, lui, en déclenche une. Le serveur ouvre la page de
connexion de Trino dans votre navigateur et écrit son URL sur stderr, que les MCP hosts gardent
dans leurs journaux. Claude Desktop écrit `mcp-server-trino.log` dans `~/Library/Logs/Claude`,
`%APPDATA%\Claude\logs` ou `~/.config/Claude/logs` ; Cursor l'affiche dans le panneau **Output**,
rubrique **MCP Logs**. Connectez-vous. Le serveur vérifie alors qui vous êtes avec
`SELECT current_user`, puis exécute l'appel.

**Le MCP host peut abandonner pendant que vous vous connectez.** Si la première réponse est un
timeout, terminez la connexion, puis posez la même question : la connexion est gardée, et le
deuxième appel s'exécute. Vous restez l'utilisateur de ce processus jusqu'à son arrêt. Pour
changer d'utilisateur, relancez le serveur.

Si votre Trino authentifie par JWT plutôt que par OAuth2, le MCP host peut quand même lancer le
serveur en stdio, avec votre token d'accès dans `MCP_USER_TOKEN` : voir
[MCP hosts locaux en stdio](#mcp-hosts-locaux-en-stdio).

## 3. Un serveur partagé pour une équipe : streamable HTTP et un token par utilisateur

Un serveur, déployé une fois, sert toute l'équipe, et le MCP host de chaque personne envoie le
bearer token de cette personne. C'est le mode par défaut, `jwt`. Le serveur vérifie le token, émis
pour le serveur par votre identity provider. Il l'échange auprès de l'identity provider contre un
token émis pour Trino, et Trino exécute la requête au nom de l'utilisateur, avec ses propres
droits. Déployez le serveur comme dans [Déploiement](deployment.md#lancer), derrière https, avec
`MCP_RESOURCE_URL` à son adresse publique.

Les entrées ci-dessous mettent le token de chaque utilisateur dans un en-tête écrit à la main.
C'est un raccourci pour tester et pour un premier déploiement, pas la façon dont une équipe doit
travailler au quotidien. Le token expire, souvent en moins d'une heure (une heure dans le
sandbox), et chacun doit alors en demander un nouveau, le coller et relancer le serveur dans son
MCP host.

Pour **Cursor**, l'entrée pointe vers l'adresse du serveur :

```json
{
  "mcpServers": {
    "trino": {
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <le token d'accès de l'utilisateur>"
      }
    }
  }
}
```

Cursor résout `${env:NAME}` dans `url` et `headers`. L'en-tête peut donc valoir
`"Bearer ${env:AKKO_MCP_TOKEN}"` et garder le token hors du fichier, à condition que Cursor soit
lancé avec cette variable.

Pour **Claude Desktop**, on passe par le pont :

```json
{
  "mcpServers": {
    "trino": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.15.2",
        "https://mcp.example.com/mcp",
        "--header",
        "Authorization:${AUTH_HEADER}"
      ],
      "env": {
        "AUTH_HEADER": "Bearer <le token d'accès de l'utilisateur>"
      }
    }
  }
}
```

Si le certificat du serveur est signé par l'autorité de votre organisation, ajoutez
`"NODE_EXTRA_CA_CERTS": "/path/to/ca.pem"` dans `env` : le pont ne l'hérite pas de votre shell.
`mcp-remote` sait aussi lire l'en-tête dans un fichier (`--header-file`, une ligne `Nom: valeur`
par en-tête) plutôt que dans la configuration.

**Obtenir et renouveler le token.** Chaque utilisateur demande à l'identity provider un token
d'accès dont l'audience est le serveur (`MCP_OIDC_AUDIENCE` ou `MCP_RESOURCE_URL`), comme votre
organisation en délivre déjà aux outils en ligne de commande. Le serveur refuse un token émis pour
Trino, car il n'accepte que les tokens émis pour lui. Quand le MCP host signale un `401`, le token
a expiré : demandez-en un nouveau, remplacez-le et relancez le serveur dans le MCP host.

**Ce qu'une équipe doit faire à la place.** Le MCP host de chacun doit obtenir et renouveler
lui-même le token de son utilisateur, par l'autorisation MCP. Avec `MCP_RESOURCE_URL`, le serveur
publie les métadonnées qui mènent le MCP host à l'identity provider. Cursor documente une
connexion OAuth pour les serveurs distants, et c'est ce que fait `mcp-remote` quand on ne lui donne
pas d'en-tête. Aucun de ces deux chemins n'a été vérifié avec ce serveur, donc cette page ne
montre que l'en-tête. Une équipe ne doit jamais partager un token entre plusieurs personnes : le
serveur verrait un seul utilisateur, Trino appliquerait les droits de celui-là, et l'audit le
nommerait pour tout le monde. Le même raisonnement écarte une identité partagée ; voir
[Notes de conception](design-notes.md#pourquoi-pas-une-identité-partagée).

## MCP hosts locaux en stdio

Claude Desktop, Cursor et Mistral Vibe peuvent lancer le serveur eux-mêmes. Il n'y a alors pas de
requête HTTP, donc le serveur prend l'identité de l'appelant dans l'environnement : le propre
token d'accès de l'utilisateur, émis pour ce serveur, vérifié exactement comme un en-tête bearer,
et lié au processus. Avec `MCP_AUTH_REQUIRED=true`, le réglage par défaut, le serveur refuse de
démarrer sans lui. Les requêtes s'exécutent au nom de l'utilisateur en mode `jwt` (le token est
échangé contre un token pour Trino, comme en http) et en mode `impersonation`, et sous
`TRINO_USER` en mode `service`.

L'entrée ci-dessous est [`examples/mcp-stdio.json`](../../examples/mcp-stdio.json) :

```json
{
  "mcpServers": {
    "trino": {
      "command": "uvx",
      "args": [
        "akko-mcp-trino==0.7.2"
      ],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "TRINO_HOST": "trino.example.internal",
        "TRINO_PORT": "8443",
        "TRINO_HTTP_SCHEME": "https",
        "MCP_JWKS_URL": "https://idp.example.com/realms/data/protocol/openid-connect/certs",
        "MCP_OIDC_ISSUER": "https://idp.example.com/realms/data",
        "MCP_OIDC_AUDIENCE": "akko-mcp",
        "MCP_JWT_USER_CLAIM": "preferred_username",
        "TRINO_TOKEN_EXCHANGE_URL": "https://idp.example.com/realms/data/protocol/openid-connect/token",
        "TRINO_TOKEN_EXCHANGE_CLIENT_ID": "akko-mcp",
        "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET": "<this server's client secret>",
        "TRINO_TOKEN_EXCHANGE_AUDIENCE": "trino",
        "MCP_USER_TOKEN": "<the user's access token, issued for the MCP server>"
      }
    }
  }
}
```

`MCP_JWT_USER_CLAIM` est posé pour Keycloak ; la valeur par défaut est `sub` (voir
[Le claim de l'utilisateur](identity.md#le-claim-de-lutilisateur)).

## Depuis Python

Avec le SDK officiel, un client s'écrit ainsi :

```python
import anyio
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


async def main(token: str):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    url = "http://localhost:3000/mcp"
    async with streamable_http_client(url, http_client=http) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("execute_query", {"sql": "SELECT 1"})
            print(result.content[0].text)


anyio.run(main, "<token>")
```

## Depuis un agent

[`examples/agent.py`](../../examples/agent.py) est un agent complet en quatre-vingts lignes. Il
marche avec tout modèle compatible OpenAI (Mistral sur La Plateforme, par OpenRouter ou LiteLLM,
ou tout autre fournisseur), les tools MCP et le token de l'utilisateur. Le modèle ne voit jamais le
token : le serveur le reçoit à chaque tool call et le vérifie.

```bash
pip install akko-mcp-trino openai
export LLM_BASE_URL=https://api.mistral.ai/v1
export LLM_API_KEY=...
export LLM_MODEL=mistral-small-latest
export MCP_URL=http://localhost:3000/mcp
export USER_TOKEN=<the user's access token>
python examples/agent.py "Which catalogs can I see, and what is in them?"
```

Le dossier [examples](../../examples/README.md) donne le détail (en anglais).

## Vérifier une configuration sans le MCP host

[`examples/check_host_config.py`](../../examples/check_host_config.py) lit le même fichier que le
MCP host, lance ou joint le serveur de la même façon, liste les tools et appelle
`search_columns`. Il ne fait pas partie du paquet : lancez-le depuis un clone de ce dépôt, avec
Python 3.12 ou plus récent.

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

Cette sortie vient de la 0.7.2, qui a 8 tools. À partir de la 0.8, la liste comprend aussi
`profile_table`.

Il résout les `${env:NAME}` de Cursor depuis votre environnement, comme Cursor, et s'arrête si
l'une d'elles n'est pas définie. Pour un fichier qui déclare plusieurs serveurs, nommez l'entrée
avec `--server trino`.

## Les erreurs courantes

| Ce que vous voyez | Pourquoi | Que faire |
|---|---|---|
| `401`, `this server requires a bearer token issued for it by the identity provider: send Authorization: Bearer <access token>` | pas d'en-tête, ou un en-tête sans le préfixe `Bearer ` | écrire `Bearer ` puis le token, avec l'espace |
| `401`, `the bearer token is invalid, expired, or not issued for this server` | le token a expiré (une heure dans le sandbox), ou son audience est un autre client, Trino compris | demander un nouveau token émis pour le serveur, le remplacer et relancer le serveur dans le MCP host ; dans le sandbox, `./token.sh <utilisateur>` sans second argument |
| `401`, et le serveur écrit `token refused: no sub claim` (ou le claim que nomme `MCP_JWT_USER_CLAIM`) | le token n'a pas ce claim, ou sa valeur est vide ou n'est pas du texte | régler `MCP_JWT_USER_CLAIM` sur le claim où votre identity provider place l'utilisateur (`preferred_username` pour Keycloak) |
| le pont écrit `Dynamic Client Registration rejected (HTTP 401)` | le serveur a refusé le token, et `mcp-remote` a tenté de se connecter seul | corriger le token comme plus haut ; s'il insiste, supprimer `~/.mcp-auth` et relancer le MCP host |
| le pont écrit `Non-HTTPS URLs are only allowed for localhost or when --allow-http flag is provided` | une URL `http://` qui n'est pas `localhost` | exposer le serveur en https (chemin 3) ; `--allow-http` seulement sur un réseau privé de confiance |
| le pont écrit `ReferenceError: File is not defined`, ou `npm warn EBADENGINE` pour `undici` | Node.js est plus ancien que la 20.18.1 | installer Node.js 20.18.1 ou plus récent ; Claude Desktop prend celui du système |
| le pont écrit `unable to get local issuer certificate` | le certificat du serveur vient d'une autorité que Node.js ne connaît pas | `NODE_EXTRA_CA_CERTS` dans le `env` de l'entrée |
| `TRINO_IDENTITY_MODE=oauth2 requires TRINO_HTTP_SCHEME=https` | `oauth2` en http | `TRINO_HTTP_SCHEME=https`, et le port https de Trino |
| `TRINO_IDENTITY_MODE=oauth2 requires MCP_TRANSPORT=stdio` | `oauth2` sur un transport HTTP | `oauth2` sert au chemin 2 seulement ; un serveur d'équipe utilise `jwt` |
| `MCP_AUTH_REQUIRED=true and MCP_USER_TOKEN is missing or invalid: refusing to serve over stdio without a verified identity` | une entrée stdio en mode `jwt` sans token | ajouter `MCP_USER_TOKEN`, ou passer en `oauth2` avec `MCP_AUTH_ENABLED` et `MCP_AUTH_REQUIRED` à `false` |
| la première question du chemin 2 part en timeout | le MCP host a cessé d'attendre pendant la connexion navigateur | terminer la connexion, puis reposer la question |
| le serveur ne démarre pas, `spawn uvx ENOENT` ou `npx` introuvable dans le journal du MCP host | le MCP host ne voit pas le `PATH` de votre shell | mettre le chemin complet dans `command` (`which uvx`, `which npx`) |
| les requêtes échouent sur un ancien Trino | akko-mcp-trino parle le protocole des versions de Trino listées dans [Prérequis](prerequisites.md#compatibilité) | lire la version (`curl https://<trino>/v1/info`) et mettre Trino à jour |

## Annexe : ce qui a été vérifié

Les 1er et 2 novembre 2026, sur une machine Linux, avec le sandbox au commit `dac01db`,
akko-mcp-trino 0.7.0 depuis PyPI (`uvx akko-mcp-trino==0.7.0`) et Docker Hub
(`akkoai/akko-mcp-trino:0.7.0`), le SDK Python MCP officiel 2.3.0, Node.js 22 et 20.18.1, et
`mcp-remote` 0.15.2, voici ce qui a été exécuté. Les versions épinglées dans cette page ont été
montées en 0.7.2 ensuite.

| Configuration | Exécutée comment | Résultat |
|---|---|---|
| Cursor, chemin 1 (`url`, `headers`) | streamable HTTP, token de Carol | 8 tools en 0.7 ; `search_columns("email")` trouve `demo.crm.customers.email` |
| Claude Desktop, chemin 1 (`npx mcp-remote`) | stdio, le pont lancé par le client, sous Node.js 22 et sous Node.js 20.18.1 | idem |
| Cursor et Claude Desktop, chemin 2 (`uvx`, `oauth2`) | stdio, le Trino du sandbox avec son authentificateur OAuth2, un navigateur scripté qui se connecte à Keycloak | l'avertissement cité plus haut au démarrage ; 8 tools en 0.7 avant toute connexion ; le premier appel ouvre la connexion puis répond ; `SELECT current_user` rend `carol_analyst` |
| chemin 2, le MCP host qui abandonne | un premier appel limité à 5 secondes pendant la connexion, puis un second | le premier part en timeout, le second répond au nom de `carol_analyst` |
| chemin 2 avec Docker | stdio, `docker run -i` | 8 tools en 0.7, la recherche répond |
| Cursor et Claude Desktop, chemin 3 | https derrière un proxy TLS, autorité privée (`NODE_EXTRA_CA_CERTS` pour le pont) | 8 tools en 0.7, la recherche répond |
| les erreurs du tableau | `curl` et les mêmes clients | les messages cités plus haut |

Pour les essais sur le sandbox, seuls les emplacements à remplacer ont changé : le token, le nom
de Trino dans le réseau du sandbox (`trino`), son catalog `demo` et son autorité de démonstration
(`TRINO_VERIFY`) ; l'essai Docker avait en plus `--network` et un volume pour cette autorité. Les
`${env:NAME}` de Cursor ont été résolus par le script de vérification, comme le décrit la
documentation de Cursor. L'authentificateur OAuth2 de Trino a été ajouté au sandbox pour ce seul
essai : le sandbox lui-même authentifie par JWT. Les lignes du tableau d'erreurs sur le `PATH` et
sur un ancien Trino viennent de la documentation, pas d'un essai. Ce que nous n'avons pas fait,
c'est ouvrir Claude Desktop ou Cursor et cliquer dans leurs menus.

Les sources sont [Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
(le fichier de Claude Desktop, ses journaux, ses tools),
[Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom) (les
connecteurs distants de Claude), [Cursor, Model Context Protocol](https://cursor.com/docs/context/mcp)
(les fichiers, clés et variables de Cursor) et [mcp-remote](https://github.com/punkpeye/mcp-remote)
(le pont).
