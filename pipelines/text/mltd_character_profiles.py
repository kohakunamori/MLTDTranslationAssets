#!/usr/bin/env python3
import sys
from pathlib import Path
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))
"""Load explicitly reviewed MLTD character-voice profiles."""
from __future__ import annotations

import json
from pathlib import Path


def _read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            yield row


def load_character_profiles(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    out: dict[str, dict] = {}
    for row in _read_jsonl(path):
        if str(row.get("review_status", "")).lower() != "approved":
            continue
        code = str(row.get("speaker_code", "")).lower()
        profile = row.get("profile")
        if not code or not isinstance(profile, dict):
            raise ValueError("approved character profile requires speaker_code and profile object")
        tone = str(profile.get("tone_summary", "")).strip()
        traits = profile.get("speech_traits", [])
        avoid = profile.get("avoid", [])
        if not tone or not isinstance(traits, list) or not isinstance(avoid, list):
            raise ValueError(f"approved character profile is incomplete: {code}")
        if code in out:
            raise ValueError(f"duplicate approved character profile: {code}")
        out[code] = {
            "speaker_code": code,
            "name_jp": str(row.get("name_jp", "")),
            "idol_id": row.get("idol_id"),
            "profile": {
                "tone_summary": tone,
                "speech_traits": [str(x) for x in traits],
                "addressing_preferences": [
                    str(x) for x in profile.get("addressing_preferences", [])
                ],
                "avoid": [str(x) for x in avoid],
                "notes": str(profile.get("notes", "")),
            },
            "review_provenance": str(row.get("review_provenance", "")),
        }
    return out


def select_character_profile(usage_profile: dict, profiles: dict[str, dict]) -> dict | None:
    codes = usage_profile.get("speaker_codes", []) if isinstance(usage_profile, dict) else []
    if not isinstance(codes, list) or len(codes) != 1:
        return None
    return profiles.get(str(codes[0]).lower())
