"""An image overlay must bind audited bytes to the current official catalogue."""
import hashlib
import json
from pathlib import Path

import msgpack
import pytest

import publish_image_overlay as images


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "source.unity3d"
    artifact = tmp_path / "translated.unity3d"
    source.write_bytes(b"official")
    artifact.write_bytes(b"translated")
    report = tmp_path / "report.json"
    audit = tmp_path / "audit.json"
    record = {"bundle": "picture", "remote": "remote.unity3d", "source_bundle": str(source),
              "output_bundle": str(artifact), "source_size": 8, "output_size": 10,
              "source_sha256": images.sha256_file(source), "output_sha256": images.sha256_file(artifact),
              "roundtrip_verified": True}
    report.write_text(json.dumps({"install_manifest_sha256": "a" * 64, "bundles": [record]}), encoding="utf-8")
    audit_doc = {"kind": "independent-reviewed-image-unity-repack-audit", "errors": [],
                 "failed_bundles": 0, "report_audited_sha256": images.sha256_file(report),
                 "install_manifest_sha256": "a" * 64, "manifest_bundles": 1,
                 "audited_bundles": 1, "passed_bundles": 1}
    audit.write_text(json.dumps(audit_doc), encoding="utf-8")
    version = {"asset_version": "100", "index_name": "index.data"}
    index = {"picture.unity3d": {"catalog_hash": "h", "remote": "remote.unity3d", "declared_size": 8}}
    return tmp_path, version, index, report, audit, artifact, audit_doc


def test_export_publishes_current_index_and_verified_image_bytes(inputs):
    root, version, index, report, audit, artifact, _doc = inputs
    output = root / "overlay"
    result = images.export(version, index, report, audit, output)
    assert result["asset_version"] == "100"
    assert (output / "jp-android/remote.unity3d").read_bytes() == artifact.read_bytes()
    table = msgpack.unpackb((output / "jp-android/index.data").read_bytes(), raw=False)[0]
    assert table["picture.unity3d"][2] == 10
    assert result["records"][0]["original_bytes"] == 8


@pytest.mark.parametrize("defect", ["partial", "errors", "old_remote", "changed_artifact", "wrong_report"])
def test_invalid_overlay_is_refused_before_any_output(inputs, defect):
    root, version, index, report, audit, artifact, audit_doc = inputs
    if defect == "partial":
        audit_doc["manifest_bundles"] = 2
    elif defect == "errors":
        audit_doc["errors"] = [{"error": "failed independent audit"}]
    elif defect == "old_remote":
        index["picture.unity3d"]["remote"] = "new-official-remote.unity3d"
    elif defect == "changed_artifact":
        artifact.write_bytes(b"changed")
    else:
        audit_doc["report_audited_sha256"] = "f" * 64
    audit.write_text(json.dumps(audit_doc), encoding="utf-8")
    output = root / "overlay"
    with pytest.raises(ValueError):
        images.export(version, index, report, audit, output)
    assert not output.exists()
