#!/usr/bin/env python3
"""QA-only 55-source/51-bundle composite; 5 baseline PASS name false-negatives.

46 prior 50-source trial remotes are copied byte-exact. Four new remotes and
one shared remote are rebuilt from frozen original UnityFS, preserving ALL
fifty earlier drafts and changing exactly five preexisting base QA fields.
No asset/producer/NAS/official resources or immutable old stages touched.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter,defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from scripts.build_event_unit_review_pack import sha_file
from scripts.localization_version_identity import version_identity
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id
from scripts.stage_event_unit_10_targeted_review_drafts import (
    BASE,BASE_MANIFEST,COHORT_BUNDLE_ROOT,COHORT_PATH,INDEX,SNAPSHOT,
    materialize,
)
from scripts.build_event_unit_tamaki_qa_pass_repairs import DEST as DRAFT_ROOT

BUILD=ROOT/"build/localization-90200"
PRIOR=BUILD/"staging-event-unit-combined-50-unreviewed-drafts"
PRIOR_MANIFEST=PRIOR/"manifest.json"
DRAFT=DRAFT_ROOT/"five-originally-qa-pass-name-corrections.jsonl"
DRAFT_MANIFEST=DRAFT_ROOT/"manifest.json"
DEST=BUILD/"staging-event-unit-combined-55-unreviewed-drafts"
BASE_SHA="c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
PRIOR_SHA="9480b987c2ba9d17c74fccbc4da8dcd3e92cd80ace8e5aa1c35aeab7aa6b8bcd"
DRAFT_SHA="6fe6159320d7909d247a5d0ee2590b1730f2fb59c4bb74f778a7118629475f1c"

def validate_patch(
    baseline:dict,prior:dict,drafts:list[dict],
)->tuple[dict[str,dict],dict[str,dict],dict[str,dict[str,dict]]]:
    if (baseline.get("text_fields_changed")!=12204
        or baseline.get("bundles_written")!=852
        or baseline.get("safe_to_mount_as_final_overlay") is not False
        or baseline.get("independent_reviewed") is not False
        or len(baseline.get("bundles",[]))!=852
        or prior.get("bundle_count")!=47
        or prior.get("additional_unreviewed_source_unique")!=50
        or prior.get("draft_deterministic_qa")!={"PASS":48,"REVIEW":2}
        or prior.get("safe_to_mount_as_final_overlay") is not False
        or prior.get("independent_review_complete") is not False
        or prior.get("overlay_merge_authorized") is not False):
        raise ValueError("frozen original 852 or prior 50-stage provenance differs")
    by_base={b["remote"]:b for b in baseline["bundles"]}
    by_prior={b["remote"]:b for b in prior["bundles"]}
    if len(by_base)!=852 or len(by_prior)!=47:
        raise ValueError("duplicate prior-stage remotes")
    base_translation={}
    old_sids=set()
    for b in baseline["bundles"]:
        for c in b["changes"]:
            sid,src,zh=c["path_source_sha256"],c["original"],c["localized"]
            if sid!=source_id(src):raise ValueError("stale original QA source")
            old=base_translation.setdefault(sid,{
                "source_sha256":sid,"source":src,
                "translation":zh,"status":"QA-only-baseline",
            })
            if old["source"]!=src or old["translation"]!=zh:
                raise ValueError("old baseline has conflicting same-source translations")
            old_sids.add(sid)
    all_prior:dict[str,dict[str,dict]]=defaultdict(dict)
    seen=set()
    for b in prior["bundles"]:
        remote=b["remote"]
        if (remote not in by_base
            or b.get("original_bundle_sha256")!=by_base[remote]["source_sha256"]
            or b.get("baseline_852_qa_sha256")!=by_base[remote]["localized_sha256"]
            or b.get("roundtrip_verified_prior_trial") is not True
            or b.get("non_text_objects_byte_identical_prior_trial") is not True
            or b.get("release_gate")!="NOT_EVALUATED"):
            raise ValueError("prior trial source/baseline bundle changed")
        for c in b["additional_unreviewed_fields"]:
            sid,src,zh=c["path_source_sha256"],c["original"],c["localized"]
            if sid in seen or sid in old_sids or sid!=source_id(src):
                raise ValueError("prior draft SHA duplicates or contradicts baseline")
            seen.add(sid)
            all_prior[remote][sid]={"source":src,"translation_draft":zh}
            base_translation[sid]={
                "source_sha256":sid,"source":src,
                "translation":zh,"status":"agent_draft_unreviewed",
            }
    if len(seen)!=50 or sum(len(x) for x in all_prior.values())!=50:
        raise ValueError("50 previous source drafts were not all preserved")
    five={}
    remotes={}
    for r in drafts:
        sid,src=r.get("source_sha256"),r.get("source")
        if (not isinstance(src,str) or sid!=source_id(src)
            or sid in five or sid in seen
            or sid not in old_sids
            or r.get("prior_machine_qa_verdict")!="PASS"
            or r.get("draft_qa_verdict")!="PASS" or r.get("draft_qa_issues")!=[]
            or r.get("status")!="agent_draft_unreviewed"
            or r.get("independent_review_complete") is not False
            or r.get("semantic_accuracy_verified") is not False
            or r.get("safe_to_mount_as_final_overlay") is not False
            or r.get("release_gate")!="needs_independent_review"
            or r.get("occurrences")!=1 or len(r.get("examples",[]))!=1
            or base_translation[sid]["source"]!=src
            or base_translation[sid]["translation"]!=r["machine_candidate_unreviewed"]):
            raise ValueError(f"new QA-PASS correction lacks source/old text provenance: {sid}")
        remote=r["examples"][0]["remote"]
        if remote in remotes or remote not in by_base:
            raise ValueError("each name fix must refer to one unique source bundle")
        remotes[remote]=sid
        five[sid]=r
        base_translation[sid]={
            "source_sha256":sid,"source":src,"translation":r["translation_draft"],
            "status":"agent_draft_unreviewed",
        }
    shared=set(remotes)&set(by_prior)
    if (len(five)!=5 or len(remotes)!=5 or len(shared)!=1
        or len(set(remotes)|set(by_prior))!=51):
        raise ValueError("five baseline fixes / one shared prior-50 remote expected")
    return base_translation,five,all_prior

def verify_rebuilt_bundle(
    old:dict,built:dict,five:dict[str,dict],prior_extra:dict[str,dict],
)->tuple[list[dict],list[dict]]:
    by_command={(c["command_index"],c["path_source_sha256"]):c
                for c in built["changes"]}
    if len(by_command)!=len(built["changes"]):
        raise ValueError("rebuilt bundle has duplicate same-command translations")
    baseline_keys=set()
    changed=[]
    for old_change in old["changes"]:
        sid=old_change["path_source_sha256"]
        key=(old_change["command_index"],sid)
        got=by_command.get(key)
        new=five[sid]["translation_draft"] if sid in five else old_change["localized"]
        if (got is None or got["original"]!=old_change["original"]
            or got["localized"]!=new):
            raise ValueError(f"rebuilt bundle changed unexpected baseline QA text: {sid}")
        if key in baseline_keys:
            raise ValueError("duplicate old baseline command")
        baseline_keys.add(key)
        if sid in five:changed.append({
            "command_index":key[0],"path_source_sha256":sid,
            "original":old_change["original"],
            "prior_baseline_QA_translation":old_change["localized"],
            "localized":new,
        })
    extras=[]
    for sid,proposal in prior_extra.items():
        matching=[c for key,c in by_command.items()
                  if key not in baseline_keys and key[1]==sid]
        if (len(matching)!=1
            or matching[0]["original"]!=proposal["source"]
            or matching[0]["localized"]!=proposal["translation_draft"]):
            raise ValueError(f"lost existing unreviewed source on rebuild: {sid}")
        extras.append({
            "path_source_sha256":sid,"original":proposal["source"],
            "localized":proposal["translation_draft"],
        })
    if len(by_command)!=len(baseline_keys)+len(extras):
        raise ValueError("a new unexpected text field was introduced on rebuild")
    if len(changed)!=1:
        raise ValueError("expected exactly one preexisting base field corrected")
    return changed,extras

def build(dest:Path=DEST)->dict:
    dest=dest.resolve()
    if not dest.is_relative_to(BUILD.resolve()):
        raise ValueError("isolated QA-only output must remain inside build")
    if dest.exists():raise FileExistsError("immutable 55-source QA-only output exists")
    temp=dest.with_name(dest.name+".incomplete")
    if temp.exists():raise FileExistsError("incomplete 55-source QA output exists")
    identity=version_identity(SNAPSHOT,client_version="9.0.200",
                              asset_version="1077100",asset_index=INDEX)
    if (sha_file(BASE_MANIFEST)!=BASE_SHA or sha_file(PRIOR_MANIFEST)!=PRIOR_SHA
        or sha_file(DRAFT)!=DRAFT_SHA):
        raise ValueError("frozen QA/draft manifest or data SHA mismatch")
    base=json.loads(BASE_MANIFEST.read_text(encoding="utf8"))
    prior=json.loads(PRIOR_MANIFEST.read_text(encoding="utf8"))
    draft_manifest=json.loads(DRAFT_MANIFEST.read_text(encoding="utf8"))
    cohort=json.loads(COHORT_PATH.read_text(encoding="utf8"))
    if (any(x.get("version_identity")!=identity for x in (base,prior,draft_manifest))
        or draft_manifest.get("draft_sha256")!=DRAFT_SHA
        or draft_manifest.get("previously_outside_v3_review")!=5
        or draft_manifest.get("safe_to_mount_as_final_overlay") is not False
        or cohort.get("verified")!=852
        or cohort.get("source_index_sha256")!=identity["asset_index_sha256"]):
        raise ValueError("client/assets or review release gate changed")
    translations,five,all_prior=validate_patch(base,prior,read_jsonl(DRAFT))
    old_bundles={b["remote"]:b for b in base["bundles"]}
    originals={b["remote"]:b for b in cohort["bundles"]}
    prior_bundles={b["remote"]:b for b in prior["bundles"]}
    new_by_remote={
        r["examples"][0]["remote"]:r for r in five.values()
    }
    temp.mkdir(parents=True)
    records=[]
    corrected_count=0
    all_previous=set()
    for remote in sorted(set(prior_bundles)|set(new_by_remote)):
        old=old_bundles[remote]
        out=temp/"jp-android"/remote
        out.parent.mkdir(parents=True,exist_ok=True)
        corrected=[]
        preserved=[]
        if remote in new_by_remote:
            original=originals[remote]
            src=COHORT_BUNDLE_ROOT/remote
            if (original["sha256"]!=old["source_sha256"]
                or original["logical"]!=old["logical"]
                or sha_file(src)!=original["sha256"]
                or sha_file(BASE/"jp-android"/remote)!=old["localized_sha256"]):
                raise ValueError("original UnityFS or frozen base bundle changed")
            result=materialize(original["logical"],remote,original["declared_bytes"],
                               src,out,translations,require_complete=False)
            sid=new_by_remote[remote]["source_sha256"]
            corrected,preserved=verify_rebuilt_bundle(
                old,result,{sid:five[sid]},all_prior.get(remote,{}))
            sha=result["localized_sha256"]
            mode=("original_rebuild_shared_50_plus_name_fix"
                  if remote in prior_bundles else "original_rebuild_base_name_fix")
            roundtrip=result["roundtrip_verified"]
            nontext=result["non_text_objects_byte_identical"]
        else:
            earlier=prior_bundles[remote]
            src=PRIOR/"jp-android"/remote
            if (sha_file(src)!=earlier["localized_sha256"]
                or sha_file(BASE/"jp-android"/remote)!=old["localized_sha256"]):
                raise ValueError("previous QA-only output no longer SHA exact")
            shutil.copyfile(src,out)
            sha=sha_file(out)
            mode="sha_exact_copy_of_unchanged_prior_50"
            roundtrip=earlier["roundtrip_verified_prior_trial"]
            nontext=earlier["non_text_objects_byte_identical_prior_trial"]
            preserved=earlier["additional_unreviewed_fields"]
        if not roundtrip or not nontext:
            raise ValueError("previous or rebuilt UnityFS failed nontext/roundtrip")
        for r in preserved:
            sid=r["path_source_sha256"]
            if sid in all_previous:raise ValueError("duplicate prior-50 change in new composite")
            all_previous.add(sid)
        corrected_count+=len(corrected)
        records.append({
            "remote":remote,"logical":old["logical"],
            "source_mode":mode,
            "original_bundle_sha256":old["source_sha256"],
            "baseline_852_bundle_sha256":old["localized_sha256"],
            "localized_sha256":sha,
            "output_bytes":out.stat().st_size,
            "output_path":str(dest/"jp-android"/remote),
            "roundtrip_verified":True,"non_text_objects_byte_identical":True,
            "corrected_preexisting_baseline_QA_fields":corrected,
            "preserved_previous_50_unreviewed_fields":preserved,
            "release_gate":"NOT_EVALUATED",
        })
    modes=Counter(x["source_mode"] for x in records)
    if (len(records)!=51 or corrected_count!=5 or len(all_previous)!=50
        or modes!=Counter({
            "sha_exact_copy_of_unchanged_prior_50":46,
            "original_rebuild_base_name_fix":4,
            "original_rebuild_shared_50_plus_name_fix":1,
        })):
        raise ValueError("55 source/51 bundle and prior50 preservation differs")
    report={
        "schema_version":1,
        "kind":"event-unit-55-source-51-bundle-QA-only-five-baseline-corrections",
        "version_identity":identity,
        "source_852_QA_manifest_sha256":sha_file(BASE_MANIFEST),
        "source_852_cohort_sha256":sha_file(COHORT_PATH),
        "source_50_pilot_manifest_sha256":sha_file(PRIOR_MANIFEST),
        "source_five_name_manifest_sha256":sha_file(DRAFT_MANIFEST),
        "source_five_name_drafts_sha256":sha_file(DRAFT),
        "previous_base_QA_fields_not_changed":12199,
        "previous_base_QA_fields_corrected_unreviewed":5,
        "previous_50_unreviewed_fields_preserved":50,
        "unreviewed_candidate_source_unique":55,
        "bundle_count":51,
        "copy_unchanged_prior_50_bundle_count":46,
        "new_original_rebuilt_bundle_count":5,
        "shared_prior_50_rebuilt_bundle_count":1,
        "unreviewed_draft_QA_verdicts":{"PASS":53,"REVIEW":2},
        "independent_review_complete":False,"semantic_accuracy_verified":False,
        "release_gate":"not_evaluated",
        "safe_to_mount_as_final_overlay":False,
        "overlay_merge_authorized":False,
        "production_translations_modified":False,
        "prior_852_or_50_QA_stages_modified":False,
        "official_original_assets_modified":False,
        "nas_modified":False,
        "bundles":records,
    }
    (temp/"manifest.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    os.replace(temp,dest)
    return {k:v for k,v in report.items() if k!="bundles"}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-root",type=Path,default=DEST)
    args=p.parse_args()
    print(json.dumps(build(args.output_root),ensure_ascii=False,indent=2))
if __name__=="__main__":main()

