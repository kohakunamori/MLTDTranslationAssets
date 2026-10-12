"""Read the shared translation/image policy before any image provider work."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inspect(root: Path) -> dict:
    path = root / "configs/localization-pipeline.json"
    if not path.is_file():
        return {"images_enabled": False, "reason": "Image localization is disabled by default."}
    document = json.loads(path.read_text(encoding="utf-8"))
    images = document.get("images", {})
    if document.get("schema_version") != 1 or type(images.get("enabled")) is not bool:
        raise ValueError("invalid localization pipeline feature policy")
    return {"images_enabled": images["enabled"],
            "reason": "Image localization enabled." if images["enabled"] else
                      images.get("disabled_reason", "Image localization disabled.")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = inspect(args.root)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"images_enabled={str(result['images_enabled']).lower()}\n")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
