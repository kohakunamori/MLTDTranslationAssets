import argparse
import io
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import event_unit_localization as event
import japanese_residue as residue
import localization_coverage as coverage
import llm_translate_untranslated as llm
import official_bundle_families as families
import refresh_latest_official_catalogue as refresh
import validate_repo
import build_generated_release as build
from urllib.error import HTTPError, URLError


def accepted(ja, zh, key="dialogue_text"):
    return {"ja": ja, "zh": zh, "status": "accepted", "item_key": key,
            "source_sha256": event.sha256_text(ja)}


class EventTests(unittest.TestCase):
    def setUp(self):
        self.doc = {"commands": [{"text": "こんにちは", "command": 14, "idol": 1},
                                 {"text": "", "command": 1}, {"text": "別の台詞", "label": 5}],
                    "title": "untouched", "version": 3}
        self.rows = event.merge_rows("event_unit_talk_1.unity3d", event.sources(self.doc), [], "1", "9.0.200")
        self.rows[0].update(status="accepted", zh="你好", translation_stage="llm_translated")

    def test_source_bound_position_and_all_other_fields_are_preserved(self):
        original = copy.deepcopy(self.doc)
        changed, count = event.patch_document(self.doc, self.rows)
        self.assertEqual(count, 1)
        expected = copy.deepcopy(original)
        expected["commands"][0]["text"] = "你好"
        self.assertEqual(changed, expected)
        self.assertEqual(self.doc, original)

    def test_moved_or_changed_command_cannot_receive_stale_translation(self):
        self.doc["commands"][0]["text"] = "新しい台詞"
        changed, count = event.patch_document(self.doc, self.rows)
        self.assertEqual(count, 0)
        self.assertEqual(changed, self.doc)

    def test_tampered_source_hash_and_conflicting_drafts_are_refused(self):
        bad = dict(self.rows[0], source_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "source hash"):
            event.patch_document(self.doc, [bad])
        with self.assertRaisesRegex(ValueError, "conflicting"):
            event.patch_document(self.doc, [self.rows[0], dict(self.rows[0], zh="您好")])

    def test_merge_retains_accepted_rows_but_new_source_enters_queue(self):
        current = event.sources(self.doc)
        merged = event.merge_rows("event_unit_talk_1.unity3d", current, self.rows, "2", "9.0.200")
        self.assertEqual(merged, self.rows)
        current["commands[0].text"] = "新しい台詞"
        merged = event.merge_rows("event_unit_talk_1.unity3d", current, self.rows, "2", "9.0.200")
        self.assertEqual(merged[0]["status"], "untranslated")
        self.assertEqual(merged[0]["zh"], "")

    def test_atlases_are_not_json_dialogue_and_known_event_rows_never_enter_gtx(self):
        registry = families.load_registry()
        name = "event_unit_talk_123.unity3d"
        self.assertEqual(families.classify(registry, name), "event_unit_dialogue")
        self.assertIsNone(families.classify(registry, "event_unit_talk_chr_123.unity3d"))
        selected, _ = refresh.select_bundles({name: {}}, {name.casefold()}, registry)
        self.assertEqual(selected, {})

    def test_new_surface_is_not_silently_ignored_by_repository_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in (*validate_repo.CATEGORIES, "event-unit"):
                (root / "locales" / name).mkdir(parents=True)
            path = root / "locales/event-unit/event_unit_talk_1.unity3d.jsonl"
            path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows), encoding="utf-8")
            counts = validate_repo.validate_locales(root)
            self.assertEqual(counts["total_rows"], 2)
            bad = dict(self.rows[0], source_sha256="0" * 64)
            path.write_text(json.dumps(bad, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                validate_repo.validate_locales(root)


class CoverageTests(unittest.TestCase):
    def test_image_inventory_keeps_thumbnail_counts_and_does_not_claim_ocr(self):
        index = {"ex4c1.unity3d": {}, "ex4c1_thumb.unity3d": {}, "voice.acb": {}}
        result = coverage.image_coverage(index, {"ex4c1.unity3d"}, set())["ex4c"]
        self.assertEqual(result["official_bundles"], 2)
        self.assertEqual(result["reviewed_bundles"], 1)
        self.assertEqual(result["generated_bundles"], 0)

    def test_version_manifest_alone_never_means_sources_were_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifests").mkdir()
            (root / "manifests/asset-version.json").write_text('{"asset_version":"1"}')
            index = {"a_jp.gtx.unity3d": {"remote": "hash", "declared_size": 4}}
            missing = coverage.source_coverage(index, root)
            self.assertEqual(missing["gtx_text"]["unsynchronized_bundles"], ["a_jp.gtx.unity3d"])


class TransportTests(unittest.TestCase):
    def test_transient_official_download_retries_without_recording_partial_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle"
            with patch.object(build, "urlopen", side_effect=[URLError("TLS EOF"), io.BytesIO(b"abcd")]) as request, patch.object(build.time, "sleep"):
                self.assertEqual(build.download("https://example.test/bundle", path, 4), "fetched")
                self.assertEqual(request.call_count, 2)
            self.assertEqual(path.read_bytes(), b"abcd")
            self.assertFalse(path.with_suffix(".part").exists())

    def test_permanent_http_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(build, "urlopen", side_effect=HTTPError("url", 404, "missing", {}, None)) as request:
                with self.assertRaises(HTTPError):
                    build.download("https://example.test/missing", Path(tmp) / "bundle", 4)
                self.assertEqual(request.call_count, 1)


class ResidueTests(unittest.TestCase):
    def test_partial_mail_and_identical_question_are_repaired(self):
        ja = "今日もありがとうございます。これからもよろしくお願いします。"
        self.assertEqual(residue.repair_reason(accepted(ja, "今天也谢谢你。これからもよろしくお願いします。")), "copied Japanese sentence")
        self.assertIsNotNone(residue.repair_reason(accepted("憧れる……？", "憧れる……？")))

    def test_song_titles_names_and_short_interjections_remain_allowed(self):
        for row in (accepted("憧れる……？", "憧れる……？", "song_title_1"),
                    accepted("今日もありがとうございます。", "今日もありがとうございます。", "ld_misaki_memo_detail_17"),
                    accepted("ああ！", "ああ！"), accepted("ユニット", "ユニット", "unit_name")):
            self.assertIsNone(residue.repair_reason(row))
        self.assertIsNone(residue.repair_reason(accepted("02. だってあなたはプリンセス", "02. だってあなたはプリンセス", "ld_drama_information_6")))

    def test_repair_draft_failure_preserves_accepted_and_success_enters_publish_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "locales/card/a.jsonl"
            path.parent.mkdir(parents=True)
            row = dict(accepted("憧れる……？", "憧れる……？"), bundle="a_jp.gtx", asset_version="1")
            path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            queue = root / "queue.jsonl"
            draft = root / "draft.jsonl"
            with patch.object(llm, "ROOT", root):
                llm.collect(argparse.Namespace(scope="locales", asset_version=None, output=queue))
                self.assertEqual(len(queue.read_text(encoding="utf-8").splitlines()), 1)
                draft.write_text(json.dumps({"source": row["ja"], "source_sha256": row["source_sha256"], "translation": row["zh"]}, ensure_ascii=False) + "\n", encoding="utf-8")
                llm.apply(argparse.Namespace(scope="locales", draft=draft))
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), row)
                draft.write_text(json.dumps({"source": row["ja"], "source_sha256": row["source_sha256"], "translation": "憧憬……？"}, ensure_ascii=False) + "\n", encoding="utf-8")
                llm.apply(argparse.Namespace(scope="locales", draft=draft))
                updated = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(updated["status"], "pending")
                self.assertEqual(updated["translation_stage"], "llm_translated")


if __name__ == "__main__":
    unittest.main()
