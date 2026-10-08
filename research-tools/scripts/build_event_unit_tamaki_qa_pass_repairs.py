#!/usr/bin/env python3
"""Five Tamaki/玉环 character-name fixes discovered OUTSIDE the flagged QA queue.

FROZEN 13,177-source QA audit: 105 Japanese sources contain たまき,
95 existing Chinese candidates render 环 (not 玉环), six render 玉环,
of which ONE source already has a proposed correction in reviewer v3.
These five QA-PASS originals were not in the 1,181-source risk queue.
A deterministic QA PASS does NOT imply semantic review or release approval.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.build_event_unit_review_pack import sha_file
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row,load_glossary,source_id

BUILD=ROOT/"build/localization-90200"
AUDIT=BUILD/"staging-event-unit-qa-with-tail/event-unit-QA-candidate-audit.json"
REVIEW=BUILD/"audits/event-unit-unified-review-v3-client-9.0.200-assets-1077100/review-worklist.jsonl"
BASE=BUILD/"staging-event-unit-qa-with-tail/event-unit-QA-candidate-manifest.json"
SNAPSHOT=BUILD/"jp-gtx-cache-snapshot.json"
INDEX=ROOT/"work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
DEST=BUILD/"audits/event-unit-tamaki-five-QA-pass-repairs-client-9.0.200-assets-1077100"
AUDIT_SHA="f2c8e3b4d6fb8428abc62cb2fb0b9e7b7fb41c22be2ea7ff60a0f10187aec30d"
REVIEW_SHA="7f347b347fb7707077506f40e4783631bfc4eaefcf323b40e17841fb8008c36c"
BASE_SHA="c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
PREVIOUSLY_DRAFTED="40996b95780b24cc554ac6864db53c1e7d9513ee7a979c190847bc613251fc23"
EXPECTED={
"69b28b87477842","42b7527dc99d92","deef5f261d3934",
"4e6bf7a219b17c","96c6e863366e3f",
}

def candidate_rows(audit:list[dict],review:list[dict])->list[dict]:
    if len(audit)!=13177 or len(review)!=1181:
        raise ValueError("frozen QA/reviewer population mismatch")
    all_by_sha={}
    for row in audit:
        sid,source=row.get("source_sha256"),row.get("source")
        if not isinstance(source,str) or sid!=source_id(source) or sid in all_by_sha:
            raise ValueError("duplicate/stale original Japanese audit source")
        all_by_sha[sid]=row
    reviewer={}
    for item in review:
        sid=item.get("source_sha256")
        if sid in reviewer or sid!=source_id(item.get("source","")):
            raise ValueError("review source is duplicate/unbound")
        reviewer[sid]=item
    source_samples=[r for r in audit if "たまき" in r["source"] and isinstance(r.get("translation"),str)]
    wrong=[r for r in source_samples if "玉环" in r["translation"]]
    majority=[r for r in source_samples if "环" in r["translation"] and "玉环" not in r["translation"]]
    if (len(source_samples)!=105 or len(wrong)!=6 or len(majority)!=95
        or {r["source_sha256"][:14] for r in wrong}!={*EXPECTED,PREVIOUSLY_DRAFTED[:14]}):
        raise ValueError("frozen character-name scan changed, requires fresh audit")
    prior=reviewer.get(PREVIOUSLY_DRAFTED)
    if (not prior or prior.get("agent_correction_draft_unreviewed")!="环也渐渐理解这部戏的内容了！"
        or prior.get("review_bucket")!="qa_review_with_qa_pass_draft"
        or prior.get("independent_review_complete") is not False):
        raise ValueError("already-reviewed-queue Tamaki source no longer bound to prior draft")
    outputs=[]
    for original in wrong:
        sid=original["source_sha256"]
        if sid==PREVIOUSLY_DRAFTED:continue
        if (sid in reviewer or original["qa_verdict"]!="PASS"
            or original.get("issues")!=[]
            or original.get("occurrences")!=1 or len(original.get("examples",[]))!=1):
            raise ValueError("previously QA-PASS source overlaps high-risk reviewer")
        old=original["translation"]
        if not old.startswith("玉环") or old.count("玉环")!=1 or "玉环" in original["source"]:
            raise ValueError("unsafe name correction shape")
        zh=old.replace("玉环","环",1)
        qa=evaluate_row(
            {"source_sha256":sid,"source":original["source"],
             "examples":original["examples"],"occurrences":original["occurrences"]},
            {"source_sha256":sid,"source":original["source"],"translation":zh,
             "status":"agent_draft_unreviewed"},load_glossary(None))
        if qa["qa_verdict"]!="PASS" or qa["issues"]:
            raise ValueError("new name correction did not pass deterministic QA")
        outputs.append({
            "source_sha256":sid,"source":original["source"],
            "machine_candidate_unreviewed":old,"translation_draft":zh,
            "examples":original["examples"],"occurrences":1,
            "prior_machine_qa_verdict":"PASS","draft_qa_verdict":"PASS",
            "draft_qa_issues":[],"repair_reason":"tamaki_name_yuhuan_to_huan",
            "corpus_name_evidence":"95/105 kana sources already use 环 rather than 玉环; independent review still required",
            "corpus_name_evidence_is_official":False,
            "status":"agent_draft_unreviewed","review_status":"pending",
            "release_gate":"needs_independent_review",
            "independent_review_complete":False,
            "semantic_accuracy_verified":False,
            "safe_to_mount_as_final_overlay":False,
        })
    if len(outputs)!=5 or len({x["source_sha256"] for x in outputs})!=5:
        raise ValueError("exact 5 QA-PASS name corrections required")
    return sorted(outputs,key=lambda r:r["source_sha256"])

def build(dest:Path=DEST)->dict:
    dest=dest.resolve()
    if not dest.is_relative_to((BUILD/"audits").resolve()):
        raise ValueError("name proposals must remain in isolated audits")
    if dest.exists():raise FileExistsError("frozen name audit already exists")
    tmp=dest.with_name(dest.name+".incomplete")
    if tmp.exists():raise FileExistsError("incomplete name audit exists")
    identity=version_identity(SNAPSHOT,client_version="9.0.200",
                              asset_version="1077100",asset_index=INDEX)
    base=json.loads(BASE.read_text(encoding="utf8"))
    if (sha_file(AUDIT)!=AUDIT_SHA or sha_file(REVIEW)!=REVIEW_SHA
        or sha_file(BASE)!=BASE_SHA or base.get("version_identity")!=identity
        or base.get("safe_to_mount_as_final_overlay") is not False
        or base.get("independent_reviewed") is not False):
        raise ValueError("frozen base audit/review/version changed")
    rows=candidate_rows(json.loads(AUDIT.read_text(encoding="utf8")),read_jsonl(REVIEW))
    tmp.mkdir(parents=True)
    target=tmp/"five-originally-qa-pass-name-corrections.jsonl"
    with target.open("w",encoding="utf8",newline="\n") as stream:
        for row in rows:stream.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+"\n")
    manifest={
        "schema_version":1,"kind":"event-unit-five-QA-PASS-false-negative-name-repairs-unreviewed",
        "version_identity":identity,
        "source_13177_audit_sha256":sha_file(AUDIT),
        "source_v3_1181_worklist_sha256":sha_file(REVIEW),
        "source_852_baseline_manifest_sha256":sha_file(BASE),
        "audit_jp_tamaki_sources":105,"prior_correct_name_examples":95,
        "prior_wrong_yuhuan_examples":6,"already_in_v3_draft":1,
        "previously_outside_v3_review":len(rows),
        "draft_file":target.name,"draft_sha256":sha_file(target),
        "draft_unique":len(rows),"draft_qa_verdicts":{"PASS":5},
        "five_new_sources_need_independent_review":True,
        "independent_review_complete":False,"semantic_accuracy_verified":False,
        "safe_to_mount_as_final_overlay":False,
        "production_translations_modified":False,"older_QA_stages_modified":False,
        "official_original_assets_modified":False,"nas_modified":False,
    }
    (tmp/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    os.replace(tmp,dest)
    return manifest

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output-root",type=Path,default=DEST)
    args=ap.parse_args()
    print(json.dumps(build(args.output_root),ensure_ascii=False,indent=2))
if __name__=="__main__":main()

