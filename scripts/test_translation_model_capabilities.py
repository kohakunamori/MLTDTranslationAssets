"""Provider capability regressions on the actual request -> provenance path."""
import asyncio
from collections import defaultdict
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from pipelines.core.hashing import sha256_text
from pipelines.text.translate_mltd_api_pool import (
    ModelConfig, _build_qa_retry_feedback, build_parser, build_user_prompt,
    load_model_config, request_translation, task_request_params_for_row,
    translate_with_model, validate_candidate,
)
from pipelines.text.mltd_translation_quality import evaluate_row, load_glossary


def config(**kwargs):
    return ModelConfig(id="test", model="alias", api_protocol="chat_completions",
                       endpoint="https://example.invalid/v1/chat/completions", api_key_env="",
                       concurrency=1, reasoning_effort="high", temperature=0.1, timeout=10,
                       retries=1, min_reasoning_effort="high", cache_warmup=False, **kwargs)


class CapabilityTests(unittest.TestCase):
    def test_dialogue_and_contextual_title_use_supported_reasoning(self):
        cfg = config(request_params_by_task={"DIALOGUE": {"reasoning_effort": "low"},
                                             "TITLE": {"reasoning_effort": "none"}})
        self.assertEqual(task_request_params_for_row(cfg, {"task_hint": "DIALOGUE"})[1]["reasoning_effort"], "high")
        row = {"task_hint": "TITLE", "source": "それから", "context_examples": [{"context": [
            {"relative": -1, "source": "前の章"}, {"relative": 0, "source": "それから"}]}]}
        self.assertEqual(task_request_params_for_row(cfg, row)[1]["reasoning_effort"], "high")
        self.assertEqual(task_request_params_for_row(cfg, {"task_hint": "TITLE"})[1]["reasoning_effort"], "none")
        ordinary = replace(cfg, min_reasoning_effort="")
        self.assertEqual(task_request_params_for_row(ordinary, {"task_hint": "DIALOGUE"})[1]["reasoning_effort"], "low")

    def test_wire_request_and_saved_provenance_agree(self):
        requests = []
        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body["reasoning_effort"], "high")
            return httpx.Response(200, json={"model": "reported-backend", "choices": [
                {"message": {"content": "明天见。"}}], "usage": {"total_tokens": 10}})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                cfg = config(request_params_by_task={"DIALOGUE": {"reasoning_effort": "low"}})
                source = "また明日。"
                row = {"source": source, "source_sha256": sha256_text(source), "task_hint": "DIALOGUE"}
                candidate, error = await translate_with_model(client, cfg, None, "SYSTEM", row, {}, {}, defaultdict(lambda: defaultdict(float)))
                self.assertFalse(error)
                self.assertEqual(candidate["provenance"]["reasoning_effort"], "high")
                self.assertEqual(candidate["provenance"]["reported_model"], "reported-backend")
        asyncio.run(run())
        self.assertEqual(len(requests), 1)

    def test_explicit_none_remains_none_on_both_protocols(self):
        async def run():
            for protocol in ["chat_completions", "responses"]:
                def handle(request):
                    body = json.loads(request.content)
                    effort = body.get("reasoning_effort") if protocol == "chat_completions" else body["reasoning"]["effort"]
                    self.assertEqual(effort, "none")
                    return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}],
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": "好"}]}]})
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    await request_translation(client, replace(config(), api_protocol=protocol), "SYSTEM", "SOURCE", {"reasoning_effort": "none"})
        asyncio.run(run())

    def test_configuration_rejects_unknown_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            doc = {"models": [{"id": "x", "model": "x", "base_url": "https://example.invalid/v1", "min_reasoning_effort": "invented"}]}
            path.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "min_reasoning_effort"):
                load_model_config(path)

    def test_new_unassigned_characters_are_rejected_but_source_glyphs_survive(self):
        source = "声が枯れるまで。"
        row = {"source": source, "source_sha256": sha256_text(source)}
        candidate, error = validate_candidate(row, "直到声音嘶哑。" + chr(0x4E7FF), [], [], {})
        self.assertIsNone(candidate)
        self.assertIn("introduced_unsupported_unicode", error)
        source += chr(0x4E7FF)
        row = {"source": source, "source_sha256": sha256_text(source)}
        candidate, error = validate_candidate(row, "直到声音嘶哑。" + chr(0x4E7FF), [], [], {})
        self.assertIsNotNone(candidate)

    def test_duplicate_address_cyrillic_and_context_spill_are_not_admitted(self):
        source = "{$P$}さん、また明日。"
        row = {"source": source, "source_sha256": sha256_text(source)}
        for draft, code in [("{$P$}先生先生，明天见。", "duplicated_player_honorific"),
                            ("{$P$}先生，明天见 кла", "introduced_cyrillic")]:
            candidate, error = validate_candidate(row, draft, [], [], {})
            self.assertIsNone(candidate)
            self.assertIn(code, error)
        source = "それも嘘なの？"
        row = {"source": source, "source_sha256": sha256_text(source), "task": "ASSETS_LYRICS",
               "context_examples": [{"context": [{"relative": 0, "source": source}]}]}
        candidate, error = validate_candidate(row, "之前你说独自一人无法从漫长的噩梦里醒过来，那些难道也都是谎言吗？", [], [], {})
        self.assertIsNone(candidate)
        self.assertIn("possible_context_spill", error)

    def test_lyric_line_join_uses_the_same_rule_as_apply(self):
        source = "夢の続きへ\n歩こう"
        row = {"source": source, "source_sha256": sha256_text(source), "task": "ASSETS_LYRICS"}
        candidate, error = validate_candidate(row, "向着梦想的后续\n前进吧", [], [], {})
        self.assertFalse(error)
        self.assertEqual(candidate["translation"], "向着梦想的后续 前进吧")

    def test_suspicious_lyric_expansion_gets_one_targeted_retry(self):
        requests = []
        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            text = "之前你说独自一人无法从漫长的噩梦里醒过来，那些难道也都是谎言吗？" if len(requests) == 1 else "那也是谎言吗？"
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})
        async def run():
            source = "それも嘘なの？"
            row = {"source": source, "source_sha256": sha256_text(source), "task": "ASSETS_LYRICS",
                   "context_examples": [{"context": [{"relative": -1, "source": "一人で悪夢から目覚められない。"}]}]}
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                result, error = await translate_with_model(client, replace(config(), retries=2), None, "SYSTEM", row, {}, {}, defaultdict(lambda: defaultdict(float)))
                self.assertFalse(error)
                self.assertEqual(result["translation"], "那也是谎言吗？")
        asyncio.run(run())
        self.assertEqual(len(requests), 2)
        self.assertIn("Re-translate SOURCE alone", requests[1]["messages"][-1]["content"])

    def test_authoritative_terms_target_is_checked_as_the_preferred_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            glossary_path = Path(tmp) / "authoritative-terms.json"
            glossary_path.write_text(json.dumps({"entries": {
                "茜ちゃん": {"target": "小茜", "category": "character_nickname"}
            }}, ensure_ascii=False), encoding="utf-8")
            glossary = load_glossary(glossary_path)
        source = "茜ちゃん、また明日。"
        result = evaluate_row(
            {"source": source, "source_sha256": sha256_text(source), "task_hint": "DIALOGUE"},
            {"source": source, "source_sha256": sha256_text(source), "translation": "茜酱，明天见。"},
            glossary,
        )
        self.assertIn("preferred_term_missing", {issue["code"] for issue in result["issues"]})

    def test_production_cli_defaults_use_the_checked_in_authoritative_terms(self):
        args = build_parser().parse_args(["--config", "local-config.json"])
        self.assertEqual(args.glossary, Path("glossary/authoritative-terms.json"))
        self.assertEqual(args.authoritative_terms, Path("glossary/authoritative-terms.json"))

    def test_layout_budget_auto_reflow_and_retry_recovery(self):
        source = "{$P$}さん、今日の打ち合わせを確認してください。"
        row = {
            "source": source,
            "source_sha256": sha256_text(source),
            "task_hint": "DIALOGUE",
            "layout_category": "story",
        }
        # Exceeds single-line 54-unit ceiling, yet fits cleanly in 2 lines once reflowed.
        raw_reflowable = "__MLTD_TOKEN_000__先生，请先确认今天下午这场排练的流程细节吧。"
        candidate, error = validate_candidate(row, raw_reflowable, ["{$P$}"], [], {})
        self.assertFalse(error)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["translation"].count("\n"), 1)
        self.assertIn("{$P$}先生", candidate["translation"].replace("\n", ""))

        # Exceeds 2-line story capacity (108 width units): must fail layout QA with actionable feedback.
        too_long = "这段译文实在太长了即使拆成两行也肯定会超出对话框两行合计上限导致客户端发生溢出与截断现象必须精简一下表述才行。"
        rejected, err = validate_candidate(
            {"source": "確認してください。", "source_sha256": sha256_text("確認してください。"), "task_hint": "DIALOGUE", "layout_category": "story"},
            too_long,
            [],
            [],
            {},
        )
        self.assertIsNone(rejected)
        self.assertTrue(err.startswith("layout:"))
        self.assertIn("box layout budget", _build_qa_retry_feedback(err))

        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            text = too_long if len(requests) == 1 else "请确认一下，这次演出排练的流程。"
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

        async def run():
            story_row = {
                "source": "確認してください。",
                "source_sha256": sha256_text("確認してください。"),
                "task_hint": "DIALOGUE",
                "layout_category": "story",
            }
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                result, retry_err = await translate_with_model(
                    client, replace(config(), retries=2), None, "SYSTEM",
                    story_row, {}, {}, defaultdict(lambda: defaultdict(float)),
                )
                self.assertFalse(retry_err)
                self.assertEqual(result["translation"], "请确认一下，这次演出排练的流程。")

        asyncio.run(run())
        self.assertEqual(len(requests), 2)
        self.assertIn("box layout budget", requests[1]["messages"][-1]["content"])

    def test_risk_tier_escalates_reasoning_and_emits_prompt_warnings(self):
        cfg = config(request_params_by_task={
            "DIALOGUE": {"reasoning_effort": "low"},
            "TITLE": {"reasoning_effort": "none"},
        })
        ordinary = replace(cfg, min_reasoning_effort="")
        low_title = {"task_hint": "TITLE", "source": "設定"}
        self.assertEqual(task_request_params_for_row(ordinary, low_title)[1]["reasoning_effort"], "none")
        self.assertEqual(task_request_params_for_row(cfg, low_title)[1]["reasoning_effort"], "none")

        high_title = {
            "task_hint": "TITLE",
            "source": "あの日の約束を忘れない",
            "layout_category": "story",
            "usage_profile": {
                "categories": ["event", "main"],
                "multi_category": True,
                "requires_cross_context_consistency": True,
            },
        }
        _, high_params = task_request_params_for_row(ordinary, high_title)
        self.assertEqual(high_params["reasoning_effort"], "medium")
        self.assertGreaterEqual(high_params["max_tokens"], 1024)
        self.assertEqual(task_request_params_for_row(cfg, high_title)[1]["reasoning_effort"], "high")

        critical_row = {
            "task_hint": "DIALOGUE",
            "source": "準備はいい？",
            "usage_profile": {
                "categories": ["main"],
                "speaker_codes": ["har", "chi"],
                "multi_speaker": True,
            },
        }
        _, critical_params = task_request_params_for_row(ordinary, critical_row)
        self.assertEqual(critical_params["reasoning_effort"], "high")
        self.assertGreaterEqual(critical_params["max_tokens"], 1024)

        prompt_high = build_user_prompt(high_title, high_title["source"])
        self.assertIn("TRANSLATION_RISK=high", prompt_high)
        self.assertIn("OUTPUT_LAYOUT:", prompt_high)

        prompt_low = build_user_prompt(low_title, low_title["source"])
        self.assertNotIn("TRANSLATION_RISK=", prompt_low)

    def test_speaker_profile_and_voice_hint_are_scoped_to_single_speaker_dialogue_or_mail_title(self):
        profiles = {
            "har": {
                "speaker_code": "har",
                "canonical_name_zh": "天海春香",
                "speech_style": "元气坦率",
                "status": "accepted",
            }
        }
        evidence = {
            "har": {"speech_style": "备用提示不应覆盖已审资料"},
            "chi": {"speech_style": "冷静克制", "ja_samples": ["secret"]},
        }
        har_dialogue = {
            "task_hint": "DIALOGUE",
            "source": "今日も頑張ろうね！",
            "usage_profile": {"speaker_codes": ["har"]},
        }
        har_prompt = build_user_prompt(
            har_dialogue, har_dialogue["source"],
            character_profiles=profiles, speaker_evidence=evidence,
        )
        self.assertIn("SPEAKER_PROFILE=", har_prompt)
        self.assertIn("天海春香", har_prompt)
        self.assertNotIn('"status"', har_prompt)
        self.assertNotIn("SPEAKER_VOICE_HINT=", har_prompt)

        chi_dialogue = {
            "task_hint": "DIALOGUE",
            "source": "次へ進みましょう。",
            "usage_profile": {"speaker_codes": ["chi"]},
        }
        chi_prompt = build_user_prompt(
            chi_dialogue, chi_dialogue["source"],
            character_profiles=profiles, speaker_evidence=evidence,
        )
        self.assertIn("SPEAKER_VOICE_HINT=", chi_prompt)
        self.assertIn("冷静克制", chi_prompt)
        self.assertNotIn("ja_samples", chi_prompt)

        mail_title = {
            "task_hint": "TITLE",
            "surface_hint": "MAIL_TITLE",
            "source": "お疲れ様です",
            "usage_profile": {"speaker_codes": ["har"]},
        }
        self.assertIn("SPEAKER_PROFILE=", build_user_prompt(
            mail_title, mail_title["source"], character_profiles=profiles,
        ))

        mail_reply = {
            "task_hint": "DIALOGUE",
            "surface_hint": "MAIL_REPLY",
            "source": "ありがとう！",
            "usage_profile": {"speaker_codes": ["har"]},
        }
        reply_prompt = build_user_prompt(
            mail_reply, mail_reply["source"],
            character_profiles=profiles, speaker_evidence=evidence,
        )
        self.assertNotIn("SPEAKER_PROFILE=", reply_prompt)
        self.assertNotIn("SPEAKER_VOICE_HINT=", reply_prompt)


if __name__ == "__main__":
    unittest.main()
