#!/usr/bin/env python3
"""Materialize the canonical MLTD zh-CN System Prompt audit snapshot."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.mltd_translation_prompt import compile_prompt_bundle, write_prompt_snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--glossary", type=Path, default=Path("localization/quality/glossary.json"))
    ap.add_argument(
        "--character-evidence",
        type=Path,
        default=Path("build/localization-90200/character-voice-evidence.json"),
    )
    ap.add_argument("--output", type=Path, default=Path("localization/prompts/mltd-zhcn-system.md"))
    ap.add_argument(
        "--manifest",
        type=Path,
        default=Path("localization/prompts/mltd-zhcn-system.manifest.json"),
    )
    args = ap.parse_args()
    bundle = compile_prompt_bundle(
        args.glossary,
        args.character_evidence,
        output_path=args.output,
    )
    write_prompt_snapshot(bundle, args.output, args.manifest)
    print(json.dumps(bundle.manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
