#!/usr/bin/env python3
"""Regression suite for the unified generated-release materializer entry point.

Everything runs offline against ``tempfile`` directories; no game assets, no
git, no network.  The surface plugins are stub children that implement the
documented child contract (see ``scripts/materialize_generated_release.py``), so
the tests pin the entry point's own rules:

* a composite identity (``9.0.200+1077100``) is refused on either axis, and a
  non-digits ``asset_version`` is refused;
* a surface whose entry point is absent is reported ``not_implemented`` and the
  run fails closed -- checked against the **real** repository root, where the
  image-side entry point genuinely does not exist;
* ``generated/`` is never touched when any surface failed or is unimplemented: a
  failed run leaves an existing successful build byte-identical;
* a successful run produces a manifest whose entries carry every required field
  and whose objects exist at the recorded ``object_path``;
* re-running the same inputs is idempotent (same object paths, no new objects);
* a child that is not release-ready, that declares a non-publishable status, or
  that emits a duplicate ``logical_path`` fails the whole run closed.

Run: python scripts/test_materialize_generated_release.py
Also collected by: python -m pytest scripts/test_materialize_generated_release.py
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts import assets_generated_index as store_mod  # noqa: E402
from scripts import materialize_generated_release as mod  # noqa: E402

# 测试工具显式读取 legacy fixture；入口本身不得运行时导入它。
# 产品探针仅由本测试 CLI 的显式 pair 启用，不发现兄弟路径、不读取环境变量。
PRODUCT_WRITER_ROOT: Path | None = None
PRODUCT_WRITER_PIN: str | None = None
PROBE_EVIDENCE_DIR: Path | None = None

BLOCK_MAIN_WRITER = '''
import importlib.abc
import importlib
import json
import sys

class BlockMainWriter(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "scripts.assets_generated_index":
            raise ModuleNotFoundError("main writer import blocked", name=fullname)

sys.meta_path.insert(0, BlockMainWriter())
# 负控先证明阻断器生效，再在同一干净进程导入中央入口。
try:
    importlib.import_module("scripts.assets_generated_index")
except ModuleNotFoundError as exc:
    assert exc.name == "scripts.assets_generated_index"
else:
    raise AssertionError("main writer import was not blocked")
from scripts import materialize_generated_release as entry
assert "scripts.assets_generated_index" not in sys.modules
print("main writer import blocked: negative control passed", file=sys.stderr)
raise SystemExit(entry.main(json.loads(sys.argv[1])))
'''

# The entry point publishes through the store's staged-store transaction
# (``GeneratedStore.transaction(prune=...)``), which is a newer API than the
# entry point itself.  Against a store copy without it the entry point refuses
# every run by design -- the class below pins that refusal, and the rest of the
# suite is skipped with a reason instead of failing as if the entry point were
# broken.  (Run with the repository's own store: the API is present and every
# case below runs.)
def _store_has_transaction() -> bool:
    probe = store_mod.GeneratedStore.__new__(store_mod.GeneratedStore)
    return mod._transaction_supported(probe)


STORE_HAS_TRANSACTION = _store_has_transaction()
requires_transaction_store = unittest.skipUnless(
    STORE_HAS_TRANSACTION,
    "the store in this checkout predates GeneratedStore.transaction(prune=...); "
    "the entry point refuses such a store by design, and that refusal is pinned by "
    "test_a_store_without_the_transaction_api_is_refused")

try:  # optional: the schema conformance test is skipped when it is absent
    import jsonschema as _jsonschema
except ImportError:  # pragma: no cover - environment dependent
    _jsonschema = None

SOURCE_COMMIT = "a" * 40
TRANSLATION_COMMIT = "b" * 40
GENERATED_COMMIT = "c" * 40
ASSET_VERSION = "1077100"
CLIENT_VERSION = "9.0.200"
STUB_ENV = "MLTD_MATERIALIZE_STUB_MODE"
IMAGE_STUB_ENV = "MLTD_IMAGE_STUB_MODE"

# 真实 run 必须指明发布所用的 store 写者。本套件离线自足，因此 pin *本仓库自带*
# 写者：路径是本文件的同级，其字节 SHA-256 在 import 时读出，pin 真实（run 会核
# 验这些字节），但不依赖任何外部 checkout。各用例因此仍走同一写者——这两个参数
# 只切换来源，不削弱任何 admission/transaction 保证。
PINNED_WRITER_ROOT = REPO
PINNED_WRITER_PIN = hashlib.sha256(
    (REPO / "scripts" / "assets_generated_index.py").read_bytes()).hexdigest()

# A stub surface plugin implementing the child contract.  ``STUB_ENV`` switches
# behaviour so a single file can also act as a misbehaving child.
STUB_SURFACE = '''#!/usr/bin/env python3
"""Stub surface materializer for scripts/test_materialize_generated_release.py."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

MANIFEST_NAME = {manifest_name!r}
SURFACE = {surface!r}
# The real surfaces consume different source queues, so this stub only picks up
# the ledger rows that belong to its own surface.
SURFACE_TAG = {surface_tag!r}


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_rows(paths):
    rows = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-version", required=True)
    ap.add_argument("--asset-version", required=True)
    ap.add_argument("--translations", action="append", default=[])
    ap.add_argument("--output-root", required=True)
    ap.add_argument("--preflight-only", action="store_true")
    args = ap.parse_args()
    mode = os.environ.get({stub_env!r}, "ok")
    # When asked, record the fact that the child ran -- proving a bad writer
    # source was refused *before* any surface was spawned, not merely before the
    # store was written.
    marker = os.environ.get("MLTD_MATERIALIZE_SPAWN_MARKER")
    if marker:
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write(SURFACE + "\\n")
    rows = read_rows(args.translations)
    accepted = [row for row in rows
                if str(row.get("release_gate")) == "accepted"
                and (not row.get("surface_tags") or SURFACE_TAG in row["surface_tags"])]
    out = Path(args.output_root)

    if args.preflight_only:
        print(json.dumps({{"kind": SURFACE + "-preflight", "asset_version": args.asset_version,
                           "release_accepted": len(accepted), "rows": len(rows)}}, indent=2))
        return 0 if accepted else 3

    if mode == "crash":
        print(json.dumps({{"kind": SURFACE + "-materializer", "status": "refused",
                           "reason": "stub crash mode", "output_created": False}}, indent=2))
        return 4
    if not accepted:
        print(json.dumps({{"kind": SURFACE + "-materializer", "status": "refused",
                           "reason": "no gate-accepted translation rows",
                           "output_created": False}}, indent=2))
        return 4

    bundles = []
    scope = "jp-android"
    for index, row in enumerate(accepted):
        logical = str(row["logical"])
        if mode == "duplicate":
            logical = "shared_bundle.unity3d"
        remote = sha(("remote:" + logical).encode("utf-8"))[:40] + ".unity3d"
        if mode == "duplicate":
            # Same logical, same bytes: the entry point must refuse the duplicate
            # logical_path instead of silently storing one of the two.
            payload = "{{}}:{{}}".format(args.asset_version, logical)
        else:
            payload = "{{}}:{{}}:{{}}".format(args.asset_version, logical, row["translation"])
        artifact = out / scope / remote
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(payload.encode("utf-8"))
        bundle = {{
            "logical": logical, "remote": remote,
            "source_sha256": sha(("official:" + logical).encode("utf-8")),
            "localized_sha256": sha(payload.encode("utf-8")),
            "output_path": str(artifact),
        }}
        if mode == "declared_pending":
            bundle["translation_status"] = "pending"
        bundles.append(bundle)
    manifest = {{
        "schema_version": 1, "kind": SURFACE + "-stub-overlay", "scope": scope,
        "asset_version": args.asset_version, "app_version": args.client_version,
        "bundles_written": len(bundles),
        "release_ready": mode != "partial",
        "all_roundtrip_verified": True,
        "isolated_partial_trial": mode == "partial",
        "status": "isolated_candidate_not_published" if mode != "partial"
                  else "isolated_partial_trial_never_release",
        "bundles": bundles,
    }}
    (out / MANIFEST_NAME).parent.mkdir(parents=True, exist_ok=True)
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\\n", encoding="utf-8")
    print(json.dumps({{"kind": SURFACE + "-stub", "bundles_written": len(bundles)}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

STUB_IMAGE = '''#!/usr/bin/env python3
"""Stub image-side entry point for scripts/test_materialize_generated_release.py.

Implements the real injector's CLI surface: ``--all``/``--bundle``,
``--install-manifest``, ``--original-root``, ``--out``, ``--report``,
``--expect-manifest-sha256`` and the read-only ``--preflight-context``.
``MLTD_IMAGE_STUB_MODE`` switches the failure shapes the entry point must catch.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

MODE_ENV = "MLTD_IMAGE_STUB_MODE"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--bundle")
    ap.add_argument("--install-manifest", default=None)
    ap.add_argument("--original-root", default=None)
    ap.add_argument("--out")
    ap.add_argument("--report")
    ap.add_argument("--expect-manifest-sha256", default=None)
    ap.add_argument("--preflight-context", action="store_true")
    args = ap.parse_args()
    mode = os.environ.get(MODE_ENV, "ok")

    if args.preflight_context:
        if mode == "probe_crash":
            print("stub probe crashed on purpose", file=sys.stderr)
            return 9
        if mode == "probe_ignores":
            # An older entry point that does not know the flag: it answers with
            # its ordinary document.
            print(json.dumps({"kind": "stub-image", "bundles": 0}, indent=2))
            return 0
        manifest = args.install_manifest
        if mode == "probe_no_manifest":
            print(json.dumps({
                "kind": "mltd-image-injection-input-context",
                "mode": "preflight_context_no_bundles_written",
                "manifest": None, "manifest_sha256": None,
                "manifest_sources": [],
                "manifest_counts": None, "original_root": None,
                "refused": "no reviewed install manifest",
            }, indent=2))
            return 1
        if manifest and not Path(manifest).is_file():
            print(json.dumps({
                "kind": "mltd-image-injection-input-context",
                "mode": "preflight_context_no_bundles_written",
                "manifest": None, "manifest_sha256": None,
                "manifest_sources": [{"source": "--install-manifest / $MLTD_IMAGE_INSTALL_MANIFEST",
                                      "path": manifest}],
                "manifest_counts": None, "original_root": args.original_root,
                "refused": "install manifest does not exist: " + manifest,
            }, indent=2))
            return 1
        if not manifest:
            print(json.dumps({
                "kind": "mltd-image-injection-input-context",
                "mode": "preflight_context_no_bundles_written",
                "manifest": None, "manifest_sha256": None,
                "manifest_sources": [{"source": "repository default",
                                      "path": "work/agents/image-localization/"
                                              "reviewed937-texture-stage/"
                                              "texture-install-manifest.jsonl"}],
                "manifest_counts": None, "original_root": None,
                "refused": "no reviewed install manifest",
            }, indent=2))
            return 1
        digest = sha(Path(manifest).read_bytes())
        if mode == "probe_other_manifest":
            # The probe answers about some other cohort than the one it was given.
            manifest = "some/other/cohort.jsonl"
        if args.expect_manifest_sha256 and digest != args.expect_manifest_sha256:
            print("REFUSED: manifest sha256 mismatch", file=sys.stderr)
            return 1
        print(json.dumps({
            "kind": "mltd-image-injection-input-context",
            "mode": "preflight_context_no_bundles_written",
            "manifest": manifest, "manifest_sha256": digest,
            "manifest_sources": [{"source": "--install-manifest",
                                  "path": manifest}],
            "manifest_counts": {"rows": 2, "bundles": 2, "note": None},
            "original_root": args.original_root, "report": args.report,
        }, indent=2))
        return 0

    if args.out is None:
        print("REFUSED: --out is required with --all/--bundle", file=sys.stderr)
        return 1
    if mode == "no_report":
        print("stub wrote bundles but no report on purpose", file=sys.stderr)
        return 0
    if mode == "nonzero_with_json":
        print(json.dumps({"kind": "stub-image", "status": "refused",
                          "reason": "stub refused on purpose"}, indent=2))
        return 3
    if mode == "no_json":
        print("stub finished but printed no JSON on purpose", file=sys.stderr)
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    records = []
    for index in range(2):
        logical = "event_00%02d_info.unity3d" % (index + 1)
        remote = sha(("image:" + logical).encode("utf-8"))[:40] + ".unity3d"
        payload = "image-bundle:" + logical
        artifact = out / remote
        artifact.write_bytes(payload.encode("utf-8"))
        rows.append({
            "logical_key": logical,
            "logical_path": logical,
            "remote": remote,
            "source_sha256": sha(("image-official:" + logical).encode("utf-8")),
            "artifact_sha256": sha(payload.encode("utf-8")),
            "artifact_file": remote,
            "reuse_status": "exact",
            "translation_status": "accepted",
        })
        records.append({
            "bundle": logical,
            "remote": remote,
            "source_bundle": "official/" + remote,
            "source_sha256": rows[-1]["source_sha256"],
            "output_bundle": str(artifact),
            # `bad_audit` lies here; the independent audit re-reads the bytes.
            "output_sha256": ("0" * 64 if mode == "bad_audit"
                              else sha(payload.encode("utf-8"))),
            "texture_count": 1,
            "roundtrip_verified": True,
            "reused_existing": False,
        })
    if mode == "inventory_extra":
        # A bundle the staged run never produced and the audit never read.
        rows.append({
            "logical_key": "event_0099_info.unity3d",
            "logical_path": "event_0099_info.unity3d",
            "remote": "f" * 40 + ".unity3d",
            "source_sha256": sha(b"unproduced"),
            "artifact_sha256": sha(b"unproduced"),
            "artifact_file": "f" * 40 + ".unity3d",
            "reuse_status": "exact",
            "translation_status": "accepted",
        })
        (out / ("f" * 40 + ".unity3d")).write_bytes(b"unproduced")
    inventory = {"kind": "mltd-image-backfill-inventory", "bundles": rows}
    (out / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\\n", encoding="utf-8")
    if args.report:
        kind = ("mltd-image-backfill-inventory" if mode == "foreign_report_kind"
                else "reviewed-texture-injection-report")
        report = {"kind": kind, "schema_version": 1,
                  "mode": "isolated_reviewed_texture_injection_not_published",
                  "install_manifest": args.install_manifest,
                  "inventory": str(out / "inventory.json"),
                  "bundles": records}
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2) + "\\n", encoding="utf-8")
    print(json.dumps({"kind": "stub-image", "bundles": len(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

STUB_IMAGE_AUDIT = '''#!/usr/bin/env python3
"""Stub independent re-audit for scripts/test_materialize_generated_release.py.

It trusts nothing but the report's paths and SHA-256 values, exactly like the
real ``verify_bundle_repack.py``: every ``output_bundle`` is re-read from disk
and re-hashed against the recorded ``output_sha256``.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--install-manifest", default=None)
    ap.add_argument("--audit", default=None)
    ap.add_argument("--allow-partial", action="store_true")
    args = ap.parse_args()
    mode = os.environ.get("MLTD_IMAGE_STUB_MODE", "ok")
    report_path = Path(args.report)
    if not report_path.is_file():
        print("REFUSED: report does not exist", file=sys.stderr)
        return 1
    document = json.loads(report_path.read_text(encoding="utf-8"))
    records = document.get("bundles") or []
    errors = []
    passed = []
    for record in records:
        output = Path(str(record["output_bundle"]))
        if not output.is_file() or sha(output) != record["output_sha256"]:
            errors.append({"bundle": record.get("bundle"),
                           "error": "archive does not hash to the recorded output_sha256"})
            continue
        passed.append({"remote": record.get("remote")})
    if mode == "audit_missing_bundle":
        # Audits only the first bundle yet claims success: the entry point must
        # notice that the audit no longer covers the run.
        passed = passed[:1]
    audit = {
        "kind": "independent-reviewed-image-unity-repack-audit",
        "report_audited": ("some-other-report.json" if mode == "audit_foreign_report"
                           else str(report_path)),
        "report_audited_sha256": sha(report_path),
        "install_manifest": str(args.install_manifest),
        "audited_bundles": len(records),
        "passed_bundles": len(passed),
        "failed_bundles": len(errors),
        "target_textures": len(passed),
        "untouched_unity_objects_byte_identical": 0,
        "errors": errors,
        "scope": "isolated candidate only",
    }
    if mode == "audit_empty":
        audit = {}
    if mode == "audit_no_counts":
        for field in ("audited_bundles", "passed_bundles", "failed_bundles", "target_textures"):
            audit.pop(field, None)
    if mode == "audit_no_textures":
        audit["target_textures"] = 0
    audit_path = Path(args.audit) if args.audit else report_path.parent / "audit.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, indent=2) + "\\n", encoding="utf-8")
    print(json.dumps(audit, indent=2))
    print("AUDIT", audit_path)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
'''

# A minimal stub store module, used only to prove the writer API is checked
# before anything is spawned or written.  It carries the module-level API this
# entry point needs but a ``GeneratedStore`` that cannot transact, so the
# missing-API refusal is exercised without copying the whole writer.
STUB_STORE_MISSING_TRANSACTION = '''#!/usr/bin/env python3
"""Stub store module for scripts/test_materialize_generated_release.py."""
import hashlib
from pathlib import Path


class GeneratedStoreError(RuntimeError):
    pass


class GeneratedStore:
    def __init__(self, root):
        self.root = Path(root)

    # Deliberately no ``transaction``: the entry point must refuse this API.


class ReuseLedger:
    def __init__(self, previous, verified):
        self.previous = previous
        self.verified = verified


ADMISSIBLE_REUSE_STATUSES = ("exact", "verified-compatible")
ADMISSIBLE_TRANSLATION_STATUSES = ("accepted", "modified", "reused")
RESOURCE_KIND_BUNDLE = "bundle"
RESOURCE_KIND_OTHER = "other"
RESOURCE_KIND_TEXTURE = "texture"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_asset_version(value):
    return str(value)


def validate_source_client_version(value):
    return str(value)


def validate_commit(value, field_name):
    return str(value)


def validate_ci_run_id(value):
    return value
'''

# A stub store module whose ``GeneratedStore`` exposes ``build_release`` as a
# non-callable (``None``) and whose ``transaction`` has no ``prune`` parameter:
# both must be caught *before* any store is constructed, temp dir made, or
# surface spawned.  The writer body never runs past the import check.
STUB_STORE_BAD_CALLABLE = '''#!/usr/bin/env python3
"""Stub store module with a non-callable API member."""
import hashlib
from pathlib import Path


class GeneratedStoreError(RuntimeError):
    pass


class GeneratedStore:
    def __init__(self, root):
        raise AssertionError("a refused writer's GeneratedStore must not be constructed")

    build_release = None  # non-callable
    manifest_path = None
    checksums_path = None
    load_manifest = None
    put_object = None
    verify_release = None

    def transaction(self):  # no ``prune`` parameter
        raise AssertionError("must not be called")


class ReuseLedger:
    def __init__(self, previous, verified):
        pass


ADMISSIBLE_REUSE_STATUSES = ("exact", "verified-compatible")
ADMISSIBLE_TRANSLATION_STATUSES = ("accepted", "modified", "reused")
RESOURCE_KIND_BUNDLE = "bundle"
RESOURCE_KIND_OTHER = "other"
RESOURCE_KIND_TEXTURE = "texture"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_asset_version(value):
    return str(value)


def validate_source_client_version(value):
    return str(value)


def validate_commit(value, field_name):
    return str(value)


def validate_ci_run_id(value):
    return value
'''

# A stub store module whose ``GeneratedStore`` has every method callable but a
# ``transaction`` with no ``prune`` parameter: the class-level signature check
# must refuse it, again before construction/mkdtemp/spawn.
STUB_STORE_BAD_TXN = '''#!/usr/bin/env python3
"""Stub store module with a callable-but-wrong transaction signature."""
import hashlib
from pathlib import Path


class GeneratedStoreError(RuntimeError):
    pass


class GeneratedStore:
    def __init__(self, root):
        raise AssertionError("a refused writer's GeneratedStore must not be constructed")

    def manifest_path(self, v):
        return Path(v)

    def checksums_path(self, v):
        return Path(v)

    def load_manifest(self, v):
        return {}

    def build_release(self, *a, **k):
        raise AssertionError("must not be called")

    def put_object(self, *a, **k):
        raise AssertionError("must not be called")

    def verify_release(self, *a, **k):
        raise AssertionError("must not be called")

    def transaction(self):  # callable, but no ``prune`` parameter
        raise AssertionError("must not be called")


class ReuseLedger:
    def __init__(self, previous, verified):
        pass


ADMISSIBLE_REUSE_STATUSES = ("exact", "verified-compatible")
ADMISSIBLE_TRANSLATION_STATUSES = ("accepted", "modified", "reused")
RESOURCE_KIND_BUNDLE = "bundle"
RESOURCE_KIND_OTHER = "other"
RESOURCE_KIND_TEXTURE = "texture"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_asset_version(value):
    return str(value)


def validate_source_client_version(value):
    return str(value)


def validate_commit(value, field_name):
    return str(value)


def validate_ci_run_id(value):
    return value
'''

REQUIRED_ENTRY_FIELDS = (
    "channel",
    "asset_version",
    "client_version",
    "source_client_version",
    "source_commit",
    "translation_commit",
    "generated_commit",
    "logical_key",
    "logical_path",
    "runtime_path",
    "source_sha256",
    "translated_sha256",
    "object_path",
    "artifact_sha256",
    "reuse_status",
    "translation_status",
)


def digest_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@requires_transaction_store
class MaterializeTestCase(unittest.TestCase):
    """Common harness: a stub surface repository plus an isolated output root.

    Skipped as a whole when the store in this checkout predates the staged-store
    transaction: the entry point refuses such a store by design (pinned by
    ``TestStoreWithoutTransaction``), so every case here would be exercising the
    refusal instead of the behaviour it names.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="materialize-generated-test-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.stub_repo = self.base / "stub-repo"
        (self.stub_repo / "scripts").mkdir(parents=True)
        (self.stub_repo / "scripts" / "materialize_event_unit_90200.py").write_text(
            STUB_SURFACE.format(manifest_name="event-unit-90200-manifest.json",
                                surface="stub-event-unit", surface_tag=mod.SURFACE_TEXT_EVENT_UNIT,
                                stub_env=STUB_ENV),
            encoding="utf-8")
        (self.stub_repo / "scripts" / "materialize_mld_90200.py").write_text(
            STUB_SURFACE.format(manifest_name="mld-90200-manifest.json",
                                surface="stub-mld", surface_tag=mod.SURFACE_TEXT_MLD,
                                stub_env=STUB_ENV),
            encoding="utf-8")
        # The stub surfaces live at the *default* image entry/audit paths, which
        # are this repository's own tools/mltd_image_localization/ layout.
        image_dir = self.stub_repo / "tools" / "mltd_image_localization"
        image_dir.mkdir(parents=True)
        (image_dir / "inject_reviewed_textures.py").write_text(STUB_IMAGE, encoding="utf-8")
        (image_dir / "verify_bundle_repack.py").write_text(STUB_IMAGE_AUDIT, encoding="utf-8")
        # An Assets-repository-shaped override, for the explicit --image-entry path.
        (self.stub_repo / "pipelines" / "image").mkdir(parents=True)
        (self.stub_repo / "pipelines" / "image" / "inject_reviewed_textures.py").write_text(
            STUB_IMAGE, encoding="utf-8")
        (self.stub_repo / "pipelines" / "image" / "verify_bundle_repack.py").write_text(
            STUB_IMAGE_AUDIT, encoding="utf-8")

        self.input_root = self.base / "inputs"
        self.input_root.mkdir()
        self.output_root = self.base / "out"
        self.store_root = self.output_root / "generated"
        self.report_path = self.base / "report.json"
        # The reviewed image cohort is not in this repository's commit contract;
        # every image-surface test supplies its own manifest + original root.
        self.reviewed_manifest = self.base / "texture-install-manifest.jsonl"
        self.reviewed_manifest.write_text(
            json.dumps({"remote": "b32e11c51e6713bd7581d8e3bec12e3ff5a63fe0.unity3d",
                        "texture_path_id": 1, "review_status":
                        "user_approved_for_isolated_install_staging"},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        self.original_root = self.base / "original-root"
        self.original_root.mkdir()

    # -- helpers ---------------------------------------------------------- #
    def ledger_tagged(self, rows: list[tuple[str, str, str | None]],
                      name: str = "ledger.jsonl") -> Path:
        """Write a ledger whose rows say which surface they belong to.

        The real materializers consume disjoint source queues (event-unit talks
        vs the MD.mld data_map), so one accepted ledger legitimately feeds
        several surfaces without any bundle being produced twice.
        """
        path = self.input_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(
            json.dumps({"logical": logical, "translation": translation,
                        "release_gate": "accepted",
                        "surface_tags": [tag] if tag else None},
                       ensure_ascii=False) + "\n"
            for logical, translation, tag in rows), encoding="utf-8")
        return path

    def ledger(self, rows: list[tuple[str, str]], name: str = "ledger.jsonl",
               surface_tags: str | list[str] | None = None) -> Path:
        tags = surface_tags if isinstance(surface_tags, list) else (
            [surface_tags] if surface_tags else None)
        return self.ledger_tagged([(logical, translation,
                                    tags[0] if tags and len(tags) == 1 else None)
                                   for logical, translation in rows], name=name)

    def complete_ledger(self, name: str = "ledger.jsonl") -> Path:
        return self.ledger_tagged([
            ("event_unit_talk_1000.unity3d", "第一句", mod.SURFACE_TEXT_EVENT_UNIT),
            ("event_unit_talk_1001.unity3d", "第二句", mod.SURFACE_TEXT_EVENT_UNIT),
        ], name=name)

    def mld_ledger(self, name: str = "mld.jsonl") -> Path:
        return self.ledger_tagged([("md.mld", "第三句", mod.SURFACE_TEXT_MLD)], name=name)

    def full_ledger(self, name: str = "ledger.jsonl") -> Path:
        """Rows for every text surface, so one ledger serves text and image."""
        return self.ledger_tagged([
            ("event_unit_talk_1000.unity3d", "第一句", mod.SURFACE_TEXT_EVENT_UNIT),
            ("event_unit_talk_1001.unity3d", "第二句", mod.SURFACE_TEXT_EVENT_UNIT),
            ("md.mld", "第三句", mod.SURFACE_TEXT_MLD),
        ], name=name)

    def argv(self, *, repository_root: Path | None = None, surfaces: str = "text-event-unit",
             ledger: Path | None = None, extra: list[str] | None = None,
             omit: str | None = None, image_inputs: bool | None = None,
             writer: str = "pinned") -> list[str]:
        """Build an argv, optionally replacing/omitting one option's value.

        ``omit`` names the option whose *value* is dropped so a caller can append
        its own (used to exercise refused identities without duplicated flags).
        ``image_inputs`` adds the reviewed image manifest/root; it defaults to
        "whenever the image surface is requested" so tests exercise the wired
        path, and can be set to ``False`` to exercise the refusal.

        ``writer`` 选择本次 run 的写者来源形态：``"pinned"``（默认整对，指向本仓库
        自带 store）、``"none"``（两参数都缺）、``"root_only"``/``"pin_only"``
        （半对）、``"bad_pin"``（非 64-hex）、``"wrong_pin"``（64-hex 但与字节不
        符）或 ``"bad_root"``（root 内无写者文件）。
        """
        writer_args: list[tuple[str, str] | tuple[str]] = []
        if writer == "pinned":
            writer_args = [("--assets-writer-root", str(PINNED_WRITER_ROOT)),
                           ("--assets-writer-pin", PINNED_WRITER_PIN)]
        elif writer == "none":
            writer_args = []
        elif writer == "root_only":
            writer_args = [("--assets-writer-root", str(PINNED_WRITER_ROOT))]
        elif writer == "pin_only":
            writer_args = [("--assets-writer-pin", PINNED_WRITER_PIN)]
        elif writer == "bad_pin":
            writer_args = [("--assets-writer-root", str(PINNED_WRITER_ROOT)),
                           ("--assets-writer-pin", "not-a-sha")]
        elif writer == "wrong_pin":
            writer_args = [("--assets-writer-root", str(PINNED_WRITER_ROOT)),
                           ("--assets-writer-pin", "0" * 64)]
        elif writer == "bad_root":
            empty_root = self.base / "empty-writer-root"
            empty_root.mkdir(exist_ok=True)
            writer_args = [("--assets-writer-root", str(empty_root)),
                           ("--assets-writer-pin", PINNED_WRITER_PIN)]
        else:
            raise ValueError(f"unknown writer shape {writer!r}")
        pairs = [
            ("--asset-version", ASSET_VERSION),
            ("--source-client-version", CLIENT_VERSION),
            ("--source-commit", SOURCE_COMMIT),
            ("--translation-commit", TRANSLATION_COMMIT),
            ("--generated-commit", GENERATED_COMMIT),
            ("--input-root", str(self.input_root)),
            ("--output-root", str(self.output_root)),
            ("--repository-root", str(repository_root or self.stub_repo)),
            ("--surfaces", surfaces),
            ("--report", str(self.report_path)),
        ]
        args: list[str] = []
        for name, value in pairs:
            if omit is not None and name == omit:
                continue
            args.extend([name, value])
        for pair in writer_args:
            args.extend(pair)
        if ledger is not None:
            args.extend(["--translated-ledger", str(ledger)])
        if image_inputs is None:
            image_inputs = "image" in [part.strip() for part in surfaces.split(",")]
        if image_inputs:
            args.extend(["--image-install-manifest", str(self.reviewed_manifest),
                         "--image-original-root", str(self.original_root)])
        args.extend(extra or [])
        return args

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mod.main(argv)
        return code, out.getvalue(), err.getvalue()

    def report(self) -> dict:
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def tree_snapshot(self) -> dict[str, str]:
        if not self.store_root.exists():
            return {}
        return {
            path.relative_to(self.store_root).as_posix(): store_mod.sha256_file(path)
            for path in sorted(self.store_root.rglob("*")) if path.is_file()
        }

    def stub(self, mode: str, **kwargs):
        env = dict(os.environ)
        env[STUB_ENV] = mode
        return mock.patch.dict(os.environ, env, clear=False)

    def stub_image(self, mode: str, **kwargs):
        env = dict(os.environ)
        env[IMAGE_STUB_ENV] = mode
        return mock.patch.dict(os.environ, env, clear=False)

    # -- 1. version axes ---------------------------------------------------- #
    def test_composite_asset_version_is_refused(self):
        argv = self.argv(ledger=self.complete_ledger(), omit="--asset-version",
                         extra=["--asset-version", "9.0.200+1077100"])
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("independent axes", err)
        self.assertIn("forbidden", err)
        self.assertFalse(self.output_root.exists(),
                         "a refused identity must not create the output root")
        report = self.report()
        self.assertEqual(report["build_status"], "failed")
        self.assertEqual(report["mode"], "refused")
        self.assertFalse(report["generated_written"])
        self.assertEqual(report["asset_version"], "9.0.200+1077100")
        self.assertEqual(report["surfaces"], [])

    def test_composite_source_client_version_is_refused(self):
        argv = self.argv(ledger=self.complete_ledger(), omit="--source-client-version",
                         extra=["--source-client-version",
                                "client-9.0.200-assets-1077100"])
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("independent axes", err)
        self.assertFalse(self.output_root.exists())

    def test_non_digit_asset_version_is_refused(self):
        argv = self.argv(ledger=self.complete_ledger(), omit="--asset-version",
                         extra=["--asset-version", "10771a0"])
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("digits only", err)
        self.assertFalse(self.output_root.exists())

    def test_malformed_client_version_is_refused(self):
        argv = self.argv(ledger=self.complete_ledger(), omit="--source-client-version",
                         extra=["--source-client-version", "9.0"])
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("X.Y.Z", err)

    def test_malformed_commit_is_refused(self):
        argv = self.argv(ledger=self.complete_ledger(), omit="--source-commit",
                         extra=["--source-commit", "deadbeef"])
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("40-char hex", err)
        self.assertFalse(self.output_root.exists())

    # -- 2. not_implemented surface ---------------------------------------- #
    def test_image_surface_without_a_reviewed_manifest_is_not_implemented(self):
        """Outside a runner the reviewed cohort is still missing: named, not guessed.

        Against the **real** repository root the injector now exists at its home
        path, so the surface gets past the entry-point check and stops at the
        input check instead: the reviewed install manifest is a build-run
        artifact, and the run must say so (with every source it consulted)
        rather than inject from the entry point's ``work/`` default.
        """
        code, _out, err = self.run_cli(self.argv(
            repository_root=REPO, surfaces="image", ledger=self.complete_ledger(),
            image_inputs=False))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        report = self.report()
        self.assertEqual(report["build_status"], "not_implemented")
        self.assertFalse(report["generated_written"])
        surface = report["surfaces"][0]
        self.assertEqual(surface["surface"], "image")
        self.assertEqual(surface["build_status"], "not_implemented")
        self.assertIn("--image-install-manifest", surface["reason"])
        self.assertIn("reviewed937-texture-stage", surface["reason"])
        self.assertEqual(surface["entry_point"]["exists"], True)
        self.assertFalse(self.store_root.exists(),
                         "generated/ must not exist after a not_implemented surface")

    def test_missing_image_entry_point_is_reported_not_implemented(self):
        # An entry point that is genuinely absent (the Assets repository calls
        # it pipelines/image/...): the surface is not_implemented, never skipped.
        code, _out, err = self.run_cli(self.argv(
            ledger=self.full_ledger(), surfaces="image",
            extra=["--image-entry", "pipelines/image/absent_injector.py"]))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        surface = self.report()["surfaces"][0]
        self.assertEqual(surface["build_status"], "not_implemented")
        self.assertIn("the image-side entry point is absent", surface["reason"])
        self.assertIn("absent_injector.py", surface["reason"])
        self.assertIn("verify_bundle_repack.py", surface["reason"])

    def test_missing_image_audit_is_reported_not_implemented(self):
        """Re-hashing the inventory is not an independent audit of the repack."""
        code, _out, err = self.run_cli(self.argv(
            ledger=self.full_ledger(), surfaces="image",
            extra=["--image-audit", "pipelines/image/absent_audit.py"]))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        surface = self.report()["surfaces"][0]
        self.assertEqual(surface["build_status"], "not_implemented")
        self.assertIn("independent re-audit is absent", surface["reason"])

    def test_not_implemented_output_root_is_untouched_but_an_existing_store_survives(self):
        code, _out, err = self.run_cli(self.argv(
            ledger=self.full_ledger(),
            surfaces="text-event-unit,text-mld,image"))
        self.assertEqual(code, mod.EXIT_OK, err)
        before = self.tree_snapshot()
        self.assertTrue(before, "the successful run must have written a store")

        # Now drop the reviewed manifest the image surface needs.
        code, _out, err = self.run_cli(self.argv(
            ledger=self.ledger([("event_unit_talk_1000.unity3d", "改过的译文")],
                               "second.jsonl",
                               surface_tags=mod.SURFACE_TEXT_EVENT_UNIT),
            surfaces="text-event-unit,text-mld,image", image_inputs=False))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertEqual(self.report()["build_status"], "not_implemented")
        self.assertEqual(self.tree_snapshot(), before,
                         "a not_implemented surface must leave generated/ byte-identical")

    # -- 3. the image surface's wiring -------------------------------------- #
    def image_surface(self) -> dict:
        return self.report()["surfaces"][-1]

    def test_the_image_surface_runs_the_injector_and_its_independent_audit(self):
        """Both are executed, and only the audit's verdict admits the bundles."""
        code, _out, err = self.run_cli(self.argv(
            ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_OK, err)
        surface = self.image_surface()
        self.assertEqual(surface["build_status"], "success")
        argv = surface["entry_point"]["argv"]
        # The reviewed inputs are passed explicitly and the SHA the input
        # context reported is pinned onto the injector's own argv.
        self.assertIn("--install-manifest", argv)
        self.assertIn(str(self.reviewed_manifest), argv)
        self.assertIn("--original-root", argv)
        self.assertIn(str(self.original_root), argv)
        self.assertIn("--report", argv)
        index = argv.index("--expect-manifest-sha256")
        self.assertEqual(argv[index + 1], store_mod.sha256_file(self.reviewed_manifest))
        audit = surface["provenance"]
        self.assertEqual(audit["audit_failed_bundles"], 0)
        self.assertEqual(audit["audit_passed_bundles"], 2)
        self.assertEqual(audit["input_context"]["manifest_counts"]["bundles"], 2)
        # The audit report itself is a staging artifact (the staging tree is
        # removed with the run); its recorded digest is what lands in the store.
        self.assertEqual(len(audit["audit_sha256"]), 64)
        self.assertIn("materialize-generated-", audit["audit_path"])
        # The two are distinct programs: the audit is a second entry point, not a
        # re-read of the inventory by the injector itself.
        roles = {item["role"]: item["path"] for item in surface["inputs"]}
        self.assertIn("surface entry point", roles)
        self.assertIn("independent re-audit", roles)
        self.assertNotEqual(roles["surface entry point"], roles["independent re-audit"])

    def test_an_injector_run_that_fails_the_audit_is_not_published(self):
        with self.stub_image("bad_audit"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        surface = self.image_surface()
        self.assertEqual(surface["build_status"], "failed")
        self.assertIn("independent re-audit", surface["reason"])
        self.assertFalse(self.store_root.exists(),
                         "a failed audit must not publish the surface")

    def test_an_injector_that_writes_no_report_is_not_published(self):
        with self.stub_image("no_report"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertIn("did not write the document it was asked for",
                      self.image_surface()["reason"])

    def test_a_nonzero_injector_exit_is_a_failure_even_with_a_json_document(self):
        """The exit code decides; the document only supplies the reason."""
        with self.stub_image("nonzero_with_json"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        reason = self.image_surface()["reason"]
        self.assertIn("exited 3", reason)
        self.assertIn("stub refused on purpose", reason)

    def test_an_injector_that_prints_no_json_is_a_failure(self):
        """Exiting 0 is not enough: the run report is the audit's only input."""
        with self.stub_image("no_json"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertIn("did not write the document it was asked for",
                      self.image_surface()["reason"])

    def test_a_foreign_run_report_kind_is_not_published(self):
        with self.stub_image("foreign_report_kind"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertIn("has kind 'mltd-image-backfill-inventory'",
                      self.image_surface()["reason"])

    # -- 3b. the entry point's own fail-open guards ------------------------- #
    def test_an_entry_point_whose_probe_crashes_is_never_preflight_success(self):
        """A probe that cannot answer must not be read as "inputs are fine"."""
        with self.stub_image("probe_crash"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        surface = self.image_surface()
        self.assertEqual(surface["build_status"], "not_implemented")
        self.assertIn("could not be established", surface["reason"])
        self.assertIn("exited 9", surface["reason"])
        self.assertEqual(surface["entry_count"], 0)
        self.assertFalse(self.store_root.exists())

    def test_a_probe_that_answers_without_a_manifest_is_not_success(self):
        with self.stub_image("probe_no_manifest"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        surface = self.image_surface()
        self.assertEqual(surface["build_status"], "not_implemented")
        self.assertIn("exited 1", surface["reason"])
        self.assertIn("no reviewed install manifest", surface["reason"])

    def test_an_old_entry_point_without_the_probe_flag_is_not_success(self):
        """An entry point that ignores --preflight-context never proves anything."""
        with self.stub_image("probe_ignores"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        surface = self.image_surface()
        self.assertEqual(surface["build_status"], "not_implemented")
        self.assertIn("unexpected document kind", surface["reason"])

    def test_a_probe_that_resolves_a_different_cohort_is_refused(self):
        """On a real run the cohort is the file this run was given, not whatever resolved."""
        other = self.base / "other-manifest.jsonl"
        other.write_text(self.reviewed_manifest.read_text(encoding="utf-8"), encoding="utf-8")
        self.reviewed_manifest = other
        with self.stub_image("probe_other_manifest"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        surface = self.image_surface()
        self.assertEqual(surface["build_status"], "not_implemented")
        self.assertIn("not the reviewed cohort this run was given", surface["reason"])

    def test_an_empty_audit_document_is_not_a_pass(self):
        """``{}`` must never read as "zero failures"."""
        with self.stub_image("audit_empty"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertIn("has kind None", self.image_surface()["reason"])

    def test_an_audit_of_a_different_report_is_not_a_pass(self):
        with self.stub_image("audit_foreign_report"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertIn("not this run's report", self.image_surface()["reason"])

    def test_an_audit_that_misses_a_bundle_is_not_a_pass(self):
        """A partial audit must not authorise the bundles it never read.

        The entry point measures the audit against this run's own report, so a
        "successful" audit that covered fewer bundles than the report carries is
        refused on the count, not silently accepted as complete.
        """
        with self.stub_image("audit_missing_bundle"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        reason = self.image_surface()["reason"]
        self.assertIn("audited", reason)
        self.assertIn("2", reason)

    def test_an_audit_without_counts_is_not_a_pass(self):
        with self.stub_image("audit_no_counts"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertIn("not a count", self.image_surface()["reason"])

    def test_an_audit_with_no_audited_textures_is_not_a_pass(self):
        with self.stub_image("audit_no_textures"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertIn("no audited texture", self.image_surface()["reason"])

    def test_an_inventory_that_adds_a_never_audited_bundle_is_not_published(self):
        """The published set must be exactly the audited set."""
        with self.stub_image("inventory_extra"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.full_ledger(), surfaces="image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        reason = self.image_surface()["reason"]
        self.assertIn("audited bundles and the inventory disagree", reason)
        self.assertIn("never audited", reason)
        self.assertFalse(self.store_root.exists())

    def test_preflight_only_proves_the_image_inputs_resolve(self):
        code, out, err = self.run_cli(self.argv(
            ledger=self.full_ledger(), surfaces="image", extra=["--preflight-only"]))
        self.assertEqual(code, mod.EXIT_OK, err)
        surface = self.image_surface()
        self.assertEqual(surface["build_status"], "success")
        self.assertIn("input_context_only", surface["preflight_checked"])
        self.assertEqual(surface["entry_count"], 0)

    # -- 4. successful run -------------------------------------------------- #
    def test_successful_run_writes_object_addressed_release(self):
        # Both text surfaces consume the same accepted ledger, mirroring the real
        # contract (one text owner ledger; each materializer reads its own queue).
        ledger = self.full_ledger()
        code, out, err = self.run_cli(self.argv(
            ledger=ledger, surfaces="text-event-unit,text-mld,image"))
        self.assertEqual(code, mod.EXIT_OK, err)
        # 2 event-unit + 1 MD.mld + 2 image; the stub surfaces keep only the
        # ledger rows tagged for them, exactly like the real source queues.
        self._assert_successful_release(expect_entries=5)

    def _assert_successful_release(self, *, expect_entries: int):
        """Assert the store/report shape of a successful run."""
        payload = self.report()
        self.assertEqual(payload["build_status"], "success")
        self.assertTrue(payload["generated_written"])
        self.assertTrue(payload["store"]["verify_ok"], payload["store"]["verify_failures"])

        store = store_mod.GeneratedStore(self.store_root)
        manifest = store.load_manifest(ASSET_VERSION)
        entries = manifest["entries"]
        self.assertEqual(len(entries), expect_entries)
        self.assertEqual(manifest["asset_version"], ASSET_VERSION)
        self.assertIsNone(manifest["client_version"])
        self.assertEqual(manifest["source_client_version"], CLIENT_VERSION)
        self.assertEqual(manifest["entry_count"], len(entries))
        self.assertEqual(manifest["reuse_summary"]["rejected_entries"], 0)

        logical_paths = [entry["logical_path"] for entry in entries]
        self.assertEqual(logical_paths, sorted(logical_paths))
        self.assertEqual(len(set(logical_paths)), len(logical_paths))
        for entry in entries:
            for field in REQUIRED_ENTRY_FIELDS:
                self.assertIn(field, entry)
            self.assertEqual(entry["channel"], "assets")
            self.assertEqual(entry["asset_version"], ASSET_VERSION)
            self.assertIsNone(entry["client_version"])
            self.assertEqual(entry["source_client_version"], CLIENT_VERSION)
            self.assertEqual(entry["source_commit"], SOURCE_COMMIT)
            self.assertEqual(entry["translation_commit"], TRANSLATION_COMMIT)
            self.assertEqual(entry["generated_commit"], GENERATED_COMMIT)
            self.assertEqual(entry["reuse_status"], "exact", "a baseline build is exact")
            self.assertEqual(entry["translation_status"], "modified")
            self.assertEqual(entry["translated_sha256"], entry["artifact_sha256"])
            self.assertEqual(
                entry["object_path"],
                store_mod.cas_object_path(entry["artifact_sha256"]).as_posix())
            self.assertTrue(store.object_path(entry["artifact_sha256"]).is_file(),
                            "every recorded object_path must exist")
            self.assertEqual(store_mod.sha256_file(
                store.object_path(entry["artifact_sha256"])), entry["artifact_sha256"])

        # The default logical_path is the resource path inside one asset_version.
        self.assertTrue(all(entry["logical_path"].startswith("production/2018/Android/")
                            for entry in entries), logical_paths)
        self.assertTrue(store.verify_release(ASSET_VERSION).ok)
        self.assertTrue(store.checksums_path(ASSET_VERSION).is_file())

        # The build report is the CI's commit-or-don't input.
        report = self.report()
        for surface in report["surfaces"]:
            self.assertEqual(surface["build_status"], "success", surface["reason"])
            self.assertTrue(surface["entry_point"]["sha256"])
            self.assertTrue(surface["entry_point"]["argv"])
            self.assertEqual(surface["entry_point"]["exit_code"], 0)
            self.assertEqual(len(surface["entries"]), surface["entry_count"])
            for entry in surface["entries"]:
                self.assertEqual(len(entry["artifact_sha256"]), 64)
                self.assertTrue(Path(entry["artifact_path"]).is_file()
                                or "materialize-generated-" in entry["artifact_path"]
                                or entry["artifact_path"])
                self.assertTrue(entry["object_path"].startswith("objects/sha256/"))
            for item in surface["inputs"]:
                self.assertIn("path", item)
                self.assertIn("role", item)

    def test_object_paths_do_not_collide_when_two_surfaces_emit_equal_bytes(self):
        # Identical bytes must be stored exactly once (content addressing).
        ledger = self.full_ledger()
        code, _out, err = self.run_cli(self.argv(
            ledger=ledger, surfaces="text-event-unit,text-mld,image"))
        self.assertEqual(code, mod.EXIT_OK, err)
        store = store_mod.GeneratedStore(self.store_root)
        objects = [path.name for path in store.iter_objects()]
        manifest = store.load_manifest(ASSET_VERSION)
        self.assertEqual(len(objects), len(set(objects)))
        self.assertEqual(sorted(objects),
                         sorted({entry["artifact_sha256"] for entry in manifest["entries"]}))
        self.assertEqual(store.find_orphans(), [])

    def test_preflight_only_validates_without_producing_a_bundle(self):
        code, out, err = self.run_cli(self.argv(
            ledger=self.full_ledger(), surfaces="text-event-unit,text-mld,image",
            extra=["--preflight-only"]))
        self.assertEqual(code, mod.EXIT_OK, err)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "preflight-only")
        self.assertEqual(payload["build_status"], "success")
        self.assertFalse(payload["generated_written"])
        self.assertIsNone(payload["store"])
        self.assertFalse(self.output_root.exists(),
                         "--preflight-only must not create the output root")
        for surface in payload["surfaces"]:
            self.assertEqual(surface["build_status"], "success", surface["reason"])
            self.assertEqual(surface["entry_count"], 0)

    def test_preflight_only_reports_not_ready_when_the_ledger_is_empty(self):
        empty = self.input_root / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        code, out, err = self.run_cli(self.argv(
            ledger=empty, surfaces="text-event-unit", extra=["--preflight-only"]))
        self.assertEqual(code, mod.EXIT_PREFLIGHT_NOT_READY, err)
        self.assertEqual(json.loads(out)["build_status"], "failed")
        self.assertFalse(self.output_root.exists())

    # -- 4. idempotency ----------------------------------------------------- #
    def test_no_prune_orphans_keeps_the_superseded_object(self):
        """兼容参数保留被替换对象，并在报告中记录不清理。"""
        first = self.complete_ledger("first.jsonl")
        surfaces = "text-event-unit"
        code, _out, err = self.run_cli(self.argv(ledger=first, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_OK, err)
        first_objects = sorted(path.name for path in
                               store_mod.GeneratedStore(self.store_root).iter_objects())

        second = self.ledger_tagged([
            ("event_unit_talk_1000.unity3d", "改写后的第一句", mod.SURFACE_TEXT_EVENT_UNIT),
            ("event_unit_talk_1001.unity3d", "第二句", mod.SURFACE_TEXT_EVENT_UNIT),
        ], "second.jsonl")
        code, out, err = self.run_cli(self.argv(
            ledger=second, surfaces=surfaces, extra=["--no-prune-orphans"]))
        self.assertEqual(code, mod.EXIT_OK, err)
        after = sorted(path.name for path in
                       store_mod.GeneratedStore(self.store_root).iter_objects())
        self.assertEqual(len(after), len(first_objects) + 1,
                         "with pruning off the superseded object stays on disk")
        self.assertEqual(json.loads(out)["store"]["transaction"]["pruned_orphans"], False)

    def test_default_run_keeps_the_superseded_object(self):
        first = self.complete_ledger("first.jsonl")
        surfaces = "text-event-unit"
        code, _out, err = self.run_cli(self.argv(ledger=first, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_OK, err)
        store = store_mod.GeneratedStore(self.store_root)
        old_entry = next(entry for entry in store.load_manifest(ASSET_VERSION)["entries"]
                         if entry["logical_key"] == "event_unit_talk_1000.unity3d")
        old_object = self.store_root / old_entry["object_path"]
        old_sha = store_mod.sha256_file(old_object)

        second = self.ledger_tagged([
            ("event_unit_talk_1000.unity3d", "改写后的第一句", mod.SURFACE_TEXT_EVENT_UNIT),
            ("event_unit_talk_1001.unity3d", "第二句", mod.SURFACE_TEXT_EVENT_UNIT),
        ], "second.jsonl")
        code, out, err = self.run_cli(self.argv(ledger=second, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_OK, err)
        new_entry = next(entry for entry in store.load_manifest(ASSET_VERSION)["entries"]
                         if entry["logical_key"] == old_entry["logical_key"])
        self.assertNotEqual(new_entry["object_path"], old_entry["object_path"])
        self.assertTrue((self.store_root / new_entry["object_path"]).is_file())
        self.assertEqual(store_mod.sha256_file(old_object), old_sha,
                         "默认运行必须保留被替换对象的原始字节")
        self.assertIn(old_object, store.find_orphans())
        self.assertFalse(json.loads(out)["store"]["transaction"]["pruned_orphans"])
        self.assertTrue(store.verify_release(ASSET_VERSION).ok)

    def test_rerun_is_idempotent(self):
        ledger = self.full_ledger()
        surfaces = "text-event-unit,text-mld,image"
        code, out, err = self.run_cli(self.argv(ledger=ledger, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_OK, err)
        first = json.loads(out)
        store = store_mod.GeneratedStore(self.store_root)
        first_objects = sorted(path.name for path in store.iter_objects())
        first_paths = sorted(
            (entry["logical_key"], entry["object_path"])
            for surface in self.report()["surfaces"] for entry in surface["entries"])
        first_manifest_entries = store.load_manifest(ASSET_VERSION)["entries"]
        first_tree = self.tree_snapshot()

        code, out, err = self.run_cli(self.argv(ledger=ledger, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_OK, err)
        second = json.loads(out)
        second_objects = sorted(path.name for path in store.iter_objects())
        second_paths = sorted(
            (entry["logical_key"], entry["object_path"])
            for surface in self.report()["surfaces"] for entry in surface["entries"])

        self.assertEqual(first_objects, second_objects,
                         "a rerun of the same inputs must not create new objects")
        self.assertEqual(first_paths, second_paths)
        self.assertEqual(second["store"]["objects_written"], 0)
        self.assertEqual(second["store"]["objects_deduped"],
                         second["store"]["accepted"])
        self.assertEqual(second["store"]["accepted"], first["store"]["accepted"])
        self.assertTrue(store.verify_release(ASSET_VERSION).ok)
        # The bundle bytes and every object path are unchanged by a rerun; only
        # the translation dimension may move from `modified` to `reused`
        # (the store's own reuse ledger decides that from the retained manifest).
        second_manifest_entries = store.load_manifest(ASSET_VERSION)["entries"]
        self.assertEqual([entry["logical_path"] for entry in first_manifest_entries],
                         [entry["logical_path"] for entry in second_manifest_entries])
        for before, after in zip(first_manifest_entries, second_manifest_entries):
            for field in ("logical_key", "source_sha256", "translated_sha256",
                          "artifact_sha256", "object_path", "reuse_status"):
                self.assertEqual(before[field], after[field], field)
            self.assertEqual(after["translation_status"], "reused")
        self.assertEqual(set(first_tree), set(self.tree_snapshot()),
                         "the file set must not change on a rerun")
        self.assertEqual(
            {name: sha for name, sha in first_tree.items()
             if name.startswith("objects/")},
            {name: sha for name, sha in self.tree_snapshot().items()
             if name.startswith("objects/")},
            "object bytes must be untouched by a rerun")

    def test_second_run_with_a_changed_translation_adds_an_object_and_stays_exact(self):
        first_ledger = self.complete_ledger("first.jsonl")
        second_rows = [
            ("event_unit_talk_1000.unity3d", "改写后的第一句", mod.SURFACE_TEXT_EVENT_UNIT),
            ("event_unit_talk_1001.unity3d", "第二句", mod.SURFACE_TEXT_EVENT_UNIT),
        ]
        surfaces = "text-event-unit"
        code, out, err = self.run_cli(self.argv(
            ledger=first_ledger, surfaces=surfaces, extra=["--prune-orphans"]))
        self.assertEqual(code, mod.EXIT_OK, err)
        first = json.loads(out)
        first_objects = sorted(path.name for path in
                               store_mod.GeneratedStore(self.store_root).iter_objects())

        second_ledger = self.ledger_tagged(second_rows, "second.jsonl")
        code, out, err = self.run_cli(self.argv(
            ledger=second_ledger, surfaces=surfaces, extra=["--prune-orphans"]))
        self.assertEqual(code, mod.EXIT_OK, err)
        second = json.loads(out)
        store = store_mod.GeneratedStore(self.store_root)
        second_objects = sorted(path.name for path in store.iter_objects())

        # 显式清理在候选目录切换前进行，因而：
        # the superseded object is gone with the release that referenced it --
        # and the byte-identical bundle is still deduplicated.
        self.assertEqual(len(second_objects), len(first_objects),
                         "a changed translation must add exactly the new object and drop "
                         f"exactly the superseded one; first={first_objects} "
                         f"second={second_objects}")
        added = set(second_objects) - set(first_objects)
        dropped = set(first_objects) - set(second_objects)
        self.assertEqual(len(added), 1, f"exactly one new object; added={sorted(added)}")
        self.assertEqual(len(dropped), 1,
                         f"exactly the superseded object is dropped; dropped={sorted(dropped)}")
        self.assertEqual(second["store"]["transaction"]["pruned_orphans"], True)
        statuses = {(entry["reuse_status"], entry["translation_status"])
                    for entry in store.load_manifest(ASSET_VERSION)["entries"]}
        self.assertEqual(statuses, {("exact", "modified"), ("exact", "reused")},
                         "an unchanged official source stays exact; only the translation moved")
        self.assertEqual(second["store"]["objects_written"], 1)
        self.assertEqual(second["store"]["objects_deduped"], 1,
                         "the bundle whose translation did not change is deduplicated")
        self.assertTrue(store.verify_release(ASSET_VERSION).ok)

    # -- 5. failed runs ----------------------------------------------------- #
    def test_child_that_is_not_release_ready_fails_the_run_closed(self):
        with self.stub("partial"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.complete_ledger(), surfaces="text-event-unit"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        report = self.report()
        self.assertEqual(report["build_status"], "failed")
        self.assertIn("not release-ready", report["surfaces"][0]["reason"])
        self.assertIn("release_ready", report["surfaces"][0]["reason"])
        self.assertIn("isolated_partial_trial", report["surfaces"][0]["reason"])
        self.assertFalse(self.store_root.exists())

    def test_child_that_declares_a_non_publishable_status_fails_closed(self):
        with self.stub("declared_pending"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.complete_ledger(), surfaces="text-event-unit"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        report = self.report()
        self.assertIn("non-publishable status", report["failure_reason"])
        self.assertIn("translation_status 'pending'", report["failure_reason"])
        self.assertFalse(self.store_root.exists())

    def test_crashing_child_fails_closed_with_its_reason(self):
        with self.stub("crash"):
            code, _out, err = self.run_cli(self.argv(
                ledger=self.complete_ledger(), surfaces="text-event-unit"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        surface = self.report()["surfaces"][0]
        self.assertEqual(surface["entry_point"]["exit_code"], 4)
        self.assertIn("stub crash mode", surface["reason"])

    def test_duplicate_logical_path_across_surfaces_fails_closed(self):
        # Each stub surface keeps only its own ledger rows, so the duplicate has
        # to come from one surface that emits the same logical twice.
        duplicate_ledger = self.ledger_tagged([
            ("event_unit_talk_1000.unity3d", "第一句", mod.SURFACE_TEXT_EVENT_UNIT),
            ("event_unit_talk_1001.unity3d", "第二句", mod.SURFACE_TEXT_EVENT_UNIT),
        ], "duplicate.jsonl")
        with self.stub("duplicate"):
            code, _out, err = self.run_cli(self.argv(
                ledger=duplicate_ledger, surfaces="text-event-unit"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertIn("duplicate logical_path", self.report()["failure_reason"])
        self.assertFalse(self.store_root.exists())

        # Same rule when two different surfaces collide on one logical_path.
        colliding = self.ledger_tagged([
            ("event_unit_talk_1000.unity3d", "第一句", None),
        ], "collide.jsonl")
        code, _out, err = self.run_cli(self.argv(
            ledger=colliding, surfaces="text-event-unit,text-mld"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertIn("duplicate logical_path", self.report()["failure_reason"])
        self.assertFalse(self.store_root.exists())

    def test_empty_ledger_is_refused_by_the_child_and_nothing_is_written(self):
        empty = self.input_root / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        code, _out, err = self.run_cli(self.argv(
            ledger=empty, surfaces="text-event-unit,text-mld,image"))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        report = self.report()
        self.assertFalse(report["generated_written"])
        self.assertFalse(self.store_root.exists())

    def test_missing_ledger_input_is_refused(self):
        code, _out, err = self.run_cli(self.argv(surfaces="text-event-unit"))
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("no accepted translation ledger", err)
        self.assertFalse(self.output_root.exists())

    # -- 4b. the staged-store switch (transaction) -------------------------- #
    def test_the_report_paths_point_into_the_promoted_store(self):
        """The staged paths are gone after the switch; the report must not name them."""
        code, _out, err = self.run_cli(self.argv(
            ledger=self.complete_ledger(), surfaces="text-event-unit"))
        self.assertEqual(code, mod.EXIT_OK, err)
        store = store_mod.GeneratedStore(self.store_root)
        payload = self.report()["store"]
        self.assertEqual(payload["paths_repointed_to_live_root"], True)
        self.assertEqual(Path(payload["manifest_path"]).resolve(),
                         store.manifest_path(ASSET_VERSION).resolve())
        self.assertEqual(Path(payload["checksums_path"]).resolve(),
                         store.checksums_path(ASSET_VERSION).resolve())
        self.assertTrue(Path(payload["manifest_path"]).is_file())
        self.assertTrue(Path(payload["checksums_path"]).is_file())
        self.assertNotIn("mltd-materialize-generated-", payload["manifest_path"],
                         "the staging root is renamed away on promotion")
        # The digests in the report are the digests of those real files.
        self.assertEqual(payload["manifest_sha256"],
                         store_mod.sha256_file(Path(payload["manifest_path"])))
        self.assertEqual(payload["checksums_sha256"],
                         store_mod.sha256_file(Path(payload["checksums_path"])))
        self.assertEqual(payload["verify_ok"], True)

        # And the paths a consumer would read back are inside the committed tree.
        generated = (self.store_root).resolve()
        for key in ("manifest_path", "checksums_path"):
            self.assertTrue(Path(payload[key]).resolve().is_relative_to(generated),
                            f"{key} must live under generated/: {payload[key]}")

    def test_the_release_is_staged_and_switched_in_as_one_step(self):
        """The live store is only ever one of the two complete roots."""
        ledger = self.complete_ledger()
        code, _out, err = self.run_cli(self.argv(
            ledger=ledger, surfaces="text-event-unit", extra=["--prune-orphans"]))
        self.assertEqual(code, mod.EXIT_OK, err)
        payload = self.report()["store"]
        self.assertEqual(payload["transaction"],
                         {"staged": True, "pruned_orphans": True, "promoted": True})
        before = self.tree_snapshot()
        sibling_candidates = [p for p in self.store_root.parent.iterdir()
                              if p.name.startswith(f".{self.store_root.name}.")]
        self.assertEqual(sibling_candidates, [],
                         f"a promoted run leaves no candidate/backup behind: {sibling_candidates}")

        # An unchanged rerun rebuilds the manifest (its `generated_at` moves), so
        # the manifest is not expected to be byte-identical; every object is.
        code, _out, err = self.run_cli(self.argv(
            ledger=ledger, surfaces="text-event-unit", extra=["--prune-orphans"]))
        self.assertEqual(code, mod.EXIT_OK, err)
        after = self.tree_snapshot()
        self.assertEqual({name: sha for name, sha in before.items()
                          if name.startswith("objects/")},
                         {name: sha for name, sha in after.items()
                          if name.startswith("objects/")},
                         "an unchanged rerun must not change a single object byte")
        self.assertEqual(set(before), set(after))

    def test_a_failure_while_switching_restores_the_previous_root(self):
        """fault_inject at the first move: the live root must be whole afterwards."""
        ledger = self.complete_ledger()
        code, _out, err = self.run_cli(self.argv(ledger=ledger, surfaces="text-event-unit"))
        self.assertEqual(code, mod.EXIT_OK, err)
        before = self.tree_snapshot()
        self.assertTrue(before)

        def boom(step, index):
            if step == "switch_live_to_backup":
                raise RuntimeError("injected switch failure")

        store = store_mod.GeneratedStore(self.store_root)
        original = store.transaction

        def transaction(*, prune=False, **kwargs):
            return original(prune=prune, fault_inject=boom)

        with mock.patch.object(type(store), "transaction", staticmethod(transaction)):
            with self.assertRaises(RuntimeError):
                mod._materialize_entries(
                    store=store, staging=self.base, entries=[], asset_version=ASSET_VERSION,
                    client_version=CLIENT_VERSION, source_commit=SOURCE_COMMIT,
                    translation_commit=TRANSLATION_COMMIT,
                    generated_commit=GENERATED_COMMIT, ci_run_id=None, prune=False)
        self.assertEqual(self.tree_snapshot(), before,
                         "a failed switch must leave the previous root in place")

    def test_failed_second_run_leaves_the_first_run_byte_identical(self):
        ledger = self.full_ledger()
        surfaces = "text-event-unit,text-mld,image"
        code, _out, err = self.run_cli(self.argv(ledger=ledger, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_OK, err)
        before = self.tree_snapshot()
        self.assertTrue(before)

        empty = self.input_root / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        code, _out, err = self.run_cli(self.argv(ledger=empty, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertEqual(self.report()["build_status"], "failed")
        self.assertEqual(self.tree_snapshot(), before,
                         "a failed run must leave an existing generated/ byte-identical")

        with self.stub("partial"):
            code, _out, err = self.run_cli(self.argv(ledger=ledger, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        self.assertEqual(self.tree_snapshot(), before,
                         "a not-release-ready child must not alter generated/")

    def test_refused_reuse_ledger_fails_closed_and_leaves_the_tree_alone(self):
        ledger = self.complete_ledger()
        surfaces = "text-event-unit"
        code, _out, err = self.run_cli(self.argv(ledger=ledger, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_OK, err)
        before = self.tree_snapshot()

        # A logical key the retained manifest does not know cannot be proven
        # compatible: the store's own reuse ledger blocks it, and the run must
        # fail without writing anything.
        other = self.ledger([("event_unit_talk_9999.unity3d", "新条目")],
                            "other.jsonl",
                            surface_tags=mod.SURFACE_TEXT_EVENT_UNIT)
        code, _out, err = self.run_cli(self.argv(ledger=other, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_FAILED_CLOSED, err)
        reason = self.report()["failure_reason"]
        self.assertIn("reuse is forbidden", reason)
        self.assertIn("event_unit_talk_9999.unity3d", reason)
        self.assertEqual(self.tree_snapshot(), before)

        # Dropping a key the retained manifest DOES know is a different refusal:
        # it is admissible reuse, so the run succeeds and simply releases less.
        smaller = self.ledger([("event_unit_talk_1000.unity3d", "第一句")], "smaller.jsonl",
                              surface_tags=mod.SURFACE_TEXT_EVENT_UNIT)
        code, out, err = self.run_cli(self.argv(ledger=smaller, surfaces=surfaces))
        self.assertEqual(code, mod.EXIT_OK, err)
        entries = json.loads(out)["store"]["accepted"]
        self.assertEqual(entries, 1)

    def test_unknown_surface_is_refused(self):
        code, _out, err = self.run_cli(self.argv(
            ledger=self.complete_ledger(), surfaces="text-event-unit,audio"))
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("unknown --surfaces", err)

    # -- 6. schema conformance --------------------------------------------- #
    @unittest.skipUnless(_jsonschema is not None, "jsonschema is not installed")
    def test_produced_manifest_conforms_to_the_store_schema(self):
        schema_path = REPO / "configs" / "schemas" / "assets-generated-manifest.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        code, _out, err = self.run_cli(self.argv(
            ledger=self.full_ledger(), surfaces="text-event-unit,text-mld,image"))
        self.assertEqual(code, mod.EXIT_OK, err)
        manifest = json.loads(store_mod.GeneratedStore(self.store_root)
                              .manifest_path(ASSET_VERSION).read_text(encoding="utf-8"))
        _jsonschema.Draft202012Validator(schema).validate(manifest)

    def test_manifest_entries_survive_an_independent_rehash(self):
        code, _out, err = self.run_cli(self.argv(
            ledger=self.full_ledger(), surfaces="text-event-unit,text-mld,image"))
        self.assertEqual(code, mod.EXIT_OK, err)
        store = store_mod.GeneratedStore(self.store_root)
        report = store.verify_release(ASSET_VERSION)
        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.checked_objects,
                         len(store.load_manifest(ASSET_VERSION)["entries"]))

        checksums = store.checksums_path(ASSET_VERSION).read_text(encoding="utf-8")
        for line in checksums.splitlines():
            digest, rel = line.split(None, 1)
            self.assertEqual(Path(rel).name, digest)
            self.assertEqual(store_mod.sha256_file(self.store_root / rel), digest)

        # Tampering with an object must be visible (the CI verifies before commit).
        entry = store.load_manifest(ASSET_VERSION)["entries"][0]
        store.object_path(entry["artifact_sha256"]).write_bytes(b"tampered")
        broken = store.verify_release(ASSET_VERSION)
        self.assertFalse(broken.ok)
        self.assertTrue(any("hashes to" in failure for failure in broken.failures))

    def test_logical_path_prefix_can_be_emptied(self):
        code, _out, err = self.run_cli(self.argv(
            ledger=self.complete_ledger(), surfaces="text-event-unit",
            extra=["--logical-path-prefix", ""]))
        self.assertEqual(code, mod.EXIT_OK, err)
        store = store_mod.GeneratedStore(self.store_root)
        paths = [entry["logical_path"]
                 for entry in store.load_manifest(ASSET_VERSION)["entries"]]
        self.assertTrue(all("/" not in path for path in paths), paths)

    # -- pinned writer source（写者来源） --------------------------------- #
    def test_a_real_run_without_a_pinned_writer_source_is_refused(self):
        """写入必须指明写者：不静默回退到 main 模块。"""
        argv = self.argv(ledger=self.complete_ledger(), writer="none")
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("--assets-writer-root", err)
        self.assertIn("--assets-writer-pin", err)
        self.assertFalse(self.output_root.exists(),
                         "被拒的写者来源不得创建输出根")

    def test_a_half_writer_pair_is_refused_surfaces_including_preflight(self):
        """只给 root 或只给 pin 在任意模式都拒绝，preflight-only 亦然。"""
        for writer in ("root_only", "pin_only"):
            for extra in ([], ["--preflight-only"]):
                with self.subTest(writer=writer, preflight=bool(extra)):
                    argv = self.argv(ledger=self.complete_ledger(), writer=writer, extra=extra)
                    code, _out, err = self.run_cli(argv)
                    self.assertEqual(code, mod.EXIT_REFUSED, err)
                    self.assertIn("pair", err)
                    self.assertFalse(self.output_root.exists())

    def test_a_malformed_or_mismatched_writer_pin_is_refused(self):
        for writer, needle in (("bad_pin", "64-hex"), ("wrong_pin", "does not match")):
            with self.subTest(writer=writer):
                argv = self.argv(ledger=self.complete_ledger(), writer=writer)
                code, _out, err = self.run_cli(argv)
                self.assertEqual(code, mod.EXIT_REFUSED, err)
                self.assertIn(needle, err)
                self.assertFalse(self.output_root.exists(),
                                 "坏 pin 必须在任何写入或 mkdir 之前失败")

    def test_a_writer_root_without_the_writer_file_is_refused(self):
        argv = self.argv(ledger=self.complete_ledger(), writer="bad_root")
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("does not exist", err)
        self.assertFalse(self.store_root.exists(),
                         "坏 writer root 必须在触碰 store 之前失败")

    def test_a_writer_source_env_variable_is_never_consulted(self):
        """来源只来自显式参数，任何环境变量都不能顶替。"""
        env = dict(os.environ)
        env["MLTD_ASSETS_WRITER_ROOT"] = str(PINNED_WRITER_ROOT)
        env["MLTD_ASSETS_WRITER_PIN"] = PINNED_WRITER_PIN
        with mock.patch.dict(os.environ, env, clear=False):
            argv = self.argv(ledger=self.complete_ledger(), writer="none")
            code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("--assets-writer-root", err)
        self.assertFalse(self.output_root.exists(),
                         "环境变量绝不能顶替成对参数")

    def test_a_pinned_writer_source_is_used_instead_of_the_in_process_module(self):
        """写入走 pinned 模块实例，报告如实记录来源。"""
        argv = self.argv(ledger=self.complete_ledger())
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_OK, err)
        report = self.report()
        source = report["writer_source"]
        self.assertEqual(source["kind"], "pinned")
        self.assertTrue(source["verified"])
        self.assertFalse(source["unknown"])
        self.assertEqual(source["sha256"], PINNED_WRITER_PIN)
        self.assertEqual(source["pin"], PINNED_WRITER_PIN)
        self.assertTrue(str(source["module"]).startswith("_mltd_pinned_assets_writer_"),
                        "pinned 模块须在自己的命名空间内")
        self.assertIsNotNone(source["path"])
        # 完整 release 确由该模块写出，而非只做分析。
        store = store_mod.GeneratedStore(self.store_root)
        self.assertTrue(store.manifest_path(ASSET_VERSION).is_file())
        self.assertEqual(report["store"]["accepted"], 2)

    def _pinned_root_copy(self) -> tuple[Path, str]:
        """私有 writer root：只放一份写者文件，另埋一个 init 探针。

        只复制单个写者文件（不复制仓库树），并在其旁放一个会写哨兵文件的
        ``scripts/__init__.py``：任何执行了包初始化器（或除已核验模块外的
        root 代码）的 loader 都会留下哨兵。
        """
        root = self.base / "pinned-root"
        (root / "scripts").mkdir(parents=True)
        source = REPO / "scripts" / "assets_generated_index.py"
        (root / "scripts" / "assets_generated_index.py").write_bytes(source.read_bytes())
        sentinel = root / "root-init-ran.marker"
        (root / "scripts" / "__init__.py").write_text(
            "import pathlib\n"
            f"pathlib.Path({str(sentinel)!r}).write_text('imported')\n",
            encoding="utf-8")
        pin = hashlib.sha256((root / "scripts" / "assets_generated_index.py").read_bytes()
                             ).hexdigest()
        return root, pin

    def test_the_writer_root_package_init_is_not_executed(self):
        """只执行已核验文件本身：不触发 ``scripts/__init__.py`` 副作用。"""
        root, pin = self._pinned_root_copy()
        argv = self.argv(ledger=self.complete_ledger(), writer="none",
                         extra=["--assets-writer-root", str(root), "--assets-writer-pin", pin])
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_OK, err)
        self.assertEqual(self.report()["writer_source"]["sha256"], pin)
        self.assertFalse((root / "root-init-ran.marker").exists(),
                         "writer root 的包初始化器绝不得执行")

    def test_a_valid_header_cached_pyc_is_ignored(self):
        """写者的 ``__pycache__`` 缓存项绝不被读取。

        毒化文件放在 ``SourceFileLoader`` 会选中的确切名字下，头部记录的源码
        mtime/size 与真实文件一致——信任缓存的 loader 会执行它并失败。入口改
        为编译已核验字节，故本次 run 仍成功。
        """
        import importlib.util
        import struct
        root, pin = self._pinned_root_copy()
        source = root / "scripts" / "assets_generated_index.py"
        cache = root / "scripts" / "__pycache__"
        cache.mkdir()
        tag = f"cpython-{sys.version_info.major}{sys.version_info.minor}"
        pyc = cache / f"assets_generated_index.{tag}.pyc"
        poison = compile("raise RuntimeError('cached bytecode was executed')",
                         str(source), "exec")
        stat = source.stat()
        header = (importlib.util.MAGIC_NUMBER
                  + struct.pack("<I", 0)            # flags（0 = 基于时间戳）
                  + struct.pack("<I", int(stat.st_mtime) & 0xFFFFFFFF)
                  + struct.pack("<I", stat.st_size & 0xFFFFFFFF))
        import marshal
        pyc.write_bytes(header + marshal.dumps(poison))
        self.addCleanup(lambda: pyc.unlink(missing_ok=True))

        argv = self.argv(ledger=self.complete_ledger(), writer="none",
                         extra=["--assets-writer-root", str(root), "--assets-writer-pin", pin])
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_OK, err)
        self.assertEqual(self.report()["writer_source"]["sha256"], pin)

    def test_a_bad_writer_source_refuses_before_any_surface_is_spawned(self):
        """坏写者来源要在任何 surface 子进程/mkdir/写之前拒绝。

        子进程 stub 在收到环境标记时记录自身运行；正中对照（正常 run）会留下
        标记，证明机制有效，而被拒的 run 不产生任何标记，也绝不创建输出根。
        """
        marker = self.base / "spawn-marker.txt"

        def run_with_marker(argv):
            env = dict(os.environ)
            env["MLTD_MATERIALIZE_SPAWN_MARKER"] = str(marker)
            with mock.patch.dict(os.environ, env, clear=False):
                return self.run_cli(argv)

        # 对照：写者来源正常时子进程确实会运行并留下标记。
        code, _out, err = run_with_marker(self.argv(ledger=self.complete_ledger()))
        self.assertEqual(code, mod.EXIT_OK, err)
        self.assertTrue(marker.exists(), "对照 run 本应产生 spawn 标记")
        marker.unlink()
        self.output_root.rename(self.base / "out-control")

        # 被拒的写者来源：不 spawn、不 mkdir、不写。
        code, _out, err = run_with_marker(
            self.argv(ledger=self.complete_ledger(), writer="wrong_pin"))
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertFalse(marker.exists(),
                         "写者来源在解析前就应拒绝：不得 spawn 任何 surface")
        self.assertFalse(self.output_root.exists(),
                         "写者来源被拒时不得创建输出根")

    def test_preflight_only_without_a_writer_pair_refuses_before_any_producer(self):
        """所有模式缺 pair 均早期拒绝，不创建输出根、不运行 producer。"""
        argv = self.argv(ledger=self.complete_ledger(), writer="none",
                         extra=["--preflight-only"])
        with mock.patch.object(mod, "_run_child") as producer:
            code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_REFUSED, err)
        self.assertIn("--assets-writer-root", err)
        self.assertIn("--assets-writer-pin", err)
        producer.assert_not_called()
        self.assertFalse(self.output_root.exists(), "缺 pair 不得创建输出根")
        self.assertFalse(self.report()["generated_written"])

    def test_legacy_resolver_arguments_cannot_enable_an_unpinned_fallback(self):
        """保留外部调用形状不等于保留旧来源回退。"""
        with self.assertRaises(mod.RefusedInput):
            mod.resolve_writer_source(store_mod, writer_root=None, writer_pin=None,
                                      require_pair=False)
        source = mod.resolve_writer_source(None, writer_root=PINNED_WRITER_ROOT,
                                           writer_pin=PINNED_WRITER_PIN, require_pair=True)
        self.assertEqual(source.kind, "pinned")
        self.assertEqual(source.module_sha256, PINNED_WRITER_PIN)

    def _run_blocked_cli(self, argv: list[str]) -> subprocess.CompletedProcess:
        """干净子进程内阻断主仓 writer，不改共享树文件。"""
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", "-c", BLOCK_MAIN_WRITER,
             json.dumps(argv, ensure_ascii=False)], cwd=REPO,
            capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertIn("negative control passed", result.stderr)
        return result

    def test_import_and_help_succeed_when_main_writer_import_is_blocked(self):
        result = self._run_blocked_cli(["--help"])
        self.assertEqual(result.returncode, mod.EXIT_OK, result.stderr)
        self.assertIn("--assets-writer-root", result.stdout)

    def test_explicit_legacy_fixture_runs_when_main_writer_import_is_blocked(self):
        """显式 pin 的 legacy 仅为离线 fixture，证明入口无隐式 import。"""
        argv = self.argv(ledger=self.complete_ledger())
        preflight = self._run_blocked_cli(argv + ["--preflight-only"])
        self.assertEqual(preflight.returncode, mod.EXIT_OK, preflight.stderr)
        self.assertFalse(self.output_root.exists())
        result = self._run_blocked_cli(argv)
        self.assertEqual(result.returncode, mod.EXIT_OK, result.stderr)
        self.assertTrue(self.report()["store"]["verify_ok"])

    def test_pinned_store_error_rolls_back_and_unrelated_error_is_not_swallowed(self):
        """独立命名空间的写者异常转为拒绝；其它异常保留原类型且事务回滚。"""
        ledger = self.complete_ledger()
        code, _out, err = self.run_cli(self.argv(ledger=ledger))
        self.assertEqual(code, mod.EXIT_OK, err)
        before = self.tree_snapshot()
        root = self.base / "error-writer"
        (root / "scripts").mkdir(parents=True)
        path = root / "scripts" / "assets_generated_index.py"
        raw = (REPO / "scripts" / "assets_generated_index.py").read_bytes()
        for error_name in ("GeneratedStoreError", "RuntimeError"):
            with self.subTest(error=error_name):
                path.write_bytes(raw + (
                    "\nclass GeneratedStore(GeneratedStore):\n"
                    "    def build_release(self, *args, **kwargs):\n"
                    f"        raise {error_name}('pinned staged build failed')\n").encode())
                pin = hashlib.sha256(path.read_bytes()).hexdigest()
                argv = self.argv(ledger=ledger, writer="none", extra=[
                    "--assets-writer-root", str(root), "--assets-writer-pin", pin])
                if error_name == "GeneratedStoreError":
                    code, _out, err = self.run_cli(argv)
                    self.assertEqual(code, mod.EXIT_REFUSED, err)
                    self.assertIn("pinned staged build failed", err)
                    self.assertFalse(self.report()["generated_written"])
                else:
                    with self.assertRaisesRegex(RuntimeError, "pinned staged build failed"):
                        self.run_cli(argv)
                self.assertEqual(self.tree_snapshot(), before)

    def verify_explicit_product_rawpin_preflight_materialize_and_no_gc(self):
        """真实产品单文件 + 合成文本输入；不读取产品 generated 或生产素材。"""
        if PRODUCT_WRITER_ROOT is None or PRODUCT_WRITER_PIN is None:
            self.fail("显式产品探针必须提供 --product-writer-root/--product-writer-pin")
        source_path = PRODUCT_WRITER_ROOT / mod.ASSETS_WRITER_RELATIVE
        source_raw = source_path.read_bytes()
        self.assertEqual(hashlib.sha256(source_raw).hexdigest(), PRODUCT_WRITER_PIN)
        ledger = self.complete_ledger()
        argv = self.argv(ledger=ledger, writer="none", extra=[
            "--assets-writer-root", str(PRODUCT_WRITER_ROOT),
            "--assets-writer-pin", str(PRODUCT_WRITER_PIN)])
        preflight = self._run_blocked_cli(argv + ["--preflight-only"])
        self.assertEqual(preflight.returncode, mod.EXIT_OK, preflight.stderr)
        self.assertFalse(self.output_root.exists(), "产品预检不创建输出根")
        preflight_report = self.report()
        self.assertFalse(preflight_report["generated_written"])
        reports = []
        manifests = []
        initial_objects: dict[str, str] = {}
        for phase in ("首次候选", "译文变化后候选"):
            if reports:
                self.ledger_tagged([
                    ("event_unit_talk_1000.unity3d", "变更后的第一句", mod.SURFACE_TEXT_EVENT_UNIT),
                    ("event_unit_talk_1001.unity3d", "第二句", mod.SURFACE_TEXT_EVENT_UNIT)])
            result = self._run_blocked_cli(argv)
            self.assertEqual(result.returncode, mod.EXIT_OK, result.stderr)
            report = self.report()
            reports.append(report)
            source = report["writer_source"]
            self.assertEqual(source["path"], str(source_path.resolve()))
            self.assertEqual(source["sha256"], PRODUCT_WRITER_PIN)
            self.assertEqual(source["pin"], PRODUCT_WRITER_PIN)
            self.assertEqual(source["kind"], "pinned")
            self.assertTrue(source["verified"])
            self.assertTrue(report["store"]["verify_ok"])
            self.assertEqual(report["store"]["transaction"], {
                "staged": True, "pruned_orphans": False, "promoted": True})
            manifest_path = self.store_root / ASSET_VERSION / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifests.append(manifest)
            self.assertEqual(len(manifest["entries"]), 2)
            report_rows = {entry["logical_key"]: entry
                           for surface in report["surfaces"] for entry in surface["entries"]}
            checksums = (self.store_root / ASSET_VERSION / "checksums.txt").read_text()
            for entry in manifest["entries"]:
                digest = entry["artifact_sha256"]
                self.assertEqual(entry["object_path"], f"objects/sha256/{digest}", phase)
                obj = self.store_root / entry["object_path"]
                self.assertEqual(hashlib.sha256(obj.read_bytes()).hexdigest(), digest)
                self.assertIn(f"{digest}  {entry['object_path']}", checksums)
                remote = hashlib.sha256(
                    ("remote:" + entry["logical_key"]).encode()).hexdigest()[:40] + ".unity3d"
                runtime = f"{mod.LOGICAL_PATH_PREFIX_DEFAULT}/{remote}"
                self.assertEqual(entry["logical_path"], runtime)
                self.assertEqual(entry["runtime_path"], runtime)
                self.assertEqual(report_rows[entry["logical_key"]]["runtime_path"], runtime)
            if len(reports) == 1:
                initial_objects = {entry["object_path"]: entry["artifact_sha256"]
                                   for entry in manifest["entries"]}
            else:
                self.assertFalse(report["reuse_baseline"], "二次候选必须使用产品复用账本")
                for obj_path, digest in initial_objects.items():
                    self.assertEqual(hashlib.sha256(
                        (self.store_root / obj_path).read_bytes()).hexdigest(), digest,
                        "默认 noGC 必须保留失去当前引用的旧对象")
        retained = {entry["object_path"] for entry in manifests[-1]["entries"]}
        self.assertTrue(set(initial_objects) - retained, "对照必须实际产生孤儿对象")
        # 产品自己的 GeneratedStoreError（独立命名空间）应被转换为拒绝。
        manifest_path.write_bytes(b"not-json")
        corrupt_before = self.tree_snapshot()
        marker = self.base / "product-corrupt-producer.marker"
        with mock.patch.dict(os.environ, {"MLTD_MATERIALIZE_SPAWN_MARKER": str(marker)}):
            refused = self._run_blocked_cli(argv + ["--preflight-only"])
        self.assertEqual(refused.returncode, mod.EXIT_REFUSED, refused.stderr)
        self.assertIn("not valid JSON", refused.stderr)
        self.assertFalse(marker.exists(), "保留清单损坏应在 producer 前拒绝")
        self.assertFalse(self.report()["generated_written"])
        self.assertEqual(self.tree_snapshot(), corrupt_before)
        self.assertEqual(source_path.read_bytes(), source_raw, "产品源码只读，不归一化行尾")
        if PROBE_EVIDENCE_DIR is not None:
            PROBE_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            evidence = {
                "kind": "materializer-detached-product-probe", "canonical": False,
                "source_path": str(source_path.resolve()), "raw_sha256": PRODUCT_WRITER_PIN,
                "source_bytes": len(source_raw), "asset_version": ASSET_VERSION,
                "source_client_version": CLIENT_VERSION, "abi": "arm64",
                "surface_inputs": "隔离合成文本 ledger/stub；没有执行真实 producer",
                "import_negative_control": "每个干净子进程先确认主仓 writer 导入被阻断",
                "preflight_report": preflight_report, "materialize_reports": reports,
                "manifests": manifests, "orphan_objects_retained": initial_objects,
                "corrupt_retained_manifest_refusal": self.report(),
                "corrupt_retained_manifest_stderr": refused.stderr,
                "product_source_unchanged": True,
                "scope": "源码单源化候选验证；不代表真实资源/图片/设备/CI 验收",
            }
            (PROBE_EVIDENCE_DIR / "product-probe.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_a_pinned_writer_outside_the_repo_publishes_a_whole_release(self):
        """隔离的外部 pinned 写者经由**中央 run** 发布完整 release。

        写者 = 本仓库 store 文件的字节（未改动），但复制到一个 *仓库外* 的私有
        root；由此证明中央 run 走的是 pinned 文件本身，而不是
        ``scripts.assets_generated_index`` 内的类，且子进程/切源来自外部目录。

        对象布局断言保持与写者一致（object_path 处逐对象存在），因为本仓库常驻
        测试不得依赖兄弟产品；产品写者的**flat**路径属性在私有 smoke 中用真实
        产品源码单独核验。此处仍严格核验：runtime_path/logical_path、版本保留
        （旧对象不自动 GC）、以及 ``verify_release`` 通过。
        """
        writer_root = self.base / "isolated-writer-root"
        (writer_root / "scripts").mkdir(parents=True)
        writer_file = writer_root / "scripts" / "assets_generated_index.py"
        writer_file.write_bytes((REPO / "scripts" / "assets_generated_index.py").read_bytes())
        pin = hashlib.sha256(writer_file.read_bytes()).hexdigest()

        # 源停留在仓库外的私有 temp 目录。
        outer_in = Path(tempfile.mkdtemp(prefix="b4-e2e-in-"))
        self.addCleanup(shutil.rmtree, outer_in, ignore_errors=True)
        ledgers = outer_in / "ledgers"
        ledgers.mkdir(parents=True)
        (ledgers / "l.jsonl").write_text("".join(
            json.dumps({"logical": name, "translation": "句" + name,
                        "release_gate": "accepted",
                        "surface_tags": [mod.SURFACE_TEXT_EVENT_UNIT]}) + "\n"
            for name in ("event_unit_talk_2000.unity3d", "event_unit_talk_2001.unity3d")),
            encoding="utf-8")
        outer_out = Path(tempfile.mkdtemp(prefix="b4-e2e-out-"))
        self.addCleanup(shutil.rmtree, outer_out, ignore_errors=True)
        report_path = outer_out / "report.json"

        argv = [
            "--asset-version", ASSET_VERSION, "--source-client-version", CLIENT_VERSION,
            "--source-commit", SOURCE_COMMIT, "--translation-commit", TRANSLATION_COMMIT,
            "--generated-commit", GENERATED_COMMIT,
            "--input-root", str(outer_in), "--output-root", str(outer_out),
            "--repository-root", str(self.stub_repo), "--surfaces", "text-event-unit",
            "--translated-ledger", str(ledgers / "l.jsonl"), "--report", str(report_path),
            "--assets-writer-root", str(writer_root), "--assets-writer-pin", pin,
        ]
        code, _out, err = self.run_cli(argv)
        self.assertEqual(code, mod.EXIT_OK, err)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["writer_source"]["sha256"], pin)
        self.assertTrue(report["writer_source"]["verified"])
        self.assertTrue(str(report["writer_source"]["module"]).startswith(
            "_mltd_pinned_assets_writer_"))

        store_root = outer_out / "generated"
        store = store_mod.GeneratedStore(store_root)
        manifest = store.load_manifest(ASSET_VERSION)
        entries = manifest["entries"]
        self.assertEqual(len(entries), 2)
        # runtime_path：中央把**已核验的现有服务路径**(logical_path=前缀+remote)
        # 显式带作 runtime_path，且 manifest/report 内一致。
        manifest_runtime = {e["logical_key"]: e["runtime_path"] for e in entries}
        report_runtime = {e["logical_key"]: e["runtime_path"]
                          for s in report["surfaces"] for e in s["entries"]}
        for entry in entries:
            self.assertTrue((store_root / entry["object_path"]).is_file(),
                            entry["object_path"])
            self.assertIn("runtime_path", entry)
            # synthetic 精确断言：前缀 + child 宣称的 remote（= stub 的命名规则）。
            expected_remote = hashlib.sha256(
                ("remote:" + entry["logical_key"]).encode("utf-8")).hexdigest()[:40] + ".unity3d"
            expected_path = f"{mod.LOGICAL_PATH_PREFIX_DEFAULT}/{expected_remote}"
            self.assertEqual(entry["logical_path"], expected_path)
            self.assertEqual(entry["runtime_path"], expected_path)
            self.assertEqual(report_runtime[entry["logical_key"]], expected_path)
        # 版本保留：发布后对象仍在磁盘（不自动 GC）。
        self.assertTrue(store.verify_release(ASSET_VERSION).ok)
        files = {p.relative_to(store_root).as_posix()
                 for p in store_root.rglob("*") if p.is_file()}
        self.assertTrue(any(p.startswith("objects/") for p in files))
        self.assertIn(f"{ASSET_VERSION}/manifest.json", files)

    def test_an_entry_without_a_declared_path_source_is_refused(self):
        """无 declared remote/logical 来源时拒绝该条，不造假 runtime_path。

        图片 stub 的 inventory 行带 remote，text stub 带 logical；这里用一个
        只带 artifact（无 remote/logical）的行，证明中央拒绝而非用 CAS 摘要名兜底。
        """
        # 手工构造一个无 declared_remote/declared_logical_path 的 EntryRow。
        artifact = self.base / "no-declared-name.bin"
        artifact.write_bytes(b"payload")
        entry = mod.EntryRow(
            logical_key="k", logical_path="", declared_logical_path=None,
            declared_remote=None, source_sha256="0" * 64,
            translated_sha256=store_mod.sha256_file(artifact),
            artifact_path=artifact, bytes=artifact.stat().st_size, surface="x")
        self.assertNotIn("runtime_path", entry.admission_dict(),
                         "无 declared 路径来源时不得带 runtime_path")
        self.assertIsNone(entry.to_dict()["runtime_path"])


class TestPinnedWriterApiBoundary(unittest.TestCase):
    """pinned 写者缺 API 时在任何写入之前拒绝。"""

    def test_a_pinned_writer_without_the_transaction_api_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="b4-writer-api-") as raw:
            base = Path(raw)
            root = base / "pinned-root"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "assets_generated_index.py").write_text(
                STUB_STORE_MISSING_TRANSACTION, encoding="utf-8")
            pin = hashlib.sha256(
                (root / "scripts" / "assets_generated_index.py").read_bytes()).hexdigest()
            in_root = base / "in"
            (in_root / "ledgers").mkdir(parents=True)
            (in_root / "ledgers" / "l.jsonl").write_text(
                json.dumps({"logical": "x.unity3d", "translation": "句",
                            "release_gate": "accepted"}) + "\n", encoding="utf-8")
            out = base / "out"
            argv = [
                "--asset-version", ASSET_VERSION, "--source-client-version", CLIENT_VERSION,
                "--source-commit", SOURCE_COMMIT, "--input-root", str(in_root),
                "--output-root", str(out), "--repository-root", str(base),
                "--surfaces", "text-event-unit",
                "--assets-writer-root", str(root), "--assets-writer-pin", pin,
            ]
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = mod.main(argv)
            self.assertEqual(code, mod.EXIT_REFUSED, err.getvalue())
            self.assertIn("store API", err.getvalue())
            self.assertFalse(out.exists(),
                             "API 坏的写者必须在任何写入之前失败")

    def test_a_noncallable_store_api_and_bad_transaction_are_refused_early(self):
        """``build_release=None`` 与坏 transaction 签名都在任何副作用之前拒绝。

        负控证明：不构造 ``GeneratedStore``（其 ``__init__`` 会断言）、不 mkdir
        输出根、不 spawn 任何 surface（子进程 stub 会留下标记）。
        """
        with tempfile.TemporaryDirectory(prefix="b4-writer-noncallable-") as raw:
            base = Path(raw)
            root = base / "pinned-root"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "assets_generated_index.py").write_text(
                STUB_STORE_BAD_CALLABLE, encoding="utf-8")
            pin = hashlib.sha256(
                (root / "scripts" / "assets_generated_index.py").read_bytes()).hexdigest()
            in_root = base / "in"
            (in_root / "ledgers").mkdir(parents=True)
            (in_root / "ledgers" / "l.jsonl").write_text(
                json.dumps({"logical": "x.unity3d", "translation": "句",
                            "release_gate": "accepted"}) + "\n", encoding="utf-8")
            out = base / "out"
            marker = base / "spawn-marker.txt"
            argv = [
                "--asset-version", ASSET_VERSION, "--source-client-version", CLIENT_VERSION,
                "--source-commit", SOURCE_COMMIT, "--input-root", str(in_root),
                "--output-root", str(out), "--repository-root", str(base),
                "--surfaces", "text-event-unit",
                "--assets-writer-root", str(root), "--assets-writer-pin", pin,
            ]
            env = dict(os.environ)
            env["MLTD_MATERIALIZE_SPAWN_MARKER"] = str(marker)
            err = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), \
                    contextlib.redirect_stderr(err):
                code = mod.main(argv)
            self.assertEqual(code, mod.EXIT_REFUSED, err.getvalue())
            self.assertIn("store API", err.getvalue())
            self.assertFalse(out.exists(), "不得创建输出根")
            self.assertFalse(marker.exists(), "不得 spawn 任何 surface")

    def test_a_callable_transaction_with_a_bad_signature_is_refused_early(self):
        """可调用但缺 ``prune`` 的 transaction 也在构造/mkdtemp/spawn 之前拒绝。"""
        with tempfile.TemporaryDirectory(prefix="b4-writer-badtxn-") as raw:
            base = Path(raw)
            root = base / "pinned-root"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "assets_generated_index.py").write_text(
                STUB_STORE_BAD_TXN, encoding="utf-8")
            pin = hashlib.sha256(
                (root / "scripts" / "assets_generated_index.py").read_bytes()).hexdigest()
            in_root = base / "in"
            (in_root / "ledgers").mkdir(parents=True)
            (in_root / "ledgers" / "l.jsonl").write_text(
                json.dumps({"logical": "x.unity3d", "translation": "句",
                            "release_gate": "accepted"}) + "\n", encoding="utf-8")
            out = base / "out"
            marker = base / "spawn-marker.txt"
            argv = [
                "--asset-version", ASSET_VERSION, "--source-client-version", CLIENT_VERSION,
                "--source-commit", SOURCE_COMMIT, "--input-root", str(in_root),
                "--output-root", str(out), "--repository-root", str(base),
                "--surfaces", "text-event-unit",
                "--assets-writer-root", str(root), "--assets-writer-pin", pin,
            ]
            env = dict(os.environ)
            env["MLTD_MATERIALIZE_SPAWN_MARKER"] = str(marker)
            err = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), \
                    contextlib.redirect_stderr(err):
                code = mod.main(argv)
            self.assertEqual(code, mod.EXIT_REFUSED, err.getvalue())
            self.assertIn("staged-store transaction", err.getvalue())
            self.assertFalse(out.exists(), "不得创建输出根")
            self.assertFalse(marker.exists(), "不得 spawn 任何 surface")


class TestStoreWithoutTransaction(unittest.TestCase):
    """A store older than the staged-store API is refused, not written in place.

    The guarantee the CI depends on -- a failed run leaves the previous release
    byte-identical -- is only true while every write happens in a staged root
    that is verified before it is switched in.  So the entry point checks the
    API up front and refuses a store that cannot offer it, instead of falling
    back to writing the live store directly.
    """

    def test_a_store_without_the_transaction_api_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="no-transaction-store-") as raw:
            base = Path(raw)
            without = type("OldStore", (), {})()
            with self.assertRaises(mod.RefusedInput) as caught:
                mod._materialize_entries(
                    store=without, staging=base, entries=[], asset_version=ASSET_VERSION,
                    client_version=CLIENT_VERSION, source_commit=SOURCE_COMMIT,
                    translation_commit=TRANSLATION_COMMIT,
                    generated_commit=GENERATED_COMMIT, ci_run_id=None, prune=True)
            self.assertIn("staged-store transaction", str(caught.exception))
            self.assertIn("OldStore", str(caught.exception))
            self.assertEqual(list(base.iterdir()), [],
                             "a refused store must not have been written to")

    def test_a_transaction_without_the_prune_flag_is_refused(self):
        """The API is checked, not assumed: an older signature is caught too."""
        with tempfile.TemporaryDirectory(prefix="old-transaction-store-") as raw:
            base = Path(raw)

            class OldTransactionStore:
                root = base

                def transaction(self):  # no ``prune`` parameter
                    raise AssertionError("must not be called")

            with self.assertRaises(mod.RefusedInput) as caught:
                mod._materialize_entries(
                    store=OldTransactionStore(), staging=base, entries=[],
                    asset_version=ASSET_VERSION, client_version=CLIENT_VERSION,
                    source_commit=SOURCE_COMMIT, translation_commit=TRANSLATION_COMMIT,
                    generated_commit=GENERATED_COMMIT, ci_run_id=None, prune=True)
            self.assertIn("staged-store transaction", str(caught.exception))
            self.assertEqual(list(base.iterdir()), [])


if __name__ == "__main__":
    probe_parser = argparse.ArgumentParser(add_help=False)
    probe_parser.add_argument("--product-writer-root", type=Path)
    probe_parser.add_argument("--product-writer-pin")
    probe_parser.add_argument("--probe-evidence-dir", type=Path)
    probe_args, test_args = probe_parser.parse_known_args()
    if (probe_args.product_writer_root is None) != (probe_args.product_writer_pin is None):
        probe_parser.error("产品探针的 root/pin 必须显式成对给出")
    PRODUCT_WRITER_ROOT = probe_args.product_writer_root
    PRODUCT_WRITER_PIN = probe_args.product_writer_pin
    PROBE_EVIDENCE_DIR = probe_args.probe_evidence_dir
    unit_run = unittest.main(argv=[sys.argv[0], *test_args], verbosity=2, exit=False)
    if not unit_run.result.wasSuccessful():
        raise SystemExit(1)
    if PRODUCT_WRITER_ROOT is not None:
        print("显式产品 integration probe（默认 unit/CI 不覆盖产品来源）", file=sys.stderr)
        probe_suite = unittest.TestSuite([MaterializeTestCase(
            "verify_explicit_product_rawpin_preflight_materialize_and_no_gc")])
        probe_result = unittest.TextTestRunner(verbosity=2).run(probe_suite)
        if probe_result.skipped or not probe_result.wasSuccessful():
            raise SystemExit(1)
