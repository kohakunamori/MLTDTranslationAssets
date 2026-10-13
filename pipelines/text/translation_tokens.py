"""Shared plain-text rules for MLTD translation rows.

The token contract is used by extraction, quality review, promotion, and
repository validation.  Keeping it here prevents rule drift and avoids making
any plain-text consumer import a full Unity bundle reader.

``KANA_RE`` lives here for the same reason: the bundle codec used to be its only
definition, so the quality reviewer and the translation pool pulled UnityPy and
AES in just to test whether a row still contains kana.  The range is unchanged
and the codec re-exports the name, so every existing importer still works.
"""
from __future__ import annotations

import re
from collections import Counter


# Hiragana + katakana blocks, U+3040-U+30FF.  Deliberately not "all CJK":
# kanji-only sources are expected in the queue and must not be flagged here.
KANA_RE = re.compile(r"[\u3040-\u30ff]")


PROTECTED_TOKEN_RE = re.compile(
    r"\{[^{}]+\}"
    r"|%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z]"
    r"|<[^<>\r\n]+>"
    r"|\\[nrt]"
    r"|\\[0-9]{2}\\"
)


def protected_tokens(value: str) -> Counter[str]:
    """Return the multiset of formatting/control tokens in ``value``."""

    return Counter(PROTECTED_TOKEN_RE.findall(value))


def validate_translation(source: str, translated: str) -> None:
    """Ensure a translation preserves every protected token exactly."""

    before = protected_tokens(source)
    after = protected_tokens(translated)
    if before != after:
        raise ValueError(
            "protected token mismatch: "
            f"source={dict(before)!r} translation={dict(after)!r}"
        )


validate_translation_tokens = validate_translation

__all__ = [
    "KANA_RE",
    "PROTECTED_TOKEN_RE",
    "protected_tokens",
    "validate_translation",
    "validate_translation_tokens",
]
