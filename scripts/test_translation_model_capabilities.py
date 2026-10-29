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
    ModelConfig, load_model_config, request_translation,
    task_request_params_for_row, translate_with_model,
    validate_candidate,
)


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


if __name__ == "__main__":
    unittest.main()
