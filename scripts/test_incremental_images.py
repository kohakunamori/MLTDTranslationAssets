"""Queue expansion, durable progress and source-bound image publication."""
import copy
import base64
import io
import json
from pathlib import Path

from PIL import Image
import pytest
import yaml

import incremental_images as images


def test_discovery_expands_beyond_frozen_cohort_and_changes_are_requeued():
    row = {'catalog_hash': 'hash', 'remote': 'remote.unity3d', 'declared_size': 10}
    index = {name: dict(row) for name in ('ex4c00001.unity3d', 'ex4c00002.unity3d',
        'hitokoma_100.unity3d', 'exwb002.unity3d', 'music_001.unity3d')}
    state = {'exwb002.unity3d': {'identity': images.identity(row), 'status': 'translated'},
             'hitokoma_100.unity3d': {'identity': ['old', 'remote.unity3d', 10], 'status': 'translated'}}
    chosen, counts, remaining = images.select(index, state, {'ex4c00001.unity3d': 'remote.unity3d'}, 1)
    assert remaining == 2 and len(chosen) == 1
    assert counts['whiteboard']['completed'] == 1
    assert counts['comic']['completed'] == 1 and counts['hitokoma']['pending'] == 1
    assert all(name != 'music_001.unity3d' for name, _ in chosen)


def task_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(images, 'ROOT', tmp_path)
    images.write(tmp_path / 'manifests/asset-version.json', {'asset_version': '100'})
    source = tmp_path / 'work/source.png'
    source.parent.mkdir()
    Image.new('RGBA', (20, 20), (255, 0, 0, 128)).save(source)
    output = source.with_name('edited.png')
    Image.new('RGB', (20, 20), 'blue').save(output)
    digest = images.sha(source)
    task = {'input': 'source.png', 'sha256': digest, 'regions': [{'box': [2, 2, 5, 5], 'source_text': '日本'}],
        'layout': [{'id': 1, 'original': 'source.png', 'source_sha256': digest, 'size': [20, 20], 'box': [0, 0, 20, 20]}]}
    queue = {'asset_version': '100', 'pending_bundles': 3, 'selected_bundles': 1, 'bundles': [
        {'logical': 'hitokoma_1.unity3d', 'identity': ['h', 'remote.unity3d', 9], 'archive_sha256': 'a' * 64,
         'archive': 'archive.unity3d', 'tasks': [task]}]}
    images.write(source.parent / 'queue.json', queue)
    results = {'tasks': {digest: {'status': 'generated', 'output': 'edited.png', 'sha256': images.sha(output),
                                  'provider': {'requested_image_model': 'imas-trans-image'}}}}
    return source.parent, source, output, results


def test_package_keeps_artwork_and_alpha_and_commits_sources_independent_of_releases(tmp_path, monkeypatch):
    work, source, _, results = task_fixture(tmp_path, monkeypatch)
    images.write(work / 'results.json', results)
    images.package(work)
    state = images.read(work / 'next-state.json')['bundles']['hitokoma_1.unity3d']
    localized = Image.open(tmp_path / state['textures'][0]['localized'])
    original = Image.open(source)
    assert localized.getpixel((0, 0)) == original.getpixel((0, 0))
    assert localized.getpixel((3, 3)) == (0, 0, 255, 128)
    assert list(localized.getchannel('A').getdata()) == list(original.getchannel('A').getdata())
    assert state['textures'][0]['audit']['outside_mask_changed_pixels'] == 0
    assert len((work / 'install.jsonl').read_text().splitlines()) == 1


def test_incomplete_batch_does_not_mark_source_complete(tmp_path, monkeypatch):
    work, _, _, _ = task_fixture(tmp_path, monkeypatch)
    images.package(work)
    assert images.read(work / 'next-state.json')['bundles'] == {}
    assert not (work / 'install.jsonl').read_text()


def test_altered_result_or_changed_version_cannot_reach_injector(tmp_path, monkeypatch):
    work, _, output, results = task_fixture(tmp_path, monkeypatch)
    images.write(work / 'results.json', results)
    output.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='SHA-bound'):
        images.package(work)
    images.write(tmp_path / 'manifests/asset-version.json', {'asset_version': '101'})
    with pytest.raises(ValueError, match='version changed'):
        images.package(work)


def test_api_stage_uses_required_model_and_restores_completed_tasks(tmp_path, monkeypatch):
    import provider_config
    work, _, output, results = task_fixture(tmp_path, monkeypatch)
    images.write(tmp_path / 'configs/localization-pipeline.json', {'images': {
        'model': 'imas-trans-image', 'requests_per_minute': 2, 'concurrency': 1}})
    images.write(tmp_path / 'configs/api-models.example.json', {'defaults': {'base_url': 'https://api.test/v1'}})
    monkeypatch.setenv('MLTD_LLM_API_KEY', 'test-key')
    called = []
    def edit(config, raw, prompt):
        called.append(config)
        assert config['image_provider']['min_request_interval_seconds'] == 30
        assert config['image_provider']['model'] == 'imas-trans-image'
        return output.read_bytes(), {'requested_image_model': 'imas-trans-image'}
    monkeypatch.setattr(provider_config, 'image_edit', edit)
    images.generate(work)
    images.generate(work)
    assert len(called) == 1  # completed work is never billed twice


def test_rate_gate_serializes_every_start_including_retries(monkeypatch):
    import provider_config as provider
    now, waits = [100.0], []
    monkeypatch.setattr(provider, 'monotonic', lambda: now[0])
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(provider, 'sleep', sleep)
    monkeypatch.setattr(provider, '_last_image_request_at', 100.0)
    config = {'image_provider': {'min_request_interval_seconds': 30}}
    provider.throttle_image_request(config)
    provider.throttle_image_request(config)
    provider.throttle_image_request(config)
    assert waits == [30, 30, 30]


def test_native_image_alias_protocol_pads_without_distortion_then_removes_padding(monkeypatch):
    import provider_config as provider
    config = copy.deepcopy(provider.DEFAULTS)
    config['image_provider'].update(base_url='https://api.test/v1', model='imas-trans-image',
                                    mode='gemini_generate_content', api_key='test-secret')
    original = Image.new('RGBA', (20, 60), (255, 0, 0, 128))
    source = io.BytesIO(); original.save(source, format='PNG')
    calls = []
    def send(config, request, timeout):
        assert request.full_url == 'https://api.test/v1beta/models/imas-trans-image:generateContent'
        payload = json.loads(request.data)
        assert payload['generationConfig']['imageConfig']['aspectRatio'] == '1:4'
        raw = base64.b64decode(payload['contents'][0]['parts'][1]['inlineData']['data'])
        assert Image.open(io.BytesIO(raw)).size == (20, 80)
        output = io.BytesIO(); Image.new('RGB', (20, 80), 'blue').save(output, format='PNG')
        return {'candidates': [{'content': {'parts': [{'inlineData': {'data': base64.b64encode(output.getvalue()).decode()}}]}}]}
    monkeypatch.setattr(provider, 'send', send)
    monkeypatch.setattr(provider, 'throttle_image_request', lambda _: calls.append('rate-gate'))
    raw, meta = provider.image_edit(config, source.getvalue(), 'Translate Japanese')
    assert Image.open(io.BytesIO(raw)).size == (20, 60)
    assert calls == ['rate-gate']
    assert meta['requested_image_model'] == 'imas-trans-image' and meta['padding_removed']


def test_native_moderation_decision_is_never_retried_as_transport_failure(monkeypatch):
    import provider_config as provider
    config = copy.deepcopy(provider.DEFAULTS)
    config['image_provider']['mode'] = 'gemini_generate_content'
    source = io.BytesIO(); Image.new('RGB', (20, 20)).save(source, format='PNG')
    monkeypatch.setattr(provider, 'throttle_image_request', lambda _: None)
    monkeypatch.setattr(provider, 'send', lambda *args: {'promptFeedback': {'blockReason': 'SAFETY'}})
    with pytest.raises(provider.ProviderImageDeclined):
        provider.image_edit(config, source.getvalue(), 'Translate Japanese')


def test_lossless_unity_encoding_accepts_odd_comic_strip_dimensions():
    from UnityPy.export import Texture2DConverter as converter
    from UnityPy.enums import TextureFormat
    original = Image.new('RGBA', (7, 5), (12, 34, 56, 78))
    original.putpixel((2, 3), (122, 99, 15, 200))
    payload, fmt = converter.image_to_texture2d(original, TextureFormat.RGBA32)
    decoded = converter.parse_image_data(payload, 7, 5, fmt, (2021, 3, 0, 0), 13)
    assert decoded.convert('RGBA').tobytes() == original.tobytes()


def test_gc_and_all_publishers_share_one_lock_and_api_job_has_no_heavy_dependency():
    root = Path(__file__).resolve().parents[1]
    workflows = root / '.github/workflows'
    image = yaml.safe_load((workflows / 'image-localize-assets.yml').read_text())['jobs']
    assert image['publish']['concurrency']['group'] == 'r2-mutation'
    assert image['publish']['concurrency']['queue'] == 'max'
    for file, job in [('assets-generated.yml', 'build'), ('cloudflare-gateway.yml', 'deploy')]:
        assert yaml.safe_load((workflows / file).read_text())['jobs'][job]['concurrency']['group'] == 'r2-mutation'
        assert yaml.safe_load((workflows / file).read_text())['jobs'][job]['concurrency']['queue'] == 'max'
    commands = '\n'.join(step.get('run', '') for step in image['localize']['steps'])
    assert 'UnityPy' not in commands and 'tesseract' not in commands
    assert 'generate' in commands
