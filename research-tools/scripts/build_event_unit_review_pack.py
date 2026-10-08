#!/usr/bin/env python3
"""Build a source-bound *review-only* event-unit subtitle pack for frozen 1077100.

The 47 legacy-only items may include old Traditional Chinese references.  Such
references are evidence for reviewers, never machine-complete or release-ready.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build/localization-90200"
STAGE = BUILD / "staging-event-unit-qa"
QUEUE = BUILD / "event-unit-translation-queue.jsonl"
QA = STAGE / "event-unit-QA-candidate-audit.json"
MANIFEST = STAGE / "event-unit-QA-candidate-manifest.json"
LEGACY = BUILD / "legacy-zh-translations.jsonl"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
OUTPUT = BUILD / "audits/event-unit-review-pack-client-9.0.200-assets-1077100"


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_review_rows(
    queue_rows: list[dict], qa_rows: list[dict], legacy_rows: list[dict],
) -> tuple[list[dict], dict]:
    queue = {}
    for item in queue_rows:
        source, sid = item["source"], item["source_sha256"]
        if sid != source_id(source) or sid in queue:
            raise ValueError("duplicate/stale event-unit queue source SHA")
        queue[sid] = item
    qa = {}
    for item in qa_rows:
        sid = item["source_sha256"]
        if sid not in queue or sid in qa:
            raise ValueError("extra/duplicate event-unit QA source SHA")
        if "source" in item and item["source"] != queue[sid]["source"]:
            raise ValueError("QA source text differs from frozen queue")
        qa[sid] = item
    if set(qa) != set(queue):
        raise ValueError("QA audit does not cover the exact frozen event-unit queue")

    refs = defaultdict(set)
    missing = {sid for sid, item in qa.items() if item["qa_verdict"] == "MISSING_MACHINE"}
    for item in legacy_rows:
        source = item.get("source")
        if not isinstance(source, str):
            continue
        sid = source_id(source)
        if sid not in missing or item.get("status") != "official_legacy":
            continue
        ref = item.get("translation")
        if isinstance(ref, str) and ref.strip():
            refs[sid].add(ref)
    verdicts = Counter(item["qa_verdict"] for item in qa_rows)
    if set(verdicts) - {"PASS", "REVIEW", "REJECT", "MISSING_MACHINE"}:
        raise ValueError(f"unsupported QA verdicts: {sorted(verdicts)}")
    priority = {"REJECT": 0, "MISSING_MACHINE": 1, "REVIEW": 2}
    rows = []
    for sid, item in qa.items():
        verdict = item["qa_verdict"]
        if verdict == "PASS":
            continue
        q = queue[sid]
        if verdict in ("REJECT", "REVIEW") and not item.get("issues"):
            raise ValueError(f"missing QA issues on {sid}")
        if verdict == "MISSING_MACHINE" and item.get("translation"):
            raise ValueError(f"machine-missing event-unit source has translation: {sid}")
        rows.append({
            "source_sha256": sid,
            "source": q["source"],
            "occurrences": q["occurrences"],
            "examples": q["examples"],
            "qa_verdict": verdict,
            "issues": item.get("issues", []),
            "machine_candidate_unreviewed": item.get("translation", ""),
            "legacy_traditional_references_unreviewed": sorted(refs.get(sid, ())),
            "review_status": "pending",
            "release_gate": "needs_independent_review",
        })
    rows.sort(key=lambda x: (
        priority[x["qa_verdict"]], -x["occurrences"], x["source_sha256"]
    ))
    return rows, {
        "source_unique": len(queue),
        "qa_verdicts": dict(verdicts),
        "review_queue_unique": len(rows),
        "missing_machine_with_legacy_reference": sum(
            r["qa_verdict"] == "MISSING_MACHINE"
            and bool(r["legacy_traditional_references_unreviewed"]) for r in rows
        ),
        "issue_codes": dict(sorted(Counter(
            issue["code"] for r in rows for issue in r["issues"]
        ).items())),
    }


def build(output_root: Path) -> dict:
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100", asset_index=INDEX
    )
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (manifest.get("version_identity") != identity
        or manifest.get("source_queue_sha256") != sha_file(QUEUE)
        or manifest.get("release_gate") != "not_evaluated"
        or manifest.get("independent_reviewed") is not False
        or manifest.get("safe_to_mount_as_final_overlay") is not False
        or manifest.get("source_unique") != 13177
        or manifest.get("bundles_scanned") != 852):
        raise ValueError("event-unit QA staging manifest is stale or wrongly release-gated")
    queue_rows = read_jsonl(QUEUE)
    qa_rows = json.loads(QA.read_text(encoding="utf-8"))
    if Counter(x["qa_verdict"] for x in qa_rows) != Counter(manifest["qa_verdicts"]):
        raise ValueError("event-unit QA audit differs from staging manifest")
    legacy = read_jsonl(LEGACY)
    rows, counts = build_review_rows(queue_rows, qa_rows, legacy)
    if counts["source_unique"] != 13177 or counts["review_queue_unique"] != 1181:
        raise ValueError("unexpected frozen event-unit QA population")
    output_root = output_root.resolve()
    if output_root.exists():
        raise FileExistsError(f"review pack already exists: {output_root}")
    if (output_root.is_relative_to((ROOT / "work/local-assets").resolve())
        or output_root.is_relative_to((BUILD / "overlay").resolve())
        or output_root.is_relative_to(STAGE.resolve())):
        raise ValueError("review pack output must not be placed inside a resource overlay")
    temporary = output_root.with_name(output_root.name + ".incomplete")
    if temporary.exists():
        raise FileExistsError(f"incomplete review pack already exists: {temporary}")
    temporary.mkdir(parents=True)
    queue_output = temporary / "review-queue.jsonl"
    with queue_output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "schema_version": 1,
        "kind": "event-unit-independent-review-worklist-only",
        "version_identity": identity,
        "source_queue_sha256": sha_file(QUEUE),
        "source_qa_audit_sha256": sha_file(QA),
        "source_staging_manifest_sha256": sha_file(MANIFEST),
        "legacy_reference_sha256": sha_file(LEGACY),
        "review_queue_sha256": sha_file(queue_output),
        "review_queue_name": queue_output.name,
        "reviewed": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "nas_modified": False,
        **counts,
    }
    (temporary / "review-pack-manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, output_root)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

