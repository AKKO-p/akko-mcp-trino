"""Catalogue text is cleaned and bounded."""

import pathlib

from akko_mcp_trino.context.text import NAME_CHARS, clean_name, clean_text


def test_zero_width_and_bidi_characters_are_removed():
    assert clean_text("a​b‮c", 10) == ("abc", False)


def test_control_characters_are_removed_but_newline_and_tab_stay():
    assert clean_text("a\x00b\x1b\nc\td", 10) == ("ab\nc\td", False)


def test_a_text_longer_than_the_limit_is_cut_and_says_so():
    assert clean_text("abcdef", 4) == ("abcd", True)


def test_a_text_of_exactly_the_limit_is_not_cut():
    assert clean_text("abcd", 4) == ("abcd", False)


def test_the_limit_applies_after_cleaning():
    assert clean_text("a​​bc", 3) == ("abc", False)


def test_line_and_paragraph_separators_are_removed():
    assert clean_text("a\u2028b\u2029c\nd", 10) == ("abc\nd", False)


class _Reads(str):
    """A text whose characters past ``allowed`` must never be read."""

    def __new__(cls, text, allowed):
        made = super().__new__(cls, text)
        made.allowed = allowed
        return made

    def __iter__(self):
        for n, c in enumerate(str.__iter__(self)):
            if n >= self.allowed:
                raise AssertionError("a character past the bound was read")
            yield c


def test_reading_stops_at_the_first_kept_character_past_the_limit():
    assert clean_text(_Reads("a" * 100_000, allowed=11), 10) == ("a" * 10, True)


def test_a_text_of_dropped_characters_is_read_up_to_four_times_its_limit():
    hidden = _Reads("\u200b" * 100_000 + "x", allowed=41)
    assert clean_text(hidden, 10) == ("", True)
    assert clean_text("\u200b" * 30 + "abc", 10) == ("abc", False), "within the bound, whole"
    assert clean_text("\u200b" * 40 + "abc", 10) == ("a", True), "past it, cut and said"


def test_a_name_within_its_bound_is_cleaned_whole():
    assert clean_name("p\u200bii") == "pii"
    assert clean_name("n" * NAME_CHARS) == "n" * NAME_CHARS


def test_a_name_past_its_bound_is_dropped_never_cut():
    assert clean_name("n" * (NAME_CHARS + 1)) == ""
    assert clean_name("\u200b" * (4 * NAME_CHARS + 1) + "n") == "", "past the read window"


def test_a_huge_name_is_read_within_its_window_only():
    huge = _Reads("n" * 1_000_000, allowed=4 * NAME_CHARS + 1)
    assert clean_name(huge) == ""


def test_the_bound_written_in_the_docs_is_the_codes():
    root = pathlib.Path(__file__).resolve().parents[2]

    def text(name):
        return " ".join((root / name).read_text(encoding="utf-8").split())

    window = 4 * NAME_CHARS + 1
    for name in ("README.md", "CHANGELOG.md", "MIGRATING.md"):
        assert f"longer than {NAME_CHARS} characters once cleaned" in text(name), name
    assert f"reads at most {window} characters" in text("README.md")
    assert f"de plus de {NAME_CHARS} caractères une fois nettoyé" in text("README.fr.md")
    assert f"lit au plus {window} caractères" in text("README.fr.md")
