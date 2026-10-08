#!/usr/bin/env python3
"""Stage only deterministic-QA PASS FontRender bundles for a frozen version pair.

Explicitly NOT a release: outputs remain isolated from the production GTX
overlay until independent review and the official FontRender release gate pass.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.build_fontrender_overlay import modify_bundle
from scripts.extract_fontrender_localization import (
    extract_bundle,
    load_asset_index,
    sha256_file,
)
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import SOURCE_TEXT_RE, read_jsonl, translation_status_is_accepted
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id


def collect_candidates(paths: list[Path]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for path in paths:
        for row in read_jsonl(path):
            source = str(row.get("source", ""))
            translated = str(row.get("translation", ""))
            sid = str(row.get("source_sha256", ""))
            if not source or sid != source_id(source) or not translated:
                raise ValueError(f"{path}: source SHA or translation missing")
            if not translation_status_is_accepted(str(row.get("status", ""))):
                raise ValueError(f"{path}: unaccepted translation status {sid}")
            prior = result.get(sid)
            if prior is not None and (
                prior["source"] != source or prior["translation"] != translated
            ):
                raise ValueError(f"conflicting FontRender candidate: {sid}")
            result[sid] = row
    return result


def select_qa_pass(
    queue: list[dict],
    candidates: dict[str, dict],
    glossary: dict,
) -> tuple[dict[str, dict], list[dict]]:
    usable: dict[str, dict] = {}
    qa_rows: list[dict] = []
    for item in queue:
        sid = item["source_sha256"]
        if sid != source_id(item["source"]):
            raise ValueError(f"invalid frozen FontRender queue SHA: {sid}")
        candidate = candidates.get(sid)
        if candidate is None:
            qa_rows.append({"source_sha256": sid, "qa_verdict": "MISSING"})
            continue
        result = evaluate_row(item, candidate, glossary)
        qa_rows.append(result)
        if result["qa_verdict"] == "PASS":
            usable[sid] = candidate
    if len({x["source_sha256"] for x in queue}) != len(queue):
        raise ValueError("duplicate frozen FontRender source")
    return usable, qa_rows


def stage(
    *,
    snapshot: Path,
    client_version: str,
    asset_version: str,
    asset_index: Path,
    bundle_root: Path,
    queue_path: Path,
    translation_paths: list[Path],
    output_root: Path,
    glossary_path: Path | None = None,
) -> dict:
    identity = version_identity(
        snapshot,
        client_version=client_version,
        asset_version=asset_version,
        asset_index=asset_index,
    )
    index = load_asset_index(asset_index)
    bundles = sorted(
        logical for logical in index
        if logical.startswith("fontrender_") and logical.endswith(".unity3d")
    )
    if not bundles:
        raise ValueError("frozen index has no FontRender bundles")
    queue = read_jsonl(queue_path)
    candidates = collect_candidates(translation_paths)
    usable, qa_rows = select_qa_pass(queue, candidates, load_glossary(glossary_path))
    audit = Counter(x["qa_verdict"] for x in qa_rows)
    if audit["MISSING"]:
        raise ValueError(f"FontRender candidates incomplete: {audit['MISSING']} missing")
    if not usable:
        raise ValueError("no deterministic-QA PASS FontRender candidates")
    identity_path = output_root / "version-identity.json"
    if identity_path.exists():
        if json.loads(identity_path.read_text(encoding="utf-8")) != identity:
            raise ValueError("candidate output belongs to a different client/assets pair")
    else:
        scope_root = output_root / identity["scope"]
        if scope_root.exists() and any(scope_root.glob("*.unity3d")):
            raise ValueError("unidentified FontRender staging assets already exist")
        output_root.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(
            json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    source_ids: set[str] = set()
    stage_entries: list[dict] = []
    counts: Counter[str] = Counter()
    for logical in bundles:
        _hash, remote, size = index[logical][:3]
        source_path = bundle_root / str(remote)
        if not source_path.is_file() or source_path.stat().st_size != int(size):
            raise ValueError(f"missing/wrong frozen FontRender source: {logical}")
        occurrences = extract_bundle(logical, str(remote), source_path)
        source_ids.update(
            source_id(str(x["source"]))
            for x in occurrences if SOURCE_TEXT_RE.search(str(x["source"]))
        )
        output_path = output_root / identity["scope"] / str(remote)
        changes, verification = modify_bundle(source_path, output_path, usable)
        counts["bundles_scanned"] += 1
        if changes:
            counts["bundles_written"] += 1
            counts["fields_changed"] += len(changes)
            stage_entries.append({
                "logical": logical,
                "remote": str(remote),
                "source_path": str(source_path),
                "output_path": str(output_path),
                "qa_pass_candidate_not_released": True,
                **verification,
                "changes": changes,
            })
    if source_ids != {r["source_sha256"] for r in queue}:
        raise ValueError("frozen FontRender queue differs from current bundle sources")
    result = {
        "schema_version": 1,
        "kind": "mltd-fontrender-QA-candidate-only",
        "version_identity": identity,
        "release_gate": "not_evaluated",
        "human_reviewed": False,
        "safe_to_mount_as_final_overlay": False,
        "production_gtx_overlay_modified": False,
        "asset_index": str(asset_index),
        "asset_index_sha256": sha256_file(asset_index),
        "bundle_root": str(bundle_root),
        "output_root": str(output_root),
        "source_unique": len(source_ids),
        "qa_verdicts": dict(audit),
        "qa_pass_unique": len(usable),
        **dict(counts),
        "bundles": stage_entries,
    }
    for filename, obj in [
        ("fontrender-candidate-manifest.json", result),
        ("fontrender-candidate-qa.json", qa_rows),
    ]:
        dest = output_root / filename
        dest.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return {key: val for key, val in result.items() if key != "bundles"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--client-version", required=True)
    ap.add_argument("--asset-version", required=True)
    ap.add_argument("--asset-index", type=Path, required=True)
    ap.add_argument("--bundle-root", type=Path, required=True)
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--translations", type=Path, action="append", required=True)
    ap.add_argument("--output-root", type=Path, required=True)
    ap.add_argument("--glossary", type=Path)
    args = ap.parse_args()
    print(json.dumps(stage(
        snapshot=args.snapshot,
        client_version=args.client_version,
        asset_version=args.asset_version,
        asset_index=args.asset_index,
        bundle_root=args.bundle_root,
        queue_path=args.queue,
        translation_paths=args.translations,
        output_root=args.output_root,
        glossary_path=args.glossary,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
