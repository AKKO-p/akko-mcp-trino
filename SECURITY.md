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

Anything that lets a caller read data the verified user could not read from
another client, run a write through `execute_query`, act without a verified
identity in strict mode, act as an unregistered agent product when products
are registered, or extract a token from a log or an error message.
