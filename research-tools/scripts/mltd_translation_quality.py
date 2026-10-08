#!/usr/bin/env python3
"""Deterministic quality checks for MLTD Simplified Chinese translation rows."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipelines.text.mltd_localize_gtx import KANA_RE, PROTECTED_TOKEN_RE, read_jsonl, validate_translation

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
JP_PERCENT_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)パーセント")
TRADITIONAL_CHAR_MAP_PATH = (
    REPO / "localization/quality/traditional-to-simplified-char-map.json"
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
ZH_BAD_MEANING_CUE_RE = re.compile(r"(?:坏(?:的)?意思|不好(?:的)?意思|恶意|贬义|负面意思)")
JP_KONDO_FUTURE_RE = re.compile(r"今度[\s\S]{0,120}(?:します|いたします|するよ|するね|しよう|したい|行く|来る|持って|差し入れ)")
ZH_THIS_TIME_RE = re.compile(r"(?:这次|此次)")
JP_HANASHI_RE = re.compile(r"話")
ZH_STORY_RE = re.compile(r"故事")
JP_MOTORBIKE_RE = re.compile(r"バイク")
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
    "译者注", "翻译注", "原文意思", "这里的意思", "可能是：", "可能是:", "translation:", "translator note"
)


def source_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def introduced_traditional_chars(source: str, translation: str) -> Counter[str]:
    # "著名" is a standard simplified Chinese word; 著 in this fixed phrase
    # must not be mistaken for the traditional auxiliary 著 (= 着).
    source_counts = Counter(ch for ch in source.replace("著名", "") if ch in TRADITIONAL_OUTPUT_CHARS)
    translation_counts = Counter(
        ch for ch in translation.replace("著名", "") if ch in TRADITIONAL_OUTPUT_CHARS
    )
    return Counter(
        {
            ch: count - source_counts.get(ch, 0)
            for ch, count in translation_counts.items()
            if count > source_counts.get(ch, 0)
        }
    )


def _issue(code: str, severity: str, detail: str = "") -> dict:
    row = {"code": code, "severity": severity}
    if detail:
        row["detail"] = detail
    return row


def applicable_glossary(source: str, glossary: dict) -> list[dict]:
    result: list[dict] = []
    for jp, spec in glossary.get("entries", {}).items():
        if jp not in source:
            continue
        if isinstance(spec, str):
            spec = {"preferred": spec}
        if not isinstance(spec, dict):
            continue
        result.append({
            "source_term": jp,
            "preferred": str(spec.get("preferred", "")),
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

    if queue_row.get("_queue_missing"):
        issues.append(_issue("queue_source_missing", "reject"))

    candidate_source = str(candidate.get("source", source))
    candidate_sid = str(candidate.get("source_sha256", sid)) or source_id(candidate_source)
    if candidate_sid != sid or candidate_source != source:
        issues.append(_issue("source_identity_mismatch", "reject"))

    if not translation.strip():
        issues.append(_issue("empty_translation", "reject"))
    elif translation == source:
        issues.append(_issue("unchanged_translation", "review"))

    try:
        validate_translation(source, translation)
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
    if JP_BAD_MEANING_RE.search(source_plain) and not ZH_BAD_MEANING_CUE_RE.search(translation_plain):
        issues.append(_issue(
            "bad_meaning_semantics_missing",
            "review",
            "Japanese explicitly refers to a bad/negative meaning but Chinese lacks an equivalent bad/negative-meaning cue",
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
    if JP_MOTORBIKE_RE.search(source_plain) and ZH_BICYCLE_RE.search(translation_plain):
        issues.append(_issue(
            "motorbike_mistranslated_as_bicycle",
            "review",
            "Japanese バイク denotes a motorbike/motorcycle here; bicycle wording changes the depicted vehicle class",
        ))
    if JP_STANDALONE_GEKI_RE.search(source_plain) and ZH_GENERIC_PERFORMANCE_RE.search(translation_plain) and not ZH_PLAY_CUE_RE.search(translation_plain):
        issues.append(_issue(
            "play_generalized_to_performance",
            "review",
            "standalone Japanese 劇 is rendered only as generic 演出, losing the play/drama sense",
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


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


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
