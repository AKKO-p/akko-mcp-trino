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
