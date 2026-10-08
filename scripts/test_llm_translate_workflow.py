#!/usr/bin/env python3
"""``llm-translate-assets.yml`` 的离线回归契约。

2026-10-01 ~ 10-08 的实际故障：翻译步骤只要有一个条目耗尽 provider 重试就以退出码 2
结束，而 ``Apply``/``Commit`` 步骤依赖默认的 ``success()``，于是每次真正干活的定时运行
都把已经翻好的 ~1.3k 行连同草稿一起丢掉（控制台只留下 exit code 2，失败条目本身也没
上传 artifact，连续一周没人发现）。

本套测试冻结修复后的边界：

1. 翻译步骤容忍部分失败（``continue-on-error``），应用/提交步骤显式承接它；
2. 但应用/提交仍然不会在**前置步骤真失败**或**校验失败**后执行（不能绕过 validate_repo）；
3. 诊断文件每次运行都上传，真实故障才登记 Issue；
4. 增量下载的 memo（``manifests/official-bundle-index.json``）与翻译结果同一次提交发布。

只做 YAML 结构断言：不访问网络、不触发任何工作流。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "llm-translate-assets.yml"


class LlmTranslateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)
        cls.job = cls.workflow["jobs"]["draft"]
        cls.steps = {step.get("name"): step for step in cls.job["steps"]}

    def test_partial_item_failures_do_not_discard_accepted_rows(self):
        translate = self.steps["Translate with the existing provider pool"]
        self.assertEqual(translate["id"], "translate")
        self.assertIs(translate["continue-on-error"], True)
        apply_step = self.steps["Apply LLM translations"]
        self.assertEqual(apply_step["id"], "apply")
        self.assertIn("steps.translate.outcome == 'failure'", apply_step["if"])
        commit = self.steps["Commit LLM translations to main"]
        self.assertIn("steps.apply.outcome == 'success'", commit["if"])

    def test_commit_still_runs_after_a_skipped_or_failed_neighbour(self):
        """The tolerated translate failure must not leave the commit unguarded."""
        commit = self.steps["Commit LLM translations to main"]
        self.assertNotEqual(commit.get("if"), "always()")
        self.assertNotEqual(commit.get("continue-on-error"), True)
        apply_step = self.steps["Apply LLM translations"]
        self.assertNotEqual(apply_step.get("if"), "always()")
        self.assertIn("python scripts/validate_repo.py", apply_step["run"])

    def test_diagnostics_are_always_uploaded(self):
        upload = self.steps["Upload translation diagnostics"]
        self.assertEqual(upload["if"], "always()")
        self.assertTrue(upload["uses"].startswith("actions/upload-artifact@"))
        self.assertIs(upload["with"]["include-hidden-files"], True)
        for name in (".llm-summary.json", ".llm-failed.jsonl", ".llm-draft.jsonl", ".llm-queue.jsonl"):
            self.assertIn(name, upload["with"]["path"])

    def test_only_real_breakage_fails_the_run_and_reports_an_issue(self):
        summary = self.steps["Summarize translation outcome"]["run"]
        self.assertIn('"$accepted" -eq 0', summary)          # nothing accepted -> red
        self.assertIn("::warning", summary)                  # partial failure -> warning only
        report = self.steps["Report a real failure as a tracking issue"]
        self.assertEqual(report["if"], "failure()")
        self.assertIn("issues: write", self.text)
        self.assertIn("gh issue", report["run"])

    def test_incremental_bundle_index_is_wired_and_published(self):
        extract = self.steps["Extract new source rows from official latest assets"]["run"]
        self.assertIn("--bundle-index manifests/official-bundle-index.json", extract)
        self.assertIn("--full-rescan", extract)
        # PyYAML resolves the bare `on:` key to boolean True.
        triggers = self.workflow["on"] if "on" in self.workflow else self.workflow[True]
        self.assertIn("full_rescan", triggers["workflow_dispatch"]["inputs"])
        commit = self.steps["Commit LLM translations to main"]["run"]
        self.assertIn("manifests/official-bundle-index.json", commit)
        self.assertIn("git add", commit)

    def test_job_cannot_hang_forever(self):
        self.assertEqual(self.job["timeout-minutes"], 180)


if __name__ == "__main__":
    unittest.main()
