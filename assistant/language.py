"""Lightweight language hint for incoming messages.

The result is only a hint for Claude, so simple heuristics are enough.
"""
from __future__ import annotations

import re
from typing import Optional

_APOSTROPHES = "'ʻʼ’‘`"
_APOSTROPHE_RE = re.compile("[" + re.escape(_APOSTROPHES) + "]")

_UZBEK_CYRILLIC_LETTERS = set("ўқғҳ")
_UZBEK_CYRILLIC_WORDS = {
    "ассалому", "алайкум", "салом", "рахмат", "яхши", "нима", "қалесан", "калесан",
    "йўқ", "йук", "бугун", "эртага", "керак", "хоп", "майли", "кейин", "хозир",
    "мен", "сен", "сиз", "биз",
}

_UZBEK_LATIN_WORDS = {
    "assalomu", "alaykum", "salom", "rahmat", "yaxshi", "nima", "qalesan", "qalaysan",
    "qalaysiz", "qayerda", "qayerdasan", "qachon", "qancha", "bugun", "ertaga", "kerak",
    "xop", "bopti", "zor", "hozir", "keyin", "yoq", "men", "sen", "siz", "biz",
}
_UZBEK_LATIN_SUFFIXES = (
    "misan", "misiz", "mizmi", "yapsan", "yapman", "yapti", "yapmiz",
    "asizmi", "ganman", "dingmi",
)

_CYRILLIC_WORD_RE = re.compile(r"[а-яёўқғҳ]+")
_LATIN_TOKEN_RE = re.compile(r"[a-z']+")
# English clitics that can follow "o" or "g": who's, dog's, who'd, who'll, who've, who're.
_ENGLISH_CLITIC_RE = re.compile(r"^[a-z]+'(s|d|ll|ve|re)$")


def detect_language(text: str) -> Optional[str]:
    """Return ``"uz"``, ``"ru"``, ``"en"``, or ``None`` when the text has no letters."""
    cyrillic = sum(1 for char in text if "Ѐ" <= char <= "ӿ")
    latin = sum(1 for char in text if char.isascii() and char.isalpha())
    if not cyrillic and not latin:
        return None
    lowered = text.lower()
    if cyrillic > latin:
        return "uz" if _looks_uzbek_cyrillic(lowered) else "ru"
    return "uz" if _looks_uzbek_latin(lowered) else "en"


def _looks_uzbek_cyrillic(lowered: str) -> bool:
    if _UZBEK_CYRILLIC_LETTERS.intersection(lowered):
        return True
    return any(word in _UZBEK_CYRILLIC_WORDS for word in _CYRILLIC_WORD_RE.findall(lowered))


def _looks_uzbek_latin(lowered: str) -> bool:
    normalized = _APOSTROPHE_RE.sub("'", lowered)
    tokens = _LATIN_TOKEN_RE.findall(normalized)
    if any(_has_uzbek_apostrophe(token) for token in tokens):
        return True
    for token in tokens:
        bare = token.replace("'", "")
        if bare in _UZBEK_LATIN_WORDS or bare.endswith(_UZBEK_LATIN_SUFFIXES):
            return True
    return False


def _has_uzbek_apostrophe(token: str) -> bool:
    if token.startswith("'"):
        # A quoted word such as 'hello' is not an o' or g' letter.
        return False
    if token.startswith("o'clock") or _ENGLISH_CLITIC_RE.match(token):
        return False
    return "o'" in token or "g'" in token
