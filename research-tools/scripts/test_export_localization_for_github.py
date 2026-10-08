"""Unit tests for MLTD localization GitHub repository export pipeline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from scripts.export_localization_for_github import (
    CATEGORIES,
    ExportValidationError,
    build_idols_roster,
    classify_bundle,
    clean_translation,
    export_all,
    export_apk_builtin_manifest,
    export_bottom_bar_manifest,
    export_images_manifest,
    export_locales,
    export_lyrics,
    load_accepted_translations,
    load_authoritative_terms,
    load_machine_translations,
    main,
    sha256_text,
)


def test_classify_bundle_all_categories() -> None:
    # 1. Story
    assert classify_bundle("event_0448_story_01_jp.gtx") == "story"
    assert classify_bundle("event_0448_story_chat_jp.gtx") == "story"
    assert classify_bundle("special_528_rs_01_jp.gtx") == "story"
    assert classify_bundle("main_story_chapter_01.gtx") == "story"
    assert classify_bundle("blog_story_001.gtx") == "story"

    # 2. Card
    assert classify_bundle("card_episode_011ami0574_jp.gtx") == "card"
    assert classify_bundle("card_blst_026yur0754_jp.gtx") == "card"
    assert classify_bundle("card_message_001.gtx") == "card"
    assert classify_bundle("ch_card_burst_01.gtx") == "card"

    # 3. Dialogue
    assert classify_bundle("liveresult_011ami_003_3000_jp.gtx") == "dialogue"
    assert classify_bundle("lbonus_0119_special_jp.gtx") == "dialogue"
    assert classify_bundle("talk_lobby_001.gtx") == "dialogue"
    assert classify_bundle("theater_greeting_morning_01.gtx") == "dialogue"
    assert classify_bundle("touch_reaction_01.gtx") == "dialogue"
    assert classify_bundle("job_text_001har_1000.gtx") == "dialogue"
    assert classify_bundle("adv_commu_001.gtx") == "dialogue"

    # 4. Birth
    assert classify_bundle("birth_bdl_047sub_010_jp.gtx") == "birth"
    assert classify_bundle("birth_bdl2_015siz_010_jp.gtx") == "birth"
    assert classify_bundle("birth_ent_047sub_010_jp.gtx") == "birth"
    assert classify_bundle("birth_p_001har_001_jp.gtx") == "birth"

    # 5. Master / System
    assert classify_bundle("MD_jp.gtx") == "master"
    assert classify_bundle("CM_jp.gtx") == "master"
    assert classify_bundle("CD_jp.gtx") == "master"
    assert classify_bundle("MB_jp.gtx") == "master"
    assert classify_bundle("ST_jp.gtx") == "master"
    assert classify_bundle("bi_jp.gtx") == "master"
    assert classify_bundle("igp_jp.gtx") == "master"


def test_clean_translation_sanitizes_reserved_delimiters() -> None:
    # Safe text remains unchanged
    clean_text, issues = clean_translation("制作人，辛苦了！")
    assert clean_text == "制作人，辛苦了！"
    assert issues == []

    # '|' is intercepted and converted to '｜' (fullwidth)
    dirty_pipe, issues_pipe = clean_translation("这是选项A|这是选项B")
    assert dirty_pipe == "这是选项A｜这是选项B"
    assert "|" not in dirty_pipe
    assert len(issues_pipe) == 1
    assert "Replaced 1 '|'" in issues_pipe[0]

    # '^' is intercepted and converted to '＾' (fullwidth)
    dirty_hat, issues_hat = clean_translation("第一行^第二行")
    assert dirty_hat == "第一行＾第二行"
    assert "^" not in dirty_hat
    assert len(issues_hat) == 1
    assert "Replaced 1 '^'" in issues_hat[0]

    # Multiple delimiters in one text
    dirty_multi, issues_multi = clean_translation("A|B^C|D")
    assert dirty_multi == "A｜B＾C｜D"
    assert "|" not in dirty_multi
    assert "^" not in dirty_multi
    assert len(issues_multi) == 2


def test_idols_roster_spec_and_rulings() -> None:
    doc = build_idols_roster()
    assert doc["counts"]["total_standard_idols"] == 52
    assert doc["counts"]["allstars"] == 13
    assert doc["counts"]["theater"] == 39
    assert len(doc["idols"]) == 52

    idols_by_code = {item["speaker_code"]: item for item in doc["idols"]}

    # Verify Owner Rulings (2026-09-26)
    emi = idols_by_code["032emi"]
    assert emi["name_zh"] == "艾米莉·斯图亚特"
    assert emi["provenance"] == "owner_ruling_2026-09-26"

    karen = idols_by_code["045kar"]
    assert karen["name_zh"] == "篠宫可怜"
    assert karen["provenance"] == "owner_ruling_2026-09-26"

    tsumugi = idols_by_code["051tmg"]
    assert tsumugi["name_zh"] == "白石䌷"
    assert tsumugi["provenance"] == "owner_ruling_2026-09-26"

    # Verify Allstars and Theater boundaries
    haruka = idols_by_code["001har"]
    assert haruka["division"] == "765PRO Allstars"
    assert haruka["type"] == "Princess"

    mirai = idols_by_code["014mir"]
    assert mirai["division"] == "765PRO Theater"
    assert mirai["type"] == "Princess"

    kaori = idols_by_code["052kao"]
    assert kaori["name_zh"] == "樱守歌织"
    assert kaori["division"] == "765PRO Theater"
    assert kaori["type"] == "Angel"


def test_source_sha256_mismatch_raises_validation_error(tmp_path: Path) -> None:
    queue_file = tmp_path / "queue_mismatch.jsonl"
    bad_row = {
        "bundle": "event_0448_story_01_jp.gtx",
        "key": "msg_001",
        "source": "プロデューサー、お疲れ様です！",
        "source_sha256": "0" * 64,  # intentionally invalid hash
        "translation": "",
        "status": "pending",
    }
    queue_file.write_text(json.dumps(bad_row, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(ExportValidationError, match="source_sha256 mismatch"):
        export_locales(
            queue_path=queue_file,
            out_dir=tmp_path / "out",
            asset_version="1077100",
            source_client_version="9.0.200",
            legacy_exact={},
            legacy_sha={},
            machine_sha={},
            authoritative_terms={},
            include_machine_drafts=True,
            timestamp="2026-09-27T00:00:00Z",
        )


def test_export_locales_resolution_hierarchy_and_schema(tmp_path: Path) -> None:
    # Setup test queue with diverse cases
    src_exact = "本領発揮"
    src_memory = "受け取る"
    src_term = "ゴールドランカー"
    src_machine = "明日はフェスです"
    src_untrans = "まだ誰も訳していないテキスト"
    src_delim = "選択肢A|選択肢B"

    rows = [
        {"bundle": "event_0448_story_01_jp.gtx", "key": "k_exact", "source": src_exact, "source_sha256": sha256_text(src_exact)},
        {"bundle": "birth_bdl_047sub_010_jp.gtx", "key": "k_mem", "source": src_memory, "source_sha256": sha256_text(src_memory)},
        {"bundle": "CM_jp.gtx", "key": "k_term", "source": src_term, "source_sha256": sha256_text(src_term)},
        {"bundle": "card_episode_011ami0574_jp.gtx", "key": "k_mach", "source": src_machine, "source_sha256": sha256_text(src_machine)},
        {"bundle": "MD_jp.gtx", "key": "k_untrans", "source": src_untrans, "source_sha256": sha256_text(src_untrans)},
        {"bundle": "liveresult_011ami_003_3000_jp.gtx", "key": "k_delim", "source": src_delim, "source_sha256": sha256_text(src_delim)},
    ]

    queue_path = tmp_path / "queue.jsonl"
    with queue_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    legacy_exact = {("event_0448_story_01_jp.gtx", "k_exact"): "大显身手"}
    legacy_sha = {sha256_text(src_memory): "领取"}
    machine_sha = {sha256_text(src_machine): "明天是演出"}
    authoritative_terms = {src_term: "黄金排名"}
    # For delimiter test, put a translation with '|'
    legacy_exact[("liveresult_011ami_003_3000_jp.gtx", "k_delim")] = "选项A|选项B"

    out_dir = tmp_path / "export_test"
    stats = export_locales(
        queue_path=queue_path,
        out_dir=out_dir,
        asset_version="1077100",
        source_client_version="9.0.200",
        legacy_exact=legacy_exact,
        legacy_sha=legacy_sha,
        machine_sha=machine_sha,
        authoritative_terms=authoritative_terms,
        include_machine_drafts=True,
        timestamp="2026-09-27T00:00:00Z",
    )

    assert stats["total_rows"] == 6
    assert stats["status_counts"]["accepted"] == 5  # exact, mem, term, machine draft, delim(cleaned)
    assert stats["status_counts"]["pending"] == 0
    assert stats["status_counts"]["untranslated"] == 1
    assert stats["sanitized_delimiter_rows"] == 1

    # Verify category file creation and content
    story_file = out_dir / "locales" / "story" / "event_0448_story_01_jp.gtx.jsonl"
    assert story_file.is_file()
    entry_exact = json.loads(story_file.read_text(encoding="utf-8").strip())
    # Independent axes: assets identity is digits-only, client_version is null,
    # and the client version survives only as provenance.
    assert entry_exact["channel"] == "assets"
    assert entry_exact["asset_version"] == "1077100"
    assert entry_exact["client_version"] is None
    assert entry_exact["source_client_version"] == "9.0.200"
    assert "base_version" not in entry_exact
    assert entry_exact["bundle"] == "event_0448_story_01_jp.gtx"
    assert entry_exact["item_key"] == "k_exact"
    assert entry_exact["source_sha256"] == sha256_text(src_exact)
    assert entry_exact["ja"] == src_exact
    assert entry_exact["zh"] == "大显身手"
    assert entry_exact["status"] == "accepted"

    # Verify delimiter was cleaned in dialogue file
    dialogue_file = out_dir / "locales" / "dialogue" / "liveresult_011ami_003_3000_jp.gtx.jsonl"
    assert dialogue_file.is_file()
    entry_delim = json.loads(dialogue_file.read_text(encoding="utf-8").strip())
    assert "|" not in entry_delim["zh"]
    assert "选项A｜选项B" == entry_delim["zh"]

    # Verify untranslated item has zh == ""
    master_file = out_dir / "locales" / "master" / "MD_jp.gtx.jsonl"
    assert master_file.is_file()
    entry_untrans = json.loads(master_file.read_text(encoding="utf-8").strip())
    assert entry_untrans["zh"] == ""
    assert entry_untrans["status"] == "untranslated"


def test_composite_versions_are_rejected(tmp_path: Path) -> None:
    """The Client and Assets axes must never be spliced into one identity string."""
    queue_file = tmp_path / "queue.jsonl"
    queue_file.write_text(
        json.dumps(
            {
                "bundle": "event_0448_story_01_jp.gtx",
                "key": "k_axis",
                "source": "本領発揮",
                "source_sha256": sha256_text("本領発揮"),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    common = dict(
        queue_path=queue_file,
        out_dir=tmp_path / "out",
        legacy_exact={},
        legacy_sha={},
        machine_sha={},
        authoritative_terms={},
        include_machine_drafts=False,
        timestamp="2026-09-27T00:00:00Z",
    )
    for bad_asset in ("9.0.200+1077100", "client-9.0.200-assets-1077100", "1077100+9.0.200", "assets-1077100"):
        with pytest.raises(ExportValidationError, match="asset_version"):
            export_locales(asset_version=bad_asset, source_client_version="9.0.200", **common)
    for bad_client in ("9.0.200+1077100", "9.0.200-arm64", "9.0", ""):
        with pytest.raises(ExportValidationError, match="source_client_version"):
            export_locales(asset_version="1077100", source_client_version=bad_client, **common)


def test_export_lyrics_bilingual_alignment(tmp_path: Path) -> None:
    slots_file = tmp_path / "lyrics_slots.jsonl"
    translations_file = tmp_path / "lyrics_trans.jsonl"
    manifest_file = tmp_path / "lyrics_manifest.json"

    s1 = "Make me happy いつだって"
    s2 = "今日は踊ろう"
    sha1 = sha256_text(s1)
    sha2 = sha256_text(s2)

    slots_data = [
        {"logical": "scrobj_song01.unity3d", "index": 0, "tick": 1000, "absTime": 1.0, "source": s1, "source_sha256": sha1},
        {"logical": "scrobj_song01.unity3d", "index": 1, "tick": 2000, "absTime": 2.0, "source": s2, "source_sha256": sha2},
    ]
    with slots_file.open("w", encoding="utf-8") as f:
        for r in slots_data:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    trans_data = [
        {"source_sha256": sha1, "translation": "Make me happy 无论何时"},
        {"source_sha256": sha2, "translation": "今天尽情跳舞吧"},
    ]
    with translations_file.open("w", encoding="utf-8") as f:
        for r in trans_data:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    out_dir = tmp_path / "export_lyrics_out"
    counts = export_lyrics(
        manifest_path=manifest_file,
        slots_path=slots_file,
        translations_path=translations_file,
        out_dir=out_dir,
        timestamp="2026-09-27T00:00:00Z",
    )

    assert counts["total_songs"] == 1
    assert counts["total_slots"] == 2
    assert counts["translated_slots"] == 2

    song_file = out_dir / "lyrics" / "songs" / "scrobj_song01.unity3d.jsonl"
    assert song_file.is_file()
    lines = [json.loads(line) for line in song_file.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2
    assert lines[0]["ja"] == s1
    assert lines[0]["zh"] == "Make me happy 无论何时"
    assert lines[0]["status"] == "accepted"


def test_export_images_manifest_structure(tmp_path: Path) -> None:
    texture_manifest = tmp_path / "textures.jsonl"
    tex_rows = [
        {
            "bundle": "event_0015_info.unity3d",
            "texture_path_id": 1001,
            "original_png": "original/event_0015_info/1001_info.png",
            "original_png_sha256": "a" * 64,
            "restored_png_sha256": "b" * 64,
            "original_size": [512, 512],
            "review_status": "accepted",
        }
    ]
    with texture_manifest.open("w", encoding="utf-8") as f:
        for r in tex_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    out_dir = tmp_path / "export_manifest_out"

    counts = export_images_manifest(
        texture_manifest_path=texture_manifest,
        out_dir=out_dir,
        timestamp="2026-09-27T00:00:00Z",
    )

    # Bottom-bar atlases are APK built-ins, so they no longer inflate this manifest.
    assert counts["total_images"] == 1
    assert counts["reviewed_textures"] == 1
    assert "bottom_bar_textures" not in counts

    manifest_file = out_dir / "manifests" / "images.manifest.json"
    assert manifest_file.is_file()
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert data["counts"]["total_images"] == 1
    assert all(item["kind"] == "ui_texture" for item in data["images"])

    # Check distribution schema
    item0 = data["images"][0]
    assert "distribution" in item0
    assert "github_release" in item0["distribution"]
    assert "cloudflare_r2" in item0["distribution"]
    assert item0["distribution"]["github_release"]["asset_name"].startswith("tex_")


def test_apk_builtin_manifest_indexes_client_only_surfaces(tmp_path: Path) -> None:
    verification = tmp_path / "verification-summary.json"
    verification.write_text(
        json.dumps(
            {
                "client_version": "9.0.200",
                "assets_version": "1077100",
                "UNREVIEWED": True,
                "independent_reviewed": False,
                "verified_artifact": {"bytes": 88571345},
                "checks": {
                    "embedded_data_unity3d_sha256": "f" * 64,
                    "textassets_changed": [{"path_id": 646, "name": "BI_jp.gtx"}],
                    "bi_runtime_records": 632,
                    "bi_runtime_unique_keys": 628,
                    "bi_runtime_values_with_kana": 0,
                    "bi_runtime_values_with_han": 586,
                    "font_object_path_id": 48,
                    "font_object_name": "TUDShinGoPR6-Regular",
                    "font_object_sha256": "d" * 64,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out_dir = tmp_path / "builtin_out"
    counts = export_apk_builtin_manifest(verification, out_dir, "2026-09-27T00:00:00Z")
    assert counts["surfaces"] == 3

    doc = json.loads((out_dir / "manifests" / "apk-builtin.manifest.json").read_text(encoding="utf-8"))
    names = [s["name"] for s in doc["surfaces"]]
    assert names == ["bottom-bar-atlas", "runtime-bi", "cjk-font"]
    # The manifest must not imply a release.
    assert doc["provenance"]["artifact_status"] == "unreviewed_candidate"
    assert doc["provenance"]["reviewed"] is False

    bi = next(s for s in doc["surfaces"] if s["name"] == "runtime-bi")
    assert bi["textasset"] == "BI_jp.gtx"
    assert bi["target"] == "data.unity3d:646"


def test_apk_builtin_manifest_without_verification_report(tmp_path: Path) -> None:
    out_dir = tmp_path / "builtin_empty"
    counts = export_apk_builtin_manifest(Path(""), out_dir, "2026-09-27T00:00:00Z")
    assert counts["surfaces"] == 1  # the bottom-bar atlas index alone
    doc = json.loads((out_dir / "manifests" / "apk-builtin.manifest.json").read_text(encoding="utf-8"))
    assert doc["provenance"]["artifact_status"] == "unrecorded"


def test_bottom_bar_manifest_export(tmp_path: Path) -> None:
    out_dir = tmp_path / "bb_out"
    strings_file = tmp_path / "apk-ui-strings.json"
    strings_data = {
        "surfaces": {
            "bottom-bar-footer": {
                "slots": [
                    {"index": 0, "ja": "劇場", "zh": "剧场", "provenance": "project_decision"},
                    {"index": 1, "ja": "アイドル", "zh": "偶像", "provenance": "shared_glossary"},
                ]
            }
        }
    }
    strings_file.write_text(json.dumps(strings_data, ensure_ascii=False), encoding="utf-8")

    res = export_bottom_bar_manifest(strings_file, out_dir, "2026-09-27T00:00:00Z")
    assert res["slot_count"] == 2

    bb_file = out_dir / "manifests" / "bottom-bar.manifest.json"
    assert bb_file.is_file()
    doc = json.loads(bb_file.read_text(encoding="utf-8"))
    assert len(doc["slots"]) == 2
    assert doc["slots"][0]["zh"] == "剧场"


def test_protected_token_quarantine_demotes_accepted_rows(tmp_path: Path) -> None:
    """A translation whose protected tokens do not match the source must not
    leave the exporter as `accepted`: the public Assets gate refuses such rows
    (PR #5) and generation treats them as unsafe."""
    bs = chr(92)  # a literal backslash; control codes are ``\NN\`` text
    src_ok = "{$P$}、見て！"                        # placeholder kept
    src_missing = "{$P$}。せっかくの機会です。"      # placeholder dropped in zh
    src_codes = f"わんだほー{bs}01{bs}なのです{bs}17{bs}"  # control code dropped in zh
    zh_ok = "{$P$}，你看！"
    zh_missing = "难得的机会。"                       # {$P$} lost
    zh_codes = f"真是太美妙了唷{bs}17{bs}"             # \01\ lost

    rows = [
        {"bundle": "event_0448_story_01_jp.gtx", "key": "k_ok",
         "source": src_ok, "source_sha256": sha256_text(src_ok)},
        {"bundle": "CM_jp.gtx", "key": "k_missing",
         "source": src_missing, "source_sha256": sha256_text(src_missing)},
        {"bundle": "MB_jp.gtx", "key": "k_codes",
         "source": src_codes, "source_sha256": sha256_text(src_codes)},
    ]
    queue_path = tmp_path / "queue.jsonl"
    with queue_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Identity is casefolded as (bundle.casefold(), item_key) in export_locales.
    legacy_exact = {
        ("event_0448_story_01_jp.gtx", "k_ok"): zh_ok,
        ("cm_jp.gtx", "k_missing"): zh_missing,
        ("mb_jp.gtx", "k_codes"): zh_codes,
    }
    out_dir = tmp_path / "quarantine_out"
    stats = export_locales(
        queue_path=queue_path,
        out_dir=out_dir,
        asset_version="1077100",
        source_client_version="9.0.200",
        legacy_exact=legacy_exact,
        legacy_sha={},
        machine_sha={},
        authoritative_terms={},
        include_machine_drafts=True,
        timestamp="2026-09-27T00:00:00Z",
    )

    assert stats["protected_token_quarantined"] == 2
    assert stats["status_counts"]["accepted"] == 1
    assert stats["status_counts"]["pending"] == 2

    def read_entry(category: str, bundle: str) -> Dict[str, Any]:
        path = out_dir / "locales" / category / f"{bundle}.jsonl"
        return json.loads(path.read_text(encoding="utf-8").strip())

    assert read_entry("story", "event_0448_story_01_jp.gtx")["status"] == "accepted"
    demoted = read_entry("master", "CM_jp.gtx")
    assert demoted["status"] == "pending"
    assert demoted["zh"] == zh_missing          # translation preserved for review
    assert read_entry("master", "MB_jp.gtx")["status"] == "pending"

    sample_keys = {sample["item_key"] for sample in stats["protected_token_samples"]}
    assert sample_keys == {"k_missing", "k_codes"}
