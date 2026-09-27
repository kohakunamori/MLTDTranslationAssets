#!/usr/bin/env python3
import sys
from pathlib import Path
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))
"""Build authoritative MLTD source-term replacements from project evidence."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

# Small, deliberate project rules for terms that must not drift between requests.
# target == source means preserve the branded token verbatim.
CURATED = {
    "{$P$}さん": {
        "target": "{$P$}先生",
        "category": "player_address",
        "evidence": "project_address_convention+official_legacy_majority",
    },
    "茜ちゃん": {
        "target": "小茜",
        "category": "character_nickname",
        "evidence": "official_legacy_zhcn_consistent_substring",
    },
    "ゴールドランカー": {
        "target": "黄金排名",
        "category": "rank_title",
        "evidence": "official_legacy_zhcn_consistent_substring",
    },
    "トッププラチナマスター": {
        "target": "顶尖白金大师",
        "category": "rank_title",
        "evidence": "official_legacy_zhcn_consistent_substring",
    },
    "アナザー2衣装": {
        "target": "异色2服装",
        "category": "costume_system_term",
        "evidence": "official_legacy_zhcn_consistent_substring",
    },
    "アナザー衣装": {
        "target": "异色服装",
        "category": "costume_system_term",
        "evidence": "official_legacy_zhcn_consistent_substring",
    },
    "キミさけ": {
        "target": "キミさけ",
        "category": "brand_title",
        "evidence": "project_policy_preserve_unofficially_localized_brand",
    },
    # Historical official rows use stable labels even though their numeric rank
    # thresholds no longer match current JP data.  Promote only the label text;
    # numeric literals remain source-owned and are preserved independently.
    "ハイスコア ランキング": {
        "target": "最高分排行榜",
        "category": "ranking_label",
        "evidence": "official_legacy_label_only_numeric_thresholds_ignored",
    },
    "イベント ランキング": {
        "target": "活动排行榜",
        "category": "ranking_label",
        "evidence": "official_legacy_label_only_numeric_thresholds_ignored",
    },
    "ラウンジ ランキング": {
        "target": "社交厅排行榜",
        "category": "ranking_label",
        "evidence": "official_legacy_label_only_numeric_thresholds_ignored",
    },
    "CHALLENGE FOR GLOW-RY D＠YS!!!": {
        "target": "CHALLENGE FOR GLOW-RY D＠YS!!!",
        "category": "brand_title",
        "evidence": "project_policy_preserve_unofficially_localized_brand",
    },
    "7D@ys Smile!!": {
        "target": "7D@ys Smile!!",
        "category": "brand_title",
        "evidence": "project_policy_preserve_unofficially_localized_brand",
    },
    "BRAND NEW PERFORM@NCE!!!": {
        "target": "BRAND NEW PERFORM@NCE!!!",
        "category": "brand_title",
        "evidence": "project_policy_preserve_unofficially_localized_brand",
    },
    "THE IDOLM@STER M@STERS OF IDOL WORLD!!!!!": {
        "target": "THE IDOLM@STER M@STERS OF IDOL WORLD!!!!!",
        "category": "brand_title",
        "evidence": "project_policy_preserve_unofficially_localized_brand",
    },
    "THE IDOLM@STER ORCHESTRA CONCERT": {
        "target": "THE IDOLM@STER ORCHESTRA CONCERT",
        "category": "brand_title",
        "evidence": "project_policy_preserve_unofficially_localized_brand",
    },
    "打ち上げガシャ": {
        "target": "庆功扭蛋",
        "category": "system_term",
        "evidence": "current_project_consistency_rule",
    },
    "シアターデイズ": {
        "target": "剧场时光",
        "category": "franchise_term",
        "evidence": "current_project_consistency_rule",
    },
    "ミリオンジュエル": {
        "target": "百万宝石",
        "category": "currency",
        "evidence": "current_project_consistency_rule",
    },
    "スペシャルログインボーナス": {
        "target": "特别登录奖励",
        "category": "system_term",
        "evidence": "current_project_consistency_rule",
    },
    "BOTファン数": {
        "target": "BOT粉丝数",
        "category": "system_term",
        "evidence": "information_preservation_rule",
    },
}

# Current unresolved production rows contain 3rd-9th anniversary strings in
# both katakana and playful hiragana spellings.  Normalize all ordinals here so
# models never get to mix forms such as "5th周年" / "5th anniversary".
for _ordinal, _number in {
    "1st": 1,
    "2nd": 2,
    "3rd": 3,
    "4th": 4,
    "5th": 5,
    "6th": 6,
    "7th": 7,
    "8th": 8,
    "9th": 9,
}.items():
    for _spelling in ("アニバーサリー", "あにばーさりー"):
        CURATED[f"{_ordinal}{_spelling}"] = {
            "target": f"{_number}周年纪念",
            "category": "anniversary_phrase",
            "evidence": "project_zhcn_style_rule",
        }


_OWNER_RULED_IDOL_NAMES = {
    # Owner ruling 2026-09-26.  Each row differs from the mechanical OpenCC t2s form
    # of the same official Traditional name, so the reason is recorded verbatim.
    "エミリースチュアート": {
        "target": "艾米莉·斯图亚特",
        "mechanical_opencc_t2s": "艾蜜莉‧司徒亚特",
        "reason": "owner chose the U+00B7 separator and the 米莉/斯图亚特 spelling",
    },
    "篠宮可憐": {
        "target": "篠宫可怜",
        "mechanical_opencc_t2s": "筿宫可怜",
        "reason": "owner keeps the 篠 U+7BE0 family glyph instead of OpenCC's 筿 U+7B7F",
    },
    "白石紬": {
        "target": "白石䌷",
        "mechanical_opencc_t2s": "白石䌷",
        "reason": "owner confirmed the published U+4337 form; no divergence from t2s",
    },
}


def _idol_name_entries() -> dict[str, dict]:
    """Idol full names the image surfaces must render (owner ruling 2026-09-26).

    The official Traditional corpus writes each of these names verbatim on the
    Japanese side of its own row and repeats it verbatim in the Chinese side, so
    a mechanical OpenCC t2s conversion of that Chinese text is evidence-bound
    rather than invented.  Only names the exact-source majority pass in main()
    cannot reach are listed here: it keys on a row whose *whole* source equals the
    name, and most name rows also carry dialogue.

    The targets are FROZEN literals, not a live OpenCC call: this table must stay
    byte-reproducible without an optional dependency, and the published text
    ledger already carries exactly these forms.  They were derived with
    OpenCC("t2s") plus build_t2s_waived_ledger.normalize_simplified -- the same
    chain as published batch 6 -- and cross-checked against the published ledger.

    ``corpus_rows`` / ``published_text_rows`` were measured on
    build/localization-90200/legacy-zh-translations.jsonl and on the published
    batch-6 t2s ledger; see the run HANDOFF for the reproduction command.
    """
    measured = {
        "天海春香": ("天海春香", 103, 103),
        "如月千早": ("如月千早", 106, 106),
        "星井美希": ("星井美希", 99, 99),
        "菊地真": ("菊地真", 103, 103),
        "秋月律子": ("秋月律子", 95, 95),
        "伊吹翼": ("伊吹翼", 114, 114),
        "田中琴葉": ("田中琴叶", 121, 121),
        "佐竹美奈子": ("佐竹美奈子", 110, 110),
        "箱崎星梨花": ("箱崎星梨花", 116, 116),
        "望月杏奈": ("望月杏奈", 111, 111),
        "七尾百合子": ("七尾百合子", 120, 120),
        "高山紗代子": ("高山纱代子", 110, 110),
        "高坂海美": ("高坂海美", 106, 106),
        "中谷育": ("中谷育", 117, 117),
        "天空橋朋花": ("天空桥朋花", 106, 106),
        "矢吹可奈": ("矢吹可奈", 132, 133),
        "二階堂千鶴": ("二阶堂千鹤", 111, 111),
        "大神環": ("大神环", 103, 103),
        "宮尾美也": ("宫尾美也", 110, 110),
        "真壁瑞希": ("真壁瑞希", 130, 130),
        "永吉昴": ("永吉昴", 108, 108),
        "北上麗花": ("北上丽花", 130, 130),
        "周防桃子": ("周防桃子", 111, 111),
    }
    entries: dict[str, dict] = {}
    for source, (target, corpus_rows, published_rows) in measured.items():
        entries[source] = {
            "target": target,
            "category": "character_name",
            "evidence": "official_legacy_zhcn_verbatim_source_mechanical_t2s",
            "corpus_rows": corpus_rows,
            "published_text_rows": published_rows,
        }
    for source, ruled in _OWNER_RULED_IDOL_NAMES.items():
        entries[source] = {
            "target": ruled["target"],
            "category": "character_name",
            "evidence": "owner_ruling_2026-09-26",
            "mechanical_opencc_t2s": ruled["mechanical_opencc_t2s"],
            "reason": ruled["reason"],
        }
    return entries


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--character-evidence",
        type=Path,
        default=Path("build/localization-90200/character-voice-evidence.json"),
    )
    ap.add_argument(
        "--official-zhcn",
        type=Path,
        default=Path("build/localization-90200/legacy-zh-cn-candidates.jsonl"),
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("localization/quality/authoritative-terms.json"),
    )
    ap.add_argument("--min-exact-evidence", type=int, default=2)
    ap.add_argument("--min-majority-ratio", type=float, default=0.8)
    args = ap.parse_args()

    evidence = json.loads(args.character_evidence.read_text(encoding="utf-8-sig"))
    speakers = evidence.get("speakers", {})
    names = {
        str(info.get("name_jp", "")).strip()
        for info in speakers.values()
        if isinstance(info, dict) and str(info.get("name_jp", "")).strip()
    }
    exact: dict[str, Counter[str]] = {name: Counter() for name in names}

    with args.official_zhcn.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            source = str(row.get("source", ""))
            if source not in exact:
                continue
            translation = str(row.get("translation", "")).strip()
            if translation:
                exact[source][translation] += 1

    entries: dict[str, dict] = dict(CURATED)
    # Idol full names the image surfaces must render.  They go in before the
    # majority pass because most of them never reach it (see _idol_name_entries).
    entries.update(_idol_name_entries())
    evidence_names = 0
    for source in sorted(exact):
        counts = exact[source]
        total = sum(counts.values())
        if total < args.min_exact_evidence:
            continue
        target, count = counts.most_common(1)[0]
        ratio = count / total
        if ratio < args.min_majority_ratio:
            continue
        entries[source] = {
            "target": target,
            "category": "character_name",
            "evidence": "official_legacy_zhcn_exact_source",
            "evidence_count": count,
            "evidence_total": total,
            "majority_ratio": round(ratio, 6),
        }
        evidence_names += 1

    doc = {
        "schema_version": 1,
        "policy": {
            "match": "literal_substring_longest_first",
            "model_sees_markers_not_authoritative_source_terms": True,
            "exact_source_terms_can_skip_model": True,
            "character_name_min_exact_evidence": args.min_exact_evidence,
            "character_name_min_majority_ratio": args.min_majority_ratio,
        },
        "counts": {
            "entries": len(entries),
            "curated": len(CURATED),
            "evidence_backed_character_names": evidence_names,
            "idol_names": len(_idol_name_entries()),
            "owner_ruled_character_names": len(_OWNER_RULED_IDOL_NAMES),
        },
        "entries": entries,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(doc["counts"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
