## What and why

<!-- One change per pull request. Say what it does and why it exists. -->

## Checklist

- [ ] A test fails before this change and passes after it (coverage stays at 100 %, line and branch).
- [ ] The package stays product-neutral (`bash lint-vendor-neutral.sh`).
- [ ] The server still carries identity and does not decide access.
- [ ] Every new public name has a docstring; the README and `CHANGELOG.md` (*Unreleased*) are updated when behaviour changes.
- [ ] `ruff check`, `ruff format --check`, `mypy akko_mcp_trino`, `pytest` are green locally.
