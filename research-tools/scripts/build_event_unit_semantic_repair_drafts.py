#!/usr/bin/env python3
"""Source-bound Event-unit translation-quality correction DRAFTS, not a release.

21 intentionally authored Japanese->Simplified Chinese candidate corrections.
No prior candidate, reviewer decision, producer JSONL or Unity bundle is edited.
"""
from __future__ import annotations

import argparse
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
INPUT_ROOT = BUILD / "audits/event-unit-plural-qa-recheck-client-9.0.200-assets-1077100"
INPUT = INPUT_ROOT / "still-review.jsonl"
INPUT_MANIFEST = INPUT_ROOT / "manifest.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
QUALITY = ROOT / "scripts/mltd_translation_quality.py"
GLOSSARY = ROOT / "localization/quality/glossary.json"
DEST = BUILD / "audits/event-unit-semantic-repair-drafts-client-9.0.200-assets-1077100"
INPUT_SHA = "7d4527281b38ee4277a4606bbf07485038c2d7da2044649df4025b050f18e7dd"
GROUPS = {
    "group_cardinality_and_group_pronouns": {
        "5ca975e810e4ef": "毕竟是4个人一起跳舞，就算出了什么状况，也能彼此照应嘛。",
        "e0f664793d0d4a": "就这么定了！\n5个人一定要一起逃出去！",
        "364d4d87abf5ab": "既然也洗过澡了……\n那就3个人一起开个会，商量明天的工作吧！",
        "84cabd6103405b": "呐，现在3个人一起去逛街吧！",
        "a790bbf126ac70": "而且，能在这里3个人一起练习，我也很开心。……好了，去彩排吧♪",
    },
    "unwarranted_or_awkward_explicit_subject": {
        "00cfa83eb902f6": "好——，那就开始练习吧！\n啊，放着之前那首歌练可以吗？",
        "01a4b452151342": "「到了！\n接下来请大家在这座城镇寻找温泉之外的魅力！」",
        "01d61698688a56": "那就待会儿见～！",
        "4c659767f9ef1a": "……呵呵，是啊。那就出发吧。\n真期待你们俩会选什么♪",
    },
    "sentence_final_adversative_and_name": {
        "0f47c1f46f1a73": "接下来，就只等工作结束的木实回来了，不过……",
        "00fb87e641aa69": "是……对不起！\n我知道现在不适合提这件事，可是……",
        "09c6b848b501ac": "当然，不过还是得先向对方确认这样是否可以。",
    },
    "indefinite_semantic_repair": {
        "02dbfeded00acb": "……不。对不起，真。\n还是能请你再多揉一会儿面团吗？",
        "0433b22b811ba": "哇，对不起，茉莉！\n我刚才那意见可能有点多余！",
        "048f9ea98e5d37": "明明恋爱经验不多，却说了些暧昧的话吧？也会有这种时候嘛！",
        "04c26c68f7283a": "嗯，我觉得不用太在意。那孩子一定只是被育的演技震撼到了。",
        "0a6c9ccc65cc7f": "茜当时在一座公园里。\n她面前有好多猫……",
        "0b1723ded4227d": "几天后。\n一档歌唱节目录制结束后……",
        "0e536695d4c037": "不过，今晚可是冠军争夺战，\n我本来还想赛后互相聊聊感想呢～。",
        "2080f2b9dc68a5": "哼哼，别客气，尽管点吧！",
        "31d0a9401a205d": "看了照片，说不定能让我稍微有点自信……也许还能有所收获……虽、虽然很害羞……",
    },
}
EXPECTED_OLD_ISSUE = {
    "group_cardinality_and_group_pronouns": "unsupported_first_person_plural_addition",
    "unwarranted_or_awkward_explicit_subject": "unsupported_first_person_plural_addition",
    "sentence_final_adversative_and_name": "sentence_final_adversative_missing",
    "indefinite_semantic_repair": "unsupported_indefinite_object_addition",
}


def candidate_rows(
    review_rows: list[dict], groups: dict[str, dict[str, str]] = GROUPS,
) -> tuple[list[dict], dict]:
    review = {}
    for row in review_rows:
        sid, source = row.get("source_sha256"), row.get("source")
        if (not isinstance(source, str) or sid != source_id(source)
            or sid in review):
            raise ValueError("stale/duplicate review source")
        review[sid] = row
    if len(review) != 860:
        raise ValueError("not the frozen second-round review population")
    drafts: list[dict] = []
    counts: Counter[str] = Counter()
    consumed = set()
    glossary = load_glossary(None)
    for group_name, items in groups.items():
        if group_name not in EXPECTED_OLD_ISSUE:
            raise ValueError("unknown manual repair category")
        if not isinstance(items, dict) or not items:
            raise ValueError("empty/invalid manual repair category")
        for prefix, zh in items.items():
            if (not isinstance(prefix, str) or not (12 <= len(prefix) <= 16)
                or any(ch not in "0123456789abcdef" for ch in prefix)):
                raise ValueError("manual source SHA prefix must be 14 hex characters")
            options = [sid for sid in review if sid.startswith(prefix)]
            if len(options) != 1:
                raise ValueError(f"draft original source SHA missing/ambiguous: {prefix}")
            sid = options[0]
            row = review[sid]
            if sid in consumed:
                raise ValueError("source SHA assigned more than one manual draft")
            consumed.add(sid)
            previous = row.get("machine_candidate_unreviewed")
            if (row.get("qa_verdict") != "REVIEW"
                or row.get("prior_qa_verdict") != "REVIEW"
                or row.get("release_gate") != "needs_independent_review"
                or row.get("review_status") != "pending"
                or row.get("independent_review_complete") is not False
                or row.get("semantic_accuracy_verified") is not False
                or row.get("safe_to_mount_as_final_overlay") is not False
                or len(row.get("issues", [])) != 1
                or row["issues"][0]["code"] != EXPECTED_OLD_ISSUE[group_name]
                or not isinstance(previous, str) or not previous
                or not isinstance(zh, str) or not zh.strip() or zh == previous
                or not row.get("examples") or row.get("occurrences", 0) < 1):
                raise ValueError(f"manual draft has changed source/status/old issue: {sid}")
            candidate = {
                "source_sha256": sid, "source": row["source"],
                "translation": zh, "status": "agent_draft_unreviewed",
            }
            evaluated = evaluate_row(
                {"source_sha256": sid, "source": row["source"],
                 "examples": row["examples"], "occurrences": row["occurrences"]},
                candidate,
                glossary,
            )
            if evaluated["qa_verdict"] != "PASS" or evaluated["issues"]:
                raise ValueError(f"new draft has deterministic QA issues: {sid}: {evaluated['issues']}")
            counts[group_name] += 1
            drafts.append({
                "source_sha256": sid, "source": row["source"],
                "machine_candidate_unreviewed": previous,
                "translation_draft": zh,
                "repair_reason": group_name,
                "prior_qa_verdict": row["qa_verdict"],
                "prior_qa_issues": row["issues"],
                "draft_qa_verdict": evaluated["qa_verdict"],
                "draft_qa_issues": evaluated["issues"],
                "examples": row["examples"], "occurrences": row["occurrences"],
                "status": "agent_draft_unreviewed",
                "review_status": "pending",
                "release_gate": "needs_independent_review",
                "independent_review_complete": False,
                "semantic_accuracy_verified": False,
                "safe_to_mount_as_final_overlay": False,
            })
    if len(drafts) != 21 or len(consumed) != 21:
        raise ValueError("expected 21 isolated semantic correction source IDs")
    drafts.sort(key=lambda x: x["source_sha256"])
    return drafts, dict(counts)


def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to((BUILD / "audits").resolve()):
        raise ValueError("draft output must be isolated under audits")
    if dest.exists():
        raise FileExistsError(f"immutable semantic draft directory exists: {dest}")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError(f"incomplete semantic draft directory exists: {temp}")
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100", asset_index=INDEX
    )
    prior = json.loads(INPUT_MANIFEST.read_text(encoding="utf8"))
    if (prior.get("version_identity") != identity
        or prior.get("file_hashes", {}).get(INPUT.name) != INPUT_SHA
        or sha_file(INPUT) != INPUT_SHA
        or prior.get("current_QA_verdicts") != {"PASS": 12316, "REVIEW": 860, "REJECT": 1}
        or prior.get("remaining_independent_review") != 1181
        or prior.get("safe_to_mount_as_final_overlay") is not False
        or prior.get("independent_review_complete") is not False):
        raise ValueError("second-round Event-unit review lineage changed")
    drafts, groups = candidate_rows(read_jsonl(INPUT))
    temp.mkdir(parents=True)
    output = temp / "21-source-bound-semantic-correction-drafts.jsonl"
    with output.open("w", encoding="utf8", newline="\n") as stream:
        for draft in drafts:
            stream.write(json.dumps(draft, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "schema_version": 1,
        "kind": "event-unit-21-contextual-semantic-agent-drafts-NOT-REVIEWED",
        "version_identity": identity,
        "source_review_manifest_sha256": sha_file(INPUT_MANIFEST),
        "source_review_queue_sha256": sha_file(INPUT),
        "quality_rules_sha256": sha_file(QUALITY),
        "glossary_sha256": sha_file(GLOSSARY),
        "draft_file": output.name,
        "draft_sha256": sha_file(output),
        "draft_unique": len(drafts),
        "draft_occurrences": sum(x["occurrences"] for x in drafts),
        "repair_reasons": groups,
        "draft_deterministic_qa_verdicts": {"PASS": len(drafts)},
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "safe_to_mount_as_final_overlay": False,
        "release_gate": "needs_independent_review",
        "production_translations_modified": False,
        "existing_852_bundle_QA_stage_modified": False,
        "nas_modified": False,
    }
    (temp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
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

