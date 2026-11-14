"""Check committed name evidence in the CI job that checks out locales/.

Keep this module stdlib-only so the validate job needs no test dependencies.
Prompt behavior belongs in test_given_name_hints.py, whose offline CI checkout
deliberately omits the locale trees.
"""

import json
import unittest
from pathlib import Path

from pipelines.text.mltd_given_name_hints import load_given_name_hints

ROOT = Path(__file__).resolve().parents[1]
HINTS_PATH = ROOT / "glossary" / "given-name-hints.json"
EXPECTED = {
    "このみ": ("木实", 39, "馬場このみ", "马场木实"),
    "やよい": ("弥生", 5, "高槻やよい", "高槻弥生"),
    "あずさ": ("梓", 10, "三浦あずさ", "三浦梓"),
    "まつり": ("茉莉", 21, "徳川まつり", "德川茉莉"),
    "のり子": ("法子", 43, "福田のり子", "福田法子"),
    "エレナ": ("艾琳娜", 18, "島原エレナ", "岛原艾琳娜"),
    "ひなた": ("日向", 35, "木下ひなた", "木下日向"),
    "紬": ("䌷", 51, "白石紬", "白石䌷"),
    "亜利沙": ("亚利沙", 28, "松田亜利沙", "松田亚利沙"),
    "雪歩": ("雪步", 4, "萩原雪歩", "萩原雪步"),
    "伊織": ("伊织", 7, "水瀬伊織", "水濑伊织"),
    "貴音": ("贵音", 8, "四条貴音", "四条贵音"),
    "亜美": ("亚美", 11, "双海亜美", "双海亚美"),
    "響": ("响", 13, "我那覇響", "我那霸响"),
    "琴葉": ("琴叶", 17, "田中琴葉", "田中琴叶"),
    "恵美": ("惠美", 20, "所恵美", "所惠美"),
    "紗代子": ("纱代子", 27, "高山紗代子", "高山纱代子"),
    "千鶴": ("千鹤", 38, "二階堂千鶴", "二阶堂千鹤"),
    "環": ("环", 40, "大神環", "大神环"),
    "風花": ("风花", 41, "豊川風花", "丰川风花"),
    "可憐": ("可怜", 45, "篠宮可憐", "篠宫可怜"),
    "莉緒": ("莉绪", 46, "百瀬莉緒", "百濑莉绪"),
    "麗花": ("丽花", 48, "北上麗花", "北上丽花"),
    "歌織": ("歌织", 52, "桜守歌織", "樱守歌织"),
}


class GivenNameHintAuthorityTests(unittest.TestCase):
    def test_authority_entries_match_glossary_and_roster(self):
        document = load_given_name_hints(HINTS_PATH)
        actual = {
            entry.source_form: (
                entry.target,
                entry.idol_id,
                entry.canonical_source,
                entry.canonical_target,
            )
            for entry in document.entries
        }
        self.assertEqual(actual, EXPECTED)
        for entry in document.entries:
            self.assertEqual(
                entry.review_basis,
                "project_evidence+agent_review_not_human_reviewed",
            )

        glossary = json.loads(
            (ROOT / "glossary" / "authoritative-terms.json").read_text(
                encoding="utf-8-sig"
            )
        )["entries"]
        roster = json.loads(
            (ROOT / "glossary" / "idols.json").read_text(encoding="utf-8-sig")
        )["idols"]
        by_id = {row["idol_id"]: row for row in roster}
        for entry in document.entries:
            self.assertEqual(
                glossary[entry.canonical_source]["target"],
                entry.canonical_target,
            )
            self.assertTrue(entry.canonical_source.endswith(entry.source_form))
            self.assertTrue(entry.canonical_target.endswith(entry.target))
            self.assertEqual(by_id[entry.idol_id]["name_jp"], entry.canonical_source)
            self.assertEqual(by_id[entry.idol_id]["name_zh"], entry.canonical_target)

        typed_keys = {entry.typed_record_key for entry in document.entries}
        typed_rows = {}
        with (ROOT / "locales" / "master" / "MD_jp.gtx.jsonl").open(
            encoding="utf-8-sig"
        ) as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("item_key") in typed_keys:
                    typed_rows[row["item_key"]] = (row.get("ja"), row.get("zh"))
        for entry in document.entries:
            self.assertEqual(
                typed_rows[entry.typed_record_key],
                (entry.source_form, entry.target),
            )


if __name__ == "__main__":
    unittest.main()
