"""Completion needs source identity, review provenance and real PNG bytes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_ci_state as state


def fixture(root):
    images = []
    sources = []
    for i in range(2):
        refs = {}
        for kind, color in [('original', (i + 1, 20, 30)), ('localized', (i + 1, 40, 50))]:
            path = root / f'images/{kind}/{i}.png'
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (4, 3), color).save(path)
            refs[kind] = {'relative_path': path.relative_to(root).as_posix(),
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        images.append({'id': f'bundle{i}.unity3d:{i}', 'dimensions': {'width': 4, 'height': 3},
                       'review_status': 'user_approved_for_isolated_install_staging', **refs})
        sources.append({'sha256': refs['original']['sha256'], 'source_ids': [f'bundle{i}:{i}'],
                        'dimensions': {'width': 4, 'height': 3}})
    cohort = {'schema_version': 1, 'kind': 'mltd-frozen-image-cohort', 'cohort_id': 'synthetic',
              'expected_images': 2, 'sources': sources}
    manifest = {'kind': 'mltd-images-manifest', 'counts': {'total_images': 2, 'reviewed_textures': 2},
                'images': images}
    return cohort, manifest


def test_reviewed_real_bytes_are_current(tmp_path):
    cohort, manifest = fixture(tmp_path)
    result = state.inspect(tmp_path, cohort, manifest)
    assert result['status'] == 'reviewed_outputs_current'
    assert result['verified_reviewed_images'] == 2
    assert not result['needs_work']


def test_unreviewed_output_is_pending_not_approved(tmp_path):
    cohort, manifest = fixture(tmp_path)
    manifest['images'][0]['review_status'] = 'generated_unreviewed'
    manifest['counts']['reviewed_textures'] = 1
    assert state.inspect(tmp_path, cohort, manifest)['pending'] == ['bundle0.unity3d:0']


def test_external_originals_keep_source_binding_and_verify_outputs(tmp_path):
    cohort, manifest = fixture(tmp_path)
    for row in manifest['images']:
        (tmp_path / row['original']['relative_path']).unlink()
    assert state.inspect(tmp_path, cohort, manifest)['verified_reviewed_images'] == 2
    manifest['images'][0]['original']['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='locator/source'):
        state.inspect(tmp_path, cohort, manifest)


@pytest.mark.parametrize('kind', ['original', 'localized'])
def test_modified_approved_bytes_fail_closed(tmp_path, kind):
    cohort, manifest = fixture(tmp_path)
    (tmp_path / manifest['images'][0][kind]['relative_path']).write_bytes(b'broken')
    with pytest.raises(ValueError, match='SHA mismatch'):
        state.inspect(tmp_path, cohort, manifest)


def test_another_texture_with_identical_pixels_cannot_replace_source(tmp_path):
    cohort, manifest = fixture(tmp_path)
    manifest['images'][0]['id'] = 'other.unity3d:0'
    with pytest.raises(ValueError, match='locator/source'):
        state.inspect(tmp_path, cohort, manifest)


def test_manifest_cannot_redefine_source_dimensions(tmp_path):
    cohort, manifest = fixture(tmp_path)
    manifest['images'][0]['dimensions']['width'] = 8
    with pytest.raises(ValueError, match='dimensions differ'):
        state.inspect(tmp_path, cohort, manifest)


def test_duplicate_source_and_incorrect_reviewed_count_fail(tmp_path):
    cohort, manifest = fixture(tmp_path)
    other = copy.deepcopy(manifest)
    other['images'][1] = copy.deepcopy(other['images'][0])
    with pytest.raises(ValueError, match='locator/source'):
        state.inspect(tmp_path, cohort, other)
    manifest['counts']['reviewed_textures'] = 1
    with pytest.raises(ValueError, match='reviewed image count'):
        state.inspect(tmp_path, cohort, manifest)


def test_path_outside_images_is_rejected(tmp_path):
    cohort, manifest = fixture(tmp_path)
    manifest['images'][0]['original']['relative_path'] = '../private.png'
    with pytest.raises(ValueError, match='escapes images'):
        state.inspect(tmp_path, cohort, manifest)


def test_cli_writes_the_actual_decision(tmp_path):
    cohort, manifest = fixture(tmp_path)
    for directory, name, value in [('configs', 'image-cohort.json', cohort),
                                   ('manifests', 'images.manifest.json', manifest)]:
        (tmp_path / directory).mkdir()
        (tmp_path / directory / name).write_text(json.dumps(value), encoding='utf-8')
    output = tmp_path / 'github-output'
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, str(Path(state.__file__)), '--root', str(tmp_path),
                             '--report', str(tmp_path / 'report.json'), '--github-output', str(output)],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == 'needs_work=false\n'


@pytest.mark.parametrize('status', ['time_budget_exhausted', 'completed_bounded_run'])
def test_clean_partial_batch_resumes_without_approval(status):
    assert not state.batch_status({'status': status, 'failed': 0})['audit_ready']


def test_finished_batch_can_reach_audit_but_errors_cannot():
    assert state.batch_status({'status': 'completed_run'})['audit_ready']
    for progress in [{'status': 'running'}, {'status': 'stopped_proxy_http_error'},
                     {'status': 'completed_run', 'failed': 1},
                     {'status': 'completed_run', 'geometry_review': 1}]:
        with pytest.raises(ValueError):
            state.batch_status(progress)
