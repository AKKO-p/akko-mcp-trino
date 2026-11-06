"""The protocol a context provider implements."""

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from .types import TableContext, TableRef, Term


@runtime_checkable
class ContextProvider(Protocol):
    """A catalogue that describes the objects Trino lists.

    ``deadline`` is an instant of ``time.monotonic()``. A provider never receives
    the Trino client or the caller's identity, and never writes to its catalogue.

    One member is optional and is not part of what ``isinstance`` checks:
    ``reset(self) -> None``, which the server calls on ``SIGHUP``, from its signal
    handler, so that the provider forgets what it remembered of its catalogue. It is a
    plain function, never a coroutine function (nothing awaits it); it returns ``None``
    at once, never blocks, sends no request and does not raise (what it raises is
    logged by type and dropped). ``ContextContract`` checks it when it is defined.
    """

    name: str
    context_api: int

    def maps(self, catalog: str) -> bool:
        """Say whether this provider covers a Trino catalog."""
        ...

    def served_sets(self) -> Mapping[str, frozenset[str]]:
        """Return the allowed sets, by kind (glossary, classification, property, owner)."""
        ...

    async def describe(
        self,
        refs: Sequence[TableRef],
        *,
        deadline: float,
        on_behalf_of: None = None,
    ) -> Mapping[TableRef, TableContext]:
        """Describe tables, their columns included, in one batch."""
        ...

    async def terms(self, keys: Sequence[str], *, deadline: float) -> Mapping[str, Term]:
        """Read glossary terms by key, once each."""
        ...

    def check_lines(self) -> list[str]:
        """Return the lines the configuration check prints for this provider."""
        ...

    async def aclose(self) -> None:
        """Release what the provider holds open."""
        ...

    def credentials_stamp(self) -> str:
        """Return a value that changes when a secret mounted as a file changes.

        It is empty when no secret comes from a file.
        """
        ...
