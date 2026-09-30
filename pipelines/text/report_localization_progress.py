#!/usr/bin/env python3
"""Report current MLTD localization coverage from source-bound translation state."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localization_progress_eta import estimate, format_duration

ACCEPTED_EXCLUSIONS = {"pending", "needs_review", "skip"}


def read_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def accepted_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    result: set[str] = set()
    for row in read_jsonl(path):
        sid = str(row.get("source_sha256", "")).strip()
        translation = str(row.get("translation", "")).strip()
        status = str(row.get("status", "")).strip()
        if sid and translation and status not in ACCEPTED_EXCLUSIONS:
            result.add(sid)
    return result


def line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("rb") as handle:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1024 * 1024), b""))


def pct(value: int, total: int) -> float:
    return 0.0 if total <= 0 else round(value * 100.0 / total, 4)


def display_pct(done: int, total: int) -> str:
    """Never report 100.00% while even one source is still missing."""
    if total <= 0:
        return "0.00%"
    value = 100.0 * done / total
    if 0 <= done < total and value >= 99.995:
        return f"{value:.4f}%"
    return f"{value:.2f}%"


def build_report(workspace: Path, api_output: Path, legacy_output: Path) -> dict:
    memory = workspace / "translation-memory.jsonl"
    queue = workspace / "machine-translation-queue.jsonl"
    stale = workspace / "machine-translations-api.stale.jsonl"
    repair = workspace / "machine-translation-stale-repair-queue.jsonl"
    companion_queue = workspace / "machine-translation-nongtx-queue.jsonl"
    companion_output = workspace / "machine-translations-nongtx-api.jsonl"
    resource_coverage = workspace / "localization-resource-coverage-audit.json"

    required = [memory, queue]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing localization progress input(s): " + ", ".join(missing))

    api_ids = accepted_ids(api_output)
    legacy_ids = accepted_ids(legacy_output)
    machine_done = api_ids | legacy_ids
    queue_ids = {
        str(row.get("source_sha256", "")).strip()
        for row in read_jsonl(queue)
        if str(row.get("source_sha256", "")).strip()
    }

    unique_total = 0
    occurrences_total = 0
    seed_unique = 0
    seed_occurrences = 0
    machine_done_occurrences = 0
    pending_occurrences = 0

    for row in read_jsonl(memory):
        sid = str(row.get("source_sha256", "")).strip()
        occurrences = int(row.get("occurrences", 1) or 1)
        unique_total += 1
        occurrences_total += occurrences
        if sid not in queue_ids:
            seed_unique += 1
            seed_occurrences += occurrences
        elif sid in machine_done:
            machine_done_occurrences += occurrences
        else:
            pending_occurrences += occurrences

    machine_done_ids = machine_done & queue_ids
    machine_pending_ids = queue_ids - machine_done
    overall_done_unique = seed_unique + len(machine_done_ids)
    overall_done_occurrences = seed_occurrences + machine_done_occurrences

    companion_ids: set[str] = set()
    companion_occurrences: dict[str, int] = {}
    if companion_queue.is_file():
        for row in read_jsonl(companion_queue):
            sid = str(row.get("source_sha256", "")).strip()
            if not sid:
                continue
            companion_ids.add(sid)
            companion_occurrences[sid] = int(row.get("occurrences", 1) or 1)
    companion_done_ids = accepted_ids(companion_output) & companion_ids
    companion_pending_ids = companion_ids - companion_done_ids

    confirmed_text_total = unique_total + len(companion_ids)
    confirmed_text_done = overall_done_unique + len(companion_done_ids)

    coverage_doc = {}
    if resource_coverage.is_file():
        value = json.loads(resource_coverage.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict):
            coverage_doc = value
    coverage_text = coverage_doc.get("textual_surfaces", {})
    audited_union = int(coverage_text.get("confirmed_union_unique", 0) or 0)
    if audited_union and audited_union != confirmed_text_total:
        # Keep the report usable if a newly discovered surface has not yet been
        # folded into the companion queue, but make the mismatch explicit.
        confirmed_text_total = audited_union

    eta = estimate(
        workspace,
        {
            "gtx": {"done": len(machine_done_ids), "pending": len(machine_pending_ids)},
            "companion": {"done": len(companion_done_ids), "pending": len(companion_pending_ids)},
        },
        {"gtx": api_output, "companion": companion_output},
        {
            "gtx": workspace / "machine-translations-api.summary.json",
            "companion": workspace / "machine-translations-nongtx-api.summary.json",
        },
    )

    control_audit = workspace / "audits" / "numbered-control-code-current.summary.json"
    repair_ids: set[str] = set()
    if control_audit.is_file():
        try:
            audit_doc = json.loads(control_audit.read_text(encoding="utf-8-sig"))
            repair_ids = set(audit_doc.get("repair_source_ids", []))
        except (OSError, ValueError, TypeError):
            pass

    return {
        "schema_version": 1,
        "eta": eta,
        "control_code_repair": {
            "pending_unique": len(repair_ids & machine_pending_ids),
            "audited_source_ids": len(repair_ids),
            "audit": str(control_audit) if control_audit.is_file() else None,
        },
        "workspace": str(workspace),
        "unique_sources": {
            "total": unique_total,
            "seed_covered": seed_unique,
            "machine_queue": len(queue_ids),
            "machine_done": len(machine_done_ids),
            "machine_pending": len(machine_pending_ids),
            "overall_done": overall_done_unique,
            "overall_percent": pct(overall_done_unique, unique_total),
            "machine_queue_percent": pct(len(machine_done_ids), len(queue_ids)),
        },
        "occurrences": {
            "total": occurrences_total,
            "seed_covered": seed_occurrences,
            "machine_done": machine_done_occurrences,
            "machine_pending": pending_occurrences,
            "overall_done": overall_done_occurrences,
            "overall_percent": pct(overall_done_occurrences, occurrences_total),
            "machine_queue_percent": pct(
                machine_done_occurrences,
                machine_done_occurrences + pending_occurrences,
            ),
        },
        "confirmed_resource_text": {
            "total_unique": confirmed_text_total,
            "done_unique": confirmed_text_done,
            "pending_unique": max(0, confirmed_text_total - confirmed_text_done),
            "overall_percent": pct(confirmed_text_done, confirmed_text_total),
            "main_gtx_total_unique": unique_total,
            "main_gtx_done_unique": overall_done_unique,
            "companion_total_unique": len(companion_ids),
            "companion_done_unique": len(companion_done_ids),
            "companion_pending_unique": len(companion_pending_ids),
            "companion_total_occurrences": sum(companion_occurrences.values()),
            "companion_done_occurrences": sum(
                companion_occurrences.get(sid, 0) for sid in companion_done_ids
            ),
            "scope_note": (
                "Confirmed text only: GTX + FontRender + APK bootstrap BI + event-unit + MLD visible config. "
                "APK baked unkeyed UI review candidates, Android branding, baked video, and baked "
                "Texture2D/Sprite text are tracked separately and not included."
            ),
        },
        "outputs": {
            "api_unique": len(api_ids),
            "legacy_unique": len(legacy_ids),
            "api_legacy_overlap": len(api_ids & legacy_ids),
            "stale_evidence_rows": line_count(stale),
            "repair_queue_rows": line_count(repair),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workspace",
        type=Path,
        default=Path("build/localization-90200"),
    )
    ap.add_argument(
        "--api-output",
        type=Path,
        default=Path("build/localization-90200/machine-translations-api.jsonl"),
    )
    ap.add_argument(
        "--legacy-output",
        type=Path,
        default=Path("build/localization-90200/machine-translations-codex.jsonl"),
    )
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    report = build_report(args.workspace, args.api_output, args.legacy_output)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    u = report["unique_sources"]
    o = report["occurrences"]
    rsrc = report["confirmed_resource_text"]
    out = report["outputs"]
    eta = report["eta"]
    control = report["control_code_repair"]
    print("MLTD localization progress")
    print(
        f"  confirmed resource text: {rsrc['done_unique']:,}/{rsrc['total_unique']:,} "
        f"({display_pct(rsrc['done_unique'], rsrc['total_unique'])}), pending {rsrc['pending_unique']:,}"
    )
    print(
        f"  GTX unique sources:      {u['overall_done']:,}/{u['total']:,} "
        f"({display_pct(u['overall_done'], u['total'])})"
    )
    print(
        f"  GTX machine queue:       {u['machine_done']:,}/{u['machine_queue']:,} "
        f"({display_pct(u['machine_done'], u['machine_queue'])}), pending {u['machine_pending']:,}"
    )
    print(
        f"  non-GTX companion:       {rsrc['companion_done_unique']:,}/"
        f"{rsrc['companion_total_unique']:,}, pending {rsrc['companion_pending_unique']:,}"
    )
    print(
        f"  occurrences:    {o['overall_done']:,}/{o['total']:,} "
        f"({display_pct(o['overall_done'], o['total'])})"
    )
    print(
        f"  seed/machine:   {u['seed_covered']:,} seed + {u['machine_done']:,} machine"
    )
    print(
        f"  active outputs: API {out['api_unique']:,}, legacy {out['legacy_unique']:,}, "
        f"overlap {out['api_legacy_overlap']:,}"
    )
    print(
        f"  stale/repair:   {out['stale_evidence_rows']:,} evidence rows / "
        f"{out['repair_queue_rows']:,} unresolved repair rows"
    )
    if control["audit"]:
        print(
            f"  numbered control-code repair: {control['pending_unique']:,} "
            f"unresolved source IDs (audited {control['audited_source_ids']:,})"
        )
    print("  machine-translation ETA (approximate, excludes QA-failed/rework not active):")
    for key, label in (("gtx", "GTX"), ("companion", "non-GTX")):
        item = eta[key]
        if item["basis"] == "complete":
            detail = "complete"
        elif not item["active"]:
            if item["eta_seconds"] is not None:
                detail = (
                    f"~{format_duration(item['eta_seconds'])} estimated work "
                    f"at previous {item['rate_per_minute']:.1f} accepted/min "
                    "(not running; no finish-time prediction)"
                )
            else:
                detail = "not running; ETA unavailable"
        elif item["eta_seconds"] is None:
            detail = "no reliable rate yet (" + item["basis"] + ")"
        else:
            basis = "recent samples" if item["fresh"] else "previous completed run"
            detail = (
                f"~{format_duration(item['eta_seconds'])}, "
                f"{item['rate_per_minute']:.1f} accepted/min ({basis})"
            )
        print(f"    {label}: {item['pending']:,} pending; {detail}")
    if eta["gtx"]["pending"] and not eta["gtx"]["active"]:
        print("    GTX pending/rework not included in non-GTX ETA; rerun separately.")
    if "checkpoint_warning" in eta:
        print("    ETA checkpoint unavailable; falling back to historical measurements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
