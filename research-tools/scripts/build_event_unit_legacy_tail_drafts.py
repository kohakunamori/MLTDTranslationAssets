#!/usr/bin/env python3
"""Source-bound Simplified Chinese *drafts* for 47 frozen event-unit legacy-only cues.

These are editable reviewer suggestions, not production machine rows, approvals or bundles.
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

from scripts.build_event_unit_review_pack import OUTPUT as REVIEW_ROOT, sha_file
from scripts.localization_version_identity import version_identity
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id
from scripts.build_event_unit_review_pack import SNAPSHOT, INDEX, BUILD

OUTPUT = BUILD / "audits/event-unit-tail-draft-client-9.0.200-assets-1077100"

# Individually considered cues.  Preserve the original source EXACTLY;
# a changed frozen queue causes a hard failure rather than a positional merge.
DRAFT_BY_SOURCE = {
    "数十分後……": "几十分钟后……",
    "1時間後……": "1小时后……",
    "数分後……": "几分钟后……",
    "翌日……": "次日……",
    "えっ……！？": "咦……！？",
    "10分後……": "10分钟后……",
    "ガチャッ！": "咔嗒！",
    "パチパチパチパチ……！": "啪啪啪啪……！",
    "数時間後……": "几小时后……",
    "特訓の成果": "特训的成果",
    "……えっ？": "……咦？",
    "はぁ…………。": "唉…………。",
    "えっ！？": "咦！？",
    "えっ……？": "咦……？",
    "よろしくお願いします！": "请多多关照！",
    "そうですね……。": "是啊……。",
    "……はぁ……。": "……唉……。",
    "あっ！": "啊！",
    "はれっ！？": "咦！？",
    "30分後……": "30分钟后……",
    "じーっ……。": "（盯着看）……。",
    "……はぁ。": "……唉。",
    "そ、そうですね……。": "说、说得也是……。",
    "はぁ、はぁ、はぁ……。": "呼、呼、呼……。",
    "う～ん……。": "嗯……。",
    "ええーっ！？": "咦——！？",
    "お疲れさまでした～。": "辛苦了～。",
    "なるほど……。": "原来如此……。",
    "ん……？": "嗯……？",
    "えいっ！": "嘿！",
    "20分後……": "20分钟后……",
    "ああっ……！？": "啊……！？",
    "え～っ！？": "咦～！？",
    "おはようございま～す♪": "早上好～♪",
    "かんぱーい！": "干杯！",
    "2時間後……": "2小时后……",
    "わあ～っ……！": "哇～……！",
    "わぁっ！": "哇！",
    "受け取ってくださいっ！": "请收下！",
    "ひっ！？": "呀！？",
    "ええ～っ！": "咦——！",
    "え、でも……。": "咦，可是……。",
    "あれっ！？": "咦！？",
    "アイドルとして": "作为偶像",
    "自信を持って！": "要有自信！",
    "撮影開始": "开始拍摄",
    "そして、数時間後……": "然后，几小时后……",
}


def draft_rows(review_rows: list[dict]) -> tuple[list[dict], dict]:
    pending: dict[str, dict] = {}
    seen_source_ids: set[str] = set()
    for item in review_rows:
        sid, src = item["source_sha256"], item["source"]
        if sid != source_id(src) or sid in seen_source_ids:
            raise ValueError("review pack has stale/duplicate source SHA")
        seen_source_ids.add(sid)
        if item.get("review_status") != "pending" or item.get("release_gate") != "needs_independent_review":
            raise ValueError("review pack already modified/release-gated")
        if item["qa_verdict"] == "MISSING_MACHINE":
            if item.get("machine_candidate_unreviewed"):
                raise ValueError("missing-machine cue already has a machine translation")
            pending[src] = item
    if set(pending) != set(DRAFT_BY_SOURCE) or len(pending) != 47:
        raise ValueError("frozen 47-cue source universe differs from draft map")
    qa_counts = Counter()
    rows = []
    glossary = load_glossary(None)
    for source, translation in DRAFT_BY_SOURCE.items():
        item = pending[source]
        sid = item["source_sha256"]
        qa = evaluate_row(
            {"source_sha256": sid, "source": source},
            {"source_sha256": sid, "source": source, "translation": translation,
             "status": "agent_draft_unreviewed"}, glossary,
        )
        qa_counts[qa["qa_verdict"]] += 1
        rows.append({
            "source_sha256": sid, "source": source,
            "translation_draft": translation,
            "legacy_traditional_references_unreviewed":
                item["legacy_traditional_references_unreviewed"],
            "occurrences": item["occurrences"], "examples": item["examples"],
            "qa_verdict": qa["qa_verdict"], "issues": qa.get("issues", []),
            "status": "agent_draft_unreviewed", "release_gate": "needs_review",
            "semantic_accuracy_verified": False,
            "independent_review_complete": False,
            "safe_to_auto_promote": False,
        })
    return rows, dict(qa_counts)


def build(output_root: Path) -> dict:
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100", asset_index=INDEX
    )
    manifest = json.loads((REVIEW_ROOT / "review-pack-manifest.json").read_text(encoding="utf8"))
    review_path = REVIEW_ROOT / "review-queue.jsonl"
    if (manifest["version_identity"] != identity
        or manifest["review_queue_sha256"] != sha_file(review_path)
        or manifest["review_queue_unique"] != 1181
        or manifest["missing_machine_with_legacy_reference"] != 47
        or manifest["reviewed"] is not False
        or manifest["safe_to_mount_as_final_overlay"] is not False):
        raise ValueError("stale or release-gated event-unit review pack")
    review = [json.loads(line) for line in review_path.read_text(encoding="utf8").splitlines()]
    rows, verdicts = draft_rows(review)
    out = output_root.resolve()
    if not out.is_relative_to((BUILD / "audits").resolve()):
        raise ValueError("drafts may only be written in build/localization-90200/audits")
    if out.exists():
        raise FileExistsError(f"draft root already exists: {out}")
    temporary = out.with_name(out.name + ".incomplete")
    if temporary.exists():
        raise FileExistsError(f"incomplete draft root: {temporary}")
    temporary.mkdir(parents=True)
    path = temporary / "47-source-bound-drafts.jsonl"
    with path.open("w", encoding="utf8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "schema_version": 1,
        "kind": "event-unit-47-legacy-only-agent-drafts-not-accepted",
        "version_identity": identity,
        "review_pack_manifest_sha256": sha_file(REVIEW_ROOT / "review-pack-manifest.json"),
        "review_queue_sha256": sha_file(review_path),
        "draft_filename": path.name, "draft_sha256": sha_file(path),
        "draft_unique": len(rows), "qa_verdicts": verdicts,
        "candidate_is_machine_translation": False,
        "semantic_accuracy_verified": False,
        "independent_review_complete": False,
        "safe_to_mount_as_final_overlay": False,
        "release_gate": "needs_independent_review",
        "production_translation_files_modified": False,
        "nas_modified": False,
    }
    (temporary / "draft-manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    os.replace(temporary, out)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

