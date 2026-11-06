"""What an adapter of this server tests itself with: the contract every context provider passes.

Install it with ``pip install "akko-mcp-trino[contract]"``, which brings ``pytest`` by a range,
not a pin. It needs nothing else: no network, no catalogue, no Trino. ``ContextContract``
checks the provider alone; ``GateContract`` proves, over the provider and the core's tools,
that nothing of an object Trino hides from a caller reaches them, with ``FakeTrino`` in
place of Trino.
"""

from .context_contract import (
    ContextContract,
    ContractHarness,
    ContractNames,
    GateContract,
    GateHarness,
)
from .fake_trino import FakeTrino, Statement, Visible

__all__ = [
    "ContextContract",
    "ContractHarness",
    "ContractNames",
    "FakeTrino",
    "GateContract",
    "GateHarness",
    "Statement",
    "Visible",
]
