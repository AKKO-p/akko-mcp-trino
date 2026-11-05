"""What an adapter of this server tests itself with: the contract every context provider passes.

It needs ``pytest`` (the ``test`` extra) and nothing else: no network, no catalogue, no Trino.
"""

from .context_contract import ContextContract, ContractHarness, ContractNames

__all__ = ["ContextContract", "ContractHarness", "ContractNames"]
