#!/usr/bin/env python3
"""Strict JP 9.0.200 / 1077100 event-unit sub-title write-back materializer.

Frozen identity is explicit and fail-closed: ``--client-version`` must be the
frozen JP client and ``--asset-version`` is re-derived from the archived source
snapshot's ``upstream_root`` (never inferred from a directory name or the newest
remote).  The 852 ``event_unit_talk_*.unity3d`` originals are read from the
read-only 1077100 source cohort; every one of them must still contain exactly
two Unity objects and exactly one TextAsset whose ``m_Name`` matches the
indexed logical name.

Only the text-localization owner's independently reviewed ledger is accepted:
every row needs ``release_gate="accepted"``, ``qa_verdict="PASS"``, an
independent reviewer identity and a re-run of the current deterministic QA plus
the current release policy.  Raw machine JSONL, legacy seed, smoke, benchmark,
pilot and synthetic evidence files are refused by name, and rows without
``release_gate`` are refused regardless of file name.  No reviewer identity is
never treated as approval.

Write-back is verified, not assumed: the bundle is re-serialized with the
original UnityFS flags, reloaded, and then each command's non-text fields, every
other serialized Unity object's raw bytes, the TextAsset name/path id and the
canonical JSON encoding are compared.  Output is written to a NEW isolated
root only; the frozen source cohort, the canonical asset archive and
``build/localization-90200`` are never written.

This script is offline: it does not test the game client, does not contact the
asset server, and never publishes.  ``--allow-partial`` produces
``release_ready=false`` isolated trial output and is never a release candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import UnityPy

from scripts.build_fontrender_overlay import load_release_translations
from scripts.build_translation_release_ledger import (
    independence_reasons,
    review_is_fresh,
    reviewer_identity,
)
from scripts.localization_version_identity import version_identity
from pipelines.text.mltd_localize_gtx import (
    SOURCE_TEXT_RE,
    read_jsonl,
    translation_status_is_accepted,
    validate_translation,
)
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id
from scripts.mltd_translation_release_gate import classify, is_official

# ------------------------------------------------------------------ frozen identity

FROZEN_CLIENT_VERSION = "9.0.200"
FROZEN_ASSET_VERSION = "1077100"

DATA_DIR = REPO / "work" / "local-assets" / "jp-android"
INDEX = DATA_DIR / "d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
INDEX_SHA = "d7631544b9b1c7c0bea2729fbfdb688bf61d612a695be8233b36082738201e01"
SNAPSHOT = REPO / "build" / "localization-90200" / "jp-gtx-cache-snapshot.json"
SNAPSHOT_SHA = "5761f6e570b2272ad8d64c7205dd75d157d747d2f3f23a0e8a7e3a91db1bee74"

STAGE = REPO / "work" / "agents" / "image-localization" / "reviewed937-texture-stage"
SOURCE_ROOT = STAGE / "event-unit-source1077100"
COHORT = STAGE / "event-unit-source1077100-index.json"
COHORT_SHA = "be6bdc3455b3e3420c099c185cced3dd59c3699389170bac0b873746cfb98e4d"
AUDIT = STAGE / "event-unit-source1077100-independent-audit.json"
AUDIT_SHA = "ded99e40694c304e87dd05e30fbfa60f76101b7636666b18f39b81d64a606255"
QUEUE = REPO / "build" / "localization-90200" / "event-unit-translation-queue.jsonl"
QUEUE_SHA = "094b1ae099c679b86be8d53f4415076fa5f0eb576e29c860ff1003866d30c549"
GLOSSARY = REPO / "localization" / "quality" / "glossary.json"
GLOSSARY_SHA = "b472b896bd819ebb1170a14563624283c542de99abd2a160b838cd43967d5946"

EXPECTED_SOURCE_BUNDLES = 852
EXPECTED_UNIQUE_SOURCES = 13177
EXPECTED_OCCURRENCES = 13339

# Evidence whose file name announces itself as non-production.  This is a second
# line of defence only: the row-level gate below rejects these files' contents
# anyway because they carry no release_gate.
FORBIDDEN_EVIDENCE_MARKERS = ("pilot", "synthetic", "legacy", "smoke", "benchmark")
FORBIDDEN_OUTPUT_ROOTS = (
    REPO / "work" / "local-assets",
    REPO / "build" / "localization-90200",
)
JSON_DUMPS_KWARGS = {"ensure_ascii": False, "indent": 2}


class GateError(ValueError):
    """A frozen identity, provenance or reviewer-independence gate refused input."""


def sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve(value: Path) -> Path:
    return Path(value).resolve() if Path(value).is_absolute() else (REPO / value).resolve()


# ------------------------------------------------------------------ source identity


def frozen_version(args_client: str, args_asset: str, asset_index: Path) -> dict:
    """Fail-closed identity: the client is pinned, the assets are re-derived."""
    if args_client != FROZEN_CLIENT_VERSION:
        raise GateError(
            f"client version must be the frozen {FROZEN_CLIENT_VERSION}, got {args_client!r}"
        )
    if sha_file(SNAPSHOT) != SNAPSHOT_SHA:
        raise GateError(f"archived source snapshot changed: {SNAPSHOT}")
    identity = version_identity(
        SNAPSHOT, client_version=args_client, asset_version=args_asset,
        asset_index=asset_index,
    )
    if identity["asset_index_sha256"] != INDEX_SHA:
        raise GateError("archived asset index is not the frozen 1077100 index")
    if identity["assets_version"] != FROZEN_ASSET_VERSION:
        raise GateError(
            f"assets version must be the frozen {FROZEN_ASSET_VERSION}, "
            f"got {identity['assets_version']!r}"
        )
    return identity


def read_frozen_queue() -> dict[str, dict]:
    if sha_file(QUEUE) != QUEUE_SHA:
        raise GateError(f"frozen event-unit source queue changed: {QUEUE}")
    source: dict[str, dict] = {}
    for row in read_jsonl(QUEUE):
        sid = str(row.get("source_sha256", ""))
        text = row.get("source")
        if not isinstance(text, str) or source_id(text) != sid:
            raise GateError("frozen event-unit queue row has an invalid source identity")
        if sid in source:
            raise GateError(f"duplicate frozen event-unit source SHA: {sid}")
        source[sid] = row
    if len(source) != EXPECTED_UNIQUE_SOURCES:
        raise GateError(
            f"frozen event-unit queue drift: {len(source)} unique sources, "
            f"expected {EXPECTED_UNIQUE_SOURCES}"
        )
    return source


def read_frozen_cohort() -> list[dict]:
    if sha_file(COHORT) != COHORT_SHA:
        raise GateError(f"frozen event-unit source index changed: {COHORT}")
    cohort = json.loads(COHORT.read_text(encoding="utf-8"))
    if (cohort.get("complete") is not True
            or cohort.get("verified") != EXPECTED_SOURCE_BUNDLES
            or cohort.get("selected") != EXPECTED_SOURCE_BUNDLES
            or cohort.get("failed") not in (0, [], None)
            or cohort.get("source_index_sha256") != INDEX_SHA
            or cohort.get("source_queue_sha256") != QUEUE_SHA
            or cohort.get("scope") not in (None, "jp-android")
            or len(cohort.get("bundles", [])) != EXPECTED_SOURCE_BUNDLES):
        raise GateError("frozen event-unit source cohort is not the audited 852-bundle set")
    return cohort["bundles"]


def read_frozen_audit() -> dict:
    if sha_file(AUDIT) != AUDIT_SHA:
        raise GateError(f"independent event-unit source audit changed: {AUDIT}")
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    if (audit.get("status") != "passed"
            or audit.get("bundles") != EXPECTED_SOURCE_BUNDLES
            or audit.get("source_text_unique") != EXPECTED_UNIQUE_SOURCES
            or audit.get("source_text_occurrences") != EXPECTED_OCCURRENCES
            or not audit.get("source_id_multiset_equal")
            or not audit.get("all_original_sha256_verified")
            or audit.get("source_queue_sha256") != QUEUE_SHA):
        raise GateError("independent event-unit source audit did not pass")
    return audit


# ------------------------------------------------------------------ release gate


def assert_ledger_shaped(path: Path) -> int:
    """Refuse a file whose rows carry no explicit ``release_gate`` at all.

    The file name is not evidence: the production machine JSONL is genuinely
    named ``machine-translations-*.jsonl`` and has 12,811 rows with zero gates,
    while a real owner ledger writes ``release_gate`` on *every* row it emits
    (``accepted``, ``needs_review`` or ``rejected``).  Streamed so that the
    multi-hundred-megabyte GTX machine file never has to be materialised in
    memory just to be refused.  An empty file is accepted because coverage
    reporting (0 of 13,177) is more useful evidence than an input error.
    """
    rows = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            rows += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise GateError(f"{path}:{number}: invalid ledger JSON: {exc}") from exc
            if not isinstance(row, dict) or "release_gate" not in row:
                raise GateError(
                    f"non-production translation evidence is refused: {path} "
                    f"(row {number} carries no explicit release_gate; "
                    "a raw machine/queue JSONL is not a reviewed ledger)"
                )
    return rows


def load_gate_accepted(paths: list[Path], source: dict[str, dict], glossary: dict,
                       *, label: str = "frozen event-unit queue") -> dict:
    """Owner-reviewed ledger rows only; a release_gate string alone is not enough."""
    for path in paths:
        lowered = str(path).lower()
        for marker in FORBIDDEN_EVIDENCE_MARKERS:
            if marker in lowered:
                raise GateError(
                    f"non-production translation evidence is refused: {path} "
                    f"(contains {marker!r})"
                )
        if not path.is_file():
            raise GateError(f"translation ledger missing: {path}")
        assert_ledger_shaped(path)
    # Reuses the FontRender/GTX loader: release_gate=accepted, exact source
    # identity, protected-token validation and duplicate-conflict refusal.
    raw = load_release_translations(paths)
    accepted: dict[str, dict] = {}
    for sid, row in raw.items():
        if sid not in source:
            raise GateError(f"accepted row is not in the {label}: {sid}")
        text = source[sid]["source"]
        if str(row.get("source", "")) != text:
            raise GateError(f"accepted row binds to a different original text: {sid}")
        if str(row.get("release_gate", "")).strip().lower() != "accepted":
            raise GateError(f"accepted ledger row lost its explicit release_gate: {sid}")
        if str(row.get("qa_verdict", "")).upper() != "PASS":
            raise GateError(f"accepted ledger row has no deterministic QA PASS: {sid}")
        if str(row.get("status", "")) and not translation_status_is_accepted(str(row.get("status"))):
            raise GateError(f"accepted ledger row has a non-accepted status: {sid}")
        if not is_official(row):
            review = row.get("review")
            known, _identity = reviewer_identity(review)
            if not known:
                raise GateError(
                    f"accepted ledger row has no independent reviewer identity: {sid}"
                )
            if not review_is_fresh(review, sid, text, str(row.get("translation", ""))):
                raise GateError(f"accepted ledger row carries a stale review payload: {sid}")
        qa = evaluate_row({"source_sha256": sid, "source": text}, row, glossary)
        if str(qa.get("qa_verdict", "")).upper() != "PASS":
            raise GateError(f"current deterministic QA did not accept: {sid}")
        verdict, _ = classify(
            row, qa, row.get("review"), row.get("risk"), row.get("second_review")
        )
        if verdict != "accepted":
            raise GateError(f"current release policy did not accept: {sid}")
        reasons = independence_reasons(row, row.get("review"), row.get("second_review"))
        if reasons:
            raise GateError(f"reviewer independence not established: {sid} {reasons}")
        accepted[sid] = row
    return accepted


# ------------------------------------------------------------------ bundle write-back


def inspect_textasset(env, logical: str):
    """Exactly two objects, exactly one TextAsset, indexed original name."""
    objects = list(env.objects)
    if len(objects) != 2:
        raise GateError(
            f"{logical}: expected 2 Unity objects, found {len(objects)}"
        )
    text_assets = [obj for obj in objects if obj.type.name == "TextAsset"]
    if len(text_assets) != 1:
        raise GateError(
            f"{logical}: expected exactly one TextAsset, found {len(text_assets)}"
        )
    obj = text_assets[0]
    data = obj.read()
    if str(data.m_Name) != logical.removesuffix(".unity3d"):
        raise GateError(
            f"{logical}: TextAsset name {data.m_Name!r} does not match the indexed logical"
        )
    raw = bytes(data.m_Script)
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"{logical}: TextAsset is not the event_unit commands JSON: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) != {"commands"} or not isinstance(doc["commands"], list):
        raise GateError(f"{logical}: TextAsset is not the expected event_unit commands object")
    canonical = json.dumps(doc, **JSON_DUMPS_KWARGS).encode("utf-8")
    if canonical != raw:
        raise GateError(f"{logical}: script is not canonically encoded; refusing blind rewrite")
    return obj, data, raw, doc


def plan_commands(doc: dict, translations: dict) -> tuple[dict, list[dict], set[str]]:
    """Only commands[*].text may change; every other field is compared verbatim."""
    edited = json.loads(json.dumps(doc, ensure_ascii=False))
    changes: list[dict] = []
    missing: set[str] = set()
    for index, (before, after) in enumerate(zip(doc["commands"], edited["commands"])):
        if not isinstance(before, dict) or set(before) != set(after):
            raise GateError(f"event command {index} structure changed while copying")
        if not isinstance(before.get("text"), str):
            raise GateError(f"event command {index} has an unsupported text field")
        original = before["text"]
        if not original or not SOURCE_TEXT_RE.search(original):
            if after["text"] != original:
                raise GateError(f"event command {index} non-translatable text changed")
            continue
        sid = source_id(original)
        row = translations.get(sid)
        if row is None:
            missing.add(sid)
            continue
        if str(row.get("source", "")) != original:
            raise GateError(f"accepted translation binds to a different original: {sid}")
        localized = str(row["translation"])
        validate_translation(original, localized)
        if localized == original:
            continue
        after["text"] = localized
        changes.append({
            "command_index": index,
            "command": before.get("command"),
            "source_sha256": sid,
            "original": original,
            "localized": localized,
        })
    if len(doc["commands"]) != len(edited["commands"]):
        raise GateError("event command count changed")
    return edited, changes, missing


def materialize_bundle(
    logical: str,
    remote: str,
    declared_bytes: int,
    original: Path,
    output: Path,
    translations: dict,
    require_complete: bool,
) -> dict:
    if original.name != remote or output.name != remote:
        raise GateError(f"{logical}: original/output must keep the official remote name")
    if original.resolve() == output.resolve():
        raise GateError(f"{logical}: source and output path are identical")
    if not original.is_file() or original.stat().st_size != declared_bytes:
        raise GateError(f"{logical}: original bundle missing or declared-size mismatch")
    source_sha = sha_file(original)

    env = UnityPy.load(str(original))
    object_map = {(int(obj.path_id), obj.type.name) for obj in env.objects}
    non_text_raw = {
        (int(obj.path_id), obj.type.name): sha_bytes(obj.get_raw_data())
        for obj in env.objects if obj.type.name != "TextAsset"
    }
    obj, data, raw, doc = inspect_textasset(env, logical)
    new_doc, changes, missing = plan_commands(doc, translations)
    if missing and require_complete:
        raise GateError(
            f"{logical}: {len(missing)} source IDs have no gate-accepted translation"
        )
    # Every bundle is written back through the same verified path, including the
    # (rare) case where an accepted translation is byte-identical to its source.
    # Re-serializing an untouched script still has to prove object/field identity,
    # and it keeps one code path instead of a silent copy shortcut.
    encoded = json.dumps(new_doc, **JSON_DUMPS_KWARGS).encode("utf-8")
    if json.loads(encoded.decode("utf-8")) != new_doc:
        raise GateError(f"{logical}: localized script failed a JSON roundtrip")
    data.m_Script = encoded
    data.save()

    files = list(env.files.values())
    if len(files) != 1 or not hasattr(files[0], "save"):
        raise GateError(f"{logical}: unsupported UnityFS layout")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".writing")
    if temporary.exists():
        raise FileExistsError(str(temporary))
    try:
        temporary.write_bytes(files[0].save(packer="original"))
        check = UnityPy.load(str(temporary))
        if {(int(o.path_id), o.type.name) for o in check.objects} != object_map:
            raise GateError(f"{logical}: Unity object map changed on write-back")
        for other in check.objects:
            if other.type.name == "TextAsset":
                continue
            key = (int(other.path_id), other.type.name)
            if sha_bytes(other.get_raw_data()) != non_text_raw[key]:
                raise GateError(f"{logical}: non-text Unity object bytes changed: {key}")
        check_obj, _check_data, saved, parsed = inspect_textasset(check, logical)
        if int(check_obj.path_id) != int(obj.path_id):
            raise GateError(f"{logical}: TextAsset path id changed")
        if saved != encoded or parsed != new_doc:
            raise GateError(f"{logical}: TextAsset script failed the UnityFS roundtrip")
        if sha_file(original) != source_sha:
            raise GateError(f"{logical}: frozen source bundle was modified during write-back")
        recording = check_obj.read()
        if str(recording.m_Name) != logical.removesuffix(".unity3d"):
            raise GateError(f"{logical}: TextAsset name changed on write-back")
        for index, (before, after) in enumerate(zip(doc["commands"], parsed["commands"])):
            if {k: v for k, v in before.items() if k != "text"} != {
                    k: v for k, v in after.items() if k != "text"}:
                raise GateError(f"{logical}: non-text command field changed at index {index}")
            if before["text"] != after["text"] and index not in {
                    change["command_index"] for change in changes}:
                raise GateError(f"{logical}: unexpected text change at index {index}")
        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "logical": logical, "remote": remote,
        "source_sha256": source_sha, "original_bytes": declared_bytes,
        "localized_sha256": sha_file(output),
        "output_bytes": output.stat().st_size,
        "localized_bytes": output.stat().st_size,
        "source_script_sha256": sha_bytes(raw),
        "output_script_sha256": sha_bytes(encoded),
        "source_object_count": len(object_map),
        "roundtrip_verified": True,
        "non_text_objects_byte_identical": True,
        "canonical_json_roundtrip_verified": True,
        "missing_source_count": len(missing),
        "changes": changes,
        "output_path": str(output),
    }


# ------------------------------------------------------------------ driver


def refuse_output_root(dest: Path) -> None:
    if dest.exists() or dest.with_name(dest.name + ".incomplete").exists():
        raise FileExistsError("event-unit output root must be NEW and isolated")
    for forbidden in FORBIDDEN_OUTPUT_ROOTS:
        if dest.is_relative_to(forbidden.resolve()):
            raise GateError(f"refusing to write inside a read-only/canonical root: {forbidden}")
    source_root = SOURCE_ROOT.resolve()
    if dest.is_relative_to(source_root) or source_root.is_relative_to(dest / "x"):
        raise GateError("refusing to write inside the frozen event-unit source cohort")


def run(args) -> int:
    translation_paths = [_resolve(path) for path in args.translations]
    dest = _resolve(args.output_root)
    identity = frozen_version(args.client_version, args.asset_version, INDEX)
    if sha_file(INDEX) != INDEX_SHA:
        raise GateError(f"frozen 1077100 asset index changed: {INDEX}")
    source = read_frozen_queue()
    cohort = read_frozen_cohort()
    audit = read_frozen_audit()
    glossary_path = GLOSSARY if GLOSSARY.is_file() else None
    if glossary_path is not None and sha_file(glossary_path) != GLOSSARY_SHA:
        raise GateError(f"localization glossary changed: {glossary_path}")
    glossary = load_glossary(glossary_path)
    translations = load_gate_accepted(translation_paths, source, glossary)

    missing = sorted(set(source) - set(translations))
    preflight = {
        "kind": "event-unit-90200-release-preflight",
        "version_identity": identity,
        "source_bundles": EXPECTED_SOURCE_BUNDLES,
        "frozen_source_audit": audit["status"],
        "unique_sources": len(source),
        "release_accepted_source_values": len(translations),
        "missing_release_accepted": len(missing),
        "require_complete": args.require_complete,
        "ready": not missing,
        "output_created": False,
    }
    print(json.dumps(preflight, ensure_ascii=False, indent=2))
    if args.preflight_only:
        return 0 if (not missing or not args.require_complete) else 3
    if missing and args.require_complete:
        raise GateError(
            f"{len(missing)} event-unit source IDs have no gate-accepted translation; "
            "an owner-reviewed accepted ledger is required"
        )
    refuse_output_root(dest)
    if args.bundle_limit is not None:
        if args.require_complete:
            raise GateError("--bundle-limit is an isolated trial switch; it forces --allow-partial")
        if not 1 <= args.bundle_limit <= EXPECTED_SOURCE_BUNDLES:
            raise GateError(f"--bundle-limit must be within 1..{EXPECTED_SOURCE_BUNDLES}")
    work = cohort[:args.bundle_limit] if args.bundle_limit else cohort

    temporary = dest.with_name(dest.name + ".incomplete")
    temporary.mkdir(parents=True)
    records: list[dict] = []
    occurrences = 0
    source_shas_before = {}
    try:
        for row in work:
            remote, logical = row["remote"], row["logical"]
            original = SOURCE_ROOT / remote
            source_shas_before[remote] = sha_file(original)
            if source_shas_before[remote] != row["sha256"]:
                raise GateError(f"frozen source bytes changed: {logical}")
            output = temporary / identity["scope"] / remote
            record = materialize_bundle(
                logical, remote, row["declared_bytes"], original, output,
                translations, args.require_complete,
            )
            if record.get("missing_source_count") and args.require_complete:
                raise GateError(f"event-unit bundle has untranslated text: {logical}")
            if record["source_sha256"] != row["sha256"]:
                raise GateError(f"materializer bound to a different source: {logical}")
            record["source_path"] = str(original)
            record["output_path"] = str(dest / identity["scope"] / remote)
            occurrences += len(record["changes"])
            records.append(record)
        if len(records) != len(work):
            raise GateError("event-unit source selection was truncated")
        if args.require_complete and len(translations) != EXPECTED_UNIQUE_SOURCES:
            raise GateError("complete mode needs all 13177 gate-accepted source IDs")
        modified = [
            remote for remote, digest in source_shas_before.items()
            if sha_file(SOURCE_ROOT / remote) != digest
        ]
        if modified:
            raise GateError(f"frozen source cohort modified during the run: {modified[:4]}")
        manifest = {
            "schema_version": 1,
            "kind": "mltd-event-unit-full-textasset-localization-overlay",
            "app_version": args.client_version,
            "asset_version": args.asset_version,
            "scope": identity["scope"],
            "output_root": str(dest),
            "bundle_root": str(SOURCE_ROOT),
            "source_index_sha256": INDEX_SHA,
            "event_unit_source_audit_sha256": sha_file(AUDIT),
            "source_cohort_index_sha256": sha_file(COHORT),
            "source_queue_sha256": sha_file(QUEUE),
            "snapshot_sha256": sha_file(SNAPSHOT),
            "glossary_sha256": sha_file(glossary_path) if glossary_path else None,
            "unityfs_packer": "original",
            "translations": [
                {"path": str(path), "sha256": sha_file(path)} for path in translation_paths
            ],
            "source_bundles": EXPECTED_SOURCE_BUNDLES,
            "bundles_written": len(records),
            "unique_source_values": len(source),
            "release_accepted_source_values": len(translations),
            "localized_text_occurrences": occurrences,
            "require_complete": bool(args.require_complete),
            "release_ready": bool(args.require_complete and not missing),
            "release_ready_semantics": (
                "frozen-source coverage only: the ledger must still be the text owner's "
                "independently reviewed release_gate=accepted output"
            ),
            "isolated_partial_trial": not args.require_complete,
            "all_roundtrip_verified": True,
            "source_archive_modified": False,
            "canonical_archive_modified": False,
            "translation_production_modified": False,
            "NAS_modified": False,
            "formal_API_modified": False,
            "real_device_verified": False,
            "client_verified": False,
            "status": (
                "isolated_complete_source_cohort_not_published" if args.require_complete
                else "isolated_partial_trial_never_release"
            ),
            "bundles": records,
        }
        (temporary / "event-unit-90200-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, dest)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(json.dumps(
        {key: value for key, value in manifest.items() if key != "bundles"},
        ensure_ascii=False, indent=2,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-version", required=True,
                        help=f"frozen JP client version (must be {FROZEN_CLIENT_VERSION})")
    parser.add_argument("--asset-version", required=True,
                        help=f"frozen assets version (must be {FROZEN_ASSET_VERSION})")
    parser.add_argument("--translations", type=Path, action="append", required=True,
                        help="text owner release_gate=accepted ledger; repeatable")
    parser.add_argument("--output-root", type=Path, required=True,
                        help="NEW isolated output root; never a canonical archive")
    parser.add_argument("--preflight-only", action="store_true",
                        help="report release coverage without writing any bundle")
    parser.add_argument("--allow-partial", dest="require_complete", action="store_false",
                        help="isolated trial only: allows a partial ledger, never release-ready")
    parser.add_argument("--bundle-limit", type=int, default=None,
                        help="isolated trial only: write back at most N bundles; requires --allow-partial")
    parser.set_defaults(require_complete=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except GateError as exc:
        # A refusal is an expected outcome (the production machine JSONL is not a
        # ledger); report it as machine-readable evidence instead of a traceback.
        print(json.dumps({
            "kind": "event-unit-90200-materializer", "status": "refused",
            "reason": str(exc), "output_created": False,
        }, ensure_ascii=False, indent=2))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
