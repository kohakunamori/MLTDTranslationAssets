#!/usr/bin/env python3
"""Extend the frozen MLTD image-localization input manifest to NEW asset surfaces.

This is an EXTENSION of the existing pipeline (tools/mltd_image_localization/), not a
second pipeline. It reuses the frozen conventions verbatim:

  * CDN base + archive SHA/size verification and PNG export naming from
    prepare_mltd_image25_review.py (same `{path_id}_{safe_name}.png` layout, same
    `id = "<logical-without-ext>:<path_id>"` identity, same Sprite member census).
  * Output rows keep the SAME schema as work/image-localization-25/manifest.jsonl
    (id, bundle, remote, archive_sha256, type, texture_name, texture_path_id,
    original, original_sha256, original_size, original_mode, edited, review,
    sprite_members, review_status) and add only ADDITIVE keys.

It consumes only immutable, source-bound inputs: build/current-unity-assets.sqlite
(bundle_state / source_asset / unity_object) and the public asset CDN for the frozen
cohort. It never writes Unity bundles, never calls image_generation, never touches
work/image-localization-25/, and cannot print credentials (it reads no config file).

Version cohort is frozen by --cohort; do not reuse a manifest across cohorts.

Candidate-only: every written path lives under --out and is labelled candidate.
"""
from __future__ import annotations
import argparse
import concurrent.futures as futures
import json
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any

import prepare_mltd_image25_review as frozen  # CDN / sha_file / safe / label conventions

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "build/current-unity-assets.sqlite"
DEFAULT_OUT = ROOT / "work/agents/image-localization/closeout-run-d"
COHORT = "9.0.200/1077100"

# Frozen surface definitions. `match` is a regex on the bundle logical name; every
# matched bundle must also exist in bundle_state with status='ok'.
SURFACES: dict[str, dict[str, Any]] = {
    "comics_ex4c": {
        "match": r"^ex4c\d{5}(_thumb)?\.unity3d$",
        "family": "comics",
        "note": "Event comic page; 6 Texture2D per full bundle (title card + 5 panels).",
    },
    "whiteboard_exwb": {
        "match": r"^exwb\d+(_thumb)?\.unity3d$",
        "family": "whiteboard",
        "note": "Theater room whiteboard; one 512x512 texture with a _00/_01 Sprite pair.",
    },
    "hitokoma": {
        "match": r"^hitokoma(_\d+)?(_thumb)?\.unity3d$",
        "family": "hitokoma",
        "note": "One-panel comic; one Texture2D covered by one same-named full-rect Sprite.",
    },
    "talk_stamp": {
        "match": (r"^(mobile_talk_stamp_[a-z0-9]+_\d+|chat_stamp|igp_producer_stamp_common\d+)"
                  r"\.unity3d$"),
        "family": "stamp",
        "note": ("Talk/chat/producer sticker atlases. 191 mobile_talk_stamp_* (1 texture each) + "
                 "chat_stamp (3 atlas textures / 124 Sprites) + 9 igp_producer_stamp_common* = "
                 "201 bundles / 203 Texture2D, matching the frozen 203-object census."),
    },
}

# The DB stores Texture2D/Sprite metadata; per-texture geometry is read from
# normalized_json exactly like discover_sprite_layout.py does.
class Index:
    def __init__(self, db: Path):
        self.con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)

    def bundles(self, surfaces: list[str]) -> dict[str, dict[str, Any]]:
        """Return {logical_name: surface} for every matching, ok-status bundle."""
        rows = self.con.execute(
            "select logical_name,status,object_count from bundle_state").fetchall()
        found: dict[str, dict[str, Any]] = {}
        for logical, status, count in rows:
            for surface in surfaces:
                if re.match(SURFACES[surface]["match"], logical):
                    if logical in found:
                        raise ValueError("Bundle matched two surfaces: " + logical)
                    found[logical] = {"surface": surface, "status": status, "objects": count}
                    break
        return found

    def provenance(self, names: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for i in range(0, len(names), 400):
            chunk = names[i:i + 400]
            q = ("select logical_name,remote_name,archive_sha256,archive_size "
                 "from source_asset where logical_name in (%s)" % ",".join("?" * len(chunk)))
            for logical, remote, sha, size in self.con.execute(q, chunk):
                out[logical] = {"remote": remote, "archive_sha256": sha, "archive_size": size}
        missing = [n for n in names if n not in out]
        if missing:
            raise ValueError("source_asset provenance missing for %d bundles, e.g. %s"
                             % (len(missing), missing[:3]))
        return out

    def raw_hashes(self, names: list[str], types: tuple[str, ...] = ("Texture2D",)) -> dict[tuple[str, int], dict[str, Any]]:
        out = {}
        for i in range(0, len(names), 300):
            chunk = names[i:i + 300]
            q = ("select logical_name,type_name,object_name,path_id,byte_size,raw_sha256,parse_status "
                 "from unity_object indexed by idx_unity_object_key_nocase "
                 "where logical_name in (%s) and type_name in (%s)"
                 % (",".join("?" * len(chunk)), ",".join("?" * len(types))))
            for logical, tname, oname, pid, size, sha, status in self.con.execute(q, list(chunk) + list(types)):
                out[(logical, int(pid))] = {"type": tname, "object_name": oname,
                                            "byte_size": size, "raw_sha256": sha,
                                            "parse_status": status}
        return out


def fetch(row: dict[str, Any], cache: Path) -> Path:
    """Download + verify a bundle into the run-local cache (resumable)."""
    path = cache / row["remote"]
    expect_size = row["archive_size"]; expect_sha = row["archive_sha256"]
    if path.is_file():
        if path.stat().st_size == expect_size and frozen.sha_file(path).lower() == expect_sha.lower():
            return path
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    try:
        with urllib.request.urlopen(frozen.CDN + row["remote"], timeout=90) as resp, tmp.open("wb") as f:
            while chunk := resp.read(1024 * 1024):
                f.write(chunk)
        if tmp.stat().st_size != expect_size or frozen.sha_file(tmp).lower() != expect_sha.lower():
            raise ValueError("download length/SHA mismatch for " + row["remote"])
        tmp.replace(path)
        return path
    finally:
        if tmp.exists():
            tmp.unlink()


def export_bundle(logical: str, surface: str, prov: dict[str, Any], meta: dict[tuple[str, int], dict[str, Any]],
                  bundle: Path, original_root: Path) -> dict[str, Any]:
    """Export every Texture2D of one bundle as a PNG and return its manifest rows."""
    import UnityPy
    env = UnityPy.load(str(bundle))
    sprites = []
    for ob in env.objects:
        if ob.type.name == "Sprite":
            try:
                data = ob.read()
                sprites.append({"name": str(data.m_Name), "path_id": int(ob.path_id)})
            except Exception:
                continue
    sprites.sort(key=lambda s: s["name"])
    label = frozen.label(logical)
    rows = []
    for ob in env.objects:
        if ob.type.name != "Texture2D":
            continue
        obj = ob.read()
        image = obj.image
        if image is None:
            raise RuntimeError(f"{logical} path_id={ob.path_id}: Texture2D image missing")
        name = f"{int(ob.path_id)}_{frozen.safe(str(obj.m_Name))}.png"
        path = original_root / label / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            tmp = path.with_suffix(".png.writing")
            image.save(tmp, format="PNG")
            tmp.replace(path)
        width, height = image.size
        key = (logical, int(ob.path_id))
        dbmeta = meta.get(key, {})
        rows.append({
            "id": f"{label}:{int(ob.path_id)}",
            "bundle": logical,
            "remote": prov["remote"],
            "archive_sha256": prov["archive_sha256"],
            "type": "Texture2D",
            "texture_name": str(obj.m_Name),
            "texture_path_id": int(ob.path_id),
            "original": path.relative_to(ROOT).as_posix(),
            "original_sha256": frozen.sha_file(path),
            "original_size": [width, height],
            "original_mode": image.mode,
            "edited": f"edited/{label}/{name}",
            "review": f"review/{label}/{name}",
            "sprite_members": sprites,
            "review_status": "unreviewed",
            # --- additive extension keys (never present in the frozen manifest) ---
            "surface": surface,
            "cohort": COHORT,
            "raw_sha256": dbmeta.get("raw_sha256"),
            "db_object_name": dbmeta.get("object_name"),
            "db_parse_status": dbmeta.get("parse_status"),
            "artifact_status": "candidate",
        })
    return {"rows": rows, "sprites": sprites}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cache", type=Path, default=None,
                    help="Bundle cache (default <out>/bundles)")
    ap.add_argument("--surfaces", nargs="*", default=sorted(SURFACES),
                    help="Surface ids to extend (default: all four)")
    ap.add_argument("--workers", type=int, default=6, help="Download/export workers (1..8)")
    ap.add_argument("--max-bundles", type=int, default=0, help="Bound the run (0 = all)")
    ap.add_argument("--list-only", action="store_true", help="Enumerate surfaces only; no download/export")
    args = ap.parse_args()
    if not 1 <= args.workers <= 8:
        raise ValueError("--workers must be 1..8")
    unknown = [s for s in args.surfaces if s not in SURFACES]
    if unknown:
        raise ValueError("Unknown surface(s): " + ",".join(unknown))
    args.out.mkdir(parents=True, exist_ok=True)
    cache = args.cache or (args.out / "bundles")
    index = Index(args.db)

    found = index.bundles(args.surfaces)
    per_surface: dict[str, list[str]] = {s: [] for s in args.surfaces}
    for logical, info in found.items():
        if info["status"] != "ok":
            raise ValueError(f"bundle_state status={info['status']} for {logical}")
        per_surface[info["surface"]].append(logical)
    for s in per_surface:
        per_surface[s].sort()
    census = {s: {"bundles": len(v), "full": sum(not n.endswith("_thumb.unity3d") for n in v),
                  "thumb": sum(n.endswith("_thumb.unity3d") for n in v)} for s, v in per_surface.items()}
    print("SURFACES", json.dumps(census, ensure_ascii=False), flush=True)
    if args.list_only:
        (args.out / "surface-enumeration.json").write_text(json.dumps(
            {"schema_version": 1, "cohort": COHORT, "source_db": str(args.db),
             "surfaces": census, "bundles": per_surface,
             "artifact_status": "candidate"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    targets: list[tuple[str, str]] = []
    for s in args.surfaces:
        for logical in per_surface[s]:
            targets.append((s, logical))
    if args.max_bundles > 0:
        targets = targets[:args.max_bundles]
    names = [t[1] for t in targets]
    prov = index.provenance(names)
    meta = index.raw_hashes(names, types=("Texture2D", "Sprite"))
    original_root = args.out / "extracted" / "original"
    # carry over any previously exported rows so the run is resumable
    rows_path = args.out / "surface-objects.jsonl"
    known: dict[str, dict[str, Any]] = {}
    if rows_path.is_file():
        for line in rows_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                known[r["id"]] = r
    started = time.time()
    errors = []
    done = 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(fetch, {"remote": prov[n]["remote"], "archive_size": prov[n]["archive_size"],
                                    "archive_sha256": prov[n]["archive_sha256"]}, cache): (s, n)
                for s, n in targets}
        fetched: dict[str, Path] = {}
        for fut in futures.as_completed(jobs):
            s, n = jobs[fut]
            try:
                fetched[n] = fut.result()
            except Exception as exc:
                errors.append({"bundle": n, "stage": "download",
                               "error": type(exc).__name__ + ": " + str(exc)[:300]})
    print("DOWNLOADED", len(fetched), "errors", len(errors), "%.1fs" % (time.time() - started), flush=True)
    with futures.ThreadPoolExecutor(max_workers=max(1, args.workers // 2)) as pool:
        jobs = {pool.submit(export_bundle, n, s, prov[n], meta, fetched[n], original_root): (s, n)
                for s, n in targets if n in fetched}
        for fut in futures.as_completed(jobs):
            s, n = jobs[fut]
            try:
                result = fut.result()
            except Exception as exc:
                errors.append({"bundle": n, "stage": "export",
                               "error": type(exc).__name__ + ": " + str(exc)[:300]})
                continue
            for row in result["rows"]:
                known[row["id"]] = row
            done += 1
            if done % 200 == 0:
                print("EXPORTED", done, "/", len(jobs), "%.1fs" % (time.time() - started), flush=True)
    rows = sorted(known.values(), key=lambda r: (r["surface"], r["bundle"], r["texture_path_id"]))
    tmp = rows_path.with_suffix(".jsonl.writing")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(rows_path)

    # Edit-unit manifest: one row per distinct source object identity (raw_sha256),
    # keeping every source locator so one edit result can be reused safely.
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = row["raw_sha256"] or ("png:" + row["original_sha256"])
        groups.setdefault(key, []).append(row)
    units = []
    for key, members in groups.items():
        members.sort(key=lambda r: (r["bundle"], r["texture_path_id"]))
        rep = dict(members[0])
        png_shas = sorted({m["original_sha256"] for m in members})
        rep["source_locators"] = [{"id": m["id"], "bundle": m["bundle"], "remote": m["remote"],
                                   "archive_sha256": m["archive_sha256"],
                                   "texture_name": m["texture_name"],
                                   "texture_path_id": m["texture_path_id"],
                                   "surface": m["surface"], "original": m["original"]}
                                  for m in members]
        rep["locator_count"] = len(members)
        rep["distinct_png_sha_in_group"] = len(png_shas)
        units.append(rep)
    units.sort(key=lambda r: (r["surface"], r["bundle"], r["texture_path_id"]))
    manifest = args.out / "surface-manifest.jsonl"
    tmp = manifest.with_suffix(".jsonl.writing")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for row in units:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(manifest)

    summary = {"schema_version": 1, "cohort": COHORT, "source_db": str(args.db),
               "source_db_sha256": frozen.sha_file(args.db) if args.db.stat().st_size < 2**31 else None,
               "surfaces": {}, "artifact_status": "candidate",
               "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "elapsed_seconds": round(time.time() - started, 1),
               "manifest_schema_note": ("Rows keep the frozen manifest.jsonl schema; "
                                        "surface/cohort/raw_sha256/source_locators are additive."),
               "errors": errors}
    for s in args.surfaces:
        s_rows = [r for r in rows if r["surface"] == s]
        s_units = [r for r in units if r["surface"] == s]
        full_units = [r for r in s_units if not r["bundle"].endswith("_thumb.unity3d")]
        thumb_units = [r for r in s_units if r["bundle"].endswith("_thumb.unity3d")]
        summary["surfaces"][s] = {
            "bundles": len(per_surface[s]),
            "texture_objects": len(s_rows),
            "distinct_edit_units": len(s_units),
            "full_bundle_units": len(full_units),
            "thumb_bundle_units": len(thumb_units),
            "distinct_db_raw_sha": len({r["raw_sha256"] for r in s_rows if r["raw_sha256"]}),
            "distinct_png_sha": len({r["original_sha256"] for r in s_rows}),
            "duplicate_objects": len(s_rows) - len(s_units),
            "duplicate_groups": sum(1 for r in s_units if r["locator_count"] > 1),
            "groups_with_multiple_png_shas": sum(1 for r in s_units if r["distinct_png_sha_in_group"] > 1),
            "objects_without_db_raw_sha": sum(1 for r in s_rows if not r["raw_sha256"]),
            "texture_objects_missing_from_db": sum(1 for r in s_rows
                                                   if r["db_parse_status"] is None),
            "name_mismatches_db_vs_unitypy": sum(1 for r in s_rows
                                                 if r["db_object_name"] not in (None, r["texture_name"])),
            "bytes_on_disk": sum((ROOT / r["original"]).stat().st_size for r in s_rows),
        }
    (args.out / "surface-manifest-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("FINAL", json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
