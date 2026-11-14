#!/usr/bin/env python3
"""Deterministic quality checks for MLTD Simplified Chinese translation rows."""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipelines.core.hashing import sha256_text  # noqa: E402
from pipelines.core.jsonl import read_jsonl, write_jsonl  # noqa: E402
from pipelines.text.authoritative_term_matching import find_term_matches  # noqa: E402
from pipelines.text.translation_tokens import (  # noqa: E402
    KANA_RE,
    PROTECTED_TOKEN_RE,
    validate_translation,
)
from pipelines.text.mltd_lyric_rules import validate_lyric_tokens  # noqa: E402

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
JP_PERCENT_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)パーセント")
# Pipeline placeholders are normally restored before QA. Mask any that survive
# so the script check below never mistakes infrastructure for translated text.
# The shape is matched case-insensitively and exactly: exactly three index
# digits or the literal letters NNN inside the wrapper.  __MLTD_TERM_nnn__ and
# __mltd_TOKEN_NNN__ are the same infrastructure with the wrapper's case lost,
# while ordinary "NNN" text, __MLTD_FOO_NNN__ and __MLTD_TERM_NNNN__ stay
# content.
PIPELINE_MARKER_RE = re.compile(
    r"__MLTD_(?:TERM|TOKEN|NUMBER)_(?:\d{3}|NNN)__", re.IGNORECASE
)
TRADITIONAL_CHAR_MAP_PATH = (
    REPO / "pipelines/text/configs/traditional-to-simplified-char-map.json"
)
FALLBACK_TRADITIONAL_OUTPUT_CHARS = set(
    "妳製觀藝體這為會來說對沒裡還時學應與發萬張聲點門間從將讓見過麼麗實該閉腦現給難當國們歡聽獎續緊認謝種樣個開關頭書話寫讀轉選氣亞達場樂"
)


def load_traditional_char_map(path: Path = TRADITIONAL_CHAR_MAP_PATH) -> dict[str, str]:
    """Load the runtime-portable one-code-point Traditional->Simplified map."""
    if path.is_file():
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        mapping = doc.get("map", {}) if isinstance(doc, dict) else {}
        if isinstance(mapping, dict):
            clean = {
                str(source): str(target)
                for source, target in mapping.items()
                if len(str(source)) == 1
                and len(str(target)) == 1
                and str(source) != str(target)
            }
            if clean:
                return clean
    # Partial checkouts retain the earlier conservative gate rather than failing.
    return {ch: ch for ch in FALLBACK_TRADITIONAL_OUTPUT_CHARS}


TRADITIONAL_CHAR_MAP = load_traditional_char_map()
TRADITIONAL_OUTPUT_CHARS = set(TRADITIONAL_CHAR_MAP)
JP_FINAL_ADVERSATIVE_RE = re.compile(r"(?:けど(?:も)?|けれど(?:も)?|ですが|だが)\s*[………。！？!?♪～〜]*\s*$")
ZH_ADVERSATIVE_RE = re.compile(r"(?:不过|但是|可是|但|然而|只是|倒是|虽然)")
JP_INDEFINITE_CUE_RE = re.compile(r"(?:何|なに|なん|誰|だれ|どこ|いつ|どれ|どの|どんな|どういう)")
# The 什么 within 为什么 is an interrogative reason, not a newly invented object.
ZH_INDEFINITE_OBJECT_RE = re.compile(r"(?:(?<!为)什么|某个|某种|某些)")
JP_BAD_MEANING_RE = re.compile(r"悪い意味")
ZH_BAD_MEANING_CUE_RE = re.compile(
    r"(?:坏(?:的)?(?:意思|意义|处|方面|事)|不好的(?:意思|意义|方面)|不好(?:意义|方面)|(?:不是说|并非|不算|并不是)不好|"
    r"恶意|贬义|贬低|负面(?:的)?(?:意思|意义)|批评|嘲笑|往坏处|往坏的方面)"
)
JP_KONDO_FUTURE_RE = re.compile(r"今度[\s\S]{0,120}(?:します|いたします|するよ|するね|しよう|したい|行く|来る|持って|差し入れ)")
ZH_THIS_TIME_RE = re.compile(r"(?:这次|此次)")
JP_HANASHI_RE = re.compile(r"話")
ZH_STORY_RE = re.compile(r"故事")
JP_MOTORBIKE_RE = re.compile(r"バイク")
JP_BICYCLE_COMPOUND_RE = re.compile(
    r"(?:マウンテンバイク|ロードバイク|クロスバイク|エアロバイク|フィットネスバイク|ミニバイク|ポケバイ)"
)
JP_TRIATHLON_RE = re.compile(r"トライアスロン")
ZH_MOTORBIKE_CUE_RE = re.compile(r"(?:摩托|机车|機車|重机|重機|电单车|電單車)")
ZH_BICYCLE_RE = re.compile(r"(?:自行车|自行車|单车|單車|脚踏车|腳踏車)")
JP_STANDALONE_GEKI_RE = re.compile(r"(?<!演)劇(?!場|団|團|的)")
ZH_GENERIC_PERFORMANCE_RE = re.compile(r"演出")
ZH_PLAY_CUE_RE = re.compile(r"(?:舞台剧|舞台劇|戏剧|戲劇|话剧|話劇|剧|劇)")
JP_TACKLE_RE = re.compile(r"タックル")
ZH_GENERIC_COLLISION_RE = re.compile(r"冲撞")
ZH_TACKLE_SPECIFIC_RE = re.compile(r"(?:擒抱|抱摔|阻截|铲球|鏟球)")
ZH_AWKWARD_LATE_EFFORT_RE = re.compile(r"(?:一直)?(?:留|待)(?:到|至)很晚.{0,8}(?:努力着|加油着)")
JP_FIRST_PERSON_PLURAL_CUE_RE = re.compile(r"(?:私たち|私達|わたしたち|僕たち|僕達|ぼくたち|俺たち|俺達|おれたち|我々|われわれ|みんなで|皆で|一緒に|(?:ワタシ|アタシ|ボク|オレ)(?:たち|達)|自分(?:たち|達)|(?:私|僕|俺|ワタシ|ボク|オレ|ウチ)(?:ら|等)|うちら|こっちのチーム)")
ZH_FIRST_PERSON_PLURAL_RE = re.compile(r"(?:我们|我們|咱们|咱們)")
JP_GUEST_RE = re.compile(r"(?:お客(?:さん|様)?|客(?:さん|様)?)")
JP_EXPLICIT_AUDIENCE_CONTEXT_RE = re.compile(r"(?:ライブ|ステージ|会場|客席|観客|コンサート|公演|劇場)")
ZH_AUDIENCE_RE = re.compile(r"(?:观众|觀眾)")
JP_OUEN_RE = re.compile(r"応援")
ZH_OUEN_LOAN_RE = re.compile(r"(?:应援|應援)")
JP_COQUETTISH_EXCL_RE = re.compile(r"(?:や[ぁあ]?|いや)[～〜ー]*んっ?[！!]")
ZH_NEUTRAL_SURPRISE_RE = re.compile(r"(?:哎呀|哎哟|哎呦)")
JP_MILD_ACCEPTANCE_RE = re.compile(r"(?:まあ|ま)[、,]?\s*いいか")
ZH_STRONG_RESIGNATION_RE = re.compile(r"(?:算了|就这样吧|就這樣吧|就这么着吧|就這麼著吧)")
PLAYER_HONORIFIC = "{$P$}さん"
PLAYER_HONORIFIC_ZH = "{$P$}先生"
JP_HONORIFIC_RESIDUAL_RE = re.compile(r"(?:ちゃん|くん|さん|さま)")
JP_CHAN_HONORIFIC_RE = re.compile(r"ちゃん(?!と)")
# Be conservative when SOURCE also contains a food/sauce term that can
# legitimately produce Chinese 酱.  In that mixed case the generic gate abstains
# rather than guessing which 酱 occurrence came from ちゃん.
JP_SAUCE_HINT_RE = re.compile(r"(?:ジャム|ケチャップ|ソース|味噌|みそ|醤|タレ|たれ)")
# Narrow blocking detector for Japanese grammar accidentally left attached to
# otherwise Chinese output.  Do not make all kana blocking: song/brand/person
# names may legitimately remain Japanese when no authoritative localization is
# available.  These patterns instead target auxiliaries, sentence particles and
# Japanese-only elongation/small-tsu tails that are not valid zh-CN grammar.
JP_GRAMMAR_RESIDUAL_RE = re.compile(
    r"(?:なのです|のです|でした|でしょう|です|ました|ません|ましょう|ます|"
    r"だよ|だね|だぞ|だぜ|だわ|だな|だろう|でしょ|じゃん|"
    r"やん|やで|やろ|やろう|やねん|やから|やけど|やな)"
    r"[ー～〜っッ！？!?♪…]*(?![\u3040-\u30ff])"
    r"|[\u3400-\u9fff](?:さ|ね|よ|ぞ|ぜ|な|の)[ー～〜っッ！？!?♪…]+"
    r"|[\u3400-\u9fff](?:っ|ッ|ー)(?=[！？!?♪…\s]|$)"
)
# Narrow zh-CN style blocker for the common ACG calque "的说" when it is used
# as a sentence-final particle.  Do not match normal words such as "这样的说法".
ZH_SENTENCE_FINAL_DE_SHUO_RE = re.compile(
    r"的说(?=(?:[！!。…～〜♪☆？?，,]|\\\d+\\|\s*$))"
)
META_PATTERNS = (
    "译者注",
    "翻译注",
    "原文意思",
    "这里的意思",
    "可能是：",
    "可能是:",
    "变成：",
    "变成:",
    "translation:",
    "translator note",
    "[::thought",
    "🫧thought",
    "[::]",
    "[::-]",
    "[:::]",
    "adrayelejson",
    "unerquicklich",
    "offsetof",
    "taamatut",
    "geschniegelt",
    "encapsed",
    "rel久久热",
    "umyzyň",
)

# Upstream proxy gateways occasionally return HTTP 200 chat/response payloads
# whose message text is a suspended-account, ToS, quota, or AI-refusal notice.
PROVIDER_ERROR_RE = re.compile(
    r"this service has been disabled"
    r"|violation of terms of service"
    r"|submit an appeal to continue"
    r"|account has been (?:disabled|suspended|banned|terminated)"
    r"|account is (?:disabled|suspended)"
    r"|exceeded your current quota"
    r"|insufficient[_ ]quota"
    r"|\b(?:too many requests|rate limit exceeded|service unavailable|bad gateway)\b"
    r"|\b(?:upstream error|model is currently overloaded)\b"
    r"|i'?m sorry,?\s+(?:but\s+)?i can(?:not|'t)\s+(?:assist|help|translate|comply|fulfill)"
    r"|\bas an ai(?:\s+(?:language\s+)?model)?\b",
    re.IGNORECASE,
)


def detect_provider_error_text(text: str) -> str | None:
    """Return the matched upstream provider error/refusal snippet, if any."""
    if not text:
        return None
    match = PROVIDER_ERROR_RE.search(text)
    return match.group(0) if match else None


def source_id(text: str) -> str:
    return sha256_text(text)


def load_glossary(path: Path | None) -> dict:
    if path is None:
        return {"entries": {}, "kana_allowlist": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("glossary must be a JSON object")
    entries = value.get("entries", {})
    if not isinstance(entries, dict):
        raise ValueError("glossary.entries must be an object")
    allow = value.get("kana_allowlist", [])
    if not isinstance(allow, list):
        raise ValueError("glossary.kana_allowlist must be an array")
    return {"entries": entries, "kana_allowlist": [str(x) for x in allow]}


def index_unique(rows: list[dict], label: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        source = str(row.get("source", ""))
        sid = str(row.get("source_sha256", "")) or source_id(source)
        if not source or sid != source_id(source):
            raise ValueError(f"{label}: invalid source identity {sid!r}")
        if sid in out:
            raise ValueError(f"{label}: duplicate source_sha256 {sid}")
        out[sid] = row
    return out


def visible_numbers(text: str) -> Counter[str]:
    value = PROTECTED_TOKEN_RE.sub("", text)
    value = re.sub(r"(\d+(?:\.\d+)?)(?:パーセント|％)", lambda m: m.group(1) + "%", value)
    return Counter(NUMBER_RE.findall(value))


# Standard simplified Chinese words where 著 is orthographic rather than
# the Traditional auxiliary 著 (= 着); exclude "著名流" and "著作笔记" which
# are real "着 + 名流 / 作笔记" regressions disguised next to those prefixes.
SIMPLIFIED_ZHU_COMPOUND_RE = re.compile(
    r"著名(?!流)|著作(?!笔记)|名著|原著|显著|执著|著称|著者|编著|合著|巨著|专著|"
    r"译著|遗著|新著|旧著|拙著|土著|昭著|着作|(?<![讨争议辩谈理结无言论])论著"
)

def introduced_traditional_chars(source: str, translation: str) -> Counter[str]:
    clean_source = SIMPLIFIED_ZHU_COMPOUND_RE.sub("", source)
    # Authoritative zh-CN surname+given-name spelling 篠宫 uses simplified 宫
    # with orthodox 篠 even when Japanese source only used a given name/pronoun.
    clean_translation = SIMPLIFIED_ZHU_COMPOUND_RE.sub("", translation).replace("篠宫", "")
    source_counts = Counter(ch for ch in clean_source if ch in TRADITIONAL_OUTPUT_CHARS)
    translation_counts = Counter(
        ch for ch in clean_translation if ch in TRADITIONAL_OUTPUT_CHARS
    )
    return Counter(
        {
            ch: count - source_counts.get(ch, 0)
            for ch, count in translation_counts.items()
            if count > source_counts.get(ch, 0)
        }
    )


INTRODUCED_BLOCKED_SCRIPTS = (
    "HANGUL",
    "DEVANAGARI",
    "THAI",
    "MALAYALAM",
    "ARMENIAN",
    "GUJARATI",
    "KANNADA",
    "GEORGIAN",
    "HEBREW",
    "ARABIC",
    "BUBBLES",
    "THOUGHT BALLOON",
    "SPEECH BALLOON",
)
INTRODUCED_INVISIBLE_FORMAT_CODEPOINTS = frozenset({
    0x00A0,  # NO-BREAK SPACE
    0x200B,  # ZERO WIDTH SPACE
    0x200F,  # RIGHT-TO-LEFT MARK
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
})


def script_scan_text(value: str) -> str:
    """Return value with formatting tokens and pipeline markers removed."""
    return PIPELINE_MARKER_RE.sub("", PROTECTED_TOKEN_RE.sub("", value))


def pipeline_marker_counts(value: str) -> Counter[str]:
    """Return the exact pipeline-marker multiset carried by ``value``."""
    return Counter(PIPELINE_MARKER_RE.findall(value))


def pipeline_marker_sequence(value: str) -> list[str]:
    """Return pipeline markers in their original textual order."""
    return PIPELINE_MARKER_RE.findall(value)


def pipeline_marker_delta(
    source: str, translation: str
) -> tuple[Counter[str], Counter[str], bool]:
    """Return added/missing markers and whether equal multisets were reordered."""
    source_sequence = pipeline_marker_sequence(source)
    translation_sequence = pipeline_marker_sequence(translation)
    source_counts = Counter(source_sequence)
    translation_counts = Counter(translation_sequence)
    added = translation_counts - source_counts
    missing = source_counts - translation_counts
    order_changed = not added and not missing and source_sequence != translation_sequence
    return (
        added,
        missing,
        order_changed,
    )


def introduced_blocked_script_codepoints(source: str, translation: str) -> list[tuple[str, int]]:
    """Return (script, codepoint) pairs introduced by translation only.

    Hangul and Devanagari are legitimate content when SOURCE itself uses them
    (for example a Korean or Devanagari title).  Only characters absent from
    SOURCE are residue.  This deliberately does not ban other non-Chinese
    scripts or ordinary kana, which stays a non-blocking review signal below.
    """
    source_chars = set(script_scan_text(source))
    introduced: list[tuple[str, int]] = []
    seen: set[int] = set()
    for char in script_scan_text(translation):
        code = ord(char)
        if char in source_chars or code in seen:
            continue
        name = unicodedata.name(char, "")
        for script in INTRODUCED_BLOCKED_SCRIPTS:
            if script in name:
                seen.add(code)
                introduced.append((script, code))
                break
    return introduced


def introduced_invisible_format_codepoints(source: str, translation: str) -> list[int]:
    """Return unsupported invisible/space-control codepoints introduced by translation.

    Counts, rather than SOURCE membership, are the compatibility rule: equal or
    fewer occurrences than SOURCE may be preserved, while additional copies
    introduced by the translation remain residue.
    Ordinary spaces, line breaks, and U+3000 IDEOGRAPHIC SPACE are not gated.
    """
    source_counts = Counter(source)
    translation_counts = Counter(translation)
    return sorted(
        code
        for code in INTRODUCED_INVISIBLE_FORMAT_CODEPOINTS
        if translation_counts[chr(code)] > source_counts[chr(code)]
    )


def _introduced_unsupported_unicode_detail(source: str, translation: str) -> str:
    """Describe unsupported non-BMP/unassigned/format/blocked-script chars introduced vs SOURCE."""
    unsupported = sorted({ord(char) for char in translation
                          if char not in source
                          and (unicodedata.category(char) in {"Cn", "Cs", "Co"}
                               or ord(char) >= 0x20000)})
    unsupported_format = introduced_invisible_format_codepoints(source, translation)
    introduced_blocked_scripts = introduced_blocked_script_codepoints(source, translation)
    if not (unsupported or unsupported_format or introduced_blocked_scripts):
        return ""
    detail_parts: list[str] = []
    if unsupported:
        detail_parts.append(
            "codepoints=" + ",".join(f"U+{code:04X}" for code in unsupported))
    if unsupported_format:
        detail_parts.append(
            "format_codepoints=" + ",".join(
                f"U+{code:04X}" for code in unsupported_format))
    if introduced_blocked_scripts:
        detail_parts.append(
            "scripts=" + ",".join(
                f"{script}:U+{code:04X}"
                for script, code in introduced_blocked_scripts))
    return "; ".join(detail_parts)


def _introduced_cyrillic_codepoints(source: str, translation: str) -> list[int]:
    return sorted({ord(char) for char in translation if char not in source
                   and unicodedata.category(char).startswith("L")
                   and unicodedata.name(char, "").startswith("CYRILLIC")})


_ZHS_FORBIDDEN_TERMS = (
    "艾蜜莉", "转蛋", "轉蛋", "推播", "此讯息", "此訊息",
    "部落格", "弹跳视窗", "彈跳視窗", "资料夹", "資料夾",
    "应用程式", "應用程式", "快取资料", "快取資料",
)
_ZHS_BROKEN_RANK_RE = re.compile(r"第(?:最高分|活动|社交厅|粉丝数?)排行榜")
_SOLO_CORRECTION_RE = re.compile(r"ソロコレ(?:クション)?")
_TADOKORO_AZUSA_RE = re.compile(r"田所\s*あずさ")
_TRUNCATED_KATA_ADJ_HAN_RE = re.compile(r"(?:[ァ-ヺー]+(?=[一-龯])|(?<=[一-龯])[ァ-ヺー]+)")
_KATAKANA_TOKEN_RE = re.compile(r"[ァ-ヺー]{2,}")
_KATAKANA_COMPOUND_TAILS = (
    "コラボ", "カフェ", "カウント", "ダウン", "セレクション", "ライブ", "イベント",
    "ガシャ", "ガチャ", "キャンペーン", "マスター", "エリア", "ツアー", "ショップ",
    "グッズ", "シャツ", "バッジ", "ポスター", "パネル", "ステージ", "シリーズ",
)
_KATAKANA_IGNORE_TOKENS = frozenset({"ー", "ノ", "ノシ", "ッ"})
_QUOTED_TEXT_RE = re.compile(r"「[^」\n]*」|『[^』\n]*』|《[^》\n]*》|“[^”\n]*”")
_UNTRANSLATED_JAPANESE_FRAGMENT_RE = re.compile(
    r"わたくし|ぜんぜん[ぁ-ゖ一-龯]|知ってるけど|分かってない|"
    r"故意に[一-龯]|憧れ[一-龯]|かしみや|ぴょーん|必ゅ|さ玛|っ斯"
)


def find_truncated_katakana_hybrid(source: str, translation: str) -> tuple[str, str] | None:
    """Return ``(fragment, source_token)`` when zh fuses a sliced Katakana piece with Hanzi."""
    if not translation or translation == source:
        return None
    source_tokens = _KATAKANA_TOKEN_RE.findall(source)
    if not source_tokens:
        return None
    # Unwrap katakana broken across line wraps before testing token boundaries.
    unwrapped = re.sub(r"(?<=[ァ-ヺー])\n(?=[ァ-ヺー])", "", translation)
    for match in _TRUNCATED_KATA_ADJ_HAN_RE.finditer(unwrapped):
        fragment = match.group(0)
        if set(fragment) <= {"ー"} or fragment in _KATAKANA_IGNORE_TOKENS or fragment in source_tokens:
            continue
        for source_token in source_tokens:
            if fragment == source_token or fragment not in source_token:
                continue
            if source_token.startswith(fragment):
                tail = source_token[len(fragment):]
                if tail.startswith(_KATAKANA_COMPOUND_TAILS) or (len(fragment) >= 4 and len(tail) >= 3):
                    continue
            return fragment, source_token
    return None


def find_untranslated_japanese_fragment(source: str, translation: str) -> str | None:
    """Return an untranslated hiragana/typos fragment leaked into Chinese prose."""
    if not translation or translation == source:
        return None
    unquoted = _QUOTED_TEXT_RE.sub("", translation)
    match = _UNTRANSLATED_JAPANESE_FRAGMENT_RE.search(unquoted)
    return match.group(0) if match else None


def _surface_lexicon_issues(source: str, translation: str) -> list[tuple[str, str, str]]:
    """Return ``(code, error_message, detail)`` for deterministic zh-CN lexicon violations."""
    if not translation or translation == source:
        return []
    found: list[tuple[str, str, str]] = []

    def _add_idol_issue(message: str, detail: str) -> None:
        found.append(("non_authoritative_idol_name", f"non-authoritative idol name: {message}", detail))

    for term in _ZHS_FORBIDDEN_TERMS:
        if term in translation and term not in source:
            found.append((
                "non_mainland_forbidden_term",
                f"non-mainland forbidden term: {term}",
                f"forbidden_term={term}",
            ))
    if "白石紬" in translation:
        _add_idol_issue("白石紬 (use 白石䌷)", "白石紬 -> 白石䌷")
    elif "白石䋾" in translation:
        _add_idol_issue("白石䋾 (use 白石䌷)", "白石䋾 -> 白石䌷")
    if "筿" in translation and "筿" not in source:
        _add_idol_issue("筿 (use 篠宫可怜)", "筿 -> 篠")
    if ("司徒亚特" in translation and "司徒亚特" not in source) or any(
        bad in translation
        for bad in (
            "艾米莉‧斯图亚特",
            "艾米莉・斯图亚特",
            "艾米莉 斯图亚特",
            "艾米莉\u3000斯图亚特",
        )
    ):
        _add_idol_issue("expected 艾米莉·斯图亚特 (with middle dot ·)", "艾米莉·斯图亚特")
    if "やよ伊" in translation and "やよ伊" not in source:
        _add_idol_issue("やよ伊 (use 高槻弥生)", "やよ伊 -> 高槻弥生")
    for bad_roco in ("ソROCOレ", "ROCOレ", "索ROCO", "单ROCO", "ROCO集"):
        if bad_roco in translation and bad_roco not in source:
            _add_idol_issue(f"{bad_roco} (use 个人精选 for ソロコレ and ROCO for ロコ)", f"{bad_roco} -> 个人精选")
            break
    else:
        if "ソロコレ" in source and "ROCO" in translation:
            stripped_source = _SOLO_CORRECTION_RE.sub("", source)
            if "ロコ" not in stripped_source and "ROCO" not in stripped_source:
                _add_idol_issue("ソロコレ misrendered as ROCO (use 个人精选)", "ソロコレ -> 个人精选")
    for bad_tsumugi in ("白石纺", "白石紡", "纺希", "䌷希", "阿纺", "小纺", "纺纺", "纺小姐", "䋾"):
        if bad_tsumugi in translation and bad_tsumugi not in source:
            _add_idol_issue(f"{bad_tsumugi} (use 白石䌷 / 䌷 U+4337)", f"{bad_tsumugi} -> 䌷")
            break
    else:
        if ("紬" in source or "つむぎ" in source) and not any(
            k in source for k in ("紡", "つむぐ", "織", "糸")
        ):
            if ("纺织" in translation or "紡織" in translation or ("纺" in translation and "纺" not in source)):
                _add_idol_issue("纺/纺织 for 紬 (use 䌷 U+4337)", "纺 -> 䌷")
    if any(k in source for k in ("亜利沙", "ありさ", "アリサ")):
        if "亜利沙" in translation or ("有栖" in translation and "有栖" not in source) or any(
            bad in translation and bad not in source for bad in ("阿莉莎", "亚丽莎", "亚利纱")
        ):
            _add_idol_issue("expected 松田亚利沙 / 亚利沙", "松田亜利沙 -> 松田亚利沙")
    if "あずさ" in _TADOKORO_AZUSA_RE.sub("", source):
        azusa_spaced_ok = any(k in source for k in ("三浦\u3000あずさ", "三浦 あずさ")) and "三浦 梓" in translation
        if (
            any(bad in translation and bad not in source for bad in ("あず沙", "あず莎"))
            or (
                any(k in source for k in ("三浦あずさ", "三浦\u3000あずさ", "三浦 あずさ"))
                and "三浦" in translation
                and "三浦梓" not in translation
                and not azusa_spaced_ok
            )
            or ("秋月" in translation and not any(k in source for k in ("秋月", "律子", "りつこ", "リツコ")))
            or ("静香" in translation and not any(k in source for k in ("静香", "最上", "しずか", "シズカ")))
            or ("瑞希" in translation and not any(k in source for k in ("瑞希", "真壁", "みずき", "ミズキ")))
        ):
            _add_idol_issue("expected 三浦梓 / 梓", "三浦あずさ -> 三浦梓")
    if "まつり" in translation.replace("まつり湯", "").replace("まつり汤", ""):
        _add_idol_issue("expected 德川茉莉 / 茉莉", "徳川まつり -> 德川茉莉")
    elif "まつり" in source.replace("まつり湯", ""):
        matsuri_spaced_ok = any(k in source for k in ("徳川\u3000まつり", "徳川 まつり")) and "德川 茉莉" in translation
        if (
            any(bad in translation and bad not in source for bad in ("小毬", "玛茜", "小松莉", "松莉", "まつ莉"))
            or (re.search(r"小祭(?![典坛祀])", translation) is not None and "小祭" not in source)
            or (
                any(k in source for k in ("徳川まつり", "徳川\u3000まつり", "徳川 まつり", "徳川茉莉"))
                and "德川" in translation
                and "德川茉莉" not in translation
                and not matsuri_spaced_ok
            )
        ):
            _add_idol_issue("expected 德川茉莉 / 茉莉", "徳川まつり -> 德川茉莉")
    if "このみ" in source:
        konomi_spaced_ok = any(k in source for k in ("馬場\u3000このみ", "馬場 このみ")) and "马场 木实" in translation
        if (
            any(
                bad in translation and bad not in source
                for bad in ("木乃美", "这乃", "この美", "这个み", "马场木乃", "这个位美", "这位小美", "这位木实")
            )
            or re.search(r"(?:这位|马场|阿)这美|(?<![这那])这美(?:小姐|前辈|桑|酱)", translation) is not None
            or ("木下" in translation and not any(k in source for k in ("木下", "ひなた", "ヒナタ")))
            or (
                any(k in source for k in ("馬場このみ", "馬場\u3000このみ", "馬場 このみ"))
                and "马场" in translation
                and "马场木实" not in translation
                and not konomi_spaced_ok
            )
        ):
            _add_idol_issue("expected 马场木实 / 木实", "馬場このみ -> 马场木实")
    if "スタイルドロップ" in source:
        for bad_drop in (
            "大师造型水滴",
            "大师风格掉落",
            "大师风格微滴",
            "Style Drop",
            "StyleDrop",
            "风格掉落道具",
            "风格掉落物",
            "风格服装代币",
            "造型掉落物",
            "造型水滴",
            "样式水滴",
            "身姿水滴",
            "造型掉落",
            "造型滴露",
            "造型徽章",
            "风格掉落",
            "风格滴露",
            "风格糖果",
            "风格吊饰",
            "风格微滴",
            "风格碎片",
        ):
            if bad_drop in translation and bad_drop not in source:
                _add_idol_issue(
                    f"non-authoritative Style Drop wording: {bad_drop} (use 风格水滴 / 大师风格水滴)",
                    "スタイルドロップ -> 风格水滴",
                )
                break
    if "ネトスワ" in source or "ネットスワイプス" in source:
        if "NetSwipes" not in translation or any(
            k in translation for k in ("ネトスワ", "ネットスワイプス")
        ):
            _add_idol_issue("expected NetSwipes for ネトスワ / ネットスワイプス", "ネトスワ -> NetSwipes")
    if _ZHS_BROKEN_RANK_RE.search(translation):
        found.append((
            "malformed_ranking_phrase",
            "malformed ranking phrase in translation",
            "malformed ranking phrase",
        ))
    if "ログイン" in source and "登入" in translation and "登入" not in source:
        found.append((
            "non_mainland_login_calque",
            "non-mainland zh-TW login wording: 登入 (use 登录)",
            "登入 -> 登录",
        ))
    if "履歴" in source and "履歴書" not in source and "履历" in translation and "履历" not in source:
        found.append((
            "non_mainland_history_calque",
            "non-mainland zh-TW history wording: 履历 (use 记录)",
            "履历 -> 记录",
        ))
    truncated = find_truncated_katakana_hybrid(source, translation)
    if truncated:
        fragment, source_token = truncated
        found.append((
            "truncated_katakana_hybrid",
            f"truncated katakana hybrid: {fragment} (from {source_token})",
            f"{fragment} in {source_token}",
        ))
    fragment = find_untranslated_japanese_fragment(source, translation)
    if fragment:
        found.append((
            "untranslated_japanese_fragment",
            f"untranslated Japanese fragment: {fragment}",
            f"fragment={fragment}",
        ))
    return found


def _blocking_semantic_issues(source_plain: str, translation_plain: str) -> list[tuple[str, str]]:
    """Return high-precision semantic/lexical mistranslations safe for blocking QA."""
    found: list[tuple[str, str]] = []
    if JP_BAD_MEANING_RE.search(source_plain) and not ZH_BAD_MEANING_CUE_RE.search(translation_plain):
        found.append((
            "bad_meaning_semantics_missing",
            "Japanese explicitly refers to a bad/negative meaning but Chinese lacks an equivalent bad/negative-meaning cue",
        ))
    if JP_MOTORBIKE_RE.search(source_plain):
        source_motorbike = JP_BICYCLE_COMPOUND_RE.sub("", source_plain)
        is_triathlon_context = bool(
            JP_TRIATHLON_RE.search(source_plain)
            or ("スイム" in source_plain and "ラン" in source_plain)
        )
        if (
            JP_MOTORBIKE_RE.search(source_motorbike)
            and not is_triathlon_context
            and ZH_BICYCLE_RE.search(translation_plain)
            and not ZH_MOTORBIKE_CUE_RE.search(translation_plain)
        ):
            found.append((
                "motorbike_mistranslated_as_bicycle",
                "Japanese バイク denotes a motorbike/motorcycle here; bicycle wording changes the depicted vehicle class",
            ))
    if (
        JP_STANDALONE_GEKI_RE.search(source_plain)
        and ZH_GENERIC_PERFORMANCE_RE.search(translation_plain)
        and not ZH_PLAY_CUE_RE.search(translation_plain)
    ):
        found.append((
            "play_generalized_to_performance",
            "standalone Japanese 劇 is rendered only as generic 演出, losing the play/drama sense",
        ))
    return found


def validate_zhcn_surface_quality(source: str, translation: str) -> None:
    """Validate deterministic zh-CN surface quality invariants for an accepted row."""
    if not translation:
        return
    unicode_detail = _introduced_unsupported_unicode_detail(source, translation)
    if unicode_detail:
        raise ValueError(f"introduced unsupported unicode: {unicode_detail}")
    cyrillic = _introduced_cyrillic_codepoints(source, translation)
    if cyrillic:
        raise ValueError(
            "introduced cyrillic codepoints: " + ",".join(f"U+{code:04X}" for code in cyrillic)
        )
    added_markers, missing_markers, order_changed = pipeline_marker_delta(source, translation)
    if added_markers or missing_markers or order_changed:
        raise ValueError("introduced or reordered pipeline marker")
    introduced_trad = introduced_traditional_chars(source, translation)
    if introduced_trad:
        raise ValueError(f"introduced traditional Chinese chars: {dict(introduced_trad)!r}")
    lowered_source = source.lower()
    lowered_translation = translation.lower()
    for marker in META_PATTERNS:
        if marker.lower() in lowered_translation and marker.lower() not in lowered_source:
            raise ValueError(f"translator meta or garbled token: {marker!r}")
    lexicon_issues = _surface_lexicon_issues(source, translation)
    if lexicon_issues:
        raise ValueError(lexicon_issues[0][1])
    if "悪い意味" in source or "バイク" in source or "劇" in source:
        semantic_issues = _blocking_semantic_issues(
            PROTECTED_TOKEN_RE.sub("", source),
            PROTECTED_TOKEN_RE.sub("", translation),
        )
        if semantic_issues:
            raise ValueError(f"{semantic_issues[0][0]}: {semantic_issues[0][1]}")


def _issue(code: str, severity: str, detail: str = "") -> dict:
    row = {"code": code, "severity": severity}
    if detail:
        row["detail"] = detail
    return row


def applicable_glossary(source: str, glossary: dict) -> list[dict]:
    result: list[dict] = []
    entries = glossary.get("entries", {})
    seen: set[str] = set()
    for match in find_term_matches(source, entries):
        jp = match.term
        if jp in seen:
            continue
        seen.add(jp)
        spec = entries[jp]
        if isinstance(spec, str):
            spec = {"preferred": spec}
        if not isinstance(spec, dict):
            continue
        # Production passes ``glossary/authoritative-terms.json``, whose
        # evidence-backed field is named ``target``. The older review glossary
        # uses ``preferred``. Treat both as the same required target so a
        # terminology check cannot silently disappear at the CLI boundary.
        preferred = spec.get("preferred", spec.get("target", ""))
        result.append({
            "source_term": jp,
            "preferred": str(preferred),
            "forbidden": [str(x) for x in spec.get("forbidden", [])],
            "category": str(spec.get("category", "")),
            "notes": str(spec.get("notes", "")),
        })
    return result


def evaluate_row(queue_row: dict, candidate: dict, glossary: dict) -> dict:
    source = str(queue_row.get("source", ""))
    sid = str(queue_row.get("source_sha256", "")) or source_id(source)
    translation = str(candidate.get("translation", ""))
    issues: list[dict] = []
    usage = queue_row.get("usage_profile", {})
    lyric = queue_row.get("task") == "ASSETS_LYRICS" or (
        isinstance(usage, dict) and usage.get("category") == "LIVE_LYRICS")

    if queue_row.get("_queue_missing"):
        issues.append(_issue("queue_source_missing", "reject"))

    candidate_source = str(candidate.get("source", source))
    candidate_sid = str(candidate.get("source_sha256", sid)) or source_id(candidate_source)
    if candidate_sid != sid or candidate_source != source:
        issues.append(_issue("source_identity_mismatch", "reject"))

    if not translation.strip():
        issues.append(_issue("empty_translation", "reject"))
    else:
        if translation == source:
            issues.append(_issue("unchanged_translation", "review"))
        provider_error = detect_provider_error_text(translation)
        if provider_error and provider_error.lower() not in source.lower():
            issues.append(_issue("provider_error_text", "reject", provider_error))

    # Unassigned (Cn), surrogate (Cs) and private-use (Co) codepoints are not
    # reviewable translated text when SOURCE does not carry them; private use is
    # included because it is how internal marker leakage looks, for example the
    # U+E200/U+E201/U+E202 term wrapper.  A private-use codepoint can still have
    # a glyph in a private font, so the SOURCE-presence check decides: what
    # SOURCE itself carries stays allowed, exactly like the scripts below.
    unsupported_detail = _introduced_unsupported_unicode_detail(source, translation)
    if unsupported_detail:
        issues.append(_issue("introduced_unsupported_unicode", "reject", unsupported_detail))

    added_markers, missing_markers, order_changed = pipeline_marker_delta(
        source, translation
    )
    if added_markers or missing_markers or order_changed:
        detail_parts = []
        if added_markers:
            detail_parts.append(f"added={dict(added_markers)!r}")
        if missing_markers:
            detail_parts.append(f"missing={dict(missing_markers)!r}")
        if order_changed:
            detail_parts.append("order_changed")
        issues.append(_issue(
            "introduced_pipeline_marker",
            "reject",
            "; ".join(detail_parts),
        ))

    cyrillic = _introduced_cyrillic_codepoints(source, translation)
    if cyrillic:
        issues.append(_issue("introduced_cyrillic", "reject",
                             "codepoints=" + ",".join(f"U+{code:04X}" for code in cyrillic)))
    if "{$P$}先生先生" in translation and not any(x in source for x in ["先生先生", "さん先生", "先生さん"]):
        issues.append(_issue("duplicated_player_honorific", "reject"))
    if lyric and (queue_row.get("context_examples") or queue_row.get("expanded_context_examples")):
        source_plain = PROTECTED_TOKEN_RE.sub("", source)
        target_plain = PROTECTED_TOKEN_RE.sub("", translation)
        if len(target_plain) > max(26, 2 * len(source_plain)):
            issues.append(_issue("possible_context_spill", "reject",
                                 "long lyric draft requires source-scope review; not a confirmed semantic error"))

    try:
        (validate_lyric_tokens if lyric else validate_translation)(source, translation)
    except Exception as exc:
        issues.append(_issue("protected_token_mismatch", "reject", str(exc)))

    if visible_numbers(source) != visible_numbers(translation):
        issues.append(_issue(
            "numeric_literal_mismatch",
            "reject",
            f"source={dict(visible_numbers(source))!r} translation={dict(visible_numbers(translation))!r}",
        ))

    introduced_traditional = introduced_traditional_chars(source, translation)
    if introduced_traditional:
        issues.append(_issue(
            "introduced_traditional_chinese",
            "reject",
            f"introduced={dict(introduced_traditional)!r}",
        ))

    # Ordinary Japanese honorifics should be rendered naturally in Chinese.
    # This gate is intentionally narrower than the general kana residual check:
    # song titles, credits and hashtags may legitimately preserve Japanese,
    # while suffixes such as 茜ちゃん / ～さん are almost always a translation
    # leak when they survive into otherwise Chinese output.
    translation_plain_for_honorific = PROTECTED_TOKEN_RE.sub("", translation)
    residual_honorifics = sorted(set(JP_HONORIFIC_RESIDUAL_RE.findall(translation_plain_for_honorific)))
    if residual_honorifics:
        issues.append(_issue(
            "japanese_honorific_residual",
            "reject",
            "residual=" + ",".join(residual_honorifics),
        ))

    # Mainland zh-CN project evidence does not transliterate Japanese ちゃん as
    # the internet suffix “酱”.  Apply this only when SOURCE actually contains
    # ちゃん and does not also contain an obvious food/sauce term that could
    # legitimately translate to Chinese 酱.
    if (
        JP_CHAN_HONORIFIC_RE.search(source)
        and not JP_SAUCE_HINT_RE.search(source)
        and "酱" in translation_plain_for_honorific
    ):
        issues.append(_issue(
            "honorific_chan_jiang_calque",
            "reject",
            "contains=酱",
        ))

    translation_plain_for_grammar = translation_plain_for_honorific
    for token in glossary.get("kana_allowlist", []):
        translation_plain_for_grammar = translation_plain_for_grammar.replace(token, "")
    grammar_residuals = sorted(
        set(match.group(0) for match in JP_GRAMMAR_RESIDUAL_RE.finditer(translation_plain_for_grammar))
    )
    # A fully preserved title/name can legitimately be unchanged and is already
    # surfaced by unchanged_translation for review.  The blocking case here is
    # hybrid Chinese output with Japanese grammar tails left behind.
    if translation != source and grammar_residuals:
        issues.append(_issue(
            "japanese_grammar_residual",
            "reject",
            "residual=" + ",".join(grammar_residuals),
        ))

    # "的说" is not Mainland-style Chinese when used as a sentence-final ACG
    # particle.  Block only particle-shaped occurrences so normal phrases such
    # as "这样的说法" remain untouched.
    de_shuo_residuals = sorted(
        set(match.group(0) for match in ZH_SENTENCE_FINAL_DE_SHUO_RE.finditer(translation))
    )
    if de_shuo_residuals:
        issues.append(_issue(
            "sentence_final_de_shuo_calque",
            "reject",
            "residual=" + ",".join(de_shuo_residuals),
        ))

    # Historical official-derived Chinese evidence is overwhelmingly stable
    # for the player's さん address: 3724/3757 current matched occurrences use
    # {$P$}先生 after zh-CN normalization.  Keep exceptions reviewable rather
    # than hard-rejecting them, but never let an unexplained dropped honorific
    # auto-pass deterministic QA.
    if PLAYER_HONORIFIC in source and PLAYER_HONORIFIC_ZH not in translation:
        issues.append(_issue(
            "player_honorific_convention_missing",
            "review",
            f"expected normal convention {PLAYER_HONORIFIC_ZH}",
        ))

    source_plain = PROTECTED_TOKEN_RE.sub("", source)
    translation_plain = PROTECTED_TOKEN_RE.sub("", translation)
    if JP_FINAL_ADVERSATIVE_RE.search(source_plain) and not ZH_ADVERSATIVE_RE.search(translation_plain):
        issues.append(_issue(
            "sentence_final_adversative_missing",
            "review",
            "explicit Japanese sentence-final adversative lacks a Chinese adversative cue",
        ))
    if ZH_INDEFINITE_OBJECT_RE.search(translation_plain) and not JP_INDEFINITE_CUE_RE.search(source_plain):
        issues.append(_issue(
            "unsupported_indefinite_object_addition",
            "review",
            "Chinese indefinite object appears without a corresponding Japanese indefinite/interrogative cue",
        ))
    blocking_semantic_codes = {
        code: detail for code, detail in _blocking_semantic_issues(source_plain, translation_plain)
    }
    if "bad_meaning_semantics_missing" in blocking_semantic_codes:
        issues.append(_issue(
            "bad_meaning_semantics_missing",
            "review",
            blocking_semantic_codes["bad_meaning_semantics_missing"],
        ))
    if JP_KONDO_FUTURE_RE.search(source_plain) and ZH_THIS_TIME_RE.search(translation_plain):
        issues.append(_issue(
            "kondo_future_rendered_as_this_time",
            "review",
            "今度 occurs in a future-intent context but Chinese renders it as this time",
        ))
    if JP_HANASHI_RE.search(source_plain) and ZH_STORY_RE.search(translation_plain):
        issues.append(_issue(
            "hanashi_narrowed_to_story",
            "review",
            "話 is context-sensitive and Chinese 故事 narrows it to narrative/story meaning",
        ))
    if "motorbike_mistranslated_as_bicycle" in blocking_semantic_codes:
        issues.append(_issue(
            "motorbike_mistranslated_as_bicycle",
            "review",
            blocking_semantic_codes["motorbike_mistranslated_as_bicycle"],
        ))
    if "play_generalized_to_performance" in blocking_semantic_codes:
        issues.append(_issue(
            "play_generalized_to_performance",
            "review",
            blocking_semantic_codes["play_generalized_to_performance"],
        ))
    if JP_TACKLE_RE.search(source_plain) and ZH_GENERIC_COLLISION_RE.search(translation_plain) and not ZH_TACKLE_SPECIFIC_RE.search(translation_plain):
        issues.append(_issue(
            "tackle_generalized_to_collision",
            "review",
            "タックル names a tackle/defensive action; generic 冲撞 loses action specificity",
        ))
    if ZH_AWKWARD_LATE_EFFORT_RE.search(translation_plain):
        issues.append(_issue(
            "awkward_late_effort_calque",
            "review",
            "Chinese word order around staying late and 努力着/加油着 is a detectable literal calque and requires fluency review",
        ))
    if ZH_FIRST_PERSON_PLURAL_RE.search(translation_plain) and not JP_FIRST_PERSON_PLURAL_CUE_RE.search(source_plain):
        issues.append(_issue(
            "unsupported_first_person_plural_addition",
            "review",
            "Chinese introduces an explicit first-person plural subject without a corresponding Japanese plural/inclusive cue",
        ))
    if JP_GUEST_RE.search(source_plain) and ZH_AUDIENCE_RE.search(translation_plain) and not JP_EXPLICIT_AUDIENCE_CONTEXT_RE.search(source_plain):
        issues.append(_issue(
            "guest_narrowed_to_audience",
            "review",
            "Japanese お客/客 is broader than audience here; Chinese 观众 requires explicit audience/stage context",
        ))
    ouen_spec = glossary.get("entries", {}).get("応援")
    if isinstance(ouen_spec, str):
        ouen_preferred = ouen_spec
    elif isinstance(ouen_spec, dict):
        ouen_preferred = str(ouen_spec.get("preferred", ""))
    else:
        ouen_preferred = ""
    if JP_OUEN_RE.search(source_plain) and ZH_OUEN_LOAN_RE.search(translation_plain) and "应援" not in ouen_preferred and "應援" not in ouen_preferred:
        issues.append(_issue(
            "spoken_ouen_loanword_uncertain",
            "review",
            "spoken Japanese 応援 is rendered as the fandom loanword 应援 without glossary/source-bound terminology evidence",
        ))
    if JP_COQUETTISH_EXCL_RE.search(source_plain) and ZH_NEUTRAL_SURPRISE_RE.search(translation_plain):
        issues.append(_issue(
            "coquettish_exclamation_neutralized",
            "review",
            "coquettish/protesting Japanese exclamation is flattened into a neutral Chinese surprise interjection",
        ))
    if JP_MILD_ACCEPTANCE_RE.search(source_plain) and ZH_STRONG_RESIGNATION_RE.search(translation_plain):
        issues.append(_issue(
            "mild_acceptance_over_resigned",
            "review",
            "mild Japanese acceptance such as まあ、いいか is rendered with stronger Chinese resignation/abandonment wording",
        ))

    allow = glossary.get("kana_allowlist", [])
    kana_text = translation
    for token in allow:
        kana_text = kana_text.replace(token, "")
    if KANA_RE.search(kana_text):
        issues.append(_issue("japanese_kana_residual", "review"))

    lowered = translation.lower()
    if any(marker.lower() in lowered for marker in META_PATTERNS):
        issues.append(_issue("translator_meta_text", "review"))

    for code, _msg, detail in _surface_lexicon_issues(source, translation):
        issues.append(_issue(code, "reject", detail))

    for rule in applicable_glossary(source, glossary):
        preferred = rule["preferred"]
        if preferred and preferred not in translation:
            issues.append(_issue(
                "preferred_term_missing",
                "review",
                f"{rule['source_term']} -> {preferred}",
            ))
        for forbidden in rule["forbidden"]:
            if forbidden and forbidden in translation:
                issues.append(_issue(
                    "forbidden_term_present",
                    "review",
                    f"{rule['source_term']} forbids {forbidden}",
                ))

    source_visible = PROTECTED_TOKEN_RE.sub("", source).strip()
    translated_visible = PROTECTED_TOKEN_RE.sub("", translation).strip()
    if len(source_visible) >= 4 and translated_visible:
        ratio = len(translated_visible) / max(1, len(source_visible))
        if ratio < 0.25 or ratio > 4.0:
            issues.append(_issue("length_outlier", "review", f"ratio={ratio:.3f}"))

    severities = {issue["severity"] for issue in issues}
    verdict = "REJECT" if "reject" in severities else ("REVIEW" if "review" in severities else "PASS")
    return {
        "source_sha256": sid,
        "source": source,
        "translation": translation,
        "qa_verdict": verdict,
        "issues": issues,
        "provenance": candidate.get("provenance", ""),
        "model": candidate.get("model", ""),
        "status": candidate.get("status", ""),
        "examples": queue_row.get("examples", candidate.get("examples", [])),
        "occurrences": queue_row.get("occurrences", candidate.get("occurrences")),
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--candidates", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--glossary", type=Path)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    queue = index_unique(read_jsonl(args.queue), "queue")
    candidates = index_unique(read_jsonl(args.candidates), "candidates")
    glossary = load_glossary(args.glossary)

    output: list[dict] = []
    counts = Counter()
    missing = 0
    for sid, candidate in candidates.items():
        queue_row = queue.get(sid)
        if queue_row is None:
            source = str(candidate.get("source", ""))
            queue_row = {
                "source_sha256": sid,
                "source": source,
                "examples": candidate.get("examples", []),
                "occurrences": candidate.get("occurrences"),
                "_queue_missing": True,
            }
            missing += 1
        result = evaluate_row(queue_row, candidate, glossary)
        output.append(result)
        counts[result["qa_verdict"]] += 1
        for issue in result["issues"]:
            counts[f"issue:{issue['code']}"] += 1

    write_jsonl(args.output, output)
    summary = {
        "schema_version": 1,
        "queue_rows": len(queue),
        "candidate_rows": len(candidates),
        "queue_missing_for_candidate": missing,
        "verdicts": {k: v for k, v in counts.items() if not k.startswith("issue:")},
        "issues": {k[6:]: v for k, v in counts.items() if k.startswith("issue:")},
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # REJECT is a normal classification outcome, not a pipeline execution error.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
