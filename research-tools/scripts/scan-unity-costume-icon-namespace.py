#!/usr/bin/env python3
"""Build an exact resource-to-update-batch map from Unity costume icons.

The MLTD asset index maps logical names to opaque physical bundle names.  The
logical icon name exposes the costume resource namespace, while the
AssetBundle container path exposes the update batch that shipped it.  This
scanner joins those two facts for every costume icon in a complete static
asset view.  It is intentionally read-only and can run directly on the NAS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterable

import msgpack
import UnityPy


BATCH_RE = re.compile(r"(?:^|/)update/([^/]+)/")
ICON_RE = re.compile(r"^costume_icon_(.+)\.unity3d$")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_icon_resource(logical: str) -> str:
    match = ICON_RE.fullmatch(logical)
    if not match:
        raise ValueError(f"not a costume icon logical: {logical}")
    resource = match.group(1)
    if resource.endswith("_ll"):
        resource = resource[:-3]
    # Some icon logicals add a transport-level costume_ prefix to an otherwise
    # idol-specific resource ID.  Keep common costume_<number> resources intact.
    if re.match(r"^costume_\d{3}[a-z]{3}", resource):
        resource = resource[len("costume_") :]
    return resource


def scan_one(item: tuple[str, str, str, int]) -> dict[str, Any]:
    logical, physical, path_text, expected_size = item
    path = Path(path_text)
    row: dict[str, Any] = {
        "logical": logical,
        "physical": physical,
        "resource_id": normalize_icon_resource(logical),
        "expected_size": expected_size,
    }
    if not path.is_file():
        row["error"] = "missing_file"
        return row
    actual_size = path.stat().st_size
    row["actual_size"] = actual_size
    if expected_size > 0 and actual_size != expected_size:
        row["error"] = "size_mismatch"
        return row

    try:
        env = UnityPy.load(str(path))
    except Exception as exc:
        row["error"] = f"unity_load:{type(exc).__name__}"
        return row

    batches: set[str] = set()
    containers: set[str] = set()
    object_types: Counter[str] = Counter()
    try:
        for obj in env.objects:
            object_types[obj.type.name] += 1
            if obj.type.name != "AssetBundle":
                continue
            bundle = obj.read()
            raw_container = getattr(bundle, "m_Container", {}) or {}
            values = raw_container.keys() if isinstance(raw_container, Mapping) else (
                entry[0] if isinstance(entry, (list, tuple)) and entry else entry
                for entry in raw_container
            )
            for value in values:
                container = str(value)
                containers.add(container)
                batches.update(BATCH_RE.findall(container))
    except Exception as exc:
        row["error"] = f"assetbundle_read:{type(exc).__name__}"
        return row

    row["batches"] = sorted(batches)
    row["containers"] = sorted(containers)
    row["object_types"] = dict(sorted(object_types.items()))
    if not batches:
        row["error"] = "no_update_batch"
    return row


def load_index(path: Path) -> dict[str, Any]:
    raw = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        raise ValueError("unexpected MLTD asset-index structure")
    return raw[0]


def iter_icon_items(index: dict[str, Any], asset_root: Path) -> Iterable[tuple[str, str, str, int]]:
    for logical in sorted(index):
        if not isinstance(logical, str) or ICON_RE.fullmatch(logical) is None:
            continue
        record = index[logical]
        if not isinstance(record, (list, tuple)) or len(record) < 2:
            continue
        physical = str(record[1])
        expected_size = int(record[2]) if len(record) >= 3 else 0
        yield logical, physical, str(asset_root / physical), expected_size


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset-index", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="unknown")
    parser.add_argument("--scope", default="jp-android")
    parser.add_argument("--workers", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.asset_index.is_file():
        raise SystemExit(f"asset index not found: {args.asset_index}")
    if not args.asset_root.is_dir():
        raise SystemExit(f"asset root not found: {args.asset_root}")
    if args.workers < 1:
        raise SystemExit("workers must be positive")

    index = load_index(args.asset_index)
    items = list(iter_icon_items(index, args.asset_root))
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for number, row in enumerate(pool.map(scan_one, items, chunksize=32), 1):
            rows.append(row)
            if number % 500 == 0 or number == len(items):
                print(f"scanned={number}/{len(items)}", flush=True)

    resources: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["resource_id"])].append(row)
    for resource_id, members in sorted(grouped.items()):
        resources[resource_id] = {
            "resource_id": resource_id,
            "logicals": sorted({str(row["logical"]) for row in members}),
            "physicals": sorted({str(row["physical"]) for row in members}),
            "batches": sorted({batch for row in members for batch in row.get("batches", [])}),
            "containers": sorted(
                {container for row in members for container in row.get("containers", [])}
            ),
            "errors": sorted({str(row["error"]) for row in members if row.get("error")}),
        }

    batch_resources: dict[str, list[str]] = defaultdict(list)
    for resource_id, row in resources.items():
        for batch in row["batches"]:
            batch_resources[batch].append(resource_id)
    batch_resources = {
        batch: sorted(values) for batch, values in sorted(batch_resources.items())
    }

    errors = Counter(str(row.get("error")) for row in rows if row.get("error"))
    result = {
        "schema": "mltd-current-unity-costume-icon-namespace-v1",
        "schema_version": 1,
        "version": args.version,
        "scope": args.scope,
        "inputs": {
            "asset_index": str(args.asset_index),
            "asset_index_sha256": sha256_path(args.asset_index),
            "asset_root": str(args.asset_root),
        },
        "counts": {
            "manifest_entries": len(index),
            "icon_logicals": len(items),
            "scanned_icon_logicals": len(rows),
            "unique_resource_ids": len(resources),
            "resources_with_one_batch": sum(len(row["batches"]) == 1 for row in resources.values()),
            "resources_with_multiple_batches": sum(len(row["batches"]) > 1 for row in resources.values()),
            "resources_without_batch": sum(not row["batches"] for row in resources.values()),
            "update_batches": len(batch_resources),
            "error_logicals": sum(errors.values()),
        },
        "errors": dict(sorted(errors.items())),
        "resources": resources,
        "batches": batch_resources,
        "logical_rows": rows,
        "policy": {
            "resource_id_is_derived_from": "costume_icon_<resource_id> logical name",
            "batch_is_derived_from": "AssetBundle m_Container /update/<batch>/ path",
            "absence_is_not_a_master_row": True,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(result["counts"], ensure_ascii=False, sort_keys=True))
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
