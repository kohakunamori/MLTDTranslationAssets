#!/usr/bin/env python3
"""Consumer-shaped contract tests for the two isolated staging tools.

Scope honesty: every fixture here is *synthetic* — a fake install manifest
(JSONL), small PNGs, and non-Unity "source bundle" bytes.  These tests prove
**source/CLI closure** (explicit inputs only, fail-closed refusals, unchanged
review whitelist and pin/binding semantics), NOT a Unity repack acceptance and
NOT a complete image closure.  A passing preflight is an input check, not a
bundle proof.  No real archive is repacked anywhere here.

The inputs the tools read are all CLI-only.  Round 1 of this candidate left the
injector's two resolvers reading ``$MLTD_IMAGE_INSTALL_MANIFEST`` /
``$MLTD_IMAGE_ORIGINAL_ROOT`` and left the auditor's ``--install-manifest`` and
``--audit`` optional with environment/report-sibling fallbacks.  Those were
review findings; the negative controls below (an environment variable set to a
file that *does* exist and looks usable, and an auditor given only ``--report``)
exist specifically to prove that is no longer possible.

Runnable from any CWD:

    python -m pytest test_image_injection_contract.py

The tests invoke the scripts as subprocesses (as the release entry point does)
and also import them for the resolution helpers a synthetic bundle can never
reach.  No test is skipped or xfailed anywhere.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

IMAGE_DIR = Path(__file__).resolve().parent
INJECTOR = IMAGE_DIR / "inject_reviewed_textures.py"
AUDITOR = IMAGE_DIR / "verify_bundle_repack.py"

MANIFEST_ENV = "MLTD_IMAGE_INSTALL_MANIFEST"
ORIGINAL_ROOT_ENV = "MLTD_IMAGE_ORIGINAL_ROOT"
APPROVED_STATUS = "user_approved_for_isolated_install_staging"
# The tag the Assets repository's public ``stage_reviewed_images.py`` writes.
# This candidate's injector must NOT silently accept it as an approval; the
# schema gap is recorded in the run HANDOFF instead of bridged here.
PRODUCT_STAGE_TAG = "approved_for_staging_not_installed"


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def clean_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in (MANIFEST_ENV, ORIGINAL_ROOT_ENV)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(overrides)
    return env


def run(script: Path, argv: list[str], cwd: Path, env: dict[str, str] | None = None):
    return subprocess.run([sys.executable, str(script), *argv],
                          capture_output=True, text=True, cwd=str(cwd),
                          env=env if env is not None else clean_env())


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def small_png(path: Path, size=(4, 4), color=(10, 20, 30, 255)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, color).save(path, format="PNG")
    return path


def manifest_row(*, remote: str, source: Path, original_png: str, restored_png: Path,
                 texture_path_id: int = 7, review_status: str = APPROVED_STATUS,
                 bundle: str | None = None) -> dict:
    return {
        "archive_sha256": sha256_file(source),
        "bundle": bundle or f"{remote}.unity3d",
        "original_png": original_png,
        "original_size": [4, 4],
        "remote": remote,
        "restored_png": str(restored_png),
        "restored_png_sha256": sha256_file(restored_png),
        "source_bundle": str(source),
        "source_id": f"{remote}:{texture_path_id}",
        "texture_path_id": texture_path_id,
        "review_status": review_status,
    }


def write_manifest(directory: Path, rows: list[dict],
                   name: str = "install-manifest.jsonl") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return target


@pytest.fixture
def workspace(tmp_path):
    """A synthetic reviewed cohort: fake source bytes + reviewed candidate PNG."""
    source = tmp_path / "sources" / "fake_bundle_0001.unity3d"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"NOT-A-UNITY-BUNDLE fake source bytes")
    restored = small_png(tmp_path / "reviewed" / "restored.png", color=(200, 10, 10, 255))
    original = small_png(tmp_path / "originals" / "original.png", color=(10, 10, 200, 255))
    return {"root": tmp_path, "source": source, "restored": restored, "original": original}


# --------------------------------------------------------------------------- #
# 1. help and required-argument refusals from a foreign CWD under a candidate
#    path containing a space; zero output side effects.
# --------------------------------------------------------------------------- #
def test_help_and_missing_explicit_inputs_from_foreign_cwd(tmp_path):
    # The candidate is exercised from a copy whose path contains a space and
    # whose parents deliberately do NOT look like the repository layout.
    copy = tmp_path / "ca ndidate work" / "image"
    copy.parent.mkdir(parents=True)
    shutil.copytree(IMAGE_DIR, copy)
    injector = copy / "inject_reviewed_textures.py"
    auditor = copy / "verify_bundle_repack.py"
    foreign_cwd = tmp_path / "else where"
    foreign_cwd.mkdir()
    listing_after_copy = sorted(str(p.relative_to(copy)) for p in copy.rglob("*"))

    # --help needs no environment and exits 0 from any CWD.
    assert run(injector, ["--help"], foreign_cwd).returncode == 0
    assert "usage" in run(injector, ["--help"], foreign_cwd).stdout.lower()
    assert run(auditor, ["--help"], foreign_cwd).returncode == 0

    # --install-manifest is required: no selection at all is an argparse error
    # (exit 2) that names the missing flag; the same holds for --preflight.
    for argv in ([], ["--all", "--out", str(tmp_path / "never")],
                 ["--preflight-context"]):
        result = run(injector, argv, foreign_cwd)
        assert result.returncode == 2, (argv, result.stderr)
        assert "--install-manifest" in result.stderr, argv
    assert not (tmp_path / "never").exists()

    # The auditor requires all three: --report, --install-manifest and --audit.
    for argv, missing in (([], "--report"),
                          (["--report", "r.json"], "--install-manifest"),
                          (["--report", "r.json", "--install-manifest", "m.jsonl"], "--audit")):
        result = run(auditor, argv, foreign_cwd)
        assert result.returncode == 2, (argv, result.stderr)
        assert missing in result.stderr, (argv, result.stderr)

    # An explicit but nonexistent --install-manifest is a clean refusal (exit 1)
    # with the read-only preflight document; the source named is the CLI flag.
    result = run(injector, ["--preflight-context",
                            "--install-manifest", str(tmp_path / "absent.jsonl")], foreign_cwd)
    assert result.returncode == 1
    document = json.loads(result.stdout)
    assert document["manifest"] is None and "does not exist" in document["refused"]
    assert all("--install-manifest" in item["source"] for item in document["manifest_sources"])

    # The copy was exercised read-only: nothing appeared beside it.
    listing_after_runs = sorted(str(p.relative_to(copy)) for p in copy.rglob("*"))
    assert listing_after_runs == listing_after_copy


# --------------------------------------------------------------------------- #
# 2. environment variables are never consulted — even when they name a file
#    that exists and looks usable.
# --------------------------------------------------------------------------- #
def test_environment_variables_are_never_consulted(tmp_path, workspace):
    usable_manifest = write_manifest(tmp_path / "env-cohort", [manifest_row(
        remote="env_bundle", source=workspace["source"],
        original_png=str(workspace["original"]), restored_png=workspace["restored"])])
    usable_root = tmp_path / "env-root"
    usable_root.mkdir()
    env = clean_env(**{MANIFEST_ENV: str(usable_manifest),
                       ORIGINAL_ROOT_ENV: str(usable_root)})

    # (a) CLI: no --install-manifest, env names a usable file -> still refused,
    #     and the env path never appears in the output.
    out = tmp_path / "out-env"
    result = run(INJECTOR, ["--preflight-context", "--out", str(out)], tmp_path, env=env)
    assert result.returncode == 2
    assert "--install-manifest" in result.stderr
    assert str(usable_manifest) not in result.stdout + result.stderr
    assert not out.exists()

    # (b) module level: the resolvers ignore the environment entirely.
    module = load_module(INJECTOR, "injector_env_control")
    with pytest.raises(module.RefusedInput):
        module.resolve_manifest(None)                      # env set, still refused
    assert module.resolve_manifest(str(usable_manifest)) == usable_manifest
    assert module.resolve_original_root(None, usable_manifest) is None  # env root ignored

    # (c) the explicit flag wins over a *different* usable env value.
    other_manifest = write_manifest(tmp_path / "explicit-cohort", [manifest_row(
        remote="explicit_bundle", source=workspace["source"],
        original_png=str(workspace["original"]), restored_png=workspace["restored"])])
    result = run(INJECTOR, ["--preflight-context", "--install-manifest", str(other_manifest)],
                 tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert Path(document["manifest"]).resolve() == other_manifest.resolve()
    assert document["manifest_counts"]["bundles"] == 1
    assert str(usable_manifest) not in result.stdout
    # The document no longer advertises an environment spelling at all.
    assert "original_root_env" not in document
    # A relative original_png with only the env root set is still refused.
    with pytest.raises(module.RefusedInput):
        module.resolve_original_png("rel/original.png", None, {"source_id": "env:1"})


# --------------------------------------------------------------------------- #
# 3. preflight resolves an explicit manifest, writes nothing, and prints the
#    exact fields the release entry point reads.
# --------------------------------------------------------------------------- #
def test_preflight_context_explicit_manifest_writes_nothing(tmp_path, workspace):
    row = manifest_row(remote="fake_bundle_0001", source=workspace["source"],
                       original_png=str(workspace["original"]),
                       restored_png=workspace["restored"])
    other_row = manifest_row(remote="fake_bundle_0002", source=workspace["source"],
                             original_png=str(workspace["original"]),
                             restored_png=workspace["restored"], texture_path_id=8)
    manifest = write_manifest(tmp_path / "space dir", [row, other_row])

    out = tmp_path / "preflight-out"
    report = tmp_path / "reports" / "preflight-report.json"
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    result = run(INJECTOR, ["--preflight-context", "--out", str(out),
                            "--install-manifest", str(manifest), "--report", str(report)],
                 tmp_path)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["kind"] == "mltd-image-injection-input-context"
    assert document["mode"] == "preflight_context_no_bundles_written"
    assert Path(document["manifest"]).resolve() == manifest.resolve()
    assert document["manifest_sha256"] == sha256_file(manifest)
    assert document["manifest_counts"] == {"rows": 2, "bundles": 2, "note": None}
    assert document["original_root"] is None
    assert document["original_root_default"] is None
    assert document["report"] == str(report)
    assert all("--install-manifest" in item["source"] for item in document["manifest_sources"])
    # Read-only on purpose: neither --out nor --report is created anywhere.
    assert not out.exists() and not report.exists()
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


# --------------------------------------------------------------------------- #
# 4. no default machinery survives, and a sibling ``work/`` tree is never read.
# --------------------------------------------------------------------------- #
def test_no_default_machinery_and_sibling_work_never_read(tmp_path, workspace):
    install = tmp_path / "installed" / "pipelines" / "image"
    install.mkdir(parents=True)
    shutil.copy2(INJECTOR, install / INJECTOR.name)
    decoy_dir = (tmp_path / "installed" / "work" / "agents" / "image-localization"
                 / "reviewed937-texture-stage")
    decoy = write_manifest(decoy_dir, [manifest_row(
        remote="decoy", source=workspace["source"],
        original_png=str(workspace["original"]), restored_png=workspace["restored"])])
    assert decoy.is_file()

    somewhere = tmp_path / "somewhere"
    somewhere.mkdir()
    result = run(install / INJECTOR.name, ["--preflight-context"], somewhere)
    assert result.returncode == 2 and "--install-manifest" in result.stderr
    assert "decoy" not in result.stdout + result.stderr

    module = load_module(install / INJECTOR.name, "injector_no_default")
    for gone in ("_default_path", "_default_path_root",
                 "DEFAULT_MANIFEST_REL", "DEFAULT_ORIGINAL_ROOT_REL",
                 "MANIFEST_ENV", "ORIGINAL_ROOT_ENV"):
        assert not hasattr(module, gone), gone
    assert module.resolve_original_root(None, decoy) is None


# --------------------------------------------------------------------------- #
# 5. review-status whitelist is unchanged and fails closed.
# --------------------------------------------------------------------------- #
def test_review_status_whitelist_unchanged_and_fail_closed(tmp_path, workspace):
    for index, status in enumerate((PRODUCT_STAGE_TAG, "user_approved", "approved", "")):
        out = tmp_path / f"out-{index}"
        row = manifest_row(remote="fake_bundle_0001", source=workspace["source"],
                           original_png=str(workspace["original"]),
                           restored_png=workspace["restored"], review_status=status)
        manifest = write_manifest(tmp_path / f"m-{index}", [row])
        result = run(INJECTOR, ["--all", "--out", str(out),
                                "--install-manifest", str(manifest)], tmp_path)
        assert result.returncode == 1, (status, result.stderr)
        assert "review_status" in result.stderr and "not a user approval" in result.stderr
        assert not out.exists(), "refusal happened after output side effects"

    # The approved tag passes the whitelist; the run then fails closed on the
    # synthetic source bytes with no inventory and no bundle written.
    row = manifest_row(remote="fake_bundle_0001", source=workspace["source"],
                       original_png=str(workspace["original"]),
                       restored_png=workspace["restored"])
    manifest = write_manifest(tmp_path / "approved", [row])
    out = tmp_path / "out-approved"
    result = run(INJECTOR, ["--all", "--out", str(out),
                            "--install-manifest", str(manifest)], tmp_path)
    assert result.returncode != 0
    assert "not a user approval" not in result.stderr
    assert not (out / "inventory.json").exists()
    assert not (out / "fake_bundle_0001").exists()
    assert [p.name for p in out.iterdir()] == []


# --------------------------------------------------------------------------- #
# 6. manifest pin, and the auditor's all-required explicit inputs.
# --------------------------------------------------------------------------- #
def test_manifest_pin_and_auditor_explicit_inputs(tmp_path, workspace):
    row = manifest_row(remote="fake_bundle_0001", source=workspace["source"],
                       original_png=str(workspace["original"]),
                       restored_png=workspace["restored"])
    manifest = write_manifest(tmp_path / "cohort", [row])

    out = tmp_path / "pinned-out"
    result = run(INJECTOR, ["--all", "--out", str(out), "--install-manifest", str(manifest),
                            "--expect-manifest-sha256", "0" * 64], tmp_path)
    assert result.returncode == 1 and "manifest sha256 is" in result.stderr
    assert not out.exists()

    result = run(INJECTOR, ["--all", "--out", str(out), "--install-manifest", str(manifest),
                            "--expect-manifest-sha256", sha256_file(manifest)], tmp_path)
    assert result.returncode != 0 and "manifest sha256 is" not in result.stderr
    assert out.exists()

    # A valid-shaped report whose own "install_manifest" field points at a real
    # file, but with no --install-manifest flag: refused, never read from the
    # report or the environment.
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "kind": "reviewed-texture-injection-report",
        "install_manifest": str(manifest),
        "bundles": [{"remote": "fake_bundle_0001", "bundle": "fake_bundle_0001.unity3d",
                     "source_bundle": str(workspace["source"]),
                     "source_sha256": sha256_file(workspace["source"]),
                     "output_bundle": str(tmp_path / "nope.unity3d"),
                     "output_sha256": "0" * 64, "texture_count": 1}],
    }), encoding="utf-8")
    result = run(AUDITOR, ["--report", str(report), "--audit", str(tmp_path / "a.json")],
                 tmp_path)
    assert result.returncode == 2 and "--install-manifest" in result.stderr

    # Explicit manifest, but it has no reviewed rows -> refused by name.
    empty_manifest = write_manifest(tmp_path / "other", [], name="other.jsonl")
    result = run(AUDITOR, ["--report", str(report), "--install-manifest", str(empty_manifest),
                           "--audit", str(tmp_path / "a.json")], tmp_path)
    assert result.returncode != 0
    assert "no reviewed texture locators" in result.stderr
    # Nothing was written to the audit path on refusal.
    assert not (tmp_path / "a.json").exists()


# --------------------------------------------------------------------------- #
# 7. the auditor runs end-to-end on synthetic fixtures, writes the audit only at
#    the explicit --audit path, and fails closed on fake archive bytes.
# --------------------------------------------------------------------------- #
def test_auditor_explicit_paths_and_fail_closed_document(tmp_path, workspace):
    source = workspace["source"]
    output = tmp_path / "repacked" / "fake_bundle_0001.unity3d"
    output.parent.mkdir()
    output.write_bytes(b"NOT-A-UNITY-BUNDLE repacked bytes")
    manifest = write_manifest(tmp_path / "cohort", [manifest_row(
        remote="fake_bundle_0001", source=source, original_png=str(workspace["original"]),
        restored_png=workspace["restored"])])
    report = tmp_path / "reports" / "inject-report.json"
    report.parent.mkdir()
    report.write_text(json.dumps({
        "kind": "reviewed-texture-injection-report",
        "schema_version": 1,
        "mode": "isolated_reviewed_texture_injection_not_published",
        "install_manifest": str(manifest),
        "inventory": str(tmp_path / "inventory.json"),
        "bundles": [{
            "bundle": "fake_bundle_0001.unity3d", "remote": "fake_bundle_0001",
            "source_bundle": str(source), "source_sha256": sha256_file(source),
            "output_bundle": str(output), "output_sha256": sha256_file(output),
            "source_size": source.stat().st_size, "output_size": output.stat().st_size,
            "object_count": 0, "texture_count": 1, "roundtrip_verified": True,
            "converted_texture_formats": {}, "reused_existing": False,
        }],
    }, indent=2), encoding="utf-8")

    audit = tmp_path / "audits" / "audit.json"
    result = run(AUDITOR, ["--report", str(report), "--install-manifest", str(manifest),
                           "--audit", str(audit), "--allow-partial"], tmp_path)
    assert result.returncode == 2, result.stdout + result.stderr

    document = json.loads(audit.read_text(encoding="utf-8"))
    assert document["kind"] == "independent-reviewed-image-unity-repack-audit"
    assert Path(document["report_audited"]).resolve() == report.resolve()
    assert document["report_audited_sha256"] == sha256_file(report)
    assert document["install_manifest"] == str(manifest)
    assert document["install_manifest_sha256"] == sha256_file(manifest)
    assert document["audited_bundles"] == 1 and document["passed_bundles"] == 0
    assert document["failed_bundles"] == 1 and document["errors"]
    assert "fake_bundle_0001" in json.dumps(document["errors"])
    assert document["scope"].startswith("isolated candidate only")
    # The audit landed exactly at the explicit --audit path; nothing appeared
    # beside the report (no report-adjacent default output).
    assert sorted(p.name for p in report.parent.iterdir()) == [report.name]
    assert sorted(p.name for p in (tmp_path / "audits").iterdir()) == ["audit.json"]


# --------------------------------------------------------------------------- #
# 8. the two resolution helpers a synthetic bundle can never reach, and source
#    hygiene: no environment read, no repository path, no default machinery.
# --------------------------------------------------------------------------- #
def test_resolution_is_cli_only_and_sources_are_clean(tmp_path, workspace, monkeypatch):
    module = load_module(INJECTOR, "injector_resolution")
    row = {"source_id": "fake:7"}
    root = tmp_path / "roots" / "originals"
    root.mkdir(parents=True)

    with pytest.raises(module.RefusedInput) as error:
        module.resolve_original_png("nested/original.png", None, row)
    assert "--original-root" in str(error.value)
    assert module.resolve_original_png("nested/original.png", root, row) == \
        root / "nested/original.png"
    absolute = workspace["original"]
    assert module.resolve_original_png(str(absolute), None, row) == absolute
    assert module.resolve_original_root(None, tmp_path / "m") is None
    with pytest.raises(module.RefusedInput):
        module.resolve_original_root(str(workspace["source"]), tmp_path / "m")  # a file

    # Source hygiene: no environment read and no embedded repository path.
    injector_text = INJECTOR.read_text(encoding="utf-8")
    auditor_text = AUDITOR.read_text(encoding="utf-8")
    for text, name in ((injector_text, INJECTOR.name), (auditor_text, AUDITOR.name)):
        assert "os.environ" not in text and "getenv" not in text, name
        assert "MANIFEST_ENV" not in text and "ORIGINAL_ROOT_ENV" not in text, name
        assert "mltd-current" not in text, name
        assert "work/image-localization-25" not in text, name
        assert "reviewed937-texture-stage" not in text, name
        assert "D:/" not in text and "D:\\" not in text, name
    # The auditor keeps its documented report-relative *input* resolution (for
    # source/output bundle paths carried by the report) but names the manifest
    # and the audit output explicitly.
    assert "_resolve" in auditor_text and "report_path.parent" in auditor_text
    assert "--install-manifest" in auditor_text and "required=True" in auditor_text
