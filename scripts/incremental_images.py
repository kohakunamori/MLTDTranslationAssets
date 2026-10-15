"""Bounded image discovery, serial API work, and source-bound publication.

Heavy preparation/OCR/repacking is a hosted job. The API stage imports no
UnityPy or OCR engine and carries only PNGs and a SHA-bound task manifest.
Translation sources remain in Git independently of release payload retention.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'pipelines/image'))
JAPANESE = re.compile(r'[\u3040-\u30ff\u3400-\u9fff]')
# These surfaces have a Texture2D writer and known image semantics. Other
# resource families are counted separately; audio/3D art never goes to the API.
FAMILIES = {
    'comic': r'ex4c\d{5}\.unity3d',
    'hitokoma': r'hitokoma(?:_\d+)?\.unity3d',
    'whiteboard': r'exwb\d+\.unity3d',
    'stamp': r'(?:mobile_talk_stamp_[a-z0-9]+_\d+|chat_stamp|igp_producer_stamp_common\d+)\.unity3d',
    'event_banners': r'event_\d+(?:_\d+)?_info(?:_thumb)?\.unity3d',
    'tutorial': r'(?:tutorialinfo|igp_tutorial_info_|specialeventinfo).*\.unity3d',
    'costume_sale': r'(?:costumesalesinfo|pinkyringstonesalesinfo).*\.unity3d',
    'titles_logos': r'(?:event_\d+_(?:title|logo)|titlebg_|title_logo).*\.unity3d',
}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.writing')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def identity(row):
    return [row['catalog_hash'], row['remote'], row['declared_size']]


def select(index, state, published, limit):
    pending, counts = [], {}
    for logical, row in sorted(index.items()):
        family = next((f for f, pattern in FAMILIES.items() if re.fullmatch(pattern, logical)), None)
        if family is None:
            continue
        count = counts.setdefault(family, {'official': 0, 'completed': 0, 'manual_review': 0, 'pending': 0})
        count['official'] += 1
        old = state.get(logical)
        if old and old['identity'] == identity(row):
            count['manual_review' if old['status'] == 'manual_review' else 'completed'] += 1
        elif published.get(logical) == row['remote']:
            count['completed'] += 1
        else:
            count['pending'] += 1
            pending.append((logical, row))
    # Latest comic pages first; deterministic across resumptions.
    pending.sort(key=lambda item: (0 if item[0].startswith('ex4c') else 1,
                                  -int(re.findall(r'\d+', item[0].removesuffix('.unity3d'))[-1])
                                  if re.findall(r'\d+', item[0].removesuffix('.unity3d')) else 0,
                                  item[0]))
    return pending[:limit], counts, len(pending)


def text_regions(path):
    """OCR boxes are the only pixels a model result may replace."""
    result = subprocess.run(['tesseract', str(path), 'stdout', '-l', 'jpn+jpn_vert',
                             '--psm', '11', 'tsv'], check=True, capture_output=True, text=True, encoding='utf-8')
    with Image.open(path) as image:
        width, height = image.size
    boxes = []
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter='\t'):
        if not JAPANESE.search(row.get('text', '')) or float(row['conf']) < 30:
            continue
        x, y, w, h = (int(row[k]) for k in ('left', 'top', 'width', 'height'))
        pad = max(3, min(10, h // 6))
        boxes.append({'box': [max(0, x-pad), max(0, y-pad), min(width, x+w+pad), min(height, y+h+pad)],
                      'source_text': row['text']})
    return boxes


def prepare(work, limit):
    import UnityPy  # Hosted preparation only.
    from build_generated_release import download, load_official_index, load_version_manifest
    version = load_version_manifest(ROOT / 'manifests/asset-version.json')
    index_path = work / version['index_name']
    download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    index = load_official_index(index_path)
    state = read(ROOT / 'manifests/image-incremental.json', {'bundles': {}})
    published = {}
    from r2_release_storage import retained_versions
    for number in reversed(retained_versions(ROOT)):
        release = read(ROOT / 'generated' / number / 'manifest.json', {})
        for entry in release.get('entries', []):
            if entry.get('resource_kind') == 'texture':
                published[entry['logical_key']] = entry['runtime_path'].rsplit('/', 1)[-1]
    chosen, counts, remaining = select(index, state['bundles'], published, limit)
    document = {'schema_version': 1, 'asset_version': version['asset_version'], 'families': counts,
                'pending_bundles': remaining, 'selected_bundles': len(chosen), 'bundles': [],
                'scope': 'registered image families; not all game Texture2D assets'}
    for logical, record in chosen:
        archive = work / 'bundles' / record['remote']
        download(f"{version['asset_root']}/{record['remote']}", archive, record['declared_size'])
        key = hashlib.sha256((logical + sha(archive)).encode()).hexdigest()
        folder = work / 'tasks' / key
        folder.mkdir(parents=True, exist_ok=True)
        textures = []
        for obj in UnityPy.load(str(archive)).objects:
            if obj.type.name != 'Texture2D':
                continue
            texture = obj.read()
            image = texture.image.convert('RGBA')
            if image.width * image.height > 16_777_216:
                raise ValueError('Texture exceeds bounded image task size: ' + logical)
            path = folder / f'{obj.path_id}.png'
            image.save(path)
            textures.append({'id': int(obj.path_id), 'name': str(texture.m_Name),
                             'original': path.relative_to(work).as_posix(),
                             'source_sha256': sha(path), 'size': list(image.size)})
        if not textures:
            raise ValueError('No Texture2D in selected image bundle: ' + logical)
        # Comic parts must be assembled in their explicit numeric reading order.
        if logical.startswith('ex4c'):
            textures.sort(key=lambda t: int(t['name'].rsplit('_', 1)[1]))
            if [int(t['name'].rsplit('_', 1)[1]) for t in textures] != list(range(6)):
                raise ValueError('Comic layout is not the six-part source layout: ' + logical)
            groups = [textures]
        else:
            groups = [[texture] for texture in textures]
        tasks = []
        for number, group in enumerate(groups):
            images = [Image.open(work / t['original']).convert('RGBA') for t in group]
            if len({im.width for im in images}) != 1:
                raise ValueError('Reconstruction widths disagree')
            canvas = Image.new('RGBA', (images[0].width, sum(im.height for im in images)), 'white')
            offset = 0
            layout = []
            for texture, image in zip(group, images):
                canvas.paste(image, (0, offset))
                layout.append({**texture, 'box': [0, offset, image.width, offset + image.height]})
                offset += image.height
            source = folder / f'input-{number}.png'
            canvas.save(source)
            regions = text_regions(source)
            tasks.append({'input': source.relative_to(work).as_posix(), 'sha256': sha(source),
                          'regions': regions, 'layout': layout})
        document['bundles'].append({'logical': logical, 'identity': identity(record),
            'archive': archive.relative_to(work).as_posix(), 'archive_sha256': sha(archive), 'tasks': tasks})
        print('PREPARED', logical, len(tasks), 'edit_regions', sum(len(t['regions']) for t in tasks), flush=True)
    write(work / 'queue.json', document)
    print(json.dumps({k: v for k, v in document.items() if k != 'bundles'}))
    return document


def generate(work, max_seconds=7200):
    import provider_config
    policy = read(ROOT / 'configs/localization-pipeline.json')['images']
    public = read(ROOT / 'configs/api-models.example.json')
    config = {key: dict(value) for key, value in provider_config.DEFAULTS.items()}
    if policy['model'] != 'imas-trans-image' or policy['requests_per_minute'] != 2 or policy['concurrency'] != 1:
        raise ValueError('Image API policy must be serial imas-trans-image at RPM 2')
    config['image_provider'].update(base_url=public['defaults']['base_url'], model=policy['model'],
        mode=policy.get('api_protocol', 'gemini_generate_content'),
        api_key_env='MLTD_LLM_API_KEY', require_api_key=True, min_request_interval_seconds=30)
    # Config and credentials are workflow-owned, never restored from work cache.
    provider_config.api_key(config)
    document = read(work / 'queue.json')
    progress = read(work / 'results.json', {'tasks': {}})
    start = time.monotonic()
    for bundle in document['bundles']:
        for task in bundle['tasks']:
            if not task['regions'] or task['sha256'] in progress['tasks']:
                continue
            if time.monotonic() - start >= max_seconds:
                write(work / 'results.json', progress)
                return
            source = work / task['input']
            if sha(source) != task['sha256']:
                raise ValueError('Image input changed before provider call')
            # Persist UTC pacing across interrupted process retries too.
            last = read(work / 'request-time.json', {'started': 0})['started']
            time.sleep(max(0, 30 - (time.time() - last)))
            write(work / 'request-time.json', {'started': time.time()})
            prompt = ('Translate all Japanese lettering into fluent Simplified Chinese. '
                      'Preserve artwork, characters, logo, panel order and exact canvas geometry. '
                      'Edit text only inside its original regions. Return one PNG. Source OCR: ' +
                      json.dumps([r['source_text'] for r in task['regions']], ensure_ascii=False))
            try:
                raw, metadata = provider_config.image_edit(config, source.read_bytes(), prompt)
                image = Image.open(io.BytesIO(raw)); image.load()
                original = Image.open(source)
                if abs(image.width / image.height / (original.width / original.height) - 1) > .01:
                    progress['tasks'][task['sha256']] = {'status': 'manual_review', 'reason': 'provider_changed_geometry'}
                else:
                    output = source.with_name(source.stem + '-generated.png')
                    output.write_bytes(raw)
                    progress['tasks'][task['sha256']] = {'status': 'generated',
                        'output': output.relative_to(work).as_posix(), 'sha256': sha(output), 'provider': metadata}
            except provider_config.ProviderImageDeclined:
                progress['tasks'][task['sha256']] = {'status': 'manual_review', 'reason': 'provider_safety_decision'}
            except urllib.error.HTTPError as error:
                # Never log provider bodies (they can echo credentials). Only
                # explicit moderation decisions are held without automatic retry.
                try:
                    reason = json.loads(error.read(8192)).get('error', {})
                except (ValueError, AttributeError):
                    reason = {}
                if not isinstance(reason, dict):
                    reason = {'message': str(reason)}
                safe_error = {k: str(reason[k]).replace(provider_config.api_key(config), '[REDACTED]')[:400]
                              for k in ('code', 'type', 'param', 'message') if k in reason}
                reason = {'code': 'moderation_blocked', 'http_status': error.code} if (
                    error.code in (400, 403) and reason.get('code') == 'moderation_blocked') else None
                if reason is None:
                    progress['provider_error'] = {'http_status': error.code, **safe_error}
                    write(work / 'results.json', progress)
                    raise RuntimeError(f'Image provider HTTP {error.code}: {json.dumps(safe_error)}; '
                                       'generated progress retained') from None
                progress['tasks'][task['sha256']] = {'status': 'manual_review', 'reason': reason}
            progress.pop('provider_error', None)
            write(work / 'results.json', progress)
            print('IMAGE_RESULT', bundle['logical'], progress['tasks'][task['sha256']]['status'], flush=True)
    write(work / 'results.json', progress)


def package(work):
    from image_text_regions import restore
    queue = read(work / 'queue.json')
    version = str(read(ROOT / 'manifests/asset-version.json')['asset_version'])
    if version != queue['asset_version']:
        raise ValueError('Official version changed during image work; do not publish stale overlay')
    results = read(work / 'results.json', {'tasks': {}})['tasks']
    state = read(ROOT / 'manifests/image-incremental.json', {'schema_version': 1, 'bundles': {}})
    rows, completed = [], 0
    for bundle in queue['bundles']:
        tasks = bundle['tasks']
        needed = [t for t in tasks if t['regions']]
        if any(t['sha256'] not in results for t in needed):
            continue
        manual = any(results[t['sha256']]['status'] != 'generated' for t in needed)
        record = {'identity': bundle['identity'], 'archive_sha256': bundle['archive_sha256'],
                  'status': 'manual_review' if manual else 'translated' if needed else 'ocr_no_text',
                  'asset_version': version, 'textures': []}
        if not manual:
            for task in needed:
                original = work / task['input']
                result = results[task['sha256']]
                generated = work / result['output']
                if sha(original) != task['sha256'] or sha(generated) != result['sha256']:
                    raise ValueError('SHA-bound image handoff changed')
                source = Image.open(original).convert('RGBA')
                localized, audit = restore(source, Image.open(generated), source.size, task['regions'])
                for texture in task['layout']:
                    original_png = work / texture['original']
                    if sha(original_png) != texture['source_sha256']:
                        raise ValueError('Source Texture2D changed')
                    target = work / 'restored' / bundle['archive_sha256'] / f"{texture['id']}.png"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    localized.crop(texture['box']).save(target)
                    digest = sha(target)
                    destination = ROOT / 'images/localized' / (digest + '.png')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(target.read_bytes())
                    record['textures'].append({'id': texture['id'], 'localized': destination.relative_to(ROOT).as_posix(),
                        'sha256': digest, 'source_sha256': texture['source_sha256'], 'text_regions': task['regions'],
                        'audit': audit, 'model': result['provider']['requested_image_model']})
                    rows.append({'archive_sha256': bundle['archive_sha256'], 'bundle': bundle['logical'],
                        'remote': bundle['identity'][1], 'source_bundle': str((work / bundle['archive']).resolve()),
                        'source_id': bundle['logical'].removesuffix('.unity3d') + ':' + str(texture['id']),
                        'texture_path_id': texture['id'], 'original_png': str(original_png.resolve()),
                        'original_png_sha256': texture['source_sha256'], 'original_size': texture['size'],
                        'restored_png': str(target.resolve()), 'restored_png_sha256': digest,
                        'review_status': 'auto_approved_no_human_signoff'})
        state['bundles'][bundle['logical']] = record
        completed += 1
    install = work / 'install.jsonl'
    install.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    # State is committed only after independent repack audit/R2 upload succeeds.
    write(work / 'next-state.json', state)
    output = {'completed': completed, 'remaining': max(0, queue['pending_bundles'] - completed),
              'has_images': bool(rows), 'ready': completed == queue['selected_bundles']}
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in output.items():
                stream.write(f'{key}={str(value).lower()}\n')
    print(json.dumps(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'generate', 'package'])
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--max-seconds', type=int, default=7200)
    args = parser.parse_args()
    if not 1 <= args.limit <= 50:
        raise ValueError('Batch size must be 1..50 bundles')
    args.work.mkdir(parents=True, exist_ok=True)
    if args.action == 'prepare':
        prepare(args.work, args.limit)
    elif args.action == 'generate':
        generate(args.work, args.max_seconds)
    else:
        package(args.work)


if __name__ == '__main__':
    main()
