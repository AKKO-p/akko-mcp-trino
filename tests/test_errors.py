"""What an error means for an agent: the kind that says what to do about it."""

import pytest

from akko_mcp_trino.audit import CallTimeout
from akko_mcp_trino.errors import ERROR_KINDS, explain_error, metadata_view_error


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


def test_a_right_refused_by_the_server_or_the_identity_provider_is_permission():
    # PermissionError is an OSError, but a refusal is not a lost connection.
    assert explain_error(PermissionError("refused"))["kind"] == "permission"


def test_a_real_trino_user_error_is_explained_whole():
    from trino.exceptions import TrinoUserError

    exc = TrinoUserError(
        {
            "errorName": "COLUMN_NOT_FOUND",
            "errorType": "USER_ERROR",
            "message": "line 1:8: Column 'revnue' cannot be resolved",
        },
        query_id="q",
    )
    out = explain_error(exc)
    assert out["error"] == str(exc)
    assert out["kind"] == "column" and out["identifier"] == "revnue"


def test_an_error_name_without_a_type_is_unavailable():
    assert explain_error(FakeTrinoError("COLUMN_NOT_FOUND", None, "x"))["kind"] == "unavailable"


_VIEWS_HINT = (
    "This tool reads system.jdbc.columns and system.metadata.table_comments, which Trino "
    "filters for each user; the access control must allow reading them."
)


@pytest.mark.parametrize(
    "name, message",
    [
        ("PERMISSION_DENIED", "Access Denied: Cannot select from table system.jdbc.columns"),
        ("CATALOG_NOT_FOUND", "line 1:15: Catalog 'system' does not exist."),
        ("TABLE_NOT_FOUND", "line 1:15: Table 'system.jdbc.columns' does not exist"),
    ],
)
def test_a_refused_metadata_view_says_what_the_access_control_must_allow(name, message):
    exc = FakeTrinoError(name, "USER_ERROR", message)
    out = metadata_view_error(exc)
    expected = message if message.endswith(".") else message + "."
    assert out["error"] == f"{expected} {_VIEWS_HINT}"
    assert {k: v for k, v in out.items() if k != "error"} == {
        k: v for k, v in explain_error(exc).items() if k != "error"
    }


def test_a_refusal_by_the_server_or_the_identity_provider_says_it_too():
    assert metadata_view_error(PermissionError("refused"))["error"] == f"refused. {_VIEWS_HINT}"


def test_any_other_error_on_a_metadata_view_is_explained_as_usual():
    for exc in (
        FakeTrinoError("GENERIC_INTERNAL_ERROR", "INTERNAL_ERROR", "x"),
        FakeTrinoError("SYNTAX_ERROR", "USER_ERROR", "line 1:1: mismatched input"),
        CallTimeout("budget"),
    ):
        assert metadata_view_error(exc) == explain_error(exc)
