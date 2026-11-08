# Contributing

**English** · [Français](CONTRIBUTING.fr.md)

Thank you for considering a contribution. This project is small on purpose;
keeping it small is a feature.

## Ground rules

- **Tests first.** Every change comes with a test that fails before the change
  and passes after it. Coverage must stay at 100%; CI refuses less.
- **The package stays product-neutral.** Nothing in `akko_mcp_trino/` may import
  a product layer or hardcode a vendor value. `lint-vendor-neutral.sh` checks.
- **The server does not decide access.** Trino does, for the identity the
  identity mode gives. A change that puts policy in the server (allow-lists of
  tables, per-role SQL rewriting…) will be declined: that belongs in the engine.
- **Fail closed.** When in doubt, refuse. A check that cannot verify must not
  pretend to.
- **Code in English.** Code, comments and commit messages are in English. The READMEs, the
  documentation (`docs/en` and `docs/fr`, page for page) and this guide exist in English
  and French and are updated together. [Developers](docs/en/developers.md#contributing-to-the-docs)
  gives the terms the documentation uses.

## Set up

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements/build.txt -r requirements/dev.txt   # the tools CI runs
pip install --no-deps --no-build-isolation -e .
```

## Before you push

```bash
ruff check akko_mcp_trino tests && ruff format akko_mcp_trino tests
bash lint-vendor-neutral.sh
pytest
```

## Dependencies and locks

`pyproject.toml` declares ranges, so that the server installs next to other
tools; CI runs the tests at the lowest and at the highest versions they allow.
Everything else installs from a lock, at exact versions and with their hashes,
written by [pip-compile](https://pip-tools.readthedocs.io/) from its source:

| Lock | Source | Installed by |
|---|---|---|
| `requirements/image.txt` | `requirements/image.in`: the dependencies the core and the Atlas provider declare | the container image, the license and vulnerability checks |
| `requirements/build.txt` | `requirements/build.in`: the build frontend and the backend of `[build-system]`, and Cython for the sdists of the `kerberos` extra | the image's build stage, and the checks, with `dev.txt` |
| `requirements/release.txt` | `requirements/release.in`: the build tools and twine | the release (sdist, wheel, upload) |
| `requirements/dev.txt` | `requirements/dev.in`: the tools the checks run, unpinned in the source, resolved to the versions used by the image and the release | CI and the setup above, on every Python version |
| `requirements/kerberos.txt` | its `.in`: the `kerberos` extra of the Atlas provider | the container image (its build stage builds the wheels), the license check |

The versions of the tools (pytest, ruff, mypy and the others) live in
`requirements/dev.txt` only: the `test` and `dev` extras of `pyproject.toml`
name them without a version, so that an update, by Dependabot or by
`lock.sh compile`, changes the lock alone.

The wheels and the sdist are built by the locked backend, without isolation
(`python -m build --no-isolation`), so that no build tool is fetched unlocked.
The image installs its lock with `pip install --only-binary=:all:
--require-hashes`, then the core and the Atlas provider without dependencies;
`pip check` fails the build if the lock misses a dependency that one of them or
a locked package declares. It does not see a dependency that only an extra
brings, such as the CLI of `mcp[cli]`.

`dev.txt`, `image.txt` and `kerberos.txt` install the packages from their
directories (`-e` or `-r image.in`). They therefore leave out the packages of
the repository (the core and the Atlas provider), pip and setuptools
(`--unsafe-package`). Dependabot compiles any lock that pins
pip or setuptools with `--allow-unsafe`, and that fails for a package installed
from its directory. setuptools still gets pinned for the checks, because they
install `build.txt` together with `dev.txt`. `build.txt` and `release.txt` pin
pip and setuptools (`--allow-unsafe`). The `kerberos` lock builds gssapi and krb5 from their
sdists (hashes checked) against the system's MIT Kerberos (`libkrb5-dev`). The
image's build stage builds them without isolation, with the Cython of
`build.txt`, into wheels that the final stage installs through a mount, with
`--no-index`; the final stage adds the MIT Kerberos runtime libraries
(`libgssapi-krb5-2`). The license check installs the lock with isolation: the
build tools pip fetches there for those two are not locked.

The Dockerfile names its base image once, pinned by digest, in both stages, so
that the image is reproducible; Dependabot brings its security updates, a
Python patch release or new system packages, as a new tag and digest. The
image's Python is the one of the locks: `scripts/lock.sh` reads it from the
version on the Dockerfile's `FROM` line (`LOCK_PYTHON`), and
`tests/test_locks.py` checks that the locks were compiled on it. Moving to a new
Python minor version is a manual step: the `FROM` line, then
`bash scripts/lock.sh compile` on that Python.

`scripts/lock.sh` is the one place CI and the release read the versions from:
`LOCK_PYTHON`, the Python versions of the checks and of the Atlas provider
(`CHECK_PYTHONS`), the platforms of the image (`IMAGE_PLATFORMS`) and the
glibc of the base image (`IMAGE_GLIBC`, which CI checks on the image it
builds), from which it derives the wheel tags of each platform. In the
environment set up above, on that Python:

```bash
bash scripts/lock.sh compile    # after a change to a .in file or to the dependencies of pyproject.toml
bash scripts/lock.sh check      # what CI runs: compile again, hashes included, fail if a lock changes
bash scripts/lock.sh verify     # every pin of the image lock has a wheel for each platform
bash scripts/lock.sh pins pytest pytest-cov   # the versions of dev.txt, for a job that installs them alone
PIP_COMPILE_ARGS="--upgrade-package sqlglot" bash scripts/lock.sh compile   # one package to its latest version
```

The `kerberos` lock needs `libkrb5-dev` to compile. A real run of `check`,
after `pyyaml` was added to `image.in` without compiling again:

```text
+    --hash=sha256:fc09d0aa354569bc501d4e787133afc08552722d3ab34836a80547331bb5d4a0
+    # via -r requirements/image.in
 referencing==0.37.0 \
     --hash=sha256:381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231 \
     --hash=sha256:44aefc3142c5b842538163acb373e24cce6632bd54bdb01b21ad5863489f50d8
lock.sh: a lock is stale; run: bash scripts/lock.sh compile
```

and of `verify`:

```text
lock.sh: requirements/image.txt resolves for linux/amd64 (52 wheels)
lock.sh: requirements/image.txt resolves for linux/arm64 (52 wheels)
```

pip-compile says "The generated requirements file may be rejected by pip
install": the package itself is left out of the locks on purpose, as it
installs from its source.

Dependabot updates the locks once a month, in one pull request, and moves a
range of `pyproject.toml` only when a new version falls outside it
(`versioning-strategy: increase-if-necessary`). The `-r` lines of `dev.in`,
`release.in` and `kerberos.in` make it compile the locks they read first; its
image has the MIT Kerberos headers that the `kerberos` lock needs.

The release downloads one tool outside pip, `mcp-publisher`, at the exact
version of `MCP_PUBLISHER_VERSION` in `.github/workflows/release.yml`, and
fails unless its archive matches `MCP_PUBLISHER_SHA256`. Dependabot cannot
follow it: to move it, set both from the new release. The hash is the
`mcp-publisher_linux_amd64.tar.gz` line of its `registry_<version>_checksums.txt`.

## Pull requests

1. Fork, branch from `main` (`git checkout -b feat/my-change`).
2. Keep one change per pull request.
3. Write the commit message as a short imperative title, a blank line, then
   *why* the change exists. Conventional prefixes (`feat:`, `fix:`, `docs:`)
   are welcome.
4. Update `CHANGELOG.md` under *Unreleased*.
5. Open the pull request; CI runs lint, the neutrality check, the tests on
   every Python version of `scripts/lock.sh`, the freshness of the locks, a
   package build and an image build.

## Reporting a bug

Open an issue with the version (`pip show akko-mcp-trino`), the Trino version,
the transport, the relevant `X-Reason` and request id, and the smallest
configuration that reproduces it. Never paste a token.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
