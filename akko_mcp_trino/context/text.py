"""Catalogue text is untrusted: it is cleaned and bounded before it reaches an agent."""

import unicodedata

_KEPT_CONTROLS = {"\n", "\t"}


def clean_text(text: str, limit: int) -> tuple[str, bool]:
    """Drop control and format characters, then cut to ``limit`` characters.

    Characters of category ``Cc`` are removed except newline and tab; those of category
    ``Cf`` (zero width, bidirectional controls) are removed. The boolean says whether the
    text was cut.
    """
    kept = "".join(
        c for c in text if c in _KEPT_CONTROLS or unicodedata.category(c) not in ("Cc", "Cf")
    )
    if len(kept) > limit:
        return kept[:limit], True
    return kept, False
