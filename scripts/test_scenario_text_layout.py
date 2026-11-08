import json
import tempfile
import unittest
from pathlib import Path
from scripts.scenario_text_layout import (Policy, apply_category, atoms, audit, blocking, lint,
                                          load_exceptions, policy_for, reflow, width)
from pipelines.core.hashing import sha256_text


class LayoutTests(unittest.TestCase):
    def test_width(self):
        self.assertEqual(width('甲A'), 3)
        self.assertEqual(width('e\u0301'), 1)

    def test_boundary(self):
        self.assertEqual(lint('甲' * 25, Policy()), [])
        self.assertEqual(lint('甲' * 26, Policy()), ['over_target'])
        self.assertIn('over_ceiling', lint('甲' * 28, Policy()))

    def test_reflow_preserves_characters(self):
        text = '今天的舞台非常精彩，大家都非常努力。' * 2
        out = reflow(text, Policy())
        self.assertEqual(out.replace('\n', ''), text)
        self.assertLessEqual(len(out.split('\n')), 2)
        self.assertNotIn('over_ceiling', lint(out, Policy()))

    def test_token_indivisible(self):
        text = '甲' * 15 + '{$P$}' + '乙' * 15
        out = reflow(text, Policy())
        self.assertIn('{$P$}', out)
        self.assertEqual(out.replace('\n', ''), text)

    def test_english_and_digits_indivisible(self):
        self.assertEqual(atoms('AB123甲'), ['AB123', '甲'])
        with self.assertRaises(ValueError): reflow('A' * 60, Policy())

    def test_too_long_refused(self):
        with self.assertRaises(ValueError): reflow('甲' * 55, Policy())

    def test_kinsoku(self):
        out = reflow('甲' * 24 + '，' + '乙' * 24, Policy())
        self.assertNotIn('kinsoku_start', lint(out, Policy()))

    def test_unknown_markup_refused(self):
        with self.assertRaises(ValueError): reflow('<color=red>甲</color>', Policy())

    def test_unknown_category_excluded(self):
        self.assertIsNone(policy_for('master', {'ja': '甲\n乙'}))

    def test_source_exception_preserved(self):
        self.assertEqual(policy_for('story', {'ja': '甲\n乙\n丙'}).lines, 3)
        self.assertEqual(policy_for('event-unit', {'ja': '甲\n乙\n丙'}).lines, 2)

    def test_blank_and_cr(self):
        self.assertIn('empty_line', lint('甲\n', Policy()))
        with self.assertRaises(ValueError): reflow('甲\r\n乙', Policy())

    def test_source_line_preference_does_not_merge(self):
        out = reflow('不要换台哦！\n……话说，这次的直播可没有广告啦～♪', Policy(), 2)
        self.assertEqual(out.count('\n'), 1)
        self.assertFalse(blocking(lint(out, Policy(), 2)))

    def test_mismatch_is_report_only(self):
        issues = lint('你好', Policy(), 2)
        self.assertIn('line_count_mismatch', issues)
        self.assertFalse(blocking(issues))

    def test_stable(self):
        text = '甲' * 20 + '\n' + '乙' * 20
        self.assertEqual(reflow(text, Policy()), text)


class ApplyTests(unittest.TestCase):
    def row(self, zh, key='item_001'):
        return {'item_key': key, 'bundle': 'event_unit_talk_1.unity3d', 'ja': '甲\n乙',
                'zh': zh, 'status': 'accepted', 'source_sha256': 'x'}

    def root(self, rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'locales' / 'event-unit').mkdir(parents=True)
        path = root / 'locales' / 'event-unit' / 'a.jsonl'
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
        return root, path

    def test_all_or_nothing_on_blocked_row(self):
        root, path = self.root([self.row('甲' * 50), self.row('甲' * 55)])
        before = path.read_text(encoding='utf-8')
        result = apply_category(root, 'event-unit')
        self.assertEqual(result['written'], 0)
        self.assertEqual(result['blocked'], 1)
        self.assertEqual(path.read_text(encoding='utf-8'), before)

    def test_apply_only_replaces_layout(self):
        root, path = self.root([self.row('甲' * 50)])
        result = apply_category(root, 'event-unit')
        self.assertEqual(result['written'], 1)
        row = json.loads(path.read_text(encoding='utf-8').strip())
        self.assertEqual(row['ja'], '甲\n乙')
        self.assertEqual(row['status'], 'accepted')
        self.assertEqual(row['source_sha256'], 'x')
        self.assertEqual(row['zh'].replace('\n', ''), '甲' * 50)
        self.assertFalse([i for i in lint(row['zh'], policy_for('event-unit', row)) if i != 'over_target'])

    def test_u2028_is_not_a_jsonl_separator(self):
        text = '甲' * 25 + '\u2028' + '乙' * 25
        root, path = self.root([self.row(text)])
        result = apply_category(root, 'event-unit')
        self.assertEqual(result['written'], 1)
        rows = [json.loads(s) for s in path.read_text(encoding='utf-8').split('\n') if s.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['zh'].replace('\n', ''), text)


class ExceptionTests(unittest.TestCase):
    def _root(self, zh):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'locales' / 'card').mkdir(parents=True)
        row = {'item_key': 'card_x_1', 'bundle': 'card_x', 'ja': '甲\n乙', 'zh': zh,
               'status': 'accepted', 'source_sha256': 'x'}
        (root / 'locales' / 'card' / 'c.jsonl').write_text(
            json.dumps(row, ensure_ascii=False) + '\n', encoding='utf-8')
        return root, row

    def _entry(self, row, digest):
        return {('locales/card/c.jsonl', row['item_key']): {
            'path': 'locales/card/c.jsonl', 'item_key': row['item_key'],
            'zh_sha256': digest, 'reason': 'over_budget_beyond_2x54'}}

    def test_exception_removes_blocking_but_stays_visible(self):
        root, row = self._root('甲' * 60)
        exc = self._entry(row, sha256_text(row['zh']))
        records = audit(root, 'card', exc)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['issues'], [])
        self.assertIn('excepted', records[0])
        self.assertFalse(blocking(records[0]['issues']))

    def test_stale_exception_is_blocking(self):
        root, row = self._root('甲' * 60)
        records = audit(root, 'card', self._entry(row, '0' * 64))
        self.assertIn('stale_exception', records[0]['issues'])
        self.assertTrue(blocking(records[0]['issues']))

    def test_unused_exception_is_reported(self):
        root, row = self._root('甲' * 20)
        exc = {('locales/card/gone.jsonl', 'card_gone'): {
            'path': 'locales/card/gone.jsonl', 'item_key': 'card_gone',
            'zh_sha256': '0' * 64, 'reason': 'over_budget_beyond_2x54'}}
        records = audit(root, 'card', exc)
        self.assertTrue(any('stale_exception' in record['issues'] for record in records))

    def test_apply_leaves_excepted_row_untouched(self):
        root, row = self._root('甲' * 60)
        path = root / 'locales' / 'card' / 'c.jsonl'
        before = path.read_text(encoding='utf-8')
        result = apply_category(root, 'card', self._entry(row, sha256_text(row['zh'])))
        self.assertEqual(result['written'], 0)
        self.assertEqual(result['excepted'], 1)
        self.assertEqual(path.read_text(encoding='utf-8'), before)

    def test_extra_lines_override_reflows_to_three_lines(self):
        root, row = self._root('甲' * 60)
        entry = {('locales/card/c.jsonl', row['item_key']): {
            'path': 'locales/card/c.jsonl', 'item_key': row['item_key'],
            'ja_sha256': sha256_text(row['ja']), 'mode': 'reflow_extra_lines',
            'max_lines': 3, 'reason': 'over_budget_needs_third_line'}}
        before = audit(root, 'card', entry)
        self.assertTrue([r for r in before if blocking(r['issues'])])
        result = apply_category(root, 'card', entry)
        self.assertEqual(result['written'], 1)
        self.assertEqual(result['reflowed'], 1)
        after = audit(root, 'card', entry)
        self.assertFalse([r for r in after if blocking(r['issues'])])
        updated = json.loads((root / 'locales' / 'card' / 'c.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(len(updated['zh'].split('\n')), 3)
        self.assertEqual(updated['zh'].replace('\n', ''), '甲' * 60)

    def test_load_exceptions_validates_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'e.json'
            path.write_text('[{"path": "a", "item_key": "b"}]', encoding='utf-8')
            with self.assertRaises(ValueError):
                load_exceptions(path)


if __name__ == '__main__': unittest.main()
