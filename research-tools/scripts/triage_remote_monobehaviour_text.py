#!/usr/bin/env python3
"""Triage remote MonoBehaviour JP strings for localization relevance.

The raw audit intentionally over-collects all Japanese MonoBehaviour string
fields. This stage separates obvious developer/runtime metadata (effect labels,
scene-template names) from display-text/defaults that need localization/runtime
review. It does not auto-promote candidates into production translation memory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.audit_apk_baked_ui_candidates import classify as classify_ui


def source_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify_row(row: dict[str, Any]) -> tuple[str, list[str]]:
    source = str(row.get("source", ""))
    examples = row.get("examples", [])
    if not isinstance(examples, list):
        examples = []
    paths = {str(e.get("field_path", "")) for e in examples if isinstance(e, dict)}
    scripts = {str(e.get("script_class", "")) for e in examples if isinstance(e, dict)}

    if scripts and scripts <= {"Imas.Live.CharEffectSettings"}:
        return "internal_effect_label", ["CharEffectSettings developer label"]
    if scripts and scripts <= {"Imas.Commu2.BgBoxTemplateInfoSobj"}:
        return "internal_scene_template", ["BgBox template/object name"]
    if scripts and scripts <= {
        "Imas.SilhouetteParams",
        "Imas.Live.CameraEffectParams",
    }:
        return "internal_render_parameter", ["render/effect parameter label"]

    has_display_field = "$.m_Text" in paths or bool(
        scripts
        & {
            "UnityEngine.UI.Text",
            "Imas.DLText",
            "Imas.PictgraphText",
            "Imas.ImasButton",
            "Imas.Theater.ImasCommonButtonLayout",
            "IGP.Board.View.UIGrandprixPassButton",
            "IGP.View.IGPRadioToggle",
            "IGP.Overlay.View.UIDialog",
            "Imas.DLLabel",
        }
    )
    has_text_key = any(
        "TextKey" in path
        or path.endswith("._labelText")
        or path.endswith("._onLabelText")
        or path.endswith("._title")
        for path in paths
    )

    if has_display_field:
        # Reuse the baked-UI language-shape classifier. Remote objects do not
        # currently carry GameObject names in this audit, so the decision is
        # based on source shape/cues rather than fabricated object metadata.
        apk_class, reasons = classify_ui(source, {})
        if apk_class == "debug_or_test":
            return "debug_or_test", reasons
        if apk_class == "runtime_placeholder":
            return "runtime_placeholder", reasons
        if apk_class == "user_visible_candidate":
            return "user_visible_candidate", reasons + ["display-text component"]
        return "short_ui_ambiguous", reasons + ["display-text component"]

    if has_text_key:
        return "localization_key_candidate", ["text/key-like UI field"]

    return "other_review", ["Japanese string in unclassified MonoBehaviour field"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--input",
        type=Path,
        default=Path("build/localization-90200/remote-monobehaviour-jp-text-audit.json"),
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("build/localization-90200/remote-monobehaviour-jp-text-triage.json"),
    )
    ap.add_argument(
        "--review-queue",
        type=Path,
        default=Path("build/localization-90200/remote-monobehaviour-jp-text-review-queue.jsonl"),
    )
    args = ap.parse_args()

    doc = json.loads(args.input.read_text(encoding="utf-8-sig"))
    rows = doc.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError("input rows must be a list")

    counts = Counter()
    out_rows: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    review_classes = {
        "user_visible_candidate",
        "short_ui_ambiguous",
        "localization_key_candidate",
        "other_review",
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        classification, reasons = classify_row(row)
        counts[classification] += 1
        out = {
            "source_sha256": str(row.get("source_sha256", "")) or source_id(str(row.get("source", ""))),
            "source": str(row.get("source", "")),
            "occurrences": int(row.get("occurrences", 0) or 0),
            "classification": classification,
            "classification_reasons": reasons,
            "examples": row.get("examples", []),
        }
        out_rows.append(out)
        if classification in review_classes:
            review.append(out)

    result = {
        "schema_version": 1,
        "kind": "mltd-remote-monobehaviour-jp-text-triage",
        "input": str(args.input),
        "input_unknown_unique": len(rows),
        "classification_counts": dict(sorted(counts.items())),
        "review_required": len(review),
        "policy": {
            "user_visible_candidate": "high-priority runtime/materialization review",
            "short_ui_ambiguous": "display component but short/default-like; runtime review",
            "localization_key_candidate": "verify key resolution before patching literal field",
            "internal_effect_label": "exclude from localization denominator",
            "internal_scene_template": "exclude from localization denominator",
            "internal_render_parameter": "exclude from localization denominator",
            "debug_or_test": "exclude unless runtime evidence proves visibility",
            "runtime_placeholder": "likely overwritten; verify before patching",
        },
        "rows": out_rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with args.review_queue.open("w", encoding="utf-8", newline="\n") as handle:
        for row in review:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    print(
        json.dumps(
            {
                "input_unknown_unique": len(rows),
                "classification_counts": dict(sorted(counts.items())),
                "review_required": len(review),
                "output": str(args.output),
                "review_queue": str(args.review_queue),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
