#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

RESOURCE_RE = re.compile(r"ld_(backdancer_)?(original|extra)_costume_resource_id_(\d+)$")
TITLE_RE = re.compile(r"^\[(.*?)\]")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def load_localization(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for record in path.read_text(encoding="utf-8", errors="replace").split("|"):
        if "^" in record:
            key, value = record.split("^", 1)
            out[key] = value
    return out


def title(text: str) -> str:
    match = TITLE_RE.match(text or "")
    return match.group(1) if match else (text or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--localization", type=Path, required=True)
    ap.add_argument("--formal-content", type=Path, required=True)
    ap.add_argument("--icon-namespace", type=Path, required=True)
    ap.add_argument("--current-catalog", type=Path, required=True)
    ap.add_argument("--dump-cs", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    for path in (
        args.localization,
        args.formal_content,
        args.icon_namespace,
        args.current_catalog,
        args.dump_cs,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)

    loc = load_localization(args.localization)
    formal = json.loads(args.formal_content.read_text(encoding="utf-8"))["local_content"]["costumes"]
    namespace = json.loads(args.icon_namespace.read_text(encoding="utf-8"))
    catalog = json.loads(args.current_catalog.read_text(encoding="utf-8"))
    idol_rows = {int(row["id"]): row for row in catalog["princess"]["idols"]}
    def cohort_for(idol_id: int) -> str:
        if 1 <= idol_id <= 13:
            return "765as"
        return {1: "princess", 2: "fairy", 3: "angel"}.get(
            int((idol_rows.get(idol_id) or {}).get("type") or 0), "unknown"
        )
    dump = args.dump_cs.read_text(encoding="utf-8", errors="replace")

    by_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
    formal_ids = {int(row["mst_costume_id"]) for row in formal}
    for row in formal:
        by_resource[str(row.get("resource_id") or "")].append(row)

    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, expected: Any) -> None:
        ok = actual == expected
        checks.append({"name": name, "actual": actual, "expected": expected, "pass": ok})
        if not ok:
            raise ValueError(f"{name}: expected {expected!r}, got {actual!r}")

    for marker in (
        "public CostumeStatus costume; // 0x20",
        "public CostumeStatus originalCostume; // 0x28",
        "public CostumeStatus extraCostume; // 0x30",
        "public string resource_id; // 0x18",
    ):
        check("dump_marker:" + marker, marker in dump, True)

    entries: list[dict[str, Any]] = []
    stats: dict[str, Counter[str]] = defaultdict(Counter)
    for key, resource_id in sorted(loc.items()):
        match = RESOURCE_RE.fullmatch(key)
        if not match:
            continue
        prefix = "backdancer_" if match.group(1) else ""
        variant = match.group(2)
        setting_id = int(match.group(3))
        kind = prefix + variant
        name = loc.get(f"ld_{prefix}{variant}_costume_name_{setting_id}", "")
        hits = by_resource.get(resource_id, [])
        exact = [
            row
            for row in hits
            if title(loc.get(f"ld_costume_name_{int(row['mst_costume_id'])}", "")) == name
        ]
        stats[kind]["keys"] += 1
        stats[kind]["resource_hit"] += bool(hits)
        stats[kind]["exact_name_resource_hit"] += bool(exact)
        stats[kind]["resource_miss"] += not bool(hits)
        entries.append(
            {
                "key": key,
                "kind": kind,
                "setting_id": setting_id,
                "name": name,
                "resource_id": resource_id,
                "formal_resource_hit_count": len(hits),
                "formal_exact_name_resource_hit_count": len(exact),
                "formal_hit_costume_ids": [int(row["mst_costume_id"]) for row in hits],
                "formal_exact_costume_ids": [int(row["mst_costume_id"]) for row in exact],
            }
        )

    check("resource_keys.total", len(entries), 152)

    fixed_stats = {
        "original": {"keys": 132, "resource_hit": 130, "exact_name_resource_hit": 128, "resource_miss": 2},
        "backdancer_original": {"keys": 1, "resource_hit": 0, "exact_name_resource_hit": 0, "resource_miss": 1},
        "backdancer_extra": {"keys": 1, "resource_hit": 1, "exact_name_resource_hit": 0, "resource_miss": 0},
    }
    for kind, wanted in fixed_stats.items():
        for metric, value in wanted.items():
            check(f"stats.{kind}.{metric}", int(stats[kind][metric]), value)

    # Extra keys are a recovery surface: after a family is promoted, a prior miss
    # becomes an exact formal hit. Keep semantic invariants rather than pre-merge counts.
    check("stats.extra.keys", int(stats["extra"]["keys"]), 18)
    check(
        "stats.extra.no_name_mismatches",
        int(stats["extra"]["resource_hit"]),
        int(stats["extra"]["exact_name_resource_hit"]),
    )
    check("stats.extra.minimum_calibrated_hits", int(stats["extra"]["exact_name_resource_hit"]) >= 4, True)
    check(
        "stats.extra.partition",
        int(stats["extra"]["resource_hit"]) + int(stats["extra"]["resource_miss"]),
        18,
    )

    common_resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if entry["kind"] not in {"original", "extra"}:
            continue
        resource_id = str(entry["resource_id"])
        if not resource_id.startswith("costume_") or resource_id in seen:
            continue
        hits = by_resource.get(resource_id, [])
        if not hits:
            continue
        seen.add(resource_id)
        common_resources.append(
            {
                "resource_id": resource_id,
                "source_key": entry["key"],
                "name": entry["name"],
                "row_count": len(hits),
                "idol_count": len({int(row["mst_idol_id"]) for row in hits}),
                "sort_identity_count": sum(
                    int(row["sort_id"]) == int(row["mst_costume_id"]) for row in hits
                ),
                "neutral_flag_count": sum(
                    row.get("exclude_album") is False
                    and row.get("exclude_random") is False
                    and int(row.get("collabo_number") or 0) == 0
                    and int(row.get("replace_group_id") or 0) == 0
                    and int(row.get("gorgeous_appeal_type") or 0) == 0
                    for row in hits
                ),
            }
        )

    common_rows = sum(item["row_count"] for item in common_resources)
    check("common_resource_calibration.resources_min", len(common_resources) >= 7, True)
    check("common_resource_calibration.rows_min", common_rows >= 305, True)
    check(
        "common_resource_calibration.sort_identity",
        sum(item["sort_identity_count"] for item in common_resources),
        common_rows,
    )
    check(
        "common_resource_calibration.neutral_flags",
        sum(item["neutral_flag_count"] for item in common_resources),
        common_rows,
    )

    by_key = {entry["key"]: entry for entry in entries}
    original = by_key["ld_original_costume_resource_id_2001"]
    extra = by_key["ld_extra_costume_resource_id_2001"]
    backdancer_original = by_key["ld_backdancer_original_costume_resource_id_2001"]
    backdancer_extra = by_key["ld_backdancer_extra_costume_resource_id_2001"]

    check("setting2001.original.name", original["name"], "スターピースメモリーズ")
    check("setting2001.original.resource", original["resource_id"], "001har0373")
    check("setting2001.original.formal_hits", original["formal_resource_hit_count"], 13)
    check(
        "setting2001.original.formal_ids",
        original["formal_hit_costume_ids"],
        [40262000 + idol_id * 10 for idol_id in range(1, 14)],
    )
    check("setting2001.extra.name", extra["name"], "スターピースメモリーズ 奏")
    check("setting2001.extra.resource", extra["resource_id"], "costume_62_vv")
    check("setting2001.extra.formal_state", extra["formal_resource_hit_count"] in (0, 13), True)
    check(
        "setting2001.extra.formal_exact_if_present",
        extra["formal_exact_name_resource_hit_count"],
        extra["formal_resource_hit_count"],
    )
    if extra["formal_resource_hit_count"]:
        check(
            "setting2001.extra.formal_ids",
            extra["formal_hit_costume_ids"],
            [40262000 + idol_id * 10 + 2 for idol_id in range(1, 14)],
        )

    check("setting2001.backdancer_extra.name", backdancer_extra["name"], "スターピースドリーマー 奏")
    check("setting2001.backdancer_extra.resource", backdancer_extra["resource_id"], "costume_63_vv")
    check(
        "setting2001.backdancer_extra.not_exact",
        backdancer_extra["formal_exact_name_resource_hit_count"],
        0,
    )

    resources = namespace.get("resources") or {}
    resource62 = resources.get("costume_62_vv") or {}
    check("namespace.costume_62_vv.exists", bool(resource62), True)
    check(
        "namespace.costume_62_vv.batch",
        resource62.get("batches"),
        ["202308_91230804_nf_ppsale"],
    )

    costume_title_ids: dict[str, list[int]] = defaultdict(list)
    for key, value in loc.items():
        if key.startswith("ld_costume_name_") and key[16:].isdigit():
            costume_title_ids[title(value)].append(int(key[16:]))

    missing_extra: list[dict[str, Any]] = []
    missing_original: list[dict[str, Any]] = []
    for entry in entries:
        ids = sorted(
            costume_id
            for costume_id in costume_title_ids.get(entry["name"], [])
            if costume_id not in formal_ids
        )
        if entry["kind"] == "extra" and entry["formal_resource_hit_count"] == 0:
            missing_extra.append({**entry, "matching_missing_costume_ids": ids})
        # Original keys are a much larger field-level recovery surface.  Only
        # expose a key after that exact key has at least one formal row whose
        # localized costume title and CostumeStatus.resource_id both match.
        # This binds resource_id only; group/model/release remain downstream gates.
        if (
            entry["kind"] == "original"
            and entry["formal_exact_name_resource_hit_count"] > 0
            and ids
        ):
            exact_ids = [int(x) for x in entry.get("formal_exact_costume_ids") or []]
            exact_rows = [row for row in formal if int(row["mst_costume_id"]) in set(exact_ids)]
            exact_cohorts = sorted({cohort_for(int(row["mst_idol_id"])) for row in exact_rows})
            scoped_ids = []
            for costume_id in ids:
                idol_id = (costume_id % 1000) // 10
                if idol_id in idol_rows and cohort_for(idol_id) in exact_cohorts:
                    scoped_ids.append(costume_id)
            if scoped_ids:
                missing_original.append(
                    {
                        **entry,
                        "formal_exact_cohorts": exact_cohorts,
                        "matching_missing_costume_ids": scoped_ids,
                        "scope_policy": "same-cohort-as-formal-exact-resource-anchor",
                    }
                )

    result = {
        "schema": "mltd-current-stageunit-costume-resource-localization-v1",
        "schema_version": 1,
        "inputs": {
            "localization": str(args.localization),
            "localization_sha256": sha(args.localization),
            "formal_content": str(args.formal_content),
            "formal_content_sha256": sha(args.formal_content),
            "icon_namespace": str(args.icon_namespace),
            "icon_namespace_sha256": sha(args.icon_namespace),
            "current_catalog": str(args.current_catalog),
            "current_catalog_sha256": sha(args.current_catalog),
            "dump_cs": str(args.dump_cs),
            "dump_cs_sha256": sha(args.dump_cs),
        },
        "semantics": {
            "normal_original_extra_are_full_costume_status_fields": True,
            "normal_resource_key_calibration": {
                key: dict(value)
                for key, value in stats.items()
                if not key.startswith("backdancer_")
            },
            "backdancer_keys_are_representative_only": True,
            "backdancer_reason": (
                "backdancer_extra_2001 names Dreamer Kanade while costume_63_vv currently "
                "resolves to formal Dreamer base rows; it is not a per-row binding."
            ),
        },
        "resource_keys": entries,
        "common_resource_calibration": common_resources,
        "setting_2001": {
            "original": original,
            "extra": extra,
            "backdancer_original": backdancer_original,
            "backdancer_extra": backdancer_extra,
        },
        "missing_normal_extra_candidates": missing_extra,
        "missing_normal_original_candidates": missing_original,
        "checks": {
            "passed": sum(bool(row["pass"]) for row in checks),
            "total": len(checks),
            "all_pass": all(bool(row["pass"]) for row in checks),
            "rows": checks,
        },
        "resource_semantics_source_bound": True,
        "formal_merge_allowed": False,
        "note": (
            "This audit establishes normal original/extra resource-key semantics and excludes "
            "backdancer representative keys. Family-specific identity/group/release evidence is "
            "still required before merge."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "checks": result["checks"]["passed"],
                "total": result["checks"]["total"],
                "missing_extra_candidates": len(missing_extra),
                "missing_original_candidates": len(missing_original),
                "missing_original_candidate_rows": sum(
                    len(row.get("matching_missing_costume_ids") or []) for row in missing_original
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
