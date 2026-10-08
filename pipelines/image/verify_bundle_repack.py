#!/usr/bin/env python3
"""Independent re-audit of the bundles ``inject_reviewed_textures.py`` produced.

This is the portable form of the isolated reviewed-run verifier: nothing here
trusts the injector's own report beyond the file paths and SHAs it recorded —
the source archive, the repacked archive, every Unity object and every injected
texture are re-read from disk and re-checked against the reviewed install
manifest.

Checks per audited bundle
-------------------------
* the manifest group for that ``remote`` has the declared number of rows and no
  duplicate ``texture_path_id``;
* ``source_bundle`` and ``output_bundle`` hash to the recorded SHAs;
* both archives expose the same Unity object path IDs and types;
* an object that was *not* a target is byte-identical between the two archives;
* every target Texture2D keeps its name and dimensions, its dimensions match the
  manifest, the reviewed candidate PNG still hashes to ``restored_png_sha256``,
  and the repacked texture reads back identical to that PNG.

Inputs are all required on the command line
-------------------------------------------
``--report``, ``--install-manifest`` and ``--audit`` are each named explicitly
and none of them is discovered: the reviewed cohort is the caller's assertion
about this run, and the audit's own location is the caller's choice.  No
environment variable, no report-adjacent file and no repository-relative default
is consulted, so a stale ``MLTD_IMAGE_INSTALL_MANIFEST`` or an unrelated
``repack-independent-audit.json`` beside the report can never stand in for a
named input or capture the output.

Exit status is 0 only when the audit is complete (by default: every bundle in the
install manifest was audited) and no check failed.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from PIL import Image
import UnityPy

AUDIT_KIND = "independent-reviewed-image-unity-repack-audit"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AuditRefused(ValueError):
    """The audit could not be completed; the report is not evidence."""


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def identical(left: Image.Image, right: Image.Image) -> bool:
    return left.size == right.size and np.array_equal(
        np.asarray(left.convert("RGBA")), np.asarray(right.convert("RGBA")))


def _resolve(value: Any, base: Path) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base / path)


def load_report(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, Mapping):
        records = document.get("bundles")
    elif isinstance(document, list):
        records = document
    else:
        raise AuditRefused(f"{path}: report is neither an object nor a list")
    if not isinstance(records, list) or not records:
        raise AuditRefused(f"{path}: report carries no bundles")
    if any(not isinstance(record, Mapping) for record in records):
        raise AuditRefused(f"{path}: report has a non-object bundle record")
    return dict(document), [dict(record) for record in records]


def load_manifest(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Group the reviewed install manifest by served bundle name."""
    grouped: dict[str, list[dict[str, Any]]] = collections.OrderedDict()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise AuditRefused(f"{path}:{lineno}: not JSON: {error}") from None
        if not isinstance(row, dict) or "remote" not in row:
            raise AuditRefused(f"{path}:{lineno}: manifest row has no remote")
        grouped.setdefault(str(row["remote"]), []).append(row)
    if not grouped:
        raise AuditRefused(f"{path}: no reviewed texture locators")
    return grouped


def audit_bundle(record: Mapping[str, Any], group: Sequence[Mapping[str, Any]],
                 base: Path) -> dict[str, Any]:
    remote = str(record.get("remote") or "")
    if not group:
        raise AuditRefused(
            f"{remote}: the install manifest has no reviewed row for this bundle; the audited "
            "report cannot be tied to a reviewed source cohort")
    if len(group) != int(record.get("texture_count", -1)):
        raise AuditRefused(f"{remote}: manifest rows and reported texture_count disagree")
    target = {int(row["texture_path_id"]): row for row in group}
    if len(target) != len(group):
        raise AuditRefused(f"{remote}: duplicate texture_path_id in the manifest group")

    source = _resolve(record["source_bundle"], base)
    output = _resolve(record["output_bundle"], base)
    if not source.is_file() or not output.is_file():
        raise AuditRefused(f"{remote}: source or repacked archive is missing")
    if sha(source) != record["source_sha256"] or sha(output) != record["output_sha256"]:
        raise AuditRefused(f"{remote}: archive or repack SHA does not match the audited record")

    source_env = UnityPy.load(str(source))
    output_env = UnityPy.load(str(output))
    src = {obj.path_id: obj for obj in source_env.objects}
    dst = {obj.path_id: obj for obj in output_env.objects}
    if len(src) != len(source_env.objects) or set(src) != set(dst):
        raise AuditRefused(f"{remote}: Unity object count/IDs changed")
    if not set(target) <= set(src):
        raise AuditRefused(f"{remote}: target Texture2D missing from the source archive")

    formats: collections.Counter[str] = collections.Counter()
    untouched = 0
    for path_id, obj in src.items():
        other = dst[path_id]
        if obj.type.name != other.type.name:
            raise AuditRefused(f"{remote}: Unity object type changed at {path_id}")
        if path_id not in target:
            if obj.get_raw_data() != other.get_raw_data():
                raise AuditRefused(f"{remote}: unrelated asset changed at {path_id}")
            untouched += 1
            continue
        row = target[path_id]
        src_tex = obj.read()
        dst_tex = other.read()
        if src_tex.m_Name != dst_tex.m_Name or \
                (src_tex.m_Width, src_tex.m_Height) != (dst_tex.m_Width, dst_tex.m_Height):
            raise AuditRefused(f"{remote}: Texture2D identity/dimensions changed at {path_id}")
        if (src_tex.m_Width, src_tex.m_Height) != tuple(row["original_size"]):
            raise AuditRefused(f"{remote}: Texture2D dimension/manifest mismatch at {path_id}")
        restored = Path(str(row["restored_png"]))
        recorded = str(row.get("restored_png_sha256") or "")
        if not SHA256_RE.fullmatch(recorded):
            raise AuditRefused(f"{remote}: manifest row has no restored_png_sha256")
        if sha(restored) != recorded:
            raise AuditRefused(f"{remote}: reviewed candidate PNG was modified: {restored}")
        with Image.open(restored) as candidate:
            if not identical(dst_tex.image, candidate):
                raise AuditRefused(f"{remote}: repacked Texture2D pixels do not match the "
                                   f"reviewed candidate at {path_id}")
        formats[f"{src_tex.m_TextureFormat} -> {dst_tex.m_TextureFormat}"] += 1
    return {"remote": remote, "bundle": record.get("bundle"),
            "target_textures": len(group), "untouched_objects": untouched,
            "formats": dict(formats)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_bundle_repack.py",
        description="Independent re-audit of an inject_reviewed_textures.py run: archives are "
                    "re-hashed and every target texture is re-read from disk.",
    )
    parser.add_argument("--report", required=True, type=Path,
                        help="the run report written by inject_reviewed_textures.py --report")
    parser.add_argument("--install-manifest", required=True,
                        help="reviewed texture-install manifest JSONL (required).  The cohort "
                             "this run is audited against is named here, never taken from an "
                             "environment variable or the report's own field")
    parser.add_argument("--audit", required=True, type=Path,
                        help="where to write the audit JSON (required).  The output is written "
                             "exactly here — never beside the report by default")
    parser.add_argument("--allow-partial", action="store_true",
                        help="do not require the report to cover every manifest bundle")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report_path = Path(args.report)
    if not report_path.is_file():
        print(f"REFUSED: report does not exist: {report_path}", file=sys.stderr)
        return 1
    document, records = load_report(report_path)
    manifest_path = Path(str(args.install_manifest))
    if not manifest_path.is_file():
        print(f"REFUSED: install manifest does not exist: {manifest_path}", file=sys.stderr)
        return 1

    grouped = load_manifest(manifest_path)
    base = report_path.parent
    errors: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    formats: collections.Counter[str] = collections.Counter()
    untouched = 0
    for index, record in enumerate(records, 1):
        remote = str(record.get("remote") or "")
        try:
            result = audit_bundle(record, grouped.get(remote) or [], base)
        except Exception as error:  # noqa: BLE001 - every failure is recorded, none aborts
            errors.append({"bundle": record.get("bundle"), "remote": remote,
                           "error": f"{type(error).__name__}: {str(error)[:500]}"})
            print("FAIL", index, remote, error, flush=True)
            continue
        untouched += result["untouched_objects"]
        formats.update(result["formats"])
        passed.append(result)
        print("PASS", index, remote, result["target_textures"], flush=True)

    audited = {record.get("remote") for record in records}
    missing = sorted(set(grouped) - audited)
    if missing and not args.allow_partial:
        errors.append({"error": "audited report does not cover the whole install manifest",
                       "missing_bundles": len(missing), "examples": missing[:5]})

    report = {
        "kind": AUDIT_KIND,
        "report_audited": str(report_path),
        "report_audited_sha256": sha(report_path),
        "install_manifest": str(manifest_path),
        "install_manifest_sha256": sha(manifest_path),
        "manifest_bundles": len(grouped),
        "audited_bundles": len(records),
        "passed_bundles": len(passed),
        "failed_bundles": len(errors),
        "target_textures": sum(entry["target_textures"] for entry in passed),
        "untouched_unity_objects_byte_identical": untouched,
        "texture_format_transitions": dict(formats),
        "errors": errors,
        "scope": "isolated candidate only; no formal archive or client asset mutated",
    }
    audit_path = Path(args.audit)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("AUDIT", audit_path, flush=True)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
