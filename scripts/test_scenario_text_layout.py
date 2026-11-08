import json
import tempfile
import unittest
from pathlib import Path
from scripts.scenario_text_layout import Policy, apply_category, atoms, lint, policy_for, reflow, width


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


if __name__ == '__main__': unittest.main()
