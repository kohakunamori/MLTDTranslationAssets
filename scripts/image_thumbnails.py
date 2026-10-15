"""Source-bound thumbnail derivation. Never invokes a translator or reviewer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from image_resources import thumbnail_parent


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def approved_parts(root, logical, archive_sha):
    """Return current approved source/translation locators, never raw candidates."""
    state = read(root / 'manifests/image-incremental.json', {'bundles': {}})['bundles'].get(logical, {})
    if state.get('status') == 'translated' and state.get('archive_sha256') == archive_sha:
        return {t['id']: {'source_sha256': t['source_sha256'], 'localized': t['localized'],
                         'sha256': t['sha256']} for t in state['textures']}
    # The legacy cohort is approved at texture level. Require the published full
    # source archive to match too before using its older original PNG locators.
    published = any(e.get('logical_key') == logical and e.get('source_sha256') == archive_sha
                    and e.get('resource_kind') == 'texture'
                    for path in (root / 'generated').glob('*/manifest.json')
                    for e in read(path, {}).get('entries', []))
    if not published:
        return {}
    return {int(t['texture_path_id']): {'source_sha256': t['original']['sha256'],
              'localized': t['localized']['relative_path'], 'sha256': t['localized']['sha256']}
            for t in read(root / 'manifests/images.manifest.json', {}).get('images', [])
            if t.get('bundle') == logical and t.get('review_status') == 'user_approved_for_isolated_install_staging'}


def match_thumbnail(thumbnail, candidates):
    """Require a unique full-texture match despite thumbnail compression.

    No guessed crop or guessed translated region: unrecognised layouts are held.
    Multiple full textures with similar scores are ambiguous and also held.
    """
    target = np.asarray(thumbnail.convert('RGB'), dtype=float)
    scores = []
    for key, source in candidates.items():
        if abs((source.width / source.height) / (thumbnail.width / thumbnail.height) - 1) > .03:
            continue
        pixels = np.asarray(source.convert('RGB').resize(thumbnail.size, Image.Resampling.LANCZOS), dtype=float)
        error = float(np.abs(target - pixels).mean())
        # A flat colour alone cannot prove that two resources correspond.
        correlation = float(np.corrcoef(target.ravel(), pixels.ravel())[0, 1]) if min(target.std(), pixels.std()) > 8 else 0
        if np.isfinite(correlation):
            scores.append((error, key, correlation))
    scores.sort()
    if not scores or scores[0][0] > 16 or scores[0][2] < .9:
        raise ValueError('No reliable full-size texture match')
    if len(scores) > 1 and scores[1][0] - scores[0][0] < 5:
        raise ValueError('Ambiguous full-size texture match')
    error, key, correlation = scores[0]
    return key, {'mean_absolute_rgb_error': error, 'rgb_correlation': correlation,
                 'runner_up_error': scores[1][0] if len(scores) > 1 else None,
                 'algorithm': 'full-texture-lanczos-v1'}


def prepare_derivation(root, work, version, index, logical, archive):
    import UnityPy
    from build_generated_release import download
    parent = thumbnail_parent(logical)
    record = index[logical]
    bundle = {'logical': logical, 'identity': [record['catalog_hash'], record['remote'], record['declared_size']],
              'archive': archive.relative_to(work).as_posix(), 'archive_sha256': sha(archive),
              'tasks': [], 'derivation': {'parent': parent, 'textures': [], 'status': 'deferred'}}
    if parent not in index:
        bundle['derivation']['reason'] = 'parent_not_in_current_catalogue'
        return bundle
    parent_row = index[parent]
    parent_archive = work / 'bundles' / parent_row['remote']
    download(f"{version['asset_root']}/{parent_row['remote']}", parent_archive, parent_row['declared_size'])
    parent_sha = sha(parent_archive)
    approved = approved_parts(root, parent, parent_sha)
    folder = work / 'derivations' / sha(archive)
    folder.mkdir(parents=True, exist_ok=True)
    candidates, originals = {}, {}
    for obj in UnityPy.load(str(parent_archive)).objects:
        if obj.type.name == 'Texture2D':
            image = obj.read().image.convert('RGBA')
            path = folder / f'parent-{obj.path_id}.png'
            image.save(path)
            # Include unapproved full textures in matching so a preview cannot
            # accidentally match only the approved subset of a larger bundle.
            candidates[int(obj.path_id)], originals[int(obj.path_id)] = image, path
    matches = []
    try:
        for obj in UnityPy.load(str(archive)).objects:
            if obj.type.name != 'Texture2D':
                continue
            image = obj.read().image.convert('RGBA')
            key, metrics = match_thumbnail(image, candidates)
            approval = approved.get(key)
            if not approval or sha(originals[key]) != approval['source_sha256']:
                raise ValueError('Matched full texture has no current approved translation')
            localized = root / approval['localized']
            if sha(localized) != approval['sha256']:
                raise ValueError('Approved full texture changed')
            original = folder / f'thumb-{obj.path_id}.png'
            image.save(original)
            matches.append({'id': int(obj.path_id), 'size': list(image.size),
                'original': original.relative_to(work).as_posix(), 'source_sha256': sha(original),
                'parent_texture_id': key, 'parent_original': originals[key].relative_to(work).as_posix(),
                **approval, 'thumbnail_source_sha256': sha(original), 'match': metrics})
        if not matches:
            raise ValueError('No thumbnail textures')
    except (ValueError, FileNotFoundError) as error:
        bundle['derivation']['reason'] = str(error)
        return bundle
    bundle['derivation'].update(status='ready', parent_archive=parent_archive.relative_to(work).as_posix(),
        parent_archive_sha256=parent_sha, textures=matches,
        parent_candidates=[{'id': key, 'original': path.relative_to(work).as_posix(), 'sha256': sha(path)}
                           for key, path in originals.items()])
    return bundle


def package_derivation(root, work, bundle, version):
    data = bundle['derivation']
    if data['status'] != 'ready':
        print('THUMBNAIL_DEFERRED', bundle['logical'], data.get('reason'), flush=True)
        return {'identity': bundle['identity'], 'archive_sha256': bundle['archive_sha256'],
            'asset_version': version, 'status': 'manual_review', 'textures': [], 'text_maps': {},
            'review_reason': data.get('reason'), 'derivation': {'kind': 'approved-parent-thumbnail',
                'parent': data['parent'], 'dependency': data.get('dependency'), 'model_called': False}}, []
    if (sha(work / bundle['archive']) != bundle['archive_sha256']
            or sha(work / data['parent_archive']) != data['parent_archive_sha256']):
        raise ValueError('Thumbnail/parent source bundle changed')
    approved = approved_parts(root, data['parent'], data['parent_archive_sha256'])
    candidates = {}
    for t in data['parent_candidates']:
        if sha(work / t['original']) != t['sha256']:
            raise ValueError('Thumbnail parent source pixels changed')
        candidates[t['id']] = Image.open(work / t['original']).convert('RGBA')
    rows, textures = [], []
    for t in data['textures']:
        original = work / t['original']
        parent = approved.get(t['parent_texture_id'])
        if (not parent or parent != {k: t[k] for k in ('source_sha256', 'localized', 'sha256')}
                or sha(root / t['localized']) != t['sha256']
                or sha(work / t['parent_original']) != t['source_sha256']
                or sha(original) != t['thumbnail_source_sha256']):
            raise ValueError('Thumbnail derivation approval binding changed')
        source = Image.open(original).convert('RGBA')
        key, _ = match_thumbnail(source, candidates)
        if key != t['parent_texture_id']:
            raise ValueError('Thumbnail parent texture changed')
        image = Image.open(root / t['localized']).convert('RGBA')
        if image.size != candidates[key].size:
            raise ValueError('Approved parent geometry changed')
        image = image.convert('RGB').resize(source.size, Image.Resampling.LANCZOS).convert('RGBA')
        image.putalpha(source.getchannel('A'))
        target = work / 'derived' / f"{bundle['archive_sha256']}-{t['id']}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target)
        digest = sha(target)
        destination = root / 'images/localized' / (digest + '.png')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(target.read_bytes())
        textures.append({'id': t['id'], 'localized': destination.relative_to(root).as_posix(), 'sha256': digest,
            'source_sha256': t['thumbnail_source_sha256'], 'derived_from': {'bundle': data['parent'],
            'texture_id': key, 'localized_sha256': t['sha256'], 'match': t['match']}})
        rows.append({'archive_sha256': bundle['archive_sha256'], 'bundle': bundle['logical'],
            'remote': bundle['identity'][1], 'source_bundle': str((work / bundle['archive']).resolve()),
            'source_id': bundle['logical'].removesuffix('.unity3d') + ':' + str(t['id']),
            'texture_path_id': t['id'], 'original_png': str(original.resolve()),
            'original_png_sha256': t['thumbnail_source_sha256'], 'original_size': t['size'],
            'restored_png': str(target.resolve()), 'restored_png_sha256': digest,
            'review_status': 'auto_approved_no_human_signoff'})
    record = {'identity': bundle['identity'], 'archive_sha256': bundle['archive_sha256'],
        'asset_version': version, 'status': 'translated', 'textures': textures, 'text_maps': {},
        'derivation': {'kind': 'approved-parent-thumbnail', 'parent': data['parent'],
                       'dependency': data.get('dependency'),
                       'parent_archive_sha256': data['parent_archive_sha256'], 'model_called': False}}
    return record, rows
