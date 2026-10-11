# Security policy

This server sits between AI agents and data. Take any weakness in it seriously;
we do.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

## Reporting a vulnerability

Email **security@akko-ai.com** with a description, the version, and a way to
reproduce. Do not open a public issue. You will get an acknowledgement within
three working days and a fix or a mitigation plan within thirty.

Please do not include real tokens, real data or real hostnames in a report.

## What counts

Anything that lets a caller read data their Trino identity could not read (the
configured one in `service` mode, their own in `jwt` mode), run a write through
any tool, reach Trino as another identity than the mode allows, send Trino a
token the server did not verify, act without a verified identity in strict
mode, or extract a token or a password from a log, an error message or
`--check`.
