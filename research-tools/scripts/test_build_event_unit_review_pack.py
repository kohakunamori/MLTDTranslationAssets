"""Regression checks for isolated frozen event-unit reviewer worklist."""
import json
from collections import Counter

import pytest

from scripts.build_event_unit_review_pack import OUTPUT, build_review_rows, sha_file
from scripts.mltd_translation_quality import source_id


def row(source, count=1):
    return {"source_sha256": source_id(source), "source": source,
            "occurrences": count, "examples": [{"previous": "前", "next": "後"}]}


def test_reference_is_unreviewed_and_no_pass_items_are_exported():
    passed, missing, rejected = row("おはよう"), row("1時間後", 3), row("10パーセント", 2)
    qa = [
        {"source_sha256": passed["source_sha256"], "qa_verdict": "PASS"},
        {"source_sha256": missing["source_sha256"], "qa_verdict": "MISSING_MACHINE"},
        {"source_sha256": rejected["source_sha256"], "source": rejected["source"],
         "translation": "10个百分点", "qa_verdict": "REJECT",
         "issues": [{"code": "numeric_literal_mismatch", "severity": "reject"}]},
    ]
    legacy = [
        {"source": missing["source"], "translation": "1小時後", "status": "official_legacy"},
        {"source": missing["source"], "translation": "1小時後", "status": "official_legacy"},
        {"source": missing["source"], "translation": "一小時後", "status": "official_legacy"},
    ]
    review, counts = build_review_rows([passed, missing, rejected], qa, legacy)
    assert [r["qa_verdict"] for r in review] == ["REJECT", "MISSING_MACHINE"]
    assert counts["review_queue_unique"] == 2
    assert counts["missing_machine_with_legacy_reference"] == 1
    assert review[1]["legacy_traditional_references_unreviewed"] == ["1小時後", "一小時後"]
    assert review[1]["machine_candidate_unreviewed"] == ""
    assert review[1]["examples"] == missing["examples"]
    assert all(x["review_status"] == "pending"
               and x["release_gate"] == "needs_independent_review" for x in review)


@pytest.mark.parametrize("invalid", ["duplicate_qa", "missing_qa", "extra_qa", "changed_source",
                                     "duplicate_queue", "wrong_sha", "missing_issues"])
def test_source_and_qa_fail_closed(invalid):
    q = row("1時間後")
    a = {"source_sha256": q["source_sha256"], "qa_verdict": "REJECT",
         "source": q["source"], "translation": "一小时后",
         "issues": [{"code": "numeric_literal_mismatch"}]}
    queue, audit = [q], [a]
    if invalid == "duplicate_qa":
        audit = [a, a]
    elif invalid == "missing_qa":
        audit = []
    elif invalid == "extra_qa":
        audit = [a, {"source_sha256": source_id("extra"), "qa_verdict": "PASS"}]
    elif invalid == "changed_source":
        audit = [{**a, "source": "違う"}]
    elif invalid == "duplicate_queue":
        queue = [q, q]
    elif invalid == "wrong_sha":
        queue = [{**q, "source_sha256": "0" * 64}]
    elif invalid == "missing_issues":
        audit = [{**a, "issues": []}]
    with pytest.raises(ValueError):
        build_review_rows(queue, audit, [])


def test_full_frozen_review_pack_integrity():
    manifest = json.loads((OUTPUT / "review-pack-manifest.json").read_text(encoding="utf8"))
    path = OUTPUT / manifest["review_queue_name"]
    queue = [json.loads(x) for x in path.read_text(encoding="utf8").splitlines()]
    assert sha_file(path) == manifest["review_queue_sha256"]
    assert len(queue) == manifest["review_queue_unique"] == 1181
    assert Counter(x["qa_verdict"] for x in queue) == {
        "REJECT": 1, "MISSING_MACHINE": 47, "REVIEW": 1133,
    }
    assert sum(bool(x["legacy_traditional_references_unreviewed"])
               for x in queue if x["qa_verdict"] == "MISSING_MACHINE") == 47
    assert len({x["source_sha256"] for x in queue}) == len(queue)
    assert all(x["source_sha256"] == source_id(x["source"])
               and x["review_status"] == "pending" for x in queue)
    assert manifest["version_identity"]["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert manifest["reviewed"] is False and manifest["safe_to_mount_as_final_overlay"] is False

