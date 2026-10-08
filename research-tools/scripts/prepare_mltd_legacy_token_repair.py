#!/usr/bin/env python3
"""Create immutable-source, release-safe inputs for three old legacy GTX token omissions.

No source archive or original translation JSONL is changed. This is a
source-bound correction of missing control data, NOT linguistic QA approval of
the historical zh-TW translation corpus.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_localize_gtx import PROTECTED_TOKEN_RE, validate_translation

# Lock both the original Japanese SHA and original historical Chinese SHA.
# old/new spans are unambiguous and differ only in restoring the named code.
REPAIRS = {
    "fe8247bf080a163e54f04e8bed75ad74d16a13b5cb273a81a5a8eea4376d17ef":
        ("aeb8787de345d008733eb9f34ff8bc119d95bd1e8ba8815a318e99c8330ba394",
         "真是太美妙了唷\\17\\", "真是太美妙了\\01\\唷\\17\\", "\\01\\"),
    "f8faf75bbc6692da342e190352a0022530a9c023802ed6d8f7d15268bd6a29d0":
        ("b5890e6037beaf6e889a6797c07be9600bca813bf40397cf4fdeba3ef86e35e9",
         "禮物喔～", "禮物\\04\\喔～", "\\04\\"),
    "9645ca8d340272e36d8c4b920bd299ca4833a4c3cc201886e295a2e82ae70336":
        ("28459937a54a626db035ab58352d71ae2bbe47b8c0bce2b02aae7cf24bc61bb2",
         "喔14\\", "喔\\14\\", "\\14\\"),
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(content, encoding="utf-8", newline="\n")
    os.replace(staged, path)


def prepare(input_path: Path, out_dir: Path) -> dict:
    retained = []
    repairs = []
    seen = Counter()
    accepted = 0
    for number, raw in enumerate(input_path.open(encoding="utf-8-sig"), 1):
        if not raw.strip():
            continue
        row = json.loads(raw)
        source = str(row.get("source", ""))
        sid = sha(source)
        if sid not in REPAIRS:
            retained.append(raw if raw.endswith("\n") else raw + "\n")
            continue
        seen[sid] += 1
        if seen[sid] != 1:
            raise ValueError(f"{sid}: multiple affected legacy entries; require manual audit")
        expected_chinese_sha, old, replacement, missing_code = REPAIRS[sid]
        translation = str(row.get("translation", ""))
        if (
            sha(translation) != expected_chinese_sha
            or str(row.get("status", "")) != "official_legacy"
            or str(row.get("bundle", "")) != "MB_jp.gtx"
            or translation.count(old) != 1
        ):
            raise ValueError(f"{sid}: old legacy candidate no longer matches repair evidence")
        try:
            validate_translation(source, translation)
        except ValueError:
            pass
        else:
            raise ValueError(f"{sid}: historical translation no longer needs repair")
        corrected = translation.replace(old, replacement, 1)
        validate_translation(source, corrected)
        original_codes = Counter(PROTECTED_TOKEN_RE.findall(translation))
        repaired_codes = Counter(PROTECTED_TOKEN_RE.findall(corrected))
        if (
            repaired_codes - original_codes != Counter({missing_code: 1})
            or original_codes - repaired_codes
        ):
            raise ValueError(f"{sid}: repair changed other protected tokens")
        new_row = dict(row)
        new_row.update({
            "translation": corrected,
            "status": "agent_translated",
            "provenance": {
                "provider": "local:source-bound-legacy-control-repair",
                "original_file": str(input_path),
                "original_line": number,
                "original_translation_sha256": expected_chinese_sha,
                "missing_control": missing_code,
                "not_human_verified": True,
                "not_full_zhcn_style_reviewed": True,
            },
        })
        repairs.append(new_row)
        accepted += 1
    if set(seen) != set(REPAIRS):
        raise ValueError(f"repair evidence mismatch, missing: {sorted(set(REPAIRS) - set(seen))}")
    if not retained or accepted != len(REPAIRS):
        raise ValueError("no retained legacy records or unexpected repaired count")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = out_dir / "legacy-zh-translations-release-safe.jsonl"
    fixed = out_dir / "legacy-zh-control-repairs.jsonl"
    atomic_text(
        safe,
        "".join(retained),
    )
    atomic_text(
        fixed,
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in repairs),
    )
    manifest = {
        "schema_version": 1,
        "kind": "legacy-gtx-protected-token-release-inputs",
        "original_input": str(input_path),
        "original_input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "filtered_input": str(safe),
        "source_bound_repairs": str(fixed),
        "retained_original_rows": len(retained),
        "repaired_rows": len(repairs),
        "repaired_source_sha256": sorted(seen),
        "original_modified": False,
        "language_quality_approved": False,
        "note": "Only repairs 3 known legacy control-code omissions; preserves historical zh-TW wording pending regional style review.",
    }
    atomic_text(out_dir / "legacy-control-repair-manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=Path("build/localization-90200/legacy-zh-translations.jsonl"),
    )
    parser.add_argument(
        "--out-dir", type=Path,
        default=Path("build/localization-90200/release-inputs"),
    )
    args = parser.parse_args()
    print(json.dumps(prepare(args.input, args.out_dir), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
