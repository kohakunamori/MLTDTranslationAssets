"""Bounded native image-edit comparison. No masks, publication or model fallback."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipelines/image'))
import provider_config as provider

MODELS = ('gpt-image-2.5-flare', 'gpt-image-2.5-sunburst', 'gpt-image-2.5')
GEMINI_MODEL = 'gemini-3.1-flash-image'
SUPPORTED_MODELS = (*MODELS, GEMINI_MODEL)


def raw_gemini_edit(config, source, prompt):
    """Keep the exact shared input and raw response; no production padding/crop."""
    image = config['image_provider']
    payload = {'contents': [{'role': 'user', 'parts': [{'text': prompt},
               {'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(source).decode('ascii')}}]}],
               'generationConfig': {'responseModalities': ['TEXT', 'IMAGE'],
                                    'imageConfig': {'imageSize': '2K'}}}
    endpoint = '/v1beta/models/' + GEMINI_MODEL + ':generateContent'
    request = urllib.request.Request(image['base_url'].removesuffix('/v1') + endpoint,
        data=json.dumps(payload).encode(), method='POST',
        headers={**provider.headers(config, 'application/json'), 'x-goog-api-key': provider.api_key(config)})
    provider.throttle_image_request(config)
    result = provider.send(config, request, None)
    outputs = provider.gemini_image_parts(result)
    raw = base64.b64decode(outputs[0]['data'], validate=True)
    return raw, {'requested_image_model': GEMINI_MODEL, 'endpoint': endpoint,
                 'mode': 'gemini_generate_content', 'image_size': '2K',
                 'aspect_ratio_requested': 'auto', 'padding_removed': False}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def png_size(raw):
    if len(raw) < 24 or not raw.startswith(provider.PNG_SIGNATURE) or raw[12:16] != b'IHDR':
        raise ValueError('Expected PNG with an IHDR chunk')
    width, height = struct.unpack('>II', raw[16:24])
    if not width or not height or width * height > 33_554_432:
        raise ValueError('PNG dimensions exceed the comparison bound')
    return [width, height]


def output_info(raw):
    if raw.startswith(provider.PNG_SIGNATURE):
        return png_size(raw), '.png'
    if raw.startswith(b'\xff\xd8'):
        offset = 2
        while offset + 4 <= len(raw):
            if raw[offset] != 255:
                break
            while offset < len(raw) and raw[offset] == 255:
                offset += 1
            if offset >= len(raw):
                break
            marker = raw[offset]
            offset += 1
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            if marker in (0xD9, 0xDA) or offset + 2 > len(raw):
                break
            length = int.from_bytes(raw[offset:offset + 2], 'big')
            if length < 2 or offset + length > len(raw):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 8:
                height, width = struct.unpack('>HH', raw[offset + 3:offset + 7])
                if width and height and width * height <= 33_554_432:
                    return [width, height], '.jpg'
                break
            offset += length
    raise ValueError('Expected bounded PNG or JPEG output; raw response retained')


def write(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def safe_http_error(error, key):
    result = {'http_status': error.code}
    try:
        body = json.loads(error.read(8192)).get('error', {})
    except (ValueError, AttributeError):
        body = {}
    if isinstance(body, dict):
        for field in ('code', 'type', 'param', 'message'):
            if field not in body:
                continue
            text = str(body[field])
            if key:
                text = text.replace(key, '[REDACTED]')
            text = re.sub(r'(?:Bearer\s+|sk-)[^\s"\']+', '[REDACTED]', text, flags=re.I)
            text = re.sub(r'[A-Za-z0-9_+/=-]{40,}', '[REDACTED]', text)
            result[field] = text[:500]
    return result


def compare(source, prompt, output, config, models=MODELS):
    if not models or any(model not in SUPPORTED_MODELS for model in models) or len(set(models)) != len(models):
        raise ValueError('Choose distinct, explicitly supported comparison models')
    raw = source.read_bytes()
    size = png_size(raw)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Comparison input exceeds 16 MiB')
    if config['image_provider']['mode'] != 'images_edits':
        raise ValueError('Comparison requires OpenAI images_edits protocol')
    provider._url(config['image_provider']['base_url'], 'image_provider.base_url')
    key = provider.api_key(config)
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / 'summary.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'schema_version': 1, 'models': {}}
    request = {'source_sha256': digest(raw), 'prompt_sha256': digest(prompt.encode()),
               'endpoint': config['image_provider']['base_url'].rstrip('/') + '/images/edits',
               'mode': 'images_edits', 'quality': config['image_provider']['quality'],
               'size': config['image_provider']['size'], 'output_format': 'png', 'n': 1,
               'min_request_interval_seconds': 30}
    if GEMINI_MODEL in models:
        request.update(mode='native_model_protocols', endpoint=config['image_provider']['base_url'].rstrip('/'),
            model_protocols={model: 'gemini_generate_content' if model == GEMINI_MODEL else 'images_edits' for model in models},
            gemini_image_size='2K', gemini_aspect_ratio='auto')
    if state.get('request') is not None and state['request'] != request:
        raise ValueError('Existing comparison has different inputs; do not overwrite or regenerate')
    state.update(request=request, source_size=size, review_status='unapproved', mask_applied=False)
    (output / 'input.png').write_bytes(raw)
    (output / 'prompt.txt').write_text(prompt, encoding='utf-8')
    write(state_path, state)
    config = copy.deepcopy(config)
    config['image_provider']['min_request_interval_seconds'] = 30
    for model in models:
        previous = state['models'].get(model)
        if previous is not None:
            if previous['status'] == 'generated':
                path = output / previous['output']
                if digest(path.read_bytes()) != previous['sha256']:
                    raise ValueError('Saved comparison output changed; do not generate again')
            print('MODEL_REUSED', model, previous['status'], flush=True)
            continue  # Failures/uncertain in-flight attempts also require explicit review.
        config['image_provider']['model'] = model
        config['image_provider']['mode'] = 'gemini_generate_content' if model == GEMINI_MODEL else 'images_edits'
        pacing = output / 'request-time.json'
        if pacing.exists():
            last = json.loads(pacing.read_text())['started']
            time.sleep(max(0, 30 - (time.time() - last)))
        write(pacing, {'started': time.time()})
        state['models'][model] = {'status': 'in_flight'}
        write(state_path, state)
        started = time.monotonic()
        raw_path = None
        try:
            generated, metadata = (raw_gemini_edit(config, raw, prompt) if model == GEMINI_MODEL
                                   else provider.image_edit(config, raw, prompt))
            raw_path = output / (model + '.bin')
            raw_path.write_bytes(generated)
            returned_size, suffix = output_info(generated)
            path = output / (model + suffix)
            raw_path.replace(path)
            ratio_error = abs((returned_size[0] / returned_size[1]) / (size[0] / size[1]) - 1)
            row = {'status': 'generated', 'output': path.name, 'sha256': digest(generated),
                   'output_size': returned_size, 'aspect_ratio_error': ratio_error,
                   'geometry_matches': ratio_error <= .01, 'provider': metadata}
        except provider.ProviderImageDeclined:
            row = {'status': 'declined', 'reason': 'provider_safety_decision'}
        except urllib.error.HTTPError as error:
            row = {'status': 'failed', 'error': safe_http_error(error, key)}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as error:
            row = {'status': 'failed', 'error_type': type(error).__name__}
            if raw_path is not None and raw_path.exists():
                row.update(raw_output=raw_path.name, raw_sha256=digest(raw_path.read_bytes()))
        row['elapsed_seconds'] = round(time.monotonic() - started, 2)
        state['models'][model] = row
        write(state_path, state)
        print('MODEL_RESULT', model, json.dumps(row, sort_keys=True), flush=True)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--prompt-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', action='append', choices=SUPPORTED_MODELS)
    parser.add_argument('--base-url')
    parser.add_argument('--timeout-seconds', type=int, default=330)
    args = parser.parse_args(argv)
    if not 10 <= args.timeout_seconds <= 1200:
        parser.error('timeout-seconds must be 10..1200')
    public = json.loads((ROOT / 'configs/api-models.example.json').read_text(encoding='utf-8')) if args.base_url is None else None
    config = copy.deepcopy(provider.DEFAULTS)
    config['image_provider'].update(mode='images_edits',
        base_url=(args.base_url or public['defaults']['base_url']).rstrip('/'),
        api_key_env='MLTD_LLM_API_KEY', require_api_key=True, timeout_seconds=args.timeout_seconds)
    result = compare(args.input, args.prompt_file.read_text(encoding='utf-8'), args.output, config, args.model or MODELS)
    return int(any(result['models'][model]['status'] != 'generated' for model in (args.model or MODELS)))


if __name__ == '__main__':
    raise SystemExit(main())
