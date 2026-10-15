import base64
import unittest

from PIL import Image
import test_image_model as experiment
import image_text_regions as regions
import copy
import io
import json
import urllib.error

import pytest
import compare_image_models as comparison
import image_visual_review as visual


class ImageExperimentTests(unittest.TestCase):
    def test_openai_models_use_edits_in_smoke_test_metadata(self):
        self.assertEqual(experiment.OPENAI_IMAGE_MODELS, comparison.MODELS)
        for model in comparison.MODELS:
            self.assertEqual(experiment.image_endpoint('https://api.test', model),
                             'https://api.test/v1/images/edits')
        self.assertEqual(experiment.image_endpoint('https://api.test', 'gemini-3.1-flash-image'),
                         'https://api.test/v1beta/models/gemini-3.1-flash-image:generateContent')

    def test_request_uses_native_gemini_parts_and_image_modalities(self):
        body = experiment.payload("汉化", b"png", "1:4", "2K")
        parts = body["contents"][0]["parts"]
        self.assertEqual(parts[1]["inlineData"], {"mimeType": "image/png", "data": "cG5n"})
        self.assertEqual(body["generationConfig"]["responseModalities"], ["TEXT", "IMAGE"])
        self.assertNotIn("messages", body)

    def test_assembly_retains_part_order_and_original_pixel_coordinates(self):
        a, b = Image.new("RGBA", (3, 2), "red"), Image.new("RGBA", (3, 1), "blue")
        canvas, layout = experiment.assemble([("a", a), ("b", b)])
        self.assertEqual(canvas.size, (3, 3))
        self.assertEqual(canvas.getpixel((1, 1)), a.getpixel((1, 1)))
        self.assertEqual(canvas.getpixel((1, 2)), b.getpixel((1, 0)))
        self.assertEqual(layout[1]["box"], [0, 2, 3, 3])

    def test_response_requires_one_actual_image_and_ignores_thought_parts(self):
        raw = experiment.png(Image.new("RGB", (2, 2), "red"))
        inline = {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(raw).decode()}}
        doc = {"candidates": [{"content": {"parts": [dict(inline, thought=True), inline]}}]}
        image, _ = experiment.decode_response(doc)
        self.assertEqual(image.size, (2, 2))
        doc["candidates"][0]["content"]["parts"].append(inline)
        with self.assertRaisesRegex(ValueError, "got 2"):
            experiment.decode_response(doc)


class TextRegionRestoreTests(unittest.TestCase):
    def test_source_decoder_rounding_tolerance_does_not_admit_art_changes(self):
        source = Image.new("RGBA", (2, 2), (100, 100, 100, 255))
        rounded = Image.new("RGBA", (2, 2), (101, 100, 99, 255))
        self.assertEqual(regions.verify_decoded_source(source, rounded), 1)
        with self.assertRaisesRegex(ValueError, "differs"):
            regions.verify_decoded_source(source, Image.new("RGBA", (2, 2), (102, 100, 100, 255)))
        with self.assertRaisesRegex(ValueError, "differs"):
            regions.verify_decoded_source(source, Image.new("RGBA", (2, 2), (100, 100, 100, 254)))

    def test_rgb_changes_with_unchanged_alpha_are_checked_and_confined_to_mask(self):
        original = Image.new("RGBA", (4, 4), (255, 0, 0, 128))
        generated = Image.new("RGBA", (8, 8), (0, 255, 0, 0))
        result, report = regions.restore(original, generated, (4, 4), [{"box": [1, 1, 3, 3]}], [[0, 0, 1, 1]])
        self.assertEqual(result.getpixel((0, 0)), original.getpixel((0, 0)))
        self.assertEqual(result.getpixel((1, 1)), (0, 255, 0, 128))
        self.assertEqual(list(result.getchannel("A").getdata()), list(original.getchannel("A").getdata()))
        self.assertEqual(report["outside_mask_changed_pixels"], 0)
        self.assertEqual(report["changed_pixels"], 4)

    def test_a_region_cannot_include_protected_artwork(self):
        source = Image.new("RGB", (4, 4))
        with self.assertRaisesRegex(ValueError, "overlaps protected"):
            regions.restore(source, source, (4, 4), [{"box": [0, 0, 3, 3]}], [[2, 2, 4, 4]])

    def test_changed_canvas_ratio_is_refused(self):
        with self.assertRaisesRegex(ValueError, "aspect ratio"):
            regions.restore(Image.new("RGB", (4, 4)), Image.new("RGB", (4, 8)), (4, 4), [])

    def test_out_of_bounds_or_empty_regions_are_refused(self):
        for box in ([0, 0, 5, 4], [1, 1, 1, 2]):
            with self.assertRaisesRegex(ValueError, "coordinates"):
                regions.text_mask((4, 4), [{"box": box}])


def test_openai_comparison_uses_only_edits_with_identical_inputs_and_resumes(tmp_path, monkeypatch):
    provider = comparison.provider
    source = tmp_path / 'source.png'
    raw = experiment.png(Image.new('RGB', (24, 80), 'red'))
    source.write_bytes(raw)
    config = copy.deepcopy(provider.DEFAULTS)
    config['image_provider'].update(base_url='https://api.test/v1', mode='images_edits', api_key='test-secret')
    sent, gates = [], []
    monkeypatch.setattr(provider, 'throttle_image_request', lambda c: gates.append(c['image_provider']['min_request_interval_seconds']))
    monkeypatch.setattr(comparison.time, 'sleep', lambda _: None)
    def send(config, request, timeout):
        assert request.full_url == 'https://api.test/v1/images/edits'
        assert request.get_header('Content-type').startswith('multipart/form-data;')
        assert b'name="mask"' not in request.data and b'inlineData' not in request.data
        assert b'name="n"\r\n\r\n1\r\n' in request.data
        assert raw in request.data and b'Translate Japanese' in request.data
        sent.append(config['image_provider']['model'])
        return {'data': [{'b64_json': base64.b64encode(raw).decode()}]}
    monkeypatch.setattr(provider, 'send', send)
    out = tmp_path / 'comparison'
    result = comparison.compare(source, 'Translate Japanese', out, config)
    assert sent == list(comparison.MODELS) and gates == [30, 30, 30]
    assert not result['mask_applied'] and result['review_status'] == 'unapproved'
    assert all(v['geometry_matches'] for v in result['models'].values())
    comparison.compare(source, 'Translate Japanese', out, config)
    assert len(sent) == 3
    with pytest.raises(ValueError, match='different inputs'):
        comparison.compare(source, 'Changed prompt', out, config)
    (out / (comparison.MODELS[0] + '.png')).write_bytes(b'changed')
    with pytest.raises(ValueError, match='do not generate again'):
        comparison.compare(source, 'Translate Japanese', out, config)
    assert 'test-secret' not in (out / 'summary.json').read_text()


def test_openai_comparison_records_http_failure_without_fallback_or_retry(tmp_path, monkeypatch):
    provider = comparison.provider
    source = tmp_path / 'source.png'
    raw = experiment.png(Image.new('RGB', (24, 80), 'red'))
    source.write_bytes(raw)
    config = copy.deepcopy(provider.DEFAULTS)
    config['image_provider'].update(base_url='https://api.test/v1', mode='images_edits', api_key='test-secret')
    calls = []
    def edit(config, *args):
        calls.append(config['image_provider']['model'])
        raise urllib.error.HTTPError('https://api.test/v1/images/edits', 400, 'bad request', {},
            io.BytesIO(json.dumps({'error': {'code': 'unsupported', 'message': 'test-secret route unavailable'}}).encode()))
    monkeypatch.setattr(provider, 'image_edit', edit)
    monkeypatch.setattr(comparison.time, 'sleep', lambda _: None)
    out = tmp_path / 'comparison'
    result = comparison.compare(source, 'Translate', out, config)
    assert calls == list(comparison.MODELS)
    assert all(v['status'] == 'failed' and v['error']['http_status'] == 400 for v in result['models'].values())
    comparison.compare(source, 'Translate', out, config)
    assert len(calls) == 3 and 'test-secret' not in (out / 'summary.json').read_text()


def test_mixed_comparison_sends_identical_unpadded_input_over_native_routes(tmp_path, monkeypatch):
    provider = comparison.provider
    source = tmp_path / 'source.png'
    raw = experiment.png(Image.new('RGB', (24, 80), 'red'))
    source.write_bytes(raw)
    config = copy.deepcopy(provider.DEFAULTS)
    config['image_provider'].update(base_url='https://api.test/v1', mode='images_edits', api_key='test-secret')
    prompt, calls = 'Fixed Chinese text: 果肉酸酸的', []
    monkeypatch.setattr(provider, 'throttle_image_request', lambda c: None)
    monkeypatch.setattr(comparison.time, 'sleep', lambda _: None)
    def send(config, request, timeout):
        calls.append(request.full_url)
        if request.full_url.endswith('/images/edits'):
            assert raw in request.data and prompt.encode() in request.data
            assert b'name="mask"' not in request.data
            return {'data': [{'b64_json': base64.b64encode(raw).decode()}]}
        assert request.full_url == 'https://api.test/v1beta/models/gemini-3.1-flash-image:generateContent'
        assert request.get_header('X-goog-api-key') == 'test-secret'
        body = json.loads(request.data)
        parts = body['contents'][0]['parts']
        assert parts[0]['text'] == prompt
        assert base64.b64decode(parts[1]['inlineData']['data']) == raw
        assert body['generationConfig'] == {'responseModalities': ['TEXT', 'IMAGE'], 'imageConfig': {'imageSize': '2K'}}
        return {'candidates': [{'content': {'parts': [{'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(raw).decode()}}]}}]}
    monkeypatch.setattr(provider, 'send', send)
    models = ('gpt-image-2.5', comparison.GEMINI_MODEL)
    out = tmp_path / 'comparison'
    result = comparison.compare(source, prompt, out, config, models)
    assert len(calls) == 2 and all(v['status'] == 'generated' for v in result['models'].values())
    assert result['request']['model_protocols'] == {'gpt-image-2.5': 'images_edits', comparison.GEMINI_MODEL: 'gemini_generate_content'}
    assert (out / (comparison.GEMINI_MODEL + '.png')).read_bytes() == raw
    comparison.compare(source, prompt, out, config, models)
    assert len(calls) == 2


def test_gemini_jpeg_is_preserved_without_conversion_and_invalid_bytes_retained(tmp_path, monkeypatch):
    source = tmp_path / 'source.png'
    source.write_bytes(experiment.png(Image.new('RGB', (24, 80), 'red')))
    buffer = io.BytesIO()
    Image.new('RGB', (48, 160), 'blue').save(buffer, format='JPEG')
    jpeg = buffer.getvalue()
    config = copy.deepcopy(comparison.provider.DEFAULTS)
    config['image_provider'].update(base_url='https://api.test/v1', mode='images_edits', api_key='test-secret')
    calls = []
    def edit(*args):
        calls.append(True)
        return jpeg, {'mode': 'gemini_generate_content'}
    monkeypatch.setattr(comparison, 'raw_gemini_edit', edit)
    out = tmp_path / 'jpeg'
    result = comparison.compare(source, 'Fixed text', out, config, (comparison.GEMINI_MODEL,))
    row = result['models'][comparison.GEMINI_MODEL]
    assert row['output_size'] == [48, 160] and row['output'].endswith('.jpg')
    assert (out / row['output']).read_bytes() == jpeg
    comparison.compare(source, 'Fixed text', out, config, (comparison.GEMINI_MODEL,))
    assert len(calls) == 1
    monkeypatch.setattr(comparison, 'raw_gemini_edit', lambda *args: (b'unsupported image', {}))
    out = tmp_path / 'invalid'
    row = comparison.compare(source, 'Fixed text', out, config, (comparison.GEMINI_MODEL,))['models'][comparison.GEMINI_MODEL]
    assert row['status'] == 'failed' and (out / row['raw_output']).read_bytes() == b'unsupported image'


def test_visual_acceptance_binds_two_images_and_reuses_result_without_call(tmp_path, monkeypatch):
    source = experiment.png(Image.new('RGB', (20, 30), 'red'))
    candidate = experiment.png(Image.new('RGB', (20, 30), 'blue'))
    config = visual.config('https://api.test/v1')
    config['image_provider']['api_key'] = 'test-secret'
    calls = []
    assessment = {'verdict': 'pass', 'checks': dict.fromkeys(visual.CHECKS, True), 'issues': []}
    def send(config, request):
        calls.append(request.full_url)
        assert request.full_url == 'https://api.test/v1beta/models/gemini-3.8-flash-high:generateContent'
        body = json.loads(request.data)
        parts = body['contents'][0]['parts']
        assert [base64.b64decode(p['inlineData']['data']) for p in parts if 'inlineData' in p] == [source, candidate]
        assert body['generationConfig']['responseMimeType'] == 'application/json'
        return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(assessment)}]}}]}
    monkeypatch.setattr(visual.provider, 'send', send)
    monkeypatch.setattr(visual.provider, 'throttle_image_request', lambda _: None)
    path = tmp_path / 'review.json'
    report = visual.review(source, candidate, '中文参考', config, path)
    assert visual.approved(report, source, candidate, '中文参考')
    assert visual.review(source, candidate, '中文参考', config, path) == report and len(calls) == 1
    assert not visual.approved(report, source, candidate + b'x', '中文参考')
    with pytest.raises(ValueError, match='different input'):
        visual.review(source, candidate, 'changed', config, path)
    assert 'test-secret' not in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('defect', ['false_check', 'issue', 'uncertain', 'string_bool', 'wrong_model'])
def test_visual_gate_does_not_trust_a_pass_label(defect):
    report = {'status': 'reviewed', 'approved': True, 'binding': visual.binding(b'source', b'candidate', ''),
        'assessment': {'verdict': 'pass', 'checks': dict.fromkeys(visual.CHECKS, True), 'issues': []}}
    if defect == 'false_check': report['assessment']['checks']['text_correct'] = False
    if defect == 'issue': report['assessment']['issues'] = [{'region': 'panel4', 'observed_text': '知道知道', 'expected_text': '知道', 'detail': 'duplicate'}]
    if defect == 'uncertain': report['assessment']['verdict'] = 'uncertain'
    if defect == 'string_bool': report['assessment']['checks']['text_correct'] = 'true'
    if defect == 'wrong_model': report['binding']['model'] = 'other'
    assert not visual.approved(report, b'source', b'candidate', '')


def test_whole_image_normalization_removes_only_new_edges_without_ocr_masks():
    source = Image.new('RGBA', (20, 40), (255, 255, 255, 128))
    generated = Image.new('RGB', (24, 40), 'black')
    generated.paste(Image.new('RGB', (20, 40), 'blue'), (2, 0))
    result, audit = visual.normalize(source, generated)
    assert result.size == source.size and result.getpixel((0, 0)) == (0, 0, 255, 128)
    assert audit['mask_applied'] is False and audit['removed_edge_columns'] == [2, 2]
    with pytest.raises(ValueError, match='geometry'):
        visual.normalize(source, Image.new('RGB', (40, 40), 'blue'))


def test_visual_http_failure_is_unapproved_and_not_retried(tmp_path, monkeypatch):
    raw = experiment.png(Image.new('RGB', (2, 2)))
    calls = []
    def request(*args):
        calls.append(True)
        raise urllib.error.HTTPError('https://api.test', 429, 'quota', {}, io.BytesIO(b'secret'))
    monkeypatch.setattr(visual, 'request_review', request)
    path = tmp_path / 'failure.json'
    assert not visual.review(raw, raw, '', {}, path)['approved']
    assert visual.review(raw, raw, '', {}, path)['status'] == 'failed' and len(calls) == 1
    assert 'secret' not in path.read_text()


if __name__ == "__main__":
    unittest.main()
