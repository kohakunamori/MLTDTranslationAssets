#!/usr/bin/env python3
"""Strict JP 9.0.200 / 1077100 MD.mld write-back materializer.

The frozen remote ``150d85ca...unity3d`` carries a 2,642,000-byte *encrypted*
``MD.mld`` TextAsset (PBKDF2-HMAC-SHA1 / AES-192-CBC / PKCS7, the same scheme as
``scripts/mltd_localize_gtx.py``).  That encryption is verified, not assumed: the
source ciphertext must decrypt to the archived plain snapshot byte for byte and
re-encrypting that snapshot must reproduce the source ciphertext byte for byte.

Only 15 source SHA IDs / 18 bound ``data_map`` keys of the 63,639-entry payload
may ever change, and only from the text-localization owner's independently
reviewed ledger (``release_gate="accepted"``, deterministic QA ``PASS``, current
release policy and an independent reviewer identity; raw machine JSONL, synthetic,
legacy, smoke, benchmark, pilot evidence is refused).  Every other field, the
``<cn>``/``<cm>`` delimiters and the single ``<ssp>`` section separator must stay
byte-identical, so the whole plaintext is re-parsed and compared field by field
after the write-back.

Verification order is deliberate: re-encrypt -> write the UnityFS -> reload it ->
audit every serialized Unity object's raw bytes -> confirm the reloaded
ciphertext decrypts back to the full expected plaintext.

This script is offline: it never contacts the asset server, never writes the
frozen source, the canonical archive or ``build/localization-90200``, and never
publishes.  ``--allow-partial`` only produces ``release_ready=false`` trial output.
"""
from __future__ import annotations

import argparse
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

import msgpack  # noqa: E402
import UnityPy  # noqa: E402

from scripts.build_fontrender_overlay import load_release_translations  # noqa: E402
from scripts.localization_version_identity import version_identity  # noqa: E402
from scripts.materialize_event_unit_90200 import (  # noqa: E402
    GateError,
    load_gate_accepted,
    sha_bytes,
    sha_file,
)
from pipelines.text.mltd_localize_gtx import (  # noqa: E402
    decrypt_payload,
    encrypt_payload,
)
from scripts.mltd_translation_quality import load_glossary  # noqa: E402

# ------------------------------------------------------------------ frozen identity

FROZEN_CLIENT_VERSION = "9.0.200"
FROZEN_ASSET_VERSION = "1077100"

DATA_DIR = REPO / "work" / "local-assets" / "jp-android"
INDEX = DATA_DIR / "d8e17c28b47a711a5008ee87345e49db7a2b2559.data"
INDEX_SHA = "d7631544b9b1c7c0bea2729fbfdb688bf61d612a695be8233b36082738201e01"
SNAPSHOT = REPO / "build" / "localization-90200" / "jp-gtx-cache-snapshot.json"
SNAPSHOT_SHA = "5761f6e570b2272ad8d64c7205dd75d157d747d2f3f23a0e8a7e3a91db1bee74"

STAGE = REPO / "work" / "agents" / "image-localization" / "reviewed937-texture-stage"
SOURCE_ROOT = STAGE / "mld-source1077100"
LOGICAL = "md.mld.unity3d"
REMOTE = "150d85ca7071086175de38f3ee95d87e1e3ae673.unity3d"
SOURCE = SOURCE_ROOT / REMOTE
AUDIT = STAGE / "mld-source1077100-independent-audit.json"
AUDIT_SHA = "0a1fe9de4173d31022b3a152eca47f0fbda8bc7a8f8a89ce8cbb59c5b2f378a1"
AUDIT_STATUS = "source_AES192_CBC_PKCS7_exact_roundtrip_passed"
QUEUE = REPO / "build" / "localization-90200" / "mld-translation-queue.jsonl"
QUEUE_SHA = "0970d0434bf8c4e092cc977ec599fa6ce2e8743528409d723ef77a91b090d9ef"
PLAIN_SNAPSHOT = REPO / "build" / "live-consistency-reverse-90200" / "MD.mld.plain.bin"
PLAIN_SNAPSHOT_SHA = "e9368582a9afa30828a25a506152ccff394e19353aed0e395461a8e9cbbabe8f"
DECODED = REPO / "build" / "live-consistency-reverse-90200" / "MD.mld.decoded.json"
DECODED_SHA = "9af2a330947a6562d69b9d4034e685f65c2885e94d3cb993f36e0c92e2b707ae"
GLOSSARY = REPO / "localization" / "quality" / "glossary.json"
GLOSSARY_SHA = "b472b896bd819ebb1170a14563624283c542de99abd2a160b838cd43967d5946"

BUNDLE_BYTES = 2643750
BUNDLE_SHA = "9d5e4cf1b92493e7ff37f040a04cf7b872bb552bee416063273eed3241ba0d8d"
SCRIPT_BYTES = 2642000
CIPHER_SHA = "4f3bb54f68e1cd3a8c1e32973979fae9da0adc31aff90e94cc04abb8880fadaf"
PLAIN_BYTES = 2641991
TEXTASSET_NAME = "MD.mld"
TEXTASSET_PATH_ID = 1618177082088658641
EXPECTED_OBJECTS = {(1, "AssetBundle"), (TEXTASSET_PATH_ID, "TextAsset")}

EXPECTED_ENTRIES = 63639
EXPECTED_DATA_MAP_KEYS = 63573
EXPECTED_DATA_LIST_KEYS = 66
EXPECTED_UNIQUE_SOURCES = 15
EXPECTED_BOUND_KEYS = 18

SEP_RE = re.compile(b"(<ssp>|<sp>)")
FORBIDDEN_TAGS = (b"<cn>", b"<sp>", b"<ssp>", b"<cm>")
FORBIDDEN_OUTPUT_ROOTS = (
    REPO / "work" / "local-assets",
    REPO / "build" / "localization-90200",
)


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


def read_frozen_index_row() -> list:
    if sha_file(INDEX) != INDEX_SHA:
        raise GateError(f"frozen 1077100 asset index changed: {INDEX}")
    table = msgpack.unpackb(INDEX.read_bytes(), raw=False, strict_map_key=False)
    if len(table) != 1:
        raise GateError("frozen 1077100 asset index is not a single MsgPack table")
    row = table[0].get(LOGICAL)
    if (not isinstance(row, list) or len(row) != 3 or row[1] != REMOTE
            or row[2] != BUNDLE_BYTES):
        raise GateError("frozen MD.mld logical/remote/declared length changed")
    return row


def read_frozen_audit() -> dict:
    if sha_file(AUDIT) != AUDIT_SHA:
        raise GateError(f"independent MD.mld source audit changed: {AUDIT}")
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    if (audit.get("status") != AUDIT_STATUS
            or audit.get("frozen_bundle_sha256") != BUNDLE_SHA
            or audit.get("frozen_bundle_bytes") != BUNDLE_BYTES
            or audit.get("remote") != REMOTE
            or audit.get("MD_TextAsset_name") != TEXTASSET_NAME
            or audit.get("MD_TextAsset_path_id") != TEXTASSET_PATH_ID
            or audit.get("MD_TextAsset_script_bytes") != SCRIPT_BYTES
            or audit.get("MD_TextAsset_script_sha256") != CIPHER_SHA
            or audit.get("saved_plain_snapshot_bytes") != PLAIN_BYTES
            or audit.get("saved_plain_snapshot_sha256") != PLAIN_SNAPSHOT_SHA
            or not audit.get("decrypt_source_exact_saved_plain")
            or not audit.get("encrypt_original_plain_exact_source_cipher")
            or not audit.get("plain_decoded_full_map_list_equal")
            or audit.get("plain_entry_segments") != EXPECTED_ENTRIES
            or audit.get("plain_data_map_keys") != EXPECTED_DATA_MAP_KEYS
            or audit.get("plain_data_list_keys") != EXPECTED_DATA_LIST_KEYS):
        raise GateError("independent MD.mld source audit did not pass")
    return audit


def read_frozen_queue() -> dict[str, dict]:
    """The 15 allowed UI ``data_map`` sources and their 18 bound keys."""
    if sha_file(QUEUE) != QUEUE_SHA:
        raise GateError(f"frozen MD.mld source queue changed: {QUEUE}")
    source: dict[str, dict] = {}
    keys: set[str] = set()
    occurrences = 0
    for row in load_jsonl(QUEUE):
        sid = str(row.get("source_sha256", ""))
        text = row.get("source")
        if not isinstance(text, str) or not text or sha_bytes(text.encode("utf-8")) != sid:
            raise GateError("frozen MD.mld queue row has an invalid source identity")
        if sid in source:
            raise GateError(f"duplicate frozen MD.mld source SHA: {sid}")
        examples = row.get("examples")
        if not isinstance(examples, list) or not examples:
            raise GateError(f"frozen MD.mld queue row has no bound key: {sid}")
        for example in examples:
            if (not isinstance(example, dict)
                    or example.get("logical") != LOGICAL
                    or example.get("textasset_name") != TEXTASSET_NAME
                    or example.get("section") != "data_map"
                    or not isinstance(example.get("key"), str)
                    or not example["key"]
                    or example["key"] in keys):
                raise GateError("frozen MD.mld queue occurrence provenance is not allowlisted")
            keys.add(example["key"])
            occurrences += 1
        source[sid] = row
    if len(source) != EXPECTED_UNIQUE_SOURCES or occurrences != EXPECTED_BOUND_KEYS:
        raise GateError(
            f"frozen MD.mld allowlist drift: {len(source)} unique sources / "
            f"{occurrences} keys, expected {EXPECTED_UNIQUE_SOURCES}/"
            f"{EXPECTED_BOUND_KEYS}"
        )
    return source


def bound_keys(source: dict[str, dict]) -> dict[str, tuple[str, str]]:
    bound: dict[str, tuple[str, str]] = {}
    for sid, row in source.items():
        for example in row["examples"]:
            if example["key"] in bound:
                raise GateError(f"duplicate bound MD.mld key: {example['key']}")
            bound[example["key"]] = (sid, str(row["source"]))
    return bound


def read_decoded_snapshot() -> dict:
    if sha_file(DECODED) != DECODED_SHA:
        raise GateError(f"archived MD.mld decoded snapshot changed: {DECODED}")
    decoded = json.loads(DECODED.read_text(encoding="utf-8"))
    if (not isinstance(decoded, dict)
            or len(decoded.get("data_map", {})) != EXPECTED_DATA_MAP_KEYS
            or len(decoded.get("data_list", {})) != EXPECTED_DATA_LIST_KEYS):
        raise GateError("archived MD.mld decoded snapshot is not the 63,639-entry table")
    return decoded


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise GateError(f"{path}:{number}: expected a JSON object")
            rows.append(value)
    return rows


# ------------------------------------------------------------------ plaintext model


def parse_plain(plain: bytes) -> tuple[list[bytes], list[bytes], dict[str, bytes]]:
    """Split the payload into fields, separators and a key -> raw value map.

    ``<ssp>`` occurs exactly once and is the order-section boundary; ``<sp>`` is
    the ordinary field separator.  Any other count means the payload is not the
    audited 63,639-entry table and nothing may be rewritten.
    """
    parts = SEP_RE.split(plain)
    if len(parts) != 2 * EXPECTED_ENTRIES - 1 or parts.count(b"<ssp>") != 1:
        raise GateError("MD.mld entry/segment delimiter structure changed")
    fields = parts[::2]
    separators = parts[1::2]
    mapping: dict[str, bytes] = {}
    for field in fields:
        if field.count(b"<cn>") != 1:
            raise GateError("unexpected MD.mld key/value delimiter")
        key, value = field.split(b"<cn>", 1)
        name = key.decode("utf-8")
        if name in mapping:
            raise GateError(f"duplicate MD.mld config key: {name}")
        mapping[name] = value
    return fields, separators, mapping


def join_plain(fields: list[bytes], separators: list[bytes]) -> bytes:
    if len(fields) != len(separators) + 1:
        raise GateError("MD.mld field/separator arity changed")
    out = [fields[0]]
    for separator, field in zip(separators, fields[1:]):
        out.append(separator)
        out.append(field)
    return b"".join(out)


def assert_plain_matches_decoded(mapping: dict[str, bytes], decoded: dict) -> None:
    if set(mapping) != set(decoded["data_map"]) | set(decoded["data_list"]):
        raise GateError("original MD.mld plaintext key family drift")
    for key, value in decoded["data_map"].items():
        if mapping[key].decode("utf-8") != value:
            raise GateError(f"original MD.mld scalar differs from decoded snapshot: {key}")
    for key, parts in decoded["data_list"].items():
        if mapping[key].decode("utf-8").split("<cm>") != list(parts):
            raise GateError(f"original MD.mld list differs from decoded snapshot: {key}")


def mutate_plain(plain: bytes, decoded: dict, source: dict, translations: dict
                 ) -> tuple[bytes, list[dict], int]:
    """Rewrite only the allowlisted bound keys; every other byte must survive."""
    fields, separators, mapping = parse_plain(plain)
    assert_plain_matches_decoded(mapping, decoded)
    bound = bound_keys(source)
    for key, (sid, text) in bound.items():
        if key not in mapping:
            raise GateError(f"bound MD.mld key is absent from the payload: {key}")
        if mapping[key] != text.encode("utf-8"):
            raise GateError(f"bound MD.mld key no longer holds its queued source: {key}")

    accepted_keys = {
        example["key"] for sid, row in source.items() if sid in translations
        for example in row["examples"]
    }
    new_fields: list[bytes] = []
    changes: list[dict] = []
    visited = 0
    for field in fields:
        key_raw, value = field.split(b"<cn>", 1)
        name = key_raw.decode("utf-8")
        if name not in bound:
            new_fields.append(field)
            continue
        sid, text = bound[name]
        if sid not in translations:
            new_fields.append(field)
            continue
        localized = str(translations[sid]["translation"])
        replacement = localized.encode("utf-8")
        if any(tag in replacement for tag in FORBIDDEN_TAGS):
            raise GateError(f"localized MD.mld value contains an in-band delimiter: {name}")
        if value != text.encode("utf-8"):
            raise GateError(f"bound MD.mld value changed while rewriting: {name}")
        new_fields.append(key_raw + b"<cn>" + replacement)
        visited += 1
        if localized != text:
            changes.append({
                "key": name, "source_sha256": sid,
                "old_sha256": sha_bytes(value),
                "new_sha256": sha_bytes(replacement),
                "source": text, "translation": localized,
            })
    if visited != len(accepted_keys):
        raise GateError(
            f"MD.mld bound occurrences not all reached: {visited}/{len(accepted_keys)}"
        )
    if len(changes) > EXPECTED_BOUND_KEYS:
        raise GateError("more MD.mld bound keys changed than are allowlisted")
    result = join_plain(new_fields, separators)
    check_fields, check_separators, check_mapping = parse_plain(result)
    if check_separators != separators:
        raise GateError("MD.mld segment separators changed during rewrite")
    if list(check_mapping) != list(mapping):
        raise GateError("MD.mld key order changed during rewrite")
    for key, value in mapping.items():
        if key in accepted_keys:
            sid, _text = bound[key]
            if check_mapping[key] != str(translations[sid]["translation"]).encode("utf-8"):
                raise GateError(f"MD.mld translation not found at bound key: {key}")
        elif check_mapping[key] != value:
            raise GateError(f"unrelated MD.mld value mutated: {key}")
    return result, changes, visited


def audit_reloaded_plain(reloaded: bytes, original: bytes, changes: list[dict]) -> None:
    """Reloaded payload: separators, key order and untouched fields all identical."""
    _, original_separators, original_mapping = parse_plain(original)
    _, reloaded_separators, reloaded_mapping = parse_plain(reloaded)
    if reloaded_separators != original_separators:
        raise GateError("reloaded MD.mld segment separators differ from the source")
    if list(reloaded_mapping) != list(original_mapping):
        raise GateError("reloaded MD.mld key order differs from the source")
    changed = {change["key"]: change["translation"] for change in changes}
    if len(changed) != len(changes):
        raise GateError("duplicate changed MD.mld key in the write-back record")
    for key, value in original_mapping.items():
        if key in changed:
            if reloaded_mapping[key] != changed[key].encode("utf-8"):
                raise GateError(f"reloaded MD.mld bound key lost its translation: {key}")
        elif reloaded_mapping[key] != value:
            raise GateError(f"reloaded MD.mld untouched field changed: {key}")


# ------------------------------------------------------------------ write-back


def mld_textasset(env):
    objects = list(env.objects)
    if {(int(obj.path_id), obj.type.name) for obj in objects} != EXPECTED_OBJECTS:
        raise GateError("MD.mld original Unity object map changed")
    match = [obj for obj in objects
             if obj.type.name == "TextAsset" and int(obj.path_id) == TEXTASSET_PATH_ID]
    if len(match) != 1:
        raise GateError("MD.mld encrypted TextAsset identity missing")
    data = match[0].read()
    if str(data.m_Name) != TEXTASSET_NAME:
        raise GateError(f"MD.mld TextAsset name changed: {data.m_Name!r}")
    return match[0], data


def materialize_bundle(original: Path, output: Path, translations: dict,
                       require_complete: bool) -> dict:
    if original.name != REMOTE or output.name != REMOTE:
        raise GateError("MD.mld original/output must keep the official remote name")
    if original.resolve() == output.resolve():
        raise GateError("MD.mld source and output path are identical")
    if not original.is_file() or original.stat().st_size != BUNDLE_BYTES:
        raise GateError("frozen MD.mld source bundle missing or wrong size")
    source_sha = sha_file(original)
    if source_sha != BUNDLE_SHA:
        raise GateError("frozen MD.mld source bundle SHA mismatch")

    source = read_frozen_queue()
    missing = sorted(set(source) - set(translations))
    if missing and require_complete:
        raise GateError(
            f"{len(missing)} allowlisted MD.mld source IDs have no gate-accepted translation"
        )

    env = UnityPy.load(str(original))
    object_map = {(int(obj.path_id), obj.type.name) for obj in env.objects}
    non_text_raw = {
        (int(obj.path_id), obj.type.name): sha_bytes(obj.get_raw_data())
        for obj in env.objects if obj.type.name != "TextAsset"
    }
    obj, data = mld_textasset(env)
    cipher = bytes(data.m_Script)
    if len(cipher) != SCRIPT_BYTES or sha_bytes(cipher) != CIPHER_SHA:
        raise GateError("frozen MD.mld source cipher SHA/bytes mismatch")
    plain = decrypt_payload(cipher)
    if sha_bytes(plain) != PLAIN_SNAPSHOT_SHA:
        raise GateError("frozen MD.mld source plaintext SHA mismatch")
    if plain != PLAIN_SNAPSHOT.read_bytes():
        raise GateError("frozen MD.mld source plaintext differs from the archived snapshot")
    if encrypt_payload(plain) != cipher:
        raise GateError("frozen MD.mld ciphertext cannot be reproduced from its plaintext")

    decoded = read_decoded_snapshot()
    text, changes, visited = mutate_plain(plain, decoded, source, translations)
    recipher = encrypt_payload(text)
    if decrypt_payload(recipher) != text:
        raise GateError("rewritten MD.mld ciphertext failed the AES roundtrip")

    data.m_Script = recipher
    data.save()

    files = list(env.files.values())
    if len(files) != 1 or not hasattr(files[0], "save"):
        raise GateError("unsupported MD.mld UnityFS layout")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".writing")
    if temporary.exists():
        raise FileExistsError(str(temporary))
    try:
        temporary.write_bytes(files[0].save(packer="original"))
        check = UnityPy.load(str(temporary))
        if {(int(o.path_id), o.type.name) for o in check.objects} != object_map:
            raise GateError("MD.mld Unity object map changed on write-back")
        for other in check.objects:
            if other.type.name == "TextAsset":
                continue
            key = (int(other.path_id), other.type.name)
            if sha_bytes(other.get_raw_data()) != non_text_raw[key]:
                raise GateError(f"MD.mld non-text Unity object bytes changed: {key}")
        check_obj, check_data = mld_textasset(check)
        if int(check_obj.path_id) != int(obj.path_id):
            raise GateError("MD.mld TextAsset path id changed")
        reloaded_cipher = bytes(check_data.m_Script)
        if reloaded_cipher != recipher:
            raise GateError("MD.mld encrypted TextAsset failed the UnityFS roundtrip")
        reloaded_plain = decrypt_payload(reloaded_cipher)
        if reloaded_plain != text:
            raise GateError("reloaded MD.mld ciphertext does not decrypt to the expected plaintext")
        audit_reloaded_plain(reloaded_plain, plain, changes)
        if sha_file(original) != source_sha:
            raise GateError("frozen MD.mld source bundle was modified during write-back")
        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "logical": LOGICAL, "remote": REMOTE,
        "source_path": str(original), "source_sha256": source_sha,
        "source_bytes": BUNDLE_BYTES,
        "source_script_sha256": CIPHER_SHA,
        "source_plain_sha256": PLAIN_SNAPSHOT_SHA,
        "localized_sha256": sha_file(output),
        "localized_bytes": output.stat().st_size,
        "localized_script_sha256": sha_bytes(recipher),
        "localized_plain_sha256": sha_bytes(text),
        "allowlisted_unique_sources": EXPECTED_UNIQUE_SOURCES,
        "allowlisted_bound_keys": EXPECTED_BOUND_KEYS,
        "release_accepted_unique_sources": len(translations),
        "bound_occurrences_rewritten": visited,
        "modified_occurrences": len(changes),
        "unchanged_MD_keys": EXPECTED_ENTRIES - len(changes),
        "section_separator_`<ssp>`_preserved": True,
        "AES192_CBC_PKCS7_exact_roundtrip": True,
        "non_text_unity_objects_byte_identical": True,
        "unityfs_reload_raw_bytes_audited": True,
        "unityfs_packer": "original",
        "changes": changes,
        "output_path": str(output),
    }


# ------------------------------------------------------------------ driver


def refuse_output_root(dest: Path) -> None:
    if dest.exists() or dest.with_name(dest.name + ".incomplete").exists():
        raise FileExistsError("MD.mld output root must be NEW and isolated")
    for forbidden in FORBIDDEN_OUTPUT_ROOTS:
        if dest.is_relative_to(forbidden.resolve()):
            raise GateError(f"refusing to write inside a read-only/canonical root: {forbidden}")
    source_root = SOURCE_ROOT.resolve()
    if dest.is_relative_to(source_root) or source_root.is_relative_to(dest / "x"):
        raise GateError("refusing to write inside the frozen MD.mld source cohort")


def run(args) -> int:
    translation_paths = [_resolve(path) for path in args.translations]
    dest = _resolve(args.output_root)
    identity = frozen_version(args.client_version, args.asset_version, INDEX)
    read_frozen_index_row()
    read_frozen_audit()
    source = read_frozen_queue()
    glossary_path = GLOSSARY if GLOSSARY.is_file() else None
    if glossary_path is not None and sha_file(glossary_path) != GLOSSARY_SHA:
        raise GateError(f"localization glossary changed: {glossary_path}")
    glossary = load_glossary(glossary_path)
    translations = load_gate_accepted(
        translation_paths, source, glossary,
        label="allowlisted 15 MD.mld UI data_map sources",
    )
    missing = sorted(set(source) - set(translations))
    preflight = {
        "kind": "mld-90200-release-preflight",
        "version_identity": identity,
        "allowlisted_unique_sources": EXPECTED_UNIQUE_SOURCES,
        "allowlisted_bound_keys": EXPECTED_BOUND_KEYS,
        "release_accepted_source_values": len(translations),
        "missing_release_accepted": len(missing),
        "require_complete": bool(args.require_complete),
        "ready": not missing,
        "output_created": False,
    }
    print(json.dumps(preflight, ensure_ascii=False, indent=2))
    if args.preflight_only:
        return 0 if (not missing or not args.require_complete) else 3
    if missing and args.require_complete:
        raise GateError(
            f"{len(missing)} allowlisted MD.mld source IDs have no gate-accepted "
            "translation; an owner-reviewed accepted ledger is required"
        )
    refuse_output_root(dest)

    temporary = dest.with_name(dest.name + ".incomplete")
    temporary.mkdir(parents=True)
    try:
        record = materialize_bundle(
            SOURCE, temporary / identity["scope"] / REMOTE, translations,
            bool(args.require_complete),
        )
        record["output_path"] = str(dest / identity["scope"] / REMOTE)
        manifest = {
            "schema_version": 1,
            "kind": "mltd-frozen-1077100-MD-mld-encrypted-textasset-localization-overlay",
            "app_version": args.client_version,
            "asset_version": args.asset_version,
            "scope": identity["scope"],
            "output_root": str(dest),
            "source_bundle_root": str(SOURCE_ROOT),
            "source_index_sha256": INDEX_SHA,
            "source_audit_sha256": sha_file(AUDIT),
            "source_queue_sha256": sha_file(QUEUE),
            "plain_snapshot_sha256": PLAIN_SNAPSHOT_SHA,
            "decoded_snapshot_sha256": sha_file(DECODED),
            "snapshot_sha256": sha_file(SNAPSHOT),
            "glossary_sha256": sha_file(glossary_path) if glossary_path else None,
            "translations": [
                {"path": str(path), "sha256": sha_file(path)} for path in translation_paths
            ],
            "allowlisted_unique_sources": EXPECTED_UNIQUE_SOURCES,
            "allowlisted_bound_keys": EXPECTED_BOUND_KEYS,
            "release_accepted_source_values": len(translations),
            "bundles_written": 1,
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
            "client_modified": False,
            "client_verified": False,
            "real_device_verified": False,
            "status": (
                "isolated_candidate_not_published" if args.require_complete
                else "isolated_partial_trial_never_release"
            ),
            "bundles": [record],
        }
        (temporary / "mld-90200-manifest.json").write_text(
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
                        help="report release coverage without writing the bundle")
    parser.add_argument("--allow-partial", dest="require_complete", action="store_false",
                        help="isolated trial only: allows a partial ledger, never release-ready")
    parser.set_defaults(require_complete=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except GateError as exc:
        print(json.dumps({
            "kind": "mld-90200-materializer", "status": "refused",
            "reason": str(exc), "output_created": False,
        }, ensure_ascii=False, indent=2))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
