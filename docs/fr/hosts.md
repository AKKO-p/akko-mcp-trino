# Brancher un MCP host

[English](../en/hosts.md) · **Français**

Le MCP host envoie le token d'accès de l'utilisateur, émis pour ce serveur. Le serveur le vérifie
et nomme l'appelant dans la ligne d'audit. Cette page branche quatre MCP hosts (Claude Desktop,
Claude Code, Cursor et VS Code). Elle couvre aussi tout autre MCP host en stdio, le SDK Python et
deux agents, dont un sur l'API Agents de Mistral. Les MCP hosts suivent l'un des trois chemins :

| Chemin | Transport | La requête s'exécute au nom de | Pour |
|---|---|---|---|
| [1. Essayer en cinq minutes](#1-essayer-en-cinq-minutes-le-sandbox) | streamable HTTP, `http://localhost:3000/mcp` | l'utilisateur du sandbox dont vous collez le token | un premier essai sur votre portable |
| [2. Votre propre Trino depuis votre poste](#2-votre-propre-trino-depuis-votre-poste-stdio-et-oauth2) | stdio, le MCP host lance le serveur | vous, connecté par la page de connexion de Trino (`oauth2`) | une personne, avec un Trino qui utilise OAuth2 |
| [3. Un serveur partagé pour une équipe](#3-un-serveur-partagé-pour-une-équipe-streamable-http-et-un-token-par-utilisateur) | streamable HTTP en HTTPS | chaque utilisateur, d'après son propre bearer token (`jwt`, le mode par défaut) | une équipe, un seul serveur |

**Versions.** Les commandes de cette page épinglent la 0.8.1, la release actuelle, qui liste neuf
tools.

**Trouver votre MCP host.**

| MCP host | Chemin 1 | Chemin 2 | Chemin 3 | Tout MCP host en stdio |
|---|---|---|---|---|
| Claude Desktop | [pont](#claude-desktop) | [entrée](#2-votre-propre-trino-depuis-votre-poste-stdio-et-oauth2) | [pont](#3-un-serveur-partagé-pour-une-équipe-streamable-http-et-un-token-par-utilisateur) | [entrée](#tout-mcp-host-en-stdio-avec-votre-propre-token) |
| Claude Code | [`claude mcp add`](#claude-code) | non traité | [commande du chemin 1](#3-un-serveur-partagé-pour-une-équipe-streamable-http-et-un-token-par-utilisateur) | [`claude mcp add`](#tout-mcp-host-en-stdio-avec-votre-propre-token) |
| Cursor | [entrée](#cursor) | [entrée](#2-votre-propre-trino-depuis-votre-poste-stdio-et-oauth2) | [entrée](#3-un-serveur-partagé-pour-une-équipe-streamable-http-et-un-token-par-utilisateur) | [entrée](#tout-mcp-host-en-stdio-avec-votre-propre-token) |
| VS Code | [fichier](#vs-code) | non traité | [fichier du chemin 1](#3-un-serveur-partagé-pour-une-équipe-streamable-http-et-un-token-par-utilisateur) | [fichier](#tout-mcp-host-en-stdio-avec-votre-propre-token) |

Le SDK Python et les agents se branchent comme au chemin 1 : voir
[Depuis Python](#depuis-python), [Depuis un agent](#depuis-un-agent) et
[Depuis l'API Agents de Mistral](#depuis-lapi-agents-de-mistral).

**Ce que reçoit le fournisseur du modèle.** Quel que soit le MCP host ou l'agent, les résultats
des tools partent chez le fournisseur du modèle qu'il utilise. Ils contiennent des données que
l'utilisateur a le droit de lire. Le token n'en fait pas partie : il reste entre le MCP host et le
serveur. Consultez les réglages de données et de confidentialité de ce fournisseur.

> **Ce que nous avons vérifié.** Nous avons exécuté chaque bloc JSON des trois chemins avec le SDK
> Python officiel de MCP, sur le sandbox et son catalogue de données, avec un serveur construit
> depuis `main`. Nous n'avons pas piloté les interfaces de Claude Desktop, de Claude Code, de
> Cursor et de VS Code ; les noms des menus viennent de leur documentation. L'exemple de l'API
> Agents de Mistral a tourné avec `mistral-medium-latest` ;
> [`examples/agent.py`](../../examples/agent.py) n'est pas allé jusqu'au bout.
> L'[annexe](#annexe-ce-qui-a-été-vérifié) dit ce qui a tourné, ce qui n'a pas tourné, et les
> versions.

## Où se trouve la configuration

| MCP host | Fichier | À savoir |
|---|---|---|
| Claude Desktop, macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` | s'ouvre depuis **Settings > Developer > Edit Config** ; quittez complètement Claude Desktop et relancez-le après chaque changement |
| Claude Desktop, Windows | `%APPDATA%\Claude\claude_desktop_config.json` | idem |
| Claude Desktop, Linux | `~/.config/Claude/claude_desktop_config.json` | idem |
| Claude Code | `~/.claude.json` pour les portées `local` (par défaut) et `user` ; `.mcp.json` à la racine du projet pour la portée `project` | écrit par `claude mcp add` ; `claude mcp list` vérifie chaque serveur |
| Cursor, tous les projets | `~/.cursor/mcp.json`, dans votre dossier personnel (`%USERPROFILE%\.cursor\mcp.json` sous Windows) | |
| Cursor, un seul projet | `.cursor/mcp.json` à la racine du projet | |
| VS Code, un seul espace de travail | `.vscode/mcp.json` dans l'espace de travail | l'objet s'appelle `servers`, pas `mcpServers` |
| VS Code, tous les espaces de travail | le `mcp.json` de votre profil utilisateur, ouvert par la commande **MCP: Open User Configuration** | idem |

Les fichiers de Claude Desktop, de Claude Code et de Cursor contiennent un objet `mcpServers`,
avec une entrée par serveur. VS Code appelle cet objet `servers`. Il déclare dans `inputs` les
valeurs qu'il vous demande. Les exemples appellent l'entrée `trino`. Si votre fichier déclare déjà
d'autres serveurs, ajoutez l'entrée à côté.

**Comment chaque MCP host joint le serveur.** Claude Code, Cursor et VS Code se connectent
eux-mêmes à un serveur streamable HTTP, avec une entrée `url` et `headers`.

Le fichier de configuration de Claude Desktop ne définit que des serveurs locaux. Chaque entrée a
une `command`, ses `args` et son `env`, et Claude Desktop lance le processus. Ses connecteurs
distants, ajoutés dans **Customize > Connectors**, se connectent depuis le cloud d'Anthropic et
non depuis votre ordinateur
([Claude Help Center](https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp)).
Ils ne peuvent donc pas joindre un serveur sur `localhost` ou sur un réseau privé, sauf si votre
organisation utilise les [MCP tunnels](https://claude.com/docs/connectors/mcp-tunnels/overview).
Un en-tête posé sur un connecteur distant est un seul identifiant, partagé par tous. Pour que
chacun se connecte sous son propre nom, il faut OAuth et un serveur joignable depuis internet
([documentation de Claude](https://claude.com/docs/connectors/custom)). Pour un serveur HTTP,
Claude Desktop lance donc ici un petit pont local,
[`mcp-remote`](https://github.com/punkpeye/mcp-remote), qui relaie les requêtes vers l'URL du
serveur avec votre en-tête. `mcp-remote` 0.15.2 demande Node.js 20.18.1 ou plus récent
(`node --version`), et Claude Desktop utilise le Node.js installé sur le système.

**Gardez les tokens hors de l'historique du shell.** Un token ou un client secret tapé dans une
ligne de commande finit dans l'historique du shell. Les commandes de cette page le lisent d'abord
avec `read -rs`, qui n'affiche rien, puis passent la variable.

## 1. Essayer en cinq minutes : le sandbox

Lancez le sandbox et obtenez le token de Carol comme dans le [démarrage rapide](quickstart.md).
Dans un fichier, collez le token à la place de `<le token d'accès de l'utilisateur>`. Dans une
commande, collez-le à l'invite de `read -rs`.

### Claude Desktop

Mettez ceci dans `claude_desktop_config.json`
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
sous Windows et Cursor altèrent les espaces dans `args`.

### Claude Code

Ajoutez le serveur en ligne de commande ([Claude Code, MCP](https://code.claude.com/docs/en/mcp)) :

```bash
read -rs TRINO_TOKEN    # collez le token, puis Entrée
claude mcp add --transport http trino http://localhost:3000/mcp \
  --header "Authorization: Bearer $TRINO_TOKEN"
```

```text
Added HTTP MCP server trino with URL: http://localhost:3000/mcp to local config
Headers: {
  "Authorization": "[REDACTED]"
}
```

La portée par défaut, `local`, garde l'entrée dans `~/.claude.json`, pour vous et pour ce projet
seulement. Claude Code y enregistre l'en-tête en clair, et `claude mcp get trino` l'affiche :
ne collez jamais cette sortie dans un ticket ou une discussion. `claude mcp list` n'affiche ni
l'en-tête ni le token. `--scope project` écrit l'entrée dans `.mcp.json`, un fichier fait pour
être versionné. Écrivez-y `"Authorization": "Bearer ${TRINO_TOKEN}"` : Claude Code remplace
`${VAR}` dans `.mcp.json` par la valeur de son propre environnement, et le token reste hors du
fichier. Exportez la variable (`export TRINO_TOKEN`) dans le shell qui lance Claude Code.

### Cursor

Mettez ceci dans `~/.cursor/mcp.json` ([`examples/cursor.json`](../../examples/cursor.json)) :

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

### VS Code

Mettez ceci dans `.vscode/mcp.json` ([`examples/vscode-mcp.json`](../../examples/vscode-mcp.json)) :

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "trino-token",
      "description": "The user's access token for the MCP server",
      "password": true
    }
  ],
  "servers": {
    "trino": {
      "type": "http",
      "url": "http://localhost:3000/mcp",
      "headers": {
        "Authorization": "Bearer ${input:trino-token}"
      }
    }
  }
}
```

Les projets versionnent souvent `.vscode/mcp.json`, qui ne doit donc contenir aucun secret.
VS Code demande le token au premier lancement du serveur et l'enregistre hors du fichier
([VS Code, MCP configuration](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration)).

### Vérifier que tout fonctionne

Dans Claude Desktop, ouvrez le menu **+** de la zone de saisie (*Add files, connectors, and
more*), puis **Connectors**, **Manage connectors**, et choisissez `trino`. Claude Code vérifie
chaque serveur en ligne de commande :

```bash
claude mcp list
```

```text
Checking MCP server health…

trino: http://localhost:3000/mcp (HTTP) - ✔ Connected
```

Dans une session Claude Code, `/mcp` montre l'état du serveur. Dans Cursor, le serveur est listé
dans **Customize**, ses tools sous *Available Tools*. Dans VS Code, lancez la commande
**MCP: List Servers**.

Avec la 0.8.1, la release épinglée dans cette page, vous devez voir neuf tools : `search_columns`,
`list_catalogs`, `list_schemas`, `list_tables`, `describe_schema`, `describe_table`,
`profile_table`, `explain_query` et `execute_query`. Posez ensuite les deux questions du
[démarrage rapide](quickstart.md#poser-une-question).

Pour changer d'utilisateur, donnez un autre token au MCP host et relancez le serveur. Dans Claude
Desktop, relancez l'application. Dans Claude Code, lancez `claude mcp remove trino`, puis
ajoutez-le de nouveau avec le nouveau token. Dans Cursor, désactivez le serveur puis réactivez-le
dans **Customize**. VS Code enregistre le token qu'il a demandé ; sa documentation ne dit pas
comment le remplacer, et nous n'avons pas essayé.

## 2. Votre propre Trino depuis votre poste : stdio et `oauth2`

Ici, le MCP host lance le serveur lui-même, sur votre ordinateur, et le serveur vous connecte à
Trino par la page de connexion de Trino. Il n'y a rien à déployer et aucun token à coller. Il vous
faut :

- un Trino en HTTPS, avec son authentificateur OAuth2 configuré
  (`http-server.authentication.type` contient `OAUTH2`) ; ce chemin a été vérifié dans le
  [sandbox](https://github.com/AKKO-p/akko-sandbox), avec ses images épinglées ;
- [uv](https://docs.astral.sh/uv/), dont la commande `uvx` télécharge et lance akko-mcp-trino
  depuis PyPI, ou Docker (voir plus bas).

Pour **Claude Desktop**, reprenez l'entrée ci-dessous sans `"type": "stdio"`
([`examples/claude-desktop-oauth2.json`](../../examples/claude-desktop-oauth2.json)). Pour
**Cursor**, mettez-la telle quelle dans `~/.cursor/mcp.json`
([`examples/cursor-oauth2.json`](../../examples/cursor-oauth2.json)).

```json
{
  "mcpServers": {
    "trino": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "akko-mcp-trino==0.8.1"
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

Remplacez les valeurs de `TRINO_HOST`, `TRINO_PORT` et `TRINO_CATALOG` par vos valeurs. Si le
certificat de Trino est signé par l'autorité de certification (CA) de votre organisation, ajoutez
`"TRINO_VERIFY": "/path/to/ca.pem"`. `MCP_AUTH_ENABLED` et `MCP_AUTH_REQUIRED` valent `false`
parce que ce chemin n'a pas de token MCP : Trino vous authentifie, et chaque requête s'exécute au
nom de l'utilisateur que Trino a authentifié.

Le serveur le signale au démarrage. L'avertissement va dans le journal du MCP host et dans les
instructions que le serveur envoie au MCP host à l'initialisation :

```
WARNING:akko_mcp_trino.__main__:MCP_AUTH_ENABLED=false: no caller is verified; anyone who reaches this server queries Trino
```

Ici, c'est normal. En stdio, seul le processus du MCP host qui a lancé le serveur sur votre
ordinateur peut lui parler, et la page de connexion de Trino décide qui vous êtes : aucune requête
ne part avant votre connexion, et chacune s'exécute en votre nom. Le même réglage sur un serveur
que d'autres personnes joignent par le réseau laisserait n'importe qui interroger Trino à travers
lui. N'y coupez jamais la vérification du token. N'utilisez `oauth2` qu'en stdio : le serveur le
refuse en HTTP.

**Avec Docker plutôt que uv**, passez les mêmes variables à `docker run` (pour Cursor, ajoutez
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
        "akkoai/akko-mcp-trino:0.8.1"
      ]
    }
  }
}
```

Dans un conteneur, le serveur ne peut pas ouvrir votre navigateur : cherchez l'URL de connexion
dans le journal du MCP host (voir ci-dessous). Pour `TRINO_VERIFY`, montez aussi le certificat de
l'autorité dans le conteneur (`"-v", "/path/to/ca.pem:/ca.pem:ro", "-e", "TRINO_VERIFY=/ca.pem"`).

### Ce qui se passe à la première question

Le démarrage du serveur et la liste des tools ne déclenchent aucune connexion : le MCP host affiche
tout de suite les tools. Le premier tool call déclenche la connexion. Le serveur ouvre la page de
connexion de Trino dans votre navigateur et écrit son URL sur stderr, que les MCP hosts gardent
dans leurs journaux. Dans l'essai sur le sandbox, où Trino s'appelle `trino`, la ligne était :

```text
WARNING:akko_mcp_trino.trino_client:Open this URL to authenticate with Trino: https://trino:8443/oauth2/token/initiate/...
```

Claude Desktop l'écrit dans `mcp-server-trino.log`, sous `~/Library/Logs/Claude` sur macOS,
`%APPDATA%\Claude\logs` sous Windows et `~/.config/Claude/logs` sous Linux. Cursor l'affiche dans
le panneau **Output**, rubrique **MCP Logs**. Ouvrez l'URL et connectez-vous. Le serveur vérifie
alors qui vous êtes avec `SELECT current_user`, puis exécute l'appel.

**Le MCP host peut abandonner pendant que vous vous connectez.** Si la première réponse est un
timeout, terminez la connexion, puis posez la même question : le serveur garde la connexion, et le
deuxième appel s'exécute. Le processus s'exécute en votre nom jusqu'à son arrêt. Pour changer
d'utilisateur, relancez le serveur.

Cette page ne traite pas le chemin 2 pour Claude Code ni pour VS Code. Si votre Trino authentifie
par JWT plutôt que par OAuth2, tout MCP host peut quand même lancer le serveur en stdio, avec
votre token d'accès dans `MCP_USER_TOKEN` : voir
[Tout MCP host en stdio, avec votre propre token](#tout-mcp-host-en-stdio-avec-votre-propre-token).

## 3. Un serveur partagé pour une équipe : streamable HTTP et un token par utilisateur

Un serveur, déployé une fois, sert toute l'équipe, et le MCP host de chaque personne envoie le
bearer token de cette personne. C'est le mode par défaut, `jwt`. Le serveur vérifie le token, que
votre identity provider a émis pour lui. Il l'échange ensuite auprès de l'IdP contre un token
destiné à Trino, et Trino exécute la requête au nom de l'utilisateur, avec ses propres droits.
Déployez le serveur comme dans [Déploiement](deployment.md#lancer), derrière HTTPS, avec
`MCP_RESOURCE_URL` à son adresse publique.

Les entrées ci-dessous mettent le token de chaque utilisateur dans un en-tête écrit à la main.
C'est un raccourci pour tester et pour un premier déploiement, pas la façon dont une équipe doit
travailler au quotidien. Le token expire, souvent au bout d'une heure (une heure dans le sandbox),
et chacun doit alors en demander un nouveau, le coller et relancer le serveur dans son MCP host.

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

Si le certificat du serveur est signé par l'autorité de certification de votre organisation,
ajoutez `"NODE_EXTRA_CA_CERTS": "/path/to/ca.pem"` dans `env` : le pont ne l'hérite pas de votre
shell. `mcp-remote` sait aussi lire l'en-tête dans un fichier (`--header-file`, une ligne
`Nom: valeur` par en-tête) plutôt que dans la configuration.

Pour **Claude Code**, reprenez la commande du chemin 1 avec l'adresse du serveur,
`https://mcp.example.com/mcp`.

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
`"Bearer ${env:AKKO_MCP_TOKEN}"` et garder le token hors du fichier, à condition que cette
variable soit définie quand Cursor démarre.

Pour **VS Code**, reprenez le fichier du chemin 1 avec la même adresse.

**Obtenir et renouveler le token.** Chaque utilisateur demande à l'IdP un token d'accès dont
l'audience est le serveur (`MCP_OIDC_AUDIENCE` ou `MCP_RESOURCE_URL`), comme votre organisation en
délivre déjà aux outils en ligne de commande. Le serveur refuse un token émis pour Trino, car il
n'accepte que les tokens émis pour lui. Quand le MCP host signale un `401`, le token a expiré :
demandez-en un nouveau, remplacez-le et relancez le serveur dans le MCP host.

**Ce qu'une équipe doit faire à la place.** Le MCP host de chacun doit obtenir et renouveler
lui-même le token de son utilisateur, par l'autorisation MCP. Avec `MCP_RESOURCE_URL`, le serveur
publie les métadonnées qui mènent le MCP host à l'IdP. Cursor documente une connexion OAuth pour
les serveurs distants, et c'est ce que fait `mcp-remote` quand on ne lui donne pas d'en-tête.
Aucune de ces deux connexions n'a été vérifiée avec ce serveur, donc cette page ne montre que
l'en-tête. Ne partagez jamais un token au sein d'une équipe : le serveur verrait un seul
utilisateur, Trino appliquerait les droits de celui-là, et l'audit le nommerait pour tout le
monde. Le même raisonnement écarte une identité partagée ; voir
[Notes de conception](design-notes.md#pourquoi-pas-une-identité-partagée).

## Tout MCP host en stdio, avec votre propre token

Claude Desktop, Claude Code, Cursor et VS Code peuvent lancer le serveur eux-mêmes. Il n'y a alors
pas de requête HTTP, et le serveur prend l'identité de l'appelant dans l'environnement. C'est le
token d'accès de l'utilisateur, émis pour ce serveur. Le serveur le vérifie exactement comme un
bearer token reçu en en-tête, et le lie au processus. Avec `MCP_AUTH_REQUIRED=true`, le défaut, le
serveur refuse de démarrer sans lui. En modes `jwt` et `impersonation`, les requêtes s'exécutent au
nom de l'utilisateur. En mode `jwt`, le token est échangé contre un token destiné à Trino, comme en
HTTP. En mode `service`, elles s'exécutent au nom de `TRINO_USER`.

L'entrée ci-dessous est [`examples/mcp-stdio.json`](../../examples/mcp-stdio.json), pour Claude
Desktop et Cursor :

```json
{
  "mcpServers": {
    "trino": {
      "command": "uvx",
      "args": [
        "akko-mcp-trino==0.8.1"
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

Cet exemple règle `MCP_JWT_USER_CLAIM` pour Keycloak. La valeur par défaut est `sub` (voir
[Le claim de l'utilisateur](identity.md#le-claim-de-lutilisateur)). Remplacez
`<this server's client secret>` par le client secret du serveur, et
`<the user's access token, issued for the MCP server>` par le token d'accès de l'utilisateur, émis
pour le serveur MCP. Le fichier garde ce token et ce client secret en clair. Dans Cursor,
`${env:NAME}` les garde hors du fichier, comme au chemin 3.

Dans **Claude Code**, chaque variable passe par un `--env`, et la commande vient après `--`. Lisez
d'abord le token et le client secret, pour que ni l'un ni l'autre n'entre dans l'historique du
shell :

```bash
read -rs MCP_USER_TOKEN                        # le token d'accès de l'utilisateur, émis pour le serveur MCP
read -rs TRINO_TOKEN_EXCHANGE_CLIENT_SECRET    # le client secret de ce serveur
claude mcp add \
  --env MCP_TRANSPORT=stdio \
  --env TRINO_HOST=trino.example.internal \
  --env TRINO_PORT=8443 \
  --env TRINO_HTTP_SCHEME=https \
  --env MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs \
  --env MCP_OIDC_ISSUER=https://idp.example.com/realms/data \
  --env MCP_OIDC_AUDIENCE=akko-mcp \
  --env MCP_JWT_USER_CLAIM=preferred_username \
  --env TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token \
  --env TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp \
  --env "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET=$TRINO_TOKEN_EXCHANGE_CLIENT_SECRET" \
  --env TRINO_TOKEN_EXCHANGE_AUDIENCE=trino \
  --env "MCP_USER_TOKEN=$MCP_USER_TOKEN" \
  --transport stdio trino \
  -- uvx akko-mcp-trino==0.8.1
```

`claude mcp list` affiche ensuite `✔ Connected` dès que le serveur répond. Comme au chemin 1,
Claude Code enregistre le token et le client secret en clair dans `~/.claude.json`, et
`claude mcp get trino` les affiche.

Dans **VS Code**, le token et le client secret viennent de `inputs`, et `.vscode/mcp.json` ne
contient ni l'un ni l'autre
([`examples/vscode-mcp-stdio.json`](../../examples/vscode-mcp-stdio.json)) :

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "trino-token",
      "description": "The user's access token for the MCP server",
      "password": true
    },
    {
      "type": "promptString",
      "id": "trino-client-secret",
      "description": "This server's client secret",
      "password": true
    }
  ],
  "servers": {
    "trino": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "akko-mcp-trino==0.8.1"
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
        "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET": "${input:trino-client-secret}",
        "TRINO_TOKEN_EXCHANGE_AUDIENCE": "trino",
        "MCP_USER_TOKEN": "${input:trino-token}"
      }
    }
  }
}
```

Nous avons exécuté ce fichier avec le SDK Python de MCP seulement, pas dans VS Code.

## Depuis Python

Avec le SDK Python officiel de MCP, un client s'écrit ainsi. Il lit le token dans `USER_TOKEN` :

```python
import os

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


async def main(token: str):
    url = "http://localhost:3000/mcp"
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("execute_query", {"sql": "SELECT 1"})
                print(result.content[0].text)


anyio.run(main, os.environ["USER_TOKEN"])
```

```text
{"columns": ["_col0"], "rows": [[1]], "row_count": 1, "truncated": false}
```

## Depuis un agent

[`examples/agent.py`](../../examples/agent.py) est un agent complet en moins de cent lignes. Il
associe tout modèle compatible OpenAI (Mistral sur La Plateforme, par OpenRouter ou LiteLLM, ou
tout autre fournisseur) aux tools MCP, et envoie le token de l'utilisateur au serveur. Le modèle
ne voit jamais le token : le serveur le reçoit à chaque tool call et le vérifie. Le modèle voit en
revanche les résultats des tools.

```bash
pip install akko-mcp-trino openai
export LLM_BASE_URL=https://api.mistral.ai/v1
read -rs LLM_API_KEY && export LLM_API_KEY
export LLM_MODEL=mistral-small-latest
export MCP_URL=http://localhost:3000/mcp
read -rs USER_TOKEN && export USER_TOKEN
python examples/agent.py "Which catalogs can I see, and what is in them?"
```

Cet exemple n'est pas allé jusqu'au bout sur `main` : le fournisseur a répondu
`429 - Rate limit exceeded`. Voir l'[annexe](#annexe-ce-qui-a-été-vérifié).

Le dossier [examples](../../examples/README.md) donne le détail (en anglais).

## Depuis l'API Agents de Mistral

[`examples/mistral_agents.py`](../../examples/mistral_agents.py) utilise le SDK officiel
`mistralai` et son client MCP. Le client MCP tourne dans votre processus : il joint le serveur avec
le token de l'utilisateur dans un en-tête et exécute chaque tool call que demande le modèle. L'API
Conversations de Mistral, qui fait partie de l'API Agents, reçoit la question, les descriptions des
tools et les résultats des tools. Elle ne reçoit jamais le token et ne joint jamais le serveur, qui
peut donc rester sur `localhost` ou sur un réseau privé.

```python
async def main(question: str) -> None:
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    server = MCPClientStreamableHTTP(
        StreamableHTTPServerParams(
            url=os.environ.get("MCP_URL", "http://localhost:3000/mcp"),
            headers={"Authorization": f"Bearer {os.environ['USER_TOKEN']}"},
        )
    )
    model = os.environ.get("MISTRAL_MODEL", "mistral-medium-latest")
    async with RunContext(model=model) as run_ctx:
        await run_ctx.register_mcp_client(server)
        result = await client.beta.conversations.run_async(run_ctx=run_ctx, inputs=question)
```

Le fichier complet ajoute les imports. Il affiche chaque tool call, le début de chaque résultat,
puis la réponse :

```bash
pip install "mistralai[agents]==3.7.0"
read -rs MISTRAL_API_KEY && export MISTRAL_API_KEY
export MCP_URL=http://localhost:3000/mcp
read -rs USER_TOKEN && export USER_TOKEN
python examples/mistral_agents.py "Which customers do we have, with their e-mail and country?"
```

Avec le token de Carol et `mistral-medium-latest`, le modèle a listé les catalogs, les schémas et
les tables, décrit `customers`, puis l'a interrogée. Carol obtient cinq clients européens, aux
adresses e-mail masquées :

```text
tool call: list_catalogs {}
tool result: [{"type": "text", "text": "{\"catalogs\": [\"demo\"], \"next_offset\": null, \"truncated\": false}"}]
tool call: list_schemas {"catalog": "demo"}
...
tool call: describe_table {"catalog": "demo", "schema": "crm", "table": "customers"}
...
tool call: execute_query {"sql": "SELECT full_name, email, country FROM demo.crm.customers"}
...
Here are the customers with their email addresses and countries:

| **Full Name**   | **Email**               | **Country** |
|-----------------|-------------------------|-------------|
| Lena Vogel      | ***@example.com         | DE          |
| Hugo Martin     | ***@example.com         | FR          |
| Sofia Ruiz      | ***@example.org         | ES          |
| Giulia Conti    | ***@example.org         | IT          |
| Emma de Vries   | ***@example.org         | NL          |
```

## Vérifier une configuration sans le MCP host

[`examples/check_host_config.py`](../../examples/check_host_config.py) lit le même fichier que le
MCP host, lance ou joint le serveur de la même façon, liste les tools et appelle
`search_columns`. Avec `--describe`, il appelle aussi `describe_table`. Il ne fait pas partie du
paquet : lancez-le depuis un clone de ce dépôt, avec Python 3.12 ou plus récent.

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python3 -m venv .venv && . .venv/bin/activate
pip install akko-mcp-trino==0.8.1
python3 examples/check_host_config.py ~/.cursor/mcp.json --search email --describe demo.crm.customers
```

Le script résout les variables `${env:NAME}` de Cursor depuis votre environnement, comme Cursor.
Il remplit les `${input:ID}` de VS Code depuis la variable `INPUT_<ID>`, par exemple
`INPUT_TRINO_TOKEN`, définie avec `read -rs INPUT_TRINO_TOKEN && export INPUT_TRINO_TOKEN`. Il
s'arrête si l'une d'elles n'est pas définie. Pour un fichier qui déclare plusieurs serveurs,
nommez l'entrée avec `--server trino`.

Cet essai a utilisé un serveur construit depuis `main`, sur le sandbox et son catalogue de données
(le profil `catalogs`), avec le token de Carol. Ce build précédait le passage à la 0.8.0 : il
annonce la 0.7.2 et liste les neuf tools de la 0.8.0. La sortie était :

```text
entry: trino (streamable HTTP)
server: trino-mcp 0.7.2
9 tools: search_columns, list_catalogs, list_schemas, list_tables, describe_schema, describe_table, profile_table, explain_query, execute_query
search_columns('email'):
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", "comment": "People and companies who hold an account"}], "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", "comment": null}], "schemas_searched": 5, "not_searched": [], "complete": true, "next_cursor": null}
describe_table('demo.crm.customers'):
{"columns": ["Column", "Type", "Extra", "Comment"], "rows": [["id", "integer", "", ""], ...], "truncated": false, "next_column_offset": null, "columns_cut": false, "table": {"description": "Everyone who holds an account: one row per person or company", "sources": {"description": "atlas"}, "terms": [{"name": "Customer", "ref": "t1"}]}, "column_context": {"id": {"description": "Identifier of the customer", "sources": {"description": "atlas"}}, ...}, "terms": {"t1": {"name": "Customer", "glossary": "Sales", "definition": "A person or a company that holds at least one account", "status": "approved", "source": "atlas"}}, "context_truncated": false, "undescribed_columns": [], "context_sources": {"atlas": "ok"}}
```

La `description`, les termes et `context_sources` viennent du catalogue de données, Apache Atlas,
et le serveur les relaie : voir [Contexte d'un catalogue de données](catalog-context.md). Un
serveur sans catalogue de données, comme l'entrée stdio plus haut, ne répond qu'avec les colonnes.

## Les erreurs courantes

| Ce que vous voyez | Pourquoi | Que faire |
|---|---|---|
| `401`, `this server requires a bearer token issued for it by the identity provider: send Authorization: Bearer <access token>` | pas d'en-tête, ou un en-tête sans le préfixe `Bearer ` | écrire `Bearer ` puis le token, avec l'espace |
| `401`, `the bearer token is invalid, expired, or not issued for this server` | le token a expiré (une heure dans le sandbox), ou son audience est un autre client, Trino compris | demander un nouveau token émis pour le serveur, le remplacer et relancer le serveur dans le MCP host ; dans le sandbox, `./token.sh <utilisateur>` sans second argument |
| `401`, et le serveur écrit `token refused: no sub claim` (ou le claim que nomme `MCP_JWT_USER_CLAIM`) | le token n'a pas ce claim, ou sa valeur est vide ou n'est pas du texte | régler `MCP_JWT_USER_CLAIM` sur le claim où votre IdP place l'utilisateur (`preferred_username` pour Keycloak) |
| le pont écrit `Dynamic Client Registration rejected (HTTP 401)` | le serveur a refusé le token, et `mcp-remote` a tenté de se connecter seul | corriger le token comme plus haut ; s'il insiste, supprimer `~/.mcp-auth` et relancer le MCP host |
| le pont écrit `Non-HTTPS URLs are only allowed for localhost or when --allow-http flag is provided` | une URL `http://` qui n'est pas `localhost` | placer le serveur derrière HTTPS (chemin 3) ; `--allow-http` seulement sur un réseau privé de confiance |
| le pont écrit `ReferenceError: File is not defined`, ou `npm warn EBADENGINE` pour `undici` | Node.js est plus ancien que la 20.18.1 | installer Node.js 20.18.1 ou plus récent ; Claude Desktop utilise celui du système |
| le pont écrit `unable to get local issuer certificate` | le certificat du serveur vient d'une autorité de certification que Node.js ne reconnaît pas | définir `NODE_EXTRA_CA_CERTS` dans le `env` de l'entrée |
| `TRINO_IDENTITY_MODE=oauth2 requires TRINO_HTTP_SCHEME=https` | `oauth2` en HTTP | régler `TRINO_HTTP_SCHEME=https`, avec le port HTTPS de Trino |
| `TRINO_IDENTITY_MODE=oauth2 requires MCP_TRANSPORT=stdio` | `oauth2` sur un transport HTTP | `oauth2` sert au chemin 2 seulement ; un serveur d'équipe utilise `jwt` |
| `MCP_AUTH_REQUIRED=true and MCP_USER_TOKEN is missing or invalid: refusing to serve over stdio without a verified identity` | une entrée stdio en mode `jwt` sans token | ajouter `MCP_USER_TOKEN`, ou passer en `oauth2` avec `MCP_AUTH_ENABLED` et `MCP_AUTH_REQUIRED` à `false` |
| la première question du chemin 2 échoue sur un timeout | le MCP host a cessé d'attendre pendant la connexion dans le navigateur | terminer la connexion, puis reposer la question |
| le serveur ne démarre pas, et le journal du MCP host affiche `spawn uvx ENOENT` ou `npx` introuvable | le MCP host ne voit pas le `PATH` de votre shell | mettre le chemin complet dans `command` (`which uvx`, `which npx`) |
| les requêtes échouent sur un ancien Trino | akko-mcp-trino prend en charge les versions de Trino listées dans [Prérequis](prerequisites.md#compatibilité) | lire la version (`curl https://<trino>/v1/info`) et mettre Trino à jour |

## Annexe : ce qui a été vérifié

Le 7 novembre 2026, les essais ont utilisé akko-mcp-trino construit depuis le commit `2236ee9` de
`main`, en image et en wheel, sur une machine Linux. Ils ont utilisé le sandbox au commit
`67f936a` avec son profil `catalogs` : `trinodb/trino:482`, `quay.io/keycloak/keycloak:26.7.5`,
`openpolicyagent/opa:1.21.1-static`, `postgres:17.11-alpine` et Apache Atlas 2.4.0, construit par
le `catalogs/atlas/Dockerfile` du sandbox. Les clients étaient le SDK Python officiel de MCP 2.3.0
sous Python 3.13, Node.js 22.23.4, `mcp-remote` 0.15.2, uv 0.13.7, Claude Code 2.1.319
(`@anthropic-ai/claude-code` depuis npm) et le SDK `mistralai` 3.7.0. Chaque essai d'un bloc a
listé neuf tools, trouvé `demo.crm.customers.email` avec `search_columns("email")`, et appelé
`describe_table` sur `demo.crm.customers`.

| Configuration | Mode d'exécution | Résultat |
|---|---|---|
| Claude Desktop, chemin 1 (`npx mcp-remote`) | stdio, le pont lancé par le MCP host, sous Node.js 22 | 9 tools ; la recherche répond ; `describe_table` porte le contexte du catalogue de données, `context_sources` `{"atlas": "ok"}` |
| Claude Code, chemin 1 | `claude mcp add --transport http`, puis `claude mcp list` et `claude mcp get` | `✔ Connected`, comme cité plus haut |
| Cursor, chemin 1 (`url`, `headers`) | streamable HTTP, token de Carol | comme Claude Desktop |
| VS Code, chemin 1 (`servers`, `inputs`) | streamable HTTP, la valeur de `inputs` remplie avec le token de Carol | idem |
| Claude Desktop et Cursor, chemin 2 (`uvx`, `oauth2`) | stdio, le Trino du sandbox avec un authentificateur OAuth2, un navigateur scripté qui se connecte à Keycloak | l'avertissement cité plus haut au démarrage ; 9 tools avant toute connexion ; le premier appel écrit l'URL de connexion, puis répond une fois connecté ; l'audit nomme `carol_analyst` |
| chemin 2 avec Docker | stdio, `docker run -i` | idem |
| Claude Desktop et Cursor, chemin 3 | HTTPS derrière un proxy TLS, une autorité de certification privée (`NODE_EXTRA_CA_CERTS` pour le pont, `SSL_CERT_FILE` pour le SDK) | 9 tools ; la recherche répond ; `describe_table` porte le contexte |
| l'entrée stdio (`MCP_USER_TOKEN`) | stdio, mode `jwt`, échange de token auprès de Keycloak | 9 tools ; la recherche répond ; `describe_table` rend les colonnes, sans contexte, car cette entrée ne configure aucun catalogue de données |
| Claude Code, l'entrée stdio | `claude mcp add ... --transport stdio trino -- uvx ...`, puis `claude mcp list` et `claude mcp get` | `✔ Connected` |
| VS Code, l'entrée stdio | par le SDK, les deux valeurs de `inputs` remplies | comme l'entrée stdio |
| Depuis Python | le bloc exécuté tel quel | la sortie citée plus haut |
| Depuis l'API Agents de Mistral | `mistral-medium-latest`, token de Carol | les tool calls et la réponse cités plus haut |
| Depuis un agent | `mistral-small-latest`, puis `mistral-medium-latest`, token de Carol | inachevé : `429 - Rate limit exceeded` |

Pour les essais sur le sandbox, seules les valeurs à remplacer ont changé : le token, le nom de
Trino dans le réseau du sandbox (`trino`), son catalog `demo`, les adresses de l'IdP et le
certificat de son autorité de démonstration (`TRINO_VERIFY`, et `TRINO_TOKEN_EXCHANGE_VERIFY` pour
les entrées stdio). La version épinglée `akko-mcp-trino==<version>` est devenue le wheel construit
depuis `main` (`uvx --from <wheel> akko-mcp-trino`), et l'image Docker a été remplacée par celle
construite depuis `main`. L'essai Docker avait en plus `--network` et un volume pour le certificat
de l'autorité. Le script de vérification a rempli les valeurs de `inputs` de VS Code depuis les
variables d'environnement `INPUT_TRINO_TOKEN` et `INPUT_TRINO_CLIENT_SECRET`. L'authentificateur
OAuth2 de Trino a été ajouté au sandbox pour ce seul essai : le sandbox lui-même authentifie par
JWT.

Plusieurs essais n'ont pas tourné sur `main` :

- poser une question à Claude Code, et son panneau `/mcp` : les deux demandent de se connecter à
  Anthropic, et nous ne l'avons pas fait ; les commandes de cette étape viennent de la
  documentation de Claude Code ;
- les interfaces de Claude Desktop, de Claude Code, de Cursor et de VS Code : nous ne les avons pas
  pilotées ;
- l'entrée `.mcp.json` avec `${TRINO_TOKEN}` au-delà de son approbation : `claude mcp list` la
  lit et affiche `⏸ Pending approval`, car Claude Code demande cette approbation dans une
  session interactive ; le remplacement de `${VAR}` vient de la documentation de Claude Code ;
- [`examples/agent.py`](../../examples/agent.py) jusqu'au bout : le fournisseur a répondu
  `429 - Rate limit exceeded` à chacun des cinq essais, trois avec `mistral-small-latest` et deux
  avec `mistral-medium-latest` ;
- le chemin 2 avec un MCP host qui abandonne pendant la connexion, et le pont sous Node.js 20.18.1 :
  les deux ont tourné les 1er et 2 novembre 2026, avec akko-mcp-trino 0.7.0 et le sandbox au
  commit `dac01db` ;
- les lignes du tableau d'erreurs : elles viennent de ce même essai de la 0.7.0, sauf les lignes
  sur le `PATH` et sur un ancien Trino, qui viennent de la documentation.

Les sources sont [Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
(le fichier de Claude Desktop, ses journaux, ses tools),
[Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom) (les
connecteurs distants de Claude), [Claude Code, MCP](https://code.claude.com/docs/en/mcp)
(`claude mcp`, ses portées, ses fichiers et le remplacement de `${VAR}` dans `.mcp.json`),
[Cursor, Model Context Protocol](https://cursor.com/docs/context/mcp) (les fichiers, clés et
variables de Cursor),
[VS Code, MCP configuration](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration)
(`servers`, `inputs` et les clés d'une entrée) et
[mcp-remote](https://github.com/punkpeye/mcp-remote) (le pont).
