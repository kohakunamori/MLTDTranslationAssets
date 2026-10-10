#!/usr/bin/env python3
"""Snapshot-wide MLTD GTX localization pipeline.

This layer expands the single-bundle primitives in mltd_localize_gtx.py to an
entire asset-index snapshot.  It preserves the official archive, emits a
translation-memory queue deduplicated by exact JP source text, audits coverage,
and writes only changed UnityFS bundles into a local asset overlay.

Translation inputs may be either:
  * exact rows: bundle + key + source + translation + status
  * memory rows: source + translation + status (bundle/key omitted)

Exact rows win.  Memory fallback is used only when one JP source has one
unambiguous accepted translation.  Every applied row is source-bound and token
validated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localization_version_identity import version_identity
from mltd_localize_gtx import (
    is_source_text,
    parse_records,
    read_gtx,
    read_jsonl,
    replace_records,
    save_localized_bundle,
    translation_status_is_accepted,
    validate_translation,
    write_jsonl,
)


def source_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_snapshot(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    objects = value.get("objects")
    if not isinstance(objects, list):
        raise ValueError(f"{path}: missing objects[]")
    scope = str(value.get("scope", "jp-android"))
    seen_logical: set[str] = set()
    seen_remote: set[str] = set()
    normalized = []
    for row in objects:
        logical = str(row.get("logical", ""))
        remote = str(row.get("remote", ""))
        if not logical or not remote:
            raise ValueError(f"{path}: malformed snapshot object {row!r}")
        if logical in seen_logical:
            raise ValueError(f"{path}: duplicate logical {logical}")
        if remote in seen_remote:
            # A remote object may theoretically be content-deduplicated.  Keep
            # the first logical route only because the overlay is remote-keyed.
            continue
        seen_logical.add(logical)
        seen_remote.add(remote)
        normalized.append(
            {
                "logical": logical,
                "remote": remote,
                "catalog_hash": str(row.get("catalog_hash", "")),
                "declared_size": int(row.get("declared_size", 0)),
            }
        )
    return {
        "scope": scope,
        "objects": normalized,
        "asset_index": value.get("asset_index"),
        "upstream_root": value.get("upstream_root"),
    }


@dataclass
class BundleCatalogue:
    logical: str
    remote: str
    bundle: str
    records: int
    source_rows: list[dict]


def read_snapshot_bundle(
    archive_root: Path, scope: str, row: dict
) -> BundleCatalogue:
    path = archive_root / scope / row["remote"]
    if not path.is_file():
        raise FileNotFoundError(path)
    name, text, _cipher = read_gtx(path)
    parsed = parse_records(text)
    if len(parsed) != len({key for key, _ in parsed}):
        raise ValueError(f"{row['logical']}: duplicate GTX key")
    source_rows = []
    for key, source in parsed:
        if not is_source_text(source):
            continue
        source_rows.append(
            {
                "logical": row["logical"],
                "remote": row["remote"],
                "bundle": name,
                "key": key,
                "source": source,
                "source_sha256": source_id(source),
                "translation": "",
                "status": "pending",
            }
        )
    return BundleCatalogue(
        logical=row["logical"],
        remote=row["remote"],
        bundle=name,
        records=len(parsed),
        source_rows=source_rows,
    )


def _selected_objects(snapshot: dict, limit: int) -> list[dict]:
    objects = snapshot["objects"]
    return objects[:limit] if limit else objects


def cmd_extract_snapshot(args: argparse.Namespace) -> int:
    snapshot = load_snapshot(args.snapshot)
    objects = _selected_objects(snapshot, args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output_tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    if args.memory:
        args.memory.parent.mkdir(parents=True, exist_ok=True)
        memory_tmp = args.memory.with_suffix(args.memory.suffix + ".tmp")
    else:
        memory_tmp = None

    unique: dict[str, dict] = {}
    counts = Counter()
    bundle_summaries: list[dict] = []

    def reader(row: dict) -> BundleCatalogue:
        return read_snapshot_bundle(args.archive_root, snapshot["scope"], row)

    with output_tmp.open("w", encoding="utf-8", newline="\n") as out:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for bundle in pool.map(reader, objects):
                counts["bundles"] += 1
                counts["records"] += bundle.records
                counts["source_candidates"] += len(bundle.source_rows)
                bundle_summaries.append(
                    {
                        "logical": bundle.logical,
                        "remote": bundle.remote,
                        "bundle": bundle.bundle,
                        "records": bundle.records,
                        "source_candidates": len(bundle.source_rows),
                    }
                )
                for row in bundle.source_rows:
                    out.write(
                        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
                    sid = row["source_sha256"]
                    memory = unique.get(sid)
                    example = {
                        "logical": row["logical"],
                        "bundle": row["bundle"],
                        "key": row["key"],
                    }
                    if memory is None:
                        unique[sid] = {
                            "source_sha256": sid,
                            "source": row["source"],
                            "translation": "",
                            "status": "pending",
                            "occurrences": 1,
                            "examples": [example],
                        }
                    else:
                        memory["occurrences"] += 1
                        if len(memory["examples"]) < args.example_count:
                            memory["examples"].append(example)

    output_tmp.replace(args.output)
    counts["unique_source_values"] = len(unique)
    if args.memory and memory_tmp is not None:
        write_jsonl(
            memory_tmp,
            sorted(
                unique.values(),
                key=lambda row: (-int(row["occurrences"]), str(row["source"])),
            ),
        )
        memory_tmp.replace(args.memory)
    result = {
        "schema_version": 1,
        "snapshot": str(args.snapshot),
        "scope": snapshot["scope"],
        "asset_index": snapshot["asset_index"],
        "selected_bundles": len(objects),
        **dict(counts),
        "catalogue": str(args.output),
        "memory": str(args.memory) if args.memory else None,
        "top_bundles": sorted(
            bundle_summaries,
            key=lambda row: int(row["source_candidates"]),
            reverse=True,
        )[:20],
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


@dataclass
class TranslationResolver:
    exact: dict[tuple[str, str], dict]
    source: dict[str, dict]
    ambiguous_sources: set[str]
    rejected_rows: int
    rejected_invalid_legacy_rows: int
    invalid_legacy_examples: list[dict]


def _accepted(row: dict) -> bool:
    status = str(row.get("status", "")).strip()
    translation = str(row.get("translation", ""))
    return bool(translation) and translation_status_is_accepted(status)


def is_legacy_provenance(row: dict) -> bool:
    """True for rows derived from the official Traditional corpus.

    Matched by status *or* provenance: the owner-waived t2s ledger keeps its own
    distinct status so that it is never confused with the ledger allowlist, but
    its invalid rows must receive the same lenient handling as the raw legacy
    rows they were derived from (identical drop set, no build failure).
    """
    status = str(row.get("status", "")).strip()
    if status.startswith("official_legacy"):
        return True
    return "official-legacy-zh" in str(row.get("provenance", ""))


def build_resolver(paths: Iterable[Path]) -> TranslationResolver:
    exact: dict[tuple[str, str], dict] = {}
    source_candidates: dict[str, dict] = {}
    ambiguous: set[str] = set()
    machine_candidates: dict[str, dict] = {}
    machine_conflicts: set[str] = set()
    rejected = 0
    invalid_legacy = 0
    invalid_legacy_examples: list[dict] = []

    for path in paths:
        for row in read_jsonl(path):
            if not _accepted(row):
                rejected += 1
                continue
            source = str(row.get("source", ""))
            translation = str(row.get("translation", ""))
            if not source:
                rejected += 1
                continue
            try:
                validate_translation(source, translation)
            except ValueError:
                # A few archived official-legacy rows lack original numbered
                # control tokens.  Never deploy them.  Only skip this known
                # historical provenance; current machine rows still fail closed.
                if not is_legacy_provenance(row):
                    raise
                invalid_legacy += 1
                rejected += 1
                if len(invalid_legacy_examples) < 12:
                    invalid_legacy_examples.append({
                        "bundle": row.get("bundle"),
                        "key": row.get("key"),
                        "source_sha256": source_id(source),
                    })
                continue

            bundle = str(row.get("bundle", ""))
            key = str(row.get("key", ""))
            if bundle and key:
                identity = (bundle.casefold(), key)
                prior = exact.get(identity)
                if prior and (
                    str(prior.get("source")) != source
                    or str(prior.get("translation")) != translation
                ):
                    raise ValueError(
                        f"conflicting exact translation for {bundle}/{key}"
                    )
                exact[identity] = row

            # Every accepted exact row is also useful as translation memory,
            # provided all rows for this exact JP source agree.
            sid = source_id(source)
            if not is_legacy_provenance(row):
                prior_machine = machine_candidates.get(sid)
                if prior_machine is None:
                    machine_candidates[sid] = {
                        "source": source,
                        "translation": translation,
                        "status": row.get("status"),
                        "provenance": row.get("provenance", str(path)),
                    }
                elif prior_machine["translation"] != translation:
                    machine_conflicts.add(sid)
            prior_source = source_candidates.get(sid)
            if prior_source is None:
                source_candidates[sid] = {
                    "source": source,
                    "translation": translation,
                    "status": row.get("status"),
                    "provenance": row.get("provenance", str(path)),
                }
            elif str(prior_source["translation"]) != translation:
                ambiguous.add(sid)

    # Conflicting archived translations are context-specific: keep all exact
    # bundle/key matches, but use the unique current machine translation for
    # other contexts. Two disagreeing machine rows are never silently chosen.
    for sid in ambiguous:
        if sid in machine_candidates and sid not in machine_conflicts:
            source_candidates[sid] = machine_candidates[sid]
        else:
            source_candidates.pop(sid, None)
    unresolved_ambiguities = ambiguous - (machine_candidates.keys() - machine_conflicts)
    return TranslationResolver(
        exact=exact,
        source=source_candidates,
        ambiguous_sources=unresolved_ambiguities,
        rejected_rows=rejected,
        rejected_invalid_legacy_rows=invalid_legacy,
        invalid_legacy_examples=invalid_legacy_examples,
    )


def resolve_translation(
    resolver: TranslationResolver, bundle: str, key: str, source: str
) -> tuple[str | None, str]:
    row = resolver.exact.get((bundle.casefold(), key))
    if row is not None:
        if str(row.get("source", "")) != source:
            return None, "stale_exact"
        translated = str(row.get("translation", ""))
        validate_translation(source, translated)
        return translated, "exact"

    memory = resolver.source.get(source_id(source))
    if memory is None or str(memory.get("source", "")) != source:
        return None, "unresolved"
    translated = str(memory.get("translation", ""))
    validate_translation(source, translated)
    return translated, "memory"


def cmd_audit(args: argparse.Namespace) -> int:
    resolver = build_resolver(args.translations)
    counts = Counter()
    samples: list[dict] = []
    for row in read_jsonl(args.catalogue):
        source = str(row.get("source", ""))
        bundle = str(row.get("bundle", ""))
        key = str(row.get("key", ""))
        translated, route = resolve_translation(resolver, bundle, key, source)
        counts["source_candidates"] += 1
        counts[route] += 1
        if translated is not None:
            counts["translated"] += 1
        elif len(samples) < args.sample:
            samples.append(
                {
                    "logical": row.get("logical"),
                    "bundle": bundle,
                    "key": key,
                    "source": source,
                    "reason": route,
                }
            )

    total = counts["source_candidates"]
    result = {
        "counts": dict(counts),
        "coverage": 0.0 if not total else counts["translated"] / total,
        "exact_translation_rows": len(resolver.exact),
        "unambiguous_memory_values": len(resolver.source),
        "ambiguous_memory_values": len(resolver.ambiguous_sources),
        "rejected_translation_rows": resolver.rejected_rows,
        "rejected_invalid_legacy_rows": resolver.rejected_invalid_legacy_rows,
        "invalid_legacy_examples": resolver.invalid_legacy_examples,
        "unresolved_first": samples,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if counts["translated"] == total else 2


def cmd_make_queue(args: argparse.Namespace) -> int:
    """Emit only translation-memory values not covered by accepted seeds."""
    resolver = build_resolver(args.translations)
    queued: list[dict] = []
    counts = Counter()
    for row in read_jsonl(args.memory):
        source = str(row.get("source", ""))
        if not source:
            continue
        counts["memory_values"] += 1
        sid = source_id(source)
        existing = resolver.source.get(sid)
        if existing is not None and str(existing.get("source", "")) == source:
            counts["seed_resolved"] += 1
            continue
        item = dict(row)
        item["translation"] = ""
        item["status"] = "pending"
        item["queue_reason"] = (
            "ambiguous_seed" if sid in resolver.ambiguous_sources else "unresolved"
        )
        queued.append(item)
        counts[item["queue_reason"]] += 1
    write_jsonl(args.output, queued)
    result = {
        "counts": dict(counts),
        "queue_values": len(queued),
        "output": str(args.output),
        "seed_exact_rows": len(resolver.exact),
        "seed_memory_values": len(resolver.source),
        "ambiguous_seed_values": len(resolver.ambiguous_sources),
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_build_overlay(args: argparse.Namespace) -> int:
    identity = version_identity(
        args.snapshot,
        client_version=args.client_version,
        asset_version=args.asset_version,
    )
    snapshot = load_snapshot(args.snapshot)
    objects = _selected_objects(snapshot, args.limit)
    resolver = build_resolver(args.translations)
    scope = snapshot["scope"]
    source_root = args.archive_root / scope
    output_scope = args.output_root / scope
    # A build directory belongs to exactly one immutable client/assets/index
    # identity. Record it before writing bundles so interrupted runs cannot be
    # accidentally reused for a different upstream manifest.
    identity_path = args.output_root / "version-identity.json"
    if identity_path.is_file():
        existing = json.loads(identity_path.read_text(encoding="utf-8"))
        if existing != identity:
            raise ValueError("overlay output root already belongs to another version identity")
    else:
        if output_scope.is_dir() and any(output_scope.glob("*.unity3d")):
            raise ValueError("overlay has bundles but no version identity; refuse to mix outputs")
        args.output_root.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(
            json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    manifests: list[dict] = []
    counts = Counter()

    for index, row in enumerate(objects, 1):
        source_path = source_root / row["remote"]
        name, text, _cipher = read_gtx(source_path)
        parsed = parse_records(text)
        replacements: dict[str, str] = {}
        route_counts = Counter()
        for key, source in parsed:
            if not is_source_text(source):
                continue
            translated, route = resolve_translation(resolver, name, key, source)
            route_counts[route] += 1
            if translated is not None and translated != source:
                replacements[key] = translated

        counts["bundles_scanned"] += 1
        counts["source_candidates"] += sum(route_counts.values())
        counts["resolved"] += route_counts["exact"] + route_counts["memory"]
        counts["stale_exact"] += route_counts["stale_exact"]
        # Which route answered, not just whether something did.  A key answered
        # by `memory` was answered from another bundle's row, so the bundle's
        # bytes depend on inputs that a per-bundle reuse decision cannot see;
        # `scripts/release_reuse.py` refuses to reuse a release whose keys were
        # not all answered by their own row, and it needs this split to tell.
        counts["resolved_exact"] += route_counts["exact"]
        counts["resolved_memory"] += route_counts["memory"]
        if not replacements:
            continue

        translated_text, changed = replace_records(text, replacements)
        output = output_scope / row["remote"]
        manifest = save_localized_bundle(source_path, output, translated_text)
        manifest.update(
            {
                "logical": row["logical"],
                "remote": row["remote"],
                "changed": changed,
                "exact": route_counts["exact"],
                "memory": route_counts["memory"],
            }
        )
        manifests.append(manifest)
        counts["bundles_written"] += 1
        counts["records_changed"] += changed

        if args.progress_every and index % args.progress_every == 0:
            print(
                f"progress {index}/{len(objects)} "
                f"written={counts['bundles_written']} changed={counts['records_changed']}",
                file=sys.stderr,
                flush=True,
            )

    result = {
        "schema_version": 1,
        "snapshot": str(args.snapshot),
        "version_identity": identity,
        "scope": scope,
        "output_root": str(args.output_root),
        **dict(counts),
        "exact_translation_rows": len(resolver.exact),
        "unambiguous_memory_values": len(resolver.source),
        "ambiguous_memory_values": len(resolver.ambiguous_sources),
        "rejected_translation_rows": resolver.rejected_rows,
        "rejected_invalid_legacy_rows": resolver.rejected_invalid_legacy_rows,
        "invalid_legacy_examples": resolver.invalid_legacy_examples,
        "bundles": manifests,
    }
    manifest_path = args.output_root / "localization-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "bundles"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract-snapshot")
    extract.add_argument("--snapshot", type=Path, required=True)
    extract.add_argument("--archive-root", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--memory", type=Path)
    extract.add_argument("--summary", type=Path)
    extract.add_argument("--workers", type=int, default=16)
    extract.add_argument("--limit", type=int, default=0)
    extract.add_argument("--example-count", type=int, default=3)
    extract.set_defaults(func=cmd_extract_snapshot)

    audit = sub.add_parser("audit")
    audit.add_argument("--catalogue", type=Path, required=True)
    audit.add_argument("--translations", type=Path, action="append", required=True)
    audit.add_argument("--output", type=Path)
    audit.add_argument("--sample", type=int, default=20)
    audit.set_defaults(func=cmd_audit)

    queue = sub.add_parser("make-queue")
    queue.add_argument("--memory", type=Path, required=True)
    queue.add_argument("--translations", type=Path, action="append", required=True)
    queue.add_argument("--output", type=Path, required=True)
    queue.add_argument("--summary", type=Path)
    queue.set_defaults(func=cmd_make_queue)

    build = sub.add_parser("build-overlay")
    build.add_argument("--snapshot", type=Path, required=True)
    build.add_argument("--client-version", required=True)
    build.add_argument("--asset-version", required=True)
    build.add_argument("--archive-root", type=Path, required=True)
    build.add_argument("--translations", type=Path, action="append", required=True)
    build.add_argument("--output-root", type=Path, required=True)
    build.add_argument("--limit", type=int, default=0)
    build.add_argument("--progress-every", type=int, default=500)
    build.set_defaults(func=cmd_build_overlay)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
