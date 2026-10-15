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
    monkeypatch.setattr(images, 'text_regions', lambda _: [])
    images.write(tmp_path / 'manifests/asset-version.json', {'asset_version': '100'})
    images.write(tmp_path / 'configs/localization-pipeline.json', {'images': {
        'model': 'gemini-3.1-flash-image', 'requests_per_minute': 2, 'concurrency': 1}})
    (tmp_path / 'configs/image-translation-prompt.txt').write_text(
        (Path(images.__file__).resolve().parents[1] / 'configs/image-translation-prompt.txt').read_text(encoding='utf-8'), encoding='utf-8')
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
                                  'provider': {'requested_image_model': 'gemini-3.1-flash-image'}}}}
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
    assert state['text_maps'][state['textures'][0]['text_map']]['audit']['outside_mask_changed_pixels'] == 0
    assert len(state['text_maps']) == 1
    assert len((work / 'install.jsonl').read_text().splitlines()) == 1


def test_incomplete_batch_does_not_mark_source_complete(tmp_path, monkeypatch):
    work, _, _, _ = task_fixture(tmp_path, monkeypatch)
    images.package(work)
    assert images.read(work / 'next-state.json')['bundles'] == {}
    assert not (work / 'install.jsonl').read_text()


def test_remaining_japanese_keeps_candidates_but_cannot_publish_partial_bundle(tmp_path, monkeypatch):
    work, _, _, results = task_fixture(tmp_path, monkeypatch)
    images.write(work / 'results.json', results)
    monkeypatch.setattr(images, 'text_regions', lambda _: [{'box': [1, 1, 5, 5],
        'source_text': '\u305d\u3046\u3067\u3057\u3087', 'confidence': 99}])
    images.package(work)
    state = images.read(work / 'next-state.json')['bundles']['hitokoma_1.unity3d']
    assert state['status'] == 'manual_review' and state['review_reason'] == 'remaining_japanese'
    assert not (work / 'install.jsonl').read_text()
    assert state['textures'][0]['localized'].startswith('images/candidates/')
    assert not (tmp_path / 'images/localized').exists()


def test_bad_second_texture_cannot_publish_a_good_first_texture(tmp_path, monkeypatch):
    work, _, _, results = task_fixture(tmp_path, monkeypatch)
    queue = images.read(work / 'queue.json')
    second = copy.deepcopy(queue['bundles'][0]['tasks'][0])
    original = work / 'source-two.png'
    Image.new('RGBA', (20, 20), 'green').save(original)
    second.update(input=original.name, sha256=images.sha(original))
    second['layout'][0].update(id=2, original=original.name, source_sha256=second['sha256'])
    queue['bundles'][0]['tasks'].append(second)
    results['tasks'][second['sha256']] = copy.deepcopy(next(iter(results['tasks'].values())))
    images.write(work / 'queue.json', queue)
    images.write(work / 'results.json', results)
    count = [0]
    def ocr(_):
        count[0] += 1
        return [] if count[0] == 1 else [{'source_text': '\u305d\u3046\u3067\u3057\u3087', 'confidence': 99}]
    monkeypatch.setattr(images, 'text_regions', ocr)
    images.package(work)
    record = images.read(work / 'next-state.json')['bundles']['hitokoma_1.unity3d']
    assert record['status'] == 'manual_review' and len(record['textures']) == 2
    assert not (work / 'install.jsonl').read_text()
    assert not (tmp_path / 'images/localized').exists()


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
        'model': 'gemini-3.1-flash-image', 'requests_per_minute': 2, 'concurrency': 1}})
    images.write(tmp_path / 'configs/api-models.example.json', {'defaults': {'base_url': 'https://api.test/v1'}})
    monkeypatch.setenv('MLTD_LLM_API_KEY', 'test-key')
    called = []
    def edit(config, raw, prompt):
        called.append(config)
        assert config['image_provider']['min_request_interval_seconds'] == 30
        assert config['image_provider']['model'] == 'gemini-3.1-flash-image'
        return output.read_bytes(), {'requested_image_model': 'gemini-3.1-flash-image'}
    monkeypatch.setattr(provider_config, 'image_edit', edit)
    images.generate(work)
    images.generate(work)
    assert len(called) == 1  # completed work is never billed twice


@pytest.mark.parametrize('passed', [False, True])
def test_whole_image_package_requires_bound_visual_pass_and_keeps_raw_generation(tmp_path, monkeypatch, passed):
    import image_visual_review as visual
    work, source, _, results = task_fixture(tmp_path, monkeypatch)
    policy = {'model': 'gpt-image-2.5', 'api_protocol': 'images_edits', 'requests_per_minute': 2,
        'concurrency': 1, 'prompt_profile': 'preserve_layout', 'restoration_mode': 'whole_image',
        'visual_acceptance': {'enabled': True, 'model': visual.MODEL, 'api_protocol': 'gemini_generate_content'}}
    images.write(tmp_path / 'configs/localization-pipeline.json', {'images': policy})
    images.write(tmp_path / 'configs/api-models.example.json', {'defaults': {'base_url': 'https://api.test/v1'}})
    images.write(work / 'results.json', results)
    monkeypatch.setenv('MLTD_LLM_API_KEY', 'test-secret')
    monkeypatch.setattr(visual.provider, 'image_edit', lambda *a: pytest.fail('Existing generation must be reused'))
    calls = []
    def request(*args):
        calls.append(True)
        return {'verdict': 'pass' if passed else 'reject', 'checks': dict.fromkeys(visual.CHECKS, passed), 'issues': []}
    monkeypatch.setattr(visual, 'request_review', request)
    images.generate(work)
    images.generate(work)
    assert len(calls) == 1
    if passed:
        # Japanese lettering inside a preserved game logo is not a translation
        # omission; the bound visual gate, not an unlocated kana count, decides.
        monkeypatch.setattr(images, 'text_regions', lambda _: [{'source_text': 'アイドルマスター', 'confidence': 99}])
    images.package(work)
    state = images.read(work / 'next-state.json')['bundles']['hitokoma_1.unity3d']
    assert bool((work / 'install.jsonl').read_text()) is passed
    assert state['status'] == ('translated' if passed else 'manual_review')
    cached = images.read(tmp_path / 'manifests/image-generation-cache.json')['tasks'][images.sha(source)]
    assert (tmp_path / cached['output']).is_file()
    if passed:
        output = Image.open(tmp_path / state['textures'][0]['localized'])
        assert output.getpixel((0, 0)) == (0, 0, 255, 128)  # no OCR mask pasted the red source back
        assert not next(iter(state['text_maps'].values()))['audit']['mask_applied']
        assert next(iter(state['text_maps'].values()))['ocr_advisory_only']
        assert next(iter(state['text_maps'].values()))['remaining_kana'] >= 3
    else:
        assert state['review_reason'] == 'visual_acceptance_not_passed'
    # A changed reviewer policy/binding cannot turn a saved pass into approval.
    saved = images.read(work / 'results.json')
    saved['tasks'][images.sha(source)]['visual_review']['binding']['model'] = 'other-model'
    images.write(work / 'results.json', saved)
    images.package(work)
    assert not (work / 'install.jsonl').read_text()


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


def test_native_image_model_protocol_pads_without_distortion_then_removes_padding(monkeypatch):
    import provider_config as provider
    config = copy.deepcopy(provider.DEFAULTS)
    config['image_provider'].update(base_url='https://api.test/v1', model='gemini-3.1-flash-image',
                                    mode='gemini_generate_content', api_key='test-secret')
    original = Image.new('RGBA', (20, 60), (255, 0, 0, 128))
    source = io.BytesIO(); original.save(source, format='PNG')
    calls = []
    def send(config, request, timeout):
        assert request.full_url == 'https://api.test/v1beta/models/gemini-3.1-flash-image:generateContent'
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
    assert meta['requested_image_model'] == 'gemini-3.1-flash-image' and meta['padding_removed']


def test_native_moderation_decision_is_never_retried_as_transport_failure(monkeypatch):
    import provider_config as provider
    config = copy.deepcopy(provider.DEFAULTS)
    config['image_provider']['mode'] = 'gemini_generate_content'
    source = io.BytesIO(); Image.new('RGB', (20, 20)).save(source, format='PNG')
    monkeypatch.setattr(provider, 'throttle_image_request', lambda _: None)
    monkeypatch.setattr(provider, 'send', lambda *args: {'promptFeedback': {'blockReason': 'SAFETY'}})
    with pytest.raises(provider.ProviderImageDeclined):
        provider.image_edit(config, source.getvalue(), 'Translate Japanese')


@pytest.mark.parametrize('result,expected', [
    ({}, {'candidate_count': 0, 'image_count': 0}),
    ({'candidates': [{'finishReason': 'NO_IMAGE', 'content': {'parts': [{'text': 'secret'}]}}]},
     {'finish_reasons': ['NO_IMAGE'], 'text_characters': 6, 'image_count': 0}),
    ({'candidates': [{'finishReason': 'secret', 'content': {'parts': [
        {'inlineData': {'data': 'secret'}}, {'inlineData': {'data': 'secret'}}]}}]},
     {'finish_reasons': ['UNKNOWN'], 'image_count': 2}),
    ({'candidates': [{'content': {'parts': [{'thought': True, 'inlineData': {'data': 'secret'}},
        {'inline_data': {'data': 'secret', 'mime_type': 'audio/wav'}}]}}]},
     {'thought_parts': 1, 'inline_parts': 1, 'image_count': 0}),
    ({'candidates': 'secret'}, {'reason': 'invalid_candidates'}),
    (['secret'], {'reason': 'invalid_envelope'}),
])
def test_unusable_native_responses_have_structural_diagnostics_only(result, expected):
    import provider_config as provider
    with pytest.raises(provider.ProviderImageResponseError) as caught:
        provider.gemini_image_parts(result)
    assert expected.items() <= caught.value.diagnostics.items()
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('reason', ['SAFETY', 'IMAGE_SAFETY', 'IMAGE_PROHIBITED_CONTENT',
    'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII', 'RECITATION', 'IMAGE_RECITATION',
    'ESCALATION', 'PUP_LIMITED_DISABLED'])
def test_explicit_refusals_are_classified_even_with_an_image(reason):
    import provider_config as provider
    with pytest.raises(provider.ProviderImageDeclined):
        provider.gemini_image_parts({'candidates': [{'finishReason': reason, 'content': {
            'parts': [{'inlineData': {'data': 'secret'}}]}}]})


@pytest.mark.parametrize('encoded', ['not-base64!', base64.b64encode(b'not an image').decode()])
def test_corrupt_image_is_a_retryable_response_without_raw_content(monkeypatch, encoded):
    import provider_config as provider
    config = copy.deepcopy(provider.DEFAULTS)
    config['image_provider']['mode'] = 'gemini_generate_content'
    source = io.BytesIO(); Image.new('RGB', (20, 20)).save(source, format='PNG')
    monkeypatch.setattr(provider, 'throttle_image_request', lambda _: None)
    monkeypatch.setattr(provider, 'send', lambda *args: {'candidates': [{'content': {
        'parts': [{'inlineData': {'data': encoded}}]}}]})
    with pytest.raises(provider.ProviderImageResponseError) as caught:
        provider.image_edit(config, source.getvalue(), 'Translate Japanese')
    assert caught.value.diagnostics == {'reason': 'invalid_image'}


def configure_api_fixture(tmp_path, monkeypatch):
    images.write(tmp_path / 'configs/localization-pipeline.json', {'images': {
        'model': 'gemini-3.1-flash-image', 'requests_per_minute': 2, 'concurrency': 1}})
    images.write(tmp_path / 'configs/api-models.example.json', {'defaults': {'base_url': 'https://api.test/v1'}})
    monkeypatch.setenv('MLTD_LLM_API_KEY', 'test-key')
    now = [1000.0]
    monkeypatch.setattr(images.time, 'time', lambda: now[0])
    monkeypatch.setattr(images.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(images.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    return now


def test_empty_native_response_retries_at_rpm_two_then_resumes_without_new_call(tmp_path, monkeypatch):
    import provider_config as provider
    work, _, output, _ = task_fixture(tmp_path, monkeypatch)
    now = configure_api_fixture(tmp_path, monkeypatch)
    calls, gates = [], []
    def send(*_):
        calls.append(now[0])
        if len(calls) == 1:
            return {'candidates': [{'finishReason': 'NO_IMAGE', 'content': {'parts': [{'text': 'no image'}]}}]}
        return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
            {'inline_data': {'mime_type': 'image/png', 'data': base64.b64encode(output.read_bytes()).decode()}}]}}]}
    monkeypatch.setattr(provider, 'send', send)
    monkeypatch.setattr(provider, 'throttle_image_request', lambda _: gates.append(now[0]))
    images.generate(work)
    progress = images.read(work / 'results.json')
    assert next(iter(progress['tasks'].values()))['status'] == 'generated'
    assert 'provider_error' not in progress
    assert calls == gates == [1000, 1030]
    images.generate(work)
    assert len(calls) == 2


def test_exhaustion_preserves_good_outputs_and_failed_input_remains_pending(tmp_path, monkeypatch):
    import provider_config as provider
    work, source, output, results = task_fixture(tmp_path, monkeypatch)
    configure_api_fixture(tmp_path, monkeypatch)
    images.write(work / 'results.json', results)
    queue = images.read(work / 'queue.json')
    other = work / 'other.png'
    Image.new('RGB', (20, 20), 'yellow').save(other)
    task = dict(queue['bundles'][0]['tasks'][0], input=other.name, sha256=images.sha(other))
    queue['bundles'][0]['tasks'].append(task)
    images.write(work / 'queue.json', queue)
    calls = []
    def fail(*args):
        calls.append(args[1])
        raise provider.ProviderImageResponseError({'reason': 'image_count', 'image_count': 0})
    monkeypatch.setattr(provider, 'image_edit', fail)
    with pytest.raises(RuntimeError, match='after 3 attempts'):
        images.generate(work)
    progress = images.read(work / 'results.json')
    assert len(calls) == 3 and all(raw == other.read_bytes() for raw in calls)
    assert progress['tasks'] == results['tasks'] and output.exists()
    assert progress['provider_error']['attempt'] == 3
    assert progress['provider_error']['input_sha256'] == task['sha256']
    assert task['sha256'] not in progress['tasks']
    monkeypatch.setattr(provider, 'image_edit', lambda *args: (output.read_bytes(), {}))
    images.generate(work)
    progress = images.read(work / 'results.json')
    assert len(progress['tasks']) == 2 and 'provider_error' not in progress


def test_explicit_decline_is_not_retried_and_budget_does_not_start_late_attempt(tmp_path, monkeypatch):
    import provider_config as provider
    work, _, _, _ = task_fixture(tmp_path, monkeypatch)
    configure_api_fixture(tmp_path, monkeypatch)
    calls = []
    def decline(*args):
        calls.append(True)
        raise provider.ProviderImageDeclined('explicit decision')
    monkeypatch.setattr(provider, 'image_edit', decline)
    images.write(work / 'request-time.json', {'started': 1000})
    images.generate(work, max_seconds=20)
    assert calls == [] and images.read(work / 'results.json')['tasks'] == {}
    images.generate(work)
    assert len(calls) == 1
    result = next(iter(images.read(work / 'results.json')['tasks'].values()))
    assert result == {'status': 'manual_review', 'reason': 'provider_safety_decision'}


@pytest.mark.parametrize('field', ['finishMessage', 'finish_message'])
def test_stop_with_explicit_google_filter_message_is_held_once_then_other_inputs_continue(tmp_path, monkeypatch, field):
    import provider_config as provider
    work, _, output, _ = task_fixture(tmp_path, monkeypatch)
    configure_api_fixture(tmp_path, monkeypatch)
    queue = images.read(work / 'queue.json')
    other = work / 'other.png'
    Image.new('RGB', (20, 20), 'yellow').save(other)
    second = dict(queue['bundles'][0]['tasks'][0], input=other.name, sha256=images.sha(other))
    queue['bundles'].append(dict(queue['bundles'][0], logical='hitokoma_2.unity3d', tasks=[second]))
    images.write(work / 'queue.json', queue)
    calls = []
    def send(config, request, timeout):
        calls.append(request.full_url)
        assert request.full_url.endswith('/v1beta/models/gemini-3.1-flash-image:generateContent')
        if len(calls) == 1:
            return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': []}, field:
                "Unable to show the generated image. The image was filtered out because it violated Google's policy."}]}
        return {'candidates': [{'content': {'parts': [{'inlineData': {
            'data': base64.b64encode(output.read_bytes()).decode()}}]}}]}
    monkeypatch.setattr(provider, 'send', send)
    monkeypatch.setattr(provider, 'throttle_image_request', lambda _: None)
    images.generate(work)
    progress = images.read(work / 'results.json')
    assert len(calls) == 2  # one refusal, then one request for the next input
    first_sha = queue['bundles'][0]['tasks'][0]['sha256']
    assert progress['tasks'][first_sha] == {'status': 'manual_review', 'reason': 'provider_safety_decision'}
    assert progress['tasks'][second['sha256']]['status'] == 'generated'
    assert 'provider_error' not in progress
    images.generate(work)
    assert len(calls) == 2


def test_generic_finish_message_is_not_misclassified_as_moderation():
    import provider_config as provider
    with pytest.raises(provider.ProviderImageResponseError):
        provider.gemini_image_parts({'candidates': [{'finishReason': 'STOP',
            'finishMessage': 'Unable to show the generated image due to a temporary backend error.',
            'content': {'parts': []}}]})


def test_durable_generation_survives_loss_of_actions_cache_without_another_api_call(tmp_path, monkeypatch):
    import provider_config as provider
    work, source, output, results = task_fixture(tmp_path, monkeypatch)
    configure_api_fixture(tmp_path, monkeypatch)
    task = images.read(work / 'queue.json')['bundles'][0]['tasks'][0]
    result = dict(results['tasks'][task['sha256']])
    result.pop('output')
    cache_path = tmp_path / 'images/candidates' / (result['sha256'] + '.png')
    cache_path.parent.mkdir(parents=True); cache_path.write_bytes(output.read_bytes())
    entry = {'prompt_sha256': images.hashlib.sha256(images.image_prompt(task).encode()).hexdigest(),
             'effective_model': 'gemini-3.1-flash-image', 'mode': 'gemini_generate_content',
             'output': cache_path.relative_to(tmp_path).as_posix(), 'result': result}
    images.write(tmp_path / 'manifests/image-generation-cache.json', {'tasks': {task['sha256']: entry}})
    monkeypatch.setattr(provider, 'image_edit', lambda *args: pytest.fail('saved generation was billed again'))
    images.generate(work)
    restored = images.read(work / 'results.json')['tasks'][task['sha256']]
    assert restored['status'] == 'generated'
    assert images.sha(work / restored['output']) == result['sha256']
    (work / 'results.json').unlink()
    cache_path.unlink()
    monkeypatch.setenv('GITHUB_REPOSITORY', 'owner/repository')
    monkeypatch.setattr(images.subprocess, 'check_output', lambda *args, **kwargs: 'a' * 40 + '\n')
    fetched = []
    def fetch(url, timeout):
        fetched.append(url)
        assert timeout == 60
        return io.BytesIO(output.read_bytes())
    monkeypatch.setattr(images.urllib.request, 'urlopen', fetch)
    images.generate(work)
    assert fetched == [f'https://raw.githubusercontent.com/owner/repository/{"a" * 40}/{entry["output"]}']
    (work / 'results.json').unlink()
    cache_path.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        images.generate(work)
    cache_path.write_bytes(output.read_bytes())
    entry['prompt_sha256'] = 'changed'
    images.write(tmp_path / 'manifests/image-generation-cache.json', {'tasks': {task['sha256']: entry}})
    with pytest.raises(ValueError, match='review before regenerating'):
        images.generate(work)


def test_durable_explicit_refusal_is_not_submitted_again(tmp_path, monkeypatch):
    import provider_config as provider
    work, _, _, _ = task_fixture(tmp_path, monkeypatch)
    configure_api_fixture(tmp_path, monkeypatch)
    task = images.read(work / 'queue.json')['bundles'][0]['tasks'][0]
    entry = {'prompt_sha256': images.hashlib.sha256(images.image_prompt(task).encode()).hexdigest(),
             'effective_model': 'gemini-3.1-flash-image', 'mode': 'gemini_generate_content',
             'result': {'status': 'manual_review', 'reason': 'provider_safety_decision'}}
    images.write(tmp_path / 'manifests/image-generation-cache.json', {'tasks': {task['sha256']: entry}})
    monkeypatch.setattr(provider, 'image_edit', lambda *args: pytest.fail('explicit refusal was retried'))
    images.generate(work)
    assert images.read(work / 'results.json')['tasks'][task['sha256']] == entry['result']


def test_new_visual_policy_reuses_old_generation_and_durable_review_after_cache_loss(tmp_path, monkeypatch):
    import image_visual_review as visual
    work, source, output, results = task_fixture(tmp_path, monkeypatch)
    policy = {'model': 'gpt-image-2.5', 'api_protocol': 'images_edits', 'requests_per_minute': 2,
        'concurrency': 1, 'prompt_profile': 'preserve_layout', 'restoration_mode': 'whole_image',
        'visual_acceptance': {'enabled': True, 'model': visual.MODEL, 'api_protocol': 'gemini_generate_content'}}
    images.write(tmp_path / 'configs/localization-pipeline.json', {'images': policy})
    images.write(tmp_path / 'configs/api-models.example.json', {'defaults': {'base_url': 'https://api.test/v1'}})
    monkeypatch.setenv('MLTD_LLM_API_KEY', 'test-secret')
    digest = images.sha(source)
    result = results['tasks'][digest]
    candidate = tmp_path / 'images/candidates' / (result['sha256'] + '.png')
    candidate.parent.mkdir(parents=True); candidate.write_bytes(output.read_bytes())
    images.write(tmp_path / 'manifests/image-generation-cache.json', {'tasks': {digest: {
        'effective_model': 'gemini-3.1-flash-image', 'mode': 'gemini_generate_content',
        'prompt_sha256': 'older-prompt', 'output': candidate.relative_to(tmp_path).as_posix(),
        'result': {k: result[k] for k in ('status', 'sha256', 'provider')}}}})
    monkeypatch.setattr(visual.provider, 'image_edit', lambda *a: pytest.fail('Old generation was billed again'))
    calls = []
    def request(*args):
        calls.append(True)
        return {'verdict': 'reject', 'checks': dict.fromkeys(visual.CHECKS, False), 'issues': []}
    monkeypatch.setattr(visual, 'request_review', request)
    images.generate(work); images.package(work)
    assert len(calls) == 1
    (work / 'results.json').unlink()
    for p in (work / 'visual-reviews').glob('*.json'): p.unlink()
    images.generate(work)
    assert len(calls) == 1  # persisted failed review is reused too
    assert images.read(tmp_path / 'manifests/image-generation-cache.json')['tasks'][digest]['prompt_sha256'] == 'older-prompt'


def test_lossless_unity_encoding_accepts_odd_comic_strip_dimensions():
    from UnityPy.export import Texture2DConverter as converter
    from UnityPy.enums import TextureFormat
    original = Image.new('RGBA', (7, 5), (12, 34, 56, 78))
    original.putpixel((2, 3), (122, 99, 15, 200))
    payload, fmt = converter.image_to_texture2d(original, TextureFormat.RGBA32)
    decoded = converter.parse_image_data(payload, 7, 5, fmt, (2021, 3, 0, 0), 13)
    assert decoded.convert('RGBA').tobytes() == original.tobytes()


def test_version_rename_reuses_full_sha_verified_published_source_without_model_or_unity(tmp_path, monkeypatch):
    import UnityPy
    import build_generated_release as build
    monkeypatch.setattr(images, 'ROOT', tmp_path)
    logical = 'ex4c00968.unity3d'
    source_sha = images.hashlib.sha256(b'official').hexdigest()
    images.write(tmp_path / 'manifests/asset-version.json', {'asset_version': '101'})
    images.write(tmp_path / 'generated/100/manifest.json', {'build_status': 'success', 'entries': [
        {'logical_key': logical, 'source_sha256': source_sha, 'resource_kind': 'texture',
         'runtime_path': 'production/old.unity3d', 'artifact_sha256': 'a'*64}]})
    def download(url, path, _):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'official')
    monkeypatch.setattr(build, 'download', download)
    monkeypatch.setattr(build, 'load_version_manifest', lambda _: {
        'asset_version': '101', 'asset_root': 'https://official.test/101', 'index_name': 'index.data'})
    monkeypatch.setattr(build, 'load_official_index', lambda _: {logical: {
        'catalog_hash': 'same-opaque', 'remote': 'renamed.unity3d', 'declared_size': 8}})
    monkeypatch.setattr(UnityPy, 'load', lambda _: pytest.fail('verified compatible source was unpacked'))
    work = tmp_path / 'work'
    queue = images.prepare(work, 1)
    assert queue['bundles'][0]['tasks'] == []
    assert queue['bundles'][0]['reuse_record']['status'] == 'published_compatible'
    images.package(work)
    record = images.read(work / 'next-state.json')['bundles'][logical]
    assert record['archive_sha256'] == source_sha and record['identity'][1] == 'renamed.unity3d'
    assert not (work / 'install.jsonl').read_text()


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
