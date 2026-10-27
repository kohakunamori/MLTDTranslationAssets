"""Production routing and failure propagation across reusable workflow jobs."""
from pathlib import Path
import re

import pytest
import yaml

from ci_pipeline import plan

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / '.github/workflows'


def workflow(name):
    return yaml.safe_load((FLOWS / name).read_text(encoding='utf-8'))


def evaluate(expression, values):
    expression = re.sub(r'\b(?:needs|inputs)\.[\w.-]+', lambda m: repr(values.get(m[0], '')), expression)
    expression = expression.replace('always()', 'True').replace('cancelled()', 'False')
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    expression = re.sub(r'!(?!=)', ' not ', expression)
    return bool(eval(expression.strip(), {'__builtins__': {}}, {}))


def states(stage='watch', **changes):
    result = {'needs.plan.result': 'success', 'needs.plan.outputs.stage': stage,
              'needs.plan.outputs.track': str(stage in ('watch', 'translate', 'full')).lower(),
              'needs.resolve-target.result': 'success', 'needs.resolve-target.outputs.has_update': 'false',
              'needs.freeze.result': 'skipped', 'needs.translate.result': 'success',
              'needs.translate.outputs.ready': 'true', 'needs.translate.outputs.published': 'true',
              'needs.release.result': 'success', 'needs.release.outputs.pushed': 'true',
              'needs.gateway.result': 'success', 'needs.plan.outputs.deploy': 'false'}
    result.update(changes)
    return result


@pytest.mark.parametrize('stage', ['watch', 'translate', 'release', 'gateway', 'full'])
def test_manual_modes_have_explicit_scopes(stage):
    result = plan('workflow_dispatch', stage)
    assert result['track'] is (stage in ('watch', 'translate', 'full'))
    assert result['deploy'] is (stage in ('gateway', 'full'))


def test_push_never_spends_model_tokens_and_detects_gateway_changes():
    assert plan('push', changed=['locales/story/a.jsonl']) == {'stage': 'release', 'track': False, 'deploy': False}
    assert plan('push', changed=['asset-server/cloudflare/src/index.js'])['deploy']
    assert plan('push', changed=['generated/r2-storage.json'])['deploy']
    with pytest.raises(ValueError):
        plan('workflow_dispatch', 'unknown')


def test_only_three_entry_points_and_no_dispatch_chains():
    entries = set()
    for path in FLOWS.glob('*.yml'):
        doc = workflow(path.name)
        triggers = doc.get('on', doc.get(True))
        assert 'schedule' not in triggers  # the external scheduler remains the clock
        if 'workflow_dispatch' in triggers:
            entries.add(path.name)
    assert entries == {'auto-track-jp-assets.yml', 'validate-localization.yml', 'image-model-smoke.yml'}
    assert not (FLOWS / 'workflow-patch.yml').exists()
    for name in ('assets-generated.yml', 'llm-translate-assets.yml', 'cloudflare-gateway.yml', 'image-localize-assets.yml'):
        child = workflow(name)
        assert set(child.get('on', child.get(True))) == {'workflow_call'}
        assert child['concurrency']['group'] != workflow('auto-track-jp-assets.yml')['concurrency']['group']
        assert child['concurrency']['cancel-in-progress'] is False


@pytest.mark.parametrize('stage,extra,expected', [
    ('watch', {}, False),
    ('watch', {'inputs.daily_housekeeping': 'true'}, True),
    ('watch', {'needs.resolve-target.outputs.has_update': 'true', 'needs.freeze.result': 'success'}, True),
    ('watch', {'needs.resolve-target.outputs.has_update': 'true', 'needs.freeze.result': 'failure'}, False),
    ('full', {'needs.resolve-target.result': 'failure'}, False),
    ('full', {'needs.freeze.result': 'failure'}, False),
    ('translate', {}, True),
    ('translate', {'needs.resolve-target.result': 'skipped'}, False),
    ('release', {'needs.resolve-target.result': 'skipped'}, False),
    ('gateway', {}, False),
])
def test_translation_cannot_run_before_required_freeze_or_failed_tracking(stage, extra, expected):
    job = workflow('auto-track-jp-assets.yml')['jobs']['translate']
    assert evaluate(job['if'], states(stage, **extra)) is expected
    assert job['needs'] == ['plan', 'resolve-target', 'freeze']


@pytest.mark.parametrize('stage,extra,expected', [
    ('release', {'needs.translate.result': 'skipped'}, True),
    ('watch', {}, True),
    ('watch', {'needs.translate.outputs.published': 'false'}, False),
    ('full', {'needs.translate.outputs.published': 'false'}, True),
    ('full', {'needs.translate.outputs.ready': ''}, False),
    ('watch', {'needs.translate.result': 'failure'}, False),
    ('gateway', {'needs.translate.result': 'skipped'}, False),
])
def test_release_requires_verified_ready_localization_or_explicit_existing_results(stage, extra, expected):
    job = workflow('auto-track-jp-assets.yml')['jobs']['release']
    assert evaluate(job['if'], states(stage, **extra)) is expected


@pytest.mark.parametrize('stage,extra,expected', [
    ('release', {}, True),
    ('release', {'needs.release.outputs.pushed': ''}, False),
    ('release', {'needs.release.outputs.pushed': '', 'needs.plan.outputs.deploy': 'true'}, True),
    ('release', {'needs.release.result': 'failure', 'needs.plan.outputs.deploy': 'true'}, False),
    ('release', {'needs.release.outputs.rebuild': 'true', 'needs.plan.outputs.deploy': 'true'}, False),
    ('gateway', {'needs.release.result': 'skipped'}, True),
])
def test_gateway_requires_a_published_release_or_explicit_deploy(stage, extra, expected):
    job = workflow('auto-track-jp-assets.yml')['jobs']['gateway']
    assert evaluate(job['if'], states(stage, **extra)) is expected
    assert job['uses'] == './.github/workflows/cloudflare-gateway.yml'


def test_backlog_continuation_is_after_publication_and_needs_a_truncated_loop():
    job = workflow('auto-track-jp-assets.yml')['jobs']['continue-backlog']
    good = states('translate', **{'needs.translate.outputs.remaining': '50',
                                  'needs.translate.outputs.stopped': 'budget'})
    assert evaluate(job['if'], good)
    assert evaluate(job['if'], dict(good, **{'needs.translate.outputs.stopped': 'max_rounds'}))
    # The translate job retries its own leftovers up to four rounds, so another
    # whole run is only justified when that loop was cut short.  A loop that
    # decided to stop would sample the same rows for the same verdicts, and each
    # re-dispatch pays a full rebuild plus a gateway deployment: asset 1077950
    # spent three of them (~48 minutes) settling three lines.
    for stopped in ('drained', 'no_progress', 'limited', ''):
        assert not evaluate(job['if'], dict(good, **{'needs.translate.outputs.stopped': stopped}))
    for key, value in [('needs.translate.outputs.remaining', '0'),
                       ('needs.gateway.result', 'failure'), ('inputs.max_items', '1')]:
        assert not evaluate(job['if'], dict(good, **{key: value}))
    assert 'gateway' in job['needs']
    assert all('gh workflow run' not in step.get('run', '')
               for child in ('llm-translate-assets.yml', 'cloudflare-gateway.yml')
               for value in workflow(child)['jobs'].values() for step in value.get('steps', []))


def test_checkout_uses_actual_source_then_latest_published_metadata():
    parent = workflow('auto-track-jp-assets.yml')['jobs']
    for key in ('resolve-target', 'freeze'):
        checkout = next(s for s in parent[key]['steps'] if s.get('uses', '').startswith('actions/checkout'))
        assert checkout['with']['ref'] == '${{ needs.plan.outputs.source_commit }}'
    translate = workflow('llm-translate-assets.yml')['jobs']
    assert translate['prepare']['steps'][0]['with']['ref'] == '${{ inputs.source_commit }}'
    assert translate['draft']['steps'][0]['with']['ref'] == '${{ needs.prepare.outputs.source_commit }}'
    handoff = next(s for s in translate['prepare']['steps'] if s.get('id') == 'handoff')
    assert 'git rev-parse HEAD' in handoff['run']
    for child, job in [('assets-generated.yml', 'build'), ('cloudflare-gateway.yml', 'deploy')]:
        checkout = next(s for s in workflow(child)['jobs'][job]['steps'] if s.get('uses', '').startswith('actions/checkout'))
        assert checkout['with']['ref'] == 'main'


def test_r2_objects_precede_git_metadata_and_deploy_is_a_dag_dependency():
    steps = workflow('assets-generated.yml')['jobs']['build']['steps']
    names = [s.get('name', '') for s in steps]
    assert names.index('Upload and verify immutable R2 objects before publishing metadata') < names.index('Commit reviewed translations and release metadata')
    parent = workflow('auto-track-jp-assets.yml')['jobs']
    assert 'release' in parent['gateway']['needs']
    assert parent['release']['uses'] == './.github/workflows/assets-generated.yml'
    assert parent['translate']['secrets'] == parent['release']['secrets'] == parent['gateway']['secrets'] == 'inherit'
    assert parent['summary']['if'] == 'always()'


@pytest.mark.parametrize('expected,code,version', [('101', 0, 101), ('100', 1, 100)])
def test_source_update_cannot_jump_to_a_version_without_the_freeze_barrier(tmp_path, monkeypatch, expected, code, version):
    import json
    import sys
    import track_jp_assets as tracker
    target = tmp_path / 'asset-version.json'
    target.write_text(json.dumps({'asset_version': 100}))
    monkeypatch.setattr(tracker, 'VERSION_FILE', target)
    monkeypatch.setattr(tracker, 'fetch_matsurihi_versions', lambda: [{'version': 101, 'indexName': 'a.data'}])
    monkeypatch.setattr(sys, 'argv', ['track', '--update', '--expected-latest-version', expected])
    assert tracker.main() == code
    assert json.loads(target.read_text())['asset_version'] == version
