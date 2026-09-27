#!/usr/bin/env python3
"""Publish restored textures to the public bucket and record them in the manifest.

Each restored 512x512 texture is uploaded under `images/<sha256>.png` — the key
shape `manifests/images.manifest.json` already documents — and the matching
manifest entry gets its `localized` digest and distribution pointers filled in.

Text stays in the repository, binaries stay outside it: this script is the only
thing that touches the bucket, and it never rewrites the texture bytes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
BUCKET = os.environ.get("MLTD_ASSET_BUCKET", "mltd-translation-candidates")
PUBLIC_BASE = os.environ.get("MLTD_ASSET_BASE", "https://pub-mltd-assets.nyaneko.cn")
RELEASE_TAG = os.environ.get("MLTD_RELEASE_TAG", "v1.0.0-assets")


def token() -> str:
    value = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not value:
        sys.exit("[!] CLOUDFLARE_API_TOKEN is not set")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def put(tok: str, key: str, data: bytes) -> str:
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}"
        f"/r2/buckets/{BUCKET}/objects/{urllib.parse.quote(key)}"
    )
    request = urllib.request.Request(
        url, data=data, headers={"Authorization": f"Bearer {tok}", "Content-Type": "image/png"}, method="PUT"
    )
    last = "retries exhausted"
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                if json.loads(resp.read().decode("utf-8")).get("success"):
                    return ""
                return "unsuccessful response"
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403):
                return f"HTTP {exc.code}: {exc.read().decode('utf-8')[:200]}"
            last = f"HTTP {exc.code}"
            time.sleep(3 * (attempt + 1))
        except Exception as exc:
            last = str(exc)
            time.sleep(3 * (attempt + 1))
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=ROOT / "images" / "localized")
    parser.add_argument("--manifest", type=Path, default=ROOT / "manifests" / "images.manifest.json")
    parser.add_argument("--sha-fields", type=Path, default=ROOT / "work" / "restore-summary.json")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    if not args.manifest.is_file():
        sys.exit(f"[!] manifest not found: {args.manifest}")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    by_digest = {}
    for entry in manifest.get("images", []):
        by_digest[entry.get("original", {}).get("sha256")] = entry

    summary = []
    if args.sha_fields.is_file():
        summary = json.loads(args.sha_fields.read_text(encoding="utf-8"))

    published = []
    tok = token()
    for result in summary:
        if not result.get("ok"):
            continue
        path = ROOT / result["output"]
        if not path.is_file():
            print(f"    [x] {result['output']} disappeared before publish", file=sys.stderr)
            continue
        digest = sha256_file(path)
        if digest != result.get("sha256"):
            print(f"    [x] {result['task_id']}: restored digest changed on disk", file=sys.stderr)
            continue
        key = f"images/restored/{result['task_id']}/restored-texture.png"
        error = put(tok, key, path.read_bytes())
        if error:
            print(f"    [x] {key}: {error}", file=sys.stderr)
            continue
        entry = by_digest.get(result.get("source_sha256"))
        if entry is not None:
            entry["localized"] = {
                "relative_path": result["output"],
                "sha256": digest,
            }
            entry.setdefault("distribution", {}).update(
                {
                    "storage_policy": "external_binary_hosting",
                    "cloudflare_r2": {"key": key, "url_template": f"{PUBLIC_BASE}/{key}"},
                    "github_release": {
                        "asset_name": f"tex_{digest[:16]}.png",
                        "url_template": (
                            "https://github.com/{owner}/{repo}/releases/download/"
                            f"{RELEASE_TAG}/tex_{digest[:16]}.png"
                        ),
                    },
                }
            )
        published.append({"task_id": result["task_id"], "key": key, "sha256": digest})
        print(f"    [ok] {key}")

    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ledger = ROOT / "work" / "restore-ledger.json"
    ledger.write_text(json.dumps(published, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[*] published {len(published)} texture(s); manifest updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
