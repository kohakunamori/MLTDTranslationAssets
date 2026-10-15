"""Reuse published image bytes only after verifying the current official source.

No provider, PNG processing, texture injection or old catalogue is involved.
The current catalogue supplies the route and size; the official bundle's SHA-256
must equal the source bound to the most recent published image for that logical.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re

from pipelines.core.hashing import sha256_file

HEX64 = re.compile(r"[0-9a-f]{64}")
PREFIX = "production/2018/Android/"


def published_candidates(store, asset_version: str) -> dict[str, tuple[str, dict]]:
    """A text-only release must not hide images in an older successful release."""
    candidates = {}
    versions = sorted((v for v in store.list_releases()
                       if v.isdigit() and int(v) <= int(asset_version)), key=int, reverse=True)
    for version in versions:
        manifest = store.load_manifest(version)
        if manifest.get("build_status") != "success":
            continue
        if str(manifest.get("asset_version")) != version:
            raise ValueError(f"published image release version mismatch: {version}")
        seen = set()
        for entry in manifest.get("entries", []):
            if entry.get("resource_kind") != "texture":
                continue
            logical = entry.get("logical_key")
            if (not isinstance(logical, str) or not logical.endswith(".unity3d")
                    or "/" in logical or "\\" in logical or logical in seen
                    or entry.get("translation_status") != "modified"
                    or any(not HEX64.fullmatch(str(entry.get(key, ""))) for key in
                           ("source_sha256", "translated_sha256", "artifact_sha256"))):
                raise ValueError(f"invalid published image entry in {version}: {logical!r}")
            seen.add(logical)
            candidates.setdefault(logical, (version, entry))
    return candidates


def reuse(*, store, version: dict, index: dict, work: Path,
          source_client_version: str, downloader, cache_root: Path | None = None,
          workers: int = 8) -> tuple[list[dict], dict]:
    candidates = published_candidates(store, str(version["asset_version"]))
    if not candidates:
        raise ValueError("image publication requested but no previously published image bundles exist")

    def verify(item):
        logical, (origin, old) = item
        current = index.get(logical)
        if current is None:
            return None, "absent_from_current_catalogue", origin, logical
        remote = str(current["remote"])
        if (not remote.endswith(".unity3d") or not remote.isascii()
                or "/" in remote or "\\" in remote):
            raise ValueError(f"unsafe current image route: {remote!r}")
        artifact = store.object_file(old["artifact_sha256"])
        if artifact is None or sha256_file(artifact) != old["artifact_sha256"]:
            raise ValueError(f"published image artifact missing or corrupt: {logical}")
        source = work / "image-sources" / remote
        downloader(f"{version['asset_root']}/{remote}", source, current["declared_size"],
                   content_key=f"images/{logical}/{current['catalog_hash']}", cache_root=cache_root)
        if source.stat().st_size != current["declared_size"]:
            raise ValueError(f"current official image size mismatch: {logical}")
        if sha256_file(source) != old["source_sha256"]:
            return None, "official_source_changed", origin, logical
        return {
            "logical_key": logical, "logical_path": PREFIX + logical,
            "runtime_path": PREFIX + remote, "resource_kind": "texture",
            "channel": "assets", "asset_version": str(version["asset_version"]),
            "client_version": None, "source_client_version": source_client_version,
            "source_sha256": old["source_sha256"],
            "translated_sha256": old["translated_sha256"],
            "artifact_sha256": old["artifact_sha256"],
            "artifact_file": str(artifact.resolve()),
            "reuse_status": "exact", "translation_status": "modified",
        }, "reused", origin, logical

    entries = []
    reasons = Counter()
    origins = Counter()
    skipped = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for entry, reason, origin, logical in pool.map(verify, sorted(candidates.items())):
            reasons[reason] += 1
            if entry is None:
                skipped.append({"logical": logical, "reason": reason})
            else:
                entries.append(entry)
                origins[origin] += 1
    report = {"mode": "reuse_published", "candidates": len(candidates),
              "reused": len(entries), "source_versions": dict(origins),
              "outcomes": dict(reasons), "skipped": skipped}
    return entries, report
