#!/usr/bin/env python3
"""Close out processed backfill queue rows in the portal database.

Reads the restore summary produced by `restore_texture.py` and PATCHes each row
to `done` (with the localized object key) or `failed` (with the reason). Because
the queue rows are the only record of an upload, a row that is never updated
would be retried forever; the summary file is the bridge.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
D1_ID = os.environ.get("PORTAL_D1_ID", "6dadc7c5-b994-48ea-a35a-a96698b572d5")


def token() -> str:
    value = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not value:
        sys.exit("[!] CLOUDFLARE_API_TOKEN is not set")
    return value


def run_sql(tok: str, sql: str, params: list) -> dict:
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{D1_ID}/query"
    body = json.dumps({"sql": sql, "params": params}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as resp:
        payload = json.load(resp)
    if not payload.get("success"):
        raise RuntimeError(f"D1 query failed: {payload.get('errors')}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=ROOT / "work" / "restore-summary.json")
    parser.add_argument("--ledger", type=Path, default=ROOT / "work" / "restore-ledger.json")
    args = parser.parse_args()

    if not args.summary.is_file():
        print("[*] no restore summary; nothing to close out")
        return 0

    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    ledger = {}
    if args.ledger.is_file():
        for row in json.loads(args.ledger.read_text(encoding="utf-8")):
            ledger[row["task_id"]] = row["key"]

    tok = token()
    closed = 0
    for result in summary:
        job_id = result.get("job_id")
        if not job_id:
            continue
        if result.get("ok"):
            localized_key = ledger.get(result["task_id"], "")
            run_sql(
                tok,
                "UPDATE image_restore_requests SET status='done', localized_key=?, updated_at=? WHERE id=?",
                [localized_key, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), job_id],
            )
        else:
            run_sql(
                tok,
                "UPDATE image_restore_requests SET status='failed', note=?, updated_at=? WHERE id=?",
                [str(result.get("error", ""))[:2000], __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), job_id],
            )
        closed += 1

    print(f"[*] closed out {closed} queue row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
