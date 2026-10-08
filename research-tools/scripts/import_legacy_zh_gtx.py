#!/usr/bin/env python3
"""Import preserved official Traditional-Chinese GTX text as translation memory.

The historical zh-android manifest uses the same logical GTX namespace as the
current JP client, replacing only the language suffix.  This tool intentionally
matches bundles by logical name and rows by GTX key; it never guesses by asset
hash or record position.

Subcommands:
  fetch   Download only legacy zh GTX bundles that still exist in the current
          JP snapshot.
  import  Align downloaded zh GTX rows with the current JP snapshot and emit a
          current-source-bound JSONL translation catalogue.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

import msgpack

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_localize_gtx import (
    is_source_text,
    parse_records,
    read_gtx,
    validate_translation,
    write_jsonl,
)

DEFAULT_ROOT = "https://assets.rainbowunicorn7297.com/zh-android"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_logical(name: str) -> str:
    value = name.casefold()
    for suffix in ("_jp.gtx.unity3d", "_zh.gtx.unity3d"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def load_current_snapshot(path: Path) -> tuple[str, dict[str, dict]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    scope = str(value.get("scope", "jp-android"))
    objects = value.get("objects")
    if not isinstance(objects, list):
        raise ValueError("current snapshot does not contain objects[]")
    rows: dict[str, dict] = {}
    for row in objects:
        logical = str(row.get("logical", ""))
        if not logical.casefold().endswith("_jp.gtx.unity3d"):
            continue
        identity = normalize_logical(logical)
        if identity in rows:
            raise ValueError(f"duplicate current logical identity: {identity}")
        rows[identity] = row
    return scope, rows


def load_legacy_manifest(path: Path) -> dict[str, dict]:
    raw = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
    if not isinstance(raw, (list, tuple)) or not raw or not isinstance(raw[0], dict):
        raise ValueError("invalid legacy manifest")
    result: dict[str, dict] = {}
    for logical, value in raw[0].items():
        logical = str(logical)
        if not logical.casefold().endswith("_zh.gtx.unity3d"):
            continue
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            raise ValueError(f"malformed manifest row: {logical}")
        identity = normalize_logical(logical)
        result[identity] = {
            "logical": logical,
            "catalog_hash": str(value[0]),
            "remote": str(value[1]),
            "declared_size": int(value[2]),
        }
    return result


def paired_rows(args: argparse.Namespace) -> tuple[str, list[tuple[str, dict, dict]]]:
    scope, current = load_current_snapshot(args.current_snapshot)
    legacy = load_legacy_manifest(args.legacy_manifest)
    pairs = [
        (identity, current[identity], legacy[identity])
        for identity in sorted(set(current) & set(legacy))
    ]
    return scope, pairs


def download_one(url: str, destination: Path, declared_size: int, timeout: float) -> dict:
    if destination.is_file() and destination.stat().st_size == declared_size:
        with destination.open("rb") as handle:
            if handle.read(7) == b"UnityFS":
                return {"status": "cached", "bytes": declared_size}
    request = Request(url, headers={"User-Agent": "mltd-current-localization/1"})
    with urlopen(request, timeout=timeout) as response:
        data = response.read()
    if len(data) != declared_size:
        raise ValueError(
            f"size mismatch: expected={declared_size} actual={len(data)} url={url}"
        )
    if not data.startswith(b"UnityFS"):
        raise ValueError(f"downloaded object is not UnityFS: {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".part")
    temp.write_bytes(data)
    temp.replace(destination)
    return {"status": "downloaded", "bytes": len(data), "sha256": sha256_bytes(data)}


def cmd_fetch(args: argparse.Namespace) -> int:
    scope, pairs = paired_rows(args)
    selected = pairs[: args.limit] if args.limit else pairs
    failures: list[dict] = []
    downloaded = cached = 0
    total_bytes = 0

    def fetch(pair: tuple[str, dict, dict]) -> tuple[str, dict]:
        identity, _current, legacy = pair
        destination = args.cache_root / "zh-android" / legacy["remote"]
        url = args.remote_root.rstrip("/") + "/" + legacy["remote"]
        return identity, download_one(url, destination, legacy["declared_size"], args.timeout)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch, pair): pair for pair in selected}
        for future in as_completed(futures):
            identity, _current, legacy = futures[future]
            try:
                _identity, result = future.result()
                total_bytes += int(result["bytes"])
                if result["status"] == "downloaded":
                    downloaded += 1
                else:
                    cached += 1
            except Exception as exc:
                failures.append(
                    {
                        "identity": identity,
                        "logical": legacy["logical"],
                        "remote": legacy["remote"],
                        "error": str(exc),
                    }
                )

    result = {
        "current_scope": scope,
        "legacy_scope": "zh-android",
        "paired_bundles": len(pairs),
        "selected": len(selected),
        "downloaded": downloaded,
        "cached": cached,
        "failed": len(failures),
        "bytes": total_bytes,
        "complete": not failures and downloaded + cached == len(selected),
        "cache_root": str(args.cache_root),
        "failures_first": failures[:20],
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


def safe_convert(value: str, converter) -> str:
    if converter is None:
        return value
    return converter.convert(value)


def load_converter(enabled: bool):
    if not enabled:
        return None
    try:
        from opencc import OpenCC
    except ImportError as exc:
        raise SystemExit(
            "--to-simplified requires an OpenCC-compatible Python package "
            "(for example opencc-python-reimplemented)"
        ) from exc
    return OpenCC("t2s")


def cmd_import(args: argparse.Namespace) -> int:
    current_scope, pairs = paired_rows(args)
    selected = pairs[: args.limit] if args.limit else pairs
    current_root = args.current_archive_root / current_scope
    legacy_root = args.cache_root / "zh-android"
    converter = load_converter(args.to_simplified)

    rows: list[dict] = []
    counters = {
        "paired_bundles": len(pairs),
        "selected_bundles": len(selected),
        "loaded_bundles": 0,
        "missing_legacy_bundle": 0,
        "legacy_decode_error": 0,
        "current_records": 0,
        "key_matches": 0,
        "source_candidates": 0,
        "official_translations": 0,
        "unchanged_values": 0,
        "token_mismatch": 0,
        "missing_key": 0,
    }
    mismatch_samples: list[dict] = []
    missing_samples: list[dict] = []

    for identity, current, legacy in selected:
        current_path = current_root / current["remote"]
        legacy_path = legacy_root / legacy["remote"]
        if not current_path.is_file():
            raise FileNotFoundError(f"current JP bundle missing: {current_path}")
        if not legacy_path.is_file():
            counters["missing_legacy_bundle"] += 1
            continue

        current_name, current_text, _ = read_gtx(current_path)
        try:
            legacy_name, legacy_text, _ = read_gtx(legacy_path)
        except Exception as exc:
            counters["legacy_decode_error"] += 1
            if len(mismatch_samples) < args.sample:
                mismatch_samples.append(
                    {
                        "logical": legacy["logical"],
                        "remote": legacy["remote"],
                        "error": f"legacy_decode_error: {exc}",
                    }
                )
            continue
        current_map = dict(parse_records(current_text))
        legacy_map = dict(parse_records(legacy_text))
        counters["loaded_bundles"] += 1
        counters["current_records"] += len(current_map)

        for key, source in current_map.items():
            if not is_source_text(source):
                continue
            counters["source_candidates"] += 1
            if key not in legacy_map:
                counters["missing_key"] += 1
                if len(missing_samples) < args.sample:
                    missing_samples.append(
                        {
                            "logical": current["logical"],
                            "bundle": current_name,
                            "key": key,
                            "source": source,
                        }
                    )
                continue
            counters["key_matches"] += 1
            translated = safe_convert(str(legacy_map[key]), converter)
            if translated == source or not translated:
                counters["unchanged_values"] += 1
                continue
            status = "official_legacy_simplified" if converter else "official_legacy"
            try:
                validate_translation(source, translated)
            except ValueError as exc:
                counters["token_mismatch"] += 1
                status = "needs_review"
                if len(mismatch_samples) < args.sample:
                    mismatch_samples.append(
                        {
                            "logical": current["logical"],
                            "key": key,
                            "source": source,
                            "translation": translated,
                            "error": str(exc),
                        }
                    )
            else:
                counters["official_translations"] += 1

            rows.append(
                {
                    "bundle": current_name,
                    "key": key,
                    "source": source,
                    "translation": translated,
                    "status": status,
                    "provenance": "official-legacy-zh",
                    "current_logical": current["logical"],
                    "current_remote": current["remote"],
                    "legacy_logical": legacy["logical"],
                    "legacy_remote": legacy["remote"],
                    "legacy_bundle": legacy_name,
                }
            )

    write_jsonl(args.output, rows)
    result = {
        **counters,
        "translation_rows": len(rows),
        "usable_translation_rows": counters["official_translations"],
        "key_match_rate": (
            0.0
            if not counters["source_candidates"]
            else counters["key_matches"] / counters["source_candidates"]
        ),
        "output": str(args.output),
        "missing_samples": missing_samples,
        "token_mismatch_samples": mismatch_samples,
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--current-snapshot", type=Path, required=True)
    common.add_argument("--legacy-manifest", type=Path, required=True)
    common.add_argument("--cache-root", type=Path, required=True)
    common.add_argument("--limit", type=int, default=0)

    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", parents=[common])
    fetch.add_argument("--remote-root", default=DEFAULT_ROOT)
    fetch.add_argument("--workers", type=int, default=32)
    fetch.add_argument("--timeout", type=float, default=60.0)
    fetch.add_argument("--summary", type=Path)
    fetch.set_defaults(func=cmd_fetch)

    imp = sub.add_parser("import", parents=[common])
    imp.add_argument("--current-archive-root", type=Path, required=True)
    imp.add_argument("--output", type=Path, required=True)
    imp.add_argument("--summary", type=Path)
    imp.add_argument("--sample", type=int, default=20)
    imp.add_argument("--to-simplified", action="store_true")
    imp.set_defaults(func=cmd_import)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
