# Contributing

Thank you for considering a contribution. This project is small on purpose;
keeping it small is a feature.

## Ground rules

- **Tests first.** Every change comes with a test that fails before the change
  and passes after it. Coverage must stay at 100 %; CI refuses less.
- **The package stays product-neutral.** Nothing in `akko_mcp_trino/` may import
  a product layer or hardcode a vendor value. `lint-vendor-neutral.sh` checks.
- **The server does not decide access.** Trino does, for the identity the
  identity mode gives. A change that puts policy in the server (allow-lists of
  tables, per-role SQL rewriting…) will be declined: that belongs in the engine.
- **Fail closed.** When in doubt, refuse. A guard that cannot verify must not
  pretend to.
- **English everywhere** — code, comments, docs, commit messages.

## Set up

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements/dev.txt   # the tools, at the versions CI runs
pip install --no-deps -e .
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
Everything else installs from a lock of `requirements/`, at exact versions and
with their hashes, written by [pip-compile](https://pip-tools.readthedocs.io/)
from its source:

| Lock | Source | Installed by |
|---|---|---|
| `requirements/image.txt` | `requirements/image.in`: the dependencies the package declares | the container image, the license and vulnerability checks |
| `requirements/dev.txt` | `requirements/dev.in`: the `dev` extra and the tools of the checks, at the image's versions | CI and the set-up above, on every Python version |

The image installs its lock with `pip install --require-hashes`, then the
package without dependencies; `pip check` fails the build if the lock misses a
dependency. The Atlas provider enters the lock with the release that publishes
both; until then the image installs it without dependencies.

The Dockerfile names its base image once, pinned by digest
(`FROM python:3.12.16-slim@sha256:…`) in both stages, so that the image is
reproducible; Dependabot brings its security updates, a Python patch release
or new system packages, as a new tag and digest. The image's Python is the one
of the locks: `scripts/lock.sh` reads it from that line (`LOCK_PYTHON`, `3.12`
here), and `tests/test_locks.py` checks that the locks were compiled on it. A
new Python minor version is made by hand: the `FROM` line, then
`bash scripts/lock.sh compile` on that Python.

`scripts/lock.sh` is the one place CI and the release read the versions from:
`LOCK_PYTHON`, the Python versions of the checks (`CHECK_PYTHONS`) and the
platforms of the image (`IMAGE_PLATFORMS`). In the environment set up above,
on that Python:

```bash
bash scripts/lock.sh compile    # after a change to a .in file or to the dependencies of pyproject.toml
bash scripts/lock.sh check      # what CI runs: compile again, fail if a lock changes
bash scripts/lock.sh verify     # every pin of the image lock has a wheel for each platform
PIP_COMPILE_ARGS="--upgrade-package sqlglot" bash scripts/lock.sh compile   # one package to its latest version
```

A real run of `check`, after `pyyaml` was added to `image.in` without
compiling again:

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
install": the package itself is left out of the locks on purpose
(`--unsafe-package`), as it installs from its source.

Dependabot updates the locks once a month, in one pull request, and moves a
range of `pyproject.toml` only when a new version falls outside it
(`versioning-strategy: increase-if-necessary`).

## Pull requests

1. Fork, branch from `main` (`git checkout -b feat/my-change`).
2. Keep one change per pull request.
3. Write the commit message as a short imperative title, a blank line, then
   *why* the change exists. Conventional prefixes (`feat:`, `fix:`, `docs:`)
   are welcome.
4. Update `CHANGELOG.md` under *Unreleased*.
5. Open the pull request; CI runs lint, the neutrality guard, the tests on
   every Python version of `scripts/lock.sh`, the freshness of the locks, a
   package build and an image build.

## Reporting a bug

Open an issue with the version (`pip show akko-mcp-trino`), the Trino version,
the transport, the relevant `X-Reason` and request id, and the smallest
configuration that reproduces it. Never paste a token.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
