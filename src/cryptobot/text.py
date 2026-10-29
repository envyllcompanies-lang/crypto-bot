"""Text keys shared by the attention and social code: lower case, accents dropped,
letters and digits only. Its own module so the paper trader can use it without
loading the research code."""

from __future__ import annotations

import re
import unicodedata

MIN_KEY_CHARS = 4


def norm(text: str | None) -> str:
    """Lower case, accents dropped, letters and digits only."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())
