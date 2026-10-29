import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipelines.core.hashing import sha256_text
from pipelines.core.jsonl import read_jsonl, write_jsonl
from pipelines.text.translate_mltd_api_pool import APIRequestError, ModelConfig
from scripts import translation_quality_experiment as experiment


class ExperimentTests(unittest.TestCase):
    def test_requests_exclude_hidden_reference_and_resume_refuses_changed_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = "また明日ね。"
            row = {"source": source, "source_sha256": sha256_text(source), "task_hint": "DIALOGUE",
                   "reference_translation": "HIDDEN-REFERENCE-MUST-STAY-LOCAL",
                   "previous_translation": "HIDDEN-REFERENCE-MUST-STAY-LOCAL",
                   "context_examples": [{"reference": "HIDDEN-REFERENCE-MUST-STAY-LOCAL", "context": [
                       {"relative": 0, "source": source, "translation": "HIDDEN-REFERENCE-MUST-STAY-LOCAL"}]}]}
            write_jsonl(root / "source.jsonl", [row])
            cfg = ModelConfig(id="test", model="alias", api_protocol="chat_completions", endpoint="https://example.invalid/v1/chat/completions",
                api_key_env="", concurrency=2, reasoning_effort="low", temperature=0.1, timeout=10, retries=1)
            prompts = []
            async def request(client, config, system, user, params):
                prompts.append(user)
                self.assertNotIn(row["reference_translation"], user)
                return "明天见。", {"reported_model": "backend", "usage": {}, "latency_seconds": 0.01}
            args = argparse.Namespace(input=root / "source.jsonl", config=root / "config.json", production=False,
                                      output=root / "results.jsonl", system_prompt=None, seed="test")
            with patch.object(experiment, "runtime_model", return_value=cfg), patch.object(experiment, "request_translation", side_effect=request):
                asyncio.run(experiment.run_pairs(args))
                self.assertEqual(len(prompts), 2)
                asyncio.run(experiment.run_pairs(args))
                self.assertEqual(len(prompts), 2, "a matching resume sends no new requests")
                with patch.object(experiment, "EDITOR_BRIEF", "Changed policy"):
                    with self.assertRaisesRegex(ValueError, "resume inputs/model/prompt changed"):
                        asyncio.run(experiment.run_pairs(args))
            self.assertEqual(len(read_jsonl(root / "results.jsonl")), 2)

    def test_blinding_keeps_mapping_and_references_out_of_judge_packet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = {"source_sha256": "id", "source": "原文", "stratum": "lyrics", "reference_translation": "secret-reference"}
            write_jsonl(root / "source.jsonl", [row])
            write_jsonl(root / "results.jsonl", [{"source_sha256": "id", "variant": v, "translation": v + "译文", "ok": True} for v in ["baseline", "candidate"]])
            args = argparse.Namespace(input=root / "source.jsonl", results=root / "results.jsonl", output=root / "blind.jsonl", mapping=root / "mapping.jsonl", seed="test")
            experiment.blind(args)
            packet, = read_jsonl(args.output)
            self.assertNotIn("reference_translation", packet)
            self.assertNotIn("variant", packet)
            mapping, = read_jsonl(args.mapping)
            winner = next(k for k in ["A", "B"] if mapping[k] == "candidate")
            write_jsonl(root / "votes.jsonl", [{"id": "id", "winner": winner, "severity_A": "none", "severity_B": "minor"}])
            experiment.summarize(argparse.Namespace(mapping=args.mapping, votes=root / "votes.jsonl", output=root / "summary.json"))
            report = json.loads((root / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(report["counts"]["candidate"], 1)
            self.assertEqual(report["kind"], "blinded_model_preference_not_accuracy")

    def test_output_inside_repo_is_refused(self):
        with self.assertRaisesRegex(ValueError, "outside the source checkout"):
            experiment.outside_repo(experiment.ROOT / "human-reference.json")

    def test_judge_stops_queued_work_on_quota_and_reports_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(root / "blind.jsonl", [{"id": str(i), "source": "原文", "A": "甲", "B": "乙"} for i in range(12)])
            cfg = ModelConfig(id="test", model="judge", api_protocol="chat_completions", endpoint="https://example.invalid/v1/chat/completions",
                api_key_env="", concurrency=3, reasoning_effort="high", temperature=0.1, timeout=10, retries=1)
            args = argparse.Namespace(input=root / "blind.jsonl", config=root / "config.json", output=root / "votes.jsonl", model=None)
            async def fail(*args):
                await asyncio.sleep(0)
                raise APIRequestError(429, "", "quota exhausted")
            with patch.object(experiment, "runtime_model", return_value=cfg), patch.object(experiment, "request_translation", side_effect=fail) as request:
                with self.assertRaisesRegex(RuntimeError, "judge incomplete: 12"):
                    asyncio.run(experiment.judge(args))
                self.assertLessEqual(request.call_count, 3, "only already in-flight calls may finish")
            write_jsonl(args.output, [{"id": "0", "judge_model": "stale"}])
            with patch.object(experiment, "runtime_model", return_value=cfg):
                with self.assertRaisesRegex(ValueError, "judge resume"):
                    asyncio.run(experiment.judge(args))

    def test_partial_evidence_requires_explicit_acknowledgment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(root / "mapping.jsonl", [{"id": str(i), "A": "baseline", "B": "candidate", "stratum": "lyrics"} for i in range(2)])
            write_jsonl(root / "votes.jsonl", [{"id": "0", "winner": "tie", "severity_A": "none", "severity_B": "none"}])
            args = argparse.Namespace(mapping=root / "mapping.jsonl", votes=root / "votes.jsonl", output=root / "summary.json")
            with self.assertRaisesRegex(ValueError, "incomplete judgment report"):
                experiment.summarize(args)
            self.assertEqual(json.loads(args.output.read_text())["missing_judgments"], 1)


if __name__ == "__main__":
    unittest.main()
