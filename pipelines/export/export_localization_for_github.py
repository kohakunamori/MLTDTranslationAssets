#!/usr/bin/env python3
"""Export MLTD localization assets into a standard GitHub public repository layout.

Project architecture:
- Text in Git: JSONL per bundle, categorized into story, card, dialogue, birth, master.
- Binary media external: referenced in manifests/ with SHA-256 and Release/R2 URLs.
- Zero delimiter leakage: intercepts '|' and '^' to protect game client engine.
- Exact source-hash binding: prevents version drift across updates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

RESERVED_DELIMITERS = ("|", "^")
DELIMITER_REPLACEMENTS = {
    "|": "｜",  # U+FF5C Fullwidth Vertical Line
    "^": "＾",  # U+FF3E Fullwidth Circumflex Accent
}

VALID_STATUSES = {"untranslated", "pending", "accepted"}
CATEGORIES = ("story", "card", "dialogue", "birth", "master")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CJK_PATTERN = re.compile(r"[぀-ゟ゠-ヿ一-鿿㐀-䶿ｦ-ﾟ]")
LATIN_PATTERN = re.compile(r"[a-zA-ZＡ-Ｚａ-ｚ]")


def is_pure_english_lyric(text: str) -> bool:
    """Return True if text contains Latin letters and no CJK ideographs/kana."""
    clean = text.strip()
    return bool(LATIN_PATTERN.search(clean)) and not bool(CJK_PATTERN.search(clean))


class ExportValidationError(ValueError):
    """Raised when data integrity or schema constraints are violated."""
    pass


def sha256_text(value: str) -> str:
    """Return SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest().lower()


def sha256_file(path: Path) -> str:
    """Return SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def clean_translation(text: str) -> Tuple[str, List[str]]:
    """Sanitize reserved delimiters (| and ^) from translation string.

    Returns (cleaned_text, list_of_interceptions).
    """
    if not text:
        return "", []
    interceptions: List[str] = []
    cleaned = text
    for char, replacement in DELIMITER_REPLACEMENTS.items():
        if char in cleaned:
            count = cleaned.count(char)
            cleaned = cleaned.replace(char, replacement)
            interceptions.append(f"Replaced {count} '{char}' with '{replacement}'")
    return cleaned, interceptions


def classify_bundle(bundle: str) -> str:
    """Classify bundle into one of the 5 standard categories.

    Categories:
    - story: event stories, main stories, special commu, blog stories
    - card: card awakening, card episodes, card messages
    - dialogue: idol greetings, login bonus, live results, lounge talk, jobs
    - birth: birthday commu, birthday greetings
    - master: master tables (MD, CM, CD, MB, ST, bi, igp), system messages
    """
    b = bundle.lower()
    if b.startswith("birth_"):
        return "birth"
    if b.startswith("card_") or b.startswith("ch_"):
        return "card"
    if "story" in b or b.startswith(("event_", "special_", "main_", "blog_")):
        return "story"
    if b.startswith((
        "liveresult_", "lbonus_", "adv_", "talk_", "greeting_",
        "theater_greeting_", "touch_", "job_"
    )):
        return "dialogue"
    if b.startswith(("md_", "cm_", "cd_", "mb_", "st_", "igp_", "bi_")):
        return "master"
    return "master"


def build_idols_roster(
    character_voice_path: Optional[Path] = None,
    derived_idols_path: Optional[Path] = None,
    authoritative_terms_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build the official 52 idols roster plus staff and guest characters."""
    # Standard 52 idol metadata (id, speaker_code, name_jp, official target, type, division)
    # Owner ruling 2026-09-26: 032emi -> 艾米莉·斯图亚特, 045kar -> 篠宫可怜, 051tmg -> 白石䌷
    idols_52_def = [
        # 765PRO Allstars (1-13)
        (1, "001har", "天海春香", "天海春香", "Princess", "765PRO Allstars"),
        (2, "002chi", "如月千早", "如月千早", "Fairy", "765PRO Allstars"),
        (3, "003mik", "星井美希", "星井美希", "Angel", "765PRO Allstars"),
        (4, "004yuk", "萩原雪歩", "萩原雪步", "Princess", "765PRO Allstars"),
        (5, "005yay", "高槻やよい", "高槻弥生", "Angel", "765PRO Allstars"),
        (6, "006mak", "菊地真", "菊地真", "Princess", "765PRO Allstars"),
        (7, "007ior", "水瀬伊織", "水濑伊织", "Fairy", "765PRO Allstars"),
        (8, "008tak", "四条貴音", "四条贵音", "Fairy", "765PRO Allstars"),
        (9, "009rit", "秋月律子", "秋月律子", "Princess", "765PRO Allstars"),
        (10, "010azu", "三浦あずさ", "三浦梓", "Angel", "765PRO Allstars"),
        (11, "011ami", "双海亜美", "双海亚美", "Angel", "765PRO Allstars"),
        (12, "012mam", "双海真美", "双海真美", "Angel", "765PRO Allstars"),
        (13, "013hib", "我那覇響", "我那霸响", "Princess", "765PRO Allstars"),
        # 765PRO Theater (14-52)
        (14, "014mir", "春日未来", "春日未来", "Princess", "765PRO Theater"),
        (15, "015siz", "最上静香", "最上静香", "Fairy", "765PRO Theater"),
        (16, "016tsu", "伊吹翼", "伊吹翼", "Angel", "765PRO Theater"),
        (17, "017kth", "田中琴葉", "田中琴叶", "Princess", "765PRO Theater"),
        (18, "018ele", "島原エレナ", "岛原艾琳娜", "Angel", "765PRO Theater"),
        (19, "019min", "佐竹美奈子", "佐竹美奈子", "Princess", "765PRO Theater"),
        (20, "020meg", "所恵美", "所惠美", "Fairy", "765PRO Theater"),
        (21, "021mat", "徳川まつり", "德川茉莉", "Princess", "765PRO Theater"),
        (22, "022ser", "箱崎星梨花", "箱崎星梨花", "Angel", "765PRO Theater"),
        (23, "023aka", "野々原茜", "野野原茜", "Angel", "765PRO Theater"),
        (24, "024ann", "望月杏奈", "望月杏奈", "Angel", "765PRO Theater"),
        (25, "025roc", "ロコ", "ROCO", "Fairy", "765PRO Theater"),
        (26, "026yur", "七尾百合子", "七尾百合子", "Princess", "765PRO Theater"),
        (27, "027say", "高山紗代子", "高山纱代子", "Princess", "765PRO Theater"),
        (28, "028ari", "松田亜利沙", "松田亚利沙", "Princess", "765PRO Theater"),
        (29, "029umi", "高坂海美", "高坂海美", "Princess", "765PRO Theater"),
        (30, "030iku", "中谷育", "中谷育", "Princess", "765PRO Theater"),
        (31, "031tom", "天空橋朋花", "天空桥朋花", "Fairy", "765PRO Theater"),
        (32, "032emi", "エミリースチュアート", "艾米莉·斯图亚特", "Princess", "765PRO Theater"),
        (33, "033sih", "北沢志保", "北泽志保", "Fairy", "765PRO Theater"),
        (34, "034ayu", "舞浜歩", "舞滨步", "Fairy", "765PRO Theater"),
        (35, "035hin", "木下ひなた", "木下日向", "Angel", "765PRO Theater"),
        (36, "036kan", "矢吹可奈", "矢吹可奈", "Princess", "765PRO Theater"),
        (37, "037nao", "横山奈緒", "横山奈绪", "Princess", "765PRO Theater"),
        (38, "038chz", "二階堂千鶴", "二阶堂千鹤", "Fairy", "765PRO Theater"),
        (39, "039kon", "馬場このみ", "马场木实", "Angel", "765PRO Theater"),
        (40, "040tam", "大神環", "大神环", "Angel", "765PRO Theater"),
        (41, "041fuk", "豊川風花", "丰川风花", "Angel", "765PRO Theater"),
        (42, "042miy", "宮尾美也", "宫尾美也", "Angel", "765PRO Theater"),
        (43, "043nor", "福田のり子", "福田法子", "Princess", "765PRO Theater"),
        (44, "044miz", "真壁瑞希", "真壁瑞希", "Fairy", "765PRO Theater"),
        (45, "045kar", "篠宮可憐", "篠宫可怜", "Angel", "765PRO Theater"),
        (46, "046rio", "百瀬莉緒", "百濑莉绪", "Fairy", "765PRO Theater"),
        (47, "047sub", "永吉昴", "永吉昴", "Fairy", "765PRO Theater"),
        (48, "048rei", "北上麗花", "北上丽花", "Angel", "765PRO Theater"),
        (49, "049mom", "周防桃子", "周防桃子", "Fairy", "765PRO Theater"),
        (50, "050jul", "ジュリア", "茱莉亚", "Fairy", "765PRO Theater"),
        (51, "051tmg", "白石紬", "白石䌷", "Fairy", "765PRO Theater"),
        (52, "052kao", "桜守歌織", "樱守歌织", "Angel", "765PRO Theater"),
    ]

    idols_list: List[Dict[str, Any]] = []
    for idol_id, code, name_jp, name_zh, itype, division in idols_52_def:
        ruling = "official_legacy_mechanical_t2s"
        note = "Standard 765PRO idol roster"
        if code == "032emi":
            ruling = "owner_ruling_2026-09-26"
            note = "Owner chose U+00B7 middle dot separator and 艾米莉·斯图亚特 spelling"
        elif code == "045kar":
            ruling = "owner_ruling_2026-09-26"
            note = "Owner preserves 篠 U+7BE0 glyph instead of OpenCC 筿"
        elif code == "051tmg":
            ruling = "owner_ruling_2026-09-26"
            note = "Owner confirmed published 白石䌷 (U+4337) form"

        idols_list.append({
            "idol_id": idol_id,
            "speaker_code": code,
            "name_jp": name_jp,
            "name_zh": name_zh,
            "type": itype,
            "division": division,
            "provenance": ruling,
            "note": note,
        })

    staff_and_guests = [
        {"code": "101kot", "name_jp": "音無小鳥", "name_zh": "音无小鸟", "role": "765PRO Clerk"},
        {"code": "102mis", "name_jp": "青羽美咲", "name_zh": "青羽美咲", "role": "765PRO Theater Clerk"},
        {"code": "103jun", "name_jp": "高木順二朗", "name_zh": "高木顺二朗", "role": "765PRO President"},
        {"code": "201xxx", "name_jp": "詩花", "name_zh": "诗花", "role": "961PRO Idol (Guest)"},
        {"code": "202xxx", "name_jp": "玲音", "name_zh": "玲音", "role": "961PRO Idol (Guest)"},
        {"code": "204xxx", "name_jp": "宮本フレデリカ", "name_zh": "宫本芙蕾德莉卡", "role": "346PRO Idol (Collab)"},
        {"code": "205xxx", "name_jp": "一ノ瀬志希", "name_zh": "一之濑志希", "role": "346PRO Idol (Collab)"},
        {"code": "404apr", "name_jp": "プロデューサー", "name_zh": "制作人", "role": "Producer"},
    ]

    return {
        "schema_version": 1,
        "kind": "mltd-authoritative-idols",
        "description": "Standard 52 idols of 765PRO plus theater staff and guest idols",
        "counts": {
            "total_standard_idols": len(idols_list),
            "allstars": 13,
            "theater": 39,
            "staff_and_guests": len(staff_and_guests),
        },
        "idols": idols_list,
        "staff_and_guests": staff_and_guests,
    }


def load_authoritative_terms(path: Path) -> Dict[str, str]:
    """Load exact authoritative terms mapping source -> target."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = data.get("entries", {})
    return {k: v["target"] for k, v in entries.items() if isinstance(v, dict) and "target" in v}


def load_accepted_translations(paths: Path | List[Path]) -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    """Load accepted legacy translations.

    Returns:
    - exact_map: (bundle, key) -> translation
    - sha_map: source_sha256 -> translation
    """
    exact_map: Dict[Tuple[str, str], str] = {}
    sha_map: Dict[str, str] = {}
    path_list = [paths] if isinstance(paths, Path) else paths

    for path in path_list:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                b = row.get("bundle")
                k = row.get("key")
                t = row.get("translation")
                s = row.get("source")
                sid = row.get("source_sha256")
                if not t:
                    continue
                if b and k and (b.casefold(), k) not in exact_map:
                    exact_map[(b.casefold(), k)] = t
                if sid and sid not in sha_map:
                    sha_map[sid] = t
                if s:
                    s_sha = sha256_text(s)
                    if s_sha not in sha_map:
                        sha_map[s_sha] = t
    return exact_map, sha_map


def load_machine_translations(paths: Path | List[Path]) -> Dict[str, str]:
    """Load machine translations pool mapping source_sha256 -> translation."""
    pool: Dict[str, str] = {}
    path_list = [paths] if isinstance(paths, Path) else paths

    for path in path_list:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                s_sha = row.get("source_sha256")
                t = row.get("translation")
                s = row.get("source")
                if not t:
                    continue
                if s_sha and s_sha not in pool:
                    pool[s_sha] = t
                if s:
                    calc_sha = sha256_text(s)
                    if calc_sha not in pool:
                        pool[calc_sha] = t
    return pool


def export_locales(
    queue_path: Path,
    out_dir: Path,
    base_version: str,
    legacy_exact: Dict[Tuple[str, str], str],
    legacy_sha: Dict[str, str],
    machine_sha: Dict[str, str],
    authoritative_terms: Dict[str, str],
    include_machine_drafts: bool,
    timestamp: str,
) -> Dict[str, Any]:
    """Export queue/catalogue into categorized locales/ files with strict schema and integrity."""
    locales_dir = out_dir / "locales"
    for cat in CATEGORIES:
        cat_p = locales_dir / cat
        cat_p.mkdir(parents=True, exist_ok=True)
        for old_f in cat_p.glob("*.jsonl"):
            try:
                old_f.unlink()
            except OSError:
                pass

    from collections import defaultdict
    bundle_entries: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    bundle_categories: Dict[str, str] = {}

    stats = {
        "total_rows": 0,
        "status_counts": {"accepted": 0, "pending": 0, "untranslated": 0},
        "match_methods": {
            "legacy_exact": 0,
            "legacy_memory_sha": 0,
            "authoritative_term": 0,
            "machine_draft": 0,
            "none": 0,
        },
        "category_counts": {cat: 0 for cat in CATEGORIES},
        "category_bundle_counts": {cat: 0 for cat in CATEGORIES},
        "sanitized_delimiter_rows": 0,
    }

    with queue_path.open("r", encoding="utf-8-sig") as q_handle:
        for line_idx, line in enumerate(q_handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            bundle = row["bundle"]
            key = row["key"]
            source = row["source"]
            declared_sha = row.get("source_sha256") or sha256_text(source)

            # 1. Verify source SHA-256 integrity
            computed_sha = sha256_text(source)
            if declared_sha.lower() != computed_sha:
                raise ExportValidationError(
                    f"line {line_idx} ({bundle}/{key}): source_sha256 mismatch "
                    f"(declared {declared_sha} != computed {computed_sha})"
                )

            # 2. Determine category
            category = classify_bundle(bundle)
            bundle_categories[bundle] = category

            # 3. Match translation
            translation = ""
            status = "untranslated"
            match_method = "none"

            identity = (bundle.casefold(), key)
            if identity in legacy_exact:
                translation = legacy_exact[identity]
                status = "accepted"
                match_method = "legacy_exact"
            elif computed_sha in legacy_sha:
                translation = legacy_sha[computed_sha]
                status = "accepted"
                match_method = "legacy_memory_sha"
            elif source.strip() in authoritative_terms:
                translation = authoritative_terms[source.strip()]
                status = "accepted"
                match_method = "authoritative_term"
            elif include_machine_drafts and computed_sha in machine_sha:
                translation = machine_sha[computed_sha]
                status = "accepted"
                match_method = "machine_draft"

            # 4. Clean illegal delimiters in translation (| and ^)
            cleaned_translation, interceptions = clean_translation(translation)
            if interceptions:
                stats["sanitized_delimiter_rows"] += 1

            if not cleaned_translation:
                status = "untranslated"

            # 5. Format standard item entry
            entry = {
                "base_version": base_version,
                "bundle": bundle,
                "item_key": key,
                "source_sha256": computed_sha,
                "ja": source,
                "zh": cleaned_translation,
                "status": status,
                "updated_at": timestamp,
            }

            bundle_entries[(category, bundle)].append(entry)

            stats["total_rows"] += 1
            stats["status_counts"][status] += 1
            stats["match_methods"][match_method] += 1
            stats["category_counts"][category] += 1

    # Write each bundle file sequentially
    for (cat, bundle), entries in bundle_entries.items():
        bundle_file = locales_dir / cat / f"{bundle}.jsonl"
        with bundle_file.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    for bundle, cat in bundle_categories.items():
        stats["category_bundle_counts"][cat] += 1

    return stats


def export_lyrics(
    manifest_path: Path,
    slots_path: Path,
    translations_path: Path,
    out_dir: Path,
    timestamp: str,
    bypass_english: bool = True,
) -> Dict[str, Any]:
    """Export songs lyrics with bilingual alignment into lyrics/."""
    lyrics_dir = out_dir / "lyrics"
    songs_dir = lyrics_dir / "songs"
    songs_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load lyric translations
    translations: Dict[str, str] = {}
    if translations_path.is_file():
        with translations_path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                s_sha = row.get("source_sha256")
                t = row.get("translation")
                if s_sha and t:
                    translations[s_sha] = t

    # 2. Group slots by song bundle
    song_slots: Dict[str, List[Dict[str, Any]]] = {}
    all_lyrics_entries: List[Dict[str, Any]] = []
    total_slots = 0
    english_bypassed_slots = 0

    if slots_path.is_file():
        with slots_path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                slot = json.loads(line)
                bundle = slot.get("logical", slot.get("remote", "unknown_bundle"))
                s_sha = slot.get("source_sha256")
                source = slot.get("source", "")

                if bypass_english and is_pure_english_lyric(source):
                    cleaned_translation = ""
                    status = "untranslated"
                    english_bypassed_slots += 1
                else:
                    raw_translation = translations.get(s_sha, "")
                    cleaned_translation, _ = clean_translation(raw_translation)
                    status = "accepted" if cleaned_translation else "untranslated"

                lyric_entry = {
                    "bundle": bundle,
                    "index": slot.get("index"),
                    "tick": slot.get("tick"),
                    "abs_time": slot.get("absTime"),
                    "source_sha256": s_sha,
                    "ja": source,
                    "zh": cleaned_translation,
                    "status": status,
                    "updated_at": timestamp,
                }
                song_slots.setdefault(bundle, []).append(lyric_entry)
                all_lyrics_entries.append(lyric_entry)
                total_slots += 1

    # Write per-song JSONL
    for bundle, entries in song_slots.items():
        song_file = songs_dir / f"{bundle}.jsonl"
        with song_file.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Write consolidated all_lyrics.jsonl
    all_lyrics_file = lyrics_dir / "all_lyrics.jsonl"
    with all_lyrics_file.open("w", encoding="utf-8") as f:
        for entry in all_lyrics_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Write lyrics manifest
    manifest_doc = {
        "schema_version": 1,
        "kind": "mltd-lyrics-manifest",
        "description": "Bilingual synchronized lyrics with scrobj slot alignment",
        "counts": {
            "total_songs": len(song_slots),
            "total_slots": total_slots,
            "translated_slots": sum(1 for e in all_lyrics_entries if e["status"] == "accepted"),
            "english_bypass_slots": english_bypassed_slots,
        },
        "songs": [
            {
                "bundle": b,
                "slots": len(entries),
                "translated": sum(1 for e in entries if e["status"] == "accepted"),
            }
            for b, entries in sorted(song_slots.items())
        ],
    }
    (lyrics_dir / "lyrics_manifest.json").write_text(
        json.dumps(manifest_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return manifest_doc["counts"]


def export_images_manifest(
    texture_manifest_path: Path,
    out_dir: Path,
    timestamp: str,
) -> Dict[str, Any]:
    """Export the 937 reviewed textures to manifests/images.manifest.json.

    Bottom-bar atlases are deliberately absent: they are baked into the APK's
    embedded ``data.unity3d`` and cannot be delivered through the asset-server
    overlay, so they live in the client repository instead (see
    ``export_apk_builtin_manifest``).
    """
    manifests_dir = out_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    items: List[Dict[str, Any]] = []
    seen_hashes: Set[str] = set()

    # 1. 937 reviewed textures
    if texture_manifest_path.is_file():
        with texture_manifest_path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                restored_sha = row["restored_png_sha256"]
                if restored_sha in seen_hashes:
                    continue
                seen_hashes.add(restored_sha)

                orig_png = row.get("original_png", "")
                bundle = row.get("bundle", "")
                size = row.get("original_size", [512, 512])

                asset_id = f"{bundle}:{row.get('texture_path_id')}"
                items.append({
                    "id": asset_id,
                    "kind": "ui_texture",
                    "bundle": bundle,
                    "texture_path_id": row.get("texture_path_id"),
                    "dimensions": {
                        "width": size[0] if len(size) > 0 else 512,
                        "height": size[1] if len(size) > 1 else 512,
                    },
                    "original": {
                        "relative_path": f"images/original/{orig_png}" if not orig_png.startswith("images/") else orig_png,
                        "sha256": row.get("original_png_sha256"),
                    },
                    "localized": {
                        "relative_path": f"images/localized/{orig_png.replace('original/', '')}",
                        "sha256": restored_sha,
                    },
                    "distribution": {
                        "storage_policy": "external_binary_hosting",
                        "github_release": {
                            "asset_name": f"tex_{restored_sha[:16]}.png",
                            "url_template": f"https://github.com/{{owner}}/{{repo}}/releases/download/v1.0.0-assets/tex_{restored_sha[:16]}.png",
                        },
                        "cloudflare_r2": {
                            "key": f"images/{restored_sha}.png",
                            "url_template": f"https://pub-mltd-assets.nyaneko.cn/images/{restored_sha}.png",
                        },
                    },
                    "review_status": row.get("review_status", "accepted"),
                })

    doc = {
        "schema_version": 1,
        "kind": "mltd-images-manifest",
        "generated_at": timestamp,
        "storage_architecture": {
            "text_in_repo": True,
            "binary_in_repo": False,
            "distribution_providers": ["GitHub Release Assets", "Cloudflare R2"],
        },
        "counts": {
            "total_images": len(items),
            "reviewed_textures": len(seen_hashes),
        },
        "images": items,
    }
    (manifests_dir / "images.manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return doc["counts"]


def export_bottom_bar_manifest(
    bottom_bar_strings_path: Path,
    out_dir: Path,
    timestamp: str,
) -> Dict[str, Any]:
    """Export bottom bar navigation labels and sprite definitions.

    This surface is baked into the APK's embedded ``data.unity3d`` atlas
    (``theater_system_footer_main``), so it is delivered by the client
    repository rather than the asset-server overlay.
    """
    manifests_dir = out_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    slots: List[Dict[str, Any]] = []
    atlas_target: Dict[str, Any] = {
        "serialized_file": "sharedassets1.assets",
        "path_id": 4,
        "texture": "theater_system_footer_main",
        "size": [512, 512],
    }
    catalogue_meta: Dict[str, Any] = {}
    if bottom_bar_strings_path.is_file():
        data = json.loads(bottom_bar_strings_path.read_text(encoding="utf-8-sig"))
        surface = data.get("surfaces", {}).get("bottom-bar-footer", {})
        slots = surface.get("slots", [])
        # The client catalogue is the producer's own description of the atlas;
        # prefer it over the fallback constants whenever it carries the field.
        for key, target in (
            ("client_version", "client_version"),
            ("apk_entry", "apk_entry"),
            ("serialized_file", "serialized_file"),
            ("path_id", "path_id"),
            ("texture", "texture"),
            ("sprite_pattern", "sprite_pattern"),
        ):
            if surface.get(key) is not None:
                atlas_target[target] = surface[key]
        if surface.get("atlas_size"):
            atlas_target["size"] = surface["atlas_size"]
        if surface.get("evidence"):
            catalogue_meta["evidence"] = surface["evidence"]
        catalogue_meta["catalogue"] = bottom_bar_strings_path.as_posix()
        if data.get("policy"):
            catalogue_meta["policy"] = data["policy"]

    if not slots:
        # Fallback to standard 7 labels
        slots = [
            {"index": 0, "ja": "劇場", "zh": "剧场", "provenance": "project_decision"},
            {"index": 1, "ja": "アイドル", "zh": "偶像", "provenance": "shared_glossary"},
            {"index": 2, "ja": "コミュ", "zh": "剧情", "provenance": "project_decision"},
            {"index": 3, "ja": "ライブ", "zh": "演唱会", "provenance": "project_decision"},
            {"index": 4, "ja": "お仕事", "zh": "工作", "provenance": "project_decision"},
            {"index": 5, "ja": "ガシャ", "zh": "转蛋", "provenance": "project_decision"},
            {"index": 6, "ja": "ナビ", "zh": "导航", "provenance": "project_decision"},
        ]

    atlas_target["slot_count"] = len(slots)
    doc = {
        "schema_version": 1,
        "kind": "mltd-bottom-bar-manifest",
        "generated_at": timestamp,
        "atlas_target": atlas_target,
        "slots": slots,
        "previews": {
            "montage_off": {
                "sha256": "49d7721967789b9020802c22713dc3e8ecf5307504176bb6cfccfb1623c03a5e",
                "bytes": 97119,
            },
            "montage_on": {
                "sha256": "c371d85cc33b0f5419daf020f5e2364bac525fd04686e64020e66f825d4ce87e",
                "bytes": 112776,
            },
        },
    }
    if catalogue_meta:
        doc["provenance"] = catalogue_meta
    (manifests_dir / "bottom-bar.manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"slot_count": len(slots)}


def export_apk_builtin_manifest(
    apk_bi_verification_path: Path,
    out_dir: Path,
    timestamp: str,
) -> Dict[str, Any]:
    """Index the surfaces that only ever ship inside the APK.

    The asset-server overlay can carry GTX text, lyrics and image textures, but
    the APK's embedded ``assets/bin/Data/data.unity3d`` holds three surfaces the
    server never routes: the bottom-bar footer atlas (see the sibling
    ``bottom-bar.manifest.json``), the runtime BI string table
    (``BI_jp.gtx``) and the CJK font object. This manifest records their
    digests so the client repository is auditable without carrying any binary.

    Only a verified, non-canonical candidate is indexed; the manifest states
    that status verbatim instead of implying a release.
    """
    manifests_dir = out_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    surfaces: List[Dict[str, Any]] = [
        {
            "name": "bottom-bar-atlas",
            "kind": "baked-sprite-atlas",
            "target": "sharedassets1.assets:4",
            "texture": "theater_system_footer_main",
            "manifest": "manifests/bottom-bar.manifest.json",
            "slot_count": 7,
        }
    ]
    provenance: Dict[str, Any] = {
        "verification_manifest": None,
        "client_version": None,
        "assets_version": None,
        "artifact_status": "unrecorded",
    }

    if apk_bi_verification_path.is_file():
        data = json.loads(apk_bi_verification_path.read_text(encoding="utf-8-sig"))
        candidate = data.get("verified_artifact", {})
        checks = data.get("checks", {})
        provenance = {
            "verification_manifest": apk_bi_verification_path.as_posix(),
            "client_version": data.get("client_version"),
            "assets_version": str(data.get("assets_version")) if data.get("assets_version") else None,
            "artifact_status": "unreviewed_candidate" if data.get("UNREVIEWED") else str(data.get("status", "recorded")),
            "reviewed": bool(data.get("independent_reviewed") is True),
        }
        surfaces.append(
            {
                "name": "runtime-bi",
                "kind": "encrypted-gtx-textasset",
                "target": f"data.unity3d:{checks.get('textassets_changed', [{}])[0].get('path_id', 646)}",
                "textasset": "BI_jp.gtx",
                "records": checks.get("bi_runtime_records"),
                "unique_keys": checks.get("bi_runtime_unique_keys"),
                "values_with_kana_after": checks.get("bi_runtime_values_with_kana"),
                "values_with_han_after": checks.get("bi_runtime_values_with_han"),
                "source": {
                    "relative_path": "data.unity3d",
                    "sha256": checks.get("embedded_data_unity3d_sha256"),
                    "bytes": candidate.get("bytes"),
                },
            }
        )
        font_path_id = checks.get("font_object_path_id")
        if font_path_id is not None:
            surfaces.append(
                {
                    "name": "cjk-font",
                    "kind": "unity-font-object",
                    "target": f"data.unity3d:{font_path_id}",
                    "family": checks.get("font_object_name"),
                    "sha256": checks.get("font_object_sha256"),
                    "source_asset": "Noto Sans CJK SC",
                }
            )

    doc = {
        "schema_version": 1,
        "kind": "mltd-apk-builtin-manifest",
        "generated_at": timestamp,
        "storage_architecture": {
            "binary_in_repo": False,
            "delivery": "apk_embedded_data_unity3d",
            "note": "These surfaces cannot be delivered by the asset-server overlay.",
        },
        "provenance": provenance,
        "counts": {"surfaces": len(surfaces)},
        "surfaces": surfaces,
    }
    (manifests_dir / "apk-builtin.manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # The version anchor of this repository is the cohort the APK was actually
    # built against — read off the verification report, never a cron guess.
    if provenance["client_version"] and provenance["assets_version"]:
        version_doc = {
            "client_version": provenance["client_version"],
            "asset_version": int(provenance["assets_version"]),
            "source": provenance["verification_manifest"],
            "artifact_status": provenance["artifact_status"],
            "note": "Anchor updated by the APK build pipeline; this repository does not follow upstream releases.",
        }
        (manifests_dir / "asset-version.json").write_text(
            json.dumps(version_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return {"surfaces": len(surfaces)}


def export_schemas_and_repo_docs(out_dir: Path, timestamp: str, builtin_out_dir: Path | None = None) -> None:
    """Export formal JSON Schemas, README, .gitignore and LICENSE.

    ``out_dir`` is the asset-server repository (locales/lyrics/glossary/images).
    ``builtin_out_dir``, when given, additionally receives the APK-built-in
    repository's schema and README.
    """
    schema_dir = out_dir / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)

    entry_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MLTDLocalizationEntry",
        "description": "Standard schema for a single localization line in MLTD locales",
        "type": "object",
        "required": [
            "base_version", "bundle", "item_key", "source_sha256",
            "ja", "zh", "status", "updated_at"
        ],
        "properties": {
            "base_version": {
                "type": "string",
                "pattern": r"^[0-9]+\.[0-9]+\.[0-9]+\+[0-9]+$",
                "description": "Target client base version and assets build",
            },
            "bundle": {
                "type": "string",
                "description": "Logical or physical asset bundle name",
            },
            "item_key": {
                "type": "string",
                "description": "Unique key inside bundle",
            },
            "source_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "SHA-256 hex digest of ja source text",
            },
            "ja": {
                "type": "string",
                "minLength": 1,
                "description": "Original Japanese source text",
            },
            "zh": {
                "type": "string",
                "pattern": r"^[^|\^]*$",
                "description": "Simplified Chinese translation (no | or ^ allowed)",
            },
            "status": {
                "type": "string",
                "enum": ["untranslated", "pending", "accepted"],
                "description": "Translation review status",
            },
            "updated_at": {
                "type": "string",
                "format": "date-time",
                "description": "ISO 8601 UTC timestamp",
            },
        },
        "additionalProperties": False,
    }
    (schema_dir / "entry.schema.json").write_text(
        json.dumps(entry_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    readme_content = f"""# MLTD 简体中文汉化开源资源库 (THE IDOLM@STER MILLION LIVE! THEATER DAYS Localization Assets)

这是一个个人业余项目，用于整理《偶像大师 百万现场 剧场时光》(MLTD) 的简体中文汉化资源。
仓库中的文本、术语表、歌词和贴图 Manifest 会持续更新，欢迎通过 GitHub PR 参与修订。

本仓库只承载**经 assets 服务器下发的面**（`/cn/<asset>/` overlay）。底栏贴图、BI 文案与字体属于
APK 内置面，由配套仓库 [MLTDTranslationClient](https://github.com/kohakunamori/MLTDTranslationClient)
维护。

## 目录结构

- `locales/`：核心业务文本库（UTF-8 JSONL，单行精确定位）
  - `locales/story/`：活动剧情、主线剧情、特别剧情
  - `locales/card/`：卡片觉醒剧情、通常剧情、卡片短信
  - `locales/dialogue/`：偶像触碰台词、常驻问候、工作对话、演出结算台词
  - `locales/birth/`：偶像生日剧情与白板问候
  - `locales/master/`：Master 核心主数据表、菜单UI、卡片技能、系统提示
- `lyrics/`：全曲目歌词库（432 首歌曲对齐双语歌词与时间戳）
  - `lyrics/songs/`：按歌曲独立分轨 JSONL
  - `lyrics/all_lyrics.jsonl`：全曲歌词总汇
- `glossary/`：翻译规范与项目术语
  - `glossary/authoritative-terms.json`：项目当前采用的固定译名与避免词
  - `glossary/idols.json`：项目整理的 52 名偶像与声优名录
- `manifests/`：贴图元数据清单（文字在库，多媒体外链）
  - `manifests/images.manifest.json`：937 张已汉化贴图的 SHA-256 索引
- `schema/`：数据规范与 JSON Schema 定义

## 条目格式规范

每一行 JSONL 严格遵循以下规范：

```json
{{
  "base_version": "9.0.200+1077500",
  "bundle": "event_0448_story_06_jp.gtx",
  "item_key": "event_0448_story_06_title",
  "source_sha256": "ea4cef9ff36d07f10f6bd00f4163edfa882ccd469392bc96127a9b2b6b45ae7f",
  "ja": "本領発揮",
  "zh": "大显身手",
  "status": "accepted",
  "updated_at": "{timestamp}"
}}
```

### 关键约束
1. **防止版本漂移**：`source_sha256` 必须与 `ja` 原文字符串的 SHA-256 强校验匹配。
2. **安全隔离控制符**：客户端引擎使用 `|` 和 `^` 作为底层控制分隔符。**严禁在译文 `zh` 中输入半角 `|` 或 `^`**（可使用全角 `｜` 或 `＾`）。
3. **状态说明**：
   - `untranslated`：待翻译条目，`zh` 为空字符串。
   - `pending`：已生成初稿或机器翻译，等待人工审校。
   - `accepted`：已通过质量审校的正式译文。

## Web 翻译门户与在线协同

您可以通过社区翻译门户直接在线认领翻译与审校：
- **Web 门户**：https://mltd-translate.nyaneko.cn
- **提交 PR**：欢迎在 GitHub 直接提交 Pull Request，CI 机器人将对每一行的数据完整性进行自动化检测。

## 许可证与致谢

本仓库文本基于游戏日版文本翻译与整理，版权归 Bandai Namco Entertainment Inc. 所有。
汉化成果遵循社区开源共享协议，严禁用于任何商业用途。
"""
    (out_dir / "README.md").write_text(readme_content, encoding="utf-8")

    gitignore_content = """# Local artifacts and temp files
*.tmp
*.log
.DS_Store
Thumbs.db
__pycache__/
*.pyc
node_modules/
.idea/
.vscode/
d1-contributions-sync/
work/
"""
    (out_dir / ".gitignore").write_text(gitignore_content, encoding="utf-8")

    if builtin_out_dir is not None:
        export_apk_builtin_schema(builtin_out_dir, timestamp, gitignore_content)


def export_apk_builtin_schema(out_dir: Path, timestamp: str, gitignore_content: str = "") -> None:
    """Export the client repository's own schema and README."""
    schema_dir = out_dir / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)

    builtin_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MLTDAPKBuiltinManifest",
        "description": "Surfaces baked into the APK's embedded data.unity3d",
        "type": "object",
        "required": ["schema_version", "kind", "provenance", "surfaces"],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "kind": {"type": "string", "const": "mltd-apk-builtin-manifest"},
            "generated_at": {"type": "string", "format": "date-time"},
            "storage_architecture": {"type": "object"},
            "provenance": {
                "type": "object",
                "properties": {
                    "verification_manifest": {"type": ["string", "null"]},
                    "client_version": {"type": ["string", "null"]},
                    "assets_version": {"type": ["string", "null"]},
                    "artifact_status": {"type": "string"},
                    "reviewed": {"type": "boolean"},
                },
            },
            "counts": {"type": "object"},
            "surfaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "kind", "target"],
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": ["bottom-bar-atlas", "runtime-bi", "cjk-font"],
                        },
                        "kind": {"type": "string"},
                        "target": {"type": "string"},
                    },
                },
            },
        },
        "additionalProperties": False,
    }
    (schema_dir / "apk-builtin.schema.json").write_text(
        json.dumps(builtin_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    readme_content = f"""# MLTD 客户端内置汉化资源库 (THE IDOLM@STER MILLION LIVE! THEATER DAYS Client Built-in Localization)

本仓库承载**只能随 APK 下发**的汉化面：它们被烘焙进 APK 内的
`assets/bin/Data/data.unity3d`，assets 服务器（`/cn/<asset>/` overlay）不会也不路由器。

文本译文与贴图等经服务器下发的面，由配套仓库
[MLTDTranslationAssets](https://github.com/kohakunamori/MLTDTranslationAssets) 维护。

> 状态：`manifests/apk-builtin.manifest.json` 的 `provenance.artifact_status`
> 记录每个被打包面的验收状态（当前为 `unreviewed_candidate`，即未人工审校的候选）。
> 本仓库只做元数据索引，不携带任何二进制。

## 目录结构

- `manifests/bottom-bar.manifest.json`：底栏 7 个标签的日/中对照与图集坐标
  （`sharedassets1.assets` path_id 4 / `theater_system_footer_main` 512×512）。
- `manifests/apk-builtin.manifest.json`：APK 内置面总索引——底栏图集、运行时 BI 文案表
  （`BI_jp.gtx`）与 CJK 字体对象，各带 SHA-256 与来源验证报告。
- `manifests/asset-version.json`：**APK 实际构建所用**的客户端 + 资源 cohort
  （由 APK 构建流水线回写，本仓库不跟随上游资源版本）。
- `schema/apk-builtin.schema.json`：上述索引的 JSON Schema。

## 版本分支与标签

`main` 与 `manifests/asset-version.json` 由 APK 构建流水线推进，**不做上游版本跟随**；
每条用于构建 APK 的 cohort 另以两个 ref 冻结：

- 标签 `assets-<资源版本>`（如 `assets-1077100`）。
- 分支 `release/<客户端版本>+<资源版本>`（如 `release/9.0.200+1077100`）。

## 为什么单独成库

底栏标签是图集里的**像素**（`Texture2D` atlas），BI 文案是 `data.unity3d` 内的加密
`TextAsset`，字体是同文件内的 Font 对象——三者都不能通过文本 overlay 替换。它们与文本
译文的生产方、验收门槛与发布通道完全不同，因此各自独立成库，避免一方的版本冻结或
CI 规则误伤另一方。

## 许可证与致谢

游戏原始文本、角色、图片、字体与音频著作权均归 Bandai Namco Entertainment Inc. 所有。
汉化成果遵循 [CC-BY-NC-SA 4.0](LICENSE)。
"""
    (out_dir / "README.md").write_text(readme_content, encoding="utf-8")
    if gitignore_content:
        (out_dir / ".gitignore").write_text(gitignore_content, encoding="utf-8")


def export_all(
    queue_path: Path,
    legacy_path: Path | List[Path],
    machine_path: Path | List[Path],
    terms_path: Path,
    character_voice_path: Path,
    derived_idols_path: Path,
    lyrics_manifest_path: Path,
    lyrics_slots_path: Path,
    lyrics_translations_path: Path,
    texture_manifest_path: Path,
    bottom_bar_manifest_path: Path,
    bottom_bar_strings_path: Path,
    out_dir: Path,
    apk_bi_verification_path: Path | None = None,
    builtin_out_dir: Path | None = None,
    base_version: str = "9.0.200+1077500",
    include_machine_drafts: bool = True,
    bypass_english_lyrics: bool = True,
    timestamp: str = "2026-09-27T00:00:00Z",
) -> Dict[str, Any]:
    """Execute complete export pipeline.

    ``out_dir`` receives every surface the asset-server overlay can deliver.
    ``builtin_out_dir`` (optional) receives the surfaces that only ship inside
    the APK: the bottom-bar atlas index and the APK-built-in manifest.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load glossaries & translation dictionaries
    print(f"Loading authoritative terms from {terms_path}...")
    terms = load_authoritative_terms(terms_path)

    print(f"Loading legacy accepted translations from {legacy_path}...")
    legacy_exact, legacy_sha = load_accepted_translations(legacy_path)

    machine_sha: Dict[str, str] = {}
    if include_machine_drafts:
        print(f"Loading machine translations pool from {machine_path}...")
        machine_sha = load_machine_translations(machine_path)

    # 2. Export glossary
    print("Exporting glossary (authoritative terms and 52 idols roster)...")
    glossary_dir = out_dir / "glossary"
    glossary_dir.mkdir(parents=True, exist_ok=True)

    if terms_path.is_file():
        terms_doc = json.loads(terms_path.read_text(encoding="utf-8-sig"))
        (glossary_dir / "authoritative-terms.json").write_text(
            json.dumps(terms_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    idols_doc = build_idols_roster(
        character_voice_path=character_voice_path,
        derived_idols_path=derived_idols_path,
        authoritative_terms_path=terms_path,
    )
    (glossary_dir / "idols.json").write_text(
        json.dumps(idols_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 3. Export locales
    print(f"Exporting locales from queue {queue_path}...")
    locales_stats = export_locales(
        queue_path=queue_path,
        out_dir=out_dir,
        base_version=base_version,
        legacy_exact=legacy_exact,
        legacy_sha=legacy_sha,
        machine_sha=machine_sha,
        authoritative_terms=terms,
        include_machine_drafts=include_machine_drafts,
        timestamp=timestamp,
    )

    # 4. Export lyrics
    print(f"Exporting lyrics and scrobj slot alignments (bypass_english={bypass_english_lyrics})...")
    lyrics_counts = export_lyrics(
        manifest_path=lyrics_manifest_path,
        slots_path=lyrics_slots_path,
        translations_path=lyrics_translations_path,
        out_dir=out_dir,
        timestamp=timestamp,
        bypass_english=bypass_english_lyrics,
    )

    # 5. Export manifests
    print("Exporting rich media manifests...")
    images_counts = export_images_manifest(
        texture_manifest_path=texture_manifest_path,
        out_dir=out_dir,
        timestamp=timestamp,
    )

    builtin_counts: Dict[str, Any] = {}
    if builtin_out_dir is not None:
        print("Exporting APK built-in manifests (client repository)...")
        builtin_out_dir.mkdir(parents=True, exist_ok=True)
        bottom_bar_counts = export_bottom_bar_manifest(
            bottom_bar_strings_path=bottom_bar_strings_path,
            out_dir=builtin_out_dir,
            timestamp=timestamp,
        )
        builtin_counts = export_apk_builtin_manifest(
            apk_bi_verification_path=apk_bi_verification_path or Path(""),
            out_dir=builtin_out_dir,
            timestamp=timestamp,
        )
        builtin_counts["bottom_bar_slots"] = bottom_bar_counts["slot_count"]

    # 6. Export schemas and repo docs
    print("Generating schema definitions and README...")
    export_schemas_and_repo_docs(out_dir=out_dir, timestamp=timestamp, builtin_out_dir=builtin_out_dir)

    summary = {
        "schema_version": 1,
        "kind": "mltd-github-export-summary",
        "generated_at": timestamp,
        "base_version": base_version,
        "out_dir": str(out_dir),
        "builtin_out_dir": str(builtin_out_dir) if builtin_out_dir is not None else None,
        "locales": locales_stats,
        "lyrics": lyrics_counts,
        "images": images_counts,
        "builtin": builtin_counts,
        "bottom_bar": {"slot_count": builtin_counts.get("bottom_bar_slots")} if builtin_counts else {},
        "glossary": {
            "authoritative_terms": len(terms),
            "standard_idols": idols_doc["counts"]["total_standard_idols"],
        },
    }

    (out_dir / "export-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # Write HANDOFF.md for MLTD workstream convention
    handoff_text = f"""# HANDOFF — MLTD 汉化资源 GitHub 公开仓库分类导出 (Phase 1)

- **stream**: text-localization
- **run**: `{out_dir}`
- **artifact_status**: **candidate**
- **base_version**: `{base_version}`
- **导出时间**: `{timestamp}`

## 1. 导出概览
- **待译全集总行数**: {locales_stats['total_rows']} 行，覆盖 5 大业务分类（51 个 Bundle）。
  - `locales/master/`: {locales_stats['category_counts']['master']} 行 ({locales_stats['category_bundle_counts']['master']} bundles)
  - `locales/card/`: {locales_stats['category_counts']['card']} 行 ({locales_stats['category_bundle_counts']['card']} bundles)
  - `locales/story/`: {locales_stats['category_counts']['story']} 行 ({locales_stats['category_bundle_counts']['story']} bundles)
  - `locales/birth/`: {locales_stats['category_counts']['birth']} 行 ({locales_stats['category_bundle_counts']['birth']} bundles)
  - `locales/dialogue/`: {locales_stats['category_counts']['dialogue']} 行 ({locales_stats['category_bundle_counts']['dialogue']} bundles)
- **翻译覆盖度**:
  - `accepted` (已采纳正式译文): {locales_stats['status_counts']['accepted']}
  - `pending` (待人工审校机翻初稿): {locales_stats['status_counts']['pending']}
  - `untranslated` (待翻译空槽): {locales_stats['status_counts']['untranslated']}
- **控制符清洗**:
  - 自动拦截/转义保留控制符 `|` 与 `^` 共计 {locales_stats['sanitized_delimiter_rows']} 行。
- **歌词与曲目**:
  - {lyrics_counts['total_songs']} 首歌曲分轨，{lyrics_counts['total_slots']} 个歌词槽，已翻译 {lyrics_counts['translated_slots']} 槽（纯英文保留不翻 {lyrics_counts.get('english_bypass_slots', 0)} 槽）。
- **贴图 Manifest**:
  - {images_counts['total_images']} 张贴图索引，支持 GitHub Release Assets / Cloudflare R2。
- **名词表**:
  - 90 个项目固定术语 + 52 名偶像名录。
{f'''## 2. APK 内置面（独立仓库）

- 输出目录: `{builtin_out_dir}`
- 表面数: {builtin_counts.get('surfaces')}（底栏图集 {builtin_counts.get('bottom_bar_slots')} 槽 + 运行时 BI 文案 + CJK 字体）
- 交付通道: `assets/bin/Data/data.unity3d`，assets 服务器不路由。

''' if builtin_out_dir is not None else ''}## 3. 验证与后续使用
- 目录结构完全独立且干净，可直接在 `{out_dir}` 下执行 `git init && git add . && git commit -m "feat: initial localization repository" && git push` 推送至 GitHub。
"""
    (out_dir / "HANDOFF.md").write_text(handoff_text, encoding="utf-8")

    return summary


def find_default_queue() -> Path:
    """Find default queue path checking standard locations."""
    candidates = [
        Path("build/localization-90200/all-jp-catalogue.jsonl"),
        Path("build/localization-90200/unresolved-queue.jsonl"),
        Path("build/runs/text-localization/9.0.200/nas-pipeline-readiness-main-20260925/latest-9.0.200-1077500/delta-extraction/unresolved-queue.jsonl"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=None, help="Path to unresolved-queue.jsonl")
    parser.add_argument("--base-version", default="9.0.200+1077500", help="Base version tag (default: 9.0.200+1077500)")
    parser.add_argument(
        "--legacy-translations",
        type=Path,
        default=Path("build/runs/text-localization/9.0.200/localization-full-surface-closeout-20260926/t2s-corrected-overlay/legacy-zh-opencc-t2s-owner-waived.jsonl"),
        help="Path to accepted legacy translations JSONL",
    )
    parser.add_argument(
        "--machine-translations",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.jsonl"),
        help="Path to machine translations pool JSONL",
    )
    parser.add_argument(
        "--authoritative-terms",
        type=Path,
        default=Path("localization/quality/authoritative-terms.json"),
        help="Path to authoritative terms JSON",
    )
    parser.add_argument(
        "--character-voice",
        type=Path,
        default=Path("build/localization-90200/character-voice-evidence.json"),
        help="Path to character voice evidence JSON",
    )
    parser.add_argument(
        "--derived-idols",
        type=Path,
        default=Path("build/runs/text-localization/9.0.200/image-terminology-unblock-20260926/derived-idol-names.json"),
        help="Path to derived idol names JSON",
    )
    parser.add_argument(
        "--lyrics-manifest",
        type=Path,
        default=Path("build/runs/text-localization/9.0.200/scrobj-lyrics-full-20260924/bilingual-manifest-v6.json"),
        help="Path to lyrics bilingual manifest JSON",
    )
    parser.add_argument(
        "--lyrics-slots",
        type=Path,
        default=Path("build/runs/text-localization/9.0.200/scrobj-lyrics-full-20260924/lyrics-only-slots.jsonl"),
        help="Path to lyrics only slots JSONL",
    )
    parser.add_argument(
        "--lyrics-translations",
        type=Path,
        default=(
            Path("build/runs/text-localization/9.0.200/scrobj-lyrics-full-20260924/lyrics-only-translations-v6-display.jsonl")
            if Path("build/runs/text-localization/9.0.200/scrobj-lyrics-full-20260924/lyrics-only-translations-v6-display.jsonl").is_file()
            else Path("build/runs/text-localization/9.0.200/scrobj-lyrics-full-20260924/lyrics-only-translations-v6.jsonl")
        ),
        help="Path to lyrics translations JSONL",
    )
    parser.add_argument(
        "--texture-manifest",
        type=Path,
        default=Path("work/agents/image-localization/reviewed937-texture-stage/texture-install-manifest.jsonl"),
        help="Path to 937 textures install manifest JSONL",
    )
    parser.add_argument(
        "--bottom-bar-manifest",
        type=Path,
        default=Path("work/agents/client/bottom-nav-text-fix-20260924/manifest-both-generated-v3.json"),
        help="Path to bottom bar manifest JSON",
    )
    parser.add_argument(
        "--bottom-bar-strings",
        type=Path,
        default=Path("client/apk-ui-strings-zhcn.json"),
        help="Path to bottom bar strings JSON",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("build/runs/text-localization/9.0.200/github-export-candidate"),
        help="Output directory for the asset-server localization repository candidate",
    )
    parser.add_argument(
        "--builtin-out-dir",
        type=Path,
        default=None,
        help=(
            "Output directory for the APK built-in repository candidate "
            "(bottom-bar atlas index + apk-builtin manifest). Omit to skip."
        ),
    )
    parser.add_argument(
        "--apk-bi-verification",
        type=Path,
        default=Path(
            "build/runs/client/9.0.200/apk-bi-font-candidate-verification-20260924/verification-summary.json"
        ),
        help="Path to the APK BI + font candidate verification summary",
    )
    parser.add_argument(
        "--no-machine-drafts",
        action="store_true",
        help="Do not include machine translation drafts as pending (leave untranslated)",
    )
    parser.add_argument(
        "--no-bypass-english-lyrics",
        action="store_true",
        help="Do not bypass English lyrics (translate even pure English lines)",
    )
    parser.add_argument(
        "--timestamp",
        default="2026-09-27T00:00:00Z",
        help="Timestamp string to attach to entries",
    )

    args = parser.parse_args(argv)

    queue = args.queue
    if queue is None:
        queue = find_default_queue()

    if not queue.is_file():
        parser.error(f"Queue file not found: {queue}")

    legacy_inputs = [args.legacy_translations]
    fallback_legacy = Path("build/localization-90200/legacy-zh-translations.jsonl")
    if fallback_legacy.is_file() and fallback_legacy not in legacy_inputs:
        legacy_inputs.append(fallback_legacy)

    machine_inputs = [args.machine_translations]
    for extra_m in [
        Path("build/localization-90200/machine-translations-codex.jsonl"),
        Path("build/localization-90200/machine-translations-nongtx-api.jsonl"),
    ]:
        if extra_m.is_file() and extra_m not in machine_inputs:
            machine_inputs.append(extra_m)

    summary = export_all(
        queue_path=queue,
        legacy_path=legacy_inputs,
        machine_path=machine_inputs,
        terms_path=args.authoritative_terms,
        character_voice_path=args.character_voice,
        derived_idols_path=args.derived_idols,
        lyrics_manifest_path=args.lyrics_manifest,
        lyrics_slots_path=args.lyrics_slots,
        lyrics_translations_path=args.lyrics_translations,
        texture_manifest_path=args.texture_manifest,
        bottom_bar_manifest_path=args.bottom_bar_manifest,
        bottom_bar_strings_path=args.bottom_bar_strings,
        out_dir=args.out_dir,
        apk_bi_verification_path=args.apk_bi_verification,
        builtin_out_dir=args.builtin_out_dir,
        base_version=args.base_version,
        include_machine_drafts=not args.no_machine_drafts,
        bypass_english_lyrics=not args.no_bypass_english_lyrics,
        timestamp=args.timestamp,
    )

    print("\n" + "=" * 60)
    print("Export successfully completed!")
    print(f"Output directory: {args.out_dir}")
    print(f"Total rows: {summary['locales']['total_rows']}")
    print(f"Accepted: {summary['locales']['status_counts']['accepted']}")
    print(f"Pending: {summary['locales']['status_counts']['pending']}")
    print(f"Untranslated: {summary['locales']['status_counts']['untranslated']}")
    print(f"Categories: {summary['locales']['category_counts']}")
    print(f"Songs: {summary['lyrics']['total_songs']}")
    print(f"Images in manifest: {summary['images']['total_images']}")
    if summary.get("builtin_out_dir"):
        print(f"APK built-in directory: {summary['builtin_out_dir']}")
        print(f"Built-in surfaces: {summary['builtin']['surfaces']}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
