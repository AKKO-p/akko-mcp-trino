# Contribuer

[English](CONTRIBUTING.md) · **Français**

Merci d'envisager une contribution. Ce projet est petit à dessein, et il doit le rester.

## Règles de base

- **Les tests d'abord.** Chaque changement vient avec un test qui échoue avant le changement et
  passe après. La couverture doit rester à 100 % ; la CI refuse moins.
- **Le paquet reste neutre vis-à-vis des produits.** Rien dans `akko_mcp_trino/` ne peut importer
  une couche produit ni coder en dur une valeur d'un éditeur. `lint-vendor-neutral.sh` le vérifie.
- **Le serveur ne décide pas des accès.** Trino en décide, pour l'identité que donne le mode
  d'identité. Un changement qui met une politique dans le serveur (listes de tables autorisées,
  réécriture du SQL par rôle…) sera refusé : cela relève du moteur.
- **Refuser dans le doute.** Dans le doute, refuser. Un contrôle qui ne peut pas vérifier ne doit pas faire
  semblant.
- **Le code en anglais.** Le code, les commentaires et les messages de commit sont en anglais.
  Les README, la documentation (`docs/en` et `docs/fr`, page par page) et ce guide existent en
  anglais et en français, et changent ensemble. [Développeurs](docs/fr/developers.md#contribuer-à-la-documentation)
  donne les termes qu'emploie la documentation.

## Installation

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements/build.txt -r requirements/dev.txt   # les outils que lance la CI
pip install --no-deps --no-build-isolation -e .
```

## Avant de pousser

```bash
ruff check akko_mcp_trino tests && ruff format akko_mcp_trino tests
bash lint-vendor-neutral.sh
pytest
```

## Dépendances et verrous

`pyproject.toml` déclare des plages, pour que le serveur s'installe à côté d'autres outils ; la CI
lance les tests aux versions les plus basses et les plus hautes qu'elles permettent. Tout le reste
s'installe depuis un verrou, en versions exactes et avec leurs empreintes, écrit par
[pip-compile](https://pip-tools.readthedocs.io/) depuis sa source :

| Verrou | Source | Installé par |
|---|---|---|
| `requirements/image.txt` | `requirements/image.in` : les dépendances que déclarent le cœur et l'adaptateur Atlas | l'image de conteneur, les contrôles de licences et de vulnérabilités |
| `requirements/build.txt` | `requirements/build.in` : l'outil de construction et le backend de `[build-system]`, et Cython pour les sdists de l'extra `kerberos` | l'étape de construction de l'image, et les contrôles, avec `dev.txt` |
| `requirements/release.txt` | `requirements/release.in` : les outils de construction et twine | la publication (sdist, wheel, envoi) |
| `requirements/dev.txt` | `requirements/dev.in` : les outils que lancent les contrôles, sans version dans la source, résolus aux versions de l'image et de la publication | la CI et l'installation de développement, sur chaque version de Python |
| `requirements/kerberos.txt` | son `.in` : l'extra `kerberos` de l'adaptateur Atlas | l'image de conteneur (son étape de construction en construit les wheels), le contrôle des licences |

Les versions des outils (pytest, ruff, mypy et les autres) ne vivent que dans
`requirements/dev.txt` : les extras `test` et `dev` de `pyproject.toml` les nomment sans version,
pour qu'une mise à jour, par Dependabot ou par `lock.sh compile`, ne change que le verrou.

Les wheels et le sdist sont construits par le backend verrouillé, sans isolation
(`python -m build --no-isolation`), pour qu'aucun outil de construction ne soit téléchargé hors
verrou. L'image installe son verrou par `pip install --only-binary=:all: --require-hashes`, puis le
cœur et l'adaptateur Atlas sans leurs dépendances ; `pip check` fait échouer la construction s'il
manque au verrou une dépendance que l'un d'eux ou un paquet verrouillé déclare. Il ne voit pas une
dépendance qu'apporte seulement un extra, comme la CLI de `mcp[cli]`.

`dev.txt`, `image.txt` et `kerberos.txt` installent les paquets depuis leurs dossiers (`-e` ou
`-r image.in`). Ils laissent donc hors du verrou les paquets du dépôt (le cœur et l'adaptateur
Atlas), pip et setuptools (`--unsafe-package`). Dependabot compile tout verrou qui épingle pip ou setuptools avec
`--allow-unsafe`, ce qui échoue pour un paquet installé depuis son dossier. setuptools reste quand
même épinglé pour les contrôles, parce qu'ils installent `build.txt` avec `dev.txt`. `build.txt` et
`release.txt` épinglent pip et setuptools (`--allow-unsafe`). Le verrou `kerberos` construit gssapi et krb5 depuis
leurs sdists (empreintes vérifiées) contre le MIT Kerberos du système (`libkrb5-dev`). L'étape de
construction de l'image les construit sans isolation, avec le Cython de `build.txt`, en wheels que
l'étape finale installe par un montage, avec `--no-index` ; l'étape finale ajoute les
bibliothèques d'exécution du MIT Kerberos (`libgssapi-krb5-2`). Le contrôle des licences installe
le verrou avec isolation : les outils de construction que pip y télécharge pour ces deux-là ne
sont pas verrouillés.

Le Dockerfile nomme son image de base une seule fois, épinglée par digest, dans ses deux étapes,
pour que l'image soit reproductible ; Dependabot en apporte les mises à jour de sécurité, une
version corrective de Python ou de nouveaux paquets système, sous forme d'une nouvelle étiquette et
d'un nouveau digest. Le Python de l'image est celui des verrous : `scripts/lock.sh` le lit dans la
version de la ligne `FROM` du Dockerfile (`LOCK_PYTHON`), et `tests/test_locks.py` vérifie que les
verrous ont été compilés avec lui. Passer à une nouvelle version mineure de Python est une étape
manuelle : la ligne `FROM`, puis `bash scripts/lock.sh compile` sur ce Python.

Le Dockerfile installe quelques paquets Debian par apt : un compilateur et les en-têtes du MIT
Kerberos dans l'étape de construction, les bibliothèques du MIT Kerberos dans l'étape finale.
Chacun est épinglé à une version exacte (`nom=version`), avec `--no-install-recommends`, et
`tests/test_locks.py` échoue sur un paquet sans version. Ces versions sont celles de la version de
Debian de l'image de base, les mêmes sur `linux/amd64` et `linux/arm64`. Quand Dependabot change
l'image de base, ou quand la construction échoue parce que le miroir Debian ne sert plus une
version épinglée (une mise à jour de sécurité l'a remplacée), lisez les versions que propose la
nouvelle base et écrivez-les dans le Dockerfile :

```bash
docker run --rm python:<version>-slim@sha256:<digest> sh -c 'apt-get update -qq &&
  apt-get install -s --no-install-recommends libgssapi-krb5-2 | grep ^Inst &&
  apt-cache policy gcc libc6-dev libkrb5-dev | grep -E "^[a-z]|Candidate"'
```

Les lignes `Inst` nomment `libgssapi-krb5-2` et chaque bibliothèque qu'il apporte, avec leur
version : l'étape finale les épingle toutes. Les lignes `Candidate` donnent les versions des trois
paquets qu'épingle l'étape de construction. Construisez ensuite l'image pour les deux plateformes
(`docker buildx build --platform linux/amd64,linux/arm64 .`).

`scripts/lock.sh` est le seul endroit où la CI et la publication lisent les versions : `LOCK_PYTHON`,
les versions de Python des contrôles et de l'adaptateur Atlas (`CHECK_PYTHONS`), les plateformes de
l'image (`IMAGE_PLATFORMS`) et la glibc de l'image de base (`IMAGE_GLIBC`, que la CI vérifie sur
l'image qu'elle construit), d'où il tire les étiquettes de wheels de chaque plateforme. Dans
l'environnement installé plus haut, sur ce Python :

```bash
bash scripts/lock.sh compile    # après un changement d'un fichier .in ou des dépendances de pyproject.toml
bash scripts/lock.sh check      # ce que lance la CI : compiler à nouveau, empreintes comprises, échouer si un verrou change
bash scripts/lock.sh verify     # chaque version du verrou de l'image a une wheel pour chaque plateforme
bash scripts/lock.sh pins pytest pytest-cov   # les versions de dev.txt, pour un job qui les installe seules
PIP_COMPILE_ARGS="--upgrade-package sqlglot" bash scripts/lock.sh compile   # un paquet à sa dernière version
```

Le verrou `kerberos` demande `libkrb5-dev` pour être compilé. Une exécution réelle de `check`, après
l'ajout de `pyyaml` à `image.in` sans nouvelle compilation :

```text
+    --hash=sha256:fc09d0aa354569bc501d4e787133afc08552722d3ab34836a80547331bb5d4a0
+    # via -r requirements/image.in
 referencing==0.37.0 \
     --hash=sha256:381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231 \
     --hash=sha256:44aefc3142c5b842538163acb373e24cce6632bd54bdb01b21ad5863489f50d8
lock.sh: a lock is stale; run: bash scripts/lock.sh compile
```

et de `verify` :

```text
lock.sh: requirements/image.txt resolves for linux/amd64 (52 wheels)
lock.sh: requirements/image.txt resolves for linux/arm64 (52 wheels)
```

pip-compile prévient que « The generated requirements file may be rejected by pip install » : le
paquet lui-même reste hors des verrous à dessein, car il s'installe depuis sa source.

Dependabot met les verrous à jour une fois par mois, en une seule pull request, et ne déplace une
plage de `pyproject.toml` que lorsqu'une nouvelle version en sort
(`versioning-strategy: increase-if-necessary`). Les lignes `-r` de `dev.in`, de `release.in` et de `kerberos.in` lui
font compiler d'abord les verrous qu'ils lisent ; son image a les en-têtes du MIT Kerberos dont le
verrou `kerberos` a besoin.

La publication télécharge un seul outil hors pip, `mcp-publisher`, à la version exacte de
`MCP_PUBLISHER_VERSION` dans `.github/workflows/release.yml`, et échoue si son archive ne correspond
pas à `MCP_PUBLISHER_SHA256`. Dependabot ne peut pas le suivre : pour le changer, on reprend les deux
depuis la nouvelle version. L'empreinte est la ligne de `mcp-publisher_linux_amd64.tar.gz` dans son
`registry_<version>_checksums.txt`.

## Pull requests

1. Faites un fork, partez de `main` (`git checkout -b feat/mon-changement`).
2. Un seul changement par pull request.
3. Écrivez le message de commit comme un titre court à l'impératif, une ligne vide, puis
   *pourquoi* le changement existe. Les préfixes conventionnels (`feat:`, `fix:`, `docs:`) sont
   bienvenus.
4. Mettez à jour `CHANGELOG.md` sous *Unreleased*.
5. Ouvrez la pull request ; la CI lance le lint, le contrôle de neutralité, les tests sur chaque
   version de Python de `scripts/lock.sh`, la fraîcheur des verrous, la construction du paquet et
   celle de l'image.

## Signaler un bug

Ouvrez une issue avec la version (`pip show akko-mcp-trino`), la version de Trino, le transport,
le `X-Reason` et l'identifiant de requête concernés, et la plus petite configuration qui reproduit
le problème. Ne collez jamais de token.

## Problèmes de sécurité

N'ouvrez pas d'issue publique. Voir [SECURITY.md](SECURITY.md).
