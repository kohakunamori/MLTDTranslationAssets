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
import urllib.request

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'pipelines/image'))
JAPANESE = re.compile(r'[\u3040-\u30ff\u3400-\u9fff]')
KANA = re.compile(r'[\u3041-\u3096\u30a1-\u30fa]')
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
                      'source_text': row['text'], 'confidence': float(row['conf'])})
    return boxes


def prepare(work, limit):
    import UnityPy  # Hosted preparation only.
    from build_generated_release import download, load_official_index, load_version_manifest
    version = load_version_manifest(ROOT / 'manifests/asset-version.json')
    index_path = work / version['index_name']
    download(f"{version['asset_root']}/{version['index_name']}", index_path, None)
    index = load_official_index(index_path)
    state = read(ROOT / 'manifests/image-incremental.json', {'bundles': {}})
    published, published_sources = {}, {}
    from r2_release_storage import retained_versions
    for number in reversed(retained_versions(ROOT)):
        release = read(ROOT / 'generated' / number / 'manifest.json', {})
        for entry in release.get('entries', []):
            if entry.get('resource_kind') == 'texture':
                published[entry['logical_key']] = entry['runtime_path'].rsplit('/', 1)[-1]
                published_sources[entry['logical_key']] = entry['source_sha256']
    chosen, counts, remaining = select(index, state['bundles'], published, limit)
    document = {'schema_version': 1, 'asset_version': version['asset_version'], 'families': counts,
                'pending_bundles': remaining, 'selected_bundles': len(chosen), 'bundles': [],
                'scope': 'registered image families; not all game Texture2D assets'}
    for logical, record in chosen:
        archive = work / 'bundles' / record['remote']
        download(f"{version['asset_root']}/{record['remote']}", archive, record['declared_size'])
        archive_sha = sha(archive)
        prior = state['bundles'].get(logical)
        compatible = prior if prior and prior['archive_sha256'] == archive_sha else (
            {'status': 'published_compatible', 'textures': [], 'text_maps': {}}
            if published_sources.get(logical) == archive_sha else None)
        if compatible is not None:
            # Remote names change on version bumps. Verify actual bytes before
            # carrying approved outputs OR explicit hold decisions forward.
            document['bundles'].append({'logical': logical, 'identity': identity(record),
                'archive': archive.relative_to(work).as_posix(), 'archive_sha256': archive_sha,
                'tasks': [], 'reuse_record': compatible})
            print('SOURCE_COMPATIBLE', logical, compatible['status'], flush=True)
            continue
        key = hashlib.sha256((logical + archive_sha).encode()).hexdigest()
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


def validate_image_policy(policy):
    protocol = policy.get('api_protocol', 'gemini_generate_content')
    if ((policy.get('model'), protocol) not in (('gemini-3.1-flash-image', 'gemini_generate_content'), ('gpt-image-2.5', 'images_edits'))
            or policy.get('requests_per_minute') != 2 or policy.get('concurrency') != 1):
        raise ValueError('Image API policy requires an explicit native model/protocol pair and serial RPM 2')
    if policy['model'] == 'gpt-image-2.5':
        acceptance = policy.get('visual_acceptance', {})
        if (acceptance.get('enabled') is not True or acceptance.get('model') != 'gemini-3.8-flash-high'
                or acceptance.get('api_protocol') != 'gemini_generate_content'
                or policy.get('restoration_mode') != 'whole_image' or policy.get('prompt_profile') != 'preserve_layout'):
            raise ValueError('GPT image translation requires preserve-layout whole-image visual acceptance')


def image_prompt(task, profile='legacy'):
    if profile == 'preserve_layout':
        prompt = (ROOT / 'configs/image-translation-prompt.txt').read_text(encoding='utf-8').strip()
        if not prompt:
            raise ValueError('Image translation prompt is empty')
        return prompt + ('\n\n参考译文，请逐字排入对应区域：\n' + task['reference_text'] if task.get('reference_text') else '')
    if profile != 'legacy':
        raise ValueError('Unknown image prompt profile')
    return ('Translate all Japanese lettering into fluent Simplified Chinese. '
            'Every dialogue, caption and sound effect must be fully translated; never leave Japanese kana. '
            'Preserve artwork, characters, logo, panel order and exact canvas geometry. '
            'Edit text only inside its original regions. Return one PNG. Source OCR: ' +
            json.dumps([r['source_text'] for r in task['regions']], ensure_ascii=False))


def reuse_generation(work, task, prompt, config):
    """Restore a durable, unapproved model result without another API request."""
    entry = read(ROOT / 'manifests/image-generation-cache.json', {'tasks': {}})['tasks'].get(task['sha256'])
    if entry is None:
        return None
    if (entry['prompt_sha256'] != hashlib.sha256(prompt.encode()).hexdigest()
            or entry['effective_model'] != config['image_provider']['model']
            or entry['mode'] != config['image_provider']['mode']) and not config.get('reuse_existing_results'):
        raise ValueError('A saved image generation exists with a different request; review before regenerating')
    result = entry['result']
    if result.get('status') == 'manual_review' and result.get('reason') == 'provider_safety_decision':
        return dict(result)
    digest = result['sha256']
    relative = entry['output']
    if result['status'] != 'generated' or not re.fullmatch(r'[0-9a-f]{64}', digest) or relative != f'images/candidates/{digest}.png':
        raise ValueError('Invalid saved image generation')
    path = ROOT / relative
    if path.is_file():
        raw = path.read_bytes()
    else:
        # The light runner omits image blobs from its sparse checkout. Fetch
        # exactly this committed source PNG, pinned to the checkout revision.
        repository = os.environ.get('GITHUB_REPOSITORY', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Saved image generation is missing locally; do not generate it again')
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ValueError('Invalid generation checkpoint revision')
        with urllib.request.urlopen(f'https://raw.githubusercontent.com/{repository}/{revision}/{relative}', timeout=60) as response:
            raw = response.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Saved image generation SHA-256 mismatch; do not generate it again')
    output = (work / task['input']).with_name(Path(task['input']).stem + '-generated.png')
    output.write_bytes(raw)
    return dict(result, output=output.relative_to(work).as_posix(),
                generation_prompt_sha256=entry['prompt_sha256'])


def review_generated(work, task, result, prompt, policy, base_url):
    import image_visual_review as visual
    acceptance = policy.get('visual_acceptance', {})
    if not acceptance.get('enabled') or result['status'] != 'generated':
        return
    if (acceptance.get('model') != visual.MODEL or acceptance.get('api_protocol') != 'gemini_generate_content'
            or policy.get('restoration_mode') != 'whole_image'):
        raise ValueError('Invalid visual acceptance policy')
    source, raw = work / task['input'], work / result['output']
    if sha(source) != task['sha256'] or sha(raw) != result['sha256']:
        raise ValueError('SHA-bound image changed before visual review')
    try:
        normalized, audit = visual.normalize(Image.open(source), Image.open(raw))
    except ValueError:
        result['visual_review'] = {'status': 'failed', 'approved': False, 'reason': 'invalid_geometry'}
        return
    candidate = source.with_name(source.stem + '-candidate.png')
    normalized.save(candidate)
    source_bytes, candidate_bytes = source.read_bytes(), candidate.read_bytes()
    bound = visual.binding(source_bytes, candidate_bytes, prompt)
    key = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
    report_path = work / 'visual-reviews' / (key + '.json')
    embedded = result.get('visual_review')
    if embedded and embedded.get('binding') == bound and not report_path.exists():
        write(report_path, embedded)
    saved = read(ROOT / 'manifests/image-visual-reviews.json', {'reviews': {}})['reviews'].get(key)
    if saved is not None and saved.get('binding') == bound and not report_path.exists():
        write(report_path, saved)
    report = visual.review(source_bytes, candidate_bytes, prompt, visual.config(base_url), report_path)
    result.update(visual_review=report, normalized_output=candidate.relative_to(work).as_posix(),
                  normalized_sha256=sha(candidate), normalization=audit)


def checkpoint_generations(work, queue, results, policy):
    """Keep raw results and review provenance when short-lived work caches expire."""
    cache = read(ROOT / 'manifests/image-generation-cache.json', {'schema_version': 1, 'tasks': {}})
    reviews = read(ROOT / 'manifests/image-visual-reviews.json', {'schema_version': 1, 'reviews': {}})
    for bundle in queue['bundles']:
        for task in bundle['tasks']:
            result = results.get(task['sha256'])
            if not result:
                continue
            if result.get('status') == 'manual_review' and result.get('reason') == 'provider_safety_decision':
                cache['tasks'].setdefault(task['sha256'], {
                    'prompt_sha256': hashlib.sha256(image_prompt(task, policy.get('prompt_profile', 'legacy')).encode()).hexdigest(),
                    'effective_model': policy['model'], 'mode': policy.get('api_protocol', 'gemini_generate_content'),
                    'result': dict(result)})
            if result['status'] != 'generated':
                continue
            raw = work / result['output']
            if sha(work / task['input']) != task['sha256'] or sha(raw) != result['sha256']:
                raise ValueError('SHA-bound image handoff changed')
            destination = ROOT / 'images/candidates' / (result['sha256'] + '.png')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw.read_bytes())
            previous = cache['tasks'].get(task['sha256'])
            if previous is not None and previous.get('result', {}).get('sha256') != result['sha256']:
                raise ValueError('Existing durable generation differs; do not replace it silently')
            if previous is None:
                cache['tasks'][task['sha256']] = {'prompt_sha256': result.get('generation_prompt_sha256',
                    hashlib.sha256(image_prompt(task, policy.get('prompt_profile', 'legacy')).encode()).hexdigest()),
                    'effective_model': result['provider']['requested_image_model'],
                    'mode': result['provider'].get('mode', policy.get('api_protocol', 'gemini_generate_content')),
                    'output': destination.relative_to(ROOT).as_posix(),
                    'result': {k: result[k] for k in ('status', 'sha256', 'provider')}}
            report = result.get('visual_review')
            if report and report.get('binding'):
                key = hashlib.sha256(json.dumps(report['binding'], sort_keys=True).encode()).hexdigest()
                reviews['reviews'][key] = report
    write(ROOT / 'manifests/image-generation-cache.json', cache)
    write(ROOT / 'manifests/image-visual-reviews.json', reviews)


def request_image(config, work, progress, task, source, prompt, deadline):
    """Bound transient response failures; keep failed tasks pending on exhaustion."""
    import provider_config
    for attempt in range(1, 4):
        # Persist UTC pacing across interrupted processes and all retry starts.
        last = read(work / 'request-time.json', {'started': 0})['started']
        wait = max(0, 30 - (time.time() - last))
        if time.monotonic() + wait >= deadline:
            write(work / 'results.json', progress)
            return None
        time.sleep(wait)
        write(work / 'request-time.json', {'started': time.time()})
        try:
            return provider_config.image_edit(config, source.read_bytes(), prompt)
        except provider_config.ProviderImageResponseError as error:
            progress['provider_error'] = {'input_sha256': task['sha256'], 'attempt': attempt,
                                          'max_attempts': 3, **error.diagnostics}
            write(work / 'results.json', progress)
            print('IMAGE_RESPONSE_ERROR', json.dumps(progress['provider_error'], sort_keys=True), flush=True)
            if attempt == 3:
                raise RuntimeError('Image response failed after 3 attempts; generated progress retained; '
                                   'failed input remains pending (see results.json)') from None


def generate(work, max_seconds=7200):
    import provider_config
    policy = read(ROOT / 'configs/localization-pipeline.json')['images']
    public = read(ROOT / 'configs/api-models.example.json')
    config = {key: dict(value) for key, value in provider_config.DEFAULTS.items()}
    validate_image_policy(policy)
    config['image_provider'].update(base_url=public['defaults']['base_url'], model=policy['model'],
        mode=policy.get('api_protocol', 'gemini_generate_content'),
        api_key_env='MLTD_LLM_API_KEY', require_api_key=True, min_request_interval_seconds=30)
    config['reuse_existing_results'] = policy.get('visual_acceptance', {}).get('enabled', False)
    # Config and credentials are workflow-owned, never restored from work cache.
    provider_config.api_key(config)
    document = read(work / 'queue.json')
    progress = read(work / 'results.json', {'tasks': {}})
    start = time.monotonic()
    for bundle in document['bundles']:
        for task in bundle['tasks']:
            if not task['regions']:
                continue
            if time.monotonic() - start >= max_seconds:
                write(work / 'results.json', progress)
                return
            source = work / task['input']
            if sha(source) != task['sha256']:
                raise ValueError('Image input changed before provider call')
            prompt = image_prompt(task, policy.get('prompt_profile', 'legacy'))
            if task['sha256'] in progress['tasks']:
                review_generated(work, task, progress['tasks'][task['sha256']], prompt, policy, public['defaults']['base_url'])
                write(work / 'results.json', progress)
                continue
            reused = reuse_generation(work, task, prompt, config)
            if reused is not None:
                progress['tasks'][task['sha256']] = reused
                review_generated(work, task, reused, prompt, policy, public['defaults']['base_url'])
                progress.pop('provider_error', None)
                write(work / 'results.json', progress)
                print('IMAGE_REUSED', bundle['logical'], reused['status'], flush=True)
                continue
            try:
                response = request_image(config, work, progress, task, source, prompt, start + max_seconds)
                if response is None:
                    return
                raw, metadata = response
                image = Image.open(io.BytesIO(raw)); image.load()
                original = Image.open(source)
                if policy.get('restoration_mode') != 'whole_image' and abs(image.width / image.height / (original.width / original.height) - 1) > .01:
                    progress['tasks'][task['sha256']] = {'status': 'manual_review', 'reason': 'provider_changed_geometry'}
                else:
                    output = source.with_name(source.stem + '-generated.png')
                    output.write_bytes(raw)
                    progress['tasks'][task['sha256']] = {'status': 'generated',
                        'output': output.relative_to(work).as_posix(), 'sha256': sha(output), 'provider': metadata,
                        'generation_prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}
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
            review_generated(work, task, progress['tasks'][task['sha256']], prompt, policy, public['defaults']['base_url'])
            write(work / 'results.json', progress)
            print('IMAGE_RESULT', bundle['logical'], progress['tasks'][task['sha256']]['status'], flush=True)
    progress.pop('provider_error', None)
    write(work / 'results.json', progress)


def package(work):
    from image_text_regions import restore
    import image_visual_review as visual
    queue = read(work / 'queue.json')
    policy = read(ROOT / 'configs/localization-pipeline.json', {'images': {}})['images']
    if any(bundle['tasks'] for bundle in queue['bundles']):
        validate_image_policy(policy)
    whole_image = policy.get('restoration_mode') == 'whole_image'
    if whole_image and not policy.get('visual_acceptance', {}).get('enabled'):
        raise ValueError('Whole-image publication requires visual acceptance')
    version = str(read(ROOT / 'manifests/asset-version.json')['asset_version'])
    if version != queue['asset_version']:
        raise ValueError('Official version changed during image work; do not publish stale overlay')
    results = read(work / 'results.json', {'tasks': {}})['tasks']
    checkpoint_generations(work, queue, results, policy)
    state = read(ROOT / 'manifests/image-incremental.json', {'schema_version': 1, 'bundles': {}})
    rows, completed = [], 0
    for bundle in queue['bundles']:
        if bundle.get('reuse_record') is not None:
            record = dict(bundle['reuse_record'], identity=bundle['identity'],
                          archive_sha256=bundle['archive_sha256'], asset_version=version)
            state['bundles'][bundle['logical']] = record
            completed += 1
            continue
        needed = [t for t in bundle['tasks'] if t['regions']]
        if any(t['sha256'] not in results for t in needed):
            continue
        manual = any(results[t['sha256']]['status'] != 'generated' for t in needed)
        if whole_image and not manual:
            for task in needed:
                result = results[task['sha256']]
                candidate = work / result.get('normalized_output', '__missing_candidate__')
                reference = image_prompt(task, policy.get('prompt_profile', 'legacy'))
                if (not candidate.is_file() or sha(candidate) != result.get('normalized_sha256')
                        or policy['visual_acceptance'].get('model') != visual.MODEL
                        or not visual.approved(result.get('visual_review', {}), (work / task['input']).read_bytes(), candidate.read_bytes(), reference)):
                    manual = True
        record = {'identity': bundle['identity'], 'archive_sha256': bundle['archive_sha256'],
                  'status': 'manual_review' if manual else 'translated' if needed else 'ocr_no_text',
                  'asset_version': version, 'textures': [], 'text_maps': {}}
        if whole_image and manual:
            record['review_reason'] = 'visual_acceptance_not_passed'
        if whole_image:
            record['visual_reviews'] = {t['sha256']: results[t['sha256']].get('visual_review', {'approved': False}) for t in needed}
        parts = []
        if not manual:
            for task in needed:
                original = work / task['input']
                result = results[task['sha256']]
                generated = work / result['output']
                if sha(original) != task['sha256'] or sha(generated) != result['sha256']:
                    raise ValueError('SHA-bound image handoff changed')
                source = Image.open(original).convert('RGBA')
                if whole_image:
                    localized = Image.open(work / result['normalized_output']).convert('RGBA')
                    if localized.size != source.size or localized.getchannel('A').tobytes() != source.getchannel('A').tobytes():
                        raise ValueError('Reviewed normalized image geometry or alpha changed')
                    audit = result['normalization']
                else:
                    localized, audit = restore(source, Image.open(generated), source.size, task['regions'])
                check = work / 'quality' / (task['sha256'] + '.png')
                check.parent.mkdir(parents=True, exist_ok=True)
                localized.save(check)
                residual = [r for r in text_regions(check)
                            if r.get('confidence', 0) >= 70 and KANA.search(r['source_text'])]
                kana_count = sum(len(KANA.findall(r['source_text'])) for r in residual)
                # Whole-image visual acceptance distinguishes protected Japanese
                # game logos from untranslated content; OCR alone cannot do so.
                if kana_count >= 3 and not whole_image:
                    manual = True
                    record['review_reason'] = 'remaining_japanese'
                    record.setdefault('remaining_japanese', []).extend(residual)
                record['text_maps'][task['sha256']] = {'input_sha256': task['sha256'],
                    'size': list(source.size), 'regions': task['regions'], 'audit': audit,
                    'provider': result['provider'], 'remaining_kana': kana_count,
                    'ocr_advisory_only': whole_image}
                for texture in task['layout']:
                    original_png = work / texture['original']
                    if sha(original_png) != texture['source_sha256']:
                        raise ValueError('Source Texture2D changed')
                    target = work / 'restored' / bundle['archive_sha256'] / f"{texture['id']}.png"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    localized.crop(texture['box']).save(target)
                    parts.append((task, texture, target, original_png))
            if parts and not any(m['audit']['changed_pixels'] for m in record['text_maps'].values()):
                manual = True
                record['review_reason'] = 'no_visible_translation_change'
            # Decide the whole bundle before writing any approved source/row.
            # A good first texture cannot approve a bad second texture.
            for task, texture, target, original_png in parts:
                digest = sha(target)
                destination = ROOT / 'images' / ('candidates' if manual else 'localized') / (digest + '.png')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(target.read_bytes())
                record['textures'].append({'id': texture['id'], 'localized': destination.relative_to(ROOT).as_posix(),
                    'sha256': digest, 'source_sha256': texture['source_sha256'],
                    'text_map': task['sha256'], 'composite_box': texture['box']})
                if manual:
                    continue
                rows.append({'archive_sha256': bundle['archive_sha256'], 'bundle': bundle['logical'],
                    'remote': bundle['identity'][1], 'source_bundle': str((work / bundle['archive']).resolve()),
                    'source_id': bundle['logical'].removesuffix('.unity3d') + ':' + str(texture['id']),
                    'texture_path_id': texture['id'], 'original_png': str(original_png.resolve()),
                    'original_png_sha256': texture['source_sha256'], 'original_size': texture['size'],
                    'restored_png': str(target.resolve()), 'restored_png_sha256': digest,
                    'review_status': 'auto_approved_no_human_signoff'})
        if manual:
            record['status'] = 'manual_review'
            record.setdefault('review_reason', 'provider_declined_or_invalid_geometry')
        state['bundles'][bundle['logical']] = record
        completed += 1
    install = work / 'install.jsonl'
    install.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
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
    parser.add_argument('--limit', type=int, default=0, help='0 uses the shared image batch policy')
    parser.add_argument('--max-seconds', type=int, default=7200)
    args = parser.parse_args()
    if args.limit == 0:
        args.limit = read(ROOT / 'configs/localization-pipeline.json')['images']['batch_bundles']
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
