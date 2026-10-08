"""v4 adds only five QA-PASS false negatives, never fake human review."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import sha_file
from scripts.build_event_unit_unified_review_v4 import (
    DEST,FILES,MANIFESTS,COUNTS,BUCKETS,combine,build,
)
from scripts.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import source_id


@pytest.fixture(scope="module")
def sources():
    return (
        read_jsonl(FILES["v3"][0]),
        read_jsonl(FILES["five"][0]),
        json.loads(FILES["original_QA_13177"][0].read_text(encoding="utf8")),
        json.loads(MANIFESTS["stage55"][0].read_text(encoding="utf8")),
    )


def test_persisted_v4_reviews_1186_unique_source_ids_and_5_found_pass_mistakes(sources):
    m=json.loads((DEST/"manifest.json").read_text(encoding="utf8"))
    rows=read_jsonl(DEST/m["review_worklist_filename"])
    assert sha_file(DEST/m["review_worklist_filename"])==m["review_worklist_sha256"]
    assert m["version_identity"]["version_key"]=="jp-client-9.0.200-assets-1077100"
    for k,(path,sha) in FILES.items():
        assert sha_file(path)==sha==m["source_files_sha256"][k]
    for k,(path,sha) in MANIFESTS.items():
        assert sha_file(path)==sha==m["source_manifests_sha256"][k]
    assert m["known_targeted_review_source_unique"]==len(rows)==1186
    assert len({x["source_sha256"] for x in rows})==1186
    assert m["v3_risk_review_source_unique_unchanged"]==1181
    assert m["new_QA_PASS_semantic_false_negative_sources"]==5
    assert m["independent_semantic_reviews_still_required_in_targeted_queue"]==1186
    assert m["available_unreviewed_agent_drafts"]==102
    assert m["draft_deterministic_qa_verdicts"]=={"PASS":100,"REVIEW":2}
    assert m["qa_review_without_targeted_draft"]==811
    assert m["all_without_targeted_draft"]==1084
    assert m["review_buckets"]==COUNTS
    assert m["machine_qa_verdicts_in_targeted_queue"]=={
        "PASS":278,"REVIEW":860,"REJECT":1,"MISSING_MACHINE":47,
    }
    assert m["unreviewed_drafts_materialized_in_55_bundle_pilot"]==55
    assert Counter(x["review_bucket"] for x in rows)==COUNTS
    assert [x["review_bucket_order"] for x in rows]==sorted(
        x["review_bucket_order"] for x in rows
    )
    for key in (
        "all_other_qa_pass_sources_semantically_verified",
        "independent_review_complete","semantic_accuracy_verified",
        "safe_to_mount_as_final_overlay","production_translations_modified",
        "prior_852_50_55_bundle_stages_modified",
        "official_original_assets_modified","nas_modified",
    ):
        assert m[key] is False
    assert m["review_status"]=="pending"
    assert m["release_gate"]=="needs_independent_review"
    previous={x["source_sha256"]:x for x in sources[0]}
    name5={x["source_sha256"]:x for x in sources[1]}
    original={x["source_sha256"]:x for x in sources[2]}
    assert set(previous).isdisjoint(name5)
    for x in rows:
        sid=x["source_sha256"]
        assert sid==source_id(x["source"])
        assert x["review_status"]=="pending"
        assert x["release_gate"]=="needs_independent_review"
        assert x["independent_review_complete"] is False
        assert x["semantic_accuracy_verified"] is False
        assert x["safe_to_mount_as_final_overlay"] is False
        if sid in name5:
            old=original[sid]
            draft=name5[sid]
            assert old["qa_verdict"]=="PASS"
            assert x["initial_qa_verdict"]=="PASS"
            assert x["current_machine_qa_verdict"]=="PASS"
            assert x["current_machine_qa_issues"]==[]
            assert x["source"]==old["source"]==draft["source"]
            assert x["machine_candidate_unreviewed"]==old["translation"]
            assert x["agent_correction_draft_unreviewed"]==draft["translation_draft"]
            assert x["candidate_for_human_review_unreviewed"]==draft["translation_draft"]
            assert x["agent_draft_source_group"]=="unreviewed_tamaki_qa_pass_false_negative"
            assert x["review_bucket"]=="qa_pass_semantic_false_negative_with_draft"
            assert x["corpus_name_evidence_is_official"] is False
            assert x["previously_absent_from_v3"] is True
            assert x["prior_review_bucket_v3"] is None
        else:
            for k,v in previous[sid].items():
                if k=="review_bucket_order":
                    assert x[k]==BUCKETS.index(previous[sid]["review_bucket"])
                else:
                    assert x[k]==v
    built,_=combine(*sources)
    assert rows==built


@pytest.mark.parametrize("failure",(
    "old_duplicate","old_missing","old_approved","original_sha_wrong",
    "original_missing","draft_duplicate","draft_missing","draft_approved",
    "draft_machine_changed","draft_source_changed","draft_QA_forged",
    "draft_renamed_to_risk_source","prior50_lost","prior50_draft_changed",
    "namefield_lost","namefield_changed","bundle_missing","pilot_false_approval",
))
def test_v4_input_mutation_fails_closed(sources,failure):
    old,drafts,audit,pilot=copy.deepcopy(sources)
    sid=drafts[0]["source_sha256"]
    prev_sid=next(x["source_sha256"] for x in old if x["agent_draft_source_group"]=="unreviewed_names_9")
    if failure=="old_duplicate":old.append(old[0])
    elif failure=="old_missing":old.pop()
    elif failure=="old_approved":old[0]["independent_review_complete"]=True
    elif failure=="original_sha_wrong":
        next(x for x in audit if x["source_sha256"]==sid)["source"]+="原文造假"
    elif failure=="original_missing":
        audit=[x for x in audit if x["source_sha256"]!=sid]
    elif failure=="draft_duplicate":drafts.append(drafts[0])
    elif failure=="draft_missing":drafts.pop()
    elif failure=="draft_approved":drafts[0]["independent_review_complete"]=True
    elif failure=="draft_machine_changed":drafts[0]["machine_candidate_unreviewed"]+="不同"
    elif failure=="draft_source_changed":drafts[0]["source"]+="別の原文"
    elif failure=="draft_QA_forged":drafts[0]["draft_qa_verdict"]="REVIEW"
    elif failure=="draft_renamed_to_risk_source":drafts[0]["source_sha256"]=prev_sid
    elif failure=="prior50_lost":
        field=next(x for b in pilot["bundles"] for x in b["preserved_previous_50_unreviewed_fields"]
                   if x["path_source_sha256"]==prev_sid)
        field["path_source_sha256"]="f"*64
    elif failure=="prior50_draft_changed":
        field=next(x for b in pilot["bundles"] for x in b["preserved_previous_50_unreviewed_fields"]
                   if x["path_source_sha256"]==prev_sid)
        field["localized"]+="伪造"
    elif failure=="namefield_lost":
        field=next(x for b in pilot["bundles"] for x in b["corrected_preexisting_baseline_QA_fields"]
                   if x["path_source_sha256"]==sid)
        field["path_source_sha256"]="f"*64
    elif failure=="namefield_changed":
        field=next(x for b in pilot["bundles"] for x in b["corrected_preexisting_baseline_QA_fields"]
                   if x["path_source_sha256"]==sid)
        field["localized"]+="擅改"
    elif failure=="bundle_missing":pilot["bundles"].pop()
    elif failure=="pilot_false_approval":pilot["overlay_merge_authorized"]=True
    with pytest.raises((ValueError,KeyError)):
        combine(old,drafts,audit,pilot)


def test_v4_immutable_no_overwrite():
    with pytest.raises(FileExistsError):
        build()

