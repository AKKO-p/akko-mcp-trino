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
akko-mcp-trino 0.8.3
```

`scripts/wheelhouse.sh` tourne dans l'image de base de l'étape de construction, pour que pip
choisisse les wheels de son Python et de la bibliothèque C de RHEL 9, et échoue sur une version
épinglée qui n'en a pas. Le dossier de wheels et l'image sont ceux de la plateforme de la machine ;
`linux/amd64` est celle qui est construite et vérifiée.

Pour construire à l'intérieur, copiez le dépôt avec son dossier `wheelhouse/`, et recopiez les deux
images de base, par digest, dans un registre que la construction atteint. `MIRROR` est ce registre
et son chemin, par exemple `registry.example.internal/mirror` :

```bash
skopeo copy --all docker://registry.access.redhat.com/ubi9/python-312@sha256:5a0471c98fc10017d011c16b7615cd1d7f157d796f89eb4900ca0dc6c87384cf docker://$MIRROR/ubi9/python-312:1-1795522683
skopeo copy --all docker://registry.access.redhat.com/ubi9/python-312-minimal@sha256:3c3413652c3cf6aab00587926949ca8884f06b7d28dbbe69508e29aade34e2fe docker://$MIRROR/ubi9/python-312-minimal:9.9-1795231781
docker build --network=none -f Dockerfile.ubi9 \
  --build-arg BUILD_BASE="$MIRROR/ubi9/python-312@sha256:5a0471c98fc10017d011c16b7615cd1d7f157d796f89eb4900ca0dc6c87384cf" \
  --build-arg BASE="$MIRROR/ubi9/python-312-minimal@sha256:3c3413652c3cf6aab00587926949ca8884f06b7d28dbbe69508e29aade34e2fe" \
  -t akko-mcp-trino:ubi9 .
```

`skopeo copy --all` garde toutes les plateformes de l'image : les digests restent ceux de
`Dockerfile.ubi9`.

Dans un projet OpenShift, une BuildConfig en stratégie Docker peut construire la même image, avec
ces deux valeurs en `buildArgs`. [`integration/helm/openshift-ldap.sh`](../../integration/helm/openshift-ldap.sh)
en crée une et lui envoie, par `oc start-build --from-dir`, un dossier qui ne contient que les
fichiers que copie `Dockerfile.ubi9` et le dossier de wheels, pas le clone entier. Ses conditions :

- la construction tire les images de base : nommez le secret de tirage du registre dans
  `dockerStrategy.pullSecret`, sauf si le cluster redirige `registry.access.redhat.com` (un
  `ImageDigestMirrorSet`, que pose un administrateur) ;
- les utilisateurs du projet ont droit à la stratégie Docker (`system:build-strategy-docker`), que
  les clusters durcis retirent souvent ;
- le constructeur du cluster accepte `RUN --mount=type=bind` depuis une autre étape et depuis le
  contexte. OpenShift 4.22 l'accepte : la construction d'un contexte de 27 Mo (dont 26 Mo de wheels)
  y a pris 256 s et a poussé l'image dans le registre du projet ; le pod de construction a
  utilisé au plus 274 Mio, sous une limite de 2 Gio. La documentation d'OpenShift ne dit pas
  depuis quelle version ; sur un cluster plus ancien, essayez-le.

Si l'une d'elles manque, construisez l'image ailleurs et poussez-la, comme le fait la section
suivante.

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

Le port HTTP de Trino ne contourne pas les mots de passe : Trino 482 répond à une requête envoyée
sur ce port avec `X-Trino-User: alice` et sans mot de passe par
`403 Forbidden: Authentication over HTTP is not enabled`, tant que
`http-server.authentication.allow-insecure-over-http` reste à `false`, sa valeur par défaut. Si
d'autres pods partagent le namespace, fermez tout de même ce port (`http-server.http.enabled=false`,
avec `discovery.uri` en https et `node.internal-address` un nom de son certificat) ou filtrez-le, comme le fait
[`integration/ldap/trino/config.properties`](../../integration/ldap/trino/config.properties).

## Installer la chart

Créez le Secret qui contient le mot de passe du serveur, la ConfigMap de l'autorité de
certification interne et, si le registre l'exige, son secret de tirage :

```bash
read -rs TRINO_PASSWORD
printf %s "$TRINO_PASSWORD" | oc create secret generic mcp-trino-credentials --from-file=trino-password=/dev/stdin
oc create configmap trusted-ca --from-file=ca-bundle.crt=internal-ca.pem
read -rs REGISTRY_PASSWORD
printf '{"auths":{"registry.example.internal":{"auth":"%s"}}}' "$(printf %s:%s "$REGISTRY_USER" "$REGISTRY_PASSWORD" | base64 | tr -d '\n')" \
  | oc create secret generic internal-registry --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson=/dev/stdin
```

Les mots de passe arrivent à `oc` par son entrée standard, jamais en argument : ni l'historique du
shell ni la liste des processus ne les montrent.

La chart monte le bundle pour Trino (`TRINO_VERIFY`) et, avec `ca.setSslCertFile` (actif par
défaut), pour la lecture du JWKS (`SSL_CERT_FILE`). Cette variable remplace le magasin du système :
le bundle doit contenir toutes les autorités dont le serveur a besoin, celle de Trino et celle du
fournisseur d'identité. Si un administrateur a donné au cluster un bundle de confiance
(`spec.trustedCA` de `proxy/cluster`), une ConfigMap vide portant le label
`config.openshift.io/inject-trusted-cabundle=true` peut remplacer la deuxième commande : OpenShift
y écrit ce bundle, avec les autorités du système, sous `ca-bundle.crt`. Sans ce réglage, rien n'est
écrit, et la ConfigMap doit venir du fichier PEM.

Les valeurs, dans `my-values.yaml` (les contrôles de la chart valident et rendent le même fichier,
[`ci/airgapped-values.yaml`](../../charts/akko-mcp-trino/ci/airgapped-values.yaml)) :

```yaml
# OpenShift without internet access: the UBI 9 image from an internal registry, by digest,
# with its pull secret; Trino in the same project with password authentication, the server's
# own account in impersonation mode, its password in an existing Secret; the internal
# authority from a ConfigMap, for Trino and for the JWKS; the NetworkPolicy: the router in,
# egress to DNS, Trino in the project and the identity provider only.
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
  # The internal authorities, Trino's and the identity provider's, in one PEM bundle.
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

`imagePullSecrets` remplace les secrets de tirage qu'OpenShift donne au compte de service du pod :
pour une image du registre du projet lui-même (celle qu'a construite une BuildConfig), ne le posez
pas, sinon le tirage échoue (`ImagePullBackOff`, constaté sur OpenShift 4.22). Donnez à
`image.digest` le digest affiché par le push, à `userClaim` le claim qui porte l'uid de
l'annuaire (le nom que Trino connaît), et aux pairs de la NetworkPolicy ceux du cluster. Un
`podSelector` sans `namespaceSelector` désigne des pods du projet de la release, et le port d'une
règle est celui du pod, pas celui du Service. Un `ipBlock` correspond à l'adresse vers laquelle
partent les paquets : si le fournisseur d'identité est atteint par le routeur du cluster ou par un
proxy de sortie, nommez plutôt ceux-ci. Avec la NetworkPolicy active, le serveur atteint le DNS du
cluster, Trino et le fournisseur d'identité, et rien d'autre. Le pod de `helm test` a sa propre
policy, pour passer dans un projet qui refuse les sorties par défaut.

Sur Kubernetes sans OpenShift (`openshift: false`), la chart lance le pod sous l'UID et le groupe
10001, l'utilisateur de l'image Debian, et non 1001, celui de l'image UBI 9. Le serveur fonctionne
ainsi, puisqu'il n'écrit que dans `/tmp`.

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
racine en lecture seule. Un extrait d'une exécution réelle :

```text
  OK    without a token, the server answers 401 and says what to send
  OK    a token with the identity provider's key id, signed by another key is refused with 401: (401, '{"error":"unauthenticated","message":"the bearer token is invalid, expired, or not issued for this server: ask the identity provider for a token whose audience is this server"}')
  OK    Trino runs alice's query as alice (SELECT current_user)
  OK    Trino runs bob's query as bob (SELECT current_user)
  OK    bob reads the 5 nations his row filter leaves
  OK    alice's token with X-Trino-User: bob sent to the server: Trino still sees alice
  OK    the server's account alone reads no data: (200, 'Access Denied: Cannot select from table tpch.tiny.nation')
  OK    alice, signed in to Trino with a directory password, may not run a query as bob: (200, 'Access Denied: User alice cannot impersonate user bob')
  OK    Trino's HTTP port runs nothing as alice without her password: no listener: [Errno 111] Connection refused
  26 of 26 checks passed
  OK    no token in the server's log
  OK    no password in the server's log
  OK    the forged token is refused for its signature (InvalidSignatureError)
```

[`integration/helm/openshift-ldap.sh`](../../integration/helm/openshift-ldap.sh) lance les mêmes
contrôles dans un projet OpenShift avec les droits d'un membre : l'image construite par une
BuildConfig depuis `Dockerfile.ubi9`, la chart installée avec
[`values-ldap.yaml`](../../integration/helm/values-ldap.yaml), le pod sous `restricted-v2`, et
`helm test`. Sur OpenShift 4.22, dans un projet sans droit d'administration, tous les contrôles ont
passé : la construction, le pod sous `restricted-v2` avec un UID de la plage du projet, la racine en
lecture seule, le code qu'aucun utilisateur ne peut modifier, l'annuaire sous `restricted-v2`, les 26
contrôles de `integration/ldap/check.py` par le Service, `helm test` avec la NetworkPolicy active,
et aucun token ni mot de passe dans les journaux du serveur. Il ne prouve pas un cluster
déconnecté : la construction dans le cluster dispose du réseau que le cluster donne aux builds, et
ses fixtures tirent leurs images de registres publics.
