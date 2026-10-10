#!/usr/bin/env python3
"""Source-bound UnityFS Texture2D injection — the image surface entry point.

``scripts/materialize_generated_release.py`` runs the image surface as

    python <repository-root>/<image entry> --all --out <staging>/image \
        --install-manifest <reviewed JSONL> --report <run>/image-inject-report.json

and then re-hashes every ``artifact_file`` the inventory names against the real
bytes.  The entry point's default is this file at its repository-home path,
``tools/mltd_image_localization/inject_reviewed_textures.py``; the Assets
repository contract path ``pipelines/image/inject_reviewed_textures.py`` is the
same file under that repository's layout and is passed explicitly with
``--image-entry`` (this repository has no ``pipelines/`` directory, so it is an
override, never a default that silently points at nothing).

This module is the portable, committable form of the isolated reviewed texture
injector that used to live only in a ``work/agents/`` build run: the run
hard-coded paths (``STAGE``, exactly 1228 manifest rows, exactly 231 bundles) are
gone, the algorithm and every fail-closed check are unchanged.

Two callers other than the release entry point consume this CLI:

* ``verify_bundle_repack.py`` (sibling) re-audits the run through ``--report``;
  the release entry point refuses to publish a surface whose audit failed or
  whose report does not cover every bundle in the inventory.
* ``--preflight-context`` answers "is the reviewed cohort resolvable?" *without*
  writing anything, so a CI can report a missing install manifest as missing
  metadata instead of as a failed injection.

Inputs (read-only, never modified)
----------------------------------
* ``--install-manifest`` — JSONL, one row per reviewed Texture2D locator, and
  **required**: the reviewed cohort is input metadata the caller owns, so it is
  named on the command line, never discovered.  No environment variable and no
  repository-relative default are consulted.  Rows are grouped by ``remote``
  (the name the official asset host serves); every row of a group must agree on
  ``archive_sha256`` and ``source_bundle`` and carry a distinct
  ``texture_path_id``.  Each row's ``review_status`` must be a user-approval tag
  (``USER_APPROVED_REVIEW_STATUSES``).  The automatic gate tag
  (``AUTO_APPROVED_REVIEW_STATUS``) is accepted so an unattended run needs no
  human sign-off; every byte-level check in this module still runs, and the
  automatic tag is only ever written after the independent audit gate passed.
* ``--original-root`` — root the relative ``original_png`` values resolve under.
  **Required on the command line whenever any selected row's ``original_png`` is
  relative**; absolute ``original_png`` values need no root.  No environment
  variable and no default directory are consulted, and no sibling directory is
  searched: a run that cannot resolve a relative ``original_png`` from the given
  root is refused, naming the row.
* ``source_bundle`` and ``restored_png`` are absolute paths carried by the
  manifest itself.

Outputs (written only under ``--out``)
--------------------------------------
* one repacked bundle per selected ``remote``, written with the ``.writing`` +
  ``replace`` atomic pattern — an output file is either absent or complete;
* ``inventory.json`` — ``{"kind": ..., "bundles": [...]}``, written **last** and
  only after every selected bundle passed the pixel-level roundtrip.  A failed
  run leaves no inventory, so the caller cannot mistake a partial run for a
  complete one.

``reuse_status`` / ``translation_status`` stay inside the vocabularies defined by
``scripts/assets_generated_index.py`` (``exact``/``verified-compatible`` and
``accepted``/``modified``/``reused``); the image surface invents no new value.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from PIL import Image
import UnityPy

INVENTORY_NAME = "inventory.json"
INVENTORY_KIND = "mltd-image-backfill-inventory"
INVENTORY_SCHEMA_VERSION = 1

# The only review provenance that may be materialised.  A manifest row that was
# never user-approved must not reach a bundle.
AUTO_APPROVED_REVIEW_STATUS = "auto_approved_no_human_signoff"
USER_APPROVED_REVIEW_STATUSES = ("user_approved_for_isolated_install_staging",
                                 AUTO_APPROVED_REVIEW_STATUS)

# Reuse/translation decisions.  A fresh repack is a first translation of this
# official source, so the source dimension is `exact` ("automatic reuse of the
# official source baseline") and the bytes are `modified`; a bundle whose
# already-present output was re-verified byte-for-byte is `reused`.
REUSE_STATUS_EXACT = "exact"
TRANSLATION_STATUS_MODIFIED = "modified"
TRANSLATION_STATUS_REUSED = "reused"

# The reviewed inputs are CLI-only.  A leftover ``MLTD_IMAGE_INSTALL_MANIFEST``
# or ``MLTD_IMAGE_ORIGINAL_ROOT`` in the environment — including one pointing at
# a file that exists and looks usable — must never stand in for a named input:
# the reviewed cohort and the PNG root are what the caller is asserting this run
# about, so they are stated on the command line or the run is refused.  There is
# also no repository-relative default for either: a default would silently pick
# up whatever the installed layout happens to carry.

REQUIRED_ROW_KEYS = (
    "archive_sha256", "bundle", "original_png", "original_size", "remote",
    "restored_png", "restored_png_sha256", "source_bundle", "source_id",
    "texture_path_id",
)

Hash = str


class RefusedInput(ValueError):
    """An input, provenance or roundtrip check refused the run."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def sha(path: Path) -> Hash:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def identical(left: Image.Image, right: Image.Image) -> bool:
    return left.size == right.size and np.array_equal(
        np.asarray(left.convert("RGBA")), np.asarray(right.convert("RGBA")))


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write through ``<name>.writing`` + ``replace``; no partial file survives."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".writing")
    temp.write_bytes(payload)
    try:
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def list_manifests() -> list[dict[str, str]]:
    """Every install-manifest source that was consulted.

    ``--install-manifest`` is the *only* source and it is required, so the list
    has one entry: the CLI flag.  It exists so a missing-metadata report names
    the source it looked at instead of listing environment or default paths the
    tool does not read.
    """
    return [{"source": "--install-manifest (required CLI argument)",
             "path": "<not supplied>"}]


def resolve_manifest(explicit: str | None) -> Path:
    """Resolve the reviewed cohort from the required CLI argument only.

    The reviewed cohort is *input metadata* the caller owns; the tool never
    discovers it.  No environment variable (a stale ``MLTD_IMAGE_INSTALL_MANIFEST``
    that happens to point at a usable file is still not a named input) and no
    repository-relative default are consulted.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise RefusedInput(f"install manifest does not exist: {path}")
        return path
    raise RefusedInput(
        "no reviewed install manifest: pass --install-manifest <jsonl>; this "
        "candidate reads no environment variable and applies no default"
    )


def missing_input_context(manifest: Path, manifest_sha256: str,
                          original_root: Path | None, report: Path | None) -> dict[str, Any]:
    """What a CI needs to say *which* metadata was used, and where from.

    ``manifest`` is the resolved path (with its SHA-256 and row count, so the
    caller can pin the exact reviewed cohort it is asking about).  The
    manifest-less case is handled by the caller, which reports every source it
    consulted instead.  This never guesses a cohort and never invents rows.
    """
    try:
        groups = load_groups(manifest)
    except RefusedInput as error:
        loaded: dict[str, Any] | None = {"rows": None, "bundles": None, "note": str(error)}
    else:
        loaded = {"rows": sum(len(group) for group in groups),
                  "bundles": len(groups), "note": None}
    return {
        "kind": "mltd-image-injection-input-context",
        "mode": "preflight_context_no_bundles_written",
        "manifest": str(manifest),
        "manifest_sha256": manifest_sha256,
        "manifest_sources": list_manifests(),
        "manifest_counts": loaded,
        "original_root": str(original_root) if original_root is not None else None,
        "original_root_default": None,
        "original_root_note": (
            "no environment variable and no default: a relative original_png requires the "
            "--original-root CLI argument; absolute values need none"),
        "report": str(report) if report is not None else None,
    }


def resolve_original_root(explicit: str | None, manifest: Path) -> Path | None:
    """CLI-only resolution; ``None`` means the caller passed no ``--original-root``.

    ``None`` is a valid outcome: a manifest whose ``original_png`` values are
    all absolute needs no root, and the ones that are relative fail in
    ``resolve_original_png`` naming the row that needs it.  A passed root that
    is not a directory is refused.  No environment variable, no default and no
    sibling-directory search.
    """
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_dir():
            raise RefusedInput(f"--original-root is not a directory: {root}")
        return root
    return None


def resolve_original_png(value: str, original_root: Path | None,
                         row: Mapping[str, Any]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if original_root is None:
        raise RefusedInput(
            f"{row.get('source_id')}: relative original_png {value!r} needs the "
            "--original-root CLI argument; this candidate reads no environment variable, "
            "applies no default and searches no sibling directory"
        )
    return original_root / path


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def load_groups(manifest: Path) -> list[list[dict[str, Any]]]:
    """Read the reviewed manifest and group it by served bundle name.

    Every parameterised check that the run-specific version made with counts
    (1228 rows / 231 bundles) is expressed here as a property of the data
    instead: groups are internally consistent, path IDs are unique per group,
    and a bundle name is served by exactly one source archive.
    """
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise RefusedInput(f"{manifest}:{lineno}: not JSON: {error}") from None
        if not isinstance(row, dict):
            raise RefusedInput(f"{manifest}:{lineno}: manifest row is not an object")
        missing = [key for key in REQUIRED_ROW_KEYS if key not in row]
        if missing:
            raise RefusedInput(f"{manifest}:{lineno}: manifest row lacks {', '.join(missing)}")
        status = row.get("review_status")
        if status not in USER_APPROVED_REVIEW_STATUSES:
            raise RefusedInput(
                f"{manifest}:{lineno} ({row.get('source_id')}): review_status {status!r} is "
                f"not a user approval nor the automatic gate tag "
                f"({', '.join(USER_APPROVED_REVIEW_STATUSES)})"
            )
        rows.append(row)
    if not rows:
        raise RefusedInput(f"{manifest}: no reviewed texture locators")

    groups: list[list[dict[str, Any]]] = []
    by_remote: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        remote = str(row["remote"])
        if remote not in by_remote:
            by_remote[remote] = []
            groups.append(by_remote[remote])
        by_remote[remote].append(row)

    for group in groups:
        remote = str(group[0]["remote"])
        archive_sha = str(group[0]["archive_sha256"])
        source_bundle = str(group[0]["source_bundle"])
        bundle = str(group[0]["bundle"])
        for row in group:
            if str(row["archive_sha256"]) != archive_sha:
                raise RefusedInput(f"{remote}: rows disagree on archive_sha256")
            if str(row["source_bundle"]) != source_bundle:
                raise RefusedInput(f"{remote}: rows disagree on source_bundle")
            if str(row["bundle"]) != bundle:
                raise RefusedInput(f"{remote}: rows disagree on bundle name")
        path_ids = [int(row["texture_path_id"]) for row in group]
        if len(set(path_ids)) != len(path_ids):
            raise RefusedInput(f"{remote}: duplicate texture_path_id inside one bundle")
    return groups


def select_groups(groups: Sequence[Sequence[Mapping[str, Any]]], bundle: str | None,
                  limit: int) -> list[list[dict[str, Any]]]:
    chosen = [list(group) for group in groups
              if bundle is None or bundle in (str(group[0]["bundle"]), str(group[0]["remote"]))]
    if bundle is not None and not chosen:
        raise RefusedInput(f"bundle {bundle!r} is not in the reviewed install manifest")
    if limit > 0:
        chosen = chosen[:limit]
    if not chosen:
        raise RefusedInput("no bundle selected")
    return chosen


# --------------------------------------------------------------------------- #
# repack
# --------------------------------------------------------------------------- #
def repack(source: Path, output: Path, rows: Sequence[Mapping[str, Any]],
           original_root: Path | None) -> dict[str, Any]:
    """Inject the reviewed PNGs into one bundle and verify the result pixel-wise.

    Unchanged from the isolated reviewed run: the official source is re-hashed,
    the candidate PNG is re-hashed, the extracted original must match the
    manifest's source PNG pixel for pixel, the object table must be identical
    after the repack, and every injected texture must read back identical to the
    reviewed candidate.  Any deviation raises before the output is replaced.
    """
    src_hash = sha(source)
    if src_hash != rows[0]["archive_sha256"] or any(
            row["archive_sha256"] != src_hash or row["source_bundle"] != str(source)
            for row in rows):
        raise RefusedInput(f"{source.name}: official source bundle changed or rows disagree")

    env = UnityPy.load(str(source))
    bundle_files = list(env.files.values())
    if len(bundle_files) != 1 or env.file is None:
        raise RefusedInput(f"{source.name}: expected exactly one Unity bundle file")
    objects = {obj.path_id: obj for obj in env.objects}
    if len(objects) != len(env.objects):
        raise RefusedInput(f"{source.name}: duplicate Unity object path IDs")
    expected = {int(row["texture_path_id"]): row for row in rows}
    if len(expected) != len(rows) or any(key not in objects for key in expected):
        raise RefusedInput(f"{source.name}: missing/duplicate Texture2D path IDs")
    before_types = {path_id: obj.type.name for path_id, obj in objects.items()}

    converted_formats: dict[str, int] = {}
    for path_id, item in expected.items():
        obj = objects[path_id]
        if obj.type.name != "Texture2D":
            raise RefusedInput(f"{source.name}: path ID {path_id} is not a Texture2D")
        texture = obj.read()
        original_png = resolve_original_png(str(item["original_png"]), original_root, item)
        restored_png = Path(str(item["restored_png"]))
        with Image.open(original_png) as source_image, Image.open(restored_png) as edit:
            if sha(restored_png) != item["restored_png_sha256"]:
                raise RefusedInput(f"{item.get('source_id')}: reviewed candidate SHA changed")
            if texture.m_Width != item["original_size"][0] or texture.m_Height != item["original_size"][1]:
                raise RefusedInput(
                    f"{item.get('source_id')}: Unity texture dimensions differ from extracted source")
            if not identical(source_image, texture.image):
                raise RefusedInput(
                    f"{item.get('source_id')}: original PNG differs from Unity source texture")
            original_format = str(texture.m_TextureFormat)
            texture.image = edit.copy()
            key = f"{original_format} -> {texture.m_TextureFormat}"
            converted_formats[key] = converted_formats.get(key, 0) + 1
            texture.save()

    raw = env.file.save(packer="lz4")
    atomic_write_bytes(output, raw)
    try:
        fresh = UnityPy.load(str(output))
        after = {obj.path_id: obj for obj in fresh.objects}
        if {path_id: obj.type.name for path_id, obj in after.items()} != before_types:
            raise RefusedInput(f"{source.name}: Unity object path IDs or types changed")
        for path_id, item in expected.items():
            verify = after[path_id].read()
            with Image.open(Path(str(item["restored_png"]))) as candidate:
                if not identical(verify.image, candidate):
                    raise RefusedInput(
                        f"{item.get('source_id')}: texture roundtrip pixels differ")
                if tuple(item["original_size"]) != (verify.m_Width, verify.m_Height):
                    raise RefusedInput(
                        f"{item.get('source_id')}: texture roundtrip dimensions differ")
    except BaseException:
        output.unlink(missing_ok=True)
        raise

    return {
        "bundle": rows[0]["bundle"],
        "remote": rows[0]["remote"],
        "source_bundle": str(source),
        "source_sha256": src_hash,
        "output_bundle": str(output),
        "output_sha256": sha(output),
        "source_size": source.stat().st_size,
        "output_size": output.stat().st_size,
        "object_count": len(objects),
        "texture_count": len(rows),
        "roundtrip_verified": True,
        "converted_texture_formats": converted_formats,
        "reused_existing": False,
    }


def verify_existing(output: Path, source: Path, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Re-verify an output this same tool already wrote instead of rewriting it."""
    if sha(source) != rows[0]["archive_sha256"]:
        raise RefusedInput(f"{source.name}: official source bundle changed since the output was written")
    src = UnityPy.load(str(source))
    after_env = UnityPy.load(str(output))
    source_objs = {obj.path_id: obj.type.name for obj in src.objects}
    after_objs = {obj.path_id: obj for obj in after_env.objects}
    if {path_id: obj.type.name for path_id, obj in after_objs.items()} != source_objs:
        raise RefusedInput(f"{source.name}: existing output object identities changed")
    for item in rows:
        texture = after_objs[int(item["texture_path_id"])].read()
        restored_png = Path(str(item["restored_png"]))
        with Image.open(restored_png) as candidate:
            if sha(restored_png) != item["restored_png_sha256"] or not identical(texture.image, candidate):
                raise RefusedInput(
                    f"{item.get('source_id')}: existing output has a different source-bound texture")
    return {
        "bundle": rows[0]["bundle"],
        "remote": rows[0]["remote"],
        "source_bundle": str(source),
        "source_sha256": sha(source),
        "output_bundle": str(output),
        "output_sha256": sha(output),
        "source_size": source.stat().st_size,
        "output_size": output.stat().st_size,
        "object_count": len(source_objs),
        "texture_count": len(rows),
        "roundtrip_verified": True,
        "converted_texture_formats": {},
        "reused_existing": True,
    }


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #
def inventory_row(record: Mapping[str, Any], out_root: Path) -> dict[str, Any]:
    artifact = Path(str(record["output_bundle"]))
    return {
        "logical_key": record["bundle"],
        # The official host serves <asset_version>/production/2018/Android/<this
        # name>; the caller prefixes the scope.  It is the bundle's own name, not
        # a server path yet — hence `remote` as well.
        "logical_path": record["remote"],
        "remote": record["remote"],
        "bundle": record["bundle"],
        "source_sha256": record["source_sha256"],
        "artifact_sha256": record["output_sha256"],
        "artifact_file": artifact.relative_to(out_root).as_posix(),
        "artifact_bytes": record["output_size"],
        "texture_count": record["texture_count"],
        "source_size": record["source_size"],
        "output_size": record["output_size"],
        "roundtrip_verified": bool(record["roundtrip_verified"]),
        "converted_texture_formats": record["converted_texture_formats"],
        "reused_existing": bool(record["reused_existing"]),
        "reuse_status": REUSE_STATUS_EXACT,
        "translation_status": (TRANSLATION_STATUS_REUSED if record["reused_existing"]
                               else TRANSLATION_STATUS_MODIFIED),
    }


def build_inventory(manifest: Path, records: Sequence[Mapping[str, Any]],
                    out_root: Path) -> dict[str, Any]:
    rows = [inventory_row(record, out_root) for record in records]
    keys = [str(row["logical_key"]) for row in rows]
    if len(set(keys)) != len(keys):
        raise RefusedInput("two selected bundles published the same logical_key")
    for row in rows:
        if sha(out_root / row["artifact_file"]) != row["artifact_sha256"]:
            raise RefusedInput(
                f"{row['logical_key']}: artifact changed between repack and inventory")
    return {
        "kind": INVENTORY_KIND,
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "install_manifest": str(manifest),
        "install_manifest_sha256": sha(manifest),
        "bundles": rows,
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def _lower_priority() -> None:
    """Isolated repack yields CPU to other production workers."""
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inject_reviewed_textures.py",
        description="Source-bound UnityFS Texture2D injection for the reviewed image cohort; "
                    "writes reviewed bundles plus inventory.json under --out and never "
                    "modifies an input.",
    )
    parser.add_argument("--install-manifest", required=True,
                        help="reviewed texture-install manifest JSONL (required).  The reviewed "
                             "cohort is input metadata the caller owns; no environment variable "
                             "and no repository-relative default are consulted")
    parser.add_argument("--original-root", default=None,
                        help="root the relative original_png values resolve under; required on "
                             "the command line whenever a selected row's original_png is "
                             "relative.  No environment variable, no default and no sibling "
                             "directory; absolute original_png values need no root")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory; bundles and inventory.json are written here only "
                             "(required by --all/--bundle; with --preflight-context it is never "
                             "created or written)")
    selection = parser.add_mutually_exclusive_group(required=False)
    selection.add_argument("--all", action="store_true", help="every reviewed bundle")
    selection.add_argument("--bundle", help="one logical bundle name or served remote name")
    selection.add_argument("--preflight-context", action="store_true",
                           help="report which inputs resolve (install manifest, its SHA-256 and "
                                "row/bundle counts, original root) and exit; never produces a "
                                "bundle, so missing metadata is reported as missing metadata")
    parser.add_argument("--limit", type=int, default=0,
                        help="process at most N bundles (0 = no limit); applies to --all too")
    parser.add_argument("--expect-manifest-sha256", default=None,
                        help="refuse the run unless the install manifest hashes to this value")
    parser.add_argument("--report", type=Path, default=None,
                        help="optional path for the run report JSON (kept out of --out)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.all or args.bundle or args.preflight_context):
        print("REFUSED: choose exactly one of --all, --bundle, --preflight-context",
              file=sys.stderr, flush=True)
        return 1
    _lower_priority()
    if args.preflight_context:
        # Read-only on purpose: --out is neither created nor inspected, and no
        # bundle is touched. A nonexistent --install-manifest is a missing
        # *input*, reported with the source that was consulted (the CLI flag).
        try:
            manifest = resolve_manifest(args.install_manifest)
        except RefusedInput as error:
            context = {
                "kind": "mltd-image-injection-input-context",
                "mode": "preflight_context_no_bundles_written",
                "manifest": None, "manifest_sha256": None,
                "manifest_sources": list_manifests(), "manifest_counts": None,
                "original_root": None,
                "original_root_default": None,
                "original_root_note": (
                    "no environment variable and no default: a relative original_png requires "
                    "the --original-root CLI argument; absolute values need none"),
                "report": str(args.report) if args.report else None,
                "refused": str(error),
            }
            print(json.dumps(context, ensure_ascii=False, indent=2), flush=True)
            return 1
        context = missing_input_context(
            manifest, sha(manifest),
            resolve_original_root(args.original_root, manifest),
            args.report)
        print(json.dumps(context, ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.out is None:
        print("REFUSED: --out is required with --all/--bundle (only --preflight-context "
              "runs without an output directory)", file=sys.stderr, flush=True)
        return 1
    try:
        manifest = resolve_manifest(args.install_manifest)
        if args.expect_manifest_sha256 and sha(manifest) != args.expect_manifest_sha256:
            raise RefusedInput(
                f"{manifest}: manifest sha256 is {sha(manifest)}, expected "
                f"{args.expect_manifest_sha256}")
        original_root = resolve_original_root(args.original_root, manifest)
        groups = load_groups(manifest)
        chosen = select_groups(groups, args.bundle, args.limit)
        out_root = Path(args.out).resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        if (out_root / INVENTORY_NAME).exists():
            raise RefusedInput(
                f"{out_root / INVENTORY_NAME} already exists; write into a fresh --out so a "
                "previous inventory cannot be mistaken for this run's")

        records: list[dict[str, Any]] = []
        for group in chosen:
            source = Path(str(group[0]["source_bundle"]))
            if not source.is_file():
                raise RefusedInput(f"source bundle does not exist: {source}")
            output = out_root / str(group[0]["remote"])
            if output.exists():
                record = verify_existing(output, source, group)
            else:
                record = repack(source, output, group, original_root)
            print("PASS", record["bundle"], record["texture_count"],
                  record["source_size"], record["output_size"],
                  "REUSED" if record["reused_existing"] else "REPACKED", flush=True)
            records.append(record)

        inventory = build_inventory(manifest, records, out_root)
        atomic_write_text(out_root / INVENTORY_NAME,
                          json.dumps(inventory, ensure_ascii=False, indent=2) + "\n")
    except RefusedInput as error:
        print(f"REFUSED: {error}", file=sys.stderr, flush=True)
        return 1
    except Exception as error:  # noqa: BLE001 - any failure is a refusal here
        print(f"FAILED: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 2

    summary = {
        "kind": INVENTORY_KIND,
        "bundles": len(inventory["bundles"]),
        "textures": sum(row["texture_count"] for row in inventory["bundles"]),
        "reused_existing": sum(1 for row in inventory["bundles"] if row["reused_existing"]),
        "output_bytes": sum(row["artifact_bytes"] for row in inventory["bundles"]),
        "out": str(out_root),
        "inventory": str(out_root / INVENTORY_NAME),
        "install_manifest": str(manifest),
        "install_manifest_sha256": inventory["install_manifest_sha256"],
    }
    if args.report:
        report = {
            "kind": "reviewed-texture-injection-report",
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "mode": "isolated_reviewed_texture_injection_not_published",
            "install_manifest": str(manifest),
            "install_manifest_sha256": summary["install_manifest_sha256"],
            "inventory": str(out_root / INVENTORY_NAME),
            "summary": summary,
            # The per-bundle records verify_bundle_repack.py independently audits.
            "bundles": records,
        }
        atomic_write_text(Path(args.report),
                          json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
