#!/usr/bin/env python3
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

# Explicit full-name spellings observed in accepted Japanese source records.
# These are literal aliases, not a separator-normalization rule: adding one
# must never make two neighbouring ordinary words match a character name.
_FULLNAME_SOURCE_VARIANTS = {
    "島原 エレナ": "島原エレナ",
    "エミリー スチュアート": "エミリースチュアート",
    "エミリー・スチュアート": "エミリースチュアート",
    "徳川 まつり": "徳川まつり",
    "エミリー　スチュアート": "エミリースチュアート",
    "天海 春香": "天海春香",
    "如月 千早": "如月千早",
    "星井 美希": "星井美希",
    "萩原 雪歩": "萩原雪歩",
    "高槻 やよい": "高槻やよい",
    "菊地 真": "菊地真",
    "水瀬 伊織": "水瀬伊織",
    "四条 貴音": "四条貴音",
    "秋月 律子": "秋月律子",
    "三浦 あずさ": "三浦あずさ",
    "双海 亜美": "双海亜美",
    "双海 真美": "双海真美",
    "我那覇 響": "我那覇響",
    "春日 未来": "春日未来",
    "最上 静香": "最上静香",
    "伊吹 翼": "伊吹翼",
    "田中 琴葉": "田中琴葉",
    "佐竹 美奈子": "佐竹美奈子",
    "所 恵美": "所恵美",
    "箱崎 星梨花": "箱崎星梨花",
    "野々原 茜": "野々原茜",
    "望月 杏奈": "望月杏奈",
    "七尾 百合子": "七尾百合子",
    "高山 紗代子": "高山紗代子",
    "松田 亜利沙": "松田亜利沙",
    "高坂 海美": "高坂海美",
    "中谷 育": "中谷育",
    "天空橋 朋花": "天空橋朋花",
    "北沢 志保": "北沢志保",
    "舞浜 歩": "舞浜歩",
    "木下 ひなた": "木下ひなた",
    "矢吹 可奈": "矢吹可奈",
    "横山 奈緒": "横山奈緒",
    "二階堂 千鶴": "二階堂千鶴",
    "馬場 このみ": "馬場このみ",
    "大神 環": "大神環",
    "豊川 風花": "豊川風花",
    "宮尾 美也": "宮尾美也",
    "福田 のり子": "福田のり子",
    "真壁 瑞希": "真壁瑞希",
    "篠宮 可憐": "篠宮可憐",
    "百瀬 莉緒": "百瀬莉緒",
    "永吉 昴": "永吉昴",
    "北上 麗花": "北上麗花",
    "周防 桃子": "周防桃子",
    "白石 紬": "白石紬",
    "桜守 歌織": "桜守歌織",
}

# Separators a literal alias may insert inside its canonical spelling.  The
# observed separator class selects both the recorded evidence sentence and the
# metadata count bucket, so counts stay meaningful when a new kind is added.
_FULLNAME_VARIANT_SEPARATORS = frozenset({" ", "\u3000", "\u30fb"})
_FULLNAME_VARIANT_KINDS = (
    (frozenset({" ", "\u3000"}), "space_variant"),
    (frozenset({"\u30fb"}), "separator_variant"),
)


def _fullname_variant_kind(variant: str) -> str:
    """Return the recorded separator class of one literal full-name alias."""
    inserted = frozenset(
        char for char in variant if char in _FULLNAME_VARIANT_SEPARATORS
    )
    for separators, kind in _FULLNAME_VARIANT_KINDS:
        if inserted and inserted <= separators:
            return kind
    raise ValueError(f"unclassified source variant separator: {variant}")


def _fullname_variant_kind_counts(
    variants: dict[str, str] | None = None,
) -> Counter[str]:
    """Count literal aliases per separator class, defaulting to the table."""
    selected = _FULLNAME_SOURCE_VARIANTS if variants is None else variants
    return Counter(_fullname_variant_kind(variant) for variant in selected)


def _source_variant_entries(
    canonical_entries: dict[str, dict],
    variants: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Derive literal full-name variants from their canonical glossary rows."""
    selected = _FULLNAME_SOURCE_VARIANTS if variants is None else variants
    entries: dict[str, dict] = {}
    for variant, canonical_source in selected.items():
        kind = _fullname_variant_kind(variant)
        if variant in canonical_entries:
            raise ValueError(f"source variant already has an entry: {variant}")
        canonical = canonical_entries.get(canonical_source)
        if not isinstance(canonical, dict):
            raise ValueError(f"missing canonical source entry: {canonical_source}")
        stripped = "".join(
            char for char in variant if char not in _FULLNAME_VARIANT_SEPARATORS
        )
        if stripped != canonical_source:
            raise ValueError(
                "source variant is not a separator spelling of "
                f"{canonical_source}: {variant}"
            )
        if (
            variant[0] in _FULLNAME_VARIANT_SEPARATORS
            or variant[-1] in _FULLNAME_VARIANT_SEPARATORS
        ):
            raise ValueError(f"source variant has an edge separator: {variant}")
        target = str(canonical.get("target", "")).strip()
        if canonical.get("category") != "character_name" or not target:
            raise ValueError(
                f"canonical source is not a targeted character name: {canonical_source}"
            )
        entries[variant] = {
            "target": target,
            "category": "character_name",
            "evidence": (
                f"observed_source_fullname_{kind}"
                "+canonical_authoritative_target"
            ),
            "source_variant_of": canonical_source,
        }
    return entries


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

    entries.update(_source_variant_entries(entries))

    variant_kinds = _fullname_variant_kind_counts()
    doc = {
        "schema_version": 1,
        "policy": {
            "match": "literal_substring_longest_first_with_katakana_word_internal_guard",
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
            "fullname_source_variants": len(_FULLNAME_SOURCE_VARIANTS),
            "fullname_source_variants_space": variant_kinds["space_variant"],
            "fullname_source_variants_separator": variant_kinds[
                "separator_variant"
            ],
        },
        "entries": entries,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(doc["counts"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
