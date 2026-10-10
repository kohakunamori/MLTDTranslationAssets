import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_translate_untranslated as tool


def _row(source, *, item_key="a", zh="", status="untranslated", stage=None, version="1"):
    row = {
        "asset_version": version, "client_version": None,
        "source_client_version": "9.0.200", "bundle": "x.gtx",
        "item_key": item_key, "source_sha256": tool.sha256_text(source),
        "ja": source, "zh": zh, "status": status,
        "updated_at": "2026-01-01T00:00:00Z",
    }
    if stage is not None:
        row["translation_stage"] = stage
    return row


def _write(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _read(path):
    # Split on real newlines only: a raw U+2028 inside a JSON string is legal and
    # ``str.splitlines()`` would break the row (see the separator regression test).
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.split("\n") if line.strip()]


class LlmDraftWorkflowTests(unittest.TestCase):
    def test_apply_marks_only_untranslated_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = root / "locales" / "story" / "x.jsonl"
            locale.parent.mkdir(parents=True)
            source = "こんにちは {$P$}"
            row = {
                "asset_version": "1", "client_version": None,
                "source_client_version": "9.0.200", "bundle": "x.gtx",
                "item_key": "a", "source_sha256": tool.sha256_text(source),
                "ja": source, "zh": "", "status": "untranslated",
                "updated_at": "2026-01-01T00:00:00Z",
            }
            accepted = dict(row, item_key="b", zh="旧译文", status="accepted")
            locale.write_text(json.dumps(row, ensure_ascii=False) + "\n" + json.dumps(accepted, ensure_ascii=False) + "\n", encoding="utf-8")
            draft = root / "draft.jsonl"
            draft.write_text(json.dumps({"source": source, "source_sha256": tool.sha256_text(source), "translation": "你好 {$P$}"}) + "\n", encoding="utf-8")
            with patch.object(tool, "ROOT", root):
                self.assertEqual(tool.apply(type("Args", (), {"draft": draft})()), 0)
            values = [json.loads(line) for line in locale.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(values[0]["status"], "pending")
            self.assertEqual(values[0]["translation_stage"], "llm_translated")
            self.assertEqual(values[1]["status"], "accepted")


class LlmPublishTests(unittest.TestCase):
    """`publish` admits machine drafts without claiming a human reviewed them."""

    def _fixture(self, root: Path) -> Path:
        locale = root / "locales" / "master" / "official-2-untranslated.jsonl"
        locale.parent.mkdir(parents=True)
        _write(locale, [
            _row("こんにちは {$P$}", item_key="llm", zh="你好 {$P$}", status="pending",
                 stage="llm_translated", version="2"),
            _row("さようなら {$P$}", item_key="human", zh="再见 {$P$}", status="accepted",
                 stage="human_translated", version="2"),
            _row("おはよう {$P$}", item_key="empty", zh="", status="untranslated",
                 stage="untranslated", version="2"),
        ])
        return locale

    def _publish(self, root: Path, **kwargs):
        args = type("Args", (), {"draft": kwargs.get("draft"), "dry_run": kwargs.get("dry_run", False)})()
        with patch.object(tool, "ROOT", root):
            return tool.publish(args)

    def test_publish_admits_only_machine_rows_and_keeps_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            untouched = locale.read_text(encoding="utf-8").splitlines()[1]
            self.assertEqual(self._publish(root), 0)
            rows = _read(locale)
            self.assertEqual([row["status"] for row in rows], ["accepted", "accepted", "untranslated"])
            # Provenance survives: the admitted row is still machine output.
            self.assertEqual(rows[0]["translation_stage"], "llm_translated")
            self.assertEqual(rows[1]["translation_stage"], "human_translated")
            # The rows publish did not touch keep their exact bytes.
            self.assertEqual(locale.read_text(encoding="utf-8").splitlines()[1], untouched)

    def test_publish_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            self._publish(root)
            first = locale.read_text(encoding="utf-8")
            self._publish(root)
            self.assertEqual(locale.read_text(encoding="utf-8"), first)

    def test_publish_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            before = locale.read_text(encoding="utf-8")
            self._publish(root, dry_run=True)
            self.assertEqual(locale.read_text(encoding="utf-8"), before)

    def test_draft_scope_limits_the_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            other = _row("こんばんは {$P$}", item_key="other", zh="晚上好 {$P$}",
                         status="pending", stage="llm_translated", version="2")
            _write(locale, _read(locale) + [other])
            draft = root / "draft.jsonl"
            _write(draft, [{"source": "こんばんは {$P$}",
                            "source_sha256": tool.sha256_text("こんばんは {$P$}"),
                            "translation": "晚上好 {$P$}"}])
            self._publish(root, draft=draft)
            rows = {row["item_key"]: row for row in _read(locale)}
            self.assertEqual(rows["other"]["status"], "accepted")
            self.assertEqual(rows["llm"]["status"], "pending")

    def test_publish_refuses_a_row_whose_source_hash_drifted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            locale = self._fixture(root)
            rows = _read(locale)
            rows[0]["source_sha256"] = "0" * 64
            _write(locale, rows)
            with self.assertRaises(SystemExit):
                self._publish(root)


class LlmPublishAmbiguityTests(unittest.TestCase):
    """The generated writer refuses two accepted wordings of one source text.

    Machine promotion must therefore never create that state, and must repair it
    when a previous run created it (observed 2026-10-08: four 1077640 rows
    duplicated 1077500 wording for `birth_bdl2_001har_005_jp.gtx` and aborted
    `build_generated_release.py`).
    """

    SOURCE = "さくらがきれいだね♪"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.locale = self.root / "locales" / "master" / "x.jsonl"
        self.locale.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _row(self, *, zh, status, stage, version, item_key="k"):
        row = _row(self.SOURCE, item_key=item_key, zh=zh, status=status, version=version)
        if stage is not None:
            row["translation_stage"] = stage
        return row

    def _publish(self, **kwargs):
        args = type("Args", (), {"draft": kwargs.get("draft"), "dry_run": kwargs.get("dry_run", False)})()
        with patch.object(tool, "ROOT", self.root):
            return tool.publish(args)

    def test_promotion_is_skipped_when_the_source_already_has_an_accepted_wording(self):
        _write(self.locale, [
            self._row(zh="樱花真漂亮♪", status="accepted", stage=None, version="1077500"),
            self._row(zh="樱花开得真美♪", status="pending", stage="llm_translated", version="1077640"),
        ])
        self._publish()
        rows = _read(self.locale)
        self.assertEqual([r["status"] for r in rows], ["accepted", "pending"])
        self.assertEqual(rows[0]["zh"], "樱花真漂亮♪")

    def test_a_conflicting_machine_duplicate_is_demoted(self):
        _write(self.locale, [
            self._row(zh="樱花真漂亮♪", status="accepted", stage=None, version="1077500"),
            self._row(zh="樱花开得真美♪", status="accepted", stage="llm_translated", version="1077640"),
        ])
        self._publish()
        rows = _read(self.locale)
        self.assertEqual([r["status"] for r in rows], ["accepted", "pending"])
        self.assertEqual(rows[1]["translation_stage"], "llm_translated")
        self.assertEqual(rows[1]["zh"], "樱花开得真美♪", "the machine draft is kept for review")

    def test_the_oldest_machine_wording_wins_when_no_human_row_exists(self):
        _write(self.locale, [
            self._row(zh="樱花真漂亮♪", status="accepted", stage="llm_translated", version="1077100"),
            self._row(zh="樱花开得真美♪", status="accepted", stage="llm_translated", version="1077640"),
        ])
        self._publish()
        rows = _read(self.locale)
        self.assertEqual([r["status"] for r in rows], ["accepted", "pending"])

    def test_human_and_legacy_rows_are_never_demoted(self):
        _write(self.locale, [
            self._row(zh="樱花真漂亮♪", status="accepted", stage=None, version="1077500"),
            self._row(zh="樱花真美♪", status="accepted", stage="human_translated", version="1077600"),
        ])
        self._publish()
        rows = _read(self.locale)
        self.assertEqual([r["status"] for r in rows], ["accepted", "accepted"])

    def test_a_conflict_free_repository_is_left_alone(self):
        _write(self.locale, [
            self._row(zh="樱花真漂亮♪", status="accepted", stage=None, version="1077500"),
            _row("またね♪", item_key="other", zh="再见♪", status="pending",
                 stage="llm_translated", version="1077710"),
        ])
        before = self.locale.read_text(encoding="utf-8")
        self._publish()
        after = _read(self.locale)
        self.assertEqual(after[0]["status"], "accepted")
        self.assertEqual(after[1]["status"], "accepted", "an unrelated machine draft is promoted")
        self.assertNotEqual(before, self.locale.read_text(encoding="utf-8"))


class LlmLyricsScopeTests(unittest.TestCase):
    """Song lyrics are a second source tree: same states, one extra rule."""

    def _lyric_root(self, root: Path) -> Path:
        songs = root / "lyrics" / "songs"
        songs.mkdir(parents=True)
        return songs

    def _song(self, path: Path, rows):
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")

    def _lyric_row(self, bundle, index, text, *, zh="", status="untranslated", stage=None):
        row = {
            "bundle": bundle, "index": index, "tick": index * 960, "abs_time": index / 10.0,
            "source_sha256": tool.sha256_text(text), "ja": text, "zh": zh,
            "status": status, "updated_at": "2026-01-01T00:00:00Z",
        }
        if stage is not None:
            row["translation_stage"] = stage
        return row

    def test_collect_queues_japanese_lines_and_skips_english_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            songs = self._lyric_root(root)
            self._song(songs / "scrobj_x.unity3d.jsonl", [
                self._lyric_row("scrobj_x.unity3d", 41, "一旦愛して♡"),
                self._lyric_row("scrobj_x.unity3d", 42, "I Love You"),
                self._lyric_row("scrobj_x.unity3d", 43, "ちょうだい", zh="已经译好", status="accepted"),
            ])
            out = root / "queue.jsonl"
            args = type("Args", (), {"output": out, "asset_version": "", "scope": "lyrics"})()
            with patch.object(tool, "ROOT", root):
                self.assertEqual(tool.collect(args), 0)
            queued = _read(out)
            self.assertEqual([row["source"] for row in queued], ["一旦愛して♡"])
            self.assertEqual(queued[0]["task"], "ASSETS_LYRICS")
            self.assertEqual(queued[0]["key"], "41")

    def test_default_scope_still_ignores_lyrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            songs = self._lyric_root(root)
            self._song(songs / "scrobj_x.unity3d.jsonl", [self._lyric_row("scrobj_x.unity3d", 41, "一旦愛して♡")])
            locale = root / "locales" / "master" / "x.jsonl"
            locale.parent.mkdir(parents=True)
            _write(locale, [_row("こんにちは {$P$}")])
            out = root / "queue.jsonl"
            args = type("Args", (), {"output": out, "asset_version": "", "scope": "locales"})()
            with patch.object(tool, "ROOT", root):
                self.assertEqual(tool.collect(args), 0)
            queued = _read(out)
            self.assertEqual([row["source"] for row in queued], ["こんにちは {$P$}"])
            self.assertEqual(queued[0]["task"], "ASSETS_TEXT")

    def test_apply_writes_pending_rows_into_the_song_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            songs = self._lyric_root(root)
            song = songs / "scrobj_x.unity3d.jsonl"
            self._song(song, [
                self._lyric_row("scrobj_x.unity3d", 41, "一旦愛して♡"),
                self._lyric_row("scrobj_x.unity3d", 42, "I Love You"),
            ])
            draft = root / "draft.jsonl"
            draft.write_text(json.dumps({
                "source": "一旦愛して♡", "source_sha256": tool.sha256_text("一旦愛して♡"),
                "translation": "先爱一下♡"}) + "\n", encoding="utf-8")
            args = type("Args", (), {"draft": draft, "scope": "lyrics"})()
            with patch.object(tool, "ROOT", root):
                self.assertEqual(tool.apply(args), 0)
            rows = _read(song)
            self.assertEqual(rows[0]["zh"], "先爱一下♡")
            self.assertEqual(rows[0]["status"], "pending")
            self.assertEqual(rows[0]["translation_stage"], "llm_translated")
            self.assertEqual(rows[1]["zh"], "", "the English bypass line is untouched")

    def test_publish_promotes_lyric_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            songs = self._lyric_root(root)
            song = songs / "scrobj_x.unity3d.jsonl"
            self._song(song, [
                self._lyric_row("scrobj_x.unity3d", 41, "一旦愛して♡", zh="先爱一下♡",
                                status="pending", stage="llm_translated"),
                self._lyric_row("scrobj_x.unity3d", 42, "ちょうだい", status="untranslated", stage="untranslated"),
            ])
            args = type("Args", (), {"draft": None, "dry_run": False, "scope": "lyrics"})()
            with patch.object(tool, "ROOT", root):
                self.assertEqual(tool.publish(args), 0)
            rows = _read(song)
            self.assertEqual([row["status"] for row in rows], ["accepted", "untranslated"])
            self.assertEqual(rows[0]["translation_stage"], "llm_translated")
            first = song.read_text(encoding="utf-8")
            with patch.object(tool, "ROOT", root):
                tool.publish(args)
            self.assertEqual(song.read_text(encoding="utf-8"), first)

    def test_disagreeing_wordings_of_one_line_stay_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            songs = self._lyric_root(root)
            song = songs / "scrobj_x.unity3d.jsonl"
            self._song(song, [
                self._lyric_row("scrobj_x.unity3d", 41, "一旦愛して♡", zh="先爱一下♡",
                                status="pending", stage="llm_translated"),
                self._lyric_row("scrobj_x.unity3d", 88, "一旦愛して♡", zh="暂且爱我吧♡",
                                status="pending", stage="llm_translated"),
            ])
            args = type("Args", (), {"draft": None, "dry_run": False, "scope": "lyrics"})()
            with patch.object(tool, "ROOT", root):
                self.assertEqual(tool.publish(args), 0)
            self.assertEqual([row["status"] for row in _read(song)], ["pending", "pending"])

    def test_locale_publish_never_touches_lyrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            songs = self._lyric_root(root)
            song = songs / "scrobj_x.unity3d.jsonl"
            self._song(song, [self._lyric_row("scrobj_x.unity3d", 41, "一旦愛して♡", zh="先爱一下♡",
                                              status="pending", stage="llm_translated")])
            locale = root / "locales" / "master" / "x.jsonl"
            locale.parent.mkdir(parents=True)
            _write(locale, [_row("こんにちは {$P$}", zh="你好 {$P$}", status="pending", stage="llm_translated")])
            before = song.read_text(encoding="utf-8")
            args = type("Args", (), {"draft": None, "dry_run": False})()
            with patch.object(tool, "ROOT", root):
                tool.publish(args)
            self.assertEqual(song.read_text(encoding="utf-8"), before)
            self.assertEqual(_read(locale)[0]["status"], "accepted")

    def test_a_unicode_line_separator_never_becomes_a_real_newline(self):
        """A raw U+2028 inside a JSON string is legal and must survive a rewrite.

        ``str.splitlines()`` treats it as a line break, so a naive apply/publish
        would split one row into two and corrupt the file (observed in
        ``lyrics/songs/scrobj_gf0000.unity3d.jsonl``).
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            songs = self._lyric_root(root)
            song = songs / "scrobj_x.unity3d.jsonl"
            source = "モヤモヤするわ！\u2028"
            self._song(song, [self._lyric_row("scrobj_x.unity3d", 41, source)])
            before_lines = song.read_text(encoding="utf-8").count("\n")
            draft = root / "draft.jsonl"
            draft.write_text(json.dumps({
                "source": source, "source_sha256": tool.sha256_text(source),
                "translation": "真让人心烦意乱！"}) + "\n", encoding="utf-8")
            with patch.object(tool, "ROOT", root):
                tool.apply(type("Args", (), {"draft": draft, "scope": "lyrics"})())
                tool.publish(type("Args", (), {"draft": None, "dry_run": False, "scope": "lyrics"})())
            text = song.read_text(encoding="utf-8")
            self.assertEqual(text.count("\n"), before_lines, "no extra line was created")
            rows = _read(song)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "accepted")
            self.assertEqual(rows[0]["zh"], "真让人心烦意乱！")
            self.assertTrue(rows[0]["ja"].endswith("\u2028"))


if __name__ == "__main__":
    unittest.main()
