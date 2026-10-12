"""Export independently audited image bundles for the unified release builder."""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

import msgpack

from build_generated_release import download, load_official_index, load_version_manifest
from pipelines.core.hashing import sha256_file


def export(version: dict, index: dict, report_path: Path, audit_path: Path,
           output: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    bundles = report.get("bundles", [])
    if (not bundles or not re.fullmatch(r"[0-9a-f]{64}", str(report.get("install_manifest_sha256", "")))
            or audit.get("kind") != "independent-reviewed-image-unity-repack-audit"
            or audit.get("errors") != [] or audit.get("failed_bundles") != 0
            or audit.get("report_audited_sha256") != sha256_file(report_path)
            or audit.get("install_manifest_sha256") != report.get("install_manifest_sha256")
            or any(audit.get(field) != len(bundles) for field in
                   ("manifest_bundles", "audited_bundles", "passed_bundles"))):
        raise ValueError("image overlay requires a complete, source-bound independent repack audit")
    prepared = []
    seen = set()
    by_fold = {name.casefold(): name for name in index}
    for bundle in bundles:
        name = str(bundle["bundle"])
        name = name if name.endswith(".unity3d") else name + ".unity3d"
        logical = by_fold.get(name.casefold())
        if logical is None or logical in seen:
            raise ValueError(f"unknown or duplicate image bundle: {name}")
        seen.add(logical)
        row = index[logical]
        source = Path(bundle["source_bundle"])
        artifact = Path(bundle["output_bundle"])
        if not source.is_absolute():
            source = report_path.parent / source
        if not artifact.is_absolute():
            artifact = report_path.parent / artifact
        if (bundle.get("roundtrip_verified") is not True
                or bundle["remote"] != row["remote"]
                or source.stat().st_size != row["declared_size"]
                or source.stat().st_size != bundle["source_size"]
                or artifact.stat().st_size != bundle["output_size"]
                or sha256_file(source) != bundle["source_sha256"]
                or sha256_file(artifact) != bundle["output_sha256"]):
            raise ValueError(f"audited image bundle does not match the current official source: {logical}")
        prepared.append((artifact, {
            "logical": logical, "remote": row["remote"],
            "original_sha256": bundle["source_sha256"],
            "translated_sha256": bundle["output_sha256"],
            "original_bytes": bundle["source_size"], "translated_bytes": bundle["output_size"],
        }))
    # Validate every bundle before writing. Publish the manifest last, after all
    # artifacts and the current version's single catalogue are present.
    scope = output / "jp-android"
    scope.mkdir(parents=True, exist_ok=True)
    table = {name: [row["catalog_hash"], row["remote"], row["declared_size"]]
             for name, row in index.items()}
    for artifact, record in prepared:
        destination = scope / record["remote"]
        temporary = destination.with_suffix(".writing")
        shutil.copyfile(artifact, temporary)
        temporary.replace(destination)
        table[record["logical"]][2] = record["translated_bytes"]
    (scope / version["index_name"]).write_bytes(msgpack.packb([table], use_bin_type=True))
    shutil.copyfile(audit_path, output / "repack-independent-audit.json")
    manifest = {"schema_version": 1, "asset_version": version["asset_version"],
                "client_manifest_name": version["index_name"],
                "audit_sha256": sha256_file(audit_path),
                "records": [record for _artifact, record in prepared]}
    temporary = output / "overlay-manifest.writing"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output / "overlay-manifest.json")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("image-overlay"))
    args = parser.parse_args()
    version = load_version_manifest(args.version_manifest)
    index_path = args.work / version["index_name"]
    download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    result = export(version, load_official_index(index_path), args.report, args.audit,
                    args.output_root / version["asset_version"])
    print(json.dumps({"asset_version": result["asset_version"], "bundles": len(result["records"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
