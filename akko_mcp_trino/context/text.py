"""Catalogue text is untrusted: it is cleaned and bounded before it reaches an agent."""

import itertools
import unicodedata

_KEPT_CONTROLS = {"\n", "\t"}
_DROPPED = ("Cc", "Cf", "Zl", "Zp")
# The most characters read for each character a text may keep: a text made mostly of
# dropped characters costs what it may serve, never what the catalogue sent.
_READ_PER_KEPT = 4


def clean_text(text: str, limit: int) -> tuple[str, bool]:
    """Drop control, format and separator characters, then cut to ``limit`` characters.

    Characters of category ``Cc`` are removed except newline and tab; those of category
    ``Cf`` (zero width, bidirectional controls), ``Zl`` (line separator) and ``Zp``
    (paragraph separator) are removed. The boolean says whether the text was cut.

    It reads ``4 * limit + 1`` characters of ``text`` at most, so its cost follows
    ``limit``, not the length of the text: reading stops at the first character kept past
    ``limit``, and a text whose first ``4 * limit + 1`` characters keep fewer is cut there.
    """
    window = _READ_PER_KEPT * limit + 1
    kept: list[str] = []
    read = 0
    for c in itertools.islice(text, window):
        read += 1
        if c in _KEPT_CONTROLS or unicodedata.category(c) not in _DROPPED:
            if len(kept) == limit:
                return "".join(kept), True
            kept.append(c)
    return "".join(kept), read == window and len(text) > window
