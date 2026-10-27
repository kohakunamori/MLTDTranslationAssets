#!/usr/bin/env python3
"""Bounded retry rounds for one translation queue, inside a single job.

Why this exists
---------------
The provider pool gives every row a small number of attempts and then reports it
as terminally failed (``imas-trans:blocking_qa:numeric_literal_mismatch`` and
friends).  Until 2026-10-27 the only recovery was at the *workflow* level: the
caller re-dispatched the whole pipeline whenever a run left rows behind, so a
handful of stubborn rows cost a full build plus a full gateway deployment each
time.  On asset 1077950 three leftover rows burned three extra end-to-end runs
(~48 minutes) to translate three lines.

The rows themselves are not broken.  The pool samples non-deterministically: a
row rejected twice for "酱"/"68" is routinely accepted on a later sample.  What
it needs is *more attempts in the same job*, not another deployment.

This script owns the bookkeeping for that loop:

* ``plan``   decides whether another round is worth running and writes the round
  input (the original queue rows whose ``source_sha256`` is still pending);
* ``record`` folds one round's draft/failed reports back into the state, counts
  attempts per row, and blocks a row once it has spent its attempt budget;
* ``finish`` writes the aggregate summary and the unresolved-row report the
  diagnostics artifact already carries.

The row *shape* matters: ``.llm-failed.jsonl`` keeps only
``source_sha256``/``source``/``errors``, so a retry input has to be rebuilt from
the original queue.  That is what ``plan`` does with the pending set.

Stop reasons, in the order ``plan`` evaluates them:
``drained`` (nothing left), ``limited`` (the operator passed ``max_items``),
``no_progress`` (the last round accepted nothing, so another identical round
would settle nothing either), ``budget`` (a caller-supplied time cap was spent;
off by default) and ``max_rounds``.

``no_progress`` has to be decided *before* ``budget`` and ``max_rounds``: those
two mean "there was more worth doing", which is what makes the caller re-dispatch
the pipeline.  A round that settled nothing is the opposite -- sampling it again
would produce the same rejections -- and reporting it as ``max_rounds`` would
rebuild and redeploy the release after every run, forever, for one line.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

#: A round costs one pool invocation per queue item, and the pool already gives
#: each item ``retries`` attempts of its own.  Rounds, not samples, are the useful
#: unit here: four rounds is enough for the observed failure mode (a QA rule that
#: rejects some samples but not all of them).
DEFAULT_MAX_ROUNDS = 4
#: How many rounds one row may fail before it is taken out of the queue, so a
#: permanently unacceptable row cannot keep a large queue looping.
DEFAULT_MAX_ATTEMPTS = 4
#: Wall-clock ceiling for the whole loop.  Zero means no cap, which is the
#: default: rounds, not minutes, are what makes this loop cheap to bound, and a
#: cap would only truncate work the caller still wants done.  A caller that
#: shares the job with later stages may pass a positive value instead.
DEFAULT_BUDGET_SECONDS = 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def source_id(row: dict[str, Any]) -> str:
    return str(row.get("source_sha256", ""))


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def emit_outputs(path: Path | None, values: dict[str, Any]) -> None:
    """Append ``key=value`` lines to ``$GITHUB_OUTPUT``."""
    if path is None:
        return
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for key, value in values.items():
            if isinstance(value, str):
                text = value
            elif isinstance(value, (dict, list)):
                text = json.dumps(value)
            else:
                text = str(value)
            handle.write(f"{key}={text}\n")


def new_state(
    queue: list[dict[str, Any]],
    *,
    max_rounds: int,
    max_attempts: int,
    budget_seconds: int,
    single_shot: bool,
) -> dict[str, Any]:
    pending: list[str] = []
    seen: set[str] = set()
    for row in queue:
        sid = source_id(row)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        pending.append(sid)
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": time.time(),
        "max_rounds": max_rounds,
        "max_attempts": max_attempts,
        "budget_seconds": budget_seconds,
        "single_shot": single_shot,
        "queue_size": len(pending),
        "rounds": 0,
        "by_round": [],
        "attempts": {},
        "errors": {},
        "pending": pending,
        "blocked": [],
        "accepted": [],
    }


def stop_reason(state: dict[str, Any], now: float) -> str:
    if not state["pending"]:
        return "drained"
    if state["single_shot"] and state["rounds"] >= 1:
        # An operator-supplied `max_items` is a one-batch smoke run: the reason it
        # stopped is the limit, whatever the round happened to settle.
        return "limited"
    history = state.get("by_round") or []
    if history and not history[-1].get("accepted"):
        # Decided before the room-based reasons: a round that settled nothing is
        # a verdict about the rows, not about how much room is left, and only the
        # room-based reasons may trigger another whole pipeline run.
        return "no_progress"
    if int(state["budget_seconds"]) > 0 and now - float(state["started_at"]) >= int(
        state["budget_seconds"]
    ):
        return "budget"
    if state["rounds"] >= int(state["max_rounds"]):
        return "max_rounds"
    return ""


def plan(args: argparse.Namespace) -> int:
    queue = read_jsonl(args.queue)
    state = load_state(args.state)
    if not state:
        state = new_state(
            queue,
            max_rounds=args.max_rounds,
            max_attempts=args.max_attempts,
            budget_seconds=args.budget_seconds,
            single_shot=args.single_shot,
        )
        save_state(args.state, state)

    reason = stop_reason(state, time.time())
    round_number = int(state["rounds"]) + 1
    decision = {
        "run": 0 if reason else 1,
        "stopped": reason,
        "round": round_number if not reason else state["rounds"],
        "pending": len(state["pending"]),
        "blocked": len(state["blocked"]),
    }
    if args.plan_json:
        args.plan_json.parent.mkdir(parents=True, exist_ok=True)
        args.plan_json.write_text(
            json.dumps(decision, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if reason:
        # The reason is part of the state so ``finish`` reports the reason the
        # loop actually stopped for, not a guess made after the fact.
        state["stopped"] = reason
        save_state(args.state, state)
        write_jsonl(args.round_output, [])
        print(
            f"translation rounds: stop after {state['rounds']} round(s) "
            f"({reason}); pending={len(state['pending'])} blocked={len(state['blocked'])}"
        )
        emit_outputs(
            args.github_output,
            {
                "run": "0",
                "stopped": reason,
                "round": state["rounds"],
                "pending": len(state["pending"]),
                "blocked": len(state["blocked"]),
            },
        )
        return 0

    pending = set(state["pending"])
    round_rows = [row for row in queue if source_id(row) in pending]
    write_jsonl(args.round_output, round_rows)
    budget = int(state["budget_seconds"])
    remaining = (
        "no time cap" if budget <= 0 else f"{budget - int(time.time() - state['started_at'])}s of budget left"
    )
    print(
        f"translation rounds: round {round_number}/{state['max_rounds']}, "
        f"{len(round_rows)} row(s) pending, {remaining}"
    )
    emit_outputs(
        args.github_output,
        {
            "run": "1",
            "stopped": "",
            "round": round_number,
            "pending_before": len(round_rows),
            "pending": len(state["pending"]),
            "blocked": len(state["blocked"]),
        },
    )
    return 0


def record(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    if not state:
        print("translation rounds: no state file to record into", file=sys.stderr)
        return 1

    accepted_rows = read_jsonl(args.draft)
    accepted_ids = [source_id(row) for row in accepted_rows if source_id(row)]
    failed_rows = read_jsonl(args.failed)

    attempts: dict[str, int] = {str(k): int(v) for k, v in (state.get("attempts") or {}).items()}
    errors: dict[str, list[str]] = {
        str(k): list(v) for k, v in (state.get("errors") or {}).items() if isinstance(v, list)
    }
    blocked = list(state.get("blocked") or [])

    for row in failed_rows:
        sid = source_id(row)
        if not sid:
            continue
        attempts[sid] = attempts.get(sid, 0) + 1
        raw_errors = row.get("errors")
        if isinstance(raw_errors, list) and raw_errors:
            errors[sid] = [str(item) for item in raw_errors]
        if attempts[sid] >= int(state["max_attempts"]) and sid not in blocked:
            blocked.append(sid)

    retryable = [sid for sid in state["pending"] if sid not in accepted_ids and sid not in blocked]
    state["attempts"] = attempts
    state["errors"] = errors
    state["blocked"] = sorted(set(blocked))
    state["pending"] = retryable
    accepted_all = list(state.get("accepted") or []) + accepted_ids
    state["accepted"] = accepted_all
    state["rounds"] = int(state["rounds"]) + 1
    by_round = list(state.get("by_round") or [])
    by_round.append(
        {
            "round": state["rounds"],
            "accepted": len(accepted_ids),
            "failed": len(failed_rows),
            "elapsed_seconds": round(float(args.elapsed), 3),
            "pending_after": len(retryable),
            "blocked_total": len(state["blocked"]),
        }
    )
    state["by_round"] = by_round
    save_state(args.state, state)

    print(
        f"translation rounds: round {state['rounds']} accepted {len(accepted_ids)}, "
        f"failed {len(failed_rows)}, pending {len(retryable)}, "
        f"blocked {len(state['blocked'])}"
    )
    emit_outputs(
        args.github_output,
        {
            "round": state["rounds"],
            "accepted_round": len(accepted_ids),
            "pending": len(retryable),
            "blocked": len(state["blocked"]),
        },
    )
    return 0


def _merge_numeric(target: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, dict):
            current = target.get(key)
            target[key] = _merge_numeric(current if isinstance(current, dict) else {}, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            current = target.get(key)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                target[key] = current + value
            else:
                target[key] = value
        else:
            target[key] = value
    return target


def finish(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    if not state:
        print("translation rounds: no state file to finish", file=sys.stderr)
        return 1

    queue = read_jsonl(args.queue)
    order: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in queue:
        sid = source_id(row)
        if not sid or sid in by_id:
            continue
        by_id[sid] = row
        order.append(sid)

    accepted_ids = set(state.get("accepted") or [])
    unresolved = [sid for sid in order if sid not in accepted_ids]
    errors = state.get("errors") or {}
    failed_rows = [
        {
            "source_sha256": sid,
            "source": str(by_id[sid].get("source", "")),
            "errors": list(errors.get(sid, [])),
        }
        for sid in unresolved
    ]
    write_jsonl(args.failed_out, failed_rows)

    reason = state.get("stopped") or stop_reason(state, time.time())
    merged: dict[str, Any] = {}
    for round_summary in args.round_summary or []:
        path = Path(round_summary)
        # The pool's own summary is the same shape this module loads, and a
        # missing one costs the provider-health section, never the verdict.
        payload = load_state(path) if path.is_file() else {}
        models = payload.get("models")
        if isinstance(models, dict):
            _merge_numeric(merged, models)

    elapsed = sum(
        float(entry.get("elapsed_seconds") or 0.0) for entry in (state.get("by_round") or [])
    )
    summary = {
        "schema_version": 2,
        "rounds": state["rounds"],
        # A loop that ended without a decision (only reachable if a caller runs
        # `finish` outside the loop) must not claim to have run out of room.
        "stopped": reason or "incomplete",
        "max_rounds": state["max_rounds"],
        "max_attempts": state["max_attempts"],
        "scanned_rows": state.get("queue_size", 0),
        "selected_pending": state.get("queue_size", 0),
        "accepted": len(accepted_ids),
        "failed": len(failed_rows),
        "blocked": len(state.get("blocked") or []),
        "remaining": len(state.get("pending") or []),
        "attempts": sum(int(value) for value in (state.get("attempts") or {}).values()),
        "elapsed_seconds": round(elapsed, 3),
        "by_round": state.get("by_round") or [],
        "models": merged,
    }
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"translation rounds: {summary['rounds']} round(s), stopped={summary['stopped']}, "
        f"accepted={summary['accepted']} unresolved={summary['failed']} "
        f"attempts={summary['attempts']} elapsed={summary['elapsed_seconds']}s"
    )
    emit_outputs(
        args.github_output,
        {
            "stopped": summary["stopped"],
            "rounds": summary["rounds"],
            "accepted": summary["accepted"],
            "unresolved": summary["failed"],
        },
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser("plan", help="decide the next round and write its input")
    plan_parser.add_argument("--queue", type=Path, required=True)
    plan_parser.add_argument("--state", type=Path, required=True)
    plan_parser.add_argument("--round-output", type=Path, required=True)
    plan_parser.add_argument("--plan-json", type=Path, default=None)
    plan_parser.add_argument("--github-output", type=Path, default=None)
    plan_parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    plan_parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    plan_parser.add_argument(
        "--budget-seconds",
        type=int,
        default=DEFAULT_BUDGET_SECONDS,
        help="wall-clock cap for the whole loop; 0 (the default) means no cap",
    )
    plan_parser.add_argument(
        "--single-shot",
        action="store_true",
        help="honour an operator --max-items limit by running exactly one round",
    )
    plan_parser.set_defaults(func=plan)

    record_parser = sub.add_parser("record", help="fold one round's reports into the state")
    record_parser.add_argument("--state", type=Path, required=True)
    record_parser.add_argument("--draft", type=Path, required=True)
    record_parser.add_argument("--failed", type=Path, required=True)
    record_parser.add_argument("--elapsed", type=float, default=0.0)
    record_parser.add_argument("--github-output", type=Path, default=None)
    record_parser.set_defaults(func=record)

    finish_parser = sub.add_parser("finish", help="write the aggregate summary and failure report")
    finish_parser.add_argument("--state", type=Path, required=True)
    finish_parser.add_argument("--queue", type=Path, required=True)
    finish_parser.add_argument("--failed-out", type=Path, required=True)
    finish_parser.add_argument("--summary-out", type=Path, required=True)
    finish_parser.add_argument(
        "--round-summary",
        type=Path,
        action="append",
        default=[],
        help="per-round pool summary to merge for provider health reporting",
    )
    finish_parser.add_argument("--github-output", type=Path, default=None)
    finish_parser.set_defaults(func=finish)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
