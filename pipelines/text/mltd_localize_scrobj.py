#!/usr/bin/env python3
"""Read the lyric slots of an official song bundle (``scrobj_*``).

A lyric bundle is *not* a GTX text bundle.  It is a UnityFS archive whose
MonoBehaviour carries a ``scenario`` array; each element's ``str`` field is one
displayed line.  Handing such a bundle to the GTX reader fails with
"expected exactly one TextAsset", which is why song lyrics need their own reader
and their own place in the pipeline (``lyrics/songs/<bundle>.jsonl``).

Fidelity note: the wide/non-wide split and the row order below reproduce the
existing 432-song library exactly -- verified against ``scrobj_glowto`` and
``scrobj_aftspt`` by comparing (index, source_sha256) pairs, so a newly
extracted song is indistinguishable in shape from a song extracted by the
original offline run.

The reader only *reads*: writing a localized lyric bundle back into a mountable
overlay is a separate, explicitly gated step that this module does not perform.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import UnityPy

_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from mltd_localize_gtx import jsonl_lines  # noqa: E402
from mltd_lyric_rules import (  # noqa: E402
    KANA_KANJI_RE,
    LATIN_RE,
    is_english_bypass,
    is_localizable_line,
)

# Re-exported for callers and tests that reach the rules through this module.
__all__ = [
    "KANA_KANJI_RE",
    "LATIN_RE",
    "LyricBundleError",
    "LyricSlot",
    "is_english_bypass",
    "is_localizable_line",
    "merge_slots",
    "read_slots",
    "read_song",
    "rebuild_aggregate",
    "slot_rows",
    "song_file",
    "song_names",
    "write_song",
    "write_text_if_changed",
]


class LyricBundleError(ValueError):
    """The bundle is not a lyric bundle this reader understands."""


@dataclass(frozen=True)
class LyricSlot:
    """One displayed line of a song."""

    index: int
    tick: int
    abs_time: float
    text: str

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def read_slots(bundle: Path) -> list[LyricSlot]:
    """Every non-empty lyric line of every ``scenario`` object, in bundle order.

    Returns localizable (Japanese) lines first, then the remaining lines: that is
    the order the existing library uses, and it keeps the "translated" rows
    contiguous for the portal and the review UI.
    """
    bundle = Path(bundle)
    if not bundle.is_file():
        raise LyricBundleError(f"lyric bundle not found: {bundle}")
    try:
        environment = UnityPy.load(str(bundle))
    except Exception as exc:  # noqa: BLE001 - any container error is "not a lyric bundle"
        raise LyricBundleError(f"{bundle}: not a readable UnityFS archive ({type(exc).__name__}: {exc})") from exc
    wide: list[LyricSlot] = []
    rest: list[LyricSlot] = []
    scenario_objects = 0
    for obj in environment.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            tree = obj.read_typetree()
        except Exception:  # noqa: BLE001 - a non-scenario MonoBehaviour is not an error
            continue
        if not isinstance(tree, dict) or not isinstance(tree.get("scenario"), list):
            continue
        scenario_objects += 1
        for position, element in enumerate(tree["scenario"]):
            if not isinstance(element, dict):
                continue
            text = element.get("str")
            if not isinstance(text, str) or not text:
                continue
            slot = LyricSlot(
                index=position,
                tick=int(element.get("tick") or 0),
                abs_time=float(element.get("absTime") or 0.0),
                text=text,
            )
            (wide if is_localizable_line(text) else rest).append(slot)
    if not scenario_objects:
        raise LyricBundleError(f"{bundle}: no MonoBehaviour with a scenario[] array")
    return wide + rest


def slot_rows(bundle_name: str, slots: Iterable[LyricSlot], updated_at: str) -> list[dict]:
    """JSONL rows for a song file, in the shape the existing library uses."""
    return [
        {
            "bundle": bundle_name,
            "index": slot.index,
            "tick": slot.tick,
            "abs_time": slot.abs_time,
            "source_sha256": slot.source_sha256,
            "ja": slot.text,
            "zh": "",
            "status": "untranslated",
            "updated_at": updated_at,
        }
        for slot in slots
    ]


def merge_slots(
    bundle_name: str, existing: list[dict], slots: list[LyricSlot], updated_at: str
) -> tuple[list[dict], dict]:
    """Re-extract a song while keeping every translation that still applies.

    Identity is the *line content*, not the array position: upstream can insert
    or remove a line, and re-keying on position alone would silently discard
    accepted translations.  A line whose Japanese text is unchanged keeps its
    translation (and is re-indexed when only its position moved); a line whose
    text changed becomes a fresh untranslated row, because the old translation
    no longer describes the source.

    The content hash is computed from ``ja`` instead of trusting the stored
    ``source_sha256``: a row whose two disagree would otherwise silently pair a
    translation with the wrong source.  Such a row aborts the merge.
    """
    by_identity: dict[tuple[int, str], dict] = {}
    by_source: dict[str, list[dict]] = {}
    for row in existing:
        text = str(row.get("ja", ""))
        computed = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
        stored = str(row.get("source_sha256", ""))
        if stored and computed and stored != computed:
            raise LyricBundleError(
                f"{bundle_name}: row {row.get('index')!r} has a source_sha256 that does not "
                "match its own source text; refusing to merge"
            )
        identity = (int(row.get("index", -1)), computed)
        by_identity[identity] = row
        by_source.setdefault(computed, []).append(row)

    preserved = 0
    reindexed = 0
    retranslated = 0
    dropped_accepted = 0
    matched_identities: set[tuple[int, str]] = set()
    merged: list[dict] = []
    for slot in slots:
        same_identity = by_identity.get((slot.index, slot.source_sha256))
        if same_identity is not None:
            matched_identities.add((slot.index, slot.source_sha256))
            row = dict(same_identity)
            row["bundle"] = bundle_name
            row["tick"] = slot.tick
            row["abs_time"] = slot.abs_time
            merged.append(row)
            preserved += 1
            continue
        same_source = [row for row in by_source.get(slot.source_sha256, [])]
        if same_source:
            donor = same_source[0]
            donor_hash = hashlib.sha256(str(donor.get("ja", "")).encode("utf-8")).hexdigest()
            matched_identities.add((int(donor.get("index", -1)), donor_hash))
            row = dict(donor)
            row["bundle"] = bundle_name
            row["index"] = slot.index
            row["tick"] = slot.tick
            row["abs_time"] = slot.abs_time
            merged.append(row)
            reindexed += 1
            continue
        merged.append({
            "bundle": bundle_name,
            "index": slot.index,
            "tick": slot.tick,
            "abs_time": slot.abs_time,
            "source_sha256": slot.source_sha256,
            "ja": slot.text,
            "zh": "",
            "status": "untranslated",
            "updated_at": updated_at,
        })
        retranslated += 1

    for (index, source) in by_identity:
        if (index, source) in matched_identities:
            continue
        row = by_identity[(index, source)]
        if str(row.get("status")) == "accepted" and str(row.get("zh", "")):
            dropped_accepted += 1

    return merged, {
        "preserved": preserved,
        "reindexed": reindexed,
        "retranslated": retranslated,
        "dropped_accepted": dropped_accepted,
    }


def song_file(lyrics_root: Path, bundle_name: str) -> Path:
    return Path(lyrics_root) / "songs" / f"{bundle_name}.jsonl"


def read_song(lyrics_root: Path, bundle_name: str) -> list[dict]:
    path = song_file(lyrics_root, bundle_name)
    if not path.is_file():
        return []
    return [json.loads(line) for line in jsonl_lines(path.read_text(encoding="utf-8")) if line.strip()]


def write_text_if_changed(path: Path, text: str) -> bool:
    """Write ``text`` unless the file already holds it; return whether it moved.

    The lyric refresh runs on every scheduled build, so an unconditional write
    would touch 1.3 MB of derived files (and their timestamps) on days when
    nothing changed and make "did this run change anything?" unanswerable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return True


def write_song(lyrics_root: Path, bundle_name: str, rows: list[dict]) -> bool:
    """Write one song file in the committed byte format.

    The library was produced with ``json.dumps(..., ensure_ascii=False)`` and its
    default ``", "``/``": "`` separators.  Using compact separators here would
    rewrite every untouched line of every touched song and bury the real change
    in the diff, so the format is reproduced instead of chosen.
    """
    path = song_file(lyrics_root, bundle_name)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    return write_text_if_changed(path, text)


def song_names(lyrics_root: Path) -> list[str]:
    songs = Path(lyrics_root) / "songs"
    if not songs.is_dir():
        return []
    return sorted(path.name[: -len(".jsonl")] for path in songs.glob("*.jsonl"))


def rebuild_aggregate(lyrics_root: Path) -> dict:
    """Rewrite ``all_lyrics.jsonl`` and ``lyrics_manifest.json`` from the songs.

    Counts are derived, never carried: ``total_slots`` counts every stored row,
    ``translated_slots`` the accepted ones and ``english_bypass_slots`` the
    remaining rows that the published rule leaves in English (Latin script, no
    kana, no ideographs -- 1,066 rows for the pre-existing 432 songs, the number
    the committed manifest already reports).  Deriving them means a hand-edited
    song file can never leave the summary disagreeing with the data.
    """
    lyrics_root = Path(lyrics_root)
    names = song_names(lyrics_root)
    total_slots = 0
    translated_slots = 0
    english_bypass_slots = 0
    song_summaries: list[dict] = []
    aggregate: list[dict] = []
    for name in names:
        rows = read_song(lyrics_root, name)
        accepted = 0
        bypass = 0
        for row in rows:
            if str(row.get("status")) == "accepted" and str(row.get("zh", "")):
                accepted += 1
            elif is_english_bypass(str(row.get("ja", ""))):
                bypass += 1
            aggregate.append(row)
        total_slots += len(rows)
        translated_slots += accepted
        english_bypass_slots += bypass
        song_summaries.append({"bundle": name, "slots": len(rows), "translated": accepted})

    aggregate_path = lyrics_root / "all_lyrics.jsonl"
    write_text_if_changed(
        aggregate_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in aggregate)
    )

    manifest = {
        "schema_version": 1,
        "kind": "mltd-lyrics-manifest",
        "description": "Bilingual synchronized lyrics with scrobj slot alignment",
        "counts": {
            "total_songs": len(names),
            "total_slots": total_slots,
            "translated_slots": translated_slots,
            "english_bypass_slots": english_bypass_slots,
        },
        "songs": song_summaries,
    }
    manifest_path = lyrics_root / "lyrics_manifest.json"
    write_text_if_changed(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest
