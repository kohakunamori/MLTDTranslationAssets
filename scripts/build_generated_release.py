#!/usr/bin/env python3
"""Build the public Assets generated release from the official asset server.

The repository contains translation sources, not an unchecked copy of the
official archive.  This command resolves the independent ``asset_version``
from ``manifests/asset-version.json``, downloads only the official bundles
referenced by accepted translation rows, runs the existing GTX writer, and
publishes the resulting Unity3D files through the content-addressed store.

Images can come from a named audited overlay or ``--reuse-published-images``.
The latter verifies previously published objects against the current official
source and reuses their bytes without image generation or texture injection.
Reviewed PNG inputs alone never prove a published image artifact exists.
``--require-images`` still requires an explicit overlay before any side effect.

The release also publishes the official ``.data`` catalogue, because that is how
the client learns each bundle's declared byte length, and the client requires the
bytes it downloads to have exactly that length.  Because this release serves
rewritten bytes for every bundle it covers, it publishes a *size-aligned copy* of
the catalogue rather than the pristine download; see :func:`align_index_sizes`.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import msgpack

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from assets_generated_index import GeneratedStore, GeneratedStoreError
import build_lyric_overlay
import event_unit_localization
import merge_image_overlay
import release_reuse
import bundle_incremental as incremental
import source_provenance
import reuse_published_images
from pipelines.core.hashing import sha256_bytes, sha256_file, sha256_text
from pipelines.text.mltd_localize_gtx import parse_records, read_gtx
from pipelines.text.mltd_localization_pipeline import (
    build_resolver, resolve_translation, is_source_text,
)

#: Where the client requests every generated object, and therefore where the
#: catalogue's remote names live.  It is the only namespace in which a released
#: entry and a catalogue record can be compared at all.
ASSET_PREFIX = "production/2018/Android/"


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


def release_provenance(version: dict, root: Path | None = None,
                       *, counts: Counter | None = None) -> str:
    """The client version this release describes, asked of the library itself.

    The locale rows carry ``source_client_version`` individually and the schema
    requires it, so the majority value is the honest answer for the release as a
    whole.  ``manifests/asset-version.json`` used to hold a second,
    hand-maintained copy which read as a version to keep in step with the game;
    nothing keeps it in step today, so it is gone.

    The builder tallies every library row while reading translations, before
    projecting accepted rows for the applier. Other callers can still ask the
    library directly when they have no tally.
    """
    if counts is None:
        provenance, _counts = source_provenance.from_library(root or ROOT)
    else:
        provenance = source_provenance.from_counts(counts)
    if provenance is None:
        provenance = version.get("legacy_client_version") or None
    if provenance is None:
        raise ValueError(
            "cannot determine source_client_version: no locale row carries one and "
            "manifests/asset-version.json has no 'client_version' to fall back on"
        )
    return provenance


def load_official_index(path: Path) -> dict[str, dict]:
    # The catalogue has hundreds of thousands of rows. Decode each pair into
    # its final representation instead of holding raw and normalized maps together.
    result: dict[str, dict] = {}
    length = path.stat().st_size
    with path.open('rb') as stream:
        unpacker = msgpack.Unpacker(stream, raw=False, strict_map_key=False,
                                   read_size=min(65536, max(length, 1)),
                                   max_buffer_size=max(length, 1))
        try:
            if unpacker.read_array_header() != 1:
                raise ValueError("official asset index is not the expected one-table format")
            count = unpacker.read_map_header()
        except (ValueError, msgpack.OutOfData) as error:
            raise ValueError("official asset index is not the expected one-table format") from error
        try:
            for _ in range(count):
                logical, row = unpacker.unpack(), unpacker.unpack()
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
        except msgpack.OutOfData as error:
            raise ValueError('official asset index is incomplete') from error
        if unpacker.tell() != length:
            raise ValueError('official asset index contains trailing data')
    if not result:
        raise ValueError("official asset index is empty")
    return result


def logical_name(bundle: str) -> str:
    return bundle if bundle.endswith(".unity3d") else bundle + ".unity3d"


def read_translation_rows(root: Path, asset_version: str,
                          sources: dict[str, set[str]] | None = None,
                          *, allow_empty: bool = False,
                          provenance_counts: Counter | None = None) -> dict[str, list[dict]]:
    """Every accepted translation, grouped by the logical bundle it belongs to.

    ``sources``, when given, is filled with the reverse map the incremental path
    needs: which ``locales/`` files fed each logical bundle.  A bundle normally
    comes from one file named after it, but the row's own ``bundle`` field is the
    authority, so the map is collected here rather than guessed from file names.
    """
    grouped: dict[str, list[dict]] = {}
    seen: dict[tuple[str, str, str, str], str] = {}
    provenance_paths = set(root.glob(source_provenance.LIBRARY_GLOB)) if provenance_counts is not None else set()
    for path in sorted((root / "locales").rglob("*.jsonl")):
        relative = path.relative_to(root).as_posix()
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if provenance_counts is not None and path in provenance_paths:
                    value = row.get("source_client_version")
                    if isinstance(value, str) and value:
                        provenance_counts[value] += 1
                if row.get("status") != "accepted" or not str(row.get("zh", "")):
                    continue
                source = str(row.get("ja", ""))
                translation = str(row.get("zh", ""))
                if not source or "|" in translation or "^" in translation:
                    raise ValueError(f"invalid accepted translation at {path}:{line_no}")
                source_hash = sha256_text(source)
                if str(row.get("source_sha256", "")).lower() != source_hash:
                    raise ValueError(f"source hash mismatch at {path}:{line_no}")
                bundle = str(row.get("bundle", ""))
                if event_unit_localization.is_bundle(logical_name(bundle)):
                    continue
                key = str(row.get("item_key", ""))
                if not bundle or not key:
                    raise ValueError(f"accepted row lacks bundle/item_key at {path}:{line_no}")
                # The same bundle/key may have different source text in
                # different asset versions.  Keep each source-bound candidate;
                # the current official bundle selects the matching one below.
                row_asset_version = str(row.get("asset_version", "")).strip()
                identity = (logical_name(bundle), key, source_hash, row_asset_version)
                prior = seen.get(identity)
                if prior is not None and prior != translation:
                    raise ValueError(f"conflicting translation for {identity}")
                seen[identity] = translation
                grouped.setdefault(identity[0], []).append({
                    "bundle": bundle,
                    "key": key,
                    "source": source,
                    "source_sha256": source_hash,
                    "translation": translation,
                    "status": "accepted",
                    "asset_version": row_asset_version or asset_version,
                })
                if sources is not None:
                    sources.setdefault(identity[0], set()).add(relative)
    if not grouped and not allow_empty:
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


def prepare_text_inputs(*, work: Path, store: GeneratedStore, version: dict,
                        targets: dict, grouped: dict, previous: dict | None,
                        official_cache: Path | None) -> tuple[dict, dict, Path, dict]:
    """Resolve the entire text library; only Unity source cache misses are read.

The ledger always includes unchanged bundles, so rebuilding a subset has the
same translation-memory semantics as a full build. The resulting replacement
set, rather than its JSONL file's timestamp/layout, is each bundle's input.
    """
    parser = incremental.writer_fingerprint(ROOT, incremental.PARSER_FILES)
    writer = incremental.writer_fingerprint(ROOT, incremental.TEXT_FILES)
    old = incremental.previous_state(previous).get("text", {})
    documents, records, current_grouped = {}, {}, {}
    outcomes = {}
    missing = {}
    for logical, row in targets.items():
        document = incremental.load_sources(store.root, old.get(logical), row, parser)
        if document is None:
            missing[logical] = row
        else:
            documents[logical] = document
            records[logical] = {key: old[logical][key] for key in (
                "source_identity", "parser", "source_sha256", "source_records_sha256")}
    archive = work / "archive" / "jp-android"
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = {logical: pool.submit(
            download, f"{version['asset_root']}/{row['remote']}", archive / row["remote"],
            row["declared_size"], row.get("catalog_hash"), official_cache)
            for logical, row in missing.items()}
        for logical, future in futures.items():
            outcomes[logical] = future.result()
    for logical, row in missing.items():
        source = archive / row["remote"]
        name, plain, _cipher = read_gtx(source)
        documents[logical], records[logical] = incremental.save_sources(
            store.root, row, parser, name, list(parse_records(plain)), sha256_file(source))
    for logical in targets:
        selected = select_rows_for_current_sources(
            grouped[logical], dict(documents[logical]["records"]), str(version["asset_version"]))
        if selected:
            current_grouped[logical] = selected
    ledger = work / "all-translations.jsonl"
    with ledger.open("w", encoding="utf-8", newline="\n") as stream:
        for logical in sorted(current_grouped):
            for row in current_grouped[logical]:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    resolver = build_resolver([ledger])
    for logical, document in documents.items():
        replacements = {}
        routes = {"exact": 0, "memory": 0, "stale_exact": 0, "unresolved": 0}
        # The full writer excludes bundles with no current source-bound rows.
        if logical in current_grouped:
            for key, source in document["records"]:
                if not is_source_text(source):
                    continue
                translated, route = resolve_translation(resolver, document["name"], key, source)
                routes[route] += 1
                if translated is not None and translated != source:
                    replacements[key] = translated
        records[logical].update({
            "fingerprint": incremental.digest({"source": document["source_sha256"],
                                                "writer": writer, "replacements": replacements}),
            "resolution": routes,
        })
    stats = {"total": len(outcomes),
             "downloaded": sum(value == "fetched" for value in outcomes.values()),
             "reused_from_cache": sum(value == "reused" for value in outcomes.values()),
             "already_on_disk": sum(value == "present" for value in outcomes.values()),
             "source_records_reused": len(targets) - len(missing)}
    return current_grouped, records, ledger, stats


def load_previous_release(store: GeneratedStore, asset_version: str) -> dict | None:
    """Use this version's release, or the newest successful older version."""
    versions = [asset_version] + sorted(
        (v for v in store.list_releases() if v.isdigit() and int(v) < int(asset_version)),
        key=int, reverse=True)
    for candidate in versions:
        try:
            previous = store.load_manifest(candidate)
        except GeneratedStoreError:
            continue
        if previous.get("build_status") == "success":
            return previous
    return None


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
    for attempt in range(3):
        try:
            with urlopen(request, timeout=90) as response, temporary.open("wb") as stream:
                shutil.copyfileobj(response, stream, length=1 << 20)
            break
        except (URLError, TimeoutError, ConnectionError) as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
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
                     overlay: Path | None = None,
                     selected_grouped: dict | None = None,
                     full_ledger: Path | None = None) -> TextSurface:
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
    if selected_grouped is not None:
        targets = {logical: row for logical, row in targets.items() if logical in selected_grouped}
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
                   for row in targets.values()
                   if not (archive / "jp-android" / row["remote"]).is_file()
                   or (archive / "jp-android" / row["remote"]).stat().st_size != row["declared_size"]]
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
        if selected_grouped is not None:
            current_grouped[logical] = selected_grouped[logical]
        else:
            _name, plain, _cipher = read_gtx(archive / "jp-android" / row["remote"])
            selected_rows = select_rows_for_current_sources(
                grouped[logical], dict(parse_records(plain)), str(version["asset_version"]))
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
    run_overlay(snapshot, archive, full_ledger or ledger, overlay,
                source_client, version["asset_version"])
    document = json.loads(
        (overlay / "localization-manifest.json").read_text(encoding="utf-8"))
    return TextSurface(grouped=current_grouped, overlay=overlay, document=document,
                       official_objects=official_objects)


def build_entries(overlay: Path, localization_manifest: Path, asset_version: str,
                  source_client_version: str, *, allow_empty: bool = False) -> list[dict]:
    """Turn the overlay's own manifest into store entries.

    ``allow_empty`` is what an incremental build needs: reusing every text bundle
    leaves the overlay with nothing to write, and that is a success, not the
    "produced no changed bundles" failure a full build must still report.

    The catalogue's own entry is *not* part of this list: it can only be built once
    every surface's bundles are known (see :func:`align_index_sizes`), and this
    function sees one surface.  The caller appends it afterwards.
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
            "logical_path": f"{ASSET_PREFIX}{logical}",
            # The MLTD client reads the official .data catalog and requests
            # this hashed remote name, not the logical bundle name.
            "runtime_path": f"{ASSET_PREFIX}{remote}",
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
    return entries


@dataclass(frozen=True)
class AlignedIndex:
    """The size-aligned catalogue copy a release publishes.

    ``covered_records`` counts the catalogue records whose remote name this release
    serves -- every one of them now declares the published byte length -- and
    ``changed_records`` the subset whose declared size differs from the catalogue the
    call read; a published bundle may happen to have that length already.  Keeping
    the two apart is what makes "we rewrote N records" checkable against "the release
    serves M bundles".
    """

    #: the file the release publishes; never the pristine official file
    path: Path
    sha256: str
    records: int
    covered_records: int
    changed_records: int

    @property
    def uncovered_records(self) -> int:
        """Records this release does not serve, left as the official file had them."""
        return self.records - self.covered_records

    @property
    def unchanged_records(self) -> int:
        """Covered records that already declared the published length."""
        return self.records - self.changed_records


def _published_size(entry: Mapping[str, Any],
                    object_file: Callable[[str], Path | None] | None) -> int:
    """The byte length of the artifact this release publishes for one entry.

    An entry naming ``artifact_file`` is about to be stored from exactly those
    bytes; a reused entry names an object already in the store instead.  Both are
    the bytes the client will receive, which is the only length a catalogue record
    may declare.  Nothing is hashed here on purpose: the store re-reads and
    re-hashes the same bytes before it accepts the entry, so a file that moved
    underneath this call fails the build instead of publishing a wrong length.
    """
    artifact_file = entry.get("artifact_file")
    if artifact_file:
        path = Path(str(artifact_file))
        if not path.is_file():
            raise ValueError(f"released entry names a missing artifact: {path}")
        return path.stat().st_size
    digest = str(entry.get("artifact_sha256") or "")
    found = object_file(digest) if object_file is not None else None
    if found is None:
        raise ValueError(
            f"released entry {entry.get('logical_key')!r} carries neither an artifact_file nor a "
            f"stored object for {digest!r}, so its published length is unknown")
    return found.stat().st_size


def align_index_sizes(catalogue_path: Path, entries: Sequence[Mapping[str, Any]], *,
                      output_path: Path,
                      object_file: Callable[[str], Path | None] | None = None) -> AlignedIndex:
    """Publish a size-aligned copy of the catalogue instead of the pristine file.

    The client requires ``Content-Length == declared_size`` for every object the
    catalogue names (the official CDN answers a read-only GET with no
    ``Content-Encoding`` and a ``Content-Length`` equal to the declared size), and
    this release deliberately serves *rewritten* bytes for every bundle it covers.
    A pristine catalogue therefore contradicts the release it ships with: the client
    asks for the translated bundle, receives it, and rejects its own download
    because the length is not the declared one.  The ``/cn/`` overlay catalogue
    solved the same problem the same way -- same records, size patch only
    (``local-data/build/runs/asset-archive/9.0.200/cn-overlay-index-build-20260924``).

    So every record stays semantically identical -- same logical name, same
    position, same catalog hash, same remote name -- except that the declared size
    of a record whose remote name this release serves becomes the byte size of the
    artifact published for that name.  A remote name shared by several records moves
    all of them together, because the client resolves a name, not a record.

    Byte-identity with the official file is impossible here and not required: that
    file's msgpack is not canonically packed (part of its records use a fixed-width
    encoding the reference encoder emits two bytes shorter), so the copy is
    re-encoded, and the check that matters is that it decodes back to exactly the
    same records.  ``catalogue_path`` is read, never written: the pristine official
    catalogue has to survive the build, because a later build compares its digest
    with the one the release recorded to decide what may be reused.

    ``entries`` must be the *complete* set of entries the release publishes.  A
    surface aligned on its own would leave every other surface's bundles
    contradicting the catalogue, which is the failure this function exists to fix.

    ``changed_records`` in the result counts against ``catalogue_path``, so a caller
    that hands over a catalogue some other pass already patched (the image
    surface's merged table) is told what *this* call moved, not how far the
    published table is from the official one.
    """
    if catalogue_path.resolve() == output_path.resolve():
        raise ValueError(
            f"the aligned catalogue would replace the file it was read from "
            f"({catalogue_path.resolve()}); the pristine official catalogue must survive "
            "the build unchanged")
    table = load_official_index(catalogue_path)
    known_remotes = {row["remote"] for row in table.values()}

    sizes: dict[str, int] = {}
    for entry in entries:
        runtime = str(entry.get("runtime_path") or "")
        if not runtime.startswith(ASSET_PREFIX):
            # Not a path the client looks up in the catalogue: the catalogue's own
            # entry is appended after this call, never before it.
            continue
        remote = runtime[len(ASSET_PREFIX):]
        if remote not in known_remotes:
            raise ValueError(
                f"released entry {entry.get('logical_key')!r} is served as {runtime!r}, which no "
                "official catalogue record names; only a bundle the catalogue names can be "
                "aligned, and the catalogue's own entry belongs after this call")
        size = _published_size(entry, object_file)
        prior = sizes.get(remote)
        if prior is not None and prior != size:
            raise ValueError(
                f"two released entries are served as {remote} with different lengths ({prior} and "
                f"{size}); one client path can only have one byte length")
        sizes[remote] = size

    aligned: dict[str, list] = {}
    covered = changed = 0
    for logical, row in table.items():
        size = sizes.get(row["remote"])
        if size is None:
            aligned[logical] = [row["catalog_hash"], row["remote"], row["declared_size"]]
            continue
        covered += 1
        changed += size != row["declared_size"]
        aligned[logical] = [row["catalog_hash"], row["remote"], size]

    payload = msgpack.packb([aligned], use_bin_type=True)
    # Never publish a catalogue this build cannot read back to the same records: a
    # re-encoding that dropped or reordered one would surface far from here, as a
    # download the client never makes or a name it looks up in vain.
    if msgpack.unpackb(payload, raw=False, strict_map_key=False) != [aligned]:
        raise ValueError("the aligned catalogue does not round-trip through msgpack")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".writing")
    temporary.write_bytes(payload)
    temporary.replace(output_path)
    return AlignedIndex(path=output_path, sha256=sha256_bytes(payload), records=len(table),
                        covered_records=covered, changed_records=changed)


def index_entry(aligned: AlignedIndex, *, index_name: str, pristine_sha256: str,
                asset_version: str, source_client_version: str) -> dict:
    """The manifest entry that publishes a release's size-aligned catalogue.

    ``source_sha256`` describes the pristine official catalogue -- the file this
    build downloaded, and the one the release's ``reuse`` record is keyed on, so a
    later build can still tell whether the official catalogue moved -- while
    ``translated_sha256``, and therefore ``artifact_sha256``/``object_path``, which
    the store derives from ``artifact_file``, describe the aligned file that is
    actually served.  ``translation_status`` is ``modified`` and not ``reused``:
    the published bytes are no longer the official bytes, because every covered
    record's declared size was rewritten.  ``resource_kind`` stays ``other``,
    because the catalogue is not one of the bundles it names.
    """
    if aligned.path.name != index_name:
        raise ValueError(
            f"the aligned catalogue is named {aligned.path.name!r} but the client requests "
            f"{index_name!r}; the published file must carry the official index name")
    runtime = f"{ASSET_PREFIX}{index_name}"
    return {
        "logical_key": "__official_asset_index__",
        "logical_path": runtime,
        "runtime_path": runtime,
        "resource_kind": "other",
        "channel": "assets",
        "asset_version": asset_version,
        "client_version": None,
        "source_client_version": source_client_version,
        "source_sha256": pristine_sha256,
        "translated_sha256": aligned.sha256,
        "reuse_status": "exact",
        "translation_status": "modified",
        "artifact_file": str(aligned.path),
    }


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
                        help="regenerate text/lyric bundles even when their inputs are unchanged; "
                             "published images still require source-verified reuse")
    parser.add_argument("--reuse-published-images", action="store_true",
                        help="Publish existing image bundles whose source SHA-256 still matches "
                             "the current official bundle; never generate or inject images")
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
    if args.reuse_published_images and args.image_overlay_manifest is not None:
        raise ValueError("choose either an explicit image overlay or published-image reuse")
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
    provenance_counts = Counter()
    grouped = read_translation_rows(ROOT, version["asset_version"], allow_empty=True,
                                    provenance_counts=provenance_counts)
    # The release's provenance is a property of the text it carries, so it is
    # read from the library instead of from a field somebody has to keep current.
    source_client = release_provenance(version, counts=provenance_counts)
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
            continue
        targets[canonical] = index[canonical]
        canonical_grouped[canonical] = list(rows)
    if args.max_bundles:
        targets = dict(list(sorted(targets.items()))[:args.max_bundles])
        canonical_grouped = {name: canonical_grouped[name] for name in targets}
    official_cache = args.official_cache.resolve() if args.official_cache else None

    previous = load_previous_release(store, version["asset_version"])
    current_grouped, text_records, full_ledger, source_stats = prepare_text_inputs(
        work=work, store=store, version=version, targets=targets,
        grouped=canonical_grouped, previous=previous, official_cache=official_cache)
    plan = incremental.plan(
        surface="text", records=text_records, rows=targets, previous=previous,
        object_file=store.object_file, force=args.no_reuse, asset_version=version["asset_version"])
    print(json.dumps({"reuse_plan": plan.to_dict()}, ensure_ascii=False), file=sys.stderr)
    surface = run_text_surface(
        work=work, version=version, targets=dict(plan.rebuild), grouped=canonical_grouped,
        source_client=source_client, official_cache=official_cache,
        selected_grouped=current_grouped, full_ledger=full_ledger)
    for key in ("total", "downloaded", "reused_from_cache", "already_on_disk"):
        source_stats[key] += surface.official_objects[key]
    surface.official_objects = source_stats
    counters = {route: sum(record["resolution"][route] for record in text_records.values())
                for route in ("exact", "memory", "stale_exact", "unresolved")}
    resolution = release_reuse.Resolution(**counters)
    text_scope = "regenerated" if plan.reusable else "release"
    selected = {logical: targets[logical] for logical in current_grouped}
    overlay = surface.overlay
    lyric_pending, _lyric_library = build_lyric_overlay.pending_songs(
        index=index, lyrics_root=ROOT / "lyrics")
    lyric_writer = incremental.writer_fingerprint(ROOT, incremental.LYRIC_FILES)
    lyric_records = {}
    lyric_rows = {}
    for bundle, row, translations in lyric_pending:
        lyric_rows[bundle] = row
        # SlotTranslations is already the accepted, song-local projection. It
        # excludes timestamps, pending rows and aggregate lyric metadata.
        lyric_records[bundle] = {
            "source_identity": incremental.source_identity(row),
            "fingerprint": incremental.digest({
                "source": incremental.source_identity(row), "writer": lyric_writer,
                "translations": sorted((position, source, zh)
                                       for (position, source), zh in translations.by_position.items()),
                "by_text": translations.by_text,
            }),
        }
    lyric_plan = incremental.plan(
        surface="lyrics", records=lyric_records, rows=lyric_rows, previous=previous,
        object_file=store.object_file, force=args.no_reuse, asset_version=version["asset_version"])
    print(json.dumps({"lyric_reuse_plan": lyric_plan.to_dict()}, ensure_ascii=False),
          file=sys.stderr)
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
        targets=lyric_plan.rebuild,
    )
    entries_path = work / "entries.json"
    event_plan, event_records = event_unit_localization.build_overlay(
        root=ROOT, index=index, work=work, overlay=overlay, version=version,
        previous=previous, store=store, downloader=download,
        cache_root=official_cache, force=args.no_reuse)
    entries = build_entries(overlay, overlay / "localization-manifest.json",
                            version["asset_version"], source_client, allow_empty=True)
    # The reused bundles were never handed to the overlay, so their entries come
    # from the release already in the store: same bytes, same digest, and the
    # store re-hashes the object before it accepts the entry.  The songs the lyric
    # pass was told to leave alone are carried over by the same rule.
    entries.extend(plan.reusable.values())
    entries.extend(lyric_plan.reusable.values())
    entries.extend(event_plan.reusable.values())
    image_bundles = 0
    image_reuse = None
    if args.reuse_published_images:
        image_entries, image_reuse = reuse_published_images.reuse(
            store=store, version=version, index=index, work=work,
            source_client_version=source_client, downloader=download, cache_root=official_cache)
        image_bundles = len(image_entries)
        entries.extend(image_entries)
    # The image surface writes its own size patch into a merged table, and the
    # alignment below has to carry that patch as well, so it patches the merged
    # table rather than the pristine one.
    catalogue_to_align = index_path
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
        # The client reads one declared size per runtime_path, so one catalogue
        # carries every surface's sizes: the image bundles join this entry list,
        # and the alignment below adds the text and lyric sizes to this table.
        entries.extend(image_entries)
        catalogue_to_align = merged_index
        merged_digest = sha256_file(merged_index)
        print(json.dumps({"image_surface": "merged_into_release",
                          "image_bundles": image_bundles,
                          "text_patch_rows": text_patch_rows,
                          "merged_index_sha256": merged_digest}, ensure_ascii=False))
    # The catalogue is aligned here, once, because this is the only point at which
    # the release's complete entry set is known: text, lyrics, images and everything
    # reuse carried over.  Aligning per surface would leave whichever surface was
    # aligned last contradicting the catalogue for all the others.
    aligned = align_index_sizes(catalogue_to_align, entries,
                                output_path=work / "catalogue" / version["index_name"],
                                object_file=store.object_file)
    entries.append(index_entry(aligned, index_name=version["index_name"],
                               pristine_sha256=index_sha256,
                               asset_version=version["asset_version"],
                               source_client_version=source_client))
    entries_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
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

    # Persist both successful outputs and verified no-output results. Source
    # parse records live in generated/build-inputs/ and are checksummed by the
    # manifest, so a fresh runner can plan without fetching unchanged bundles.
    state_entries = [dict(entry, artifact_sha256=(
        sha256_file(Path(entry["artifact_file"])) if entry.get("artifact_file")
        else entry["artifact_sha256"])) for entry in entries]
    incremental.finish_records(text_records, state_entries)
    incremental.finish_records(lyric_records, state_entries)
    incremental.finish_records(event_records, state_entries)
    reuse_record = release_reuse.reuse_block(
        index_sha256=index_sha256, resolution=resolution, scope="release")
    reuse_record[incremental.STATE_KEY] = {
        "schema": incremental.SCHEMA, "text": text_records, "lyrics": lyric_records,
        "event_units": event_records,
    }
    if image_reuse is not None:
        reuse_record["image_reuse"] = image_reuse
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
                          ("no_compatible_published_images" if args.reuse_published_images else
                           "ready_for_injection" if images_ready
                           else "blocked_missing_reviewed_inputs")),
        "official_index_sha256": index_sha256,
        "index_alignment": {
            "published_index": str(aligned.path),
            "published_index_sha256": aligned.sha256,
            # What the counts below were measured against: the official catalogue,
            # or the image surface's merged table when that surface ran.
            "aligned_from": str(catalogue_to_align),
            "catalogue_records": aligned.records,
            "covered_records": aligned.covered_records,
            "changed_records": aligned.changed_records,
            "uncovered_records": aligned.uncovered_records,
        },
        "official_objects": surface.official_objects,
        "image_reuse": image_reuse,
        "reuse": {**plan.to_dict(), "objects_written": result.objects_written,
                  "objects_deduped": result.objects_deduped,
                  "text_resolution": resolution.to_dict() if resolution else None,
                  "text_scope": text_scope,
                  "lyric_scope": "regenerated" if lyric_plan.reusable else "release",
                  "lyric_reused_bundles": len(lyric_plan.reusable),
                  "lyric_rebuilt_bundles": len(lyric_plan.rebuild),
                  "text_no_output_reused": plan.negative_reused,
                  "lyric_no_output_reused": lyric_plan.negative_reused},
        "lyrics": {key: value for key, value in lyric_summary.items() if key != "patched_bundles"},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
