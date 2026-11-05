"""Catalogue text is cleaned and bounded."""

from akko_mcp_trino.context.text import clean_text


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
