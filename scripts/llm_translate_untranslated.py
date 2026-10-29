#!/usr/bin/env python3
"""Prepare, apply and publish resumable LLM translation drafts.

This adapter deliberately keeps the existing translation pool as the provider
implementation. It converts the public Assets JSONL shape to the pool's queue
and writes machine output as pending rows. Accepted text is preserved except
for narrowly identified copied-Japanese repairs; a failed repair keeps the old
accepted row. It never treats machine output as human-reviewed.

``publish`` is the separate, explicit step that admits those machine drafts to
the generated build.  It flips only ``pending`` + ``translation_stage=
llm_translated`` rows with a non-empty translation to ``status=accepted`` and
**keeps** ``translation_stage=llm_translated``, so the published release still
carries the provenance of the text it ships.  It never touches rows the machine
did not write, and it revalidates the source hash and the protected tokens of
every row it admits.

``--scope`` selects which source tree a run touches: ``locales`` (the generated
text release), ``lyrics`` (``lyrics/songs/*.jsonl``, the song lyric library) or
``all``.  The default stays ``locales`` so existing callers are unaffected.
Lyric rows follow the same states as locale rows, with one genre-specific rule:
a line whose Japanese is pure ASCII is left alone on purpose (the confirmed
"English stays English" policy), so it is never queued and never promoted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipelines.text.mltd_localize_gtx import jsonl_lines, validate_translation
from pipelines.text.mltd_lyric_rules import is_english_bypass, join_lyric_lines, validate_lyric_tokens
from pipelines.core.hashing import sha256_text
from scripts.japanese_residue import repair_reason
from pipelines.text.asset_translation_context import enrich_queue


ROOT = Path(__file__).resolve().parents[1]

SCOPES = ("locales", "lyrics", "all")

#: How many rejected drafts the run summary lists by name.  The count is always
#: complete; the list is bounded so one bad batch cannot flood the CI log.
REJECTION_SAMPLE = 10


def validate_draft(kind: str, source: str, translation: str) -> None:
    """Apply the rule that will judge this row in ``scripts/validate_repo.py``.

    Lyric lines and locale rows are not judged the same way: in a lyric, ``<...>``
    is display punctuation, so its *text* may be translated, while in a locale row
    it is an engine placeholder that must survive verbatim.  Using the locale rule
    on lyric drafts both rejects correct lines and lets lyric-specific mistakes
    through to the final repository check -- where a single bad row fails the
    whole run, which is what happened in run 38058410546.
    """
    if kind == "lyrics":
        validate_lyric_tokens(source, translation)
    else:
        validate_translation(source, translation)


def scope_of(args: argparse.Namespace) -> str:
    scope = str(getattr(args, "scope", "locales") or "locales")
    if scope not in SCOPES:
        raise SystemExit(f"unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")
    return scope


def scope_paths(root: Path, scope: str) -> list[tuple[Path, str]]:
    """``(file, kind)`` pairs a scope covers.

    ``lyrics/all_lyrics.jsonl`` is deliberately excluded: it is derived from the
    per-song files by ``mltd_localize_scrobj.rebuild_aggregate``, and writing a
    translation into both would leave the two disagreeing.
    """
    pairs: list[tuple[Path, str]] = []
    if scope in ("locales", "all"):
        pairs += [(path, "locales") for path in sorted((root / "locales").rglob("*.jsonl"))]
    if scope in ("lyrics", "all"):
        pairs += [(path, "lyrics") for path in sorted((root / "lyrics" / "songs").glob("*.jsonl"))]
    return pairs


def row_scope_key(row: dict[str, Any]) -> str:
    """Key identifying a row inside its bundle: item_key, else the lyric index."""
    key = str(row.get("item_key", ""))
    if key:
        return key
    index = row.get("index")
    return "" if index is None else str(index)


def _logical_bundle(row: dict[str, Any]) -> str:
    bundle = str(row.get("bundle", ""))
    return bundle if bundle.endswith(".unity3d") else bundle + ".unity3d"


def rows(root: Path, scope: str = "locales"):
    for path, kind in scope_paths(root, scope):
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                yield path, line_no, value, kind


def collect(args: argparse.Namespace) -> int:
    scope = scope_of(args)
    wanted_version = str(args.asset_version or "").strip()
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    queue: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, line_no, row, kind in queue_rows(ROOT, scope):
        repair = repair_reason(row) if kind == "locales" else None
        if not repair and (row.get("status") != "untranslated" or str(row.get("zh", ""))):
            continue
        source = str(row.get("ja", ""))
        if kind == "lyrics" and is_english_bypass(source):
            # Confirmed policy: a pure-English lyric line stays English
            # ("english bypass").  Queueing it would spend tokens and then
            # fight the policy on every run.
            continue
        if wanted_version and kind == "locales" and str(row.get("asset_version")) != wanted_version:
            continue
        sid = str(row.get("source_sha256", ""))
        if not source or sid != sha256_text(source):
            raise SystemExit(f"invalid source identity at {path}:{line_no}")
        if sid in seen:
            continue
        seen.add(sid)
        queue.append({
            "source": source,
            "source_sha256": sid,
            "bundle": row.get("bundle", ""),
            "key": row_scope_key(row),
            "task": "ASSETS_LYRICS" if kind == "lyrics" else "ASSETS_TEXT",
            "asset_version": row.get("asset_version"),
            "source_client_version": row.get("source_client_version"),
            "translation": "",
            "status": "pending",
            "repair_reason": repair,
        })
    enrich_queue(ROOT, queue, scope_paths(ROOT, scope))
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for queued in queue:
            stream.write(json.dumps(queued, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps({"queue": str(output), "items": len(queue),
                      "scope": scope,
                      "asset_version": wanted_version or None}, ensure_ascii=False))
    return 0


def queue_rows(root: Path, scope: str):
    """Repair confirmed published defects before draining new source backlog."""
    for candidate in rows(root, scope):
        if candidate[3] == "locales" and repair_reason(candidate[2]):
            yield candidate
    for candidate in rows(root, scope):
        if candidate[2].get("status") == "untranslated":
            yield candidate


def apply(args: argparse.Namespace) -> int:
    """Write draft translations into their source rows, skipping unusable ones.

    A draft is refused -- never written, and reported -- when it carries a
    reserved delimiter, when two drafts disagree about one source line, or when it
    breaks the rule its own surface will be validated with.  Refusing one draft
    must not discard the other thousands: that is the difference between a queue
    that drains and a run that fails whole.
    """
    scope = scope_of(args)
    translations: dict[str, str] = {}
    rejected: list[dict[str, str]] = []
    for line in jsonl_lines(args.draft.read_text(encoding="utf-8-sig")):
        if not line.strip():
            continue
        row = json.loads(line)
        source = str(row.get("source", ""))
        sid = str(row.get("source_sha256", ""))
        translation = str(row.get("translation", ""))
        if not source or sid != sha256_text(source) or not translation:
            continue
        if "|" in translation or "^" in translation:
            rejected.append({"source_sha256": sid, "reason": "reserved delimiter"})
            continue
        prior = translations.get(sid)
        if prior is not None and prior != translation:
            rejected.append({"source_sha256": sid, "reason": "draft disagrees with itself"})
            continue
        translations[sid] = translation

    if not translations:
        print(json.dumps({"updated": 0, "drafts": 0, "skipped_invalid": len(rejected),
                          "rejected": rejected[:REJECTION_SAMPLE], "scope": scope},
                         ensure_ascii=False))
        return 0

    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    updated = 0
    for path, kind in scope_paths(ROOT, scope):
        original = path.read_text(encoding="utf-8")
        changed = False
        lines = []
        for line in jsonl_lines(original):
            if not line.strip():
                lines.append(line)
                continue
            row: dict[str, Any] = json.loads(line)
            sid = str(row.get("source_sha256", ""))
            translation = translations.get(sid)
            repair = repair_reason(row) if kind == "locales" else None
            if (row.get("status") == "untranslated" or repair) and translation is not None:
                source_text = str(row.get("ja", ""))
                if sha256_text(source_text) != sid:
                    raise ValueError(f"source hash mismatch at {path}")
                candidate = translation
                if kind == "lyrics":
                    # A lyric slot renders one line, so the source's break can only
                    # become a space.  The model mirrors that break even when the
                    # prompt forbids it -- runs #26, #27 and #28 all answered
                    # scrobj_priam+.unity3d #3296 with two lines -- and joining the
                    # halves keeps a sentence that is otherwise correct.  A break the
                    # source never had is left alone and still refused.
                    candidate = join_lyric_lines(source_text, translation)
                if source_text:
                    try:
                        validate_draft(kind, source_text, candidate)
                        if kind == "locales" and repair_reason(dict(row, status="accepted", zh=candidate)):
                            raise ValueError("draft still contains copied Japanese")
                    except ValueError as exc:
                        rejected.append({
                            "source_sha256": sid,
                            "file": path.name,
                            "index": str(row.get("index", "")),
                            "reason": str(exc),
                        })
                        lines.append(line)
                        continue
                row["zh"] = candidate
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
                      "skipped_invalid": len(rejected),
                      "rejected": rejected[:REJECTION_SAMPLE],
                      "scope": scope, "stage": "llm_translated"}, ensure_ascii=False))
    return 0


def reuse(args: argparse.Namespace) -> int:
    """Fill an untranslated lyric row from an accepted row with the same source.

    One Japanese line appears in several bundle renditions (``scrobj_priam+.unity3d``
    and ``scrobj_priam.unity3d`` carry the same slot).  The library's own convention
    is that every copy carries the same Chinese: of the 2,651 sources that appear in
    more than one row, 2,324 have byte-identical translations and none diverge.

    Translating the copies again is not free.  It spends tokens, it makes one Japanese
    line read three different ways across bundles, and it can fail a rule the original
    already satisfied: the ``+`` renditions were stuck on rows whose Japanese source
    contains a line break, which the model faithfully reproduces and
    :func:`validate_draft` always refuses for lyrics.  Both runs of 2026-10-12
    produced the same two-line draft for those five rows, so no amount of retrying
    would have settled them.

    A copy is written ``accepted`` and keeps the provenance of the row it came from
    (``human_translated`` stays ``human_translated``); a twin that predates the
    ``translation_stage`` field leaves the copy without one, exactly like its
    neighbours.  Every copy is re-validated against *its own* row first, so this can
    never publish text the repository would refuse.
    """
    scope = scope_of(args)
    if scope != "lyrics":
        raise SystemExit("reuse only applies to lyrics; a locale row's wording belongs to its own build")
    dry_run = bool(getattr(args, "dry_run", False))

    accepted: dict[str, tuple[str, str | None]] = {}
    for path, _kind in scope_paths(ROOT, scope):
        for _line_no, raw in enumerate(jsonl_lines(path.read_text(encoding="utf-8")), 1):
            if not raw.strip():
                continue
            row = json.loads(raw)
            if row.get("status") != "accepted":
                continue
            translation = str(row.get("zh", ""))
            source = str(row.get("ja", ""))
            sid = str(row.get("source_sha256", ""))
            if not translation or not source or sid != sha256_text(source):
                continue
            stage = row.get("translation_stage")
            accepted.setdefault(sid, (translation, stage if isinstance(stage, str) else None))

    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    updated = 0
    reused: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    for path, kind in scope_paths(ROOT, scope):
        lines = []
        changed = False
        for line in jsonl_lines(path.read_text(encoding="utf-8")):
            if not line.strip():
                lines.append(line)
                continue
            row: dict[str, Any] = json.loads(line)
            if row.get("status") != "untranslated" or str(row.get("zh", "")):
                lines.append(line)
                continue
            source = str(row.get("ja", ""))
            sid = str(row.get("source_sha256", ""))
            twin = accepted.get(sid)
            if twin is None or not source or sid != sha256_text(source):
                lines.append(line)
                continue
            translation, stage = twin
            try:
                validate_draft(kind, source, translation)
            except ValueError as exc:
                rejected.append({
                    "source_sha256": sid,
                    "file": path.name,
                    "index": str(row.get("index", "")),
                    "reason": str(exc),
                })
                lines.append(line)
                continue
            row["zh"] = translation
            row["status"] = "accepted"
            if stage:
                row["translation_stage"] = stage
            else:
                row.pop("translation_stage", None)
            row["updated_at"] = timestamp
            changed = True
            updated += 1
            if len(reused) < REJECTION_SAMPLE:
                reused.append({
                    "source_sha256": sid,
                    "file": path.name,
                    "index": str(row.get("index", "")),
                })
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        # Untouched rows keep their exact bytes: a copy touches a handful of lines, not
        # the whole 13,729-row library.
        if changed and not dry_run:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "updated": updated,
        "dry_run": dry_run,
        "skipped_invalid": len(rejected),
        "rejected": rejected[:REJECTION_SAMPLE],
        "reused": reused,
        "scope": scope,
        "stage": "reused",
    }, ensure_ascii=False))
    return 0


def _draft_source_ids(path: Path) -> set[str]:
    """Source identities carried by a draft file, for a bounded publish."""
    wanted: set[str] = set()
    for line in jsonl_lines(path.read_text(encoding="utf-8-sig")):
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
        for line in jsonl_lines(path.read_text(encoding="utf-8")):
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
    """Admit machine drafts to the build, per scope."""
    scope = scope_of(args)
    if scope == "locales":
        return _publish_locales(args)
    if scope == "lyrics":
        return _publish_lyrics(args)
    status = _publish_locales(args)
    return _publish_lyrics(args) or status


def _publish_locales(args: argparse.Namespace) -> int:
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
        for line in jsonl_lines(original):
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
        "scope": "locales",
        "stage": "llm_translated",
    }, ensure_ascii=False, sort_keys=True))
    return 0


def _publish_lyrics(args: argparse.Namespace) -> int:
    """Promote machine-translated lyric rows to ``accepted``.

    Simpler than the locale promotion on purpose.  The invariant worth enforcing
    is agreement -- the same Japanese line inside one song must not end up with
    two different Chinese wordings, because the game shows them as the same line
    and the published bundle can only carry one.  If any two rows of one song
    disagree, all of them stay ``pending`` for a human instead of the newest one
    winning.  Each row is also checked with the same lyric rule
    ``scripts/validate_repo.py`` applies, and an unusable row is skipped and
    reported instead of failing the whole promotion.
    """
    wanted = _draft_source_ids(args.draft) if args.draft is not None else None
    files = [path for path, _kind in scope_paths(ROOT, "lyrics")]

    tokens: dict[tuple[str, str], set[bytes]] = {}
    for path in files:
        for line in jsonl_lines(path.read_text(encoding="utf-8")):
            if not line.strip():
                continue
            row = json.loads(line)
            translation = str(row.get("zh", ""))
            if not translation:
                continue
            identity = (_logical_bundle(row), str(row.get("source_sha256", "")))
            tokens.setdefault(identity, set()).add(_translation_token(translation))

    promoted = 0
    skipped_conflicting = 0
    skipped_invalid = 0
    rejected: list[dict[str, str]] = []
    changed_files = 0
    for path in files:
        original = path.read_text(encoding="utf-8")
        changed = False
        lines = []
        for line in jsonl_lines(original):
            if not line.strip():
                lines.append(line)
                continue
            row: dict[str, Any] = json.loads(line)
            translation = str(row.get("zh", ""))
            if row.get("status") != "pending" or row.get("translation_stage") != "llm_translated" or not translation:
                lines.append(line)
                continue
            sid = str(row.get("source_sha256", ""))
            if wanted is not None and sid not in wanted:
                lines.append(line)
                continue
            identity = (_logical_bundle(row), sid)
            if len(tokens.get(identity, set())) > 1:
                skipped_conflicting += 1
                lines.append(line)
                continue
            source = str(row.get("ja", ""))
            if not source or sid != sha256_text(source):
                raise SystemExit(f"source identity mismatch at {path}: refusing to publish {sid}")
            try:
                if "|" in translation or "^" in translation:
                    raise ValueError("reserved delimiter")
                validate_draft("lyrics", source, translation)
            except ValueError as exc:
                skipped_invalid += 1
                rejected.append({"file": path.name, "source_sha256": sid,
                                 "index": str(row.get("index", "")), "reason": str(exc)})
                lines.append(line)
                continue
            row["status"] = "accepted"
            changed = True
            promoted += 1
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        if changed:
            changed_files += 1
            if not args.dry_run:
                path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "promoted": promoted,
        "skipped_conflicting_wording": skipped_conflicting,
        "skipped_invalid": skipped_invalid,
        "rejected": rejected[:REJECTION_SAMPLE],
        "files": changed_files,
        "dry_run": bool(args.dry_run),
        "scope": "lyrics",
        "stage": "llm_translated",
    }, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--output", type=Path, required=True)
    collect_parser.add_argument("--asset-version", default="")
    collect_parser.add_argument("--scope", choices=SCOPES, default="locales")
    collect_parser.set_defaults(func=collect)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--draft", type=Path, required=True)
    apply_parser.add_argument("--scope", choices=SCOPES, default="locales")
    apply_parser.set_defaults(func=apply)
    reuse_parser = sub.add_parser("reuse")
    reuse_parser.add_argument(
        "--scope",
        choices=("lyrics",),
        default="lyrics",
        help="only lyrics: a locale row's wording belongs to its own build",
    )
    reuse_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the copies without writing any file",
    )
    reuse_parser.set_defaults(func=reuse)
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument(
        "--draft",
        type=Path,
        default=None,
        help="promote only the source identities present in this draft file",
    )
    publish_parser.add_argument(
        "--scope",
        choices=SCOPES,
        default="locales",
        help="which source tree to promote (default: the generated text release)",
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
