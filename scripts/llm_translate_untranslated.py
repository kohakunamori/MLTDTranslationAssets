#!/usr/bin/env python3
"""Prepare, apply and publish resumable LLM translation drafts.

This adapter deliberately keeps the existing translation pool as the provider
implementation.  It only converts the public Assets JSONL shape to the pool's
queue shape and writes machine output back as ``pending`` rows.  It never
changes an accepted row and never treats an LLM result as human-reviewed.

``publish`` is the separate, explicit step that admits those machine drafts to
the generated build.  It flips only ``pending`` + ``translation_stage=
llm_translated`` rows with a non-empty translation to ``status=accepted`` and
**keeps** ``translation_stage=llm_translated``, so the published release still
carries the provenance of the text it ships.  It never touches rows the machine
did not write, and it revalidates the source hash and the protected tokens of
every row it admits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipelines.text.mltd_localize_gtx import validate_translation


ROOT = Path(__file__).resolve().parents[1]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rows(root: Path):
    for path in sorted((root / "locales").rglob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                yield path, line_no, value


def collect(args: argparse.Namespace) -> int:
    wanted_version = str(args.asset_version or "").strip()
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    seen: set[str] = set()
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for path, line_no, row in rows(ROOT):
            if row.get("status") != "untranslated" or str(row.get("zh", "")):
                continue
            if wanted_version and str(row.get("asset_version")) != wanted_version:
                continue
            source = str(row.get("ja", ""))
            sid = str(row.get("source_sha256", ""))
            if not source or sid != sha256_text(source):
                raise SystemExit(f"invalid source identity at {path}:{line_no}")
            if sid in seen:
                continue
            seen.add(sid)
            stream.write(json.dumps({
                "source": source,
                "source_sha256": sid,
                "bundle": row.get("bundle", ""),
                "key": row.get("item_key", ""),
                "task": "ASSETS_TEXT",
                "asset_version": row.get("asset_version"),
                "source_client_version": row.get("source_client_version"),
                "translation": "",
                "status": "pending",
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    print(json.dumps({"queue": str(output), "items": count,
                      "asset_version": wanted_version or None}, ensure_ascii=False))
    return 0


def apply(args: argparse.Namespace) -> int:
    translations: dict[str, str] = {}
    for line in args.draft.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        source = str(row.get("source", ""))
        sid = str(row.get("source_sha256", ""))
        translation = str(row.get("translation", ""))
        if not source or sid != sha256_text(source) or not translation:
            continue
        validate_translation(source, translation)
        if "|" in translation or "^" in translation:
            raise SystemExit(f"LLM output contains reserved delimiter for {sid}")
        prior = translations.get(sid)
        if prior is not None and prior != translation:
            raise SystemExit(f"conflicting LLM output for source {sid}")
        translations[sid] = translation

    if not translations:
        print(json.dumps({"updated": 0, "drafts": 0}, ensure_ascii=False))
        return 0

    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    updated = 0
    for path in sorted((ROOT / "locales").rglob("*.jsonl")):
        original = path.read_text(encoding="utf-8")
        changed = False
        lines = []
        for line in original.splitlines():
            if not line.strip():
                lines.append(line)
                continue
            row: dict[str, Any] = json.loads(line)
            sid = str(row.get("source_sha256", ""))
            translation = translations.get(sid)
            if row.get("status") == "untranslated" and translation is not None:
                if str(row.get("ja", "")) != "":
                    validate_translation(str(row["ja"]), translation)
                row["zh"] = translation
                row["status"] = "pending"
                row["translation_stage"] = "llm_translated"
                row["updated_at"] = timestamp
                changed = True
                updated += 1
                lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            else:
                # Preserve untouched source lines byte-for-byte so a small LLM
                # draft does not rewrite an entire 300k-line JSONL file.
                lines.append(line)
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"updated": updated, "drafts": len(translations),
                      "stage": "llm_translated"}, ensure_ascii=False))
    return 0


def _draft_source_ids(path: Path) -> set[str]:
    """Source identities carried by a draft file, for a bounded publish."""
    wanted: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        sid = str(row.get("source_sha256", ""))
        if sid:
            wanted.add(sid)
    return wanted


def _identity(row: dict[str, Any]) -> tuple[str, str, str]:
    """The generated writer's own identity for a row: bundle, key, source text."""
    bundle = str(row.get("bundle", ""))
    logical = bundle if bundle.endswith(".unity3d") else bundle + ".unity3d"
    return (logical, str(row.get("item_key", "")), str(row.get("source_sha256", "")).lower())


def _translation_token(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _accepted_index(files: list[Path]) -> tuple[dict, set, dict, dict]:
    """Compact, streaming index of every accepted row.

    Returns (first translation token per identity, identities with more than one
    accepted translation, oldest human/legacy version per identity, oldest
    machine version per identity).  Only the token is kept per identity, so the
    index stays small on a 390k-row repository.
    """
    first: dict[tuple[str, str, str], bytes] = {}
    conflicting: set[tuple[str, str, str]] = set()
    human_version: dict[tuple[str, str, str], int] = {}
    machine_version: dict[tuple[str, str, str], int] = {}
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            translation = str(row.get("zh", ""))
            if row.get("status") != "accepted" or not translation:
                continue
            key = _identity(row)
            token = _translation_token(translation)
            prior = first.get(key)
            if prior is None:
                first[key] = token
            elif prior != token:
                conflicting.add(key)
            version = int(str(row.get("asset_version") or "0") or "0")
            # A missing stage is legacy curated text: never demote it.
            if row.get("translation_stage") == "llm_translated":
                machine_version[key] = min(version, machine_version.get(key, version))
            else:
                human_version[key] = min(version, human_version.get(key, version))
    return first, conflicting, human_version, machine_version


def publish(args: argparse.Namespace) -> int:
    """Admit machine drafts to the generated build without relabelling them.

    Two invariants keep the generated writer's ambiguity gate unreachable from
    machine output:

    * a row is never promoted when the same (bundle, key, source_sha256)
      already has an accepted translation -- the release already ships that
      source text, so a second accepted wording would only be ambiguous;
    * an accepted machine row that duplicates another accepted wording of the
      same source text is demoted back to ``pending`` (repair).  Legacy rows
      without a stage and human rows are never demoted.

    ``--draft`` narrows the promotion to one run's own output; without it every
    eligible row is promoted, which is what makes a first backfill of an
    already-translated asset_version possible.
    """
    wanted = _draft_source_ids(args.draft) if args.draft is not None else None
    files = sorted((ROOT / "locales").rglob("*.jsonl"))
    accepted, conflicting, human_version, machine_version = _accepted_index(files)

    promoted = 0
    demoted = 0
    skipped = 0
    untouched_files = 0
    versions: dict[str, int] = {}
    for path in files:
        original = path.read_text(encoding="utf-8")
        changed = False
        lines = []
        for line in original.splitlines():
            if not line.strip():
                lines.append(line)
                continue
            row: dict[str, Any] = json.loads(line)
            stage = row.get("translation_stage")
            translation = str(row.get("zh", ""))
            sid = str(row.get("source_sha256", ""))
            asset_version = str(row.get("asset_version"))

            # Repair: a machine row that duplicates another accepted wording.
            if row.get("status") == "accepted" and stage == "llm_translated" and _identity(row) in conflicting:
                keep = False
                if _identity(row) not in human_version:
                    keep = int(str(row.get("asset_version") or "0") or "0") == machine_version.get(_identity(row))
                if not keep:
                    row["status"] = "pending"
                    changed = True
                    demoted += 1
                    lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                    continue

            if row.get("status") != "pending" or stage != "llm_translated" or not translation:
                # Untouched lineage (human_translated, accepted, untranslated,
                # or a machine row without text) stays byte-for-byte identical.
                lines.append(line)
                continue
            if wanted is not None and sid not in wanted:
                lines.append(line)
                continue
            if _identity(row) in accepted:
                # The same source text already has an accepted translation:
                # promoting this draft would create a second one.
                skipped += 1
                lines.append(line)
                continue
            source = str(row.get("ja", ""))
            if not source or sid != sha256_text(source):
                raise SystemExit(f"source identity mismatch at {path}: refusing to publish {sid}")
            if "|" in translation or "^" in translation:
                raise SystemExit(f"LLM output contains reserved delimiter for {sid}")
            validate_translation(source, translation)
            row["status"] = "accepted"
            accepted[_identity(row)] = _translation_token(translation)
            # Deliberately not rewritten to human_translated: the release must
            # keep saying that this text is machine output.
            changed = True
            promoted += 1
            versions[asset_version] = versions.get(asset_version, 0) + 1
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        if changed:
            untouched_files += 1
            if not args.dry_run:
                path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "promoted": promoted,
        "demoted_conflicting_duplicates": demoted,
        "skipped_duplicate_source": skipped,
        "files": untouched_files,
        "asset_versions": dict(sorted(versions.items())),
        "dry_run": bool(args.dry_run),
        "stage": "llm_translated",
    }, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--output", type=Path, required=True)
    collect_parser.add_argument("--asset-version", default="")
    collect_parser.set_defaults(func=collect)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--draft", type=Path, required=True)
    apply_parser.set_defaults(func=apply)
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument(
        "--draft",
        type=Path,
        default=None,
        help="promote only the source identities present in this draft file",
    )
    publish_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the promotion without writing any file",
    )
    publish_parser.set_defaults(func=publish)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
