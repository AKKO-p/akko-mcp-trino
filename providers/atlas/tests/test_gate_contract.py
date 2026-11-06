"""The core's gate proven end to end over the Atlas provider.

``GateContract`` builds the server as it runs, with the provider and a recorded Atlas, and a
fake Trino in place of Trino; a caller who does not see a table or a column of the
recorded catalogue must receive nothing of it. Atlas has no notion of a join: the
harness names no join target, the join scenario is skipped with the reason, and the
contract fails if the provider ever gives a join.
"""

import pytest
from akko_mcp_trino.testing import GateContract, GateHarness
from test_contract import NAMES, harness_for, ref

HIDDEN = {
    # A table and a column of the described table, in each recorded catalogue.
    "atlas-2.1-seed": (ref("ledger", "accounts"), "customer_ref"),
    "atlas-2.1": (ref("ledger", "accounts"), "invoice_kind"),
}


class TestAtlasGate(GateContract):
    """The gate on the reference catalogue and on the real one."""

    @pytest.fixture(params=sorted(NAMES))
    def gate(self, request: pytest.FixtureRequest) -> GateHarness:
        table, column = HIDDEN[request.param]
        return GateHarness(
            contract=harness_for(request.param), hidden_table=table, hidden_column=column
        )


def test_every_catalogue_of_the_contract_has_its_hidden_objects():
    assert set(HIDDEN) == set(NAMES)
