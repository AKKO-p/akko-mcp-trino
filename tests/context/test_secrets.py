"""File secrets: read again when the file changes, named by <NAME>_FILE before <NAME>."""

import os

import pytest

from akko_mcp_trino.context.secrets import FileSecret, secret_setting


def _write(path, text, mtime):
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(mtime, mtime))


def test_a_secret_file_is_read_again_when_it_changes(tmp_path):
    path = tmp_path / "token"
    _write(path, "first\n", 1_000_000_000)
    secret = FileSecret(str(path))
    assert secret.value() == "first"
    stamp = secret.stamp()
    _write(path, "second-longer", 1_000_000_000)
    assert secret.value() == "second-longer"
    assert secret.stamp() != stamp
    _write(path, "third-longer", 2_000_000_000)
    assert secret.value() == "third-longer"


def test_a_secret_file_that_did_not_change_is_not_read_again(tmp_path):
    path = tmp_path / "token"
    _write(path, "aaaa", 1_000_000_000)
    secret = FileSecret(str(path))
    assert secret.value() == "aaaa"
    _write(path, "bbbb", 1_000_000_000)  # same size, same mtime: nothing to see
    assert secret.value() == "aaaa"


def test_the_stamp_never_holds_the_secret(tmp_path):
    path = tmp_path / "token"
    _write(path, "marker-secret", 1_000_000_000)
    assert "marker-secret" not in FileSecret(str(path)).stamp()


def test_the_stamp_of_a_missing_file_says_so_and_its_value_raises(tmp_path):
    secret = FileSecret(str(tmp_path / "absent"))
    assert secret.stamp() == "missing"
    with pytest.raises(OSError):
        secret.value()


def test_the_file_variable_wins_over_the_plain_one(tmp_path):
    path = tmp_path / "token"
    _write(path, "from-file", 1_000_000_000)
    got = secret_setting({"API_TOKEN_FILE": str(path), "API_TOKEN": "plain"}, "API_TOKEN")
    assert isinstance(got, FileSecret) and got.value() == "from-file"


def test_the_plain_variable_or_nothing_without_a_file():
    assert secret_setting({"API_TOKEN": "plain"}, "API_TOKEN") == "plain"
    assert secret_setting({"API_TOKEN_FILE": ""}, "API_TOKEN") == ""
    assert secret_setting({}, "API_TOKEN") == ""


def test_a_file_that_cannot_be_read_or_is_empty_refuses_to_start(tmp_path):
    with pytest.raises(ValueError, match=r"API_TOKEN_FILE: cannot read"):
        secret_setting({"API_TOKEN_FILE": str(tmp_path / "absent")}, "API_TOKEN")
    empty = tmp_path / "empty"
    _write(empty, "\n", 1_000_000_000)
    with pytest.raises(ValueError, match=r"API_TOKEN_FILE: the file is empty"):
        secret_setting({"API_TOKEN_FILE": str(empty)}, "API_TOKEN")
