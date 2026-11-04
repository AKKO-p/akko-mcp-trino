"""File secrets: read again when the file changes, named by <NAME>_FILE before <NAME>."""

import logging
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


def test_a_file_that_cannot_be_decoded_refuses_to_start_without_showing_a_byte(tmp_path):
    path = tmp_path / "token"
    path.write_bytes(b"ab\xffcd")
    with pytest.raises(ValueError, match=r"API_TOKEN_FILE: cannot read .*UnicodeError") as caught:
        secret_setting({"API_TOKEN_FILE": str(path)}, "API_TOKEN")
    text = f"{caught.value} {caught.value.__cause__!r}"
    assert "0xff" not in text and "position" not in text and "byte" not in text


def test_a_value_that_cannot_be_decoded_has_no_exception_text(tmp_path):
    path = tmp_path / "token"
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeError) as caught:
        FileSecret(str(path)).value()
    assert "0xff" not in str(caught.value) and caught.value.__cause__ is None


def test_a_rotation_to_an_empty_or_undecodable_file_keeps_the_last_good_value(tmp_path, caplog):
    path = tmp_path / "token"
    _write(path, "good-secret", 1_000_000_000)
    secret = secret_setting({"API_TOKEN_FILE": str(path)}, "API_TOKEN")
    assert secret.value() == "good-secret"
    with caplog.at_level(logging.WARNING, logger="akko_mcp_trino.context.secrets"):
        _write(path, "", 2_000_000_000)
        assert secret.value() == "good-secret"
        assert secret.value() == "good-secret"
        path.write_bytes(b"\xff\xfe")
        os.utime(path, ns=(3_000_000_000, 3_000_000_000))
        assert secret.value() == "good-secret"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    for text in warnings:
        assert "API_TOKEN_FILE" in text and str(path) in text
        assert "good-secret" not in text and "0xff" not in text
    _write(path, "rotated", 4_000_000_000)
    assert secret.value() == "rotated"


def test_a_file_replaced_by_another_with_the_same_size_and_time_is_read_again(tmp_path):
    path = tmp_path / "token"
    _write(path, "aaaa", 1_000_000_000)
    secret = FileSecret(str(path))
    assert secret.value() == "aaaa"
    stamp = secret.stamp()
    other = tmp_path / "other"
    _write(other, "bbbb", 1_000_000_000)
    os.replace(other, path)  # a mounted secret is rotated by swapping the file
    assert secret.stamp() != stamp
    assert secret.value() == "bbbb"
