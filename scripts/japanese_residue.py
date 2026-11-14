"""Conservative repair candidates, separate from the broad kana inventory.

Names, titles and short interjections may intentionally retain Japanese. Only
copied sentences with grammar, or an identical question with a verb, qualify.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipelines.text.mltd_translation_quality import find_truncated_katakana_hybrid  # noqa: E402

KANA = re.compile(r"[ぁ-ゖァ-ヺ]")
HIRAGANA = re.compile(r"[ぁ-ゖ]")
GRAMMAR = re.compile(r"です|ます|でした|ました|して|って|ない|れる|から|けど|ても|だよ|だね|しょう")
QUOTED_TEXT = re.compile(r"「[^」\n]*」|『[^』\n]*』")
PARTIAL_CLAUSE = re.compile(
    r"[一-龯ァ-ヺぁ-ゖ]{2,}(?:させよう|してください|でしょうか|ませんか)"
    r"|(?:[一-龯ァ-ヺぁ-ゖ]{2,}|^|[\r\n\s])(?:知ってるけど|分かってない)"
    r"|ぜんぜん[ぁ-ゖ一-龯]"
    r"|わたくし|かしみや|ぴょーん|必ゅ|さ玛|っ斯|故意に[一-龯]|憧れ[一-龯]"
)
DISPLAY_KEY = re.compile(r"(?:^|_)(?:name|title|song|memo)(?:_|$)", re.I)


def repair_reason(row: dict) -> str | None:
    if row.get("status") != "accepted" or not row.get("zh"):
        return None
    if DISPLAY_KEY.search(str(row.get("item_key", ""))):
        return None
    source, zh = str(row.get("ja", "")), str(row["zh"])
    if source == zh and re.search(r"[？?]", zh) and re.search(r"[一-龯][ぁ-ゖ]{2,}", zh):
        return "identical Japanese verb question"
    unquoted = QUOTED_TEXT.sub("", zh)
    if source != zh and PARTIAL_CLAUSE.search(unquoted):
        return "partial Japanese clause"
    if source != zh and find_truncated_katakana_hybrid(source, unquoted):
        return "truncated katakana hybrid"
    for sentence in re.split(r"[\r\n。！？!?]", source):
        sentence = sentence.strip()
        if re.match(r"^\d+\s*[.．]", sentence):
            continue  # numbered album track titles are deliberately retained
        if (len(KANA.findall(sentence)) >= 12 and len(HIRAGANA.findall(sentence)) >= 6
                and GRAMMAR.search(sentence) and sentence in zh):
            return "copied Japanese sentence"
    return None
