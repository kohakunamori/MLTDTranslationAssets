#!/usr/bin/env python3
"""Build deterministic MLTD system-text localization overlays.

The JP client loads five encrypted GTX TextAssets (CM/MD/MB/CD/ST).  This tool
keeps the official asset archive immutable and provides a key-stable workflow:

* extract: decrypt one or more GTX bundles into JSONL translation catalogues;
* diff: compare two catalogues by (bundle, key) for version-port automation;
* build: merge reviewed translations, validate protected formatting tokens,
  re-encrypt the TextAsset and write UnityFS bundles into an overlay tree;
* audit: measure source/translation coverage without modifying any bundle.

Translation rows are JSONL objects with bundle/key/source/translation/status.
A translation is accepted only when its source still exactly matches the current
JP value, preventing stale translations from silently crossing game versions.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))
import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

import UnityPy
from Crypto.Cipher import AES

PASSWORD = b"Millicon"
SALT = b"DAISUL___"
ITERATIONS = 1000
KEY_BYTES = 24
IV_BYTES = 16

# Include CJK so Kanji-only JP labels are not omitted from the translation queue.
SOURCE_TEXT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
KANA_RE = re.compile(r"[\u3040-\u30ff]")
PROTECTED_TOKEN_RE = re.compile(
    r"\{[^{}]+\}"
    r"|%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z]"
    # A kaomoji may contain < and > on separate lines; never protect prose
    # between them as a single fake rich-text tag.
    r"|<[^<>\r\n]+>"
    r"|\\[nrt]"
    # MLTD numbered inline control sequences, e.g. \03\ and \17\.
    # These are formatting/control data, never translatable prose or digits.
    r"|\\[0-9]{2}\\"
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive_key_iv() -> tuple[bytes, bytes]:
    derived = hashlib.pbkdf2_hmac(
        "sha1", PASSWORD, SALT, ITERATIONS, dklen=KEY_BYTES + IV_BYTES
    )
    return derived[:KEY_BYTES], derived[KEY_BYTES:]


def decrypt_payload(cipher: bytes) -> bytes:
    if not cipher or len(cipher) % 16:
        raise ValueError("GTX cipher payload is not AES block aligned")
    key, iv = derive_key_iv()
    padded = AES.new(key, AES.MODE_CBC, iv).decrypt(cipher)
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError("invalid GTX PKCS7 padding")
    return padded[:-pad]


def encrypt_payload(plain: bytes) -> bytes:
    pad = 16 - (len(plain) % 16)
    padded = plain + bytes([pad]) * pad
    key, iv = derive_key_iv()
    return AES.new(key, AES.MODE_CBC, iv).encrypt(padded)


def get_text_asset(bundle: Path):
    env = UnityPy.load(str(bundle))
    objects = [obj for obj in env.objects if obj.type.name == "TextAsset"]
    if len(objects) != 1:
        raise ValueError(f"{bundle}: expected exactly one TextAsset, got {len(objects)}")
    data = objects[0].read()
    raw = data.m_Script
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    elif isinstance(raw, memoryview):
        raw = raw.tobytes()
    else:
        raw = bytes(raw)
    return env, objects[0], data, str(data.m_Name), raw


def read_gtx(bundle: Path) -> tuple[str, str, bytes]:
    _env, _obj, _data, name, cipher = get_text_asset(bundle)
    plain = decrypt_payload(cipher)
    return name, plain.decode("utf-8"), cipher


def parse_records(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for record in text.split("|"):
        if "^" not in record:
            continue
        key, value = record.split("^", 1)
        rows.append((key, value))
    return rows


def replace_records(text: str, replacements: dict[str, str]) -> tuple[str, int]:
    changed = 0
    output: list[str] = []
    seen: set[str] = set()
    for record in text.split("|"):
        if "^" not in record:
            output.append(record)
            continue
        key, value = record.split("^", 1)
        if key in seen:
            raise ValueError(f"duplicate GTX key: {key}")
        seen.add(key)
        replacement = replacements.get(key)
        if replacement is not None and replacement != value:
            record = key + "^" + replacement
            changed += 1
        output.append(record)
    unknown = sorted(set(replacements) - seen)
    if unknown:
        raise ValueError(f"translations contain {len(unknown)} unknown keys; first={unknown[0]!r}")
    return "|".join(output), changed


def protected_tokens(value: str) -> Counter[str]:
    return Counter(PROTECTED_TOKEN_RE.findall(value))


def validate_translation(source: str, translated: str) -> None:
    before = protected_tokens(source)
    after = protected_tokens(translated)
    if before != after:
        raise ValueError(
            "protected token mismatch: "
            f"source={dict(before)!r} translation={dict(after)!r}"
        )


# Statuses a release decision has actually granted: only these may be used as a
# translation value.  Owner ruling 2026-09-26 replaced the previous *blacklist*
# ("anything not obviously pending is releasable") with this allowlist, because the
# blacklist let the raw Traditional-Chinese seed status `official_legacy` through and
# shipped 95,207 Traditional rows (24.5% of the published overlay's edits) to /cn/.
#
# Adding a status here IS the release decision.  A new workflow state must be added
# deliberately; it can no longer become releasable merely by not matching a prefix.
ACCEPTED_TRANSLATION_STATUSES = frozenset({
    # Public Assets JSONL uses the repository schema's explicit review state.
    # The older private release ledgers below use more granular provenance
    # states, but `accepted` must remain the canonical public input state.
    "accepted",
    # Machine and agent output.  `machine_translated` is what
    # scripts/translate_gtx_queue.py:62 (ACCEPTED_STATUS) and the API/Codex/non-GTX
    # pools emit; `agent_translated` is what a source-bound draft promotion emits
    # (scripts/promote_mltd_tail_draft.py:149).
    "machine_translated",
    "agent_translated",
    # The explicit owner-waiver channel the owner asked for.  This is the status of
    # the published batch-6 GTX overlay (90,791 rows) and of the t2s ledger it was
    # built from, so keeping it here is what makes the tightening a no-op for that
    # build: measured delta on the published resolver inputs is exactly zero.
    "official_legacy_opencc_t2s_owner_waived",
    # The only two statuses LOCALIZATION_RELEASE_LEDGER.md:39 lets skip independent
    # AI review.  Measured: no other status reaches an active resolver input.
    "official_legacy_zhcn_reviewed",
    "official_legacy_simplified_reviewed",
})


def translation_status_is_accepted(status: str) -> bool:
    """Fail closed: only explicitly granted statuses are releasable.

    Measured on 2026-09-26 against every ledger that can feed the GTX resolver
    (work/agents/text-localization/image-terminology-unblock-20260926/
    scan_resolver_statuses.py): the statuses that actually flow through are
    `machine_translated`, `agent_translated` and
    `official_legacy_opencc_t2s_owner_waived`.  Raw `official_legacy` no longer
    appears in any active resolver input -- it survives only in
    `legacy-zh-translations.jsonl` and `release-inputs/legacy-zh-translations-release-safe.jsonl`,
    both of which are Traditional-Chinese seed sources that must be converted
    (OpenCC t2s + normalize) before they may be released.  Rejecting them here is the
    fix, not a regression: rebuilding the published input set with this allowlist
    reproduces its resolver counters exactly.
    """
    return str(status or "").strip().lower() in ACCEPTED_TRANSLATION_STATUSES


def is_source_text(value: str) -> bool:
    return bool(SOURCE_TEXT_RE.search(value))


def catalogue_rows(bundle: Path) -> tuple[dict, list[dict]]:
    name, text, cipher = read_gtx(bundle)
    parsed = parse_records(text)
    if len(parsed) != len({key for key, _ in parsed}):
        raise ValueError(f"{bundle}: duplicate keys in GTX plaintext")
    rows = [
        {
            "bundle": name,
            "key": key,
            "source": value,
            "translation": "",
            "status": "pending" if is_source_text(value) else "non_source",
            "has_kana": bool(KANA_RE.search(value)),
        }
        for key, value in parsed
    ]
    summary = {
        "bundle_path": str(bundle),
        "bundle": name,
        "bundle_sha256": sha256_bytes(bundle.read_bytes()),
        "cipher_sha256": sha256_bytes(cipher),
        "plain_sha256": sha256_bytes(text.encode("utf-8")),
        "records": len(rows),
        "source_candidates": sum(row["status"] == "pending" for row in rows),
        "unique_source_values": len(
            {row["source"] for row in rows if row["status"] == "pending"}
        ),
        "kana_records": sum(row["has_kana"] for row in rows),
    }
    return summary, rows


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def translation_map(rows: Iterable[dict], bundle_name: str, current: dict[str, str]) -> tuple[dict[str, str], dict]:
    replacements: dict[str, str] = {}
    stale: list[str] = []
    invalid: list[str] = []
    for row in rows:
        if str(row.get("bundle", "")).casefold() != bundle_name.casefold():
            continue
        key = str(row.get("key", ""))
        if not key or key not in current:
            invalid.append(key)
            continue
        source = str(row.get("source", ""))
        if source != current[key]:
            stale.append(key)
            continue
        translated = str(row.get("translation", ""))
        status = str(row.get("status", "pending"))
        if not translation_status_is_accepted(status) or not translated:
            continue
        validate_translation(source, translated)
        replacements[key] = translated
    if invalid:
        raise ValueError(
            f"translation file has unknown keys for {bundle_name}: {invalid[:3]!r}"
        )
    return replacements, {"stale": stale, "applied": len(replacements)}


def save_localized_bundle(source: Path, output: Path, translated_text: str) -> dict:
    env, _obj, data, name, old_cipher = get_text_asset(source)
    new_plain = translated_text.encode("utf-8")
    new_cipher = encrypt_payload(new_plain)
    data.m_Script = new_cipher
    data.save()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(env.file.save(packer="lz4"))

    verify_name, verify_text, verify_cipher = read_gtx(output)
    if verify_name != name or verify_text != translated_text:
        raise ValueError(f"round-trip verification failed for {output}")
    return {
        "bundle": name,
        "source_path": str(source),
        "output_path": str(output),
        "source_bundle_sha256": sha256_bytes(source.read_bytes()),
        "output_bundle_sha256": sha256_bytes(output.read_bytes()),
        "source_cipher_sha256": sha256_bytes(old_cipher),
        "output_cipher_sha256": sha256_bytes(verify_cipher),
        "output_plain_sha256": sha256_bytes(new_plain),
        "output_bytes": output.stat().st_size,
    }


def cmd_extract(args: argparse.Namespace) -> int:
    all_rows: list[dict] = []
    summaries: list[dict] = []
    for bundle in args.bundle:
        summary, rows = catalogue_rows(bundle)
        summaries.append(summary)
        all_rows.extend(rows)
    write_jsonl(args.output, all_rows)
    result = {
        "bundles": summaries,
        "records": len(all_rows),
        "source_candidates": sum(r["status"] == "pending" for r in all_rows),
        "unique_source_values": len(
            {r["source"] for r in all_rows if r["status"] == "pending"}
        ),
        "kana_records": sum(r["has_kana"] for r in all_rows),
        "catalogue": str(args.output),
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    old = {(r.get("bundle"), r.get("key")): r for r in read_jsonl(args.old)}
    new = {(r.get("bundle"), r.get("key")): r for r in read_jsonl(args.new)}
    rows: list[dict] = []
    counts = Counter()
    for identity in sorted(set(old) | set(new), key=lambda x: (str(x[0]), str(x[1]))):
        before = old.get(identity)
        after = new.get(identity)
        if before is None:
            status = "added"
        elif after is None:
            status = "removed"
        elif before.get("source") != after.get("source"):
            status = "changed"
        else:
            status = "unchanged"
        counts[status] += 1
        rows.append({
            "bundle": identity[0],
            "key": identity[1],
            "status": status,
            "old_source": None if before is None else before.get("source"),
            "new_source": None if after is None else after.get("source"),
            "needs_translation": bool(after and is_source_text(str(after.get("source", ""))) and status in {"added", "changed"}),
        })
    write_jsonl(args.output, rows)
    result = {"counts": dict(counts), "output": str(args.output)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    catalogue = read_jsonl(args.catalogue)
    translations = read_jsonl(args.translations)
    tmap = {(str(r.get("bundle", "")).casefold(), str(r.get("key", ""))): r for r in translations}
    counts = Counter()
    unresolved: list[dict] = []
    stale: list[dict] = []
    for row in catalogue:
        if not is_source_text(str(row.get("source", ""))):
            counts["non_source"] += 1
            continue
        counts["source_candidates"] += 1
        identity = (str(row.get("bundle", "")).casefold(), str(row.get("key", "")))
        translated = tmap.get(identity)
        if translated is None or not str(translated.get("translation", "")):
            counts["unresolved"] += 1
            unresolved.append(row)
            continue
        if str(translated.get("source", "")) != str(row.get("source", "")):
            counts["stale"] += 1
            stale.append(row)
            continue
        status = str(translated.get("status", "pending"))
        if not translation_status_is_accepted(status):
            counts[status or "unclassified"] += 1
            unresolved.append(row)
            continue
        validate_translation(str(row.get("source", "")), str(translated.get("translation", "")))
        counts["translated"] += 1
    result = {
        "counts": dict(counts),
        "coverage": 0.0 if not counts["source_candidates"] else counts["translated"] / counts["source_candidates"],
        "unresolved_first": unresolved[: args.sample],
        "stale_first": stale[: args.sample],
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not unresolved and not stale else 2


def cmd_build(args: argparse.Namespace) -> int:
    translation_rows = read_jsonl(args.translations)
    manifests: list[dict] = []
    total_applied = 0
    for bundle in args.bundle:
        name, text, _cipher = read_gtx(bundle)
        current = dict(parse_records(text))
        replacements, meta = translation_map(translation_rows, name, current)
        translated_text, changed = replace_records(text, replacements)
        if changed != meta["applied"]:
            raise ValueError(f"{name}: changed/applied mismatch {changed}!={meta['applied']}")
        if meta["stale"]:
            raise ValueError(f"{name}: {len(meta['stale'])} stale translations; first={meta['stale'][0]!r}")
        output = args.output_root / args.scope / bundle.name
        manifest = save_localized_bundle(bundle, output, translated_text)
        manifest.update({"applied": changed, "records": len(current)})
        manifests.append(manifest)
        total_applied += changed
    result = {
        "output_root": str(args.output_root),
        "scope": args.scope,
        "bundles": manifests,
        "applied": total_applied,
    }
    manifest_path = args.output_root / "localization-manifest.json"
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract")
    extract.add_argument("--bundle", type=Path, action="append", required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--summary", type=Path)
    extract.set_defaults(func=cmd_extract)

    diff = sub.add_parser("diff")
    diff.add_argument("--old", type=Path, required=True)
    diff.add_argument("--new", type=Path, required=True)
    diff.add_argument("--output", type=Path, required=True)
    diff.set_defaults(func=cmd_diff)

    audit = sub.add_parser("audit")
    audit.add_argument("--catalogue", type=Path, required=True)
    audit.add_argument("--translations", type=Path, required=True)
    audit.add_argument("--output", type=Path)
    audit.add_argument("--sample", type=int, default=10)
    audit.set_defaults(func=cmd_audit)

    build = sub.add_parser("build")
    build.add_argument("--bundle", type=Path, action="append", required=True)
    build.add_argument("--translations", type=Path, required=True)
    build.add_argument("--output-root", type=Path, required=True)
    build.add_argument("--scope", default="jp-android")
    build.set_defaults(func=cmd_build)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
