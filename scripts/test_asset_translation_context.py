"""Exercise the production collect -> prompt path with source-only evidence."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import llm_translate_untranslated as collector
from pipelines.text.asset_translation_context import speaker_evidence
from pipelines.text.translate_mltd_api_pool import build_user_prompt, classify_task


def row(source, key, *, bundle="scene.gtx", status="accepted", **extra):
    return {"ja": source, "zh": "参考译文" if status == "accepted" else "", "status": status,
            "source_sha256": collector.sha256_text(source), "bundle": bundle, "item_key": key,
            "asset_version": "1", **extra}


class ProductionContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, rows):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def collect(self):
        output = self.root / "queue.jsonl"
        with patch.object(collector, "ROOT", self.root):
            collector.collect(argparse.Namespace(scope="all", asset_version="", output=output))
        return [json.loads(line) for line in output.read_text(encoding="utf-8").split("\n") if line]

    def test_numbered_scene_has_original_neighbors_and_speaker(self):
        self.write("locales/story/scene.jsonl", [
            row("明日は練習です。", "scene_1000_p"),
            row("あそこで待っています。", "scene_1001_001har", status="untranslated"),
            row("一緒に行きましょう。", "scene_1002_p"),
            row("別の場面です。", "other_1000_p"),
        ])
        queued, = self.collect()
        prompt = build_user_prompt(queued, queued["source"])
        self.assertEqual(classify_task(queued), "DIALOGUE")
        self.assertIn("SPEAKER=001har", prompt)
        self.assertIn("明日は練習です。", prompt)
        self.assertIn("一緒に行きましょう。", prompt)
        self.assertIn('"speaker_code": "p"', prompt)
        self.assertNotIn("参考译文", prompt)
        self.assertNotIn("別の場面", prompt)

    def test_lyrics_use_slot_order_and_reach_lyric_prompt_contract(self):
        self.write("lyrics/songs/song.jsonl", [
            row("その先の空へ", "", bundle="song", index=30),
            row("手を伸ばして", "", bundle="song", index=20, status="untranslated"),
            row("夢の続きを", "", bundle="song", index=10),
        ])
        queued, = self.collect()
        prompt = build_user_prompt(queued, queued["source"])
        self.assertEqual(classify_task(queued), "ASSETS_LYRICS")
        self.assertIn("TASK=ASSETS_LYRICS", prompt)
        self.assertIn("夢の続きを", prompt)
        self.assertIn("その先の空へ", prompt)

    def test_reused_text_accumulates_speakers_and_does_not_privilege_first_scene(self):
        source = "これからもみんなと一緒に新しい舞台を作っていきましょう。"
        for speaker in ["001har", "002chi"]:
            self.write(f"locales/story/{speaker}.jsonl", [
                row(source, f"scene_1000_{speaker}", status="untranslated"),
                row("これからもよろしく。", "scene_1001_p"),
            ])
        queued, = self.collect()
        self.assertEqual(classify_task(queued), "SHARED")
        self.assertEqual(queued["usage_profile"]["speaker_codes"], ["001har", "002chi"])
        prompt = build_user_prompt(queued, source)
        self.assertIn("SPEAKER=MULTIPLE", prompt)
        self.assertIn('"speaker_code": "001har"', prompt)
        self.assertIn('"speaker_code": "002chi"', prompt)

    def test_master_delta_is_not_mistaken_for_a_transcript(self):
        self.write("locales/master/delta.jsonl", [
            row("前の行ですが別の抽出箇所。", "scene_1000_p"),
            row("待っています。", "scene_1001_001har", status="untranslated"),
        ])
        queued, = self.collect()
        self.assertEqual(queued["context_examples"], [])
        self.assertNotIn("PREVIOUS:", build_user_prompt(queued, queued["source"]))

    def test_numbering_gap_does_not_join_different_branches(self):
        self.write("locales/story/scene.jsonl", [
            row("別の選択肢を選んだ返事です。", "scene_1000_p"),
            row("ここで待っています。", "scene_2000_001har", status="untranslated"),
        ])
        queued, = self.collect()
        self.assertNotIn("別の選択肢を選んだ返事です。", build_user_prompt(queued, queued["source"]))

    def test_default_window_includes_three_source_turns_with_speaker_labels(self):
        self.write("locales/story/scene.jsonl", [
            row("缺失片段之前。", "scene_0999_p"),
            *[row(f"原文{i}。", f"scene_{i:04d}_001har", status="untranslated" if i == 1004 else "accepted")
              for i in range(1001, 1008)],
        ])
        queued, = self.collect()
        peers = queued["context_examples"][0]["context"]
        self.assertEqual([p["relative"] for p in peers], list(range(-3, 4)))
        self.assertEqual({p["speaker_code"] for p in peers}, {"001har"})
        self.assertNotIn("缺失片段之前", build_user_prompt(queued, queued["source"]))

    def test_empty_queue_and_event_without_speaker(self):
        self.assertEqual(self.collect(), [])
        self.write("locales/event-unit/talk.jsonl", [
            row("何を探しているの？", "commands[3].text", status="untranslated"),
            row("冷蔵庫を見てみよう。", "commands[4].text"),
        ])
        queued, = self.collect()
        self.assertEqual(classify_task(queued), "DIALOGUE")
        self.assertNotIn("SPEAKER=", build_user_prompt(queued, queued["source"]))

    def test_identity_evidence_comes_from_the_registry(self):
        path = self.root / "idols.json"
        path.write_text(json.dumps({"idols": [{"speaker_code": "001har", "name_jp": "天海春香"}]}), encoding="utf-8")
        evidence = speaker_evidence(path)
        self.assertEqual(evidence["speakers"]["001har"], {"name_jp": "天海春香"})
        self.assertEqual(evidence["speakers"]["p"]["name_jp"], "プロデューサー")

    def test_mail_keeps_its_writer_and_written_surface_without_guessing_branch_order(self):
        self.write("locales/master/delta.jsonl", [
            row("お返事を待っています。", "mail_051tmg_311000_text", status="untranslated"),
            row("別の選択肢への返事です。", "mail_051tmg_311100_text"),
        ])
        queued, = self.collect()
        prompt = build_user_prompt(queued, queued["source"])
        self.assertEqual(classify_task(queued), "DIALOGUE")
        self.assertIn("SURFACE=MAIL", prompt)
        self.assertIn("SPEAKER=051tmg", prompt)
        self.assertNotIn("NEXT:", prompt)

    def test_mail_titles_use_the_idol_identity_but_reply_options_use_producer_identity(self):
        self.write("locales/master/mail.jsonl", [
            row("木下ひなたです", "mail_035hin_1000_title", status="untranslated"),
            row("なんでも食べるぞ！", "mail_035hin_2000_reply1", status="untranslated"),
        ])
        queued = {item["key"]: item for item in self.collect()}
        title = queued["mail_035hin_1000_title"]
        reply = queued["mail_035hin_2000_reply1"]
        self.assertEqual(classify_task(title), "TITLE")
        self.assertEqual(title["surface_hint"], "MAIL_TITLE")
        title_prompt = build_user_prompt(title, title["source"])
        self.assertIn("SURFACE=MAIL_TITLE", title_prompt)
        self.assertIn("SPEAKER=035hin", title_prompt)
        self.assertEqual(classify_task(reply), "DIALOGUE")
        self.assertEqual(reply["surface_hint"], "MAIL_REPLY")
        reply_prompt = build_user_prompt(reply, reply["source"])
        self.assertIn("SURFACE=MAIL_REPLY", reply_prompt)
        self.assertIn("SPEAKER=p", reply_prompt)
        self.assertNotIn("SPEAKER=035hin", reply_prompt)


if __name__ == "__main__":
    unittest.main()
