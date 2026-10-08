#!/usr/bin/env python3
"""Consume a reviewed serverless translation snapshot into an isolated candidate run.

The portal snapshot is an input boundary, not a release ledger. This command
binds every accepted row to an exact source catalogue and emits a universe plus
candidate JSONL that ``build_translation_release_ledger.py`` can consume with
``--universe`` and ``--expected-count``. It never writes production queues,
canonical overlays, NAS, or APK artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "build" / "localization-90200"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.mltd_localize_gtx import validate_translation


class SnapshotError(ValueError):
    pass


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SnapshotError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise SnapshotError(f"{path}:{line_number}: expected JSON object")
            row["_line"] = line_number
            rows.append(row)
    return rows


def field(row: dict[str, Any], name: str, *, line: int) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError(f"line {line}: missing {name}")
    return value


def _composite_in(value: str) -> bool:
    return "+" in value or "-assets-" in value


def version_matches(observed: str, target: str) -> bool:
    """Exact match only, and never on a combined identity.

    This used to split on ``+`` so a legacy ``9.0.200+1077100`` tag would match
    an asset version.  Combined identities are banned project-wide (Client and
    Assets are independent axes), so that tolerance is gone: a value carrying
    ``+`` or ``-assets-`` is refused outright rather than silently reduced to
    one of its halves.  Refusing is the point: a caller who passes one should
    learn immediately that it is not a version, not have it quietly accepted
    under the other axis's name.
    """
    if _composite_in(observed) or _composite_in(target):
        return False
    return observed == target


def load_catalogue(path: Path, base_version: str) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    by_hash: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(path):
        line = int(row.pop("_line"))
        observed = str(row.get("asset_version") or row.get("base_version") or base_version)
        if not version_matches(observed, base_version):
            raise SnapshotError(f"line {line}: catalogue base_version {observed!r} != {base_version!r}")
        bundle = field(row, "bundle", line=line)
        key = field(row, "key", line=line)
        source = field(row, "source", line=line)
        source_sha = str(row.get("source_sha256") or sha256_text(source)).lower()
        if source_sha != sha256_text(source):
            raise SnapshotError(f"line {line}: catalogue source_sha256 mismatch")
        identity = (bundle, key)
        if identity in by_key:
            raise SnapshotError(f"line {line}: duplicate catalogue bundle/key {bundle}/{key}")
        old = by_hash.get(source_sha)
        if old is not None and (old["source"] != source or old["bundle"] != bundle or old["key"] != key):
            raise SnapshotError(f"line {line}: source hash collision {source_sha}")
        asset_ver = str(row.get("asset_version") or base_version)
        normalized = {"asset_version": asset_ver, "base_version": base_version, "bundle": bundle, "key": key,
                      "source": source, "source_sha256": source_sha}
        by_key[identity] = normalized
        by_hash[source_sha] = normalized
    if not by_key:
        raise SnapshotError("source catalogue is empty")
    return by_key, by_hash


def load_snapshot(path: Path, base_version: str, catalogue: dict[tuple[str, str], dict[str, Any]],
                  catalogue_by_hash: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_hashes: set[str] = set()
    for row in read_jsonl(path):
        line = int(row.pop("_line"))
        observed = str(row.get("asset_version") or field(row, "base_version", line=line))
        if not version_matches(observed, base_version):
            raise SnapshotError(f"line {line}: snapshot base_version {observed!r} != {base_version!r}")
        if row.get("status") != "accepted":
            raise SnapshotError(f"line {line}: snapshot contains non-accepted status")
        bundle = field(row, "bundle", line=line)
        key = field(row, "key", line=line)
        source = field(row, "source", line=line)
        source_sha = field(row, "source_sha256", line=line).lower()
        translation = field(row, "translation", line=line)
        if source_sha != sha256_text(source):
            raise SnapshotError(f"line {line}: snapshot source_sha256 mismatch")
        expected = catalogue.get((bundle, key))
        if expected is None or expected["source_sha256"] != source_sha or expected["source"] != source:
            raise SnapshotError(f"line {line}: source is not the exact catalogue row for {bundle}/{key}")
        if catalogue_by_hash.get(source_sha, {}).get("source") != source:
            raise SnapshotError(f"line {line}: source hash is not in the catalogue")
        try:
            validate_translation(source, translation)
        except ValueError as exc:
            raise SnapshotError(f"line {line}: translation structural validation failed: {exc}") from exc
        if any(token in translation for token in ("\x00", "|", "^")):
            raise SnapshotError(f"line {line}: translation contains a reserved structural separator")
        identity = (bundle, key)
        if identity in seen_keys:
            raise SnapshotError(f"line {line}: duplicate snapshot bundle/key {bundle}/{key}")
        if source_sha in seen_hashes:
            raise SnapshotError(f"line {line}: duplicate snapshot source hash {source_sha}")
        seen_keys.add(identity)
        seen_hashes.add(source_sha)
        asset_ver = str(row.get("asset_version") or base_version)
        accepted.append({
            "asset_version": asset_ver,
            "base_version": base_version,
            "bundle": bundle,
            "key": key,
            "source": source,
            "source_sha256": source_sha,
            "translation": translation,
            "status": "community_candidate",
            "provenance": {"provider": "community_portal", "source": "reviewed_snapshot"},
            "portal_review": {"status": "accepted"},
        })
    if not accepted:
        raise SnapshotError("reviewed snapshot contains no accepted rows")
    return accepted


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8", newline="\n")


def consume(snapshot: Path, catalogue: Path, out_dir: Path, base_version: str) -> dict[str, Any]:
    snapshot = snapshot.resolve()
    catalogue = catalogue.resolve()
    out_dir = out_dir.resolve()
    if not snapshot.is_file() or not catalogue.is_file():
        raise SnapshotError("snapshot and source catalogue must be existing files")
    if PRODUCTION == out_dir or PRODUCTION in out_dir.parents:
        raise SnapshotError("refusing to write under production localization directory")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SnapshotError("output directory must be empty")
    by_key, by_hash = load_catalogue(catalogue, base_version)
    candidates = load_snapshot(snapshot, base_version, by_key, by_hash)
    out_dir.mkdir(parents=True, exist_ok=True)
    universe_rows = list(by_key.values())
    universe_path = out_dir / "universe.jsonl"
    candidates_path = out_dir / "candidates.jsonl"
    write_jsonl(universe_path, universe_rows)
    write_jsonl(candidates_path, candidates)
    manifest = {
        "schema_version": 1,
        "kind": "mltd-translation-portal-consumption",
        "candidate_only": True,
        "release_ready": False,
        "base_version": base_version,
        "source_catalogue": {"path": str(catalogue), "sha256": sha256_file(catalogue), "rows": len(universe_rows)},
        "portal_snapshot": {"path": str(snapshot), "sha256": sha256_file(snapshot), "accepted_rows": len(candidates)},
        "outputs": {
            "universe": {"path": str(universe_path), "sha256": sha256_file(universe_path)},
            "candidates": {"path": str(candidates_path), "sha256": sha256_file(candidates_path)},
        },
        "next_step": "Run build_translation_release_ledger.py with --universe universe.jsonl --expected-count catalogue rows; do not publish this manifest directly.",
    }
    manifest_path = out_dir / "portal-consumption-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"base_version": base_version, "catalogue_rows": len(universe_rows), "accepted_rows": len(candidates), "release_ready": False, "out_dir": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--source-catalogue", type=Path, required=True)
    parser.add_argument("--asset-version", help="Asset version (e.g. 1077100)")
    parser.add_argument("--base-version", help="Legacy alias for the asset version (digits only, e.g. 1077100); "
                                                "combined tags such as 9.0.200+1077100 are refused")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    target_version = args.asset_version or args.base_version
    if not target_version:
        parser.error("Either --asset-version or --base-version must be specified")
    try:
        print(json.dumps(consume(args.snapshot, args.source_catalogue, args.out_dir, target_version), ensure_ascii=False, indent=2))
        return 0
    except (SnapshotError, OSError, json.JSONDecodeError) as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
