#!/usr/bin/env python3
"""``llm_translation_rounds.py`` 的离线回归契约。

2026-10-27 的实际浪费：资产 1077950 有三行没翻上，而唯一的补救办法是在
工作流层面整条重派，于是三行文本换来三轮完整的打包 + 部署（约 48 分钟）。
本套测试冻结新的边界：

1. 同一个 job 内可以多轮重试，直到队列清空、尝试预算用完、或一圈没有任何
   进展——后者必须立刻停，否则遇到真翻不出来的句子会空转到超时；
2. 重试输入必须从**原始队列行**重建（``.llm-failed.jsonl`` 只有
   ``source_sha256``/``source``/``errors``，缺 ``bundle``/``key``/``task``）；
3. 停下的原因要能被调用方读懂，因为只有 ``budget`` 和 ``max_rounds``
   值得再派一次整条流水线；
4. 诊断产物的形状不变：``.llm-summary.json`` 仍带 ``accepted``/``failed``。

只做纯文件读写断言：不访问网络、不调用 provider。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import llm_translation_rounds as rounds


def queue_row(index: int, **overrides) -> dict:
    row = {
        "source": f"原文{index}",
        "source_sha256": f"sha{index:04d}",
        "bundle": f"bundle{index}.unity3d",
        "key": f"commands[{index}].text",
        "task": "ASSETS_TEXT",
        "asset_version": "1077950",
        "source_client_version": "9.0.200",
        "translation": "",
        "status": "pending",
        "repair_reason": None,
    }
    row.update(overrides)
    return row


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run(argv: list[str]) -> int:
    with redirect_stdout(io.StringIO()):
        return rounds.main(argv)


class TranslationRoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.queue = self.root / ".llm-queue.jsonl"
        self.state = self.root / ".llm-loop-state.json"
        self.round_input = self.root / ".llm-round-queue.jsonl"
        self.draft = self.root / ".llm-round-draft.jsonl"
        self.failed = self.root / ".llm-round-failed.jsonl"
        self.outputs = self.root / "github-output"
        write_jsonl(self.queue, [queue_row(1), queue_row(2), queue_row(3)])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def plan(self, *extra: str) -> dict[str, str]:
        self.outputs.write_text("", encoding="utf-8")
        code = run(
            [
                "plan",
                "--queue",
                str(self.queue),
                "--state",
                str(self.state),
                "--round-output",
                str(self.round_input),
                "--github-output",
                str(self.outputs),
                *extra,
            ]
        )
        self.assertEqual(code, 0)
        return dict(
            line.split("=", 1)
            for line in self.outputs.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )

    def record(self, accepted: list[dict], failed: list[dict], elapsed: float = 1.0) -> dict[str, str]:
        write_jsonl(self.draft, accepted)
        write_jsonl(self.failed, failed)
        self.outputs.write_text("", encoding="utf-8")
        code = run(
            [
                "record",
                "--state",
                str(self.state),
                "--draft",
                str(self.draft),
                "--failed",
                str(self.failed),
                "--elapsed",
                str(elapsed),
                "--github-output",
                str(self.outputs),
            ]
        )
        self.assertEqual(code, 0)
        return dict(
            line.split("=", 1)
            for line in self.outputs.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )

    def age_state(self, seconds: float) -> None:
        """Backdate the loop start so a time cap can be exercised without waiting."""
        import time as _time

        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["started_at"] = _time.time() - seconds
        self.state.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    # -- plan ------------------------------------------------------------
    def test_first_plan_offers_the_whole_queue(self):
        outputs = self.plan()
        self.assertEqual(outputs["run"], "1")
        self.assertEqual(outputs["round"], "1")
        self.assertEqual(len(read_jsonl(self.round_input)), 3)

    def test_round_input_keeps_the_original_queue_shape(self):
        # The pool needs bundle/key/task; the failure report does not carry them,
        # so a retry built from `.llm-failed.jsonl` would translate blind.
        self.plan()
        self.record(
            [queue_row(1)],
            [{"source_sha256": "sha0002", "source": "原文2", "errors": ["x"]}],
        )
        self.plan()
        rows = read_jsonl(self.round_input)
        self.assertEqual([row["source_sha256"] for row in rows], ["sha0002", "sha0003"])
        self.assertEqual(rows[0]["bundle"], "bundle2.unity3d")
        self.assertEqual(rows[0]["key"], "commands[2].text")

    def test_accepted_rows_leave_the_pending_set(self):
        self.plan()
        outputs = self.record(
            [queue_row(1)],
            [{"source_sha256": "sha0002", "source": "原文2", "errors": ["a"]}],
        )
        self.assertEqual(outputs["accepted_round"], "1")
        self.assertEqual(outputs["pending"], "2")
        self.plan()
        self.assertEqual(
            [row["source_sha256"] for row in read_jsonl(self.round_input)],
            ["sha0002", "sha0003"],
        )

    def test_a_row_is_blocked_once_its_attempt_budget_is_spent(self):
        self.plan("--max-attempts", "2")
        self.record(
            [queue_row(3)],
            [{"source_sha256": "sha0001", "source": "原文1", "errors": ["first"]}],
        )
        self.plan("--max-attempts", "2")
        outputs = self.record(
            [queue_row(2)],
            [{"source_sha256": "sha0001", "source": "原文1", "errors": ["second"]}],
        )
        self.assertEqual(outputs["blocked"], "1")
        self.assertEqual(outputs["pending"], "0")
        # Blocked rows leave the queue, so the loop reports a clean stop; the
        # per-row give-up is still visible through the summary's blocked count.
        outputs = self.plan("--max-attempts", "2")
        self.assertEqual(outputs["run"], "0")
        self.assertEqual(outputs["stopped"], "drained")
        self.assertEqual(outputs["blocked"], "1")

    def test_a_round_without_progress_stops_the_loop(self):
        self.plan()
        self.record(
            [],
            [
                {"source_sha256": "sha0001", "source": "原文1", "errors": ["a"]},
                {"source_sha256": "sha0002", "source": "原文2", "errors": ["b"]},
            ],
        )
        outputs = self.plan()
        self.assertEqual(outputs["run"], "0")
        self.assertEqual(outputs["stopped"], "no_progress")

    def test_a_time_cap_is_opt_in_and_honoured_when_asked_for(self):
        """The loop has no time cap unless a caller explicitly asks for one."""
        self.plan("--budget-seconds", "60")
        self.age_state(seconds=120)
        outputs = self.plan()
        self.assertEqual(outputs["run"], "0")
        self.assertEqual(outputs["stopped"], "budget")

    def test_no_time_cap_by_default(self):
        """Ageing the loop must not stop it when nobody set a cap.

        Before 2026-10-27 another whole pipeline run was the only recovery for a
        refused row, so minutes were the scarce resource and a cap was the lesser
        evil.  With the retry inside the job the scarce resource is rounds, and a
        cap would only truncate work that is already paid for.
        """
        self.plan()
        self.age_state(seconds=86400)
        outputs = self.plan()
        self.assertEqual(outputs["run"], "1")
        self.assertEqual(outputs["stopped"], "")

    def test_max_rounds_is_its_own_reason(self):
        self.plan("--max-rounds", "1")
        self.record([queue_row(1)], [])
        outputs = self.plan("--max-rounds", "1")
        self.assertEqual(outputs["run"], "0")
        self.assertEqual(outputs["stopped"], "max_rounds")

    def test_single_shot_runs_exactly_one_round(self):
        self.plan("--single-shot")
        self.record([], [{"source_sha256": "sha0001", "source": "原文1", "errors": ["a"]}])
        outputs = self.plan("--single-shot")
        self.assertEqual(outputs["run"], "0")
        self.assertEqual(outputs["stopped"], "limited")

    def test_a_fruitless_last_round_outranks_a_spent_round_count(self):
        """The reason decides whether the caller rebuilds and redeploys.

        A loop that settles one row per round and then hits the round limit has
        earned another run.  A loop whose last round settled nothing has not: the
        same rows would be sampled again for the same verdicts, and each such run
        rebuilds the release and redeploys the gateway.  Reporting that case as
        `max_rounds` would do exactly that, once per scheduled run, forever.
        """
        self.plan("--max-rounds", "2")
        # Round one moves: this is a loop that is still making progress.
        self.record([queue_row(1)], [{"source_sha256": "sha0002", "source": "原文2", "errors": ["a"]}])
        self.assertEqual(self.plan("--max-rounds", "2")["run"], "1")
        # Round two moves nothing, and it is also the last round available.
        self.record([], [{"source_sha256": "sha0002", "source": "原文2", "errors": ["a"]}])
        outputs = self.plan("--max-rounds", "2")
        self.assertEqual(outputs["run"], "0")
        self.assertEqual(outputs["stopped"], "no_progress")

    # -- finish ----------------------------------------------------------
    def test_finish_reports_unresolved_rows_and_merges_round_summaries(self):
        round_summary = self.root / ".llm-round-summary.json"
        round_summary.write_text(
            json.dumps({"models": {"imas-trans": {"requests": 4, "accepted": 1}}}),
            encoding="utf-8",
        )
        stubborn = [
            {"source_sha256": "sha0002", "source": "原文2", "errors": ["imas-trans:blocking_qa:numeric_literal_mismatch"]},
            {"source_sha256": "sha0003", "source": "原文3", "errors": ["imas-trans:blocking_qa:honorific_chan_jiang_calque"]},
        ]
        self.plan()
        self.record([queue_row(1)], stubborn, elapsed=2.5)
        # Round two retries exactly the two leftovers and moves nothing, which is
        # the signal that a further identical round is not worth its API time.
        self.plan()
        self.record([], stubborn, elapsed=1.5)
        # The loop always re-plans before it exits, which is what records the
        # reason the run stopped for.
        self.assertEqual(self.plan()["stopped"], "no_progress")
        failed_out = self.root / ".llm-failed.jsonl"
        summary_out = self.root / ".llm-summary.json"
        self.outputs.write_text("", encoding="utf-8")
        code = run(
            [
                "finish",
                "--state",
                str(self.state),
                "--queue",
                str(self.queue),
                "--failed-out",
                str(failed_out),
                "--summary-out",
                str(summary_out),
                "--round-summary",
                str(round_summary),
                "--github-output",
                str(self.outputs),
            ]
        )
        self.assertEqual(code, 0)
        unresolved = read_jsonl(failed_out)
        self.assertEqual([row["source_sha256"] for row in unresolved], ["sha0002", "sha0003"])
        self.assertEqual(
            unresolved[0]["errors"], ["imas-trans:blocking_qa:numeric_literal_mismatch"]
        )
        summary = json.loads(summary_out.read_text(encoding="utf-8"))
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["rounds"], 2)
        self.assertEqual(summary["stopped"], "no_progress")
        self.assertEqual(summary["models"]["imas-trans"]["requests"], 4)
        self.assertEqual(summary["by_round"][0]["elapsed_seconds"], 2.5)
        self.assertEqual(summary["by_round"][1]["elapsed_seconds"], 1.5)

    def test_finish_reports_a_clean_drain_as_drained(self):
        self.plan()
        self.record([queue_row(1), queue_row(2), queue_row(3)], [])
        failed_out = self.root / ".llm-failed.jsonl"
        summary_out = self.root / ".llm-summary.json"
        code = run(
            [
                "finish",
                "--state",
                str(self.state),
                "--queue",
                str(self.queue),
                "--failed-out",
                str(failed_out),
                "--summary-out",
                str(summary_out),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(read_jsonl(failed_out), [])
        summary = json.loads(summary_out.read_text(encoding="utf-8"))
        self.assertEqual(summary["stopped"], "drained")
        self.assertEqual(summary["accepted"], 3)
        self.assertEqual(summary["failed"], 0)

    def test_finish_without_a_decision_does_not_claim_to_have_run_out_of_room(self):
        """Only a loop that really ran out of room may ask for another run."""
        self.plan()
        self.record([queue_row(1)], [{"source_sha256": "sha0002", "source": "原文2", "errors": ["a"]}])
        summary_out = self.root / ".llm-summary.json"
        # No re-plan here: the state never recorded why the loop ended.
        code = run(
            [
                "finish",
                "--state",
                str(self.state),
                "--queue",
                str(self.queue),
                "--failed-out",
                str(self.root / ".llm-failed.jsonl"),
                "--summary-out",
                str(summary_out),
            ]
        )
        self.assertEqual(code, 0)
        summary = json.loads(summary_out.read_text(encoding="utf-8"))
        self.assertEqual(summary["stopped"], "incomplete")
        self.assertEqual(summary["failed"], 2)


if __name__ == "__main__":
    unittest.main()
