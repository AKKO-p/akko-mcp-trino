# Contributing

Thank you for considering a contribution. This project is small on purpose;
keeping it small is a feature.

## Ground rules

- **Tests first.** Every change comes with a test that fails before the change
  and passes after it. Coverage must stay at 100 %; CI refuses less.
- **The package stays product-neutral.** Nothing in `akko_mcp_trino/` may import
  a product layer or hardcode a vendor value. `lint-vendor-neutral.sh` checks.
- **The server carries identity; it does not decide access.** A change that
  puts policy in the server (allow-lists of tables, per-role SQL rewriting…)
  will be declined: that belongs in the engine.
- **Fail closed.** When in doubt, refuse. A guard that cannot verify must not
  pretend to.
- **English everywhere** — code, comments, docs, commit messages.

## Set up

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Before you push

```bash
ruff check akko_mcp_trino tests && ruff format akko_mcp_trino tests
bash lint-vendor-neutral.sh
pytest
```

## Pull requests

1. Fork, branch from `main` (`git checkout -b feat/my-change`).
2. Keep one change per pull request.
3. Write the commit message as a short imperative title, a blank line, then
   *why* the change exists. Conventional prefixes (`feat:`, `fix:`, `docs:`)
   are welcome.
4. Update `CHANGELOG.md` under *Unreleased*.
5. Open the pull request; CI runs lint, the neutrality guard, the tests on
   Python 3.12 and 3.13, a package build and an image build.

## Reporting a bug

Open an issue with the version (`pip show akko-mcp-trino`), the Trino version,
the transport, the relevant `X-Reason` and request id, and the smallest
configuration that reproduces it. Never paste a token.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
