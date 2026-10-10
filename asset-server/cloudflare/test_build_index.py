#!/usr/bin/env python3
"""Offline regression for the Cloudflare gateway index builder.

Everything here runs against a synthetic repository in a temp directory and
never touches the network.  The index is the only thing that decides which
requests the gateway answers from the repository and which it forwards to the
official CDN, so it is worth pinning that bad input is refused instead of
shipped as a table that would silently serve the wrong bytes.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_index  # noqa: E402

COMMIT = "a" * 40
RUNTIME_A = "production/2018/Android/" + "a" * 40 + ".unity3d"
RUNTIME_B = "production/2018/Android/" + "b" * 40 + ".unity3d"
LOGICAL_A = "production/2018/Android/birth_bdl2_001har_005_jp.gtx.unity3d"
LOGICAL_B = "production/2018/Android/costumesalesinfo0005.unity3d"


def make_repo(root: Path, *, entries, build_status="success", version="1077741", pool_overrides=None):
    """Write a minimal generated release: manifest + content-addressed pool."""
    (root / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "manifests" / "asset-version.json").write_text(
        json.dumps({"asset_version": version}), encoding="utf-8"
    )
    pool = root / "generated" / "objects" / "sha256"
    pool.mkdir(parents=True, exist_ok=True)

    manifest_entries = []
    for runtime, logical, body in entries:
        digest = hashlib.sha256(body).hexdigest()
        (pool / digest).write_bytes((pool_overrides or {}).get(digest, body))
        manifest_entries.append(
            {
                "artifact_sha256": digest,
                "logical_key": logical.split("/")[-1],
                "logical_path": logical,
                "runtime_path": runtime,
                "resource_kind": "bundle",
            }
        )
    (root / "generated" / version).mkdir(parents=True, exist_ok=True)
    (root / "generated" / version / "manifest.json").write_text(
        json.dumps(
            {
                "asset_version": version,
                "build_status": build_status,
                "entries": manifest_entries,
                "generated_commit": COMMIT,
            }
        ),
        encoding="utf-8",
    )
    return root


def build(root: Path, version="1077741", **kwargs):
    kwargs.setdefault("commit", COMMIT)
    return build_index.build_index(root, version, **kwargs)


def test_runtime_and_alias_paths_are_both_indexed(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one"), (RUNTIME_B, LOGICAL_B, b"two")])
    index = build(root)

    assert index.entry_count == 2
    assert index.runtime_keys == 2
    assert index.logical_keys == 2
    assert sorted(index.objects) == sorted([RUNTIME_A, LOGICAL_A, RUNTIME_B, LOGICAL_B])
    # Aliases are the same bytes under a second name: the release is 6 bytes, the
    # table has four paths pointing at them.
    assert index.total_bytes == 6
    assert index.source_commit == COMMIT


def test_alias_can_be_turned_off(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")])
    index = build(root, include_logical_alias=False)
    assert list(index.objects) == [RUNTIME_A]
    assert index.logical_keys == 0


def test_identical_paths_are_not_duplicated(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, RUNTIME_A, b"one")])
    index = build(root)
    assert list(index.objects) == [RUNTIME_A]
    assert index.logical_keys == 0


def test_missing_pool_object_is_refused(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")])
    (root / "generated" / "objects" / "sha256" / hashlib.sha256(b"one").hexdigest()).unlink()
    with pytest.raises(build_index.PlanError, match="missing from the pool"):
        build(root)


def test_pool_bytes_that_do_not_match_their_name_are_refused(tmp_path):
    digest = hashlib.sha256(b"one").hexdigest()
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")], pool_overrides={digest: b"tampered"})
    with pytest.raises(build_index.PlanError, match="does not hash to its own name"):
        build(root)
    # Skipping the hash check lets the tampered bytes through, which is why the
    # check is on by default.
    index = build(root, verify_hashes=False)
    assert index.total_bytes == 8


def test_one_path_claimed_by_two_artifacts_is_refused(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one"), (RUNTIME_A, LOGICAL_B, b"two")])
    with pytest.raises(build_index.PlanError, match="two different artifacts"):
        build(root)


def test_failed_build_is_never_indexed(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")], build_status="failure")
    with pytest.raises(build_index.PlanError, match="build_status"):
        build(root)


def test_path_outside_the_asset_prefix_is_refused(tmp_path):
    root = make_repo(tmp_path, entries=[("production/2019/iOS/x.unity3d", LOGICAL_A, b"one")])
    with pytest.raises(build_index.PlanError, match="outside"):
        build(root)


def test_version_must_be_numeric(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")])
    with pytest.raises(build_index.PlanError, match="purely numeric"):
        build_index.detect_version(root, "9.0.200+1077741")


def test_manifest_version_mismatch_is_refused(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")])
    manifest_path = root / "generated" / "1077741" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["asset_version"] = "1077720"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(build_index.PlanError, match="declares asset_version"):
        build(root)


def test_explicit_commit_must_be_a_full_sha(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")])
    with pytest.raises(build_index.PlanError, match="40-character sha"):
        build_index.detect_commit(root, root / "generated" / "1077741" / "manifest.json", "deadbeef")


def test_commit_is_resolved_from_git_history(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")])
    manifest_path = root / "generated" / "1077741" / "manifest.json"

    def fake_git(command, **kwargs):
        assert command[:3] == ["git", "-C", str(root)]
        assert command[-1] == "generated/1077741/manifest.json"
        return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")

    original = subprocess.run
    subprocess.run = fake_git
    try:
        assert build_index.detect_commit(root, manifest_path, None) == COMMIT
    finally:
        subprocess.run = original


def test_commit_resolution_failure_is_reported(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")])
    manifest_path = root / "generated" / "1077741" / "manifest.json"

    def failing_git(command, **kwargs):
        return subprocess.CompletedProcess(command, 128, "", "fatal: not a git repository")

    original = subprocess.run
    subprocess.run = failing_git
    try:
        with pytest.raises(build_index.PlanError, match="cannot resolve the commit"):
            build_index.detect_commit(root, manifest_path, None)
    finally:
        subprocess.run = original


def test_payload_is_a_flat_path_to_digest_table(tmp_path):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one"), (RUNTIME_B, LOGICAL_B, b"two")])
    index = build(root)
    payload = index.payload(built_at="2026-10-11T00:00:00Z")

    assert payload["schema_version"] == 1
    assert payload["asset_version"] == "1077741"
    assert payload["source_commit"] == COMMIT
    assert payload["object_root"] == "generated/objects/sha256"
    assert payload["objects"][RUNTIME_A] == hashlib.sha256(b"one").hexdigest()
    assert payload["objects"][LOGICAL_A] == hashlib.sha256(b"one").hexdigest()
    assert payload["runtime_keys"] == 2 and payload["logical_keys"] == 2
    json.dumps(payload)  # the Worker imports exactly this as JSON


def test_dry_run_writes_nothing(tmp_path, capsys):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")])
    out = tmp_path / "index.json"
    code = build_index.main(["--repo-root", str(root), "--commit", COMMIT, "--out", str(out)])
    assert code == 0
    assert "dry run" in capsys.readouterr().out
    assert not out.exists()


def test_apply_writes_the_index(tmp_path, capsys):
    root = make_repo(tmp_path, entries=[(RUNTIME_A, LOGICAL_A, b"one")])
    out = tmp_path / "index.json"
    code = build_index.main(["--repo-root", str(root), "--commit", COMMIT, "--out", str(out), "--apply"])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["objects"][RUNTIME_A] == hashlib.sha256(b"one").hexdigest()
    assert payload["built_at"].endswith("Z")
