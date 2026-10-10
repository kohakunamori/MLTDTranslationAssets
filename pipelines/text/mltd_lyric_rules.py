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
from collections import Counter

#: Kana (full and half width) and CJK ideographs.  Not punctuation: a line of
#: English with full-width punctuation is still English.
KANA_KANJI_RE = re.compile(
    r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff\u3400-\u4dbf\uff66-\uff9f]"
)
#: Latin letters, half and full width.  A line with no Latin letter at all
#: (``♪``, ``5 4 3 2 1 0``) is not an English line and is not bypassed.
LATIN_RE = re.compile(r"[a-zA-Z\uff21-\uff3a\uff41-\uff5a]")

#: Placeholder classes that must survive verbatim in a lyric line.  Bare
#: ``<...>`` is excluded on purpose: in lyric text it is a *display* quotation
#: mark (``<いつの間にかこんなに>`` -> ``<不知不觉间已经如此>``), so comparing its text
#: would flag correct rows in ``lyrics/songs/scrobj_homesf.unity3d.jsonl``; the
#: bracket *balance* is checked instead, which still catches a dropped engine tag
#: such as ``<size=24>``.
NON_ANGLE_TOKEN_RE = re.compile(
    r"\{[^{}]+\}"
    r"|%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z]"
    r"|\\[nrt]"
    r"|\\[0-9]{2}\\"
)

#: A lyric translation may not be longer than this multiple of the line it
#: replaces, with a floor so a short line keeps room to be natural.  Measured
#: over every committed row (13,729 rows, 11,065 translated): the median ratio is
#: 0.89, the 99th percentile 1.50 and the maximum 2.67, so a ceiling of 4x cannot
#: reject anything the library holds.  It exists because a model that answered
#: with an explanation instead of a line produced a 15x blob (run 38058410546);
#: that draft was caught by the bracket rule, but only by luck.
LYRIC_LENGTH_MULTIPLE = 4
LYRIC_LENGTH_FLOOR = 40


def is_english_bypass(text: str) -> bool:
    """Whether this line is deliberately left in English."""
    clean = str(text).strip()
    return bool(LATIN_RE.search(clean)) and not KANA_KANJI_RE.search(clean)


def is_localizable_line(text: str) -> bool:
    """Whether a line needs a Chinese translation (i.e. is not an English bypass)."""
    return not is_english_bypass(text)


def validate_lyric_tokens(source: str, translated: str) -> None:
    """Everything a lyric translation must satisfy before it may be stored.

    Four rules, each with a measured reason:

    * placeholders (``{...}``, ``%s``, ``\\n``) survive verbatim;
    * the number of ``<`` and ``>`` is preserved, so a dropped ``<size=24>`` tag
      is caught while emphasis text stays free to be translated;
    * the translation is one line -- the device renders one line per slot, and no
      committed row has ever needed a newline;
    * it is not an essay: see :data:`LYRIC_LENGTH_MULTIPLE`.

    Called by ``scripts/validate_repo.py`` (the published repository) *and* by
    ``scripts/llm_translate_untranslated.py`` (before a machine draft is written),
    so a draft that could never pass validation is skipped there instead of
    failing the whole translation run.
    """
    before = Counter(NON_ANGLE_TOKEN_RE.findall(source))
    after = Counter(NON_ANGLE_TOKEN_RE.findall(translated))
    if before != after:
        raise ValueError(
            f"protected token mismatch: source={dict(before)!r} translation={dict(after)!r}"
        )
    if source.count("<") != translated.count("<") or source.count(">") != translated.count(">"):
        raise ValueError(
            "angle bracket count changed: "
            f"source=({source.count('<')},{source.count('>')}) "
            f"translation=({translated.count('<')},{translated.count('>')})"
        )
    if "\n" in translated or "\r" in translated:
        raise ValueError("lyric translation must stay on one line")
    ceiling = max(LYRIC_LENGTH_FLOOR, LYRIC_LENGTH_MULTIPLE * len(source))
    if len(translated) > ceiling:
        raise ValueError(
            f"lyric translation is {len(translated)} characters for a "
            f"{len(source)}-character source (ceiling {ceiling}): "
            "an explanation or a pasted prompt is not a translation"
        )
