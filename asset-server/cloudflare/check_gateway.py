#!/usr/bin/env python3
"""Live acceptance test for a deployed MLTD asset gateway.

Nothing here is mocked: it talks to the real Worker over HTTPS and compares bytes
against the content-addressed pool in this repository.  It answers four
questions the deployment claims to answer:

1. **Translated objects come back byte-identical** to the release manifest
   (sampled, because the full release is 1.3 GB).
2. **The logical alias works** -- a request for the readable bundle name returns
   the same bytes as the hashed runtime name.
3. **Everything else is forwarded to the official CDN** -- sampled from the
   official catalogue with names this release does not contain, compared against
   a direct fetch of the official file.
4. **The edges stay closed** -- malformed paths are 404, writes are 405, a
   different version is never served from the local store.

Usage::

    python asset-server/cloudflare/check_gateway.py --base https://mltd-assets.<sub>.workers.dev
    python asset-server/cloudflare/check_gateway.py --base https://... --samples 12 --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSET_PREFIX = "production/2018/Android/"
OFFICIAL_BASE = "https://td-assets.bn765.com"
USER_AGENT = "mltd-asset-gateway-check/1"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_index  # noqa: E402  (sibling module: how the deployed table is built)
INDEX_PATH = Path(__file__).resolve().parent / "src" / "index.json"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


class Runner:
    def __init__(self, base: str, official: str, timeout: float = 60.0, proxy: str = ""):
        self.base = base.rstrip("/")
        self.official = official.rstrip("/")
        self.timeout = timeout
        self.checks: list[Check] = []
        # A gateway on *.workers.dev is unreachable from mainland China without
        # a proxy (the hostname is DNS-poisoned there), so testing from such a
        # machine needs one.  Everything else about this script is unchanged.
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            self.opener = urllib.request.build_opener(handler)
        else:
            self.opener = urllib.request.build_opener()

    def record(self, name: str, ok: bool, detail: str, *, echo: bool = True) -> bool:
        self.checks.append(Check(name, ok, detail))
        if echo:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
        return ok

    def request(self, url: str, *, method: str = "GET", headers: dict | None = None):
        # Python's default `Python-urllib/x.y` agent gets a 403 in front of this
        # gateway (Cloudflare treats it as a bot), so every request carries a
        # stable, honest agent instead.
        merged = {"User-Agent": USER_AGENT}
        merged.update(headers or {})
        request = urllib.request.Request(url, method=method, headers=merged)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, lower_headers(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, lower_headers(error.headers), error.read()


def lower_headers(headers) -> dict:
    """Header names are case-insensitive; compare them that way."""
    return {name.lower(): value for name, value in headers.items()}


def load_release(root: Path, version: str) -> dict:
    manifest = json.loads((root / "generated" / version / "manifest.json").read_text(encoding="utf-8"))
    pool = root / "generated" / "objects" / "sha256"
    return {"version": version, "manifest": manifest, "pool": pool}


def pick_translated(manifest: dict, pool: Path, samples: int, max_bytes: int, seed: int) -> list[dict]:
    entries = [e for e in manifest["entries"] if e.get("runtime_path")]
    sizes = {id(e): entry_size(e, pool) for e in entries}
    rng = random.Random(seed)
    rng.shuffle(entries)
    chosen, budget = [], max_bytes
    for entry in entries:
        if len(chosen) >= samples:
            break
        size = sizes[id(entry)]
        if size > budget and chosen:
            continue
        chosen.append(entry)
        budget -= size
    if not chosen:
        chosen = sorted(entries, key=lambda e: sizes[id(e)])[:samples]
    return chosen


def entry_size(entry: dict, pool: Path) -> int:
    return (pool / entry["artifact_sha256"]).stat().st_size


def check_health(runner: Runner, release: dict) -> None:
    status, headers, body = runner.request(f"{runner.base}/healthz")
    if status != 200:
        runner.record("healthz", False, f"HTTP {status}")
        return
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as error:
        runner.record("healthz", False, f"not JSON: {error}")
        return
    version = payload.get("asset_version") or payload.get("active_version")
    paths = payload.get("indexed_paths", payload.get("object_count"))
    ok = payload.get("ok") is True and str(version) == release["version"]
    runner.record("healthz", ok, f"asset_version={version} indexed_paths={paths} "
                                 f"commit={str(payload.get('source_commit'))[:12]}")


def check_translated(runner: Runner, release: dict, samples: list[dict]) -> None:
    pool = release["pool"]
    version = release["version"]
    for entry in samples:
        digest = entry["artifact_sha256"]
        local = (pool / digest).read_bytes()
        local_sha = hashlib.sha256(local).hexdigest()

        url = f"{runner.base}/{version}/{entry['runtime_path']}"
        status, headers, body = runner.request(url)
        if status != 200:
            runner.record(f"runtime {entry['logical_key']}", False, f"HTTP {status}")
            continue
        got = hashlib.sha256(body).hexdigest()
        runner.record(
            f"runtime {entry['logical_key']}",
            got == local_sha and got == digest,
            f"{len(body)}B sha={got[:12]} source={headers.get('x-mltd-asset-source')} "
            f"cache={headers.get('x-mltd-cache')}",
        )

        # Second read of the same object must come from the edge cache, not the
        # repository: that is what keeps GitHub at one read per object.
        status, cached_headers, body = runner.request(url)
        runner.record(
            f"cached  {entry['logical_key']}",
            status == 200 and cached_headers.get("x-mltd-cache") == "hit" and hashlib.sha256(body).hexdigest() == local_sha,
            f"HTTP {status} cache={cached_headers.get('x-mltd-cache')}",
            echo=False,
        )

        if entry["logical_path"] != entry["runtime_path"]:
            status, _, body = runner.request(f"{runner.base}/{version}/{entry['logical_path']}")
            got = hashlib.sha256(body).hexdigest() if status == 200 else "-"
            runner.record(
                f"alias   {entry['logical_key']}",
                status == 200 and got == local_sha,
                f"HTTP {status} sha={got[:12]}",
            )

        head_status, head_headers, _ = runner.request(url, method="HEAD")
        runner.record(
            f"head    {entry['logical_key']}",
            head_status == 200 and str(head_headers.get("content-length")) == str(len(local)),
            f"HTTP {head_status} len={head_headers.get('content-length')}",
            echo=False,
        )

        status, headers, body = runner.request(url, headers={"Range": "bytes=0-99"})
        expect = local[:100]
        runner.record(
            f"range   {entry['logical_key']}",
            status == 206 and body == expect and headers.get("content-range", "").endswith(f"/{len(local)}"),
            f"HTTP {status} bytes={len(body)} content-range={headers.get('content-range')}",
            echo=False,
        )


def official_runtime_names(release: dict) -> set[str]:
    """Every hashed file name the official catalogue for this version knows.

    The release carries the official `.data` catalogue as one of its objects
    (``__official_asset_index__``), which is what makes it possible to pick a
    path that is genuinely *not* translated instead of guessing file names.
    """
    import msgpack

    manifest = release["manifest"]
    entry = next((e for e in manifest["entries"] if e.get("logical_key") == "__official_asset_index__"), None)
    if entry is None:
        return set()
    data = (release["pool"] / entry["artifact_sha256"]).read_bytes()
    decoded = msgpack.unpackb(data, raw=False, strict_map_key=False)
    if not isinstance(decoded, list) or not decoded or not isinstance(decoded[0], dict):
        return set()

    names: set[str] = set()
    for row in decoded[0].values():
        if (isinstance(row, list) and len(row) == 3 and isinstance(row[1], str)
                and row[1].endswith(".unity3d") and "/" not in row[1] and "\\" not in row[1]):
            names.add(row[1])
    return names


def check_official_fallback(runner: Runner, release: dict, *, tries: int, seed: int) -> None:
    published = set()
    for entry in release["manifest"]["entries"]:
        published.add(entry["runtime_path"].removesuffix(".unity3d").rsplit("/", 1)[-1])
        published.add(entry["runtime_path"].removeprefix(ASSET_PREFIX))
        published.add(entry["logical_path"].removeprefix(ASSET_PREFIX))

    catalogue = official_runtime_names(release)
    if not catalogue:
        runner.record("forward miss", False, "the release carries no readable official catalogue object")
        return
    candidates = [name for name in catalogue if name not in published]
    rng = random.Random(seed)
    rng.shuffle(candidates)

    version = release["version"]
    proved = 0
    for name in candidates[:40]:
        if proved >= tries:
            break
        path = f"{ASSET_PREFIX}{name}"
        status, headers, body = runner.request(f"{runner.base}/{version}/{path}")
        if status != 200:
            continue
        official_status, _, official_body = runner.request(f"{runner.official}/{version}/{path}")
        ok = official_status == 200 and official_body == body
        runner.record(
            "forward miss",
            ok,
            f"{name[:16]}... HTTP {status} source={headers.get('x-mltd-asset-source')} "
            f"official={official_status} bytes_match={official_body == body}",
        )
        proved += 1
    if proved == 0:
        runner.record("forward miss", False, "no untranslated official path answered 200 within 40 tries")


def check_other_version(runner: Runner, release: dict, other: str) -> None:
    entry = release["manifest"]["entries"][0]
    path = entry["runtime_path"]
    url = f"{runner.base}/{other}/{path}"
    status, headers, _ = runner.request(url, method="HEAD")
    official_status, _, _ = runner.request(f"{runner.official}/{other}/{path}", method="HEAD")
    runner.record(
        "other version",
        headers.get("x-mltd-asset-source") == "official-other-version" and status == official_status,
        f"HTTP {status} (official {official_status}) source={headers.get('x-mltd-asset-source')}",
    )


def check_edges(runner: Runner, release: dict) -> None:
    version = release["version"]
    # Cloudflare normalises dot-segments before the Worker sees them, so a
    # traversal attempt arrives here as an ordinary (missing) path and ends as
    # the upstream's refusal.  403 and 404 are both refusals; what matters is
    # that nothing is served.
    for path, label in [
        (f"/{version}/production/2018/Android/../etc/passwd", "traversal"),
        (f"/{version}/{ASSET_PREFIX}", "empty segment"),
        (f"/{version}/%2e%2e/etc/passwd", "encoded traversal"),
        ("/", "bare root"),
    ]:
        status, _, _ = runner.request(f"{runner.base}{path}")
        runner.record(f"reject {label}", status in (403, 404), f"HTTP {status}", echo=False)

    status, _, _ = runner.request(f"{runner.base}/{version}/{ASSET_PREFIX}definitely-not-a-real-bundle.unity3d")
    runner.record("unknown file", status in (403, 404), f"HTTP {status} (upstream status forwarded)", echo=False)

    request = urllib.request.Request(f"{runner.base}/{version}/{ASSET_PREFIX}", method="PUT", data=b"x")
    try:
        with runner.opener.open(request, timeout=runner.timeout) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    # 405 is this gateway refusing; 403 is Cloudflare refusing before it gets
    # there.  Either way the write is not allowed, which is the property tested.
    runner.record("read-only", status in (403, 405), f"HTTP {status}", echo=False)


def check_freshness(runner: Runner, release: dict, *, repo_root: Path, allow_stale: bool) -> None:
    """The live gateway must be the table in this working copy, built from the newest release.

    This is the guard against the one failure mode the gateway cannot avoid on
    its own: the repository moves on and nobody rebuilds/redeploys, so the edge
    keeps serving commit-pinned bytes from the previous release.
    """
    status, _, body = runner.request(f"{runner.base}/healthz")
    if status != 200:
        runner.record("freshness", False, f"healthz HTTP {status}")
        return
    live = str(json.loads(body.decode("utf-8")).get("source_commit") or "")

    pinned = ""
    if INDEX_PATH.is_file():
        pinned = str(json.loads(INDEX_PATH.read_text(encoding="utf-8")).get("source_commit") or "")
    runner.record(
        "deployed table",
        bool(pinned) and live == pinned,
        f"live={live[:12] or '-'} local src/index.json={pinned[:12] or '-'}",
    )

    manifest_path = repo_root / "generated" / release["version"] / "manifest.json"
    try:
        newest = build_index.detect_commit(repo_root, manifest_path, None)
    except Exception as error:  # noqa: BLE001 - any git problem is reported, not raised
        runner.record("tracks newest release", False, f"cannot resolve the release commit: {error}")
        return
    current = newest == live
    runner.record(
        "tracks newest release",
        current or allow_stale,
        f"repo={newest[:12]} live={live[:12]}"
        + ("" if current else "  <- the repository has a newer release: run build_index.py --apply && wrangler deploy"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="deployed gateway root, e.g. https://x.workers.dev")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--asset-version", default=None)
    parser.add_argument("--official", default=OFFICIAL_BASE)
    parser.add_argument("--other-version", default="1077720", help="a version the gateway must not serve locally")
    parser.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "",
                        help="HTTP proxy for reaching *.workers.dev (DNS-poisoned in mainland China)")
    parser.add_argument("--allow-stale", action="store_true",
                        help="do not fail when the repository has a newer release than the gateway")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--max-bytes", type=int, default=12 * 1024 * 1024)
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = args.repo_root.resolve()
    version = args.asset_version or str(
        json.loads((root / "manifests" / "asset-version.json").read_text(encoding="utf-8"))["asset_version"]
    )
    if not (root / "generated" / version / "manifest.json").is_file():
        print(f"error: no generated release for {version} under {root}", file=sys.stderr)
        return 2

    release = load_release(root, version)
    samples = pick_translated(release["manifest"], release["pool"], args.samples, args.max_bytes, args.seed)
    runner = Runner(args.base, args.official, proxy=args.proxy)

    print(f"gateway {runner.base}   release {version}   samples {len(samples)}"
          + (f"   proxy {args.proxy}" if args.proxy else ""))
    check_health(runner, release)
    check_freshness(runner, release, repo_root=root, allow_stale=args.allow_stale)
    check_translated(runner, release, samples)
    check_edges(runner, release)
    check_other_version(runner, release, args.other_version)
    check_official_fallback(runner, release, tries=3, seed=args.seed)

    passed = sum(1 for c in runner.checks if c.ok)
    failed = [c for c in runner.checks if not c.ok]
    if args.json:
        print(json.dumps({"base": runner.base, "passed": passed, "failed": len(failed),
                          "checks": [c.__dict__ for c in runner.checks]}, ensure_ascii=False, indent=2))
    print(f"{passed}/{len(runner.checks)} checks passed")
    for check in failed:
        print(f"  FAILED {check.name}: {check.detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
