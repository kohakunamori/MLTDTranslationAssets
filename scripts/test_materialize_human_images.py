"""Manual imports preserve human provenance and never start model requests."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

import materialize_human_images as human


def fixture(root):
    approval = {"logical": "ex4c00959.unity3d", "archive_sha256": "a" * 64,
        "repacked_sha256": "b" * 64, "review_status": human.REVIEW,
        "approval": {"kind": "explicit_user_approval", "input": "images/input.png"},
        "textures": [{"localized": "images/part.png"}]}
    for record, path_key, hash_key in [(approval["approval"], "input", "input_sha256"),
                                       (approval["textures"][0], "localized", "sha256")]:
        path = root / record[path_key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"approved immutable input")
        record[hash_key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return approval


@pytest.mark.parametrize("target", ["upload", "texture"])
def test_tampered_approved_sources_refused(tmp_path, target):
    approval = fixture(tmp_path)
    human.approved_sources(tmp_path, approval)
    name = approval["approval"]["input"] if target == "upload" else approval["textures"][0]["localized"]
    (tmp_path / name).write_bytes(b"different")
    with pytest.raises(ValueError, match="input changed"):
        human.approved_sources(tmp_path, approval)


def test_automatic_review_does_not_claim_human_approval(tmp_path):
    approval = fixture(tmp_path)
    approval["review_status"] = "auto_approved_no_human_signoff"
    with pytest.raises(ValueError, match="Explicit human approval"):
        human.approved_sources(tmp_path, approval)


def test_published_manual_result_skips_network_and_repack(tmp_path, monkeypatch):
    approval = fixture(tmp_path)
    for name, document in {
        "manifests/image-human-approvals.json": {"bundles": [approval]},
        "generated/1077750/manifest.json": {"entries": [{"logical_key": approval["logical"],
            "artifact_sha256": approval["repacked_sha256"], "source_sha256": approval["archive_sha256"]}]},
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(human, "load_version_manifest", lambda path: {"asset_version": "1077750"})
    def unexpected(*args, **kwargs):
        pytest.fail("Published manual result must not download, repack or call a model")
    monkeypatch.setattr(human, "download", unexpected)
    monkeypatch.setattr(human.subprocess, "run", unexpected)
    assert human.materialize(tmp_path, tmp_path / "work") == 0


def test_manual_publication_is_independent_from_disabled_generation():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/assets-generated.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    materialize = next(s for s in steps if s.get("id") == "features")
    assert '["images_publication_enabled"]' in materialize["run"]
    manual = next(s for s in steps if s.get("name") == "Repack explicitly approved image sources without model calls")
    assert "images_publication_enabled" in manual["if"]
    assert "images_generation_enabled" not in manual["if"]
    assert "-r pipelines/image/requirements-injection.txt" in manual["run"]
    assert "human-image-env/bin/python scripts/materialize_human_images.py" in manual["run"]
    policy = json.loads((root / "configs/localization-pipeline.json").read_text(encoding="utf-8"))["images"]
    assert policy["generation_enabled"] is False
    assert policy["publication_enabled"] is True
