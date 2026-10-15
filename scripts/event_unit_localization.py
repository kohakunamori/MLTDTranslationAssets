"""Source-bound extraction and verified Unity JSON overlay for event-unit talks."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import UnityPy

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from pipelines.core.hashing import sha256_file, sha256_text
from pipelines.text.translation_tokens import validate_translation
import bundle_incremental as incremental
from scripts.japanese_residue import repair_reason

ROOT = Path(__file__).resolve().parents[1]
MEMO = "manifests/event-unit-source-index.json"
FAMILY = re.compile(r"^event_unit_talk_\d+\.unity3d$")
WRITER_FILES = incremental.COMMON_FILES + (
    "scripts/event_unit_localization.py", "pipelines/text/translation_tokens.py",
)


def is_bundle(name):
    return bool(FAMILY.fullmatch(name))


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()] if path.is_file() else []


def read_document(path):
    env = UnityPy.load(path if isinstance(path, bytes) else str(path))
    objects = [obj for obj in env.objects if obj.type.name == "TextAsset"]
    if len(objects) != 1:
        raise ValueError(f"{path}: expected one event-unit TextAsset, got {len(objects)}")
    obj = objects[0]
    asset = obj.read()
    value = asset.m_Script
    text = value.lstrip("\ufeff") if isinstance(value, str) else bytes(value).decode("utf-8-sig")
    doc = json.loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("commands"), list):
        raise ValueError(f"{path}: missing commands list")
    if any(not isinstance(command, dict) for command in doc["commands"]):
        raise ValueError(f"{path}: malformed command")
    return env, obj, asset, doc


def sources(doc):
    return {f"commands[{i}].text": command["text"]
            for i, command in enumerate(doc["commands"])
            if isinstance(command.get("text"), str) and re.search(r"[ぁ-ゖァ-ヺ一-龯]", command["text"])}


def merge_rows(bundle, current, previous, version, client, memory=None):
    retained = {(row["item_key"], row["source_sha256"]): row for row in previous}
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result = []
    for key, ja in current.items():
        sid = sha256_text(ja)
        prior = retained.get((key, sid))
        if prior is not None and prior.get("ja") == ja:
            result.append(prior)
            continue
        zh = (memory or {}).get(sid, "")
        if zh:
            try:
                validate_translation(ja, zh)
            except ValueError:
                zh = ""
        row = {"asset_version": version, "client_version": None,
               "source_client_version": client, "bundle": bundle,
               "item_key": key, "source_sha256": sid, "ja": ja,
               "zh": zh, "status": "accepted" if zh else "untranslated",
               "translation_stage": "untranslated", "updated_at": timestamp}
        # Reused translations retain their original provenance below in refresh.
        result.append(row)
    return result


def source_digest(rows):
    return incremental.digest(sorted((r["item_key"], r["ja"], r["source_sha256"]) for r in rows))


def snapshot(env):
    return {(obj.assets_file.name, obj.path_id, obj.type.name): obj.get_raw_data() for obj in env.objects}


def patch_document(doc, rows):
    expected = copy.deepcopy(doc)
    available = sources(doc)
    candidates = {}
    for row in rows:
        ja, zh = row["ja"], row["zh"]
        if row.get("status") != "accepted" or not zh:
            continue
        if sha256_text(ja) != row["source_sha256"]:
            raise ValueError("event-unit source hash mismatch")
        key = row["item_key"]
        if available.get(key) != ja:
            continue  # upstream changed: never overwrite a different command
        validate_translation(ja, zh)
        if key in candidates and candidates[key] != zh:
            raise ValueError(f"conflicting event-unit translations: {key}")
        candidates[key] = zh
    changed = 0
    for key, zh in candidates.items():
        position = int(re.fullmatch(r"commands\[(\d+)\]\.text", key)[1])
        if expected["commands"][position]["text"] != zh:
            expected["commands"][position]["text"] = zh
            changed += 1
    return expected, changed


def save_localized_bundle(source, output, rows):
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError("event-unit output must not overwrite its official source")
    env, obj, asset, doc = read_document(source)
    before = snapshot(env)
    expected, changed = patch_document(doc, rows)
    if not changed:
        return None
    serialized = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    asset.m_Script = serialized if isinstance(asset.m_Script, str) else serialized.encode("utf-8")
    asset.save()
    payload = env.file.save(packer="lz4")
    verified, verified_obj, verified_asset, actual = read_document_bytes(payload)
    after = snapshot(verified)
    target = (obj.assets_file.name, obj.path_id, obj.type.name)
    if (actual != expected or set(before) != set(after)
            or any(before[key] != after[key] for key in before if key != target)
            or verified_obj.path_id != obj.path_id or verified_asset.m_Name != asset.m_Name):
        raise ValueError("event-unit Unity round-trip changed unrelated data")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    return {"changed": changed, "exact": changed, "memory": 0,
            "source_bundle_sha256": sha256_file(source),
            "output_plain_sha256": sha256_text(json.dumps(actual, ensure_ascii=False, sort_keys=True)),
            "output_bundle_sha256": sha256_file(output), "output_bytes": len(payload), "scope": "event_units"}


def read_document_bytes(payload):
    # UnityPy accepts bytes as well as paths; use the same strict parser.
    return read_document(payload)


def build_overlay(*, root, index, work, overlay, version, previous, store, downloader, cache_root=None, force=False):
    library = {}
    for path in sorted((root / "locales" / "event-unit").glob("*.jsonl")):
        for row in read_rows(path):
            if row.get("status") == "accepted" and row.get("zh"):
                library.setdefault(row["bundle"], []).append(row)
    records, selected = {}, {}
    writer = incremental.writer_fingerprint(root, WRITER_FILES)
    for name, rows in library.items():
        if not is_bundle(name):
            raise ValueError(f"unsupported event-unit bundle: {name}")
        if name not in index:
            continue  # retired official bundle
        selected[name] = index[name]
        records[name] = {"source_identity": incremental.source_identity(index[name]),
                         "fingerprint": incremental.digest({"source": incremental.source_identity(index[name]),
                                                            "writer": writer, "rows": sorted((r["item_key"], r["source_sha256"], r["zh"]) for r in rows)})}
    plan = incremental.plan(surface="event_units", records=records, rows=selected, previous=previous,
                            object_file=store.object_file, force=force, asset_version=version["asset_version"])
    def build(name):
        row = index[name]
        remote = row["remote"]
        source = work / "archive" / "event_units" / remote
        # The catalogue's opaque field is shared by some distinct logical
        # bundles (e.g. talk 832/834), even at the same byte size. It is not a
        # globally unique content address; do not serve one source as another.
        cache_key = f"event_units/{name}/{row['catalog_hash']}" if row.get("catalog_hash") else None
        downloader(f"{version['asset_root']}/{remote}", source, row["declared_size"], cache_key, cache_root)
        result = save_localized_bundle(source, overlay / "jp-android" / remote, library[name])
        return dict(logical=name, remote=remote, **result) if result else None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        prepared = [row for row in pool.map(build, sorted(plan.rebuild)) if row]
    manifest_path = overlay / "localization-manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {"schema_version": 1, "bundles": []}
    document["bundles"].extend(prepared)
    document["event_units"] = {"bundles_patched": len(prepared), "bundles_reused": len(plan.reusable),
                               "commands_patched": sum(row["changed"] for row in prepared)}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plan, records


def main(argv=None):
    from build_generated_release import download, load_official_index, load_version_manifest
    import source_provenance
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=ROOT / ".llm-official-work")
    parser.add_argument("--full-rescan", action="store_true")
    parser.add_argument("--max-bundles", type=int, default=1200)
    parser.add_argument("--max-new-rows", type=int, default=20000)
    parser.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args(argv)
    version = load_version_manifest(ROOT / "manifests/asset-version.json")
    client = source_provenance.from_library(ROOT)[0]
    if not client:
        raise ValueError("event-unit source client provenance unavailable")
    index = load_official_index(args.index)
    memo_path = ROOT / MEMO
    memo = json.loads(memo_path.read_text(encoding="utf-8")).get("bundles", {}) if memo_path.is_file() else {}
    selected = {name: row for name, row in index.items() if is_bundle(name)}
    if len(selected) > args.max_bundles:
        raise ValueError("event-unit bundle cap exceeded")
    if sum(row["declared_size"] for row in selected.values()) > args.max_bytes:
        raise ValueError("event-unit byte cap exceeded")
    todo, previous = {}, {}
    for name, row in selected.items():
        path = ROOT / "locales/event-unit" / f"{name}.jsonl"
        previous[name] = read_rows(path)
        record = memo.get(name, {})
        if (args.full_rescan or not path.is_file() or record.get("source_identity") != incremental.source_identity(row)
                or record.get("sources") != source_digest(previous[name])):
            todo[name] = row
    # Unambiguous accepted memory only; never invent human provenance.
    memory, stages, ambiguous = {}, {}, set()
    for path in sorted((ROOT / "locales").rglob("*.jsonl")):
        for row in read_rows(path):
            if row.get("status") != "accepted" or not row.get("zh"):
                continue
            if repair_reason(row):
                continue
            sid = row["source_sha256"]
            if sid != sha256_text(row["ja"]):
                raise ValueError(f"source hash mismatch: {path}")
            if sid in memory and memory[sid] != row["zh"]:
                ambiguous.add(sid)
            memory[sid], stages[sid] = row["zh"], row.get("translation_stage")
    for sid in ambiguous:
        memory.pop(sid, None)
    def extract(name):
        row = index[name]
        source = args.work_root / "event_units" / row["remote"]
        download(f"{version['asset_root']}/{row['remote']}", source, row["declared_size"], row.get("catalog_hash"), None)
        _, _, _, doc = read_document(source)
        rows = merge_rows(name, sources(doc), previous[name], version["asset_version"], client, memory)
        for item in rows:
            if item["status"] == "accepted" and item.get("translation_stage") == "untranslated":
                stage = stages.get(item["source_sha256"])
                if stage:
                    item["translation_stage"] = stage
                else:
                    item.pop("translation_stage", None)
        return name, rows
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        extracted = dict(pool.map(extract, sorted(todo)))
    added = sum(len({(row["item_key"], row["source_sha256"]) for row in rows}
                    - {(row["item_key"], row["source_sha256"]) for row in previous[name]})
                for name, rows in extracted.items())
    if added > args.max_new_rows:
        raise ValueError(f"event-unit row cap exceeded: {added}")
    # No memo or source publication until every extraction and cap check passes.
    for name, rows in extracted.items():
        path = ROOT / "locales/event-unit" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
        memo[name] = {"source_identity": incremental.source_identity(index[name]), "sources": source_digest(rows)}
    memo_path.write_text(json.dumps({"schema_version": 1, "bundles": dict(sorted(memo.items()))}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"asset_version": version["asset_version"], "selected": len(selected), "extracted": len(extracted), "added_rows": added}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
