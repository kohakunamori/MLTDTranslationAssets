"""Per-Unity3D build records, independent of release versions and file layout.

Parsed official text is a rebuildable, checksummed input cache. Re-evaluating
the complete resolver against it is cheap and captures both successful and
unsuccessful memory lookups without opening unchanged Unity bundles. Only a
changed source, replacement set or writer needs the expensive Unity writer.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import json
import platform
from functools import cache
from pathlib import Path

from release_reuse import ReusePlan, _reused_entry, HEX64_RE

STATE_KEY = "bundle_build"
SCHEMA = 1
COMMON_FILES = (
    "scripts/build_generated_release.py",
    "scripts/bundle_incremental.py",
    "pipelines/core/hashing.py",
    "pipelines/core/jsonl.py",
)
TEXT_FILES = COMMON_FILES + (
    "pipelines/text/mltd_localization_pipeline.py",
    "pipelines/text/mltd_localize_gtx.py",
    "pipelines/text/translation_tokens.py",
)
LYRIC_FILES = COMMON_FILES + (
    "scripts/build_lyric_overlay.py",
    "pipelines/text/mltd_localize_scrobj.py",
    "pipelines/text/mltd_lyric_rules.py",
)
PARSER_FILES = (
    "pipelines/text/mltd_localize_gtx.py", "pipelines/core/jsonl.py",
    "pipelines/core/hashing.py", "pipelines/text/translation_tokens.py",
)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


@cache
def toolchain_versions() -> dict:
    versions = {"python": platform.python_version()}
    for name in ("UnityPy", "pycryptodome", "msgpack", "lz4", "brotli"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "missing"
    return versions


def writer_fingerprint(root: Path, files: tuple[str, ...]) -> str:
    code = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            if (root / name).is_file() else "missing" for name in files}
    return digest({"code": code, "toolchain": toolchain_versions()})


def source_identity(row: dict) -> list:
    # Remote names and asset versions are routing metadata. The catalogue's
    # content fingerprint AND declared size identify the official input.
    return [str(row.get("catalog_hash", "")), int(row["declared_size"])]


def previous_state(manifest: dict | None) -> dict:
    state = (manifest or {}).get(STATE_KEY)
    if ((manifest or {}).get("build_status") != "success"
            or not isinstance(state, dict) or state.get("schema") != SCHEMA):
        return {}
    return state


def load_sources(root: Path, record: dict | None, row: dict, parser: str) -> dict | None:
    """Read an authenticated parse result; a missing/corrupt cache is a miss."""
    if (not isinstance(record, dict) or record.get("source_identity") != source_identity(row)
            or record.get("parser") != parser):
        return None
    source_sha = str(record.get("source_sha256", ""))
    expected = str(record.get("source_records_sha256", ""))
    if not HEX64_RE.fullmatch(source_sha) or not HEX64_RE.fullmatch(expected):
        return None
    path = root / "build-inputs" / "text" / parser / f"{source_sha}.json.gz"
    try:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            return None
        document = json.loads(gzip.decompress(payload))
        if (not isinstance(document, dict) or document.get("schema") != SCHEMA
                or document.get("source_sha256") != source_sha
                or not isinstance(document.get("name"), str)
                or not isinstance(document.get("records"), list)
                or any(not isinstance(pair, list) or len(pair) != 2
                       or any(not isinstance(value, str) for value in pair)
                       for pair in document["records"])):
            return None
        return document
    except (OSError, ValueError, EOFError, TypeError):
        return None


def save_sources(root: Path, row: dict, parser: str, name: str,
                 records: list, source_sha: str) -> tuple[dict, dict]:
    document = {"schema": SCHEMA, "name": name, "records": records,
                "source_sha256": source_sha}
    payload = gzip.compress(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8"), mtime=0)
    path = root / "build-inputs" / "text" / parser / f"{source_sha}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_bytes() != payload:
        temporary = path.with_suffix(".writing")
        temporary.write_bytes(payload)
        temporary.replace(path)
    record = {"source_identity": source_identity(row), "parser": parser,
              "source_sha256": source_sha,
              "source_records_sha256": hashlib.sha256(payload).hexdigest()}
    return document, record


def plan(*, surface: str, records: dict, rows: dict, previous: dict | None,
         object_file, force: bool = False, asset_version: str | None = None) -> ReusePlan:
    """Compare actual per-bundle build fingerprints, including negative results."""
    old_records = previous_state(previous).get(surface, {})
    entries = {entry["logical_key"]: entry for entry in (previous or {}).get("entries", [])}
    result = ReusePlan(reason="per-bundle inputs changed")
    negative = 0
    for logical, record in records.items():
        old = old_records.get(logical)
        entry = entries.get(logical)
        reason = "new bundle build record"
        if force:
            reason = "forced rebuild"
        elif old and record["fingerprint"] == old.get("fingerprint"):
            if old.get("written") is False and entry is None:
                negative += 1
                continue
            artifact = str((entry or {}).get("artifact_sha256", ""))
            if (entry and entry.get("resource_kind") == "bundle"
                    and entry.get("translation_status") == "modified"
                    and old.get("written") is True
                    and old.get("artifact_sha256") == artifact
                    and old.get("source_sha256") == entry.get("source_sha256")
                    and HEX64_RE.fullmatch(artifact) and object_file(artifact) is not None):
                result.reusable[logical] = _reused_entry(
                    entry, logical, str(rows[logical]["remote"]),
                    asset_version or str((previous or {}).get("asset_version", "")))
                continue
            reason = "published object missing or inconsistent"
        elif old:
            reason = "bundle source, resolved translations or writer changed"
        result.rebuild[logical] = dict(rows[logical])
        result.declined[reason] = result.declined.get(reason, 0) + 1
    result.negative_reused = negative
    if result.reusable or negative:
        result.reason = "per-bundle incremental"
    return result


def finish_records(records: dict, entries: list[dict]) -> None:
    by_logical = {entry["logical_key"]: entry for entry in entries}
    for logical, record in records.items():
        entry = by_logical.get(logical)
        record["written"] = entry is not None
        if entry:
            if record.get("source_sha256") and record["source_sha256"] != entry["source_sha256"]:
                raise ValueError(f"source parse record and generated bundle disagree: {logical}")
            record["artifact_sha256"] = entry["artifact_sha256"]
            record.setdefault("source_sha256", entry["source_sha256"])
