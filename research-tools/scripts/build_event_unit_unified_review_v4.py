#!/usr/bin/env python3
"""v4 source-bound Event-unit review, adding 5 found QA-PASS semantic errors.

Prior v3's 1,181 rows remain immutable; five previously ignored PASS sources
are explicitly appended to a fresh 1,186-source HUMAN-review-target list.
Their machine QA remains PASS (false negative); new unreviewed agent drafts
are not accepted. v4 is NOT an assertion that all other PASS sources are
independently verified or that global localization can be released.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.build_event_unit_review_pack import sha_file
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id

BUILD=ROOT/"build/localization-90200"
AUDITS=BUILD/"audits"
V3=AUDITS/"event-unit-unified-review-v3-client-9.0.200-assets-1077100"
NAME5=AUDITS/"event-unit-tamaki-five-QA-pass-repairs-client-9.0.200-assets-1077100"
STAGE=BUILD/"staging-event-unit-combined-55-unreviewed-drafts"
AUDIT=BUILD/"staging-event-unit-qa-with-tail/event-unit-QA-candidate-audit.json"
SNAPSHOT=BUILD/"jp-gtx-cache-snapshot.json"
INDEX=ROOT/"work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
DEST=AUDITS/"event-unit-unified-review-v4-client-9.0.200-assets-1077100"

FILES={
    "v3":(V3/"review-worklist.jsonl","7f347b347fb7707077506f40e4783631bfc4eaefcf323b40e17841fb8008c36c"),
    "five":(NAME5/"five-originally-qa-pass-name-corrections.jsonl","6fe6159320d7909d247a5d0ee2590b1730f2fb59c4bb74f778a7118629475f1c"),
    "original_QA_13177":(AUDIT,"f2c8e3b4d6fb8428abc62cb2fb0b9e7b7fb41c22be2ea7ff60a0f10187aec30d"),
}
MANIFESTS={
    "v3":(V3/"manifest.json","e36956a1fab612a09f3c52e0c80d026760b43d3f4348f4da0fb48a4840eedfc8"),
    "five":(NAME5/"manifest.json","9a18b2665d4e5f74646d04ba4411921c51538f21095c4c7f1cb32cb689025a5c"),
    "stage55":(STAGE/"manifest.json","8c6af74fcbf70e44c1b56f6f3ea02188fd2107bad0c1fc9f0b3369d2f7e49ff5"),
}

BUCKETS=(
    "rejected_original_with_correction_draft",
    "draft_still_qa_review",
    "qa_review_with_qa_pass_draft",
    "missing_machine_with_qa_pass_draft",
    "qa_review_without_draft",
    "qa_pass_semantic_false_negative_with_draft",
    "rule_cleared_qa_pass_needs_semantic_review",
)
COUNTS={
    "rejected_original_with_correction_draft":1,
    "draft_still_qa_review":2,
    "qa_review_with_qa_pass_draft":47,
    "missing_machine_with_qa_pass_draft":47,
    "qa_review_without_draft":811,
    "qa_pass_semantic_false_negative_with_draft":5,
    "rule_cleared_qa_pass_needs_semantic_review":273,
}

def combine(
    previous:list[dict], name_five:list[dict], original:list[dict], pilot:dict,
)->tuple[list[dict],dict]:
    prev={}
    for row in previous:
        sid,src=row.get("source_sha256"),row.get("source")
        if not isinstance(src,str) or sid!=source_id(src) or sid in prev:
            raise ValueError("prior human-review source duplicate or stale")
        prev[sid]=row
    found={}
    for row in original:
        sid,src=row.get("source_sha256"),row.get("source")
        if not isinstance(src,str) or sid!=source_id(src) or sid in found:
            raise ValueError("original QA audit source duplicate or stale")
        found[sid]=row
    chosen={}
    for row in name_five:
        sid,src=row.get("source_sha256"),row.get("source")
        if not isinstance(src,str) or sid!=source_id(src) or sid in chosen:
            raise ValueError("found QA-PASS new candidate duplicate/unbound")
        chosen[sid]=row
    if (len(prev)!=1181 or len(found)!=13177 or len(chosen)!=5
        or set(chosen)&set(prev)
        or not set(chosen).issubset(found)):
        raise ValueError("QA false-negative review population overlaps old reviewer")
    fifty={}
    five={}
    remotes=set()
    for row in pilot.get("bundles",[]):
        remote=row.get("remote")
        if (remote in remotes or row.get("release_gate")!="NOT_EVALUATED"
            or row.get("roundtrip_verified") is not True
            or row.get("non_text_objects_byte_identical") is not True):
            raise ValueError("stale/duplicated 55 pilot resource")
        remotes.add(remote)
        for key,collection in (
            ("preserved_previous_50_unreviewed_fields",fifty),
            ("corrected_preexisting_baseline_QA_fields",five),
        ):
            for change in row.get(key,[]):
                sid,src=change.get("path_source_sha256"),change.get("original")
                if (not isinstance(src,str) or sid!=source_id(src)
                    or sid in collection):
                    raise ValueError("same source SHA twice in 55 QA resource")
                collection[sid]=change
    if (len(remotes)!=51 or len(fifty)!=50 or len(five)!=5
        or set(fifty)&set(five)
        or set(five)!=set(chosen)
        or pilot.get("previous_50_unreviewed_fields_preserved")!=50
        or pilot.get("previous_base_QA_fields_corrected_unreviewed")!=5
        or pilot.get("unreviewed_candidate_source_unique")!=55
        or pilot.get("unreviewed_draft_QA_verdicts")!={"PASS":53,"REVIEW":2}
        or pilot.get("safe_to_mount_as_final_overlay") is not False
        or pilot.get("independent_review_complete") is not False
        or pilot.get("overlay_merge_authorized") is not False):
        raise ValueError("immutable 55-source pilot QA/release counts changed")
    for sid,change in fifty.items():
        row=prev.get(sid)
        if (row is None or row["source"]!=change["original"]
            or row.get("agent_correction_draft_unreviewed")!=change["localized"]
            or row.get("independent_review_complete") is not False):
            raise ValueError("50 old drafts not represented in frozen v3 reviewer")
    output=[]
    counts:Counter[str]=Counter()
    for sid,row in prev.items():
        if (row.get("review_status")!="pending"
            or row.get("release_gate")!="needs_independent_review"
            or row.get("independent_review_complete") is not False
            or row.get("semantic_accuracy_verified") is not False
            or row.get("safe_to_mount_as_final_overlay") is not False):
            raise ValueError("historical reviewer entry already approved")
        copy=dict(row)
        copy["prior_review_bucket_v3"]=row["review_bucket"]
        copy["review_bucket_order"]=BUCKETS.index(row["review_bucket"])
        counts[copy["review_bucket"]]+=1
        output.append(copy)
    for sid,candidate in chosen.items():
        original_row=found[sid]
        changed=five.get(sid)
        if (original_row.get("qa_verdict")!="PASS"
            or original_row.get("issues")!=[]
            or candidate.get("prior_machine_qa_verdict")!="PASS"
            or candidate.get("draft_qa_verdict")!="PASS"
            or candidate.get("draft_qa_issues")!=[]
            or original_row["source"]!=candidate["source"]
            or original_row["translation"]!=candidate["machine_candidate_unreviewed"]
            or original_row["examples"]!=candidate["examples"]
            or original_row["occurrences"]!=candidate["occurrences"]
            or candidate.get("status")!="agent_draft_unreviewed"
            or candidate.get("review_status")!="pending"
            or candidate.get("release_gate")!="needs_independent_review"
            or candidate.get("independent_review_complete") is not False
            or candidate.get("semantic_accuracy_verified") is not False
            or candidate.get("safe_to_mount_as_final_overlay") is not False
            or candidate.get("corpus_name_evidence_is_official") is not False
            or changed is None
            or changed["original"]!=candidate["source"]
            or changed["prior_baseline_QA_translation"]!=candidate["machine_candidate_unreviewed"]
            or changed["localized"]!=candidate["translation_draft"]):
            raise ValueError("newly discovered PASS translation not source-verified")
        bucket="qa_pass_semantic_false_negative_with_draft"
        output.append({
            "source_sha256":sid,"source":candidate["source"],
            "examples":candidate["examples"],
            "occurrences":candidate["occurrences"],
            "machine_candidate_unreviewed":candidate["machine_candidate_unreviewed"],
            "initial_qa_verdict":"PASS",
            "current_machine_qa_verdict":"PASS",
            "current_machine_qa_issues":[],
            "agent_correction_draft_unreviewed":candidate["translation_draft"],
            "agent_draft_source_group":"unreviewed_tamaki_qa_pass_false_negative",
            "agent_draft_deterministic_qa_verdict":"PASS",
            "agent_draft_deterministic_qa_issues":[],
            "candidate_for_human_review_unreviewed":candidate["translation_draft"],
            "corpus_name_evidence_is_official":False,
            "previously_absent_from_v3":True,
            "prior_review_bucket_v3":None,
            "review_bucket":bucket,
            "review_bucket_order":BUCKETS.index(bucket),
            "review_status":"pending",
            "release_gate":"needs_independent_review",
            "independent_review_complete":False,
            "semantic_accuracy_verified":False,
            "safe_to_mount_as_final_overlay":False,
        })
        counts[bucket]+=1
    if counts!=Counter(COUNTS):
        raise ValueError("v4 review category counts differ")
    output.sort(key=lambda x:(x["review_bucket_order"],-x["occurrences"],x["source_sha256"]))
    with_draft=[x for x in output if x["agent_correction_draft_unreviewed"] is not None]
    verdicts=Counter(x["agent_draft_deterministic_qa_verdict"] for x in with_draft)
    if (len(output)!=1186 or len(with_draft)!=102
        or verdicts!=Counter({"PASS":100,"REVIEW":2})):
        raise ValueError("v4 source/draft technical QA population wrong")
    return output,{
        "known_targeted_review_source_unique":1186,
        "v3_risk_review_source_unique_unchanged":1181,
        "new_QA_PASS_semantic_false_negative_sources":5,
        "independent_semantic_reviews_still_required_in_targeted_queue":1186,
        "available_unreviewed_agent_drafts":102,
        "draft_deterministic_qa_verdicts":dict(verdicts),
        "qa_review_without_targeted_draft":811,
        "all_without_targeted_draft":1186-len(with_draft),
        "review_buckets":dict(counts),
        "machine_qa_verdicts_in_targeted_queue":{
            "PASS":278,"REVIEW":860,"REJECT":1,"MISSING_MACHINE":47,
        },
        "unreviewed_drafts_materialized_in_55_bundle_pilot":55,
    }

def build(dest:Path=DEST)->dict:
    dest=dest.resolve()
    if not dest.is_relative_to(AUDITS.resolve()):
        raise ValueError("v4 reviewer must stay inside audits")
    if dest.exists():raise FileExistsError("v4 immutable reviewer already exists")
    tmp=dest.with_name(dest.name+".incomplete")
    if tmp.exists():raise FileExistsError("unfinished v4 immutable reviewer already exists")
    identity=version_identity(SNAPSHOT,client_version="9.0.200",
                              asset_version="1077100",asset_index=INDEX)
    for name,(path,expected) in FILES.items():
        if sha_file(path)!=expected:
            raise ValueError(f"frozen reviewer source changed: {name}")
    source_manifests={}
    for name,(path,expected) in MANIFESTS.items():
        if sha_file(path)!=expected:
            raise ValueError(f"frozen reviewer manifest changed: {name}")
        evidence=json.loads(path.read_text(encoding="utf8"))
        if (evidence.get("version_identity")!=identity
            or evidence.get("independent_review_complete") is not False
            or evidence.get("safe_to_mount_as_final_overlay") is not False):
            raise ValueError(f"reviewer source approved or wrong version: {name}")
        source_manifests[name]=evidence
    if (source_manifests["v3"]["review_worklist_sha256"]!=FILES["v3"][1]
        or source_manifests["five"]["draft_sha256"]!=FILES["five"][1]
        or source_manifests["stage55"]["source_five_name_drafts_sha256"]!=FILES["five"][1]
        or source_manifests["stage55"]["source_50_pilot_manifest_sha256"]!=
            "9480b987c2ba9d17c74fccbc4da8dcd3e92cd80ace8e5aa1c35aeab7aa6b8bcd"):
        raise ValueError("stale v3/name/55-stage source chain")
    items,counts=combine(
        read_jsonl(FILES["v3"][0]),read_jsonl(FILES["five"][0]),
        json.loads(FILES["original_QA_13177"][0].read_text(encoding="utf8")),
        source_manifests["stage55"],
    )
    tmp.mkdir(parents=True)
    out=tmp/"review-worklist.jsonl"
    with out.open("w",encoding="utf8",newline="\n") as stream:
        for row in items:stream.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+"\n")
    manifest={
        "schema_version":4,
        "kind":"event-unit-v4-targeted-review-1186-all-pending-NOT-FULL-CORPUS-CERTIFICATION",
        "version_identity":identity,
        "source_files_sha256":{name:expected for name,(_,expected) in FILES.items()},
        "source_manifests_sha256":{name:expected for name,(_,expected) in MANIFESTS.items()},
        "review_worklist_filename":out.name,
        "review_worklist_sha256":sha_file(out),
        "bucket_order":list(BUCKETS),
        "all_other_qa_pass_sources_semantically_verified":False,
        "review_status":"pending",
        "release_gate":"needs_independent_review",
        "independent_review_complete":False,
        "semantic_accuracy_verified":False,
        "safe_to_mount_as_final_overlay":False,
        "production_translations_modified":False,
        "prior_852_50_55_bundle_stages_modified":False,
        "official_original_assets_modified":False,"nas_modified":False,
        **counts,
    }
    (tmp/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    os.replace(tmp,dest)
    return manifest

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-root",type=Path,default=DEST)
    args=p.parse_args()
    print(json.dumps(build(args.output_root),ensure_ascii=False,indent=2))
if __name__=="__main__":main()

