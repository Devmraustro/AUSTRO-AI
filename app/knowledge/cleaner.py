"""AUSTRO AI - Text normalization (NORMALIZE pipeline step).

Keeps paragraph/section boundaries visible with a single blank line between
blocks, collapses whitespace, strips control characters and normalizes Arabic
letter forms enough for deterministic hashing + keyword matching without
breaking the original readability.
"""

from __future__ import annotations

import re
import unicodedata

_ARABIC_LETTERS = "ابتثجحخدذرزسشصضطظعغفقكلمنهويءأإآةىؤئ"
_LATIN_LETTERS = "abcdefghijklmnopqrstuvwxyz"

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_SPACE = re.compile(r"[ \t\u00a0\u2000-\u200b\u202f\u205f]{2,}")
_PAGE_NUMBER_LINE = re.compile(r"^\s*\d{1,4}\s*$")
_CONTROL_SAFE = re.compile(r"[\u202a\u202b\u202c\u202d\u202e]")

_ALIASES = str.maketrans({
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ى": "ي",
    "ة": "ه",
    "ء": "",
    "\u200f": "",
})


class TextCleaner:
    """Deterministic, reversible-ish text normalization."""

    def normalize(self, text: str) -> str:
        text = text.replace("\ufeff", "")
        # Zero-width format chars (ZWNJ, ZWJ, word joiner, LRM/RLM).
        text = re.sub(r"[\u200b\u200c\u200d\u2060\u200e\u200f]", "", text)
        text = _CTRL.sub("", text)
        text = _CONTROL_SAFE.sub("", text)
        # Reflow: blank line separates paragraph/section blocks.
        lines = [
            _MULTI_SPACE.sub(" ", line).strip()
            for line in text.split("\n")
        ]
        # Drop standalone page-number lines (scanned PDFs).
        lines = [line for line in lines if not _PAGE_NUMBER_LINE.match(line)]
        cleaned_blocks: list[str] = []
        pending: list[str] = []
        for line in lines:
            if not line:
                if pending:
                    cleaned_blocks.append(" ".join(pending))
                    pending = []
            else:
                pending.append(line)
        if pending:
            cleaned_blocks.append(" ".join(pending))
        return "\n\n".join(cleaned_blocks).strip()

    def transform(self, text: str) -> str:
        """Aggressive normalization used for hashing and keyword matching."""
        text = unicodedata.normalize("NFKC", text).lower()
        text = text.translate(_ALIASES)
        return re.sub(r"[^a-z" + _ARABIC_LETTERS + r"\d\s]", " ", text).strip()

    def tokens(self, text: str) -> list[str]:
        return [t for t in self.transform(text).split() if t]


__all__ = ["TextCleaner"]