"""Catalogue text is untrusted: it is cleaned and bounded before it reaches an agent."""

import itertools
import unicodedata

_KEPT_CONTROLS = {"\n", "\t"}
_DROPPED = ("Cc", "Cf", "Zl", "Zp")
# The most characters read for each character a text may keep: a text made mostly of
# dropped characters costs what it may serve, never what the catalogue sent.
_READ_PER_KEPT = 4
# The longest name a catalogue may give and still be served: a classification, a tag, a
# set, a property's key, the owner, the tier, a term's name, glossary, status,
# category or relation, a column of a grain or a join. Catalogues give names as
# identifiers and short labels, far below this; a longer one is dropped, never cut (a cut
# name is another name), and deciding so reads at most 4 * NAME_CHARS + 1 characters of
# it, whatever its length.
NAME_CHARS = 256


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


def clean_name(name: str) -> str:
    """Return ``name`` cleaned as ``clean_text`` does, or ``""`` past ``NAME_CHARS``.

    A name that keeps more than ``NAME_CHARS`` characters once cleaned is dropped whole,
    never cut; its cost follows ``NAME_CHARS``, not its length.
    """
    text, cut = clean_text(name, NAME_CHARS)
    return "" if cut else text
