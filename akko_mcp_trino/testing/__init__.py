"""What an adapter of this server tests itself with: the contract every context provider passes.

Install it with ``pip install "akko-mcp-trino[contract]"``, which brings ``pytest`` by a range,
not a pin. It needs nothing else: no network, no catalogue, no Trino.
"""

from .context_contract import ContextContract, ContractHarness, ContractNames

__all__ = ["ContextContract", "ContractHarness", "ContractNames"]
