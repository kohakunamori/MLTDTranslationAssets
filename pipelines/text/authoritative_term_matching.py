"""Shared matching rules for authoritative source terms."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import unicodedata


@dataclass(frozen=True)
class TermMatch:
    """One selected occurrence of an authoritative term in source text."""

    start: int
    end: int
    term: str


def _is_katakana_letter(char: str) -> bool:
    if not char:
        return False
    name = unicodedata.name(char, "")
    return name.startswith(("KATAKANA LETTER ", "HALFWIDTH KATAKANA LETTER "))


def _is_prolonged_sound_mark(char: str) -> bool:
    if not char:
        return False
    return "PROLONGED SOUND MARK" in unicodedata.name(char, "")


def _is_all_katakana(term: str) -> bool:
    return bool(term) and all(
        _is_katakana_letter(char) or _is_prolonged_sound_mark(char)
        for char in term
    )


def _has_katakana_boundary(text: str, start: int, end: int, term: str) -> bool:
    if not _is_all_katakana(term):
        return False
    left = text[start - 1] if start else ""
    right = text[end] if end < len(text) else ""
    return any(
        _is_katakana_letter(char) or _is_prolonged_sound_mark(char)
        for char in (left, right)
    )


def find_term_matches(text: str, terms: Iterable[str]) -> list[TermMatch]:
    """Find non-overlapping authoritative terms using source string offsets.

    All-katakana candidates are names only when they are not embedded in a
    larger katakana run. Kanji and mixed terms intentionally have no generic
    script-boundary restriction because Japanese prose is not word-segmented.
    """

    candidates: list[TermMatch] = []
    for term in sorted({term for term in terms if term}, key=len, reverse=True):
        offset = 0
        while True:
            start = text.find(term, offset)
            if start < 0:
                break
            end = start + len(term)
            if not _has_katakana_boundary(text, start, end, term):
                candidates.append(TermMatch(start, end, term))
            offset = start + 1

    candidates.sort(key=lambda match: (-(match.end - match.start), match.start))
    selected: list[TermMatch] = []
    for candidate in candidates:
        if all(
            candidate.end <= match.start or candidate.start >= match.end
            for match in selected
        ):
            selected.append(candidate)

    return sorted(selected, key=lambda match: match.start)
