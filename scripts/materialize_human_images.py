"""Repack explicitly approved PNG sources without invoking an image model."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from build_generated_release import download, load_official_index, load_version_manifest
from pipelines.core.hashing import sha256_file
from publish_image_overlay import export

ROOT = Path(__file__).resolve().parents[1]
REVIEW = "user_approved_for_isolated_install_staging"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def approved_sources(root, approval):
    """Validate human provenance and immutable inputs before any repack."""
    if (approval.get("review_status") != REVIEW
            or approval.get("approval", {}).get("kind") != "explicit_user_approval"
            or not approval.get("textures")):
        raise ValueError("Explicit human approval and textures are required")
    files = [(approval["approval"]["input"], approval["approval"]["input_sha256"])]
    files += [(t["localized"], t["sha256"]) for t in approval["textures"]]
    for name, digest in files:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or sha256_file(path) != digest:
            raise ValueError(f"Approved image input changed: {name}")


def materialize(root, work):
    manifest = root / "manifests/image-human-approvals.json"
    if not manifest.exists():
        return 0
    approvals = read(manifest)["bundles"]
    for approval in approvals:
        approved_sources(root, approval)
    version = load_version_manifest(root / "manifests/asset-version.json")
    release_path = root / "generated" / version["asset_version"] / "manifest.json"
    published = {e["logical_key"]: e for e in read(release_path).get("entries", [])} if release_path.exists() else {}
    pending = [a for a in approvals if not (
        a.get("repacked_sha256")
        and published.get(a["logical"], {}).get("artifact_sha256") == a["repacked_sha256"]
        and published[a["logical"]].get("source_sha256") == a["archive_sha256"])]
    if not pending:
        print("Human-approved images already published; no repack needed")
        return 0
    work.mkdir(parents=True, exist_ok=True)
    index_path = work / version["index_name"]
    download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    index = load_official_index(index_path)
    overlay = root / "image-overlay" / version["asset_version"]
    if (overlay / "overlay-manifest.json").exists():
        # Never discard an automatic batch awaiting its first unified release.
        manual_keys = {a["logical"] for a in pending}
        for record in read(overlay / "overlay-manifest.json")["records"]:
            if (record["logical"] not in manual_keys
                    and published.get(record["logical"], {}).get("artifact_sha256") != record["translated_sha256"]):
                raise ValueError("Publish the pending image overlay before importing manual images")
    import UnityPy
    rows = []
    for approval in pending:
        logical = approval["logical"]
        row = index.get(logical)
        if row is None:
            print(f"Human image not present in current catalogue: {logical}")
            continue
        bundle = work / "bundles" / row["remote"]
        download(f"{version['asset_root']}/{row['remote']}", bundle, row["declared_size"])
        if sha256_file(bundle) != approval["archive_sha256"]:
            print(f"Human image source changed; holding for review: {logical}")
            continue
        objects = {int(o.path_id): o for o in UnityPy.load(str(bundle)).objects if o.type.name == "Texture2D"}
        for texture in approval["textures"]:
            original = work / "originals" / approval["archive_sha256"] / f"{texture['id']}.png"
            original.parent.mkdir(parents=True, exist_ok=True)
            objects[texture["id"]].read().image.convert("RGBA").save(original)
            rows.append({"archive_sha256": approval["archive_sha256"], "bundle": logical,
                "remote": row["remote"], "source_bundle": str(bundle.resolve()),
                "source_id": logical.removesuffix(".unity3d") + ":" + str(texture["id"]),
                "texture_path_id": texture["id"], "original_png": str(original.resolve()),
                "original_png_sha256": texture["source_sha256"], "original_size": texture["size"],
                "restored_png": str((root / texture["localized"]).resolve()),
                "restored_png_sha256": texture["sha256"], "review_status": REVIEW})
    if not rows:
        return 0
    install = work / "install.jsonl"
    install.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    report, audit = work / "injection-report.json", work / "repack-independent-audit.json"
    for script, args in [
        ("inject_reviewed_textures.py", ["--all", "--install-manifest", str(install), "--out", str(work / "injected"), "--report", str(report)]),
        ("verify_bundle_repack.py", ["--report", str(report), "--install-manifest", str(install), "--audit", str(audit)]),
    ]:
        subprocess.run([sys.executable, str(root / "pipelines/image" / script), *args], check=True)
    export(version, index, report, audit, overlay)
    print(f"Materialized {len(rows)} human-approved textures; no generation or visual model called")
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    materialize(args.root.resolve(), args.work.resolve())
