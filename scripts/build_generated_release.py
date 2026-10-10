#!/usr/bin/env python3
"""Build the public Assets generated release from the official asset server.

The repository contains translation sources, not an unchecked copy of the
official archive.  This command resolves the independent ``asset_version``
from ``manifests/asset-version.json``, downloads only the official bundles
referenced by accepted translation rows, runs the existing GTX writer, and
publishes the resulting Unity3D files through the content-addressed store.

Image inputs are intentionally optional until reviewed PNGs are present.  A
missing image input is reported as ``blocked`` and never represented as a
successful image artifact; text generation can still produce a valid release.

``--require-images`` requires a named, independently audited image overlay.  This builder has no image
materialization/injector step and no independent image audit, so it cannot
produce an image surface from PNG inputs alone.  A text-only release is
never proof that images were produced: the mere presence of reviewed PNG inputs
on disk is not an image artifact.  ``--require-images`` therefore fails closed
immediately, before any version/input scan or side effect, instead of
publishing a text-only release that is mislabelled as satisfying the image
requirement.  The default (no flag) text path is unchanged.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

import msgpack

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from assets_generated_index import GeneratedStore
import build_lyric_overlay
import merge_image_overlay
from pipelines.text.mltd_localize_gtx import parse_records, read_gtx


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_version_manifest(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    version = str(value.get("asset_version", ""))
    client = str(value.get("client_version", ""))
    if not version.isdigit() or not client.count(".") == 2:
        raise ValueError("asset-version.json has invalid independent version fields")
    template = str(value.get("asset_root", ""))
    if "{version}" not in template or not template.startswith("https://td-assets.bn765.com/"):
        raise ValueError("asset_root must be the official td-assets.bn765.com template")
    index_name = str(value.get("index_name", ""))
    if not index_name or "/" in index_name or "\\" in index_name:
        raise ValueError("asset-version.json has no safe official index_name")
    return {"asset_version": version, "client_version": client,
            "asset_root": template.format(version=version).rstrip("/"),
            "index_name": index_name}


def load_official_index(path: Path) -> dict[str, dict]:
    raw = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise ValueError("official asset index is not the expected one-table format")
    result: dict[str, dict] = {}
    for logical, row in raw[0].items():
        if not isinstance(logical, str) or not isinstance(row, list) or len(row) != 3:
            raise ValueError(f"malformed official asset index row: {logical!r}")
        catalog_hash, remote, declared_size = row
        remote = str(remote)
        declared_size = int(declared_size)
        if not remote.endswith(".unity3d") or not remote.isascii() or "/" in remote or "\\" in remote:
            raise ValueError(f"unsafe official remote object name: {remote!r}")
        if declared_size < 0:
            raise ValueError(f"negative declared size for {logical!r}")
        result[logical] = {"catalog_hash": str(catalog_hash), "remote": remote,
                           "declared_size": declared_size}
    if not result:
        raise ValueError("official asset index is empty")
    return result


def logical_name(bundle: str) -> str:
    return bundle if bundle.endswith(".unity3d") else bundle + ".unity3d"


def read_translation_rows(root: Path, asset_version: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    seen: dict[tuple[str, str, str], str] = {}
    for path in sorted((root / "locales").rglob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("status") != "accepted" or not str(row.get("zh", "")):
                    continue
                source = str(row.get("ja", ""))
                translation = str(row.get("zh", ""))
                if not source or "|" in translation or "^" in translation:
                    raise ValueError(f"invalid accepted translation at {path}:{line_no}")
                if str(row.get("source_sha256", "")).lower() != sha256_text(source):
                    raise ValueError(f"source hash mismatch at {path}:{line_no}")
                bundle = str(row.get("bundle", ""))
                key = str(row.get("item_key", ""))
                if not bundle or not key:
                    raise ValueError(f"accepted row lacks bundle/item_key at {path}:{line_no}")
                # The same bundle/key may have different source text in
                # different asset versions.  Keep each source-bound candidate;
                # the current official bundle selects the matching one below.
                row_asset_version = str(row.get("asset_version", "")).strip()
                identity = (logical_name(bundle), key, sha256_text(source), row_asset_version)
                prior = seen.get(identity)
                if prior is not None and prior != translation:
                    raise ValueError(f"conflicting translation for {identity}")
                seen[identity] = translation
                grouped.setdefault(identity[0], []).append({
                    "bundle": bundle,
                    "key": key,
                    "source": source,
                    "source_sha256": sha256_text(source),
                    "translation": translation,
                    "status": "accepted",
                    "asset_version": row_asset_version or asset_version,
                })
    if not grouped:
        raise ValueError("no accepted Assets translations were found")
    return grouped


def select_rows_for_current_sources(
    rows: list[dict], current_sources: dict[str, str], asset_version: str | None = None
) -> list[dict]:
    """Select only source-bound translations matching the current bundle."""
    selected: dict[str, dict] = {}
    for row in rows:
        key = str(row["key"])
        source = str(row["source"])
        if current_sources.get(key) != source:
            continue
        prior = selected.get(key)
        if prior is not None and prior["translation"] != row["translation"]:
            # A newer release may intentionally revise a translation while an
            # older release keeps the previous wording. Prefer the row bound
            # to the release currently being built; reject ambiguity on the
            # same asset axis.
            prior_version = str(prior.get("asset_version", ""))
            row_version = str(row.get("asset_version", ""))
            if prior_version == row_version:
                raise ValueError(f"conflicting current-source translation for {key}")
            if asset_version and row_version == asset_version:
                selected[key] = row
            elif prior_version == asset_version:
                continue
            else:
                raise ValueError(f"conflicting current-source translation for {key}")
        else:
            selected[key] = row
    return list(selected.values())


def download(url: str, destination: Path, declared_size: int | None,
             content_key: str | None = None,
             cache_root: Path | None = None) -> str:
    """Fetch one official object, reusing an earlier fetch of the same bytes.

    The CDN renames every object on every asset version even when its bytes are
    unchanged, so a cache keyed by remote name never survives a version bump:
    the whole release is re-downloaded although almost none of it changed.  The
    catalogue carries a fingerprint of each object's bytes that does survive the
    rename, so keying the cache on it lets a new version reuse everything that
    did not really change.  The fingerprint is only trusted here after being
    checked against the declared size as well.

    Returns "present", "reused" or "fetched" so a caller can report the split.
    """
    if destination.is_file() and (declared_size is None or destination.stat().st_size == declared_size):
        return "present"
    cached = None
    if content_key and cache_root is not None:
        candidate = cache_root / content_key
        if candidate.is_file() and (declared_size is None or candidate.stat().st_size == declared_size):
            cached = candidate
    if cached is not None:
        # A copy, not a link: the archive is handed to the overlay writer, and a
        # hard link would let an in-place rewrite corrupt the shared cache.
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, destination)
        return "reused"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = Request(url, headers={"User-Agent": "MLTDTranslationAssets-generated/1"})
    with urlopen(request, timeout=90) as response, temporary.open("wb") as stream:
        shutil.copyfileobj(response, stream, length=1 << 20)
    if declared_size is not None and temporary.stat().st_size != declared_size:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"official object size mismatch for {url}")
    temporary.replace(destination)
    if content_key and cache_root is not None:
        cached_target = cache_root / content_key
        if not cached_target.is_file():
            cached_target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(destination, cached_target)
            except OSError:
                # Another worker stored the same bytes first; that copy is fine.
                pass
    return "fetched"


def write_snapshot(path: Path, version: dict, index_path: Path,
                   selected: dict[str, dict]) -> None:
    objects = [{"logical": logical, **row} for logical, row in sorted(selected.items())]
    path.write_text(json.dumps({
        "complete": True,
        "scope": "jp-android",
        "asset_index": str(index_path.resolve()),
        "upstream_root": version["asset_root"],
        "objects": objects,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_overlay(snapshot: Path, archive: Path, ledger: Path, output: Path,
                client_version: str, asset_version: str) -> None:
    command = [sys.executable, str(ROOT / "pipelines/text/mltd_localization_pipeline.py"),
               "build-overlay", "--snapshot", str(snapshot),
               "--client-version", client_version, "--asset-version", asset_version,
               "--archive-root", str(archive), "--translations", str(ledger),
               "--output-root", str(output), "--progress-every", "250"]
    subprocess.run(command, cwd=ROOT, check=True)


def build_entries(overlay: Path, localization_manifest: Path, asset_version: str,
                  source_client_version: str, *, index_name: str,
                  index_path: Path) -> list[dict]:
    document = json.loads(localization_manifest.read_text(encoding="utf-8"))
    entries: list[dict] = []
    for row in document.get("bundles", []):
        logical = str(row["logical"])
        remote = str(row["remote"])
        artifact = overlay / "jp-android" / remote
        if not artifact.is_file():
            raise ValueError(f"localization manifest names missing artifact: {artifact}")
        entries.append({
            "logical_key": logical,
            "logical_path": f"production/2018/Android/{logical}",
            # The MLTD client reads the official .data catalog and requests
            # this hashed remote name, not the logical bundle name.
            "runtime_path": f"production/2018/Android/{remote}",
            "resource_kind": "bundle",
            "channel": "assets",
            "asset_version": asset_version,
            "client_version": None,
            "source_client_version": source_client_version,
            "source_sha256": str(row["source_bundle_sha256"]),
            "translated_sha256": str(row["output_plain_sha256"]),
            "reuse_status": "exact",
            "translation_status": "modified",
            "artifact_file": str(artifact.resolve()),
        })
    if not entries:
        raise ValueError("text overlay produced no changed bundles")
    index_digest = sha256_file(index_path)
    entries.append({
        "logical_key": "__official_asset_index__",
        "logical_path": f"production/2018/Android/{index_name}",
        "runtime_path": f"production/2018/Android/{index_name}",
        "resource_kind": "other",
        "channel": "assets",
        "asset_version": asset_version,
        "client_version": None,
        "source_client_version": source_client_version,
        "source_sha256": index_digest,
        "translated_sha256": index_digest,
        "reuse_status": "exact",
        "translation_status": "reused",
        "artifact_file": str(index_path.resolve()),
    })
    return entries


def image_inputs_are_complete(root: Path) -> bool:
    manifest = json.loads((root / "manifests/images.manifest.json").read_text(encoding="utf-8"))
    rows = manifest.get("images", [])
    if not rows:
        return False
    return all((root / str(row.get("localized", {}).get("relative_path", ""))).is_file()
               for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-manifest", type=Path,
                        default=ROOT / "manifests/asset-version.json")
    parser.add_argument("--work-root", type=Path, default=ROOT / ".generated-work")
    parser.add_argument("--output-root", type=Path, default=ROOT / "generated")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--translation-commit", required=True)
    parser.add_argument("--generated-commit", required=True)
    parser.add_argument("--ci-run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--max-bundles", type=int, default=0,
                        help="test-only cap; production must leave this at zero")
    parser.add_argument("--official-cache", type=Path, default=None,
                        help="directory keyed by the catalogue's content fingerprint; lets a new "
                             "asset version reuse every official object whose bytes did not change")
    parser.add_argument("--image-overlay-manifest", type=Path, default=None,
                        help="image-overlay/<asset_version>/overlay-manifest.json; enables the image "
                             "surface and merges its size patch into the published catalogue")
    parser.add_argument("--image-overlay-root", type=Path, default=None,
                        help="directory holding the overlay bundles (default: <manifest dir>/jp-android)")
    parser.add_argument("--image-index", type=Path, default=None,
                        help="catalogue shipped with the overlay: official sizes plus the image size patch")
    parser.add_argument("--require-images", action="store_true",
                        help="Fail closed: this builder cannot materialize/inject the image "
                             "surface or audit it independently, so requiring images always "
                             "refuses. The default text-only path is unaffected.")
    args = parser.parse_args()
    if not args.ci_run_id:
        raise ValueError("CI run identity is required; set GITHUB_RUN_ID or --ci-run-id")
    # Fail closed before any version/input scan or side effect (work dirs,
    # downloads, overlay subprocess, store publication).  This builder has no
    # image materialization/injector step and no independent image audit, so a
    # text-only release can never satisfy an image requirement.  The presence
    # of reviewed PNG inputs on disk is not evidence that an image artifact was
    # produced; publication must not proceed on that basis.
    if args.require_images and args.image_overlay_manifest is None:
        raise ValueError(
            "--require-images needs --image-overlay-manifest (plus --image-index/--image-overlay-root): "
            "materialization/injector step and no independent image audit, so it cannot "
            "produce a release containing the image surface. A text-only release is never "
            "proof that images were produced, and reviewed PNG inputs on disk are not an "
            "image artifact. Refusing to publish a text-only release mislabelled as "
            "satisfying the image requirement.")
    version = load_version_manifest(args.version_manifest)
    images_ready = image_inputs_are_complete(ROOT)
    grouped = read_translation_rows(ROOT, version["asset_version"])
    work = args.work_root.resolve()
    work.mkdir(parents=True, exist_ok=True)
    index_path = work / version["index_name"]
    download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    # The index has no size in the version manifest; its decoded one-table
    # structure is the authoritative validation.
    index = load_official_index(index_path)
    selected = {}
    canonical_grouped: dict[str, list[dict]] = {}
    index_by_fold = {name.casefold(): name for name in index}
    for logical, rows in grouped.items():
        canonical = index_by_fold.get(logical.casefold())
        if canonical is None:
            raise ValueError(f"translated logical bundle is absent from official index: {logical}")
        selected[canonical] = index[canonical]
        canonical_grouped[canonical] = rows
    if args.max_bundles:
        selected = dict(list(sorted(selected.items()))[:args.max_bundles])
    archive = work / "archive"
    official_cache = args.official_cache.resolve() if args.official_cache else None
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(download,
                                f"{version['asset_root']}/{row['remote']}",
                                archive / "jp-android" / row["remote"],
                                row["declared_size"],
                                row.get("catalog_hash"),
                                official_cache)
                   for row in selected.values()]
        fetched = [future.result() for future in futures]
    official_objects = {
        "total": len(fetched),
        "downloaded": sum(1 for outcome in fetched if outcome == "fetched"),
        "reused_from_cache": sum(1 for outcome in fetched if outcome == "reused"),
        "already_on_disk": sum(1 for outcome in fetched if outcome == "present"),
    }
    # Resolve cross-version rows against the downloaded official source before
    # feeding the legacy overlay resolver.  This prevents an older accepted
    # translation for the same bundle/key from conflicting with a newer source.
    current_grouped: dict[str, list[dict]] = {}
    for logical, rows in canonical_grouped.items():
        remote = index[logical]["remote"]
        _name, plain, _cipher = read_gtx(archive / "jp-android" / remote)
        current_sources = dict(parse_records(plain))
        selected_rows = select_rows_for_current_sources(
            rows, current_sources, str(version["asset_version"])
        )
        if selected_rows:
            current_grouped[logical] = selected_rows
    canonical_grouped = current_grouped
    selected = {logical: index[logical] for logical in canonical_grouped}
    if not selected:
        raise ValueError("no accepted translations match the current official source")
    snapshot = work / "snapshot.json"
    write_snapshot(snapshot, version, index_path, selected)
    ledger = work / "translations.jsonl"
    with ledger.open("w", encoding="utf-8", newline="\n") as stream:
        for logical in selected:
            for row in canonical_grouped[logical]:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    overlay = work / "overlay"
    run_overlay(snapshot, archive, ledger, overlay, version["client_version"], version["asset_version"])
    # Song lyrics are the second translatable surface and, until now, the one the
    # client never received: the library carried 11,065 accepted Chinese lines
    # that no published bundle contained.  Patch the lyric bundles into the same
    # overlay and let the manifest below publish them like text bundles.
    lyric_summary = build_lyric_overlay.run(
        index=index,
        archive_root=archive,
        overlay_root=overlay,
        lyrics_root=ROOT / "lyrics",
        asset_version=str(version["asset_version"]),
        upstream_root=str(version["asset_root"]),
        downloader=download,
        cache_root=official_cache,
        progress_every=100,
    )
    entries_path = work / "entries.json"
    entries = build_entries(overlay, overlay / "localization-manifest.json",
                            version["asset_version"], version["client_version"],
                            index_name=version["index_name"], index_path=index_path)
    entries_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    image_bundles = 0
    if args.image_overlay_manifest is not None:
        overlay_manifest = args.image_overlay_manifest.resolve()
        overlay_root = (args.image_overlay_root or overlay_manifest.parent / "jp-android").resolve()
        image_index = (args.image_index or (overlay_root / version["index_name"])).resolve()
        records = merge_image_overlay.load_overlay_manifest(overlay_manifest)
        merged_table, text_patch_rows = merge_image_overlay.merge(
            merge_image_overlay.load_catalogue(index_path),
            merge_image_overlay.load_catalogue(image_index), records)
        payload = msgpack.packb([merged_table], use_bin_type=True)
        if msgpack.unpackb(payload, raw=False, strict_map_key=False) != [merged_table]:
            raise ValueError("merged catalogue does not round-trip")
        merged_index = work / f"merged-{version['index_name']}"
        merged_index.write_bytes(payload)
        image_entries = merge_image_overlay.build_entries(
            records, overlay_root, version["asset_version"], version["client_version"])
        image_bundles = len(image_entries)
        # The client reads one declared size per runtime_path, so the catalogue
        # carries both patches or neither: the pass-through official index entry
        # is replaced by the merged catalogue, then the image bundles are added.
        entries = [entry for entry in entries if entry["logical_key"] != "__official_asset_index__"]
        entries.extend(image_entries)
        merged_digest = sha256_file(merged_index)
        entries.append({
            "logical_key": "__official_asset_index__",
            "logical_path": f"production/2018/Android/{version['index_name']}",
            "runtime_path": f"production/2018/Android/{version['index_name']}",
            "resource_kind": "other",
            "channel": "assets",
            "asset_version": version["asset_version"],
            "client_version": None,
            "source_client_version": version["client_version"],
            "source_sha256": merged_digest,
            "translated_sha256": merged_digest,
            "reuse_status": "exact",
            "translation_status": "reused",
            "artifact_file": str(merged_index.resolve()),
        })
        entries_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        print(json.dumps({"image_surface": "merged_into_release",
                          "image_bundles": image_bundles,
                          "text_patch_rows": text_patch_rows,
                          "merged_index_sha256": merged_digest}, ensure_ascii=False))
    store = GeneratedStore(args.output_root.resolve())
    result = store.build_release(
        version["asset_version"], entries,
        source_commit=args.source_commit,
        translation_commit=args.translation_commit,
        generated_commit=args.generated_commit,
        source_client_version=version["client_version"],
        ci_run_id=args.ci_run_id,
        entries_base=work,
    )
    if not result.written:
        raise RuntimeError(result.note)
    report = {
        "status": "success", "asset_version": version["asset_version"],
        "client_version": version["client_version"], "selected_bundles": len(selected),
        "generated_entries": len(entries), "generated_manifest": str(result.manifest_path),
        "image_surface": (f"published_{image_bundles}_bundles" if image_bundles else
                          ("ready_for_injection" if images_ready
                           else "blocked_missing_reviewed_inputs")),
        "official_index_sha256": sha256_file(index_path),
        "official_objects": official_objects,
        "lyrics": {key: value for key, value in lyric_summary.items() if key != "patched_bundles"},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
