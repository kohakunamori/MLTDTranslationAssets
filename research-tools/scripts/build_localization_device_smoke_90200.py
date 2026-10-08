#!/usr/bin/env python3
"""Isolated UNREVIEWED 9.0.200/1077100 device-smoke bundle/index rehearsal.

NOT a release, NEVER NAS/deploy: deliberately includes machine translations,
historical Traditional Chinese, and unreviewed Event-unit / FontRender QA.
The production assembler and its independent review gates stay intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

import msgpack

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.build_event_unit_review_pack import sha_file

BUILD = ROOT / "build/localization-90200"
IMAGE_ROOT = ROOT / "work/agents/image-localization/reviewed937-texture-stage"
INDEX = ROOT / "work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
IMAGE = IMAGE_ROOT / "frozen-1077100-direct-overlay-manifest.json"
GTX = BUILD / "overlay/localization-manifest.json"
EVENT = BUILD / "staging-event-unit-qa-with-tail/event-unit-QA-candidate-manifest.json"
EVENT55 = BUILD / "staging-event-unit-combined-55-unreviewed-drafts/manifest.json"
FONT = BUILD / "staging-fontrender/fontrender-candidate-manifest.json"
ORIGINAL_QA = BUILD / "staging-event-unit-qa-with-tail/event-unit-QA-candidate-audit.json"
DEST = BUILD / "device-smoke-UNREVIEWED-client-9.0.200-assets-1077100"
IDX_SHA = "d7631544b9b1c7c0bea2729fbfdb688bf61d612a695be8233b36082738201e01"
IMAGE_SHA = None   # Source manifest digest is pinned into new report and byte-checks all records.
EVENT_SHA = "c17a4933eed0f0e8d9b1f690b3b8738c36ebed43a87558831391d4db2fea8a63"
EVENT55_SHA = "8c6af74fcbf70e44c1b56f6f3ea02188fd2107bad0c1fc9f0b3369d2f7e49ff5"
NAME_RE = re.compile(r"[0-9a-f]{40}\.unity3d\Z")
SURFACE_COUNTS = {"gtx": 11817, "image": 231, "event_unit": 852, "fontrender": 9}

def local(value: str | Path) -> Path:
    p = Path(value)
    return p.resolve() if p.is_absolute() else (ROOT / p).resolve()

def attest(path: Path, sha: str, size: int | None = None) -> int:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    n = path.stat().st_size
    if size is not None and n != size:
        raise ValueError(f"source/candidate byte size differs: {path}")
    if sha_file(path) != sha:
        raise ValueError(f"candidate sha differs: {path}")
    return n

def inputs() -> tuple[dict, dict, list[dict], dict]:
    if sha_file(INDEX) != IDX_SHA:
        raise ValueError("frozen 1077100 asset index changed")
    table = msgpack.unpackb(INDEX.read_bytes(), raw=False, strict_map_key=False)
    if not isinstance(table, list) or len(table) != 1 or len(table[0]) != 166701:
        raise ValueError("unexpected frozen index shape")
    old = table[0]
    manifest_paths = {"image": IMAGE, "gtx": GTX, "event_unit": EVENT,
                      "followup55": EVENT55, "fontrender": FONT}
    manifests = {name: json.loads(p.read_text(encoding="utf8"))
                 for name, p in manifest_paths.items()}
    im, gt, ev, delta, font = (manifests[key] for key in
                              ("image", "gtx", "event_unit", "followup55", "fontrender"))
    if (sha_file(EVENT) != EVENT_SHA or sha_file(EVENT55) != EVENT55_SHA
        or im.get("original_manifest_sha256") != IDX_SHA
        or im.get("asset_version") != "1077100"
        or im.get("status") != "staged_not_published"
        or im.get("image_bundles") != 231 or im.get("image_textures") != 1228
        or gt.get("version_identity", {}).get("version_key") !=
            "jp-client-9.0.200-assets-1077100"
        or gt.get("bundles_written") != 11817
        or gt.get("records_changed") != 388438
        or gt.get("resolved") != gt.get("source_candidates")
        or ev.get("bundles_scanned", ev.get("source_bundles")) != 852 or ev.get("bundles_written") != 852
        or ev.get("text_fields_changed") != 12204
        or ev.get("safe_to_mount_as_final_overlay") is not False
        or delta.get("unreviewed_candidate_source_unique") != 55
        or delta.get("bundle_count") != 51
        or delta.get("independent_review_complete") is not False
        or delta.get("safe_to_mount_as_final_overlay") is not False
        or font.get("bundles_written") != 9
        or font.get("safe_to_mount_as_final_overlay") is not False):
        raise ValueError("original and unreviewed surface manifests differ")
    if (ev.get("version_identity") != gt.get("version_identity")
        or delta.get("version_identity") != gt.get("version_identity")
        or font.get("version_identity") != gt.get("version_identity")):
        raise ValueError("client + assets version identity differs")
    records = []
    def add(surface: str, logical: str, remote: str, original_sha: str,
            candidate_sha: str, candidate_path: Path, candidate_bytes: int | None):
        if (not NAME_RE.fullmatch(remote) or
            candidate_path.name != remote or
            not isinstance(logical, str) or
            logical not in old or old[logical][1] != remote or
            not isinstance(candidate_sha, str) or len(candidate_sha) != 64):
            raise ValueError(f"unsafe or stale original source mapping: {logical} {remote}")
        if not isinstance(original_sha, str) or len(original_sha) != 64:
            raise ValueError("source SHA missing")
        if not candidate_path.is_file():
            raise FileNotFoundError(str(candidate_path))
        size = candidate_path.stat().st_size
        if candidate_bytes is not None and size != candidate_bytes:
            raise ValueError(f"candidate byte size differs before staging: {candidate_path}")
        records.append({
            "surface": surface, "logical": logical, "remote": remote,
            "old_bytes": old[logical][2], "localized_bytes": size,
            "original_sha256": original_sha,
            "localized_sha256": candidate_sha, "localized_source": str(candidate_path),
        })
    for r in gt["bundles"]:
        if (r["remote"] != local(r["output_path"]).name or
            local(r["source_path"]).name != r["remote"] or
            local(r["source_path"]).stat().st_size != old[r["logical"]][2]):
            raise ValueError("GTX origin or output remote changed")
        add("gtx", r["logical"], r["remote"], r["source_bundle_sha256"],
            r["output_bundle_sha256"], local(r["output_path"]), r["output_bytes"])
    approved_image_root = local(im["overlay_root"]) / "jp-android"
    for r in im["records"]:
        src = local(r["source_file"])
        if (src != IMAGE_ROOT / "stream-optimized-exact-cab-bundles" / r["remote"]
            or not (approved_image_root / r["remote"]).samefile(src)
            or r["original_bytes"] != old[r["logical"]][2]):
            raise ValueError("not approved ASTC/image bytes or original remote")
        add("image", r["logical"], r["remote"], r["original_sha256"],
            r["translated_sha256"], src, r["translated_bytes"])
    delta_by_remote = {r["remote"]: r for r in delta["bundles"]}
    if len(delta_by_remote) != 51:
        raise ValueError("duplicate 55-draft remote")
    event_remotes = set()
    for r in ev["bundles"]:
        remote = r["remote"]
        if remote in event_remotes:
            raise ValueError("duplicate 852 original event remote")
        event_remotes.add(remote)
        final = delta_by_remote.get(remote)
        if final is not None:
            if (final["logical"] != r["logical"]
                or final["original_bundle_sha256"] != r["source_sha256"]
                or final["baseline_852_bundle_sha256"] != r["localized_sha256"]
                or final.get("roundtrip_verified") is not True
                or final.get("non_text_objects_byte_identical") is not True):
                raise ValueError("55-source source-bound Event-unit overlay drift")
            new_path = BUILD / "staging-event-unit-combined-55-unreviewed-drafts/jp-android" / remote
            new_sha = final["localized_sha256"]
            size = final["output_bytes"]
        else:
            new_path = BUILD / "staging-event-unit-qa-with-tail/jp-android" / remote
            new_sha = r["localized_sha256"]
            size = r["output_bytes"]
        if (r["original_bytes"] != old[r["logical"]][2]
            or r.get("roundtrip_verified") is not True
            or r.get("non_text_objects_byte_identical") is not True):
            raise ValueError("original Event-unit bundle/index/roundtrip drift")
        add("event_unit", r["logical"], remote, r["source_sha256"],
            new_sha, new_path, size)
    if set(delta_by_remote) - event_remotes:
        raise ValueError("55-source extra remote is not in frozen 852 cohort")
    for r in font["bundles"]:
        if (not r.get("roundtrip_verified")
            or local(r["source_path"]).stat().st_size != old[r["logical"]][2]):
            raise ValueError("FontRender original/roundtrip drift")
        add("fontrender", r["logical"], r["remote"], r["source_sha256"],
            r["output_sha256"], local(r["output_path"]), None)
    counts = Counter(r["surface"] for r in records)
    if dict(counts) != SURFACE_COUNTS:
        raise ValueError(f"surface count mismatch: {counts}")
    if (len(records) != 12909
        or len({r["remote"] for r in records}) != len(records)
        or len({r["logical"] for r in records}) != len(records)):
        raise ValueError("cross-surface duplicate remote/logical, do not overwrite")
    return old, manifests, records, {
        name: sha_file(path) for name, path in manifest_paths.items()
    }

def prepare_index(old: dict, records: list[dict]) -> bytes:
    updated = {logical: fields.copy() for logical, fields in old.items()}
    for row in records:
        before = updated[row["logical"]]
        if before[1] != row["remote"] or before[2] != row["old_bytes"]:
            raise ValueError("index source length or name changed")
        before[2] = row["localized_bytes"]
    changed_logicals = {r["logical"] for r in records}
    for key, entry in updated.items():
        old_entry = old[key]
        if entry[:2] != old_entry[:2] or (
            key not in changed_logicals and entry != old_entry):
            raise ValueError("asset index unrelated record changed")
    raw = msgpack.packb([updated], use_bin_type=True)
    if msgpack.unpackb(raw, raw=False, strict_map_key=False) != [updated]:
        raise ValueError("updated MsgPack index roundtrip failed")
    return raw

def build(dest: Path = DEST) -> dict:
    dest = dest.resolve()
    if not dest.is_relative_to(BUILD.resolve()):
        raise ValueError("isolated smoke must be under build/localization-90200")
    if dest.exists():
        raise FileExistsError(f"cannot overwrite existing smoke build: {dest}")
    temp = dest.with_name(dest.name + ".incomplete")
    if temp.exists():
        raise FileExistsError(f"unfinished smoke build: {temp}")
    old, manifests, records, manifests_sha = inputs()
    packed = prepare_index(old, records)
    index_sha = hashlib.sha256(packed).hexdigest()
    root = temp / "jp-android"
    root.mkdir(parents=True)
    try:
        for row in records:
            out = root / row["remote"]
            shutil.copyfile(row["localized_source"], out)
            attest(out, row["localized_sha256"], row["localized_bytes"])
        candidate_index = root / INDEX.name
        candidate_index.write_bytes(packed)
        attest(candidate_index, index_sha, len(packed))
        report = {
            "schema_version": 1,
            "kind": "MLTD-device-smoke-unreviewed-DO-NOT-DEPLOY",
            "client_version": "9.0.200",
            "assets_version": "1077100",
            "same_original_remote_names": True,
            "original_index_name": INDEX.name,
            "original_index_sha256": IDX_SHA,
            "smoke_index_sha256": index_sha,
            "smoke_index_bytes": len(packed),
            "index_entries": len(old),
            "changed_index_lengths": len(records),
            "untouched_index_records": len(old) - len(records),
            "surface_bundles": SURFACE_COUNTS,
            "total_bundle_count": len(records),
            "gtx_changed_text_records_prior_stage": 388438,
            "event_unit_existing_QA_fields": 12204,
            "event_unit_unreviewed_targeted_source_drafts": 55,
            "event_unit_unreviewed_targeted_bundle_overrides": 51,
            "font_render_QA_review_unique": 7,
            "images_accepted_texture_count": 1228,
            "source_manifest_sha256": manifests_sha,
            "smoke_bundle_bytes_total": sum(r["localized_bytes"] for r in records),
            "source_archive_modified": False,
            "production_translation_files_modified": False,
            "prior_QA_stages_modified": False,
            "nas_modified": False,
            "official_original_assets_modified": False,
            "client_apk_modified": False,
            "includes_bootstrap_BI_or_MLD": False,
            "independent_semantic_review_complete": False,
            "safe_to_deploy_to_NAS": False,
            "safe_to_publish": False,
            "real_client_verified": False,
            "warning": (
                "TECHNICAL SMOKE ONLY. Includes 79970 historical Traditional Chinese "
                "seed values and unreviewed machine/event/font translations. Not a "
                "complete Simplified Chinese product; does NOT include APK BI/MD.mld. "
                "Do not switch NAS current or expose this via official-server routing."
            ),
        }
        (temp / "SMOKE-DO-NOT-DEPLOY.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf8",
        )
        # Output file listing is compact and stable for individual on-device
        # missing-bundle diagnostics; keep manifest separate from asset scope.
        with (temp / "smoke-file-map.jsonl").open("w", encoding="utf8", newline="\n") as f:
            for row in records:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        temp.rename(dest)
    except BaseException:
        # Preserve incomplete evidence for diagnosis; never claim success.
        raise
    return report

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-root", type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(build(args.stage_root), ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

