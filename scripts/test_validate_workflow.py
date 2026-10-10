"""The checkout contract of ``.github/workflows/validate-localization.yml``.

``generated/`` (546 MB) and ``images/`` (333 MB) are 880 MB of this repository's
1.09 GB working copy, and neither job in that workflow opens them.  The checkout
asks for the other top-level directories by name and skips the two, which is
only worth anything together with ``filter: blob:none``: a sparse checkout on a
full clone still transfers every blob and saves nothing but the write.

A directory silently dropped from the list is a failed run at best and a check
that stopped looking at something at worst, so both jobs must carry the same
list, both must keep the partial-clone filter, and the regression job must
install the same requirement file the cache key is built from.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validate-localization.yml"
REQUIREMENTS = ROOT / "requirements-ci.txt"

#: Trees the workflow deliberately does not check out.
EXCLUDED_TREES = ("generated", "images")

#: Packages both jobs' suites cannot import without.
REQUIRED_PACKAGES = {
    "pytest", "pyyaml", "msgpack", "UnityPy", "Pillow", "pycryptodome",
    "jsonschema", "requests",
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

    def test_both_jobs_check_out_the_same_directories(self):
        self.assertEqual(self.patterns("validate"), self.patterns("regression"))

    def test_the_directories_the_suites_read_are_present(self):
        patterns = self.patterns("regression")
        for needed in (".github", "locales", "lyrics", "manifests", "pipelines",
                       "scripts", "asset-server", "server"):
            with self.subTest(needed):
                self.assertIn(needed, patterns)

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

    def test_the_regression_job_caches_the_wheels_it_installs(self):
        setup = self.step("regression", "Set up Python")["with"]
        self.assertEqual(setup.get("cache"), "pip")
        self.assertEqual(setup.get("cache-dependency-path"), "requirements-ci.txt")
        self.assertIn("-r requirements-ci.txt",
                      self.step("regression", "Install test dependencies")["run"])

    def test_the_requirement_file_still_covers_the_suite(self):
        packages = {
            line.strip()
            for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(REQUIRED_PACKAGES.issubset(packages),
                        f"requirements-ci.txt is missing {sorted(REQUIRED_PACKAGES - packages)}")


if __name__ == "__main__":
    unittest.main()
