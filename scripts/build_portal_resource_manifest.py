#!/usr/bin/env python3
"""Build the immutable resource summary consumed by the translation portal.

The Assets repository is the source of truth for release identity and source
inventory.  D1 remains the mutable review/session store; it must not be the
only place from which the portal learns that a new Assets release exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


CATEGORY_RULES = {
    "lyrics": {"domain": "lyrics", "name": "全曲打歌歌词", "description": "按正式 Assets 曲目束逐行对照与翻译", "icon": "🎵", "unit": "句", "entry": "lyrics"},
    "event_chat": {"domain": "story", "name": "活动短信与聊天", "description": "制作人与偶像活动手机联络", "icon": "📱", "unit": "句", "entry": "studio"},
    "event_story": {"domain": "story", "name": "活动剧情篇章", "description": "巡回与剧场活动全篇章节故事", "icon": "🌟", "unit": "句", "entry": "studio"},
    "special_commu": {"domain": "story", "name": "特别企划与回想", "description": "特别活动、周年企划与回忆录", "icon": "🎭", "unit": "句", "entry": "studio"},
    "main_commu": {"domain": "story", "name": "主线剧情故事", "description": "偶像个人主线剧情与剧场篇章", "icon": "📖", "unit": "句", "entry": "studio"},
    "card_episode": {"domain": "card", "name": "卡片专属觉醒物语", "description": "SSR/SR 卡片觉醒物语与专属剧情", "icon": "🎴", "unit": "句", "entry": "studio"},
    "card_blog": {"domain": "card", "name": "偶像博客与私信", "description": "卡片获得后的剧场博客与短信", "icon": "💌", "unit": "句", "entry": "studio"},
    "card_skill": {"domain": "card", "name": "卡片技能与卡面档案", "description": "队长技、演出技能与专属介绍", "icon": "⚔️", "unit": "句", "entry": "studio"},
    "theater_comm": {"domain": "dialogue", "name": "剧场工作互动对话", "description": "事务所各房间触碰与日常工作台词", "icon": "🏢", "unit": "句", "entry": "studio"},
    "message_board": {"domain": "dialogue", "name": "剧场白板日常留言", "description": "休息室白板留言涂鸦与问候", "icon": "📝", "unit": "句", "entry": "studio"},
    "live_result": {"domain": "dialogue", "name": "演出打歌结算赞誉", "description": "LIVE 完成打气与结算台词", "icon": "🎤", "unit": "句", "entry": "studio"},
    "login_bonus": {"domain": "dialogue", "name": "登录特别演出台词", "description": "签到剧场演出与纪念问候", "icon": "🎁", "unit": "句", "entry": "studio"},
    "birth_live": {"domain": "birth", "name": "生日特别演出剧情", "description": "偶像生日专属 LIVE 演出剧情", "icon": "🎂", "unit": "句", "entry": "studio"},
    "birth_greet": {"domain": "birth", "name": "生日剧场玄关祝贺", "description": "生日当天玄关祝贺与白板留言", "icon": "🎈", "unit": "句", "entry": "studio"},
    "system_ui": {"domain": "system", "name": "系统菜单与玩法规则", "description": "界面底栏、UI 引导、道具与提示弹窗", "icon": "⚙️", "unit": "句", "entry": "studio"},
}


def category_id(bundle: str, item_key: str = "") -> str:
    b, k = bundle.lower(), item_key.lower()
    if b.startswith("scrobj_") or "lyric" in b:
        return "lyrics"
    if b.startswith("event_") and ("chat" in b or "chat" in k):
        return "event_chat"
    if b.startswith("event_") or ("story" in b and not b.startswith("special_")):
        return "event_story"
    if b.startswith("special_"):
        return "special_commu"
    if b == "st_jp.gtx" or b.startswith("st_"):
        return "main_commu"
    if b.startswith("card_episode_"):
        return "card_episode"
    if b.startswith("card_blst_"):
        return "card_blog"
    if b == "cd_jp.gtx" or b.startswith("cd_"):
        return "card_skill"
    if b == "cm_jp.gtx" or b.startswith("cm_"):
        return "theater_comm"
    if b == "mb_jp.gtx" or b.startswith("mb_"):
        return "message_board"
    if b.startswith("liveresult_"):
        return "live_result"
    if b.startswith("lbonus_"):
        return "login_bonus"
    if b.startswith("birth_bdl"):
        return "birth_live"
    if b.startswith("birth_"):
        return "birth_greet"
    return "system_ui"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(root: Path) -> str | None:
    value = os.environ.get("GITHUB_SHA", "").strip()
    if value:
        return value
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def status_bucket(row: dict) -> str:
    status = str(row.get("status") or "").strip().lower()
    if status == "accepted" and row.get("zh"):
        return "translated"
    if status in {"pending", "needs_review"} and row.get("zh"):
        return "pending"
    return "untranslated"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("manifests/portal-resource-manifest.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    generated_path = (root / args.generated_manifest).resolve() if not args.generated_manifest.is_absolute() else args.generated_manifest
    asset_doc = json.loads((root / "manifests/asset-version.json").read_text(encoding="utf-8"))
    asset_version = str(asset_doc["asset_version"])
    if str(json.loads(generated_path.read_text(encoding="utf-8"))["asset_version"]) != asset_version:
        raise SystemExit("generated manifest and asset-version.json disagree")

    totals = {"total": 0, "translated": 0, "pending": 0, "untranslated": 0, "reused": 0, "suggested": 0, "blocked": 0}
    categories: dict[str, dict] = {}
    domains: dict[str, dict] = {}
    source_hash = hashlib.sha256()
    for path in sorted((root / "locales").rglob("*.jsonl")):
        source_hash.update(path.relative_to(root).as_posix().encode("utf-8"))
        source_hash.update(path.read_bytes())
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"{path}:{line_number}: invalid JSON") from exc
                bundle = str(row.get("bundle") or path.stem).strip()
                item_key = str(row.get("item_key") or "").strip()
                if not bundle or not item_key or not isinstance(row.get("ja"), str):
                    continue
                bucket = status_bucket(row)
                cat_id = category_id(bundle, item_key)
                meta = CATEGORY_RULES[cat_id]
                cat = categories.setdefault(cat_id, {"id": cat_id, **meta, **{k: 0 for k in totals if k != "total"}, "total": 0, "bundles": {}})
                dom = domains.setdefault(meta["domain"], {"id": meta["domain"], "total": 0, "translated": 0, "pending": 0, "untranslated": 0})
                totals["total"] += 1
                totals[bucket] += 1
                cat["total"] += 1
                cat[bucket] += 1
                dom["total"] += 1
                dom[bucket] += 1
                bundle_counts = cat["bundles"].setdefault(bundle, {"total": 0, "translated": 0, "pending": 0, "untranslated": 0})
                bundle_counts["total"] += 1
                bundle_counts[bucket] += 1

    generated = json.loads(generated_path.read_text(encoding="utf-8"))
    generated_hash = sha256(generated_path)
    release = {
        "asset_version": asset_version,
        "release_id": f"assets-{asset_version}",
        "server_schema_version": "v1",
        "status": "published",
        "source_manifest_sha256": generated_hash,
        "assets_commit": git_commit(root),
        "updated_at": asset_doc.get("remote_updated_at") or asset_doc.get("last_synced_at"),
    }
    output = {
        "schema": "mltd.portal.resource-manifest/v1",
        "kind": "assets",
        "generated_at": asset_doc.get("last_synced_at") or datetime.now(timezone.utc).isoformat(),
        "summary_ready": True,
        "release": release,
        "totals": totals,
        "domains": sorted(domains.values(), key=lambda item: item["id"]),
        "categories": sorted(categories.values(), key=lambda item: item["id"]),
        "source": {
            "summary": "github-assets-repository",
            "repository": "kohakunamori/MLTDTranslationAssets",
            "commit": git_commit(root),
            "source_catalogue_sha256": source_hash.hexdigest(),
            "generated_manifest_sha256": generated_hash,
            "generated_entry_count": int(generated.get("entry_count") or len(generated.get("entries") or [])),
        },
    }
    target = (root / args.output).resolve() if not args.output.is_absolute() else args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(target), "asset_version": asset_version, "total": totals["total"], "categories": len(categories)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
