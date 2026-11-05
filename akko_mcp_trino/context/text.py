"""Catalogue text is untrusted: it is cleaned and bounded before it reaches an agent."""

import unicodedata

_KEPT_CONTROLS = {"\n", "\t"}
_DROPPED = ("Cc", "Cf", "Zl", "Zp")


def clean_text(text: str, limit: int) -> tuple[str, bool]:
    """Drop control, format and separator characters, then cut to ``limit`` characters.

    Characters of category ``Cc`` are removed except newline and tab; those of category
    ``Cf`` (zero width, bidirectional controls), ``Zl`` (line separator) and ``Zp``
    (paragraph separator) are removed. The boolean says whether the text was cut.
    """
    kept = "".join(
        c for c in text if c in _KEPT_CONTROLS or unicodedata.category(c) not in _DROPPED
    )
    if len(kept) > limit:
        return kept[:limit], True
    return kept, False
