"""The checkout contract of ``.github/workflows/validate-localization.yml``.

``generated/`` (546 MB) and ``images/`` (333 MB) are 880 MB of this repository's
1.09 GB working copy, and neither job in that workflow opens them.  The checkout
asks for the other top-level directories by name and skips those two, which is
only worth anything together with ``filter: blob:none``: a sparse checkout on a
full clone still transfers every blob and saves nothing but the write.

The two jobs do not need the same tree.  ``validate`` runs ``validate_repo.py``,
which walks ``locales/`` and ``lyrics/``; the suites in ``regression`` build
their own fixtures in temp directories and read none of it, so that job drops
both trees (210 MB -> 11 MB).  A directory silently dropped from either list is
a failed run at best and a check that quietly stopped looking at something at
worst, so the split is pinned here: the test job may only carry a subset of the
validator's list, and both keep the partial-clone filter.  How the test job
installs its dependencies (uv, cached) and how it spreads the suite over the
runner's four vCPUs (worksteal, not loadfile) are pinned for the same reason:
each was measured, and each is one edit away from silently going back.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validate-localization.yml"
REQUIREMENTS = ROOT / "requirements-ci.txt"

#: Trees neither job checks out.
EXCLUDED_TREES = ("generated", "images")

#: Trees only the validator walks, via ``validate_repo.py``.
VALIDATOR_ONLY_TREES = ("locales", "lyrics")

#: Directories the offline suites import modules from.
SUITE_DIRS = (".github", "asset-server", "manifests", "pipelines", "scripts", "server")

#: Packages both jobs' suites cannot import without.
REQUIRED_PACKAGES = {
    "pytest", "pytest-xdist", "pyyaml", "msgpack", "UnityPy", "Pillow",
    "pycryptodome", "jsonschema", "requests",
}


class SparseCheckoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.jobs = cls.workflow["jobs"]

    def step(self, job: str, name: str) -> dict:
        return next(s for s in self.jobs[job]["steps"] if s.get("name") == name)

    def patterns(self, job: str) -> list[str]:
        value = self.step(job, "Checkout repository")["with"]["sparse-checkout"]
        return [line.strip() for line in value.splitlines() if line.strip()]

    def test_the_test_job_checks_out_only_a_subset_of_the_validator(self):
        """A narrower list must never grow a tree the other job does not fetch."""
        extra = set(self.patterns("regression")) - set(self.patterns("validate"))
        self.assertFalse(extra, f"regression checks out {sorted(extra)} alone")

    def test_the_validator_keeps_the_trees_it_walks(self):
        for needed in VALIDATOR_ONLY_TREES:
            with self.subTest(needed):
                self.assertIn(needed, self.patterns("validate"))

    def test_the_test_job_leaves_the_locale_trees_behind(self):
        for not_needed in VALIDATOR_ONLY_TREES:
            with self.subTest(not_needed):
                self.assertNotIn(not_needed, self.patterns("regression"))

    def test_the_test_job_keeps_the_directories_its_suites_import_from(self):
        for needed in SUITE_DIRS:
            with self.subTest(needed):
                self.assertIn(needed, self.patterns("regression"))

    def test_the_two_heavy_trees_are_not_checked_out(self):
        for job in ("validate", "regression"):
            patterns = self.patterns(job)
            for excluded in EXCLUDED_TREES:
                with self.subTest(job=job, excluded=excluded):
                    self.assertNotIn(excluded, patterns)

    def test_the_partial_clone_filter_is_kept(self):
        for job in ("validate", "regression"):
            with self.subTest(job):
                self.assertEqual(self.step(job, "Checkout repository")["with"].get("filter"),
                                 "blob:none")

    def test_the_regression_job_installs_through_the_cached_uv_setup(self):
        uv = self.step("regression", "Set up uv")["with"]
        self.assertEqual(uv.get("enable-cache"), True)
        install = self.step("regression", "Install test dependencies")["run"]
        self.assertIn("uv pip install --system", install)
        self.assertIn("-r requirements-ci.txt", install)

    def test_the_requirement_file_still_covers_the_suite(self):
        packages = {
            line.strip()
            for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(REQUIRED_PACKAGES.issubset(packages),
                        f"requirements-ci.txt is missing {sorted(REQUIRED_PACKAGES - packages)}")

    def test_the_suite_spreads_single_tests_over_the_four_workers(self):
        """``worksteal`` is what keeps the two slow files from owning the run.

        The suite's cost sits in asset-server/test_service_e2e.py (5 tests of
        ~3.6 s) and scripts/test_should_build_release.py (8 tests of ~2 s).  Under
        the previous ``loadfile`` rule those two files filled two workers while
        the other two idled: 27.6 s.  Handing the next pending test to whichever
        worker is free measures 18.8 s, and the distribution is only safe because
        no test depends on another one's state (see the workflow comment).
        """
        run = self.step("regression", "Run the offline suites that gate publishing")["run"]
        self.assertIn("-n 4", run)
        self.assertIn("--dist worksteal", run)
        self.assertNotIn("--dist loadfile", run)
        self.assertIn("pytest-xdist", REQUIREMENTS.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
