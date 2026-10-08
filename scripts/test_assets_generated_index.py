#!/usr/bin/env python3
"""Assets 产品 generated writer 回归契约（唯一维护源）。

仅使用临时 fixture；不读真实 generated、不联网、不连接 DB/NAS、不执行 GC。
新产物固定 flat，sharded 仅作历史读取兼容。来源逐用例归属、未迁部分和
仓外独立运行方法见 docs/GENERATED_STORE.md；历史主仓是只读 archive，
不是运行时或自动测试依赖。既有两个产品回归方法原样保留。

Run: python -X utf8 -B scripts/test_assets_generated_index.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import assets_generated_index as mod  # noqa: E402

SOURCE_COMMIT = "a" * 40        # main :75
TRANSLATION_COMMIT = "b" * 40   # main :76
GENERATED_COMMIT = "c" * 40     # main :77
ASSET_VERSION = "1077100"
SNAPSHOT_COMMIT = "d" * 40


def digest_of(payload: bytes) -> str:  # main :81-82
    return hashlib.sha256(payload).hexdigest()


@contextlib.contextmanager
def _no_runner_env():
    """Hide every run-id variable for the duration of the block.

    The producer reads the runner's environment by design, so a test that wants
    to prove "no run context -> null" has to make sure the *test's* environment
    is not silently supplying one.
    """
    saved = {name: os.environ.pop(name)
             for name in mod.CI_RUN_ID_ENV_VARS if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


try:
    import jsonschema as _jsonschema
except ImportError:
    _jsonschema = None


class StoreTestCase(unittest.TestCase):
    """Common harness: an isolated store root plus entry/object helpers (main :85-191)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="assets-generated-product-test-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "generated"
        self.store = mod.GeneratedStore(self.root)
        self.blobs = self.base / "blobs"
        self.blobs.mkdir()
        schema_path = REPO_ROOT / "schema" / "assets-generated-manifest.schema.json"
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # -- helpers (main :150-191; product schema is read above) ---------- #
    def blob(self, name: str, payload: bytes) -> Path:
        path = self.blobs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def entry(self, logical_path: str, blob: Path, *, reuse_status: str = "exact",
              translation_status: str = "accepted", source_sha256: str | None = None,
              **extra) -> dict:
        payload = {
            "logical_key": logical_path,
            "logical_path": logical_path,
            "resource_kind": "bundle",
            "source_sha256": source_sha256 or digest_of(b"official:" + logical_path.encode()),
            "translated_sha256": digest_of(blob.read_bytes()),
            "reuse_status": reuse_status,
            "translation_status": translation_status,
            "artifact_file": str(blob.relative_to(self.base)),
        }
        payload.update(extra)
        return payload

    def build(self, entries, *, asset_version=ASSET_VERSION, status="success",
              entries_base=None, ci_run_id=None, store=None):
        return (store or self.store).build_release(
            asset_version,
            entries,
            source_client_version="9.0.200",
            source_commit=SOURCE_COMMIT,
            translation_commit=TRANSLATION_COMMIT,
            generated_commit=GENERATED_COMMIT,
            ci_run_id=ci_run_id,
            build_status=status,
            entries_base=entries_base if entries_base is not None else self.base,
        )

    def object_rel_paths(self, store=None) -> list[str]:
        store = store or self.store
        return sorted(path.relative_to(store.root).as_posix()
                      for path in store.iter_objects())

    # -- the mirrored regression (main :1404-1440, flat layout) ------------ #
    def test_duplicate_runtime_path_fails_closed_before_any_store_change(self):
        """One client path can only map to one entry, and the clash writes nothing.

        The uniqueness check runs in admission -- before ``put_object`` and
        before the manifest/checksums pair -- so a refused build cannot have
        already changed the object pool of a store it is not allowed to update.
        """
        runtime = "production/2018/Android/" + "cd" * 20 + ".unity3d"
        first = self.blob("first.unity3d", b"first payload")
        second = self.blob("second.unity3d", b"second payload")
        self.build([self.entry("event/1/first.unity3d", first, runtime_path=runtime)])
        before_manifest = self.store.manifest_path(ASSET_VERSION).read_bytes()
        before_objects = self.object_rel_paths()

        with self.assertRaises(mod.GeneratedStoreError) as caught:
            self.build([
                self.entry("event/1/second.unity3d", second, runtime_path=runtime),
                self.entry("event/1/third.unity3d", second, runtime_path=runtime),
            ])
        self.assertIn("runtime_path", str(caught.exception))
        self.assertIn("is used by both", str(caught.exception))
        self.assertEqual(self.store.manifest_path(ASSET_VERSION).read_bytes(), before_manifest)
        self.assertEqual(self.object_rel_paths(), before_objects)
        # 空 store 的对照：同一次拒绝连对象池都不能出现新字节（main :1426-1440）。
        scratch = mod.GeneratedStore(self.base / "empty-store")
        with self.assertRaises(mod.GeneratedStoreError):
            scratch.build_release(
                ASSET_VERSION,
                [self.entry("event/1/second.unity3d", second, runtime_path=runtime),
                 self.entry("event/1/third.unity3d", second, runtime_path=runtime)],
                source_client_version="9.0.200",
                source_commit=SOURCE_COMMIT,
                translation_commit=TRANSLATION_COMMIT,
                generated_commit=GENERATED_COMMIT,
                ci_run_id=None,
                entries_base=self.base,
            )
        self.assertEqual(sorted(p.name for p in scratch.iter_objects()), [])
        # flat 布局推导：拒绝的 build 若已写对象，会落在 objects/sha256/<digest>。
        self.assertFalse(scratch.object_path(digest_of(b"second payload")).exists())
        # 整个 store root 不残留任何文件，发布清单/校验和未写。
        residue = [p for p in scratch.root.rglob("*") if p.is_file()] \
            if scratch.root.exists() else []
        self.assertEqual(residue, [])
        self.assertFalse(scratch.manifest_path(ASSET_VERSION).is_file())
        self.assertFalse(scratch.checksums_path(ASSET_VERSION).is_file())

    # -- positive control: the mirror must not drift with the layout -------- #
    def test_unique_runtime_paths_build_completes_to_flat_object_paths(self):
        """Distinct runtime paths build fine and land on the flat canonical path.

        Guards the other failure mode: if the writer (or this file) drifted to
        the retired two-hex fan-out, the duplicate-path case above could pass
        for the wrong reason.
        """
        first = self.blob("pos-first.unity3d", b"positive control payload A")
        second = self.blob("pos-second.unity3d", b"positive control payload B")
        runtime_a = "production/2018/Android/" + "ab" * 20 + ".unity3d"
        runtime_b = "production/2018/Android/" + "ef" * 20 + ".unity3d"
        result = self.build([
            self.entry("event/1/pos-first.unity3d", first, runtime_path=runtime_a),
            self.entry("event/1/pos-second.unity3d", second, runtime_path=runtime_b),
        ])
        self.assertTrue(result.written)
        digest_a = digest_of(first.read_bytes())
        digest_b = digest_of(second.read_bytes())
        self.assertEqual(self.object_rel_paths(),
                         [f"objects/sha256/{digest_a}", f"objects/sha256/{digest_b}"])
        manifest = self.store.load_manifest(ASSET_VERSION)
        self.assertEqual(sorted(entry["object_path"] for entry in manifest["entries"]),
                         [f"objects/sha256/{digest_a}", f"objects/sha256/{digest_b}"])
        # 分片布局不得复活：canonical 是 flat，fan-out 仅供读取旧清单。
        for digest in (digest_a, digest_b):
            self.assertFalse(
                (self.root / "objects" / "sha256" / digest[:2] / digest).exists())
        report = self.store.verify_release(ASSET_VERSION)
        self.assertTrue(report.ok, report.failures)


    def objects_on_disk(self) -> list[str]:
        return sorted(path.name for path in self.store.iter_objects())


    @staticmethod
    def make_fetch(statuses: dict[str, str]):
        """A compare fetch driven by a ``{commit: status}`` table.

        Direction matters: ``base`` is the candidate commit and ``head`` the
        snapshot it must descend from, so the table is keyed by the candidate.
        """
        totals = {"identical": 0, "ahead": 3, "behind": 0, "diverged": 5}

        def fetch(url: str):
            base, _, _head = url.rsplit("/", 1)[-1].partition("...")
            status = statuses.get(base, "diverged")
            return 200, {"status": status, "total_commits": totals[status]}

        return fetch


    # Source: archived main suite, lines 210-228; ownership: docs/GENERATED_STORE.md
    def test_identical_bytes_are_stored_once_and_never_rewritten(self):
        left = self.blob("a/left.unity3d", b"same-bytes")
        right = self.blob("b/right.unity3d", b"same-bytes")
        self.assertNotEqual(left, right)

        first = self.store.put_object(left)
        second = self.store.put_object(right)

        self.assertEqual(first.digest, digest_of(b"same-bytes"))
        self.assertEqual(second.digest, first.digest)
        self.assertFalse(first.deduped)
        self.assertTrue(second.deduped)
        self.assertEqual(self.objects_on_disk(), [first.digest])
        self.assertEqual(first.rel_path, mod.cas_object_path(first.digest).as_posix())
        self.assertEqual(first.rel_path, f"objects/sha256/{first.digest}")
        self.assertEqual(self.object_rel_paths(), [f"objects/sha256/{first.digest}"])
        self.assertEqual(first.path.read_bytes(), b"same-bytes")
        self.assertEqual(mod.sha256_file(first.path), first.digest)
        self.assertEqual(mod.sha256_bytes(b"same-bytes"), first.digest)


    # Source: archived main suite, lines 230-234; ownership: docs/GENERATED_STORE.md
    def test_no_temporary_files_are_left_behind(self):
        blob = self.blob("one.unity3d", b"payload")
        self.store.put_object(blob)
        leftovers = [path.name for path in self.store.objects_dir.rglob("*.tmp")]
        self.assertEqual(leftovers, [])


    # Source: archived main suite, lines 265-272; ownership: docs/GENERATED_STORE.md
    def test_failed_build_does_not_create_or_modify_the_store(self):
        blob = self.blob("x.unity3d", b"x")
        result = self.build([self.entry("event/001/title.unity3d", blob)], status="failed")

        self.assertFalse(result.written)
        self.assertEqual(result.status, "failed")
        self.assertFalse(self.root.exists(),
                         "a failed build must not even create the store root")


    # Source: archived main suite, lines 274-287; ownership: docs/GENERATED_STORE.md
    def test_failed_build_leaves_an_existing_store_byte_identical(self):
        blob = self.blob("x.unity3d", b"x")
        self.build([self.entry("event/001/title.unity3d", blob)])
        snapshot = {
            path.relative_to(self.root).as_posix(): mod.sha256_file(path)
            for path in sorted(self.root.rglob("*")) if path.is_file()
        }
        result = self.build([self.entry("event/002/title.unity3d", blob)], status="failed")
        self.assertFalse(result.written)
        after = {
            path.relative_to(self.root).as_posix(): mod.sha256_file(path)
            for path in sorted(self.root.rglob("*")) if path.is_file()
        }
        self.assertEqual(snapshot, after)


    # Source: archived main suite, lines 289-294; ownership: docs/GENERATED_STORE.md
    def test_failed_build_never_validates_entries(self):
        # The entries are garbage; an early return must skip validation entirely.
        result = self.build([{"this": "is not an entry"}], status="failed")
        self.assertFalse(result.written)
        self.assertEqual(result.rejected, [])
        self.assertFalse(self.root.exists())


    # Source: archived main suite, lines 297-327; ownership: docs/GENERATED_STORE.md
    def test_inadmissible_statuses_are_rejected_with_reasons(self):
        blob = self.blob("y.unity3d", b"y")
        cases = [
            ("exact", "untranslated"),
            ("exact", "pending"),
            ("suggested", "accepted"),
            ("blocked", "accepted"),
        ]
        entries = [
            self.entry(f"event/{index:03d}/title.unity3d", blob,
                       reuse_status=reuse, translation_status=translation)
            for index, (reuse, translation) in enumerate(cases)
        ]
        result = self.build(entries)

        self.assertTrue(result.written)
        self.assertEqual(result.accepted, [])
        self.assertEqual(len(result.rejected), 4)
        self.assertEqual(
            {(row["reuse_status"], row["translation_status"]) for row in result.rejected},
            set(cases),
        )
        for row in result.rejected:
            self.assertTrue(row["reason"], "every refusal needs a reason")
            self.assertIn("cannot enter generated/", row["reason"])
        self.assertIn("suggested", json.dumps(result.rejected, ensure_ascii=False))
        manifest = self.store.load_manifest("1077100")
        self.assertEqual(manifest["entries"], [])
        self.assertEqual(manifest["entry_count"], 0)
        self.assertEqual(manifest["reuse_summary"]["rejected_entries"], 4)
        self.assertEqual(self.objects_on_disk(), [])


    # Source: archived main suite, lines 329-343; ownership: docs/GENERATED_STORE.md
    def test_exact_reuse_is_admitted(self):
        blob = self.blob("exact.unity3d", b"exact-bytes")
        result = self.build([self.entry("event/010/title.unity3d", blob,
                                        reuse_status="exact", translation_status="accepted")])
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.rejected, [])
        entry = self.store.load_manifest("1077100")["entries"][0]
        self.assertEqual(entry["reuse_status"], "exact")
        self.assertEqual(entry["translation_status"], "accepted")
        self.assertEqual(entry["channel"], "assets")
        self.assertIsNone(entry["client_version"])
        self.assertEqual(entry["source_client_version"], "9.0.200")
        self.assertEqual(entry["logical_path"], "event/010/title.unity3d")
        self.assertEqual(entry["object_path"],
                         mod.cas_object_path(entry["artifact_sha256"]).as_posix())


    # Source: archived main suite, lines 346-393; ownership: docs/GENERATED_STORE.md
    def test_changed_translation_on_unchanged_source_is_modified_not_verified(self):
        source_sha = digest_of(b"official source bytes")
        previous = [{
            "logical_key": "event/020/title.unity3d",
            "logical_path": "event/020/title.unity3d",
            "source_sha256": source_sha,
            "translated_sha256": digest_of(b"old translation"),
            "reuse_status": "exact",
            "translation_status": "accepted",
        }]
        new_sources = [{
            "logical_key": "event/020/title.unity3d",
            "logical_path": "event/020/title.unity3d",
            "source_sha256": source_sha,
            "translated_sha256": digest_of(b"new translation"),
        }]
        decisions = mod.ReuseLedger(previous).decide(new_sources)
        self.assertEqual(len(decisions), 1)
        decision = decisions[0]
        self.assertEqual(decision.reuse_status, "exact")
        self.assertEqual(decision.translation_status, "modified")
        self.assertNotEqual(decision.reuse_status, "verified-compatible",
                            "a translation change must never be relabelled as source compatibility")
        self.assertTrue(decision.eligible)
        self.assertIn("source_sha256 unchanged", decision.reason)

        # And the same input through the store creates a NEW object.
        first_blob = self.blob("v1.unity3d", b"old translation")
        first = self.build([self.entry("event/020/title.unity3d", first_blob,
                                       source_sha256=source_sha)],
                           asset_version="1077100")
        self.assertEqual(len(self.objects_on_disk()), 1)

        second_blob = self.blob("v2.unity3d", b"new translation")
        second = self.build([self.entry("event/020/title.unity3d", second_blob,
                                        source_sha256=source_sha,
                                        translation_status="modified")],
                            asset_version="1077101")
        self.assertEqual(second.accepted[0]["asset_version"], "1077101")
        self.assertEqual(second.accepted[0]["reuse_status"], "exact")
        self.assertEqual(second.accepted[0]["translation_status"], "modified")
        self.assertEqual(len(second.accepted), 1)
        self.assertEqual(len(self.objects_on_disk()), 2,
                         "changed translated bytes must produce a new object")
        self.assertNotEqual(first.accepted[0]["artifact_sha256"],
                            second.accepted[0]["artifact_sha256"])
        self.assertEqual(first.accepted[0]["source_sha256"],
                         second.accepted[0]["source_sha256"])


    # Source: archived main suite, lines 395-420; ownership: docs/GENERATED_STORE.md
    def test_unchanged_translation_reuses_the_object(self):
        source_sha = digest_of(b"stable source")
        blob = self.blob("stable.unity3d", b"stable translation")
        first = self.build([self.entry("event/030/title.unity3d", blob,
                                       source_sha256=source_sha)], asset_version="1077100")

        previous = self.store.load_manifest("1077100")["entries"]
        decisions = mod.ReuseLedger(previous).decide([{
            "logical_key": "event/030/title.unity3d",
            "logical_path": "event/030/title.unity3d",
            "source_sha256": source_sha,
            "translated_sha256": digest_of(b"stable translation"),
        }])
        self.assertEqual(decisions[0].reuse_status, "exact")
        self.assertEqual(decisions[0].translation_status, "reused")

        second = self.build([self.entry("event/030/title.unity3d", blob,
                                        source_sha256=source_sha,
                                        translation_status="reused")],
                            asset_version="1077102")
        self.assertEqual(second.objects_written, 0)
        self.assertEqual(second.objects_deduped, 1)
        self.assertEqual(len(self.objects_on_disk()), 1,
                         "identical bytes must not create a second object")
        self.assertEqual(first.accepted[0]["artifact_sha256"],
                         second.accepted[0]["artifact_sha256"])


    # Source: archived main suite, lines 423-470; ownership: docs/GENERATED_STORE.md
    def test_verified_compatible_requires_an_explicit_record(self):
        from_sha = digest_of(b"source v1")
        to_sha = digest_of(b"source v2")
        previous = [{
            "logical_key": "event/040/title.unity3d",
            "logical_path": "event/040/title.unity3d",
            "source_sha256": from_sha,
            "translated_sha256": digest_of(b"t"),
            "reuse_status": "exact",
            "translation_status": "accepted",
        }]
        sources = [{
            "logical_key": "event/040/title.unity3d",
            "logical_path": "event/040/title.unity3d",
            "source_sha256": to_sha,
            "translated_sha256": digest_of(b"t"),
        }]

        without = mod.ReuseLedger(previous).decide(sources)[0]
        self.assertEqual(without.reuse_status, "suggested")
        self.assertFalse(without.eligible)
        self.assertIn("manual review only", without.reason)

        records = {"event/040/title.unity3d": {
            "from_sha256": from_sha, "to_sha256": to_sha,
            "evidence": "manual diff of 2026-09-24",
        }}
        with_record = mod.ReuseLedger(previous, records).decide(sources)[0]
        self.assertEqual(with_record.reuse_status, "verified-compatible")
        self.assertTrue(with_record.eligible)
        self.assertIn("manual diff of 2026-09-24", with_record.reason)

        # A record for a different digest pair must not authorise anything.
        stale = {"event/040/title.unity3d": {
            "from_sha256": digest_of(b"other"), "to_sha256": to_sha, "evidence": "stale"}}
        self.assertEqual(mod.ReuseLedger(previous, stale).decide(sources)[0].reuse_status,
                         "suggested")

        # Only `exact`/`verified-compatible` reach the store.
        blob = self.blob("v2.unity3d", b"t")
        admitted = self.build([self.entry("event/040/title.unity3d", blob, source_sha256=to_sha,
                                          reuse_status="verified-compatible")])
        self.assertEqual(len(admitted.accepted), 1)
        self.assertEqual(admitted.rejected, [])
        refused = self.build([self.entry("event/041/title.unity3d", blob, source_sha256=to_sha,
                                         reuse_status="suggested")])
        self.assertEqual(refused.accepted, [])
        self.assertEqual(len(refused.rejected), 1)


    # Source: archived main suite, lines 472-481; ownership: docs/GENERATED_STORE.md
    def test_unknown_logical_key_is_blocked(self):
        blob = self.blob("b.unity3d", b"b")
        decisions = mod.ReuseLedger([]).decide([{
            "logical_key": "event/999/title.unity3d",
            "logical_path": "event/999/title.unity3d",
            "source_sha256": digest_of(b"never seen"),
            "translated_sha256": digest_of(b"b"),
        }])
        self.assertEqual(decisions[0].reuse_status, "blocked")
        self.assertFalse(decisions[0].eligible)


    # Source: archived main suite, lines 484-506; ownership: docs/GENERATED_STORE.md
    def test_verify_release_passes_then_fails_on_tamper_and_on_missing_object(self):
        blob = self.blob("t.unity3d", b"trustworthy")
        self.build([self.entry("event/050/title.unity3d", blob)])
        self.assertTrue(self.store.verify_release("1077100").ok)

        entry = self.store.load_manifest("1077100")["entries"][0]
        object_path = self.store.object_path(entry["artifact_sha256"])
        original = object_path.read_bytes()

        object_path.write_bytes(b"tampered")
        report = self.store.verify_release("1077100")
        self.assertFalse(report.ok)
        self.assertTrue(any("hashes to" in failure for failure in report.failures))
        self.assertTrue(any("event/050/title.unity3d" in failure for failure in report.failures),
                        f"the failure must name the entry: {report.failures}")

        object_path.write_bytes(original)
        self.assertTrue(self.store.verify_release("1077100").ok)

        object_path.unlink()
        missing = self.store.verify_release("1077100")
        self.assertFalse(missing.ok)
        self.assertTrue(any("missing object" in failure for failure in missing.failures))


    # Source: archived main suite, lines 508-520; ownership: docs/GENERATED_STORE.md
    def test_verify_fails_when_checksums_disagree_with_the_manifest(self):
        blob = self.blob("c.unity3d", b"checksummed")
        self.build([self.entry("event/060/title.unity3d", blob)])
        checksums = self.store.checksums_path("1077100")
        self.assertIn("  objects/sha256/", checksums.read_text(encoding="utf-8"))
        # The retired fan-out path would still be *tolerated* by the digest
        # comparison, so this row must name a digest that is genuinely absent.
        checksums.write_text(f"{digest_of(b'nothing')}  "
                             f"{mod.cas_object_path(digest_of(b'nothing')).as_posix()}\n",
                             encoding="utf-8")
        report = self.store.verify_release("1077100")
        self.assertFalse(report.ok)
        self.assertTrue(any("not listed in checksums.txt" in failure for failure in report.failures))


    # Source: archived main suite, lines 522-533; ownership: docs/GENERATED_STORE.md
    def test_verify_rejects_a_manifest_that_smuggled_in_a_non_admissible_status(self):
        blob = self.blob("s.unity3d", b"smuggled")
        self.build([self.entry("event/070/title.unity3d", blob)])
        manifest_path = self.store.manifest_path("1077100")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["entries"][0]["reuse_status"] = "suggested"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        report = self.store.verify_release("1077100")
        self.assertFalse(report.ok)
        self.assertTrue(any("reuse_status 'suggested' is not admissible" in failure
                            for failure in report.failures), report.failures)


    # Source: archived main suite, lines 536-559; ownership: docs/GENERATED_STORE.md
    def test_combined_version_identity_is_refused(self):
        blob = self.blob("v.unity3d", b"v")
        with self.assertRaises(mod.VersionIdentityError):
            self.build([self.entry("event/080/title.unity3d", blob)],
                       asset_version="9.0.200+1077100")
        with self.assertRaises(mod.VersionIdentityError):
            self.build([self.entry("event/080/title.unity3d", blob)],
                       asset_version="client-9.0.200-assets-1077100")
        with self.assertRaises(mod.VersionIdentityError):
            self.store.build_release(
                "1077100", [self.entry("event/080/title.unity3d", blob)],
                source_client_version="9.0.200+1077100",
                source_commit=SOURCE_COMMIT, translation_commit=TRANSLATION_COMMIT,
                generated_commit=GENERATED_COMMIT, entries_base=self.base)
        self.assertFalse(self.root.exists(), "a refused identity must not create the store")

        code, _out, err = self.run_cli([
            "build", "--root", str(self.root), "--asset-version", "9.0.200+1077100",
            "--entries", "-", "--source-client-version", "9.0.200",
            "--source-commit", SOURCE_COMMIT, "--translation-commit", TRANSLATION_COMMIT,
            "--generated-commit", GENERATED_COMMIT, "--build-status", "success",
        ], stdin_text="[]")
        self.assertNotEqual(code, 0)
        self.assertIn("independent axes", err)


    # Source: archived main suite, lines 562-583; ownership: docs/GENERATED_STORE.md
    def test_second_successful_build_replaces_the_manifest_and_keeps_old_objects(self):
        first_blob = self.blob("r1.unity3d", b"revision one")
        first = self.build([self.entry("event/090/title.unity3d", first_blob)])
        first_digest = first.accepted[0]["artifact_sha256"]

        second_blob = self.blob("r2.unity3d", b"revision two")
        second = self.build([self.entry("event/090/title.unity3d", second_blob)])
        second_digest = second.accepted[0]["artifact_sha256"]
        self.assertNotEqual(first_digest, second_digest)

        manifest = self.store.load_manifest("1077100")
        self.assertEqual(manifest["entry_count"], 1)
        self.assertEqual(manifest["entries"][0]["artifact_sha256"], second_digest)
        self.assertEqual(self.store.list_releases(), ["1077100"])

        self.assertTrue(self.store.object_path(first_digest).is_file(),
                        "the superseded object must stay on disk until an explicit prune")
        self.assertEqual(self.objects_on_disk(), sorted([first_digest, second_digest]))
        self.assertEqual([path.name for path in self.store.find_orphans()], [first_digest])
        self.assertEqual(self.store.collect_referenced_objects(), {second_digest})

        self.assertTrue(self.store.verify_release("1077100").ok)


    # Source: archived main suite, lines 585-599; ownership: docs/GENERATED_STORE.md
    def test_different_asset_versions_coexist(self):
        shared = self.blob("shared.unity3d", b"shared bytes")
        only_a = self.blob("a.unity3d", b"a only")
        self.build([self.entry("event/100/title.unity3d", shared),
                    self.entry("event/100/body.unity3d", only_a)], asset_version="1077100")
        self.build([self.entry("event/100/title.unity3d", shared)], asset_version="1077500")

        self.assertEqual(self.store.list_releases(), ["1077100", "1077500"])
        self.assertEqual(len(self.objects_on_disk()), 2, "shared bytes are not duplicated")
        for version in ("1077100", "1077500"):
            self.assertTrue(self.store.verify_release(version).ok, version)
        self.assertEqual(self.store.find_orphans(), [])
        self.assertEqual(
            {entry["asset_version"] for entry in self.store.load_manifest("1077500")["entries"]},
            {"1077500"})


    def run_cli(self, argv: list[str], *, stdin_text: str | None = None):
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(stdin_text) if stdin_text is not None else None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is None:
                code = mod.main(argv)
            else:
                saved = sys.stdin
                sys.stdin = stdin
                try:
                    code = mod.main(argv)
                finally:
                    sys.stdin = saved
        return code, out.getvalue(), err.getvalue()


    # Source: archived main suite, lines 617-668; ownership: docs/GENERATED_STORE.md
    def test_cli_build_verify_round_trip(self):
        blob = self.blob("cli.unity3d", b"cli payload")
        entries_path = self.base / "entries.json"
        entries_path.write_text(json.dumps(
            [self.entry("event/110/title.unity3d", blob)]), encoding="utf-8")

        code, out, err = self.run_cli([
            "build", "--root", str(self.root), "--asset-version", "1077100",
            "--entries", str(entries_path), "--entries-base", str(self.base),
            "--source-client-version", "9.0.200", "--source-commit", SOURCE_COMMIT,
            "--translation-commit", TRANSLATION_COMMIT,
            "--generated-commit", GENERATED_COMMIT, "--build-status", "success",
        ])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["written"])
        self.assertEqual(payload["accepted"], 1)

        code, out, err = self.run_cli(["verify", "--root", str(self.root),
                                       "--asset-version", "1077100"])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["ok"])

        code, out, err = self.run_cli(["list", "--root", str(self.root)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["releases"][0]["asset_version"], "1077100")

        # A tampered object must make `verify` exit non-zero with the reason.
        entry = self.store.load_manifest("1077100")["entries"][0]
        self.store.object_path(entry["artifact_sha256"]).write_bytes(b"tampered")
        code, _out, err = self.run_cli(["verify", "--root", str(self.root),
                                        "--asset-version", "1077100"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL:", err)



    # Source: archived main suite, lines 670-685; ownership: docs/GENERATED_STORE.md
    def test_cli_failed_build_exits_non_zero_and_touches_nothing(self):
        blob = self.blob("f.unity3d", b"f")
        entries_path = self.base / "failed-entries.json"
        entries_path.write_text(json.dumps(
            [self.entry("event/120/title.unity3d", blob)]), encoding="utf-8")
        code, out, err = self.run_cli([
            "build", "--root", str(self.root), "--asset-version", "1077100",
            "--entries", str(entries_path), "--entries-base", str(self.base),
            "--source-client-version", "9.0.200", "--source-commit", SOURCE_COMMIT,
            "--translation-commit", TRANSLATION_COMMIT,
            "--generated-commit", GENERATED_COMMIT, "--build-status", "failed",
        ])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["written"])
        self.assertIn("was not touched", err)
        self.assertFalse(self.root.exists())


    # Source: archived main suite, lines 687-712; ownership: docs/GENERATED_STORE.md
    def test_cli_reuse_writes_the_decision_document(self):
        previous_path = self.base / "previous-manifest.json"
        previous_path.write_text(json.dumps({"entries": [{
            "logical_key": "event/130/title.unity3d",
            "logical_path": "event/130/title.unity3d",
            "source_sha256": digest_of(b"src"),
            "translated_sha256": digest_of(b"zh1"),
        }]}), encoding="utf-8")
        sources_path = self.base / "new-sources.json"
        sources_path.write_text(json.dumps([{
            "logical_key": "event/130/title.unity3d",
            "logical_path": "event/130/title.unity3d",
            "source_sha256": digest_of(b"src"),
            "translated_sha256": digest_of(b"zh2"),
        }]), encoding="utf-8")
        out_path = self.base / "reuse.json"

        code, _out, err = self.run_cli([
            "reuse", "--previous-manifest", str(previous_path),
            "--new-sources", str(sources_path), "--out", str(out_path),
        ])
        self.assertEqual(code, 0, err)
        document = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(document["decisions"][0]["reuse_status"], "exact")
        self.assertEqual(document["decisions"][0]["translation_status"], "modified")
        self.assertEqual(document["summary"]["eligible"], 1)


    # Source: archived main suite, lines 714-721; ownership: docs/GENERATED_STORE.md
    def test_cli_missing_store_fails_closed(self):
        code, _out, err = self.run_cli(["verify", "--root", str(self.root),
                                        "--asset-version", "1077100"])
        self.assertEqual(code, 1)
        self.assertIn("missing manifest", err)


    # Source: archived main suite, lines 725-789; ownership: docs/GENERATED_STORE.md
    @unittest.skipUnless(_jsonschema is not None, "jsonschema is not installed")
    def test_produced_manifest_conforms_and_combined_version_is_refused(self):
        blob = self.blob("schema.unity3d", b"schema payload")
        self.build([self.entry("event/140/title.unity3d", blob)])
        manifest = json.loads(self.store.manifest_path("1077100").read_text(encoding="utf-8"))
        _jsonschema.Draft202012Validator(self.schema).validate(manifest)

        combined = json.loads(json.dumps(manifest))
        combined["asset_version"] = "9.0.200+1077100"
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(combined)

        combined_client = json.loads(json.dumps(manifest))
        combined_client["source_client_version"] = "client-9.0.200-assets-1077100"
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(combined_client)

        leaked_client_version = json.loads(json.dumps(manifest))
        leaked_client_version["client_version"] = "9.0.200"
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(leaked_client_version)

        extra_field = json.loads(json.dumps(manifest))
        extra_field["entries"][0]["sneaky"] = True
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(extra_field)

        smuggled = json.loads(json.dumps(manifest))
        smuggled["entries"][0]["translation_status"] = "pending"
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(smuggled)

        bad_object_path = json.loads(json.dumps(manifest))
        bad_object_path["entries"][0]["object_path"] = "objects/sha256/aa/deadbeef"
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(bad_object_path)

        no_kind = json.loads(json.dumps(manifest))
        del no_kind["entries"][0]["resource_kind"]
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(no_kind)

        no_run_id = json.loads(json.dumps(manifest))
        del no_run_id["ci_run_id"]
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(no_run_id)

        combined_run_id = json.loads(json.dumps(manifest))
        combined_run_id["ci_run_id"] = "9.0.200+1077100"
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(combined_run_id)

        # Product builds state the flat canonical path...
        self.assertEqual(manifest["entries"][0]["object_path"],
                         f"objects/sha256/{manifest['entries'][0]['artifact_sha256']}")
        # ...while the historical sharded form stays schema-valid: a manifest
        # committed before the switch must not become unreadable.
        legacy = json.loads(json.dumps(manifest))
        digest = legacy["entries"][0]["artifact_sha256"]
        legacy["entries"][0]["object_path"] = f"objects/sha256/{digest[:2]}/{digest}"
        _jsonschema.Draft202012Validator(self.schema).validate(legacy)

        malformed_path = json.loads(json.dumps(manifest))
        malformed_path["entries"][0]["object_path"] = "objects/sha256/aa/deadbeef"
        with self.assertRaises(_jsonschema.ValidationError):
            _jsonschema.Draft202012Validator(self.schema).validate(malformed_path)


    # Source: archived main suite, lines 792-830; ownership: docs/GENERATED_STORE.md
    def test_objects_are_flat_and_the_legacy_shard_is_read_only(self):
        """New objects are flat; a shard-only object is readable, then copied.

        The layout change must not orphan bytes that a previously committed
        manifest still names.  ``object_file`` finds either form, ``put_object``
        copies a shard object to its flat home rather than reporting the object
        missing, and the shard object itself is left where the old manifest expects it.
        """
        blob = self.blob("flat.unity3d", b"flat payload")
        digest = digest_of(b"flat payload")
        stored = self.store.put_object(blob)
        self.assertEqual(stored.rel_path, f"objects/sha256/{digest}")
        self.assertEqual(self.object_rel_paths(), [f"objects/sha256/{digest}"])
        self.assertEqual(self.store.object_rel_path(digest), f"objects/sha256/{digest}")

        # An empty payload is refused: a zero-byte object is indistinguishable
        # from a failed download, so it must be an error rather than a manifest row.
        empty = self.blob("empty.unity3d", b"")
        with self.assertRaises(mod.GeneratedStoreError) as caught:
            self.store.put_object(empty)
        self.assertIn("empty file", str(caught.exception))

        # Fixture only: the bytes exist solely at the historical shard path.
        legacy_store = mod.GeneratedStore(self.root / "legacy")
        legacy_store.legacy_object_path(digest).parent.mkdir(parents=True, exist_ok=True)
        legacy_store.legacy_object_path(digest).write_bytes(b"flat payload")
        self.assertFalse(legacy_store.object_path(digest).is_file())
        self.assertEqual(legacy_store.object_file(digest),
                         legacy_store.legacy_object_path(digest))
        self.assertEqual(legacy_store.object_rel_path(digest),
                         f"objects/sha256/{digest[:2]}/{digest}")

        reparsed = legacy_store.put_object(blob)
        self.assertTrue(reparsed.deduped)
        self.assertEqual(reparsed.path, legacy_store.object_path(digest))
        self.assertEqual(legacy_store.object_path(digest).read_bytes(), b"flat payload")
        self.assertTrue(legacy_store.legacy_object_path(digest).is_file(),
                        "copying a legacy sharded object must not move it: an old manifest still names it")
        self.assertEqual(legacy_store.object_file(digest), legacy_store.object_path(digest))


    # Source: archived main suite, lines 832-889; ownership: docs/GENERATED_STORE.md
    def test_a_retained_sharded_manifest_verifies_without_rewriting(self):
        """A retained sharded release is readable and noted without rewriting.

        The failure this pins: pruning by "is there a flat file with this name"
        deletes the shard a live manifest depends on, and a verify that demands
        the flat path declares a perfectly good release broken.
        """
        entry = {
            "channel": "assets", "asset_version": "1077100", "client_version": None,
            "source_client_version": "9.0.200", "source_commit": SOURCE_COMMIT,
            "translation_commit": TRANSLATION_COMMIT, "generated_commit": GENERATED_COMMIT,
            "ci_run_id": None, "logical_key": "event/150/title.unity3d",
            "logical_path": "event/150/title.unity3d", "resource_kind": "bundle",
            "source_sha256": digest_of(b"official"),
            "translated_sha256": digest_of(b"legacy bytes"),
            "artifact_sha256": digest_of(b"legacy bytes"),
            "reuse_status": "exact", "translation_status": "accepted",
        }
        digest = entry["artifact_sha256"]
        shard = self.store.legacy_object_path(digest)
        shard.parent.mkdir(parents=True, exist_ok=True)
        shard.write_bytes(b"legacy bytes")
        entry["object_path"] = f"objects/sha256/{digest[:2]}/{digest}"
        self.store.release_dir("1077100").mkdir(parents=True, exist_ok=True)
        self.store.manifest_path("1077100").write_text(json.dumps({
            "kind": mod.MANIFEST_KIND, "schema_version": 1, "asset_version": "1077100",
            "client_version": None, "source_client_version": "9.0.200",
            "source_commit": SOURCE_COMMIT, "translation_commit": TRANSLATION_COMMIT,
            "generated_commit": GENERATED_COMMIT, "ci_run_id": None,
            "build_status": "success", "generated_at_utc": "2026-09-30T00:00:00Z",
            "entry_count": 1,
            "reuse_summary": {"entries": 1,
                              "reuse_status_counts": {"exact": 1, "verified-compatible": 0,
                                                      "suggested": 0, "blocked": 0},
                              "translation_status_counts": {"untranslated": 0, "pending": 0,
                                                            "accepted": 1, "modified": 0,
                                                            "reused": 0},
                              "rejected_entries": 0, "rejected_reasons": {}},
            "entries": [entry],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.store.checksums_path("1077100").write_text(
            f"{digest}  objects/sha256/{digest[:2]}/{digest}\n", encoding="utf-8")

        before = self.snapshot_of(self.root)
        report = self.store.verify_release("1077100")
        self.assertTrue(report.ok, report.failures)
        self.assertTrue(report.notes, "a legacy layout must be reported, not silently accepted")
        self.assertTrue(any("shard" in note for note in report.notes), report.notes)

        self.assertEqual(self.snapshot_of(self.root), before)
        self.assertFalse(self.store.object_path(digest).exists())


    # Source: archived main suite, lines 928-952; ownership: docs/GENERATED_STORE.md
    def test_a_release_whose_checksums_contradict_its_manifest_never_lands(self):
        """The pair is validated before the build reports success.

        A manifest that lands without matching checksums is accepted by the NAS
        and only fails at serve time, so the write path checks the pair it just
        wrote -- and a mismatch rolls the release directory back to what it was.
        """
        blob = self.blob("pair.unity3d", b"pair payload")
        first = self.build([self.entry("event/160/title.unity3d", blob)])
        self.assertTrue(first.written)
        before = {path.relative_to(self.root).as_posix(): mod.sha256_file(path)
                  for path in sorted(self.root.rglob("*")) if path.is_file()}

        broken = [self.entry("event/161/title.unity3d", blob)]
        broken[0]["artifact_sha256"] = digest_of(b"not the bytes")
        with self.assertRaises(mod.GeneratedStoreError):
            self.build(broken)

        after = {path.relative_to(self.root).as_posix(): mod.sha256_file(path)
                 for path in sorted(self.root.rglob("*")) if path.is_file()}
        self.assertEqual(before, after, "a failed pair write must restore the previous bytes")
        self.assertTrue(self.store.verify_release("1077100").ok)
        leftovers = [path.name for path in self.store.release_dir("1077100").iterdir()
                     if path.name.endswith((".tmp", ".staged", ".backup"))]
        self.assertEqual(leftovers, [], "staging and backup files must not survive")


    def snapshot_of(self, root: Path) -> dict:
        return {path.relative_to(root).as_posix(): mod.sha256_file(path)
                for path in sorted(root.rglob("*")) if path.is_file()}


    # Source: archived main suite, lines 959-986; ownership: docs/GENERATED_STORE.md
    def test_transaction_publishes_a_whole_candidate_and_leaves_the_live_root_alone(self):
        shared = self.blob("shared.unity3d", b"shared payload")
        extra = self.blob("extra.unity3d", b"extra payload")
        self.build([self.entry("text/1/title.unity3d", shared)])
        before = self.snapshot_of(self.root)

        with self.store.transaction(prune=False) as candidate:
            self.assertIsInstance(candidate, mod.GeneratedStore)
            self.assertNotEqual(candidate.root, self.store.root)
            self.build([self.entry("text/1/title.unity3d", shared),
                        self.entry("text/1/body.unity3d", extra)],
                       asset_version="1077500", store=candidate)
            # The candidate is seeded with what the live root serves, so the
            # new version can be verified before it is ever switched in.
            self.assertTrue(candidate.verify_release("1077500").ok)
            self.assertTrue(candidate.verify_release("1077100").ok)
            self.assertEqual(self.snapshot_of(self.root), before,
                             "the live root must not be written inside the block")

        self.assertTrue(self.store.verify_release("1077100").ok)
        self.assertTrue(self.store.verify_release("1077500").ok)
        self.assertEqual(self.store.list_releases(), ["1077100", "1077500"])
        self.assertEqual(len(self.store.find_orphans()), 0)
        self.assertEqual(len(list(self.store.iter_objects())), 2)
        # No candidate or backup directory survives a successful switch.
        leftovers = [path.name for path in self.root.parent.iterdir()
                     if path.name.startswith(f".{self.root.name}.")]
        self.assertEqual(leftovers, [])


    # Source: archived main suite, lines 988-1001; ownership: docs/GENERATED_STORE.md
    def test_transaction_rolls_back_on_an_exception_inside_the_block(self):
        shared = self.blob("shared.unity3d", b"shared payload")
        self.build([self.entry("text/2/title.unity3d", shared)])
        before = self.snapshot_of(self.root)

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with self.store.transaction(prune=False) as candidate:
                self.build([self.entry("text/2/title.unity3d", shared)],
                           asset_version="1077500", store=candidate)
                raise RuntimeError("boom")

        self.assertEqual(self.snapshot_of(self.root), before,
                         "an exception inside the block must leave the live root byte-identical")
        self.assertEqual(self.store.list_releases(), ["1077100"])


    # Source: archived main suite, lines 1003-1016; ownership: docs/GENERATED_STORE.md
    def test_transaction_refuses_a_bad_candidate_and_keeps_the_live_root(self):
        shared = self.blob("shared.unity3d", b"shared payload")
        self.build([self.entry("text/3/title.unity3d", shared)])
        before = self.snapshot_of(self.root)

        with self.assertRaises(mod.GeneratedStoreError) as caught:
            with self.store.transaction(prune=False) as candidate:
                self.build([self.entry("text/3/title.unity3d", shared)],
                           asset_version="1077500", store=candidate)
                # corrupt the candidate the way a broken upload would
                candidate.checksums_path("1077500").write_text("", encoding="utf-8")
        self.assertIn("not publishable", str(caught.exception))
        self.assertEqual(self.snapshot_of(self.root), before)
        self.assertEqual(self.store.list_releases(), ["1077100"])


    # Source: archived main suite, lines 1018-1065; ownership: docs/GENERATED_STORE.md
    def test_every_switch_failure_point_restores_the_previous_root(self):
        """The switch is two moves; a crash before, between or after them is recoverable.

        The failure mode this pins: the live root is moved aside and the new one
        never arrives (or arrives truncated), leaving a store that serves
        nothing.  Each step is simulated, and after each the live root must be
        byte-identical to what it was.
        """
        shared = self.blob("shared.unity3d", b"shared payload")
        self.build([self.entry("text/4/title.unity3d", shared)])
        before = self.snapshot_of(self.root)

        for step in ("copy_object", "switch_live_to_backup",
                     "switch_candidate_to_live"):
            with self.subTest(step=step):
                boom = RuntimeError(f"injected failure at {step}")

                def inject(name, index, *, _step=step, _boom=boom):
                    if name == _step:
                        raise _boom

                with self.assertRaises(RuntimeError) as caught:
                    with self.store.transaction(prune=False, fault_inject=inject) as candidate:
                        self.build([self.entry("text/4/title.unity3d", shared)],
                                   asset_version="1077500", store=candidate)
                        self.assertTrue(candidate.verify_release("1077500").ok)
                self.assertIs(caught.exception, boom)
                self.assertEqual(self.snapshot_of(self.root), before,
                                 f"a failure at {step} must leave the live root as it was")
                self.assertEqual(self.store.list_releases(), ["1077100"])
                self.assertEqual(len(self.store.find_orphans()), 0,
                                 "the live root is unchanged: its objects are still referenced")
                leftovers = [path.name for path in self.root.parent.iterdir()
                             if path.name.startswith(f".{self.root.name}.")]
                self.assertEqual(leftovers, [],
                                 f"a failed switch must not leave staging/backup material")

        # And after all those failures the store still promotes correctly, so
        # the injected faults did not leave hidden state behind.
        with self.store.transaction(prune=False) as candidate:
            self.build([self.entry("text/4/title.unity3d", shared)],
                       asset_version="1077500", store=candidate)
        self.assertEqual(self.store.list_releases(), ["1077100", "1077500"])
        self.assertTrue(self.store.verify_release("1077500").ok)


    # Source: archived main suite, lines 1067-1077; ownership: docs/GENERATED_STORE.md
    def test_a_candidate_that_would_drop_a_release_is_refused(self):
        shared = self.blob("shared.unity3d", b"shared payload")
        self.build([self.entry("text/5/title.unity3d", shared)])
        self.build([self.entry("text/5/title.unity3d", shared)], asset_version="1077500")
        before = self.snapshot_of(self.root)

        with self.assertRaises(mod.GeneratedStoreError) as caught:
            with self.store.transaction() as candidate:
                shutil.rmtree(candidate.release_dir("1077500"))
        self.assertIn("would drop", str(caught.exception))
        self.assertEqual(self.snapshot_of(self.root), before)


    # Source: archived main suite, lines 1079-1100; ownership: docs/GENERATED_STORE.md
    def test_the_candidate_is_isolated_from_write_through(self):
        """A candidate write must not appear in the live root.

        Hard-linking the live pool into the candidate would make the isolation a
        promise about the caller: editing a file in place would change both
        copies, and no later comparison could tell the two apart.
        """
        blob = self.blob("shared.unity3d", b"shared payload")
        self.build([self.entry("text/6/title.unity3d", blob)])
        digest = digest_of(b"shared payload")

        with self.assertRaises(mod.GeneratedStoreError):
            with self.store.transaction() as candidate:
                candidate.object_path(digest).write_bytes(b"rewritten in the candidate")
                self.assertEqual(self.store.object_path(digest).read_bytes(), b"shared payload",
                                 "a candidate write must not reach through to the live object")
                self.assertEqual(mod.sha256_file(self.store.object_path(digest)), digest)
                # ...and the corruption is the candidate's alone, so the switch
                # is refused rather than publishing bytes that do not hash.

        self.assertEqual(self.store.object_path(digest).read_bytes(), b"shared payload")
        self.assertTrue(self.store.verify_release("1077100").ok)


    # Source: archived main suite, lines 1102-1127; ownership: docs/GENERATED_STORE.md
    def test_a_failure_before_the_first_move_does_not_touch_the_live_root(self):
        """The dangerous shape: undoing a step that never ran.

        A restore path that removes the live root before checking whether the
        root was ever moved destroys the only copy of the store when the very
        first move fails.  Here the first move is refused, and the root must be
        exactly what it was -- not moved, not deleted, not re-created.
        """
        blob = self.blob("shared.unity3d", b"shared payload")
        self.build([self.entry("text/8/title.unity3d", blob)])
        before = self.snapshot_of(self.root)

        def refuse_the_first_move(step, index):
            if step == "switch_live_to_backup":
                raise RuntimeError("refused before the root was displaced")

        with self.assertRaisesRegex(RuntimeError, "refused before"):
            with self.store.transaction(prune=False,
                                        fault_inject=refuse_the_first_move) as candidate:
                self.build([self.entry("text/8/title.unity3d", blob)],
                           asset_version="1077500", store=candidate)

        self.assertTrue(self.root.is_dir())
        self.assertEqual(self.snapshot_of(self.root), before)
        self.assertTrue(self.store.verify_release("1077100").ok)
        self.assertEqual(self.store.list_releases(), ["1077100"])


    # Source: archived main suite, lines 1129-1166; ownership: docs/GENERATED_STORE.md
    def test_a_first_build_that_fails_after_the_switch_leaves_no_root(self):
        """The state to restore is "there was no root", and that is a state.

        On a first build the switch creates the root where none existed; if the
        new root then fails its post-switch verification, leaving it in place is
        not a rollback -- the store would claim a release it never had.
        """
        root = self.base / "generated-first"
        store = mod.GeneratedStore(root)
        blob = self.blob("first.unity3d", b"first payload")
        self.assertFalse(root.exists())

        digest = digest_of(b"first payload")

        def poison_before_the_second_move(step, index):
            # Break the object the candidate is about to publish, after the
            # (non-existent) previous root would have been moved aside.  The
            # corruption lands where the built release *points*, not where the
            # original file happens to sit, so the post-switch check sees it.
            if step == "switch_candidate_to_live":
                candidate_path = [path for path in root.parent.iterdir()
                                  if path.name.startswith(f".{root.name}.candidate-")][0]
                manifest = json.loads(
                    (candidate_path / "1077100" / "manifest.json").read_text(encoding="utf-8"))
                published = manifest["entries"][0]["artifact_sha256"]
                target = candidate_path / manifest["entries"][0]["object_path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"poisoned")

        with self.assertRaises(mod.GeneratedStoreError) as caught:
            with store.transaction(fault_inject=poison_before_the_second_move) as candidate:
                self.build([self.entry("text/9/title.unity3d", blob)], store=candidate)
        self.assertIn("previous root was restored", str(caught.exception))
        self.assertFalse(root.exists(),
                         "a first build that failed after the switch must leave no root behind")
        leftovers = [path.name for path in root.parent.iterdir()
                     if path.name.startswith(f".{root.name}.")]
        self.assertEqual(leftovers, [])


    # Source: archived main suite, lines 1168-1184; ownership: docs/GENERATED_STORE.md
    def test_a_failure_while_seeding_removes_the_candidate(self):
        """Staging happens before the block; a failure there must not leak a tree."""
        blob = self.blob("shared.unity3d", b"shared payload")
        self.build([self.entry("text/10/title.unity3d", blob)])

        def refuse_the_copy(step, index):
            if step == "copy_object":
                raise RuntimeError("seed refused")

        with self.assertRaisesRegex(RuntimeError, "seed refused"):
            with self.store.transaction(fault_inject=refuse_the_copy):
                self.fail("the block must not run when staging fails")

        leftovers = [path.name for path in self.root.parent.iterdir()
                     if path.name.startswith(f".{self.root.name}.")]
        self.assertEqual(leftovers, [])
        self.assertTrue(self.store.verify_release("1077100").ok)


    # Source: archived main suite, lines 1187-1205; ownership: docs/GENERATED_STORE.md
    def test_input_commits_must_descend_from_the_snapshot_commit(self):
        """A syntactically valid commit that is not an ancestor is still refused."""
        manifest = {"source_commit": SOURCE_COMMIT, "translation_commit": TRANSLATION_COMMIT,
                    "generated_commit": GENERATED_COMMIT}
        statuses = {TRANSLATION_COMMIT: "behind", GENERATED_COMMIT: "diverged"}
        problems = mod.check_release_commits(
            manifest, snapshot_commit=SNAPSHOT_COMMIT,
            fetchImpl=self.make_fetch({SOURCE_COMMIT: "identical", **statuses}))
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(all("does not contain" in problem for problem in problems), problems)
        self.assertTrue(any(TRANSLATION_COMMIT[:12] in problem for problem in problems))
        self.assertTrue(any(GENERATED_COMMIT[:12] in problem for problem in problems))

        ok = mod.check_release_commits(
            manifest, snapshot_commit=SNAPSHOT_COMMIT,
            fetchImpl=self.make_fetch({SOURCE_COMMIT: "identical",
                                       TRANSLATION_COMMIT: "ahead",
                                       GENERATED_COMMIT: "ahead"}))
        self.assertEqual(ok, [])


    # Source: archived main suite, lines 1207-1228; ownership: docs/GENERATED_STORE.md
    def test_an_unanswerable_comparison_is_not_a_pass(self):
        """404 / unreadable response / malformed snapshot all fail closed."""
        manifest = {"source_commit": SOURCE_COMMIT, "translation_commit": TRANSLATION_COMMIT,
                    "generated_commit": GENERATED_COMMIT}

        def not_found(url: str):
            return 404, {"message": "Not Found"}

        problems = mod.check_release_commits(manifest, snapshot_commit=SNAPSHOT_COMMIT,
                                             fetchImpl=not_found)
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(all("HTTP 404" in problem for problem in problems), problems)

        with self.assertRaises(mod.GeneratedStoreError):
            mod.check_release_commits(manifest, snapshot_commit="not-a-sha",
                                      fetchImpl=not_found)

        def wrong_shape(url: str):
            return 200, [1, 2, 3]

        with self.assertRaises(mod.ReleaseCommitError):
            mod.compare_commit_status(SOURCE_COMMIT, SNAPSHOT_COMMIT, fetchImpl=wrong_shape)


    # Source: archived main suite, lines 1230-1243; ownership: docs/GENERATED_STORE.md
    def test_a_later_snapshot_commit_is_accepted_for_an_earlier_generated_commit(self):
        """The rule that was withdrawn: generated_commit need not equal the read commit.

        A CI generates a release, commits it, and a bot commit lands on top; the
        manifest's generated_commit is then behind the branch head.  That is not
        an error -- what matters is that it is in the head's history.
        """
        manifest = {"source_commit": SOURCE_COMMIT, "translation_commit": TRANSLATION_COMMIT,
                    "generated_commit": GENERATED_COMMIT}
        fetch = self.make_fetch({SOURCE_COMMIT: "ahead", TRANSLATION_COMMIT: "ahead",
                                 GENERATED_COMMIT: "ahead"})
        self.assertEqual(mod.check_release_commits(manifest, snapshot_commit=SNAPSHOT_COMMIT,
                                                   fetchImpl=fetch), [])
        self.assertNotEqual(GENERATED_COMMIT, SNAPSHOT_COMMIT)


    # Source: archived main suite, lines 1245-1267; ownership: docs/GENERATED_STORE.md
    def test_a_store_checks_the_provenance_of_the_snapshot_it_was_given(self):
        """The snapshot is a fact of the read: passed in, never written to a manifest."""
        blob = self.blob("prov.unity3d", b"payload")
        self.build([self.entry("text/7/title.unity3d", blob)])
        manifest = self.store.load_manifest("1077100")
        self.assertNotIn("snapshot_commit", manifest,
                         "the manifest records the build's inputs, not the reader's commit")
        self.assertNotIn("snapshot_commit", manifest["entries"][0])

        store = mod.GeneratedStore(
            self.root, snapshot_commit=SNAPSHOT_COMMIT,
            provenance_fetch=self.make_fetch({SOURCE_COMMIT: "behind"}))
        report = store.verify_release("1077100")
        self.assertFalse(report.ok)
        self.assertTrue(any("snapshot commit" in failure for failure in report.failures),
                        report.failures)

        inherited = mod.GeneratedStore(
            self.root, snapshot_commit=SNAPSHOT_COMMIT,
            provenance_fetch=self.make_fetch({SOURCE_COMMIT: "identical",
                                              TRANSLATION_COMMIT: "ahead",
                                              GENERATED_COMMIT: "ahead"}))
        self.assertTrue(inherited.verify_release("1077100").ok)


    # Source: archived main suite, lines 1270-1297; ownership: docs/GENERATED_STORE.md
    def test_a_run_context_is_recorded_and_never_invented(self):
        """``ci_run_id`` is captured when there is one and stays null when there is not."""
        blob = self.blob("provided.unity3d", b"payload")
        self.build([self.entry("event/1/a.unity3d", blob)], ci_run_id="12345.2")
        manifest = json.loads(self.store.manifest_path("1077100").read_text(encoding="utf-8"))
        self.assertEqual(manifest["ci_run_id"], "12345.2")
        self.assertEqual(manifest["entries"][0]["ci_run_id"], "12345.2")
        self.assertTrue(self.store.verify_release("1077100").ok)

        # No caller-supplied id and no runner variable: the field is present and
        # null.  A fabricated value would be indistinguishable from a real one.
        self.store2 = mod.GeneratedStore(self.root / "generated2")
        with _no_runner_env():
            result = self.store2.build_release(
                "1077100",
                [dict(self.entry("event/1/b.unity3d", blob), artifact_file=str(blob))],
                source_client_version="9.0.200",
                source_commit=SOURCE_COMMIT,
                translation_commit=TRANSLATION_COMMIT,
                generated_commit=GENERATED_COMMIT,
                ci_run_id=None,
                entries_base=self.base,
            )
        self.assertTrue(result.written)
        other = json.loads(self.store2.manifest_path("1077100").read_text(encoding="utf-8"))
        self.assertIsNone(other["ci_run_id"])
        self.assertIsNone(other["entries"][0]["ci_run_id"])
        self.assertTrue(self.store2.verify_release("1077100").ok)


    # Source: archived main suite, lines 1299-1321; ownership: docs/GENERATED_STORE.md
    def test_the_runner_variable_is_used_when_no_id_is_passed(self):
        """A runner exporting ``GITHUB_RUN_ID`` cannot forget to carry it in."""
        blob = self.blob("env.unity3d", b"payload")
        store = mod.GeneratedStore(self.root / "generated3")
        environ = dict(os.environ)
        try:
            os.environ["GITHUB_RUN_ID"] = "987654321"
            os.environ.pop("MLTD_CI_RUN_ID", None)
            result = store.build_release(
                "1077100",
                [dict(self.entry("event/1/c.unity3d", blob), artifact_file=str(blob))],
                source_client_version="9.0.200",
                source_commit=SOURCE_COMMIT,
                translation_commit=TRANSLATION_COMMIT,
                generated_commit=GENERATED_COMMIT,
                entries_base=self.base,
            )
        finally:
            os.environ.clear()
            os.environ.update(environ)
        self.assertTrue(result.written)
        manifest = json.loads(store.manifest_path("1077100").read_text(encoding="utf-8"))
        self.assertEqual(manifest["ci_run_id"], "987654321")


    # Source: archived main suite, lines 1323-1332; ownership: docs/GENERATED_STORE.md
    def test_a_combined_or_malformed_run_id_is_refused(self):
        with self.assertRaises(mod.GeneratedStoreError):
            mod.validate_ci_run_id("9.0.200+1077100")
        with self.assertRaises(mod.GeneratedStoreError):
            mod.validate_ci_run_id("client-9.0.200-assets-1077100")
        with self.assertRaises(mod.GeneratedStoreError):
            mod.validate_ci_run_id("run-42")
        self.assertIsNone(mod.validate_ci_run_id(""))
        self.assertIsNone(mod.validate_ci_run_id(None))
        self.assertEqual(mod.validate_ci_run_id(" 42 "), "42")


    # Source: archived main suite, lines 1334-1347; ownership: docs/GENERATED_STORE.md
    def test_an_entry_must_declare_its_resource_kind(self):
        """A surface that forgot to classify its output fails the build.

        Defaulting to ``other`` would be the quiet version of the same bug: the
        manifest would be schema-valid and a consumer could not tell a real
        ``other`` from a missing classification.
        """
        blob = self.blob("unclassified.unity3d", b"payload")
        entry = self.entry("event/1/d.unity3d", blob)
        del entry["resource_kind"]
        with self.assertRaises(mod.GeneratedStoreError) as caught:
            self.build([entry])
        self.assertIn("resource_kind", str(caught.exception))
        self.assertFalse((self.root / "generated" / "1077100" / "manifest.json").exists())


    # Source: archived main suite, lines 1349-1361; ownership: docs/GENERATED_STORE.md
    def test_a_corrupt_retained_manifest_missing_the_run_id_fails_verification(self):
        """A manifest that predates the field is reported, not silently accepted."""
        blob = self.blob("legacy.unity3d", b"payload")
        self.build([self.entry("event/1/e.unity3d", blob)])
        path = self.store.manifest_path("1077100")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["kind"] = mod.MANIFEST_KIND  # unchanged; explicit for readability
        del document["ci_run_id"]
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report = self.store.verify_release("1077100")
        self.assertFalse(report.ok)
        self.assertTrue(any("ci_run_id" in failure for failure in report.failures),
                        f"expected a ci_run_id failure, got {report.failures}")


    # Source: archived main suite, lines 1364-1379; ownership: docs/GENERATED_STORE.md
    def test_runtime_path_is_kept_verbatim_and_verified(self):
        """An entry that declares runtime_path keeps it through build and verify.

        The client requests the runtime path (`production/2018/Android/<hash>.unity3d`),
        so dropping the field would silently break the distributor's first-class
        lookup key even though the build would still look successful.
        """
        runtime = "production/2018/Android/" + "ab" * 20 + ".unity3d"
        blob = self.blob("runtime.unity3d", b"runtime payload")
        entry = self.entry("production/2018/Android/birth_001_jp.gtx.unity3d", blob,
                           runtime_path=runtime)
        self.build([entry])
        manifest = self.store.load_manifest("1077100")
        self.assertEqual(manifest["entries"][0]["runtime_path"], runtime)
        report = self.store.verify_release("1077100")
        self.assertTrue(report.ok, report.failures)


    # Source: archived main suite, lines 1381-1388; ownership: docs/GENERATED_STORE.md
    def test_a_manifest_without_runtime_path_stays_valid(self):
        """runtime_path stays optional: an older entry simply omits it."""
        blob = self.blob("no-runtime.unity3d", b"no runtime payload")
        self.build([self.entry("production/2018/Android/old_001_jp.gtx.unity3d", blob)])
        manifest = self.store.load_manifest("1077100")
        self.assertNotIn("runtime_path", manifest["entries"][0])
        report = self.store.verify_release("1077100")
        self.assertTrue(report.ok, report.failures)


    # Source: archived main suite, lines 1390-1402; ownership: docs/GENERATED_STORE.md
    def test_unsafe_runtime_path_is_refused_before_anything_is_written(self):
        """A traversal/absolute/backslash runtime path fails the whole build.

        The build must leave no trace: refusing at admission means a caller
        cannot end up with a published release whose client path is unusable.
        """
        for bad in ("/abs/path.unity3d", "../up.unity3d", "dir\\file.unity3d", ""):
            with self.subTest(runtime_path=bad):
                blob = self.blob("unsafe.unity3d", b"payload")
                entry = self.entry("event/1/unsafe.unity3d", blob, runtime_path=bad)
                with self.assertRaises(mod.GeneratedStoreError):
                    self.build([entry])
                self.assertFalse(self.store.manifest_path("1077100").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
