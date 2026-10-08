#!/usr/bin/env python3
"""Frozen source-bound Event-unit proper-name and contaminated-title drafts.

A small deterministic correction *proposal* for nine already-REVIEW sources.
Not an independently reviewed release ledger and never a producer overwrite.
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
INPUT_ROOT = (
    BUILD / "audits/event-unit-qa-cue-recheck-client-9.0.200-assets-1077100"
)
AUTHORITIES = ROOT / "localization/quality/authoritative-terms.json"
SNAPSHOT = BUILD / "jp-gtx-cache-snapshot.json"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
GLOSSARY = ROOT / "localization/quality/glossary.json"
DEST = BUILD / "audits/event-unit-name-title-repair-drafts-client-9.0.200-assets-1077100"

# SHA -> (expected JP-name source, exact old fragment, exact new fragment)
# One entry (the title) is deliberately exact whole-string replacement; ordinary
# Japanese name pronunciation puzzles and language jokes remain pending review.
NAME_REPAIRS = {
    "1d6c84e75f24cdd9833ea9e78859666cf5a7c9f353dddcce6597eae9a3a38618":
        ("木下ひなた", "ひなた", "日向"),
    "351911dd428b4976439692560f6e47fdd3952f9fd10364fb3abbfb549b26e7b9":
        ("三浦あずさ", "あずさ", "梓"),
    "38967a87a8df0d41ad3983a08afe93e196a8235ff72bac562d967a70477a625a":
        ("馬場このみ", "このみ", "木实"),
    "810f9bd361514b591c11d7705a3a39c63d97c662f245df06ffe4138ae615e638":
        ("馬場このみ", "这个み", "木实"),
    "81e877be80774f3e09db060db620589783c2fb808c8ad57430cefa06841bb0cb":
        ("馬場このみ", "这个み", "木实"),
    "de309349cc5b7ba269f18e28682c0adff778aa6dc59848c5a840bf7f9e92c62a":
        ("木下ひなた", "ひなた", "日向"),
    "deb3ae0d400afd46a886823baf6fdd477a583f819d941ec8278ca6615b345ea7":
        ("徳川まつり", "まつり", "茉莉"),
    "e50ef8c4ace7df0bbc39ad9521dbb269140cf112aae80c42dcc6af553c9d9aef":
        ("徳川まつり", "まつり", "茉莉"),
}
TITLE_SHA = "3d93bb2db1c2918d8b34a9dc8444011745e5ee95d06ede5e8ab872d2cfdfc910"
TITLE_SOURCE = "アイデンティティ★クライシス"
TITLE_DRAFT = "身份认同★危机"


def draft_rows(rows: list[dict], authority: dict) -> tuple[list[dict], dict]:
    selected = {}
    for item in rows:
        sid, src = item.get("source_sha256"), item.get("source")
        if not isinstance(src, str) or sid != source_id(src) or sid in selected:
            raise ValueError("stale or duplicate Event-unit reviewer source SHA")
        selected[sid] = item
    needed = set(NAME_REPAIRS) | {TITLE_SHA}
    if not needed.issubset(selected) or len(needed) != 9:
        raise ValueError("frozen nine name/title source SHA entries missing")
    official = authority.get("entries", {})
    glossary = load_glossary(None)
    output = []
    verdicts: Counter[str] = Counter()
    for sid in sorted(needed):
        item = selected[sid]
        original = item["source"]
        previous = item["machine_candidate_unreviewed"]
        if (item.get("qa_verdict") != "REVIEW"
            or item.get("review_status") != "pending"
            or item.get("release_gate") != "needs_independent_review"
            or not any(i["code"] == "japanese_kana_residual"
                       for i in item["issues"])
            or not isinstance(previous, str) or not previous
            or not item.get("examples") or item.get("occurrences", 0) < 1):
            raise ValueError(f"review case not frozen/pending: {sid}")
        if sid == TITLE_SHA:
            if (original != TITLE_SOURCE
                or "Wait title natural" not in previous
                or "crisis?" not in previous):
                raise ValueError("contaminated source title mismatch")
            fixed = TITLE_DRAFT
            authority_name = None
            kind = "machine_meta_commentary_removed_title_draft"
        else:
            official_name, old_fragment, fixed_fragment = NAME_REPAIRS[sid]
            authoritative = official.get(official_name)
            if (not isinstance(authoritative, dict)
                or authoritative.get("category") != "character_name"
                or authoritative.get("evidence") != "official_legacy_zhcn_exact_source"
                or authoritative.get("target", "").endswith(fixed_fragment) is False
                or authoritative.get("evidence_count", 0) < 1
                or old_fragment not in previous
                or previous.count(old_fragment) != 1
                or not any(jp in original for jp in {
                    "木下ひなた": ["ひなた"],
                    "三浦あずさ": ["あずさ"],
                    "馬場このみ": ["このみ"],
                    "徳川まつり": ["まつり"],
                }[official_name])):
                raise ValueError(f"stale authoritative name/old translation: {sid}")
            fixed = previous.replace(old_fragment, fixed_fragment, 1)
            authority_name = official_name
            kind = "official_legacy_zhcn_character_short_name_projection_draft"
        if fixed == previous:
            raise ValueError("unmodified translation marked as correction")
        qa = evaluate_row(
            {"source_sha256": sid, "source": original,
             "examples": item["examples"], "occurrences": item["occurrences"]},
            {"source_sha256": sid, "source": original, "translation": fixed,
             "status": "agent_draft_unreviewed"},
            glossary,
        )
        if qa["qa_verdict"] not in ("PASS", "REVIEW"):
            raise ValueError(f"draft newly rejected: {sid}")
        if any(i["code"] == "japanese_kana_residual" for i in qa["issues"]):
            raise ValueError(f"draft still leaks problematic kana: {sid}")
        verdicts[qa["qa_verdict"]] += 1
        output.append({
            "source_sha256": sid, "source": original,
            "machine_candidate_unreviewed": previous,
            "translation_draft": fixed,
            "authoritative_character_full_name": authority_name,
            "draft_method": kind,
            "original_qa_issues": item["issues"],
            "qa_verdict_draft": qa["qa_verdict"],
            "qa_issues_draft": qa["issues"],
            "examples": item["examples"], "occurrences": item["occurrences"],
            "status": "agent_draft_unreviewed", "review_status": "pending",
            "independent_review_complete": False,
            "semantic_accuracy_verified": False,
            "release_gate": "needs_independent_review",
            "safe_to_mount_as_final_overlay": False,
        })
    return output, dict(verdicts)


def build(dest: Path = DEST) -> dict:
    identity = version_identity(
        SNAPSHOT, client_version="9.0.200", asset_version="1077100",
        asset_index=INDEX,
    )
    origin_manifest = INPUT_ROOT / "manifest.json"
    origin = json.loads(origin_manifest.read_text(encoding="utf8"))
    still = INPUT_ROOT / "still-review.jsonl"
    if (origin.get("version_identity") != identity
        or origin.get("files_sha256", {}).get(still.name) != sha_file(still)
        or origin.get("current_verdicts") !=
            {"PASS": 12275, "REVIEW": 901, "REJECT": 1}
        or origin.get("independent_review_complete") is not False
        or origin.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("frozen 901-source pending reviewer queue changed")
    authority = json.loads(AUTHORITIES.read_text(encoding="utf8"))
    rows, verdicts = draft_rows(read_jsonl(still), authority)
    if len(rows) != 9 or verdicts != {"PASS": 8, "REVIEW": 1}:
        raise ValueError("unexpected frozen nine-draft QA result")
    dest = dest.resolve()
    if not dest.is_relative_to((BUILD / "audits").resolve()):
        raise ValueError("unreviewed correction drafts must stay inside audits")
    if dest.exists():
        raise FileExistsError(f"immutable nine-draft folder already exists: {dest}")
    temporary = dest.with_name(dest.name + ".incomplete")
    if temporary.exists():
        raise FileExistsError(f"uncommitted nine-draft output: {temporary}")
    temporary.mkdir(parents=True)
    path = temporary / "nine-source-name-title-repair-drafts.jsonl"
    with path.open("w", encoding="utf8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "schema_version": 1,
        "kind": "event-unit-nine-unreviewed-name-title-repair-drafts",
        "version_identity": identity,
        "review_queue_sha256": sha_file(still),
        "review_manifest_sha256": sha_file(origin_manifest),
        "authoritative_terms_sha256": sha_file(AUTHORITIES),
        "glossary_sha256": sha_file(GLOSSARY),
        "draft_file": path.name,
        "draft_file_sha256": sha_file(path),
        "source_unique": len(rows),
        "name_draft_unique": len(NAME_REPAIRS),
        "contaminated_title_draft_unique": 1,
        "qa_verdicts_draft": verdicts,
        "independent_review_complete": False,
        "semantic_accuracy_verified": False,
        "release_gate": "not_evaluated",
        "safe_to_mount_as_final_overlay": False,
        "production_translations_modified": False,
        "existing_QA_stage_modified": False,
        "nas_modified": False,
    }
    (temporary / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8",
    )
    os.replace(temporary, dest)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.output_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

