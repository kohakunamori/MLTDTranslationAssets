#!/usr/bin/env python3
"""Six source-bound Event-unit corrections for explicit lexical/semantic errors.

Do not promote a heuristic QA PASS to independent translation acceptance.
Source identity, prior machine-translation hash, context and immutable v2 input
are checked before writing this isolated unreviewed audit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_event_unit_review_pack import sha_file
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id

BUILD = ROOT / "build/localization-90200"
V2 = BUILD / "audits/event-unit-unified-review-v2-client-9.0.200-assets-1077100"
SOURCE = V2 / "review-worklist.jsonl"
SOURCE_MANIFEST = V2 / "manifest.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
RULES = ROOT / "scripts/mltd_translation_quality.py"
DEST = BUILD / "audits/event-unit-six-contextual-corrections-client-9.0.200-assets-1077100"
SOURCE_SHA = "61a7da214fbdcb905ee947a8894c5b306e6bb5f3de326c1161ee8d877b367927"

# Frozen original machine-translation hashes avoid silently reviewing a text
# from a different version; new text is deliberately still agent-unreviewed.
DRAFTS = {
    "40996b95780b24": {
        "old_sha": "3142fb4f6291d7ffb4c6a915486c230233f7ec9606f509c4716338b03848699a",
        "translation": "环也渐渐理解这部戏的内容了！",
        "reason": "speaker_name_tamaki_not_yuhuan",
    },
    "5747a643729eb7": {
        "old_sha": "c6746b70d533736e71f1e4e9c344a4ecdf1d55e1396dad8ac104cde1f13e6409",
        "translation": "为了这次活动，ROCO准备了『●●●●制作人\n巨型福笑拼脸游戏』！",
        "reason": "translate_japanese_fukuwarai_game_name",
    },
    "3fdb2ddeb05706": {
        "old_sha": "238cf6e7c3982201b8240a57588a625bcee7337536fb8add392e81e56d79f841",
        "translation": "梓，有烦恼吗……？\n跟环和大家说说吧！",
        "reason": "speaker_inclusive_tamaki_tachi_without_duplication",
    },
    "39e116937ff421": {
        "old_sha": "98332d2961c6c8786235e40c9c9f2e5db65f7f0cbaa023442888f3438ad2a9a3",
        "translation": "亚利沙也很开心！\n听着千鹤小姐讲述，忍不住就幻想起来了……",
        "reason": "spoken_recollection_not_fiction_story",
    },
    "58cdaebcf9010d": {
        "old_sha": "8900be3d312fff5a6109ff6ebeb2b6f582ae3103996fff01e9b1ddecd79aac6f",
        "translation": "大家决定分成2组行动。\n先来看看法子和亚利沙这边……",
        "reason": "scene_transition_not_a_fiction_story_preserve_group_2",
    },
    "2073a475deaffb": {
        "old_sha": "fd086d8060cea3cce0280d92e67a0b889187f1432f8a56a8d578b9d9dc19d6bc",
        "translation": "听说临时接到了工作，不过说好开会前会回来。",
        "reason": "preserve_sentence_final_adversative_without_gender_guess",
    },
}


def proposals(rows: list[dict], drafts: dict = DRAFTS) -> list[dict]:
    lookup = {}
    for item in rows:
        sid, jp = item.get("source_sha256"), item.get("source")
        if not isinstance(jp, str) or sid != source_id(jp) or sid in lookup:
            raise ValueError("v2 reviewer source duplicate or SHA mismatch")
        lookup[sid] = item
    if len(lookup) != 1181:
        raise ValueError("wrong version or incomplete v2 review")
    if len(drafts) != 6:
        raise ValueError("not exactly six contextual corrections")
    output = []
    remotes = set()
    seen = set()
    for prefix, spec in drafts.items():
        if (len(prefix) != 14 or any(ch not in "0123456789abcdef" for ch in prefix)
            or set(spec) != {"old_sha", "translation", "reason"}):
            raise ValueError("malformed exact source-bound manual mapping")
        options = [r for sid, r in lookup.items() if sid.startswith(prefix)]
        if len(options) != 1:
            raise ValueError(f"source SHA prefix missing or ambiguous: {prefix}")
        row = options[0]
        sid = row["source_sha256"]
        if sid in seen:
            raise ValueError("two corrections for one Japanese source")
        seen.add(sid)
        old = row.get("machine_candidate_unreviewed")
        zh = spec["translation"]
        if (row.get("review_bucket") != "qa_review_without_draft"
            or row.get("current_machine_qa_verdict") != "REVIEW"
            or not row.get("current_machine_qa_issues")
            or row.get("agent_correction_draft_unreviewed") is not None
            or row.get("review_status") != "pending"
            or row.get("release_gate") != "needs_independent_review"
            or row.get("safe_to_mount_as_final_overlay") is not False
            or row.get("semantic_accuracy_verified") is not False
            or row.get("independent_review_complete") is not False
            or row.get("occurrences") != 1
            or len(row.get("examples", [])) != 1
            or not isinstance(old, str) or not old
            or hashlib.sha256(old.encode("utf8")).hexdigest() != spec["old_sha"]
            or not isinstance(zh, str) or not zh.strip() or zh == old
            or not isinstance(spec["reason"], str) or not spec["reason"]):
            raise ValueError(f"v2 source/candidate/status drift: {sid}")
        remote = row["examples"][0]["remote"]
        if remote in remotes:
            raise ValueError("this independent trial expects six different remotes")
        remotes.add(remote)
        qa = evaluate_row(
            {"source_sha256": sid, "source": row["source"],
             "examples": row["examples"], "occurrences": row["occurrences"]},
            {"source_sha256": sid, "source": row["source"],
             "translation": zh, "status": "agent_draft_unreviewed"},
            load_glossary(None),
        )
        if qa["qa_verdict"] != "PASS" or qa["issues"]:
            raise ValueError(f"new draft still has deterministic QA issues: {sid}")
        output.append({
            "source_sha256": sid, "source": row["source"],
            "machine_candidate_sha256": spec["old_sha"],
            "machine_candidate_unreviewed": old,
            "translation_draft": zh, "repair_reason": spec["reason"],
            "examples": row["examples"], "occurrences": row["occurrences"],
            "old_machine_QA_verdict": row["current_machine_qa_verdict"],
            "old_machine_QA_issues": row["current_machine_qa_issues"],
            "draft_qa_verdict": qa["qa_verdict"],
            "draft_qa_issues": qa["issues"],
            "status": "agent_draft_unreviewed", "review_status": "pending",
            "release_gate": "needs_independent_review",
            "independent_review_complete": False,
            "semantic_accuracy_verified": False,
            "safe_to_mount_as_final_overlay": False,
        })
    if len(output) != 6 or len(remotes) != 6:
        raise ValueError("six distinct source/bundle corrections required")
    return sorted(output, key=lambda x: x["source_sha256"])


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to((BUILD / "audits").resolve()):
        raise ValueError("source-bound corrections must stay under audits")
    if dest.exists():
        raise FileExistsError(f"immutable six-source audit exists: {dest}")
    tmp = dest.with_name(dest.name + ".incomplete")
    if tmp.exists():
        raise FileExistsError(f"incomplete six-source audit exists: {tmp}")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200",
        asset_version="1077100", asset_index=INDEX,
    )
    manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf8"))
    if (sha_file(SOURCE) != SOURCE_SHA
        or manifest.get("version_identity") != identity
        or manifest.get("review_worklist_sha256") != SOURCE_SHA
        or manifest.get("available_unreviewed_agent_drafts") != 90
        or manifest.get("qa_review_without_targeted_draft") != 818
        or manifest.get("independent_review_complete") is not False
        or manifest.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("frozen 9.0.200/1077100 source review changed")
    rows = proposals(read_jsonl(SOURCE))
    tmp.mkdir(parents=True)
    file = tmp / "six-source-bound-corrections.jsonl"
    with file.open("w", encoding="utf8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    out = {
        "schema_version": 1,
        "kind": "event-unit-six-contextual-drafts-NOT-INDEPENDENTLY-REVIEWED",
        "version_identity": identity,
        "frozen_v2_review_manifest_sha256": sha_file(SOURCE_MANIFEST),
        "frozen_v2_review_worklist_sha256": sha_file(SOURCE),
        "quality_rules_sha256": sha_file(RULES),
        "draft_file": file.name, "draft_sha256": sha_file(file),
        "draft_unique": len(rows), "draft_bundle_count": 6,
        "draft_deterministic_QA_verdicts": {"PASS": 6},
        "existing_v2_draft_unique_untouched": 90,
        "total_known_drafts_including_previous_line_bleed": 97,
        "qa_review_without_draft_after_six_and_previous_line": 811,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "safe_to_mount_as_final_overlay": False,
        "production_translations_modified": False,
        "existing_QA_bundle_stages_modified": False,
        "official_original_assets_modified": False,
        "nas_modified": False,
    }
    (tmp / "manifest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf8",
    )
    os.replace(tmp, dest)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

