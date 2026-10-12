"""Disabled images cannot call a provider or block text/lyric packaging."""
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


def test_images_are_explicitly_disabled_in_the_repository():
    assert features.inspect(ROOT)["images_enabled"] is False
    assert "provider" in features.inspect(ROOT)["reason"]


def test_missing_policy_defaults_to_disabled(tmp_path):
    assert features.inspect(tmp_path)["images_enabled"] is False


@pytest.mark.parametrize("enabled", ["false", "true", 1, None])
def test_invalid_enabled_values_fail_before_provider_work(tmp_path, enabled):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/localization-pipeline.json").write_text(json.dumps(
        {"schema_version": 1, "images": {"enabled": enabled}}), encoding="utf-8")
    with pytest.raises(ValueError):
        features.inspect(tmp_path)


def test_feature_cli_is_dependency_free_and_reports_disabled(tmp_path):
    output = tmp_path / "outputs"
    result = subprocess.run([sys.executable, str(Path(features.__file__)), "--root", str(tmp_path),
                             "--github-output", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "images_enabled=false\n"


def test_images_are_called_only_inside_translation_with_two_opt_in_gates():
    jobs = workflow("llm-translate-assets.yml")["jobs"]
    image_job = jobs["images"]
    assert image_job["uses"] == "./.github/workflows/image-localize-assets.yml"
    assert image_job["needs"] == "draft"
    assert "images_enabled == 'true'" in image_job["if"]
    child = workflow("image-localize-assets.yml")
    triggers = child.get("on", child.get(True))
    assert set(triggers) == {"workflow_call"}
    assert triggers["workflow_call"]["inputs"]["enabled"]["default"] is False
    assert "inputs.enabled" in child["jobs"]["localize"]["if"]
    assert "needs.policy.outputs.images_enabled == 'true'" in child["jobs"]["localize"]["if"]
    assert "image-localize-assets.yml" not in (ROOT / ".github/workflows/auto-track-jp-assets.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("enabled,image_result,ready,text_published,image_published,expected", [
    ("false", "skipped", "", "true", "", True),
    ("false", "skipped", "", "false", "", False),
    ("true", "success", "true", "true", "false", True),
    ("true", "success", "true", "false", "true", True),
    ("true", "success", "false", "true", "false", False),
    ("true", "failure", "true", "true", "true", False),
    ("true", "skipped", "", "true", "", False),
])
def test_packaging_waits_for_enabled_images_but_ignores_disabled_images(
        enabled, image_result, ready, text_published, image_published, expected):
    package = workflow("llm-translate-assets.yml")["jobs"]["package"]
    values = {"needs.draft.result": "success", "needs.draft.outputs.images_enabled": enabled,
              "needs.images.result": image_result, "needs.images.outputs.ready": ready,
              "needs.draft.outputs.published": text_published,
              "needs.images.outputs.published": image_published}
    assert condition(package["if"], values) is expected
    values["needs.draft.result"] = "failure"
    assert condition(package["if"], values) is False


def test_image_overlay_is_exported_only_after_independent_repack_audit():
    steps = workflow("image-localize-assets.yml")["jobs"]["localize"]["steps"]
    stage = next(step for step in steps if step.get("name") ==
                 "Inject and independently verify the translated image bundles")
    assert stage["run"].index("verify_bundle_repack.py") < stage["run"].index("publish_image_overlay.py")
    assert stage["if"] == "steps.batch.outputs.audit_ready == 'true'"
    assert not any("gh workflow run assets-generated.yml" in step.get("run", "") for step in steps)


def test_release_packaging_also_respects_the_disabled_image_policy():
    steps = workflow("assets-generated.yml")["jobs"]["build"]["steps"]
    command = next(step["run"] for step in steps if step.get("name") == "Build generated Unity3D release")
    assert '"$IMAGES_ENABLED" = "true"' in command
    assert "localization_features" in command
    assert 'if [ ! -f "$OVERLAY_MANIFEST" ]; then' in command
    assert "its audited overlay is not ready" in command
