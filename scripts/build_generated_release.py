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
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

import msgpack

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from assets_generated_index import GeneratedStore, GeneratedStoreError
import build_lyric_overlay
import merge_image_overlay
import release_reuse
import source_provenance
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
    if not version.isdigit():
        raise ValueError("asset-version.json has an invalid asset_version")
    template = str(value.get("asset_root", ""))
    if "{version}" not in template or not template.startswith("https://td-assets.bn765.com/"):
        raise ValueError("asset_root must be the official td-assets.bn765.com template")
    index_name = str(value.get("index_name", ""))
    if not index_name or "/" in index_name or "\\" in index_name:
        raise ValueError("asset-version.json has no safe official index_name")
    # No client version here: the release's ``source_client_version`` comes from
    # the rows themselves (see ``scripts/source_provenance.py``).  The key is
    # still honoured when present so an older checkout builds unchanged.
    legacy_client = str(value.get("client_version", "") or "")
    return {"asset_version": version, "legacy_client_version": legacy_client,
            "asset_root": template.format(version=version).rstrip("/"),
            "index_name": index_name}


def release_provenance(version: dict, root: Path = ROOT) -> str:
    """The client version this release describes, asked of the library itself.

    The locale rows carry ``source_client_version`` individually and the schema
    requires it, so the majority value is the honest answer for the release as a
    whole.  ``manifests/asset-version.json`` used to hold a second,
    hand-maintained copy which read as a version to keep in step with the game;
    nothing keeps it in step today, so it is gone.

    This reads the library rather than the rows ``read_translation_rows``
    returns: that projection deliberately keeps only what the applier needs, and
    the first version of this function trusted it, so the release build failed
    with "no locale row carries one" while 395,673 rows said 9.0.200.
    """
    provenance, _counts = source_provenance.from_library(root)
    if provenance is None:
        provenance = version.get("legacy_client_version") or None
    if provenance is None:
        raise ValueError(
            "cannot determine source_client_version: no locale row carries one and "
            "manifests/asset-version.json has no 'client_version' to fall back on"
        )
    return provenance


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


def read_translation_rows(root: Path, asset_version: str,
                          sources: dict[str, set[str]] | None = None) -> dict[str, list[dict]]:
    """Every accepted translation, grouped by the logical bundle it belongs to.

    ``sources``, when given, is filled with the reverse map the incremental path
    needs: which ``locales/`` files fed each logical bundle.  A bundle normally
    comes from one file named after it, but the row's own ``bundle`` field is the
    authority, so the map is collected here rather than guessed from file names.
    """
    grouped: dict[str, list[dict]] = {}
    seen: dict[tuple[str, str, str], str] = {}
    for path in sorted((root / "locales").rglob("*.jsonl")):
        relative = path.relative_to(root).as_posix()
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
                if sources is not None:
                    sources.setdefault(identity[0], set()).add(relative)
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


@dataclass
class TextSurface:
    """What one text-overlay pass produced."""

    #: logical bundle -> the source-bound rows that were fed to the overlay
    grouped: dict[str, list[dict]]
    overlay: Path
    #: the overlay's own ``localization-manifest.json``
    document: dict
    official_objects: dict


def run_text_surface(*, work: Path, version: dict, targets: dict[str, dict],
                     grouped: dict[str, list[dict]],
                     source_client: str,
                     official_cache: Path | None,
                     overlay: Path | None = None) -> TextSurface:
    """Fetch, read and rewrite exactly ``targets``, then report what it resolved.

    The caller decides the target set: a full build passes every catalogue
    bundle, an incremental build passes only the bundles whose inputs moved.
    Fetching *and* reading are both per-bundle, so a reused bundle skips both --
    which is where the six runner minutes of the old full rebuild went.  Reading
    is not optional for a target: it is what binds each row to the official
    source text the row was accepted against.
    """
    overlay = work / "overlay" if overlay is None else overlay
    # The overlay refuses to write into a directory that already belongs to a
    # different snapshot identity, which is right for a directory that is meant
    # to hold exactly one build.  This builder legitimately runs the surface more
    # than once (a subset attempt, then a full retry) and a person can re-run it
    # by hand in the same work directory, so every attempt starts from its own
    # empty directory instead of inheriting the previous attempt's identity.
    shutil.rmtree(overlay, ignore_errors=True)
    overlay.mkdir(parents=True, exist_ok=True)
    if not targets:
        # Nothing to regenerate: the store already holds every published bundle.
        # The overlay refuses a snapshot with no objects on purpose -- an empty
        # one would silently publish a release with no text surface at all -- so
        # a build that legitimately has nothing to rewrite does not invoke it.
        # The counter block is written in the overlay's own shape because the
        # release records it and the lyric pass appends to the same file.
        document = {"schema_version": 1, "source_candidates": 0, "resolved_exact": 0,
                    "resolved_memory": 0, "stale_exact": 0, "bundles": []}
        (overlay / "localization-manifest.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return TextSurface(
            grouped={}, overlay=overlay, document=document,
            official_objects={"total": 0, "downloaded": 0, "reused_from_cache": 0,
                              "already_on_disk": 0})
    archive = work / "archive"
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(download,
                                f"{version['asset_root']}/{row['remote']}",
                                archive / "jp-android" / row["remote"],
                                row["declared_size"],
                                row.get("catalog_hash"),
                                official_cache)
                   for row in targets.values()]
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
    for logical, row in targets.items():
        _name, plain, _cipher = read_gtx(archive / "jp-android" / row["remote"])
        current_sources = dict(parse_records(plain))
        selected_rows = select_rows_for_current_sources(
            grouped[logical], current_sources, str(version["asset_version"])
        )
        if selected_rows:
            current_grouped[logical] = selected_rows
    snapshot = work / "snapshot.json"
    write_snapshot(snapshot, version, work / version["index_name"],
                   {logical: targets[logical] for logical in current_grouped})
    ledger = work / "translations.jsonl"
    with ledger.open("w", encoding="utf-8", newline="\n") as stream:
        for logical in current_grouped:
            for row in current_grouped[logical]:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    run_overlay(snapshot, archive, ledger, overlay,
                source_client, version["asset_version"])
    document = json.loads(
        (overlay / "localization-manifest.json").read_text(encoding="utf-8"))
    return TextSurface(grouped=current_grouped, overlay=overlay, document=document,
                       official_objects=official_objects)


def build_entries(overlay: Path, localization_manifest: Path, asset_version: str,
                  source_client_version: str, *, index_name: str,
                  index_path: Path, allow_empty: bool = False) -> list[dict]:
    """Turn the overlay's own manifest into store entries.

    ``allow_empty`` is what an incremental build needs: reusing every text bundle
    leaves the overlay with nothing to write, and that is a success, not the
    "produced no changed bundles" failure a full build must still report.
    """
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
    if not entries and not allow_empty:
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
    parser.add_argument("--no-reuse", action="store_true",
                        help="regenerate every bundle even when the published release proves it "
                             "can be reused; use after changing the build procedure itself")
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
    bundle_sources: dict[str, set[str]] = {}
    grouped = read_translation_rows(ROOT, version["asset_version"], sources=bundle_sources)
    # The release's provenance is a property of the text it carries, so it is
    # read from the library instead of from a field somebody has to keep current.
    source_client = release_provenance(version)
    work = args.work_root.resolve()
    work.mkdir(parents=True, exist_ok=True)
    store = GeneratedStore(args.output_root.resolve())
    index_path = work / version["index_name"]
    download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    # The index has no size in the version manifest; its decoded one-table
    # structure is the authoritative validation.
    index = load_official_index(index_path)
    index_sha256 = sha256_file(index_path)
    targets: dict[str, dict] = {}
    canonical_grouped: dict[str, list[dict]] = {}
    canonical_sources: dict[str, set[str]] = {}
    index_by_fold = {name.casefold(): name for name in index}
    for logical, rows in grouped.items():
        canonical = index_by_fold.get(logical.casefold())
        if canonical is None:
            raise ValueError(f"translated logical bundle is absent from official index: {logical}")
        # Everything below is keyed by the catalogue's own spelling: a locale file
        # may name `CD_jp.gtx` where the catalogue says `cd_jp.gtx`, and the rows
        # have to stay attached to the name the rest of the build looks up.
        if canonical in targets:
            canonical_grouped[canonical].extend(rows)
            canonical_sources[canonical] |= bundle_sources[logical]
            continue
        targets[canonical] = index[canonical]
        canonical_grouped[canonical] = list(rows)
        canonical_sources[canonical] = set(bundle_sources[logical])
    if args.max_bundles:
        targets = dict(list(sorted(targets.items()))[:args.max_bundles])
        canonical_grouped = {name: canonical_grouped[name] for name in targets}
        canonical_sources = {name: canonical_sources[name] for name in targets}
    official_cache = args.official_cache.resolve() if args.official_cache else None

    # What did this build really have to regenerate?  The release already in the
    # store answers for every bundle whose own rows and official object are
    # unchanged; see scripts/release_reuse.py for why that is provable and why a
    # release that is not provably free of cross-bundle answers is not reused.
    previous = None
    if not args.no_reuse:
        try:
            previous = store.load_manifest(version["asset_version"])
        except GeneratedStoreError as error:
            # No published release for this version is the ordinary first build
            # of a version, not a failure; anything else is reported and then
            # answered the same safe way -- regenerate everything.
            print(f"no reusable release manifest for {version['asset_version']}: {error}",
                  file=sys.stderr)
    plan = release_reuse.plan_reuse(
        root=ROOT, manifest=previous, asset_version=version["asset_version"],
        index=targets, bundle_sources=canonical_sources, index_sha256=index_sha256,
        object_file=store.object_file)
    print(json.dumps({"reuse_plan": plan.to_dict()}, ensure_ascii=False), file=sys.stderr)

    surface = run_text_surface(work=work, version=version,
                               targets=dict(plan.rebuild), grouped=canonical_grouped,
                               source_client=source_client, official_cache=official_cache)
    resolution = release_reuse.Resolution.from_overlay(surface.document)
    subset_is_exclusive = resolution is not None and resolution.exclusive
    if plan.reusable and not subset_is_exclusive:
        # A regenerated bundle answered a key without its own row, so in this
        # subset run the global translation-memory table was smaller than the one
        # a full build would have used.  Its bytes could differ from what the
        # release would otherwise contain, and nothing cheap can tell which
        # bundles that reached; redo the whole surface before publishing.
        print(json.dumps({
            "reuse_fallback": "the subset resolved keys without their own row; "
                               "rebuilding every bundle to keep the release identical "
                               "to a full build",
            "resolution": resolution.to_dict() if resolution else None},
            ensure_ascii=False), file=sys.stderr)
        plan = release_reuse.ReusePlan(
            rebuild={name: dict(row) for name, row in targets.items()},
            reason="the subset run was not exclusive")
        surface = run_text_surface(work=work, version=version, targets=plan.rebuild,
                                   grouped=canonical_grouped, source_client=source_client,
                                   official_cache=official_cache)
        resolution = release_reuse.Resolution.from_overlay(surface.document)
    if resolution is None:
        # The overlay predates the route counters.  Publish anyway -- the release
        # is not at stake -- but record no reuse block, so the next build rebuilds
        # everything instead of inheriting an unprovable claim.
        print("the overlay did not report how it resolved its keys; this release "
              "records no reuse block and the next build will be a full rebuild",
              file=sys.stderr)
    text_scope = "regenerated" if plan.reusable else "release"
    selected = {**{logical: targets[logical] for logical in surface.grouped},
                **{logical: targets[logical] for logical in plan.reusable}}
    if not selected:
        raise ValueError("no accepted translations match the current official source")
    overlay = surface.overlay
    # Song lyrics are the second translatable surface and, until now, the one the
    # client never received: the library carried 11,065 accepted Chinese lines
    # that no published bundle contained.  Patch the lyric bundles into the same
    # overlay and let the manifest below publish them like text bundles.
    lyric_summary = build_lyric_overlay.run(
        index=index,
        archive_root=work / "archive",
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
                            version["asset_version"], source_client,
                            index_name=version["index_name"], index_path=index_path,
                            allow_empty=bool(plan.reusable))
    # The reused bundles were never handed to the overlay, so their entries come
    # from the release already in the store: same bytes, same digest, and the
    # store re-hashes the object before it accepts the entry.
    entries.extend(plan.reusable.values())
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
            records, overlay_root, version["asset_version"], source_client)
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
            "source_client_version": source_client,
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
    # One client path, one logical name, one set of bytes.  The text pass, the
    # lyric pass and the reused entries are three sources feeding one entry list,
    # and an incremental build is the first time a reused entry can collide with a
    # freshly written one; refuse rather than publish an ambiguous release.
    seen_keys: set[str] = set()
    for entry in entries:
        logical = str(entry["logical_key"])
        if logical in seen_keys:
            raise ValueError(
                f"the release would name {logical!r} twice; the text, lyric and reused "
                "surfaces must not publish the same bundle")
        seen_keys.add(logical)

    # What the next build may reuse, recorded with the release it describes.
    # `text_scope` says what the counters cover: every published bundle on a full
    # build, only the regenerated subset on an incremental one.  No counters at
    # all means no claim, and a build that makes no claim is rebuilt in full.
    reuse_record = None
    if resolution is not None:
        reuse_record = release_reuse.reuse_block(
            index_sha256=index_sha256, resolution=resolution, scope=text_scope)
    result = store.build_release(
        version["asset_version"], entries,
        source_commit=args.source_commit,
        translation_commit=args.translation_commit,
        generated_commit=args.generated_commit,
        source_client_version=source_client,
        ci_run_id=args.ci_run_id,
        entries_base=work,
        extra_manifest=reuse_record,
    )
    if not result.written:
        raise RuntimeError(result.note)
    report = {
        "status": "success", "asset_version": version["asset_version"],
        "client_version": None, "source_client_version": source_client,
        "selected_bundles": len(selected),
        "generated_entries": len(entries), "generated_manifest": str(result.manifest_path),
        "image_surface": (f"published_{image_bundles}_bundles" if image_bundles else
                          ("ready_for_injection" if images_ready
                           else "blocked_missing_reviewed_inputs")),
        "official_index_sha256": index_sha256,
        "official_objects": surface.official_objects,
        "reuse": {**plan.to_dict(), "objects_written": result.objects_written,
                  "objects_deduped": result.objects_deduped,
                  "text_resolution": resolution.to_dict() if resolution else None,
                  "text_scope": text_scope},
        "lyrics": {key: value for key, value in lyric_summary.items() if key != "patched_bundles"},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
