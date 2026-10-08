"""Inspect registered local evidence without executing producers or reading captures."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def inspect_reference(root: Path, reference: dict) -> dict:
    relative = reference["path"]
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Reference escapes repository: {relative}")
    result = {"path": relative, "availability": "missing"}
    if not target.is_file():
        return result
    result["availability"] = "present"
    expected = reference.get("sha256")
    if expected:
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        result["availability"] = "verified" if digest.lower() == expected.lower() else "changed"
    return result


def inventory(root: Path, catalog: dict, query: str = "") -> dict:
    if catalog.get("schema_version") != 1:
        raise ValueError("Unsupported evidence index schema")
    results = []
    seen = set()
    for entry in catalog["entries"]:
        if entry["id"] in seen:
            raise ValueError(f"Duplicate evidence id: {entry['id']}")
        seen.add(entry["id"])
        if query.casefold() not in json.dumps(entry, ensure_ascii=False).casefold():
            continue
        results.append({**entry, "references": [inspect_reference(root, r) for r in entry["references"]]})
    return {"schema_version": 1, "note": "Availability does not establish semantic correctness or test success.", "entries": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=ROOT / "configs/server-evidence-index.json")
    parser.add_argument("--query", default="", help="Filter by topic, operation or implementation path")
    parser.add_argument("--output", type=Path, help="Write a UTF-8 JSON report without shell encoding conversion")
    parser.add_argument("--check", action="store_true", help="Fail if a registered reference is missing or changed")
    args = parser.parse_args()
    result = inventory(ROOT, json.loads(args.index.read_text(encoding="utf-8")), args.query)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    bad = any(r["availability"] in {"missing", "changed"} for e in result["entries"] for r in e["references"])
    return int(args.check and (bad or not result["entries"]))


if __name__ == "__main__":
    raise SystemExit(main())
