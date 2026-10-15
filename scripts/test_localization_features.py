"""Paused image generation must not disable publication of existing work."""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import localization_features as features

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def condition(expression, values):
    for key, value in sorted(values.items(), key=lambda pair: len(pair[0]), reverse=True):
        expression = expression.replace(key, repr(value))
    expression = expression.replace("always()", "True").replace("cancelled()", "False")
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", "not ", expression).strip()
    tree = ast.parse("(" + expression + ")", mode="eval")
    allowed = (ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.Constant,
               ast.And, ast.Or, ast.Not, ast.Eq, ast.NotEq, ast.Load)
    assert all(isinstance(node, allowed) for node in ast.walk(tree))
    return eval(compile(tree, "workflow condition", "eval"), {"__builtins__": {}})


def test_image_generation_pauses_for_sample_review_without_disabling_publication():
    assert features.inspect(ROOT)["images_generation_enabled"] is False
    assert features.inspect(ROOT)["images_publication_enabled"] is True
    policy = json.loads((ROOT / 'configs/localization-pipeline.json').read_text())['images']
    assert (policy['model'], policy['requests_per_minute'], policy['concurrency']) == ('gemini-3.1-flash-image', 2, 1)


def test_missing_policy_defaults_to_disabled(tmp_path):
    assert features.inspect(tmp_path)["images_generation_enabled"] is False
    assert features.inspect(tmp_path)["images_publication_enabled"] is False


@pytest.mark.parametrize("field", ["generation_enabled", "publication_enabled"])
@pytest.mark.parametrize("enabled", ["false", "true", 1, None])
def test_invalid_enabled_values_fail_before_provider_work(tmp_path, field, enabled):
    (tmp_path / "configs").mkdir()
    images = {"generation_enabled": False, "publication_enabled": True, field: enabled}
    (tmp_path / "configs/localization-pipeline.json").write_text(json.dumps(
        {"schema_version": 2, "images": images}), encoding="utf-8")
    with pytest.raises(ValueError):
        features.inspect(tmp_path)


def test_feature_cli_is_dependency_free_and_reports_disabled(tmp_path):
    output = tmp_path / "outputs"
    result = subprocess.run([sys.executable, str(Path(features.__file__)), "--root", str(tmp_path),
                             "--github-output", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "images_generation_enabled=false\nimages_publication_enabled=false\n"


@pytest.mark.parametrize("generation", [False, True])
@pytest.mark.parametrize("publication", [False, True])
def test_switches_are_independent(tmp_path, generation, publication):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/localization-pipeline.json").write_text(json.dumps({
        "schema_version": 2, "images": {"generation_enabled": generation,
                                       "publication_enabled": publication}}))
    result = features.inspect(tmp_path)
    assert result["images_generation_enabled"] is generation
    assert result["images_publication_enabled"] is publication


def test_images_have_opt_in_gates_for_translation_and_explicit_image_stage():
    jobs = workflow("llm-translate-assets.yml")["jobs"]
    image_job = jobs["images"]
    assert image_job["uses"] == "./.github/workflows/image-localize-assets.yml"
    assert image_job["needs"] == "draft"
    assert "images_generation_enabled == 'true'" in image_job["if"]
    child = workflow("image-localize-assets.yml")
    triggers = child.get("on", child.get(True))
    assert set(triggers) == {"workflow_call"}
    assert triggers["workflow_call"]["inputs"]["enabled"]["default"] is False
    assert "inputs.enabled" in child["jobs"]["prepare"]["if"]
    assert "needs.policy.outputs.enabled == 'true'" in child["jobs"]["prepare"]["if"]
    assert "stage == 'images'" in workflow('auto-track-jp-assets.yml')['jobs']['images']['if']


@pytest.mark.parametrize("enabled,image_result,ready,text_published,image_published,expected", [
    ("false", "skipped", "", "true", "", True),
    ("false", "skipped", "", "false", "", True),
    ("true", "success", "true", "true", "false", True),
    ("true", "success", "true", "false", "true", True),
    ("true", "success", "false", "true", "false", False),
    ("true", "failure", "true", "true", "true", False),
    ("true", "skipped", "", "true", "", False),
])
def test_packaging_waits_for_enabled_images_but_ignores_disabled_images(
        enabled, image_result, ready, text_published, image_published, expected):
    package = workflow("llm-translate-assets.yml")["jobs"]["package"]
    values = {"needs.draft.result": "success", "needs.draft.outputs.images_generation_enabled": enabled,
              "needs.images.result": image_result, "needs.images.outputs.ready": ready,
              "needs.draft.outputs.published": text_published,
              "needs.images.outputs.published": image_published}
    assert condition(package["if"], values) is expected
    values["needs.draft.result"] = "failure"
    assert condition(package["if"], values) is False


def test_image_overlay_is_exported_only_after_independent_repack_audit():
    jobs = workflow("image-localize-assets.yml")["jobs"]
    assert jobs['publish']['runs-on'] == jobs['prepare']['runs-on'] == 'ubuntu-latest'
    assert jobs['localize']['runs-on'][-1] == 'racknerd-light'
    steps = jobs['publish']['steps']
    stage = next(step for step in steps if step.get("name") ==
                 "Inject and independently verify the translated image bundles")
    assert stage["run"].index("verify_bundle_repack.py") < stage["run"].index("publish_image_overlay.py")
    assert stage["if"] == "steps.package.outputs.has_images == 'true'"
    assert not any("gh workflow run assets-generated.yml" in step.get("run", "") for step in steps)


def test_release_reuses_images_when_generation_is_paused():
    steps = workflow("assets-generated.yml")["jobs"]["build"]["steps"]
    build = next(step for step in steps if step.get("name") == "Build generated Unity3D release")
    policy = next(step for step in steps if step.get("id") == "features")
    command = build["run"]
    assert '"$IMAGES_PUBLICATION_ENABLED" = "true"' in command
    assert build["env"]["IMAGES_PUBLICATION_ENABLED"] == "${{ steps.features.outputs.images_publication_enabled }}"
    assert build["env"]["IMAGES_GENERATION_ENABLED"] == "${{ steps.features.outputs.images_generation_enabled }}"
    assert "--reuse-published-images" in command
    assert "localization_features.py" in policy["run"]
    assert '--github-output "$GITHUB_OUTPUT"' in policy["run"]
    assert 'if [ -f "$OVERLAY_MANIFEST" ]; then' in command
