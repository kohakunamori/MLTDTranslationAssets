#!/usr/bin/env python3
"""Offline tests for high/critical gated draft review, bounded repair, and quarantine."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipelines.core.hashing import sha256_text
from pipelines.core.jsonl import read_jsonl, write_jsonl
from pipelines.text.review_gtx_translations import compute_missing_risk
from scripts import review_llm_drafts


def _queue_row(source: str, *, usage_profile: dict | None = None, queue_reason: str = "untranslated") -> dict:
    return {
        "source": source,
        "source_sha256": sha256_text(source),
        "bundle": "test.gtx",
        "key": "k_" + sha256_text(source)[:6],
        "task": "ASSETS_TEXT",
        "asset_version": "1",
        "source_client_version": "9.0.200",
        "translation": "",
        "status": "pending",
        "repair_reason": None,
        "queue_reason": queue_reason,
        "usage_profile": usage_profile or {},
    }


def _draft_row(source: str, translation: str) -> dict:
    return {
        "source_sha256": sha256_text(source),
        "source": source,
        "translation": translation,
        "status": "accepted",
        "provenance": {"provider": "mock", "model": "mock-model"},
    }


def _write_mock_reviewer(script_path: Path, state_path: Path) -> str:
    script = """import json, sys
from pathlib import Path
payload = json.load(sys.stdin)
rules = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
Path(sys.argv[2]).open("a", encoding="utf-8").write(json.dumps([i["id"] for i in payload["items"]]) + "\\n")
reviews = []
for item in payload["items"]:
    sid = item["id"]
    tr = item["translation"]
    rule = rules.get(f"{sid}:{tr}") or rules.get(sid) or {"verdict": "PASS", "blocking_errors": [], "notes": "ok"}
    score = 5 if rule["verdict"] == "PASS" else 2
    reviews.append({
        "id": sid,
        "verdict": rule["verdict"],
        "scores": {
            "semantic_accuracy": score,
            "terminology": score,
            "fluency": score,
            "character_voice": score,
            "context_consistency": score,
        },
        "blocking_errors": rule.get("blocking_errors", []),
        "notes": rule.get("notes", ""),
    })
print(json.dumps({"reviews": reviews}, ensure_ascii=False))
"""
    script_path.write_text(script, encoding="utf-8")
    seen_log = state_path.with_name("seen_batches.jsonl")
    return f'"{sys.executable}" "{script_path}" "{state_path}" "{seen_log}"'


class ReviewLlmDraftsTests(unittest.TestCase):
    def test_skips_cleanly_for_empty_low_risk_and_unconfigured_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue.jsonl"
            draft = root / "draft.jsonl"
            rev_out = root / "review.jsonl"
            rej_out = root / "rejected.jsonl"
            summary_out = root / "summary.json"

            # Empty draft skips cleanly.
            write_jsonl(queue, [_queue_row("閉じる")])
            draft.write_text("", encoding="utf-8")
            empty_res = review_llm_drafts.run_gated_review(
                queue_path=queue, draft_path=draft,
                review_output=rev_out, rejected_output=rej_out, summary_path=summary_out,
            )
            self.assertEqual(empty_res["status"], "skipped")
            self.assertEqual(empty_res["reason"], "empty_draft")

            # Low-risk only skips cleanly even before checking provider.
            write_jsonl(draft, [_draft_row("閉じる", "关闭")])
            low_res = review_llm_drafts.run_gated_review(
                queue_path=queue, draft_path=draft,
                review_output=rev_out, rejected_output=rej_out, summary_path=summary_out,
            )
            self.assertEqual(low_res["status"], "skipped")
            self.assertEqual(low_res["reason"], "no_high_risk_candidates")

            # High-risk draft without configured reviewer backend skips cleanly unless required.
            crit = _queue_row("どうする？", usage_profile={"speaker_codes": ["har", "chi"], "multi_speaker": True})
            write_jsonl(queue, [crit])
            write_jsonl(draft, [_draft_row("どうする？", "怎么办？")])
            with patch.dict(os.environ, {}, clear=True):
                no_prov = review_llm_drafts.run_gated_review(
                    queue_path=queue, draft_path=draft,
                    review_output=rev_out, rejected_output=rej_out, summary_path=summary_out,
                    config_path=None,
                )
            self.assertEqual(no_prov["status"], "skipped")
            self.assertEqual(no_prov["reason"], "no_review_provider")
            self.assertEqual(no_prov["high_risk_candidates"], 1)

    def test_resolve_review_backend_prefers_chat_completions_with_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "models.json"
            cfg_path.write_text(json.dumps({
                "models": [
                    {
                        "id": "resp-only",
                        "model": "gpt-resp",
                        "api_protocol": "responses",
                        "base_url": "https://resp.example/v1",
                        "api_key_env": "KEY_RESP",
                    },
                    {
                        "id": "chat-ok",
                        "model": "deepseek-chat",
                        "api_protocol": "chat_completions",
                        "base_url": "https://chat.example/v1",
                        "api_key_env": "KEY_CHAT",
                    },
                ]
            }), encoding="utf-8")
            with patch.dict(os.environ, {"KEY_RESP": "r1", "KEY_CHAT": "c1"}, clear=True):
                backend = review_llm_drafts.resolve_review_backend(config_path=cfg_path)
            self.assertIsNotNone(backend)
            self.assertEqual(backend["provider"], "openai-compatible")
            self.assertEqual(backend["model"], "deepseek-chat")
            self.assertEqual(backend["endpoint"], "https://chat.example/v1/chat/completions")

            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(review_llm_drafts.resolve_review_backend(config_path=cfg_path))

    def test_filters_high_critical_repairs_once_promotes_and_quarantines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            low_src = "戻る"
            fix_src = "悪い意味じゃないのよ、誤解しないでね。"
            bad_src = "それは秘密にしておいてね！"

            q_low = _queue_row(low_src)
            q_fix = _queue_row(
                fix_src,
                usage_profile={"categories": ["main"], "speaker_codes": ["har", "chi"], "multi_speaker": True},
            )
            q_bad = _queue_row(
                bad_src,
                usage_profile={"categories": ["main"]},
                queue_reason="ambiguous_seed",
            )
            queue_map = {r["source_sha256"]: r for r in (q_low, q_fix, q_bad)}
            drafts = [
                _draft_row(low_src, "返回"),
                _draft_row(fix_src, "没有别的意思，别误会。"),
                _draft_row(bad_src, "请别放在心上。"),
            ]
            cand_map = {r["source_sha256"]: r for r in drafts}
            risk = compute_missing_risk(queue_map, cand_map)
            self.assertEqual(risk[q_low["source_sha256"]]["risk_level"], "low")
            self.assertEqual(risk[q_fix["source_sha256"]]["risk_level"], "critical")
            self.assertEqual(risk[q_bad["source_sha256"]]["risk_level"], "critical")

            queue_path = root / ".llm-queue.jsonl"
            draft_path = root / ".llm-draft.jsonl"
            rev_out = root / ".llm-review.jsonl"
            rej_out = root / ".llm-review-rejected.jsonl"
            summary_out = root / ".llm-review-summary.json"
            glossary_path = root / "glossary.json"
            glossary_path.write_text(json.dumps({"entries": {}}), encoding="utf-8")
            write_jsonl(queue_path, [q_low, q_fix, q_bad])
            write_jsonl(draft_path, drafts)

            fix_sid = q_fix["source_sha256"]
            bad_sid = q_bad["source_sha256"]
            rules_path = root / "rules.json"
            rules_path.write_text(json.dumps({
                f"{fix_sid}:没有别的意思，别误会。": {
                    "verdict": "REJECT",
                    "blocking_errors": ["semantic_drift: 悪い意味じゃない != 没有别的意思"],
                    "notes": "请改为并非恶意",
                },
                f"{fix_sid}:我不是那个坏意思，别误会。": {
                    "verdict": "PASS",
                    "blocking_errors": [],
                    "notes": "repaired",
                },
                bad_sid: {
                    "verdict": "REJECT",
                    "blocking_errors": ["omission: 秘密未译出"],
                    "notes": "仍然漏译",
                },
            }, ensure_ascii=False), encoding="utf-8")

            cmd = _write_mock_reviewer(root / "mock_reviewer.py", rules_path)
            repair_calls: list[list[dict]] = []

            def fake_repair(r_queue: Path, r_draft: Path) -> int:
                items = read_jsonl(r_queue)
                repair_calls.append(items)
                repaired = []
                for item in items:
                    if item["source_sha256"] == fix_sid:
                        repaired.append(_draft_row(fix_src, "我不是那个坏意思，别误会。"))
                    else:
                        repaired.append(_draft_row(bad_src, "一直这样就好。"))
                write_jsonl(r_draft, repaired)
                return 0

            summary = review_llm_drafts.run_gated_review(
                queue_path=queue_path,
                draft_path=draft_path,
                review_output=rev_out,
                rejected_output=rej_out,
                summary_path=summary_out,
                config_path=None,
                provider="command",
                command=cmd,
                glossary_path=glossary_path,
                repair_runner=fake_repair,
            )

            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["high_risk_candidates"], 2)
            self.assertTrue(summary["repair_attempted"])
            self.assertEqual(summary["repaired"], 1)
            self.assertEqual(summary["quarantined"], 1)
            self.assertEqual(summary["kept_drafts"], 2)

            # Low-risk item was never sent to the reviewer subprocess.
            seen_batches = [
                json.loads(line)
                for line in (root / "seen_batches.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            all_reviewed_ids = {sid for batch in seen_batches for sid in batch}
            self.assertNotIn(q_low["source_sha256"], all_reviewed_ids)
            self.assertEqual(all_reviewed_ids, {fix_sid, bad_sid})

            # Repair queue carried previous_translation + stale_reasons from reviewer.
            self.assertEqual(len(repair_calls), 1)
            repair_by_sid = {row["source_sha256"]: row for row in repair_calls[0]}
            self.assertEqual(repair_by_sid[fix_sid]["previous_translation"], "没有别的意思，别误会。")
            self.assertEqual(
                repair_by_sid[fix_sid]["stale_reasons"][0]["code"],
                "independent_review_reject",
            )

            # Kept draft keeps low-risk + repaired high-risk and excludes persistent REJECT.
            kept = {row["source_sha256"]: row for row in read_jsonl(draft_path)}
            self.assertEqual(set(kept), {q_low["source_sha256"], fix_sid})
            self.assertEqual(kept[fix_sid]["translation"], "我不是那个坏意思，别误会。")
            self.assertTrue(kept[fix_sid]["provenance"]["review_repaired"])
            self.assertEqual(kept[fix_sid]["provenance"]["repair_review_verdict"], "PASS")

            # Quarantined persistent REJECT pairs the reviewed repair text with its blocking error
            # and preserves the initial rejected draft separately.
            quarantined = read_jsonl(rej_out)
            self.assertEqual([r["source_sha256"] for r in quarantined], [bad_sid])
            self.assertEqual(quarantined[0]["translation"], "一直这样就好。")
            self.assertEqual(quarantined[0]["initial_translation"], "请别放在心上。")
            self.assertEqual(quarantined[0]["review_verdict"], "REJECT")
            self.assertIn("omission: 秘密未译出", quarantined[0]["review_blocking_errors"])


if __name__ == "__main__":
    unittest.main()
