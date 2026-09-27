#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turn exported offline human image review CSV into SHA-bound staging approvals.

An approval of one reconstructed picture applies to its *exact raw-PNG SHA*
duplicates only; any rejection for that picture blocks the whole group.
Every CSV row is tied to the current exact bundle/path_id/source/edited hashes.
This does NOT edit or create Unity bundles; it never auto-accepts model results.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path

DEFAULT_WORK = Path(__file__).resolve().parents[2] / "work/image-localization-25"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(s) for s in path.read_text(encoding="utf-8-sig").splitlines()
            if s.strip()]


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing")
    with tmp.open("w", encoding="utf-8", newline="\n") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--review-csv", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args()
    work = args.work.resolve()
    prep = work / "release-prep"
    audit = json.loads((prep / "report.json").read_text(encoding="utf-8"))
    if (audit["errors"] or audit["generated_unique"] <= 0 or
        audit["unity_bundles_modified"] or audit["ready_to_install"]):
        raise ValueError("Missing/unsafe image release-prep verification report")
    candidates = read_jsonl(prep / "verified-candidates.jsonl")
    by_id = {row["source_id"]: row for row in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("Duplicate source locators in verified candidate manifest")
    by_task = defaultdict(list)
    for row in candidates:
        by_task[row["task_id"]].append(row)
    decisions: dict[str, dict] = {}
    required = {"id", "bundle", "texture_path_id", "decision", "original_sha256",
                "edited_sha256", "normalized_sha256", "date"}
    with args.review_csv.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("Not an exported MLTD image review CSV")
        for line in reader:
            status = line["decision"].strip().lower()
            if status not in {"approve", "reject", "pending", "unreviewed"}:
                raise ValueError("Unknown human review decision")
            if status not in {"approve", "reject"}:
                continue
            identity = line["id"]
            if identity in decisions:
                raise ValueError("Duplicate or conflicting decision for " + identity)
            if identity not in by_id:
                raise ValueError("Review references no verified current PNG: " + identity)
            row = by_id[identity]
            if (line["bundle"] != row["bundle"] or
                line["texture_path_id"] != str(row["texture_path_id"]) or
                line["original_sha256"] != row["original_png_sha256"] or
                line["edited_sha256"] != row["restored_png_sha256"] or
                line["normalized_sha256"] != row["restored_png_sha256"]):
                raise ValueError("Stale/wrong original, output SHA or asset identity: " + identity)
            if not line["date"].strip():
                raise ValueError("Missing review date: " + identity)
            decisions[identity] = line

    prepared: list[dict] = []
    status = Counter()
    for task_id, members in sorted(by_task.items()):
        ratings = [decisions[r["source_id"]] for r in members
                   if r["source_id"] in decisions]
        if any(r["decision"] == "reject" for r in ratings):
            status["rejected_unique"] += 1
            continue
        approved = [r for r in ratings if r["decision"] == "approve"]
        if not approved:
            status["unreviewed_unique"] += 1
            continue
        sources = {r["original_png_sha256"] for r in members}
        generated = {r["restored_png_sha256"] for r in members}
        if len(sources) != 1 or len(generated) != 1:
            raise ValueError("Same task does not share identical frozen pixels: " + task_id)
        for item in members:
            prepared.append({
                **item,
                "review_status": "approved_for_staging_not_installed",
                "reviewed_source_id": approved[0]["id"],
                "reviewed_at": approved[0]["date"],
                "review_note": approved[0].get("note", ""),
                "review_csv": str(args.review_csv.resolve()),
            })
        status["approved_unique"] += 1

    output = (args.output or prep / "approved-for-staging.jsonl").resolve()
    write(output, prepared)
    summary = {
        "approved_unique": status["approved_unique"],
        "approved_texture_locators": len(prepared),
        "unreviewed_unique": status["unreviewed_unique"],
        "rejected_unique": status["rejected_unique"],
        "model_blocked_unique": audit["model_blocked_unique"],
        "additional_uncertain_unique": audit["uncertain_unique"],
        "unmapped_unique": audit["unmapped_unique"],
        "unity_bundles_modified": 0,
        "source_bound_staging": str(output),
    }
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
