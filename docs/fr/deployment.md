# Déploiement

[English](../en/deployment.md) · **Français**

## Installer

```bash
pip install akko-mcp-trino        # ou : pipx install akko-mcp-trino
uvx akko-mcp-trino --version      # sans installation, comme le font la plupart des MCP hosts
```

On peut aussi installer depuis les sources :

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Le paquet installe un module Python (`akko_mcp_trino`) et une commande (`akko-mcp-trino`).
`python -m akko_mcp_trino` lance le même point d'entrée.

## Lancer

Pointez le serveur sur votre Trino et votre identity provider, puis lancez-le. Les valeurs par
défaut sont les plus sûres : chaque requête s'exécute au nom de l'appelant (`jwt`), et le serveur
refuse une requête sans token valide.

```bash
export TRINO_HOST=trino.example.internal
export TRINO_PORT=8443
export TRINO_HTTP_SCHEME=https          # obligatoire : un token ne circule jamais en http
export TRINO_CATALOG=iceberg

# Vérification du token : des tokens émis pour ce serveur, vérifiés avec le JWKS de l'IdP
export MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs
export MCP_OIDC_ISSUER=https://idp.example.com/realms/data
export MCP_OIDC_AUDIENCE=akko-mcp        # l'audience des tokens émis pour ce serveur
export MCP_RESOURCE_URL=https://mcp.example.com/mcp   # facultatif : l'URL canonique de ce serveur
export MCP_JWT_USER_CLAIM=preferred_username          # Keycloak ; sub par défaut

# Un token pour Trino, par token exchange (RFC 8693), jamais celui de l'appelant
export TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token
export TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp
read -rs TRINO_TOKEN_EXCHANGE_CLIENT_SECRET && export TRINO_TOKEN_EXCHANGE_CLIENT_SECRET
export TRINO_TOKEN_EXCHANGE_AUDIENCE=trino

python -m akko_mcp_trino
```

`read -rs` lit le client secret sans l'afficher : il reste hors de l'historique du shell.
[Identité](identity.md) décrit les autres modes d'identité. Avant de lancer le serveur,
`akko-mcp-trino --check` affiche la configuration effective (jamais un secret) et chaque
avertissement. Il sort avec le code 2 si un réglage empêcherait le serveur de démarrer. En stdio,
il vérifie que `MCP_USER_TOKEN` est posé, pas qu'il est valide ; le vrai démarrage vérifie le
token. Une fois lancé, le serveur écrit :

```
INFO:__main__:serving transport=streamable-http port=3000 auth=True strict=True identity=jwt extensions=none
```

Le nom du logger dépend de la façon dont on lance le serveur : `__main__` avec
`python -m akko_mcp_trino`, `akko_mcp_trino.__main__` avec la commande `akko-mcp-trino`.

Vérifiez qu'il répond et qu'il joint Trino :

```bash
curl -s localhost:3001/health   # {"status":"ok"}       ne touche jamais Trino
curl -s localhost:3001/ready    # {"status":"ready"}    jwt : /v1/info de Trino ; impersonation et service : un SELECT 1 borné sous le compte du serveur
curl -s localhost:3001/metrics  # métriques Prometheus
```

Quand Trino ne répond pas, `/ready` rend un `503` avec `{"status": "error", "detail": ...}`, le
détail étant l'erreur qu'a levée le contrôle.

## Transports

`streamable-http` (`/mcp`) est le transport par défaut, celui qu'attendent les MCP hosts actuels.
Posez `MCP_TRANSPORT=sse` pour les MCP hosts plus anciens (`/sse`) ; la vérification du token est
la même sur les deux. Avec `MCP_TRANSPORT=stdio`, le MCP host lance lui-même le serveur : voir
[Tout MCP host en stdio, avec votre propre token](hosts.md#tout-mcp-host-en-stdio-avec-votre-propre-token).

## Conteneur

Un [Dockerfile](../../Dockerfile) est livré avec le dépôt : il tourne sans root, avec un contrôle
de santé et des étiquettes OCI. L'image est publiée pour `linux/amd64` et `linux/arm64` sous
`akkoai/akko-mcp-trino` et `ghcr.io/akko-p/akko-mcp-trino`. Elle installe chaque dépendance depuis
le verrou `requirements/image.txt`, en version exacte et avec ses empreintes (voir
[CONTRIBUTING.fr.md](../../CONTRIBUTING.fr.md#dépendances-et-verrous)).

Chaque utilisateur avec ses propres droits (`jwt`, par défaut), Trino faisant confiance au même
identity provider :

```bash
read -rs TRINO_TOKEN_EXCHANGE_CLIENT_SECRET && export TRINO_TOKEN_EXCHANGE_CLIENT_SECRET
docker run -d --name akko-mcp-trino -p 3000:3000 -p 3001:3001 \
  -e TRINO_HOST=trino.example.internal -e TRINO_PORT=8443 -e TRINO_HTTP_SCHEME=https \
  -e TRINO_CATALOG=iceberg \
  -e MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs \
  -e MCP_OIDC_ISSUER=https://idp.example.com/realms/data \
  -e MCP_OIDC_AUDIENCE=akko-mcp -e MCP_JWT_USER_CLAIM=preferred_username \
  -e TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token \
  -e TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp -e TRINO_TOKEN_EXCHANGE_CLIENT_SECRET \
  -e TRINO_TOKEN_EXCHANGE_AUDIENCE=trino \
  akkoai/akko-mcp-trino:0.8
```

Sans token exchange, utilisez `-e TRINO_IDENTITY_MODE=impersonation -e TRINO_USER=mcp-service`,
avec un mot de passe ou un certificat client pour ce compte. Pour une identité unique pour tous
les appelants, lisez le mot de passe avec `read -rs TRINO_PASSWORD && export TRINO_PASSWORD`, puis
utilisez `-e TRINO_IDENTITY_MODE=service -e TRINO_USER=mcp-reader -e TRINO_PASSWORD`. Un `-e` sans
valeur passe la variable de votre shell : aucun secret n'est tapé dans la ligne de commande, où
l'historique du shell le garderait.

L'endpoint MCP est `http://localhost:3000/mcp` (streamable HTTP). Le MCP host envoie le token
d'accès de l'utilisateur dans `Authorization: Bearer <token>`. Vérifiez ensuite le serveur :

```bash
curl -s localhost:3001/health   # {"status":"ok"}
curl -s localhost:3001/ready    # {"status":"ready"} dès que Trino répond
docker run --rm akkoai/akko-mcp-trino:0.8 akko-mcp-trino --version
```

`--check` marche aussi dans un conteneur :

```bash
docker run --rm -e TRINO_HOST=... -e TRINO_HTTP_SCHEME=https -e MCP_JWKS_URL=... \
  akkoai/akko-mcp-trino:0.8 akko-mcp-trino --check
```

## Kubernetes et OpenShift

Une chart Helm est livrée dans [`charts/akko-mcp-trino/`](../../charts/akko-mcp-trino/). Par
défaut, elle lance le serveur avec la vérification du token active et le mode `jwt`. Le pod tourne
sans root, avec un système de fichiers racine en lecture seule, et tourne aussi sous l'UID
arbitraire d'OpenShift. Les secrets sont lus dans un Secret existant. La chart peut aussi ajouter
une autorité de certification interne, un Ingress, une Route OpenShift, une NetworkPolicy et un
PodDisruptionBudget.

`helm template` échoue sur un réglage que le serveur refuserait au démarrage, et sur une
vérification du token coupée sans accord explicite. `helm test` lance `--check` avec la
configuration de la release :

```bash
helm install mcp-trino ./charts/akko-mcp-trino -f my-values.yaml
helm test mcp-trino --logs
```

Le [README de la chart](../../charts/akko-mcp-trino/README.md) (en anglais) en liste les valeurs.

## Derrière Trino Gateway

La passerelle termine TLS, donc un certificat client n'atteint pas le cluster. Derrière la
passerelle, le compte du serveur en mode `impersonation` s'authentifie par mot de passe
(`TRINO_PASSWORD`, ou `trino.credential: password` dans la chart), que la passerelle transmet. La
section [Behind Trino Gateway](../../charts/akko-mcp-trino/README.md#behind-trino-gateway) de la
chart et [`ci/gateway-values.yaml`](../../charts/akko-mcp-trino/ci/gateway-values.yaml) en donnent
un exemple.

Le [sandbox](https://github.com/AKKO-p/akko-sandbox) a une variante avec Trino Gateway devant deux
clusters Trino. Le serveur MCP ne joint alors que la passerelle : chaque requête, chaque demande de
page suivante et chaque annulation passent par elle. Le sandbox la lance ainsi :

```bash
COMPOSE_FILE=compose.yaml:gateway/compose.gateway.yaml docker compose up -d --wait
COMPOSE_FILE=compose.yaml:gateway/compose.gateway.yaml ./demo.sh --check
COMPOSE_FILE=compose.yaml:gateway/compose.gateway.yaml docker compose --profile tools down -v
tests/gateway.sh   # les contrôles, de bout en bout
```

Lancez la variante avec le même `COMPOSE_FILE` à chaque fois, et arrêtez-la avec lui aussi : un
`docker compose down` sans lui laisse derrière lui le second cluster et la passerelle.

`tests/gateway.sh` vérifie que chaque cluster exécute les requêtes au nom de l'utilisateur qui les
a posées, qu'un résultat plafonné passe par la passerelle, et qu'une requête qui dépasse son budget
de temps est annulée dans Trino à travers la passerelle. Dans ce sandbox, chaque cluster fait
confiance à `X-Forwarded-Proto` venant de n'importe quel pair de son réseau. En production, rien
d'autre que la passerelle ne doit joindre un cluster.

Avec un fournisseur de contexte, les requêtes du filtre de visibilité et celles de l'appel peuvent
partir vers deux clusters d'un même groupe de routage. Tous les clusters d'un groupe doivent donc
partager le même contrôle d'accès : le même plugin et les mêmes règles. Un groupe de routage dont
les clusters appliquent des règles différentes n'est pas pris en charge.

## Avec un catalogue de données

Le serveur peut tourner à côté d'un catalogue de données de deux façons.

**Mode 1, Trino seul.** Le serveur tourne sans fournisseur de contexte. Une équipe qui fait déjà
tourner le serveur MCP de son catalogue de données le garde, à côté : rien dans ce serveur ne le
bloque ni ne le remplace. Ce mode convient quand les métadonnées du catalogue de données ne sont
pas sensibles.

**Mode 2, avec un fournisseur de contexte.** Le sens métier (descriptions, termes de glossaire,
classifications, propriétaire) n'est montré que pour les tables et les colonnes que Trino liste à
l'appelant. Ce mode convient quand les métadonnées doivent suivre les droits sur les données.

**Droits.** Le contexte suit les droits de Trino. Le fournisseur lit le catalogue de données avec
un compte technique en lecture seule. Les descriptions suivent ce que Trino liste à l'appelant.
Avec `MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table` demande en plus à Trino de confirmer que
l'appelant peut faire un SELECT des colonnes qu'il liste. `describe_schema` continue de servir les
descriptions des tables que Trino liste. Le fournisseur ne sert les termes de glossaire, les
classifications et les métadonnées métier que si leur glossaire, leur classification ou leur
ensemble est nommé dans une liste blanche. Les listes blanches sont vides par défaut. Le
propriétaire n'est servi que s'il est configuré (pour Atlas : `ATLAS_GLOSSARIES`,
`ATLAS_CLASSIFICATIONS`, `ATLAS_BUSINESS_METADATA`, vide veut dire aucun, et
`ATLAS_OWNER_ATTRIBUTE`). Un glossaire ou une classification restreints ne sont donc jamais servis,
sauf si l'administrateur les nomme. Cette version ne sait pas lire le catalogue de données sous
l'identité de l'appelant.

Sans fournisseur, aucun code de catalogue de données n'est chargé et aucun catalogue de données
n'est appelé. [Contexte d'un catalogue de données](catalog-context.md) décrit ce que sert un
fournisseur.

### Sur Kubernetes et OpenShift

La chart n'a pas de valeur à elle pour le contexte : ses variables vont dans `extraEnv`, un secret
seulement par `valueFrom`, et le fichier JSON du fournisseur `file` dans `extraVolumes` et
`extraVolumeMounts`. L'image porte le fournisseur Atlas depuis la 0.8.0, inactif, avec les
bibliothèques Kerberos de son extra `kerberos`. Pour Atlas avec une authentification Basic :

```yaml
extraEnv:
  - name: MCP_CONTEXT_PROVIDERS
    value: atlas
  - name: MCP_CONTEXT_CANARY
    value: <catalog.schema.table>   # a table no caller may see
  - name: ATLAS_URL
    value: https://atlas.example.internal
  - name: ATLAS_CLUSTERS
    value: '{"lake": "primary"}'
  - name: ATLAS_USER
    value: mcp-reader
  - name: ATLAS_PASSWORD
    valueFrom:
      secretKeyRef:
        name: mcp-trino-atlas
        key: password
```

Pour le fournisseur `file` :

```yaml
extraEnv:
  - name: MCP_CONTEXT_PROVIDERS
    value: file
  - name: MCP_CONTEXT_FILE
    value: /etc/mcp-trino/context/context.json
extraVolumes:
  - name: context
    configMap:
      name: mcp-trino-context
extraVolumeMounts:
  - name: context
    mountPath: /etc/mcp-trino/context
    readOnly: true
```

## Avec ou sans modules

Le cœur livre trois modules, `token-policy`, `fingerprint-audit` et `trino-tags`. Chacun reste
désactivé tant que `MCP_EXTENSIONS` ne le nomme pas (séparés par des virgules). Sans eux, le
serveur tourne comme décrit sur cette page. [Modules](modules.md) dit ce que chacun ajoute.

### Sur Kubernetes et OpenShift

La chart nomme les modules dans `server.extensions`. Leurs propres réglages vont dans `extraEnv` :

```yaml
server:
  extensions: [token-policy, fingerprint-audit]
extraEnv:
  - name: MCP_ALLOWED_CLIENTS
    value: mcp-host
  - name: MCP_REQUIRED_SCOPES
    value: data:query
```
