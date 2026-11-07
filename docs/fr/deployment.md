# Déploiement

[English](../en/deployment.md) · **Français**

## Installer

```bash
pip install akko-mcp-trino        # ou : pipx install akko-mcp-trino
uvx akko-mcp-trino --version      # sans installation, comme le font la plupart des MCP hosts
```

On peut aussi installer depuis les sources :

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Le paquet installe un module Python (`akko_mcp_trino`) et une commande (`akko-mcp-trino`) ;
`python -m akko_mcp_trino` est le même point d'entrée.

## Lancer

Pointez-le sur votre Trino et votre IdP, puis servez. Les valeurs par défaut sont les plus sûres :
chaque requête s'exécute sous le nom de l'appelant (`jwt`), et une requête sans token valide est
refusée.

```bash
export TRINO_HOST=trino.example.internal
export TRINO_PORT=8443
export TRINO_HTTP_SCHEME=https          # obligatoire : un token ne circule jamais en http
export TRINO_CATALOG=iceberg

# La porte : des tokens émis pour ce serveur, vérifiés avec le JWKS de l'IdP
export MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs
export MCP_OIDC_ISSUER=https://idp.example.com/realms/data
export MCP_OIDC_AUDIENCE=akko-mcp        # l'audience des tokens émis pour ce serveur
export MCP_RESOURCE_URL=https://mcp.example.com/mcp   # facultatif : l'URL canonique de ce serveur

# Un token pour Trino, par échange (RFC 8693), jamais celui de l'appelant
export TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token
export TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp
export TRINO_TOKEN_EXCHANGE_CLIENT_SECRET=...
export TRINO_TOKEN_EXCHANGE_AUDIENCE=trino

python -m akko_mcp_trino
```

Les autres modes d'identité sont dans [Identité](identity.md). Avant de servir,
`akko-mcp-trino --check` affiche la configuration effective (jamais un secret) et chaque
avertissement, et sort avec le code 2 si un réglage empêcherait le démarrage ; en stdio, il
vérifie que `MCP_USER_TOKEN` est présent, pas qu'il est valide (le vrai démarrage vérifie le
token). Le serveur écrit ensuite :

```
INFO:__main__:serving transport=streamable-http port=3000 auth=True strict=True identity=jwt extensions=none
```

Vérifiez qu'il vit et qu'il joint Trino :

```bash
curl -s localhost:3001/health   # {"status":"ok"}       ne touche jamais Trino
curl -s localhost:3001/ready    # {"status":"ready"}    jwt : /v1/info de Trino ; impersonation et service : un SELECT 1 borné sous le compte du serveur
curl -s localhost:3001/metrics  # métriques Prometheus
```

## Transports

`streamable-http` (`/mcp`) est le transport par défaut, celui qu'attendent les MCP hosts
actuels. Posez `MCP_TRANSPORT=sse` pour les MCP hosts plus anciens (`/sse`) ; la garde est la même
sur les deux. Avec `MCP_TRANSPORT=stdio`, le MCP host lance lui-même le serveur : voir
[MCP hosts locaux en stdio](hosts.md#mcp-hosts-locaux-en-stdio).

## Conteneur

Un [Dockerfile](../../Dockerfile) est livré avec le dépôt (sans root, contrôle de santé,
étiquettes OCI). L'image est publiée pour `linux/amd64` et `linux/arm64` sous
`akkoai/akko-mcp-trino` et `ghcr.io/akko-p/akko-mcp-trino`. Elle installe chaque dépendance
depuis le verrou `requirements/image.txt`, en version exacte et avec ses empreintes (voir
[CONTRIBUTING.fr.md](../../CONTRIBUTING.fr.md#dépendances-et-verrous)). Elle prend les mêmes
variables d'environnement, sur les ports `3000` (MCP) et `3001` (santé) :

```bash
docker run --rm akkoai/akko-mcp-trino:0.7 akko-mcp-trino --version
```

## Kubernetes et OpenShift

Une chart Helm est livrée dans [`charts/akko-mcp-trino/`](../../charts/akko-mcp-trino/) : le
serveur avec la porte active et le mode `jwt` par défaut, un pod sans root au système de fichiers
en lecture seule qui tourne aussi sous l'UID arbitraire d'OpenShift, des secrets lus dans un
Secret existant, une autorité de certification interne, et en option un Ingress, une Route
OpenShift, une NetworkPolicy et un PodDisruptionBudget. `helm template` refuse un réglage que le
serveur refuserait au démarrage, ou une porte coupée sans accord explicite, et `helm test` lance
`--check` avec la configuration de la release :

```bash
helm install mcp-trino ./charts/akko-mcp-trino -f my-values.yaml
helm test mcp-trino --logs
```

Le [README de la chart](../../charts/akko-mcp-trino/README.md) (en anglais) en liste les valeurs.

## Derrière Trino Gateway

Le [sandbox](https://github.com/AKKO-p/akko-sandbox) a une variante avec Trino Gateway devant deux
clusters Trino. Le serveur MCP ne joint alors que la passerelle : chaque requête, chaque page
suivante et chaque annulation passent par elle. Son `tests/gateway.sh` vérifie que chaque cluster
exécute les requêtes sous le nom de l'utilisateur qui les a posées, qu'un résultat plafonné passe
par la passerelle, et qu'une requête qui dépasse son budget de temps est annulée dans Trino à
travers la passerelle.

Avec un fournisseur de contexte, les requêtes du filtre et celles de l'appel peuvent partir vers
deux clusters d'un même groupe de routage : tous les clusters d'un groupe doivent partager le même
contrôle d'accès (le même plugin et les mêmes règles). Un groupe de routage dont les clusters
appliquent des règles différentes n'est pas pris en charge.

## Déploiement avec un catalogue

Le serveur fonctionne avec un catalogue de données selon l'un de deux modes.

**Mode 1, Trino seul.** Le serveur tourne sans fournisseur de contexte. Une équipe qui fait
déjà tourner le serveur MCP de son catalogue le garde, à côté : rien dans ce serveur ne le bloque
ni ne le remplace. Ce mode convient quand les métadonnées du catalogue ne sont pas sensibles.

**Mode 2, avec un fournisseur de contexte.** Le sens métier (descriptions, termes de glossaire,
classifications, propriétaire) n'est montré que pour les tables et les colonnes que Trino liste à
l'appelant. Ce mode convient quand les métadonnées doivent suivre les droits sur les
données.

**Droits.** Le contexte suit les droits de Trino. Le fournisseur lit le catalogue avec un compte
technique en lecture seule. Les descriptions suivent ce que Trino liste à l'appelant. Avec
`MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table` exige en plus que Trino valide un SELECT par
l'appelant des colonnes qu'il liste, tandis que `describe_schema` continue de servir les
descriptions des tables que Trino liste. Il ne sert les termes de glossaire, les classifications
et les métadonnées métier que si leur glossaire, leur classification ou leur ensemble est nommé
dans les listes blanches, fermées par défaut, et le propriétaire seulement s'il est configuré
(pour Atlas : `ATLAS_GLOSSARIES`, `ATLAS_CLASSIFICATIONS`, `ATLAS_BUSINESS_METADATA`, vide veut
dire aucun, et `ATLAS_OWNER_ATTRIBUTE`). Un glossaire ou une classification restreints dans le
catalogue ne sont donc jamais servis, sauf si l'administrateur les nomme. La lecture du
catalogue sous l'identité de l'appelant n'est pas dans cette version.

Sans fournisseur, aucun code de catalogue n'est chargé et aucun catalogue n'est appelé.
[Contexte d'un catalogue](catalog-context.md) décrit ce que sert un fournisseur.

## Avec ou sans modules

Le cœur livre trois modules, `token-policy`, `fingerprint-audit` et `trino-tags`, chacun
désactivé tant que `MCP_EXTENSIONS` ne le nomme pas (séparés par des virgules). Sans eux, le
serveur tourne comme décrit ici ; [Modules](modules.md) dit ce que chacun ajoute.
