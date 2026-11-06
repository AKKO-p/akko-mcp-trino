"""A stand-in for ``pyspnego``: it records the contexts built and the tokens they give.

No Kerberos library, no KDC and no keytab are involved; the tests check what the provider
asks of ``spnego`` and what it sends to Atlas.
"""

import sys
import types
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeSpnego:
    """What the provider asked: one entry per context, with its credential and target."""

    contexts: list[dict[str, Any]] = field(default_factory=list)
    fail: Exception | None = None

    def module(self) -> types.ModuleType:
        module = types.ModuleType("spnego")
        module.KerberosKeytab = self.keytab  # type: ignore[attr-defined]
        module.client = self.client  # type: ignore[attr-defined]
        return module

    @staticmethod
    def keytab(keytab: str, principal: str | None = None) -> tuple[str, str | None, str]:
        return ("keytab", principal, keytab)

    def client(self, **kwargs: Any) -> "_Context":
        if self.fail is not None:
            raise self.fail
        self.contexts.append(kwargs)
        return _Context(len(self.contexts))


class _Context:
    def __init__(self, number: int) -> None:
        self.number = number

    def step(self, in_token: bytes | None = None) -> bytes:
        return f"token-{self.number}".encode()


def install(monkeypatch: Any, fake: FakeSpnego | None = None) -> FakeSpnego:
    """Put the stand-in in place of ``spnego``, and empty modules in place of the Kerberos
    bindings it needs (``gssapi``, ``krb5``), for the test."""
    fake = fake or FakeSpnego()
    monkeypatch.setitem(sys.modules, "spnego", fake.module())
    for binding in ("gssapi", "krb5"):
        monkeypatch.setitem(sys.modules, binding, types.ModuleType(binding))
    return fake
