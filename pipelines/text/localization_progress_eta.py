#!/usr/bin/env python3
"""Estimate translation ETA from recent source-ID progress, never from file size.

This module stores only tiny progress observations in the build workspace.
It does not alter translation outputs, summaries, or any accepted translation.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_NAME = "localization-progress-rate.json"
MIN_SAMPLE_SECONDS = 30
MAX_SAMPLE_SECONDS = 20 * 60
STALE_SECONDS = 5 * 60


def active_translator() -> dict[str, Any] | None:
    """Detect one active local translation worker; avoid assumptions about service state."""
    try:
        import psutil
    except ImportError:
        return None
    found: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
            if not str(info.get("name", "")).lower().startswith("python"):
                continue
            cmdline = info.get("cmdline") or []
            if not any(str(arg).replace("\\", "/").endswith("translate_mltd_api_pool.py")
                       for arg in cmdline):
                continue
            stage = "companion" if any(
                "machine-translation-nongtx-queue.jsonl" in str(arg)
                for arg in cmdline
            ) else "gtx"
            found.append({
                "stage": stage, "pid": info["pid"],
                "started": float(info["create_time"]),
            })
        except (psutil.Error, OSError, ValueError, TypeError):
            continue
    return found[0] if len(found) == 1 else None


def _observations(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema_version") != 1:
            return []
        return doc.get("observations", []) if isinstance(doc.get("observations"), list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write_observations(path: Path, observations: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"schema_version": 1, "observations": observations[-40:]},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _historical_rate(summary_path: Path, process_started: float) -> float | None:
    if not summary_path.is_file():
        return None
    # Only use a completed run that PRECEDES the current worker.
    updated = summary_path.stat().st_mtime
    if updated > process_started + 2 or process_started - updated > 24 * 3600:
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        elapsed = float(summary.get("elapsed_seconds", 0))
        accepted = float(summary.get("accepted", 0))
    except (OSError, ValueError, TypeError):
        return None
    if elapsed <= 0 or accepted < 20:
        return None
    return accepted * 60 / elapsed


def estimate(
    workspace: Path,
    counts: dict[str, dict[str, int]],
    output_paths: dict[str, Path],
    summary_paths: dict[str, Path],
    *,
    now: float | None = None,
    active: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Compute stage-specific ETAs without guessing an ETA for an inactive stage."""
    now = time.time() if now is None else now
    active = active_translator() if active is None else active
    state_path = workspace / STATE_NAME
    prior = _observations(state_path)
    result: dict[str, Any] = {"active_stage": active["stage"] if active else None}
    for stage in ("gtx", "companion"):
        stats = counts[stage]
        done = int(stats["done"])
        pending = int(stats["pending"])
        output = output_paths[stage]
        mtime = output.stat().st_mtime if output.is_file() else 0
        current = bool(active and active.get("stage") == stage)
        item: dict[str, Any] = {
            "pending": pending, "done": done, "active": current,
            "rate_per_minute": None, "eta_seconds": None,
            "basis": "not_running", "fresh": False,
        }
        if pending <= 0:
            item["basis"] = "complete"
        elif not current:
            # An inactive stage can have a previous-run WORK estimate, but no
            # wall-clock completion ETA until the user starts it again.
            rate = _historical_rate(summary_paths[stage], now)
            if rate is not None:
                item["rate_per_minute"] = round(rate, 3)
                item["eta_seconds"] = int(math.ceil(pending * 60 / rate))
                item["basis"] = "previous_completed_run_inactive"
        elif current:
            started = float(active["started"])
            if now - mtime > STALE_SECONDS:
                item["basis"] = "no_recent_output"
            else:
                observations = [
                    record for record in prior
                    if record.get("stage") == stage
                    and record.get("pid") == active.get("pid")
                    and float(record.get("t", 0)) >= started - 2
                    and MIN_SAMPLE_SECONDS <= now - float(record.get("t", 0)) <= MAX_SAMPLE_SECONDS
                    and 0 <= done - int(record.get("done", -1))
                ]
                observations.sort(key=lambda record: record["t"])
                moving = next((record for record in observations
                               if done - int(record["done"]) >= 10), None)
                if moving is not None:
                    elapsed = now - float(moving["t"])
                    item["rate_per_minute"] = round((done - int(moving["done"])) * 60 / elapsed, 3)
                    item["basis"] = "recent_observations"
                    item["fresh"] = True
                else:
                    rate = _historical_rate(summary_paths[stage], started)
                    if rate is not None:
                        item["rate_per_minute"] = round(rate, 3)
                        item["basis"] = "previous_completed_run"
                        item["fresh"] = False
                    else:
                        item["basis"] = "collecting_observations"
                if item["rate_per_minute"] and item["rate_per_minute"] > 0:
                    item["eta_seconds"] = int(math.ceil(pending * 60 / item["rate_per_minute"]))
        result[stage] = item
    if persist and active:
        new_obs = {"t": now, "stage": active["stage"], "pid": active["pid"],
                   "done": counts[active["stage"]]["done"]}
        try:
            _write_observations(state_path, [*prior[-39:], new_obs])
        except OSError:
            result["checkpoint_warning"] = "could_not_save_rate_history"
    result["checkpoint_path"] = str(state_path)
    return result


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "无法估算"
    # ASCII units survive Windows cp936/GBK terminals and redirected logs.
    if seconds < 60:
        return "<1m"
    minutes = math.ceil(seconds / 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins and (days == 0 or hours == 0):
        parts.append(f"{mins}m")
    return "".join(parts) or "<1m"
