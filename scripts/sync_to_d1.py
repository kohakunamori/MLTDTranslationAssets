#!/usr/bin/env python3
"""Export translations from GitHub localization repository into batched D1 SQL seed files.

Synchronizes accepted (and optionally pending) translations from the GitHub SSOT
repository (locales/**/*.jsonl) into Cloudflare D1 'contributions' table.

This allows the serverless translation portal (https://mltd-translate.nyaneko.cn)
to immediately reflect the 50,000+ accepted translations without manual re-entry.

Safety:
- Strict source_sha256 verification against Japanese source text.
- Rejection of reserved delimiters (| and ^) in translations.
- Version identity is the digits-only `asset_version`. A legacy `base_version`
  field is read only when it is itself digits-only, so a pre-decoupling entry
  such as `9.0.200+1077100` fails the sync instead of being carried into D1 as
  a composite identity.
- SQL literals use CAST(X'...' AS TEXT) for text with control characters to avoid
  D1 expression tree depth > 100 error.
- Purely isolated output: writes only to --out-dir.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

HEX64 = re.compile(r"^[0-9a-f]{64}$")
ASSET_VERSION_PATTERN = re.compile(r"^[0-9]+$")
RESERVED = ("|", "^")
DEFAULT_AUTHOR = "ssot@mltd-localization.github"
DEFAULT_AUTHOR_NAME = "GitHub SSOT (kohakunamori/MLTDTranslationAssets)"


class SyncExportError(ValueError):
    pass


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def deterministic_id(base_version: str, bundle: str, item_key: str, source_sha: str) -> str:
    seed = f"{base_version}:{bundle}:{item_key}:{source_sha}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def sql_literal(value: str) -> str:
    """Return a single SQLite string expression for value.

    Uses CAST(X'...' AS TEXT) when value contains control characters or newlines
    to prevent D1 SQLITE_ERROR 'Expression tree is too large (maximum depth 100)'.
    """
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return f"CAST(X'{value.encode('utf-8').hex()}' AS TEXT)"
    return "'" + value.replace("'", "''") + "'"


def read_locales(repo_root: Path, statuses: set[str], files: list[Path] | None = None) -> list[dict[str, Any]]:
    locales_dir = repo_root / "locales"
    if not locales_dir.is_dir():
        raise SyncExportError(f"Missing locales directory at {locales_dir}")

    target_files = sorted(files) if files is not None else sorted(locales_dir.rglob("*.jsonl"))
    rows: list[dict[str, Any]] = []
    seen_identities: set[tuple[str, str, str]] = set()

    for jsonl_file in target_files:
        if not jsonl_file.is_file():
            continue
        with jsonl_file.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SyncExportError(f"{jsonl_file}:{line_no}: invalid JSON") from exc

                status = str(row.get("status", "")).strip().lower()
                if status not in statuses:
                    continue

                # Version identity. Post-decoupling entries carry `asset_version`
                # (digits only; `client_version` is always null on this axis). A
                # legacy `base_version` is still read so an un-migrated file does
                # not silently produce version-less rows -- but only when it is
                # itself digits-only. `9.0.200+1077100` is not a version and must
                # not be carried into D1.
                declared_axis = str(row.get("asset_version", "") or "").strip()
                legacy_axis = str(row.get("base_version", "") or "").strip()
                axis_value = declared_axis or legacy_axis
                if not ASSET_VERSION_PATTERN.match(axis_value):
                    raise SyncExportError(
                        f"{jsonl_file}:{line_no}: invalid asset axis {axis_value!r}; "
                        "digits only, composite versions (e.g. 9.0.200+1077100) are forbidden"
                    )
                base_version = axis_value
                bundle = str(row.get("bundle", "")).strip()
                item_key = str(row.get("item_key", "")).strip()
                source_sha = str(row.get("source_sha256", "")).strip().lower()
                ja = row.get("ja", "")
                zh = row.get("zh", "")
                updated_at = str(row.get("updated_at", "2026-09-27T00:00:00Z")).strip()

                if not base_version or not bundle or not item_key:
                    raise SyncExportError(f"{jsonl_file}:{line_no}: missing required key")

                if not zh:
                    continue

                # Verify SHA-256
                computed_sha = sha256_text(ja)
                if computed_sha != source_sha:
                    raise SyncExportError(
                        f"{jsonl_file}:{line_no}: source hash mismatch: declared {source_sha} != {computed_sha}"
                    )

                # Check reserved delimiters
                for delim in RESERVED:
                    if delim in zh:
                        raise SyncExportError(
                            f"{jsonl_file}:{line_no}: translation contains illegal delimiter '{delim}': {zh}"
                        )

                identity = (base_version, bundle, item_key)
                if identity in seen_identities:
                    raise SyncExportError(f"{jsonl_file}:{line_no}: duplicate entry {identity}")
                seen_identities.add(identity)

                rows.append({
                    "id": deterministic_id(base_version, bundle, item_key, source_sha),
                    "base_version": base_version,
                    "bundle": bundle,
                    "item_key": item_key,
                    "source_sha256": source_sha,
                    "source": ja,
                    "translation": zh,
                    "status": status,
                    "contributor_email": DEFAULT_AUTHOR,
                    "created_at": updated_at,
                    "updated_at": updated_at,
                })

    return rows


def render_contributions_sql(rows: list[dict[str, Any]]) -> str:
    statements: list[str] = [
        "-- Auto-generated contributions seed from GitHub SSOT repository",
        "PRAGMA foreign_keys = ON;",
        f"INSERT INTO contributors (email, display_name, role, created_at, updated_at) "
        f"VALUES ({sql_literal(DEFAULT_AUTHOR)}, {sql_literal(DEFAULT_AUTHOR_NAME)}, 'admin', '2026-09-27T00:00:00Z', '2026-09-27T00:00:00Z') "
        f"ON CONFLICT(email) DO UPDATE SET updated_at=excluded.updated_at;",
    ]

    for r in rows:
        sql = (
            "INSERT INTO contributions "
            "(id, base_version, bundle, item_key, source_sha256, source, translation, status, contributor_email, created_at, updated_at) "
            f"VALUES ({sql_literal(r['id'])}, {sql_literal(r['base_version'])}, {sql_literal(r['bundle'])}, "
            f"{sql_literal(r['item_key'])}, {sql_literal(r['source_sha256'])}, {sql_literal(r['source'])}, "
            f"{sql_literal(r['translation'])}, {sql_literal(r['status'])}, {sql_literal(r['contributor_email'])}, "
            f"{sql_literal(r['created_at'])}, {sql_literal(r['updated_at'])}) "
            "ON CONFLICT(base_version, bundle, item_key, source_sha256, contributor_email) "
            "DO UPDATE SET translation=excluded.translation, status=excluded.status, updated_at=excluded.updated_at;"
        )
        statements.append(sql)

    return "\n".join(statements) + "\n"


def export_sync_sql(repo_root: Path, out_dir: Path, batch_size: int = 3000, include_pending: bool = False, files: list[Path] | None = None) -> dict[str, Any]:
    statuses = {"accepted"}
    if include_pending:
        statuses.add("pending")

    rows = read_locales(repo_root, statuses, files=files)
    out_dir.mkdir(parents=True, exist_ok=True)

    batches: list[dict[str, Any]] = []
    total_rows = len(rows)

    for batch_idx, offset in enumerate(range(0, total_rows, batch_size), 1):
        chunk = rows[offset : offset + batch_size]
        batch_filename = f"contributions-batch-{batch_idx:04d}.sql"
        batch_path = out_dir / batch_filename
        sql_content = render_contributions_sql(chunk)
        batch_path.write_text(sql_content, encoding="utf-8")

        batches.append({
            "batch_index": batch_idx,
            "filename": batch_filename,
            "row_count": len(chunk),
            "sha256": sha256_file(batch_path),
            "bytes": batch_path.stat().st_size,
        })

    summary = {
        "schema_version": 1,
        "kind": "portal-contributions-sync-manifest",
        "repo_root": str(repo_root),
        "total_rows": total_rows,
        "statuses": sorted(statuses),
        "batch_size": batch_size,
        "batch_count": len(batches),
        "batches": batches,
    }

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True, help="Root directory of the GitHub localization repository")
    parser.add_argument("--out-dir", type=Path, required=True, help="Output directory for batched SQL files")
    parser.add_argument("--batch-size", type=int, default=3000, help="Number of contribution rows per SQL file")
    parser.add_argument("--include-pending", action="store_true", help="Include pending draft translations")
    parser.add_argument("--files", nargs="*", type=Path, default=None, help="Optional subset of .jsonl files to process (for incremental sync)")
    args = parser.parse_args()

    try:
        summary = export_sync_sql(args.repo_root, args.out_dir, args.batch_size, args.include_pending, files=args.files)
    except SyncExportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Exported {summary['total_rows']} contributions across {summary['batch_count']} batches to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
