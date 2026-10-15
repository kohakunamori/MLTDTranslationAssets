"""Offline proofs for old-image publication without any image generation."""
import contextlib
import io
import json
from pathlib import Path
from unittest import mock

import msgpack
import pytest

import build_generated_release as build
from assets_generated_index import GeneratedStore
import reuse_published_images as images
from test_build_generated_release import _write_synthetic_root, _fabricate_overlay

OFFICIAL = b"official-image"
TRANSLATED = b"published-translation"
LOGICAL = "picture.unity3d"


@pytest.fixture
def fixture(tmp_path):
    store = GeneratedStore(tmp_path / "generated")
    artifact = tmp_path / "image.bundle"
    artifact.write_bytes(TRANSLATED)
    entry = {"logical_key": LOGICAL,
             "logical_path": "production/2018/Android/" + LOGICAL,
             "runtime_path": "production/2018/Android/old.unity3d",
             "resource_kind": "texture", "source_sha256": build.sha256_bytes(OFFICIAL),
             "translated_sha256": build.sha256_bytes(TRANSLATED),
             "reuse_status": "exact", "translation_status": "modified",
             "artifact_file": str(artifact)}
    store.build_release("100", [entry], source_commit="a" * 40,
                        translation_commit="b" * 40, generated_commit="c" * 40,
                        source_client_version="9.0.200", ci_run_id="1")
    # A later text-only publication must not erase the image candidates.
    store.manifest_path("101").parent.mkdir(parents=True)
    store.manifest_path("101").write_text(json.dumps({
        "asset_version": "101", "build_status": "success", "entries": []}))
    index = {LOGICAL: {"remote": "new.unity3d", "catalog_hash": "source-id",
                       "declared_size": len(OFFICIAL)}}
    version = {"asset_version": "102", "asset_root": "https://official.invalid/102"}
    calls = []

    def download(url, destination, declared_size, *args, **kwargs):
        calls.append((url, kwargs))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(OFFICIAL)

    def run(**kwargs):
        return images.reuse(store=store, version=version, index=index,
                            work=tmp_path / "work", source_client_version="9.0.200",
                            downloader=kwargs.pop("downloader", download), **kwargs)

    return store, index, calls, run


def test_renamed_route_reuses_exact_bytes_across_text_only_release(fixture):
    store, _index, calls, run = fixture
    entries, report = run()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["runtime_path"] == "production/2018/Android/new.unity3d"
    assert entry["asset_version"] == "102"
    assert Path(entry["artifact_file"]).read_bytes() == TRANSLATED
    assert entry["artifact_sha256"] == build.sha256_bytes(TRANSLATED)
    assert report["source_versions"] == {"100": 1}
    assert calls[0][0] == "https://official.invalid/102/new.unity3d"


@pytest.mark.parametrize("change", ["source", "removed"])
def test_changed_or_removed_official_source_is_not_published(fixture, change):
    _store, index, _calls, run = fixture
    if change == "removed":
        index.clear()
        entries, report = run()
    else:
        def changed(url, destination, declared_size, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x" * len(OFFICIAL))
        entries, report = run(downloader=changed)
    assert entries == []
    assert report["reused"] == 0
    assert len(report["skipped"]) == 1


@pytest.mark.parametrize("defect", ["missing", "corrupt"])
def test_broken_published_artifact_fails_instead_of_dropping_images(fixture, defect):
    store, _index, _calls, run = fixture
    path = store.object_file(build.sha256_bytes(TRANSLATED))
    if defect == "missing":
        path.unlink()
    else:
        path.write_bytes(b"broken")
    with pytest.raises(ValueError, match="missing or corrupt"):
        run()


def test_network_failure_does_not_publish_unverified_images(fixture):
    def fail(*args, **kwargs):
        raise ConnectionError("official source unavailable")
    with pytest.raises(ConnectionError):
        fixture[3](downloader=fail)


def test_no_published_images_is_an_explicit_error(tmp_path):
    with pytest.raises(ValueError, match="no previously published"):
        images.reuse(store=GeneratedStore(tmp_path / "empty"),
                     version={"asset_version": "102"}, index={}, work=tmp_path,
                     source_client_version="9.0.200", downloader=None)


def test_newest_image_decision_is_not_replaced_by_older_translation(fixture):
    store, _index, _calls, run = fixture
    manifest = store.load_manifest("100")
    manifest["asset_version"] = "101"
    manifest["entries"][0]["source_sha256"] = "f" * 64
    store.manifest_path("101").write_text(json.dumps(manifest))
    entries, report = run()
    assert entries == []
    assert report["outcomes"] == {"official_source_changed": 1}


def test_release_combines_reused_images_with_text_in_current_catalogue(tmp_path):
    _write_synthetic_root(tmp_path, images_complete=False)
    output = tmp_path / "generated"
    old = GeneratedStore(output)
    artifact = tmp_path / "old-image"
    artifact.write_bytes(TRANSLATED)
    old.build_release("1077630", [{
        "logical_key": LOGICAL, "logical_path": "production/2018/Android/" + LOGICAL,
        "runtime_path": "production/2018/Android/old.unity3d", "resource_kind": "texture",
        "source_sha256": build.sha256_bytes(OFFICIAL),
        "translated_sha256": build.sha256_bytes(TRANSLATED),
        "reuse_status": "exact", "translation_status": "modified",
        "artifact_file": str(artifact),
    }], source_commit="a" * 40, translation_commit="b" * 40,
       generated_commit="c" * 40, source_client_version="9.0.200", ci_run_id="1")

    def download(url, destination, declared_size, *args, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix == ".data":
            destination.write_bytes(msgpack.packb([{
                "story.gtx.unity3d": ["cat", "remote-hash.unity3d", 12],
                LOGICAL: ["image", "new.unity3d", len(OFFICIAL)],
            }], use_bin_type=True))
        else:
            destination.write_bytes(OFFICIAL if destination.name == "new.unity3d"
                                    else b"bundle-bytes")
        return "fetched"

    args = ["build_generated_release.py", "--version-manifest",
            str(tmp_path / "manifests/asset-version.json"),
            "--work-root", str(tmp_path / "work"), "--output-root", str(output),
            "--source-commit", "a" * 40, "--translation-commit", "b" * 40,
            "--generated-commit", "c" * 40, "--ci-run-id", "2", "--reuse-published-images"]
    with mock.patch.object(build, "ROOT", tmp_path), \
            mock.patch.object(build, "download", side_effect=download), \
            mock.patch.object(build, "run_overlay", side_effect=_fabricate_overlay), \
            mock.patch.object(build, "read_gtx", return_value=("name", "k^原文", b"cipher")), \
            mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()):
        assert build.main() == 0
    manifest = old.load_manifest("1077640")
    entries = {entry["logical_key"]: entry for entry in manifest["entries"]}
    image = entries[LOGICAL]
    assert image["artifact_sha256"] == build.sha256_bytes(TRANSLATED)
    assert image["runtime_path"].endswith("new.unity3d")
    index = msgpack.unpackb((output / entries["__official_asset_index__"]["object_path"]).read_bytes(),
                           raw=False)[0]
    assert index[LOGICAL] == ["image", "new.unity3d", len(TRANSLATED)]
    assert manifest["image_reuse"]["reused"] == 1
    assert "story.gtx.unity3d" in entries
