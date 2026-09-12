"""core.identity: the current-identity ContextVar (X-Trino-User propagation)."""
from core import identity
from core.auth import Principal


def test_default_no_principal():
    assert identity.current_principal() is None
    assert identity.current_subject() is None


def test_set_and_read_subject():
    tok = identity.set_current_principal(Principal(subject="dave_steward", roles=["akko-steward"]))
    try:
        assert identity.current_principal().subject == "dave_steward"
        assert identity.current_subject() == "dave_steward"
    finally:
        identity.reset_current_principal(tok)
    assert identity.current_subject() is None  # reset → repli compte de service


def test_principal_without_subject_yields_none():
    tok = identity.set_current_principal(Principal(subject=""))
    try:
        assert identity.current_subject() is None
    finally:
        identity.reset_current_principal(tok)
