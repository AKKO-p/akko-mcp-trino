"""What an error means for an agent: the kind that says what to do about it."""

import pytest

from akko_mcp_trino.audit import CallTimeout
from akko_mcp_trino.errors import ERROR_KINDS, explain_error


class FakeTrinoError(Exception):
    """Carries what a Trino error carries: its name, its type and its message."""

    def __init__(self, error_name, error_type, message):
        super().__init__(message)
        self.error_name, self.error_type, self.message = error_name, error_type, message


def test_a_column_that_does_not_resolve_is_named():
    exc = FakeTrinoError(
        "COLUMN_NOT_FOUND", "USER_ERROR", "line 1:8: Column 'revnue' cannot be resolved"
    )
    assert explain_error(exc) == {"error": str(exc), "kind": "column", "identifier": "revnue"}


def test_a_refused_right_is_permission_without_identifier():
    exc = FakeTrinoError(
        "PERMISSION_DENIED",
        "USER_ERROR",
        "Access Denied: Cannot select from table demo.crm.customers",
    )
    assert explain_error(exc) == {"error": str(exc), "kind": "permission"}


def test_an_unknown_user_error_is_other():
    assert explain_error(FakeTrinoError("DIVISION_BY_ZERO", "USER_ERROR", "x"))["kind"] == "other"


def test_an_internal_error_or_a_lost_connection_is_unavailable_never_valid():
    internal = FakeTrinoError("GENERIC_INTERNAL_ERROR", "INTERNAL_ERROR", "x")
    assert explain_error(internal)["kind"] == "unavailable"
    assert explain_error(ConnectionRefusedError("refused"))["kind"] == "unavailable"


def test_trino_client_connection_errors_are_unavailable():
    from trino.exceptions import HttpError, TrinoConnectionError

    assert explain_error(HttpError("502"))["kind"] == "unavailable"
    assert explain_error(TrinoConnectionError("lost"))["kind"] == "unavailable"


def test_a_word_that_is_not_the_kind_is_not_taken_as_identifier():
    exc = FakeTrinoError("TABLE_NOT_FOUND", "USER_ERROR", "Schema 'crm' does not exist")
    assert "identifier" not in explain_error(exc)


def test_the_servers_own_refusals_have_their_kind():
    assert explain_error(ValueError("Invalid schema"))["kind"] == "invalid_argument"
    assert explain_error(CallTimeout("budget"))["kind"] == "time_budget"
    assert explain_error(TimeoutError("slow"))["kind"] == "time_budget"


def test_any_other_exception_is_other():
    assert explain_error(RuntimeError("boom")) == {"error": "boom", "kind": "other"}


@pytest.mark.parametrize("name", sorted(ERROR_KINDS))
def test_every_listed_error_name_maps_to_a_kind(name):
    exc = FakeTrinoError(name, "USER_ERROR", "x")
    assert explain_error(exc)["kind"] == ERROR_KINDS[name]
