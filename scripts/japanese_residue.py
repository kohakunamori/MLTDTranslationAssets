"""Conservative repair candidates, separate from the broad kana inventory.

Names, titles and short interjections may intentionally retain Japanese. Only
copied sentences with grammar, or an identical question with a verb, qualify.
"""
import re

KANA = re.compile(r"[ぁ-ゖァ-ヺ]")
HIRAGANA = re.compile(r"[ぁ-ゖ]")
GRAMMAR = re.compile(r"です|ます|でした|ました|して|って|ない|れる|から|けど|ても|だよ|だね|しょう")
DISPLAY_KEY = re.compile(r"(?:^|_)(?:name|title|song|memo)(?:_|$)", re.I)


def repair_reason(row: dict) -> str | None:
    if row.get("status") != "accepted" or not row.get("zh"):
        return None
    if DISPLAY_KEY.search(str(row.get("item_key", ""))):
        return None
    source, zh = str(row.get("ja", "")), str(row["zh"])
    if source == zh and re.search(r"[？?]", zh) and re.search(r"[一-龯][ぁ-ゖ]{2,}", zh):
        return "identical Japanese verb question"
    for sentence in re.split(r"[\r\n。！？!?]", source):
        sentence = sentence.strip()
        if re.match(r"^\d+\s*[.．]", sentence):
            continue  # numbered album track titles are deliberately retained
        if (len(KANA.findall(sentence)) >= 12 and len(HIRAGANA.findall(sentence)) >= 6
                and GRAMMAR.search(sentence) and sentence in zh):
            return "copied Japanese sentence"
    return None
