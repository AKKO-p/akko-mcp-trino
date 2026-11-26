# Déployer sur OpenShift sans accès à internet

[English](../en/openshift-air-gapped.md) · **Français**

Cette page couvre le déploiement le plus contraint : un cluster OpenShift qui n'atteint ni index de
paquets ni registre public, un Trino qui authentifie les utilisateurs par mot de passe auprès d'un
annuaire (LDAP), en https avec un certificat d'une autorité de certification interne, et aucun
OAuth 2.0 côté Trino. Trino ne peut alors ni vérifier ni échanger un token, donc le serveur tourne
en mode `impersonation` : il se connecte à Trino avec son propre compte de l'annuaire et demande à
Trino d'exécuter chaque requête au nom de l'appelant vérifié. Les appelants présentent un token du
fournisseur d'identité, que le serveur vérifie par JWKS.

Les étapes :

1. construire l'image sur Red Hat Universal Base Image 9 (UBI 9), sur une machine qui accède à
   l'index de paquets, ou à l'intérieur sans aucun réseau ;
2. la transférer dans le registre interne ;
3. autoriser le compte du serveur, et lui seul, à agir au nom des appelants dans Trino ;
4. installer la chart avec le registre interne, le mot de passe du serveur dans un Secret,
   l'autorité de certification interne et une NetworkPolicy ;
5. vérifier la release avec `helm test`.

## Construire l'image UBI 9

[`Dockerfile.ubi9`](../../Dockerfile.ubi9) construit l'image sur deux images Red Hat, figées par
digest : `ubi9/python-312` pour l'étape de construction, qui apporte le compilateur et les en-têtes
du MIT Kerberos, et `ubi9/python-312-minimal` pour l'image elle-même. Il installe chaque paquet
Python depuis les verrous de `requirements/`, aux versions exactes et avec leurs empreintes, depuis
un dossier local de wheels seulement, et aucun paquet système : la construction ne lit aucun index
et tourne avec `--network=none`. L'image tourne sous l'UID 1001 ou sous n'importe quel UID du
groupe 0, avec un système de fichiers racine en lecture seule.

Sur une machine qui accède à l'index de paquets et dispose de Docker (ou de Podman, avec
`CONTAINER_ENGINE=podman`), depuis un clone du dépôt, écrivez le dossier de wheels, puis
construisez :

```bash
bash scripts/wheelhouse.sh
docker build --network=none -f Dockerfile.ubi9 -t akko-mcp-trino:ubi9 .
docker run --rm akko-mcp-trino:ubi9 akko-mcp-trino --version
```

```text
wheelhouse.sh: wheelhouse holds 60 wheels and 2 sdists for x86_64
akko-mcp-trino 0.8.2
```

`scripts/wheelhouse.sh` tourne dans l'image de base de l'étape de construction, pour que pip
choisisse les wheels de son Python et de la bibliothèque C de RHEL 9, et échoue sur une version
épinglée qui n'en a pas. `PLATFORM=linux/arm64` écrit le dossier de l'autre plateforme.

Pour construire à l'intérieur, copiez le dépôt avec son dossier `wheelhouse/`, recopiez les deux
images de base dans le registre interne par digest (avec `oc-mirror` ou `skopeo copy --all`), et
nommez-les à la construction :

```bash
docker build --network=none -f Dockerfile.ubi9 \
  --build-arg BUILD_BASE="$REGISTRY/ubi9/python-312@sha256:5a0471c98fc10017d011c16b7615cd1d7f157d796f89eb4900ca0dc6c87384cf" \
  --build-arg BASE="$REGISTRY/ubi9/python-312-minimal@sha256:3c3413652c3cf6aab00587926949ca8884f06b7d28dbbe69508e29aade34e2fe" \
  -t akko-mcp-trino:ubi9 .
```

Dans un projet OpenShift, une BuildConfig en stratégie Docker construit la même image depuis le
même dossier (`oc start-build --from-dir`), avec ces deux valeurs en `buildArgs` :
[`integration/helm/openshift-ldap.sh`](../../integration/helm/openshift-ldap.sh) en crée une.

Par rapport à l'image Debian, l'image UBI 9 embarque la glibc 2.34 et le MIT Kerberos 1.21.1
(RHEL 9) au lieu de la glibc 2.41 et du MIT Kerberos 1.21.3 (Debian 13), avec le même Python 3.12.16
et les mêmes paquets verrouillés. Elle pèse environ 110 Mo compressée (465 Mo décompressée),
contre 90 Mo (384 Mo).

## Transférer l'image dans le registre interne

Enregistrez l'image dans une archive sur la machine qui l'a construite :

```bash
docker save akko-mcp-trino:ubi9 | gzip > akko-mcp-trino-ubi9.tar.gz
```

À l'intérieur, chargez-la et poussez-la dans le registre interne. `REGISTRY` est le registre et son
projet, par exemple `registry.example.internal/data` :

```bash
gunzip -c akko-mcp-trino-ubi9.tar.gz | docker load
docker tag akko-mcp-trino:ubi9 "$REGISTRY/akko-mcp-trino:ubi9"
docker push "$REGISTRY/akko-mcp-trino:ubi9"
docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$REGISTRY/akko-mcp-trino:ubi9" | grep "^$REGISTRY/"
```

La dernière ligne affiche le digest de l'image dans le registre. La chart l'épingle
(`image.digest`).

## Autoriser le compte du serveur à agir au nom des appelants

Trino vérifie le mot de passe de chaque utilisateur par un bind sur l'annuaire
(`etc/password-authenticator.properties`) :

```properties
password-authenticator.name=ldap
ldap.url=ldaps://ldap.example.internal:636
ldap.ssl.truststore.path=/etc/trino/internal-ca.pem
ldap.user-bind-pattern=uid=${USER},ou=people,dc=example,dc=internal
```

Le serveur se connecte avec son propre compte, ici `mcp-server`, et envoie le nom de l'appelant
dans `X-Trino-User`. Avec le contrôle d'accès par fichier de Trino, une règle autorise ce compte,
et aucun autre, à exécuter des requêtes au nom des utilisateurs qu'il sert :

```json
{
  "impersonation": [
    {"original_user": "mcp-server", "new_user": "(alice|bob)", "allow": true}
  ]
}
```

Un motif étroit vaut mieux que `.*` : listez les utilisateurs, ou un motif de leurs noms. Avec
Apache Ranger, la même règle est une policy du service Trino sur la ressource `trinouser` (les
utilisateurs pour lesquels le serveur peut agir) qui accorde au compte du serveur la permission
`impersonate`. Ne donnez au compte du serveur aucun droit sur les données : chaque requête tourne
alors avec les seuls droits de l'appelant.

## Installer la chart

Créez le Secret qui contient le mot de passe du serveur, la ConfigMap de l'autorité de
certification interne et, si le registre l'exige, son secret de tirage :

```bash
read -rs TRINO_PASSWORD && export TRINO_PASSWORD
oc create secret generic mcp-trino-credentials --from-literal=trino-password="$TRINO_PASSWORD"
oc create configmap trusted-ca --from-file=ca-bundle.crt=internal-ca.pem
oc create secret docker-registry internal-registry --docker-server=registry.example.internal \
  --docker-username="$REGISTRY_USER" --docker-password="$REGISTRY_PASSWORD"
```

La chart monte le bundle pour Trino (`TRINO_VERIFY`) et, avec `ca.setSslCertFile` (actif par
défaut), pour la lecture du JWKS (`SSL_CERT_FILE`). Cette variable remplace le magasin du système :
le bundle doit contenir toutes les autorités dont le serveur a besoin, celle de Trino et celle du
fournisseur d'identité. Si le cluster dispose d'un bundle de confiance, posez sur la ConfigMap le
label `config.openshift.io/inject-trusted-cabundle=true` au lieu de la remplir : OpenShift y écrit
ce bundle, autorités internes comprises, sous `ca-bundle.crt`.

Les valeurs, dans `my-values.yaml` (les contrôles de la chart valident et rendent le même fichier,
[`ci/airgapped-values.yaml`](../../charts/akko-mcp-trino/ci/airgapped-values.yaml)) :

```yaml
# OpenShift without internet access: the UBI 9 image from an internal registry, by digest,
# with its pull secret; Trino in the same project with password authentication, the server's
# own account in impersonation mode, its password in an existing Secret; the internal
# authority from a ConfigMap the cluster fills with its trusted bundle, for Trino and for the
# JWKS; the NetworkPolicy: the router in, egress to DNS, Trino in the project and the identity
# provider only.
openshift: true
image:
  repository: registry.example.internal/data/akko-mcp-trino
  digest: sha256:0000000000000000000000000000000000000000000000000000000000000000
imagePullSecrets:
  - name: internal-registry
trino:
  host: trino
  port: 8443
  catalog: lakehouse
  identityMode: impersonation
  user: mcp-server
  credential: password
auth:
  jwksUrl: https://idp.example.internal/realms/data/protocol/openid-connect/certs
  issuer: https://idp.example.internal/realms/data
  audience: mcp-trino
  resourceUrl: https://mcp.apps.example.internal/mcp
  userClaim: preferred_username
existingSecret:
  name: mcp-trino-credentials
ca:
  # A ConfigMap labelled config.openshift.io/inject-trusted-cabundle=true: the cluster writes
  # its trusted bundle, the internal authorities included, under ca-bundle.crt.
  configMap: trusted-ca
  key: ca-bundle.crt
route:
  enabled: true
  host: mcp.apps.example.internal
networkPolicy:
  enabled: true
  egress:
    trino:
      - podSelector:
          matchLabels:
            app.kubernetes.io/name: trino
            app.kubernetes.io/component: coordinator
    identityProvider:
      to:
        - ipBlock:
            cidr: 192.0.2.10/32
      ports:
        - port: 443
          protocol: TCP
```

Donnez à `image.digest` le digest affiché par le push, à `userClaim` le claim qui porte l'uid de
l'annuaire (le nom que Trino connaît), et aux pairs de la NetworkPolicy ceux du cluster. Un
`podSelector` sans `namespaceSelector` désigne des pods du projet de la release. Avec la
NetworkPolicy active, le serveur atteint le DNS du cluster, Trino et le fournisseur d'identité, et
rien d'autre. Le pod de `helm test` a sa propre policy, pour passer dans un projet qui refuse les
sorties par défaut.

Rendez-la d'abord : `helm template` échoue sur une valeur que le serveur refuserait au démarrage.

```bash
helm template mcp-trino ./charts/akko-mcp-trino -f my-values.yaml | grep -E 'image:|TRINO_VERIFY|SSL_CERT_FILE'
```

```text
  TRINO_VERIFY: "/etc/mcp-trino/ca/ca-bundle.crt"
  SSL_CERT_FILE: "/etc/mcp-trino/ca/ca-bundle.crt"
          image: "registry.example.internal/data/akko-mcp-trino@sha256:0000000000000000000000000000000000000000000000000000000000000000"
      image: "registry.example.internal/data/akko-mcp-trino@sha256:0000000000000000000000000000000000000000000000000000000000000000"
```

## Vérifier avec helm test

```bash
helm install mcp-trino ./charts/akko-mcp-trino -f my-values.yaml
helm test mcp-trino --logs
```

`helm test` appelle `/health` et `/ready` par le Service de santé, puis lance
`akko-mcp-trino --check` avec les réglages, le Secret et les montages de la release. `/ready`
répond dès que le compte du serveur a exécuté un `SELECT 1` borné dans Trino : le mot de passe,
l'autorité de certification et la NetworkPolicy sont alors justes. La première requête d'un
appelant montre ensuite la règle d'impersonation : `SELECT current_user` renvoie l'uid de
l'appelant, jamais `mcp-server`.

## Ce que le dépôt prouve

[`integration/ldap/run.sh`](../../integration/ldap/run.sh) prouve ce cas avec Docker seul : un
annuaire de trois comptes, Keycloak fédéré sur cet annuaire, Trino en https avec authentification
par mot de passe auprès de lui et la règle d'impersonation ci-dessus, et le serveur dans l'image
UBI 9, construite sans réseau et lancée sous l'UID 1234567 du groupe 0, avec un système de fichiers
racine en lecture seule. Une exécution réelle :

```text
  OK    without a token, the server answers 401 and says what to send
  OK    a token signed by a key the identity provider never had is refused with 401
  OK    Trino runs alice's query as alice (SELECT current_user)
  OK    Trino runs bob's query as bob (SELECT current_user)
  OK    bob reads the 5 nations his row filter leaves
  OK    alice's token with X-Trino-User: bob sent to the server: Trino still sees alice
  OK    the server's account alone reads no data: (200, 'Access Denied: Cannot select from table tpch.tiny.nation')
  OK    alice, signed in to Trino with a directory password, may not run a query as bob: (200, 'Access Denied: User alice cannot impersonate user bob')
  25 of 25 checks passed
  OK    no token in the server's log
  OK    no password in the server's log
```

[`integration/helm/openshift-ldap.sh`](../../integration/helm/openshift-ldap.sh) lance les mêmes
contrôles dans un projet OpenShift avec les droits d'un membre : l'image construite par une
BuildConfig depuis `Dockerfile.ubi9`, la chart installée avec
[`values-ldap.yaml`](../../integration/helm/values-ldap.yaml), le pod sous `restricted-v2`, et
`helm test`.
