#!/usr/bin/env python3
"""12 contextual Event-unit correction drafts, source-bound and NEVER released.

One honest QA-REVIEW remains because the existing bad-meaning heuristic does
not recognize natural Chinese 坏事. Do not rewrite it just to game regex QA.
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
REVIEW_ROOT = BUILD / "audits/event-unit-unified-review-client-9.0.200-assets-1077100"
REVIEW_FILE = REVIEW_ROOT / "review-worklist.jsonl"
REVIEW_MANIFEST = REVIEW_ROOT / "manifest.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
GLOSSARY = ROOT / "localization/quality/glossary.json"
QUALITY = ROOT / "scripts/mltd_translation_quality.py"
DEST = BUILD / "audits/event-unit-followup-12-semantic-drafts-client-9.0.200-assets-1077100"
INPUT_SHA = "c9c89a8337489cbee0d40f29248fdc7372f7c61b94f60c625753770bf64edc2e"

# Frozen exact prior MACHINE text SHA protects against unnoticed source-context
# or prior-translation edits. SHA prefix resolves within the immutable worklist,
# then the complete resolved Japanese source SHA is stored in every draft row.
DRAFTS = {
    # bad-meaning gate does not recognise natural 坏事. Preserve QA REVIEW.
    "362ac5de7af20d": (
        "9ef856639d8a3746bd3e35ead1f8c921afa417653ea90bbc00da314ba54ecc6a",
        "呜，我抽这种签总是特别准……\n偏偏准的都是坏事……！一定要避开下下签～！",
        "fix_negative_luck_context_keep_rule_review",
    ),
    "aaf0ef98b223ff": (
        "791f179b5fabfe48b3ec6155458c762af8a57cfc6ebc93a1a2f7919a532d1e58",
        "是、是吗？要是能更落落大方一点就好了……嗯，这样也不错！再试试别的吧。",
        "mild_acceptance_not_resignation",
    ),
    "84abb4565c586a": (
        "e2d9f1b90b1a50f9aaa477efde240102c1d8dafdad90df383106d353e65ebe27",
        "诶——！果然是这样啊！\n我还以为下次就能帅气地为大家加油了——！！",
        "spoken_ouen_as_encouragement",
    ),
    "d15588f0e2eadc": (
        "ef1d5c6b07941455ef33e16e7be80f6cf6dcbb6133b1d40dee5da7286ba94061",
        "诶诶，原来是这样吗！？\n都说是助威队长了，我还以为……",
        "spoken_ouen_group_role",
    ),
    "22a958a1aaadb4": (
        "c7041f43116210ff90efcd9da51c23c8ec850cd011a4960a81a030fc4ea09230",
        "好，就这么定了♪ 演唱会现场和SNS都要继续这样吸睛～！",
        "remove_untranslated_bae_kana",
    ),
    "043c12842fbdcd": (
        "ce286f01f2a2ae2e9f58fd49a6b35187b6c35161a99990e2d5c80715722bc077",
        "不愧是桃子。既然决定了，\n这就去准备火锅吧。",
        "avoid_unnecessary_explicit_subject",
    ),
    "0c1fea741b4c5a": (
        "8f0ef3916ab2f7813c53cf518c4ccfb66f3b66416e52ce4a5d7d36898ff7b763",
        "在ROCO的提议下，决定制作一份用于电视剧宣传的报纸。",
        "avoid_unnecessary_explicit_subject",
    ),
    "119e5e0c649b41": (
        "bdbe325601c34b52bd4594b858943d53864108c41f39be3a6daca2f631d241f6",
        "是的！还得到了“你们去了海外也能行哦”这样的鼓励……",
        "avoid_invented_speaker_gender_and_explicit_subject",
    ),
    "0f598c8ad1d6d7": (
        "0e5bca4b3359fb48c59b277ec4ed89a308b843dcb3fc3e2b182a7a5aba962d78",
        "哈啊～……\n律子姐真是让人没辙啊～……！",
        "idiomatic_shes_too_much_for_me",
    ),
    "0e526c40ffe6c5": (
        "503de89b9e8746bf8480e34c891eac8f1102259b88664dea22bd1916733c4864",
        "还得想想怎样说才像是在聊恋爱电影呢……",
        "avoid_unnecessary_explicit_subject",
    ),
    "01d4c306484636": (
        "b27a0ea8cd9faf3405aebe9fc03f3e33fa591a01a01bcd7575e66afdf5d584a0",
        "诶？怎么突然这么说？不过，才没有那回事呢。",
        "soft_sentence_final_adversative",
    ),
    "00d6a0cd71d293": (
        "8e10ddda78afe9a4ae5854e82fd48a622228084318339fd117c1de4ca82a3391",
        "原来茱莉亚很在意最后咬到舌头的事啊。\n那个……我倒是觉得特别可爱……。",
        "soft_sentence_final_adversative",
    ),
}


def candidate_rows(rows: list[dict], mapping: dict = DRAFTS) -> tuple[list[dict], dict]:
    selected: dict[str, dict] = {}
    for row in rows:
        sid, source = row.get("source_sha256"), row.get("source")
        if (not isinstance(source, str) or sid != source_id(source)
            or sid in selected):
            raise ValueError("review worklist duplicate or mismatched SHA")
        selected[sid] = row
    if len(selected) != 1181:
        raise ValueError("source-bound review population changed")
    glossary = load_glossary(None)
    output = []
    counts: Counter[str] = Counter()
    used = set()
    for prefix, spec in mapping.items():
        if (len(prefix) != 14 or not isinstance(spec, tuple)
            or len(spec) != 3):
            raise ValueError("frozen translation source prefix/spec malformed")
        matches = [sid for sid in selected if sid.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f"source SHA prefix cannot resolve uniquely: {prefix}")
        sid = matches[0]
        if sid in used:
            raise ValueError("two drafts assigned to same Japanese source")
        used.add(sid)
        row = selected[sid]
        old_sha, new_zh, reason = spec
        old = row.get("machine_candidate_unreviewed")
        if (row.get("review_bucket") != "qa_review_without_draft"
            or row.get("current_machine_qa_verdict") != "REVIEW"
            or not row.get("current_machine_qa_issues")
            or row.get("review_status") != "pending"
            or row.get("release_gate") != "needs_independent_review"
            or row.get("independent_review_complete") is not False
            or row.get("semantic_accuracy_verified") is not False
            or row.get("safe_to_mount_as_final_overlay") is not False
            or row.get("agent_correction_draft_unreviewed") is not None
            or not isinstance(old, str) or not old
            or hashlib.sha256(old.encode("utf8")).hexdigest() != old_sha
            or not isinstance(new_zh, str) or not new_zh.strip()
            or old == new_zh or not row.get("examples")
            or not isinstance(row.get("occurrences"), int)
            or row["occurrences"] != 1):
            raise ValueError(f"source-bound old review/translation changed: {sid}")
        qa = evaluate_row(
            {"source_sha256": sid, "source": row["source"],
             "examples": row["examples"], "occurrences": row["occurrences"]},
            {"source_sha256": sid, "source": row["source"],
             "translation": new_zh, "status": "agent_draft_unreviewed"},
            glossary,
        )
        if qa["qa_verdict"] == "REJECT":
            raise ValueError(f"source-bound correction has hard QA issue: {sid}")
        if sid.startswith("362ac5de7af20d"):
            if (qa["qa_verdict"] != "REVIEW"
                or [x["code"] for x in qa["issues"]] !=
                    ["bad_meaning_semantics_missing"]):
                raise ValueError("do not fake QC PASS for natural Chinese 坏事")
        elif qa["qa_verdict"] != "PASS" or qa["issues"]:
            raise ValueError(f"nonnegative correction QA should PASS: {sid}")
        old_codes = {x["code"] for x in row["current_machine_qa_issues"]}
        new_codes = {x["code"] for x in qa["issues"]}
        if not new_codes.issubset(old_codes):
            raise ValueError(f"new reviewer issue in correction draft: {sid}")
        counts[qa["qa_verdict"]] += 1
        output.append({
            "source_sha256": sid, "source": row["source"],
            "machine_candidate_sha256": old_sha,
            "machine_candidate_unreviewed": old,
            "translation_draft": new_zh,
            "repair_reason": reason,
            "examples": row["examples"], "occurrences": row["occurrences"],
            "original_machine_QA_verdict": row["current_machine_qa_verdict"],
            "original_machine_QA_issues": row["current_machine_qa_issues"],
            "draft_qa_verdict": qa["qa_verdict"],
            "draft_qa_issues": qa["issues"],
            "status": "agent_draft_unreviewed",
            "review_status": "pending",
            "release_gate": "needs_independent_review",
            "independent_review_complete": False,
            "semantic_accuracy_verified": False,
            "safe_to_mount_as_final_overlay": False,
        })
    if (len(output) != 12 or
        counts != Counter({"PASS": 11, "REVIEW": 1})):
        raise ValueError("unexpected frozen contextual 12-source draft population")
    output.sort(key=lambda x: x["source_sha256"])
    return output, dict(counts)


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to((BUILD / "audits").resolve()):
        raise ValueError("draft must remain in isolated audits")
    if dest.exists():
        raise FileExistsError("immutable followup draft already exists")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError("incomplete followup draft already exists")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    source_manifest = json.loads(REVIEW_MANIFEST.read_text(encoding="utf8"))
    if (source_manifest.get("version_identity") != identity
        or source_manifest.get("review_worklist_sha256") != INPUT_SHA
        or sha_file(REVIEW_FILE) != INPUT_SHA
        or source_manifest.get("still_requires_independent_semantic_review") != 1181
        or source_manifest.get("review_buckets", {}).get(
            "qa_review_without_draft") != 830
        or source_manifest.get("independent_review_complete") is not False
        or source_manifest.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("frozen unified reviewer source changed")
    proposals, qa_counts = candidate_rows(read_jsonl(REVIEW_FILE))
    temp.mkdir(parents=True)
    output = temp / "12-source-bound-corrections.jsonl"
    with output.open("w", encoding="utf8", newline="\n") as writer:
        for item in proposals:
            writer.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "schema_version": 1,
        "kind": "event-unit-12-followup-source-bound-contextual-agent-drafts",
        "version_identity": identity,
        "unified_review_manifest_sha256": sha_file(REVIEW_MANIFEST),
        "unified_review_worklist_sha256": sha_file(REVIEW_FILE),
        "quality_rules_sha256": sha_file(QUALITY),
        "glossary_sha256": sha_file(GLOSSARY),
        "draft_file": output.name,
        "draft_sha256": sha_file(output),
        "draft_unique": len(proposals),
        "draft_qa_verdicts": qa_counts,
        "known_rule_false_positive_not_autocleared": 1,
        "source_still_missing_targeted_draft_before_this_batch": 830,
        "source_still_missing_targeted_draft_after_this_batch": 818,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "safe_to_mount_as_final_overlay": False,
        "release_gate": "needs_independent_review",
        "producer_translations_modified": False,
        "previous_852_QA_stage_modified": False,
        "official_assets_modified": False,
        "nas_modified": False,
    }
    (temp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf8",
    )
    os.replace(temp, dest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

