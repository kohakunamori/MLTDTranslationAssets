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

import tempfile
import unittest
from pathlib import Path

import yaml

import commit_llm_drafts

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "llm-translate-assets.yml"


def staged_paths_with(**trees) -> list[str]:
    """What the commit step would stage for a repository holding ``trees``.

    The step itself is now one call to ``scripts/commit_llm_drafts.py``, so the
    paths it covers are asserted against that script rather than against bash.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in trees:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        return commit_llm_drafts.stage_paths(root)


class LlmTranslateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)
        cls.job = cls.workflow["jobs"]["draft"]
        cls.prepare = cls.workflow["jobs"]["prepare"]
        cls.steps = {step.get("name"): step for step in cls.prepare["steps"] + cls.job["steps"]}

    def test_official_bundles_never_reach_the_translation_job(self):
        checkout = self.job['steps'][0]['with']
        self.assertIn('!/image-overlay/', checkout['sparse-checkout'])
        self.assertEqual(self.prepare['runs-on'], 'ubuntu-latest')
        # Both jobs use standard GitHub-hosted runners. The bulk trees stay
        # out of the checkout above, and the translation job only ever
        # receives the bounded text handoff.
        self.assertEqual(self.job['runs-on'], 'ubuntu-latest')
        self.assertEqual(self.job['needs'], 'prepare')
        prepare_names = [step.get('name') for step in self.prepare['steps']]
        draft_names = [step.get('name') for step in self.job['steps']]
        self.assertIn('Extract event-unit dialogue', prepare_names)
        self.assertNotIn('Extract event-unit dialogue', draft_names)
        verify = self.steps['Verify and materialize synchronized text sources']['run']
        self.assertIn('--sha256 "$HANDOFF_SHA256"', verify)
        save = self.steps['Save exact-run text handoff']['with']
        restore = self.steps['Restore exact-run text handoff']['with']
        self.assertEqual(save['key'], restore['key'])
        self.assertEqual(save['path'], '.mltd-text-handoff')
        self.assertEqual(restore['path'], save['path'])
        self.assertNotIn('restore-keys', restore)
        self.assertIs(restore['fail-on-cache-miss'], True)

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
        self.assertIn('"$accepted" -eq 0', summary)          # nothing accepted ...
        self.assertIn('"$failed" -ge 50', summary)           # ... on a real queue -> red
        self.assertIn('queue=$(wc -l < .llm-queue.jsonl)', summary)
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
        self.assertIn("full_rescan", triggers["workflow_call"]["inputs"])
        commit = self.steps["Commit LLM translations to main"]["run"]
        self.assertIn("python scripts/commit_llm_drafts.py", commit)
        self.assertIn("manifests/official-bundle-index.json",
                      staged_paths_with(**{"manifests/official-bundle-index.json": None,
                                           "manifests/asset-version.json": None}))

    def test_job_cannot_hang_forever(self):
        self.assertEqual(self.job["timeout-minutes"], 180)

    def test_same_version_and_full_rescan_always_synchronize(self):
        run = self.steps["Extract new source rows from official latest assets"]["run"]
        self.assertNotIn('if [ "$has_update"', run)
        self.assertIn("inputs.full_rescan", run)
        self.assertIn('python "${args[@]}"', run)
        names = [step.get("name") for step in self.prepare["steps"] + self.job["steps"]]
        self.assertLess(names.index("Extract event-unit dialogue"), names.index("Discover new localizable bundles"))
        self.assertLess(names.index("Verify source synchronization and detect image coverage"), names.index("Collect untranslated rows"))
        self.assertIn("--require-source-sync", self.steps["Verify source synchronization and detect image coverage"]["run"])

    def test_backfill_is_bounded_and_continues_only_after_real_progress(self):
        self.assertIn('then limit=500; fi', self.steps["Translate with the existing provider pool"]["run"])
        parent = yaml.safe_load((WORKFLOW.parent / 'auto-track-jp-assets.yml').read_text(encoding='utf-8'))
        followup = parent["jobs"]["continue-backlog"]
        self.assertIn("needs.translate.outputs.updated != '0'", followup["if"])
        self.assertIn("needs.translate.outputs.remaining != '0'", followup["if"])
        self.assertIn("inputs.max_items == '0'", followup["if"])
        self.assertEqual(self.steps["Refresh translation backlog report"]["id"], "backlog")
        self.assertIn(".llm-apply.json", self.steps["Apply LLM translations"]["run"])

    def test_new_bundles_are_discovered_and_lyrics_extracted_before_translation(self):
        """The steps that used to be missing, and their order.

        The refresh can only re-verify tracked bundles, so a brand-new text or
        lyric bundle was never downloaded.  Discovery must therefore come after
        the refresh (it reads the index the refresh downloads) and before the
        queue is collected, and lyric extraction must sit in the same window.
        """
        names = [step.get("name") for step in self.prepare["steps"] + self.job["steps"]]
        refresh = names.index("Extract new source rows from official latest assets")
        discover = names.index("Discover new localizable bundles")
        lyrics = names.index("Extract new song lyrics")
        collect = names.index("Collect untranslated rows")
        self.assertLess(refresh, discover)
        self.assertLess(discover, collect)
        self.assertLess(lyrics, collect)
        discovery_run = self.steps["Discover new localizable bundles"]["run"]
        self.assertIn("scripts/discover_official_bundles.py", discovery_run)
        self.assertIn("--report .llm-discovery.json", discovery_run)
        # A missing index is a synchronization failure, even at the same version.
        self.assertIn('test -f "$index"', discovery_run)
        lyrics_run = self.steps["Extract new song lyrics"]["run"]
        self.assertIn("scripts/refresh_lyrics_catalogue.py", lyrics_run)
        self.assertIn('test -f "$index"', lyrics_run)

    def test_the_queue_and_the_commit_cover_the_lyric_source_tree(self):
        collect = self.steps["Collect untranslated rows"]["run"]
        self.assertIn("--scope all", collect)
        apply_step = self.steps["Apply LLM translations"]["run"]
        self.assertIn("--scope all", apply_step)
        publish = self.steps["Publish machine drafts for the generated build"]["run"]
        self.assertIn("publish --scope lyrics", publish)
        commit = self.steps["Commit LLM translations to main"]["run"]
        self.assertIn("python scripts/commit_llm_drafts.py", commit)
        covered = staged_paths_with(**{"lyrics/songs/x.jsonl": None,
                                       "manifests/asset-version.json": None,
                                       "manifests/official-asset-inventory.json": None})
        self.assertIn("lyrics", covered)
        self.assertIn("manifests/official-asset-inventory.json", covered)
        upload = self.steps["Upload translation diagnostics"]["with"]["path"]
        for name in (".llm-discovery.json", ".llm-lyrics.json", ".llm-publish-lyrics.json"):
            self.assertIn(name, upload)

    def test_discovery_and_lyric_extraction_are_reported_in_the_summary(self):
        summary = self.steps["Summarize translation outcome"]["run"]
        self.assertIn(".llm-discovery-summary.json", summary)
        self.assertIn(".llm-lyrics.json", summary)

    def test_machine_drafts_are_admitted_by_one_explicit_step(self):
        """C: machine output reaches generated/<ver>/ only through `publish`."""
        names = [step.get("name") for step in self.prepare["steps"] + self.job["steps"]]
        self.assertLess(names.index("Apply LLM translations"), names.index("Publish machine drafts for the generated build"))
        self.assertLess(names.index("Publish machine drafts for the generated build"), names.index("Commit LLM translations to main"))
        publish = self.steps["Publish machine drafts for the generated build"]
        self.assertEqual(publish["id"], "publish")
        self.assertIn("scripts/llm_translate_untranslated.py publish", publish["run"])
        self.assertIn("--dry-run", publish["run"])
        # Promotion flips rows to accepted, which turns on the accepted-row
        # token gate; the repository must be revalidated before the commit.
        self.assertIn("python scripts/validate_repo.py", publish["run"])
        commit = self.steps["Commit LLM translations to main"]
        self.assertIn("steps.publish.outcome != 'failure'", commit["if"])

    def test_the_generated_build_is_dispatched_explicitly(self):
        """A GITHUB_TOKEN push never starts assets-generated.yml by itself.

        Either commit can be the one that published: the apply step now pushes the
        pending rows as soon as they validate, so on a healthy run the promotion
        step has nothing left to push and only the apply step reports a publish.
        """
        package = self.workflow["jobs"]["package"]
        self.assertEqual(package["needs"], ["draft", "images"])
        self.assertIn("needs.draft.outputs.published == 'true'", package['outputs']['published'])
        self.assertIn("needs.images.outputs.published == 'true'", package['outputs']['published'])
        parent = yaml.safe_load((WORKFLOW.parent / 'auto-track-jp-assets.yml').read_text(encoding='utf-8'))
        self.assertEqual(parent['jobs']['release']['uses'], './.github/workflows/assets-generated.yml')
        self.assertIn("needs.translate.outputs.published == 'true'", parent['jobs']['release']['if'])
        self.assertIn("published=true", Path(commit_llm_drafts.__file__).read_text(encoding="utf-8"))
        self.assertIn("actions: write", self.text)

    def test_publishing_can_be_switched_off_without_editing_code(self):
        publish = self.steps["Publish machine drafts for the generated build"]
        self.assertIn("inputs.publish_drafts != 'false'", publish["if"])
        self.assertIn("vars.MLTD_PUBLISH_LLM_DRAFTS != 'false'", publish["if"])
        triggers = self.workflow["on"] if "on" in self.workflow else self.workflow[True]
        self.assertIn("publish_drafts", triggers["workflow_call"]["inputs"])

    def test_publish_evidence_is_kept_with_the_diagnostics(self):
        upload = self.steps["Upload translation diagnostics"]["with"]["path"]
        self.assertIn(".llm-publish.json", upload)
        summary = self.steps["Summarize translation outcome"]["run"]
        self.assertIn(".llm-publish.json", summary)

    def test_a_lyric_line_that_already_has_an_approved_twin_is_copied_not_translated(self):
        """The copy runs before the collector, so the paid queue never sees it.

        The 2026-10-12 runs #26 and #27 spent tokens on five rows whose Japanese
        source contains a line break, got the same two-line draft back both times
        (which the lyric validator always refuses), and left the rows untranslated
        -- while the approved Chinese for four of them was already in the library
        under another bundle.
        """
        names = [step.get("name") for step in self.prepare["steps"] + self.job["steps"]]
        reuse = names.index("Reuse approved lyric translations")
        extract = names.index("Extract new song lyrics")
        collect = names.index("Collect untranslated rows")
        self.assertLess(extract, reuse, "a new song's rows must exist before they can be matched")
        self.assertLess(reuse, collect, "otherwise the collector pays for a line that is already approved")
        run = self.steps["Reuse approved lyric translations"]["run"]
        self.assertIn("scripts/llm_translate_untranslated.py reuse --scope lyrics", run)
        upload = self.steps["Upload translation diagnostics"]["with"]["path"]
        self.assertIn(".llm-reuse.json", upload)


if __name__ == "__main__":
    unittest.main()
