# Versioning policy

**English** · [Français](../fr/versioning.md)

## Version numbers

The server follows [Semantic Versioning](https://semver.org/). Its version is `MAJOR.MINOR.PATCH`;
the two packages (`akko-mcp-trino`, `akko-mcp-trino-atlas`), the images and the chart's
`appVersion` carry the same one. The chart has its own version, raised at each change of the
chart.

While the major version is 0:

- a **patch** release (0.8.5 to 0.8.6) fixes or adds, and never breaks: a setting, a tool, an
  answer field or a default keeps its meaning;
- a **minor** release (0.7 to 0.8) may break. Each break is listed in
  [CHANGELOG.md](../../CHANGELOG.md) under the release, and [MIGRATING.md](../../MIGRATING.md)
  says what to change.

What counts as a break: a setting removed, renamed or with another meaning, a default that
opens or closes access differently, a tool removed or renamed, a field removed from a tool's
answer, a field of the audit line or a metric removed or renamed, a chart value removed or with
another meaning. A new setting, tool, field or metric is not a break, nor is a refusal of a
request that was already outside the documented contract.

## How long a version is supported

- The latest minor version gets every fix, in a new patch release.
- The previous minor version gets security fixes for three months after the next minor version
  is released.
- [SECURITY.md](../../SECURITY.md#supported-versions) lists the versions supported today.

## Rhythm

A patch release ships when a fix or an addition is ready; a security fix ships at once. A minor
version ships when a break is needed, announced in the changelog of the release before it when
it can be. A tag is never moved and a release is never replaced: a mistake is fixed by a new
release.
