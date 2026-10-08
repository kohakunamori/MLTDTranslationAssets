#!/usr/bin/env python3
"""Build a human/agent review queue for source-backed MLTD character voice profiles."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    args = ap.parse_args()

    doc = json.loads(args.evidence.read_text(encoding="utf-8"))
    speakers = doc.get("speakers", {}) if isinstance(doc, dict) else {}
    if not isinstance(speakers, dict):
        raise ValueError("evidence must contain speakers object")

    rows: list[dict] = []
    for code, evidence in sorted(speakers.items()):
        if not isinstance(evidence, dict):
            continue
        rows.append({
            "speaker_code": str(code),
            "idol_id": evidence.get("idol_id"),
            "name_jp": str(evidence.get("name_jp", "")),
            "identity_source": str(evidence.get("identity_source", "")),
            "catalogue_dialogue_rows": int(evidence.get("catalogue_dialogue_rows", 0) or 0),
            "official_evidence_available": int(evidence.get("official_evidence_available", 0) or 0),
            "official_style_samples": evidence.get("official_style_samples", []),
            "review_status": "pending",
            "review_provenance": "",
            "profile": {
                "tone_summary": "",
                "speech_traits": [],
                "addressing_preferences": [],
                "avoid": [],
                "notes": "",
            },
            "safe_to_use_in_translation": False,
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(args.output)

    summary = {
        "schema_version": 1,
        "speaker_profiles": len(rows),
        "with_name": sum(bool(row["name_jp"]) for row in rows),
        "with_official_evidence": sum(row["official_evidence_available"] > 0 for row in rows),
        "pending": len(rows),
        "approved": 0,
        "safe_to_use_in_translation": 0,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
