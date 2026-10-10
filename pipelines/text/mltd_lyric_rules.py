"""The project's lyric text rules, with no heavy imports.

Kept separate from ``mltd_localize_scrobj`` (which imports UnityPy) on purpose:
``scripts/llm_translate_untranslated.py`` needs the English-bypass rule for the
``--scope lyrics`` queue, and must not start requiring a Unity reader to run the
text-only path.

The rule is copied verbatim from the exporter that produced the committed
library, ``pipelines/export/export_localization_for_github.py``::

    CJK_PATTERN   = [\\u3040-\\u309f\\u30a0-\\u30ff\\u4e00-\\u9fff\\u3400-\\u4dbf\\uff66-\\uff9f]
    LATIN_PATTERN = [a-zA-Z\\uff21-\\uff3a\\uff41-\\uff5a]
    is_pure_english_lyric(text) = LATIN and not CJK

Reproducing it exactly is what keeps ``lyrics_manifest.json`` comparable over
time: the committed library reports 1,066 ``english_bypass_slots`` for its 432
songs, and every one of those rows satisfies this rule.  Full-width punctuation
(``！``/``～``) is *not* kana and not an ideograph, so 236 rows such as
``SAY HALLO！`` and ``『MILLION』`` stay English -- treating them as Japanese
would queue already-settled rows for translation.
"""
from __future__ import annotations

import re

#: Kana (full and half width) and CJK ideographs.  Not punctuation: a line of
#: English with full-width punctuation is still English.
KANA_KANJI_RE = re.compile(
    r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff\u3400-\u4dbf\uff66-\uff9f]"
)
#: Latin letters, half and full width.  A line with no Latin letter at all
#: (``♪``, ``5 4 3 2 1 0``) is not an English line and is not bypassed.
LATIN_RE = re.compile(r"[a-zA-Z\uff21-\uff3a\uff41-\uff5a]")


def is_english_bypass(text: str) -> bool:
    """Whether this line is deliberately left in English."""
    clean = str(text).strip()
    return bool(LATIN_RE.search(clean)) and not KANA_KANJI_RE.search(clean)


def is_localizable_line(text: str) -> bool:
    """Whether a line needs a Chinese translation (i.e. is not an English bypass)."""
    return not is_english_bypass(text)
