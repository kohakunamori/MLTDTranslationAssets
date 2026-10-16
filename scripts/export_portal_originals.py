"""Publish source-bound complete originals for the portal, without translation.

Only the coverage report's named, unreviewed full-size image bundles are read.
Comic slices follow their six explicit numbered Sprite names; split UI/whiteboard
Sprites use the established left/main + counterclockwise right/continuation layout.
Other Sprites are exported individually. Unknown layouts remain in the report.
Lossless WebP files are content addressed and pixel-checked after encoding.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.writing')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def join_parts(parts, direction):
    if not parts:
        raise ValueError('No image parts')
    if direction == 'vertical':
        if max(im.width for im in parts) - min(im.width for im in parts) > max(2, int(max(im.width for im in parts) * .02)):
            raise ValueError('Comic slice widths disagree')
        size = (max(im.width for im in parts), sum(im.height for im in parts))
    else:
        if len({im.height for im in parts}) != 1:
            raise ValueError('Split continuation height disagrees')
        size = (sum(im.width for im in parts), parts[0].height)
    if size[0] * size[1] > 16_777_216:
        raise ValueError('Complete image exceeds pixel bound')
    canvas = Image.new('RGBA', size)
    offset = 0
    for part in parts:
        canvas.paste(part, ((size[0] - part.width) // 2, offset) if direction == 'vertical' else (offset, 0))
        offset += part.height if direction == 'vertical' else part.width
    return canvas


def comic_order(sprites, bundle):
    prefix = 'ex4c_' + re.fullmatch(r'ex4c(\d+)\.unity3d', bundle)[1] + '_'
    by_name = {s.m_Name: s for s in sprites}
    names = [prefix + str(n) for n in range(6)]
    if len(by_name) != len(sprites) or set(by_name) != set(names):
        raise ValueError('Comic does not have the six explicit numbered Sprites')
    return [by_name[name] for name in names]


def rect(sprite):
    r = sprite.m_Rect
    return [r.x, r.y, r.width, r.height]


def sprite_evidence(sprite):
    return {'sprite_id': str(sprite.path_id), 'name': sprite.m_Name,
            'texture_id': str(sprite.m_RD.texture.path_id), 'rect': rect(sprite)}


def split_pair(sprites):
    if len(sprites) != 2:
        return None
    # Semantic order is supported by both names and exact source rectangles.
    for suffixes in (('_00', '_01'), ('_1', '_2'), ('_0', '_1')):
        first = next((s for s in sprites if s.m_Name.endswith(suffixes[0])), None)
        second = next((s for s in sprites if s.m_Name.endswith(suffixes[1])), None)
        if not first or not second or first.m_Name[:-len(suffixes[0])] != second.m_Name[:-len(suffixes[1])]:
            continue
        a, b = rect(first), rect(second)
        if any(round(v) != v for v in a + b):
            raise ValueError('Fractional split Sprite rect')
        # In Unity's bottom-origin coordinates the main Sprite is above the continuation.
        if a[1] < b[1] + b[3] or a[3] != b[2]:
            raise ValueError('Unexpected split Sprite geometry')
        for s in (first, second):
            settings = s.m_RD.settingsRaw
            if settings.packed or int(settings.packingRotation) != 0:
                raise ValueError('Packed split Sprite needs explicit layout review')
        return first, second
    return None


def complete_images(env, bundle, family):
    sprites = [o.read() for o in env.objects if o.type.name == 'Sprite']
    if not sprites:
        raise ValueError('No Sprite layout; raw Texture2D atlases are not published')
    if family in ('ex4c', 'comic'):
        ordered = comic_order(sprites, bundle)
        if len({s.image.width for s in ordered}) != 1 and any(abs(s.m_Pivot.X - .5) > .001 for s in ordered):
            raise ValueError('Unequal comic widths without centered Sprite pivots')
        yield 'comic', join_parts([s.image.convert('RGBA') for s in ordered], 'vertical'), {
            'method': 'six-numbered-sprites-vertical', 'parts': [sprite_evidence(s) for s in ordered]}
        return
    by_texture = {}
    for sprite in sprites:
        if sprite.m_RD.texture.file_id != 0:
            raise ValueError('External Sprite texture requires source dependency')
        by_texture.setdefault(sprite.m_RD.texture.path_id, []).append(sprite)
    for texture_id, members in sorted(by_texture.items()):
        pair = split_pair(members)
        if pair:
            main, continuation = pair
            images = [main.image.convert('RGBA'), continuation.image.convert('RGBA').transpose(Image.Transpose.ROTATE_90)]
            yield str(texture_id), join_parts(images, 'horizontal'), {
                'method': 'split-sprites-right-ccw90', 'parts': [sprite_evidence(s) for s in pair]}
        else:
            if family not in ('talk_stamps', 'stamp') and len(members) > 1:
                raise ValueError('Multiple Sprite atlas needs explicit complete-image grouping')
            for sprite in sorted(members, key=lambda s: s.path_id):
                image = sprite.image.convert('RGBA')
                if image.width * image.height > 16_777_216:
                    raise ValueError('Sprite exceeds pixel bound')
                yield 'sprite-' + str(sprite.path_id), image, {
                    'method': 'unity-sprite', 'parts': [sprite_evidence(sprite)]}


def publish_image(image, root):
    # Alpha-zero RGB is retained as well; exact=True makes WebP truly lossless.
    import io
    stream = io.BytesIO()
    image.save(stream, format='WEBP', lossless=True, exact=True, method=3)
    data = stream.getvalue()
    with Image.open(io.BytesIO(data)) as decoded:
        if decoded.convert('RGBA').tobytes() != image.tobytes():
            raise ValueError('Lossless original pixel verification failed')
    digest = hashlib.sha256(data).hexdigest()
    relative = 'images/original/complete/' + digest + '.webp'
    path = root / relative
    if path.exists():
        if sha(path) != digest:
            raise ValueError('Published original hash changed')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=digest + '.', suffix='.writing', dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            # Multiple bundles may have identical pixels. Create the immutable
            # name atomically; never replace a file another worker is reading.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if sha(path) != digest:
                    raise ValueError('Published original hash changed')
        finally:
            temporary.unlink(missing_ok=True)
    return {'relative_path': relative, 'sha256': digest}


def export_bundle(job):
    root, work, version, logical, record, family = job
    from build_generated_release import download
    import UnityPy
    key = hashlib.sha256((logical + record['remote']).encode()).hexdigest()
    cached = work / 'records' / (key + '.json')
    if cached.exists():
        saved = json.loads(cached.read_text(encoding='utf-8'))
        if saved.get('algorithm') == 1 and all((root / t['original']['relative_path']).is_file()
                and sha(root / t['original']['relative_path']) == t['original']['sha256'] for t in saved['images']):
            return saved
    archive = work / 'bundles' / record['remote']
    download(version['asset_root'] + '/' + record['remote'], archive, record['declared_size'])
    provenance = {'asset_version': version['asset_version'], 'archive_sha256': sha(archive), **record}
    env = UnityPy.load(str(archive))
    # Resolve all groups before publishing anything: partial layouts stay explicitly unresolved.
    outputs = list(complete_images(env, logical, family))
    images = []
    for identity, image, layout in outputs:
        images.append({'task_id': logical + ':complete-' + identity, 'bundle': logical,
            'kind': 'source_complete_image', 'image_layout': 'complete', 'status': 'original_only',
            'review_status': 'needs_text_review', 'dimensions': {'width': image.width, 'height': image.height},
            'original': publish_image(image, root), 'source': provenance, 'layout': layout})
    result = {'algorithm': 1, 'bundle': logical, 'family': family, 'source': provenance, 'images': images}
    write(cached, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error('workers must be 1..8')
    from build_generated_release import download, load_official_index, load_version_manifest
    root, work = args.root.resolve(), args.work.resolve()
    version = load_version_manifest(root / 'manifests/asset-version.json')
    index_path = work / version['index_name']
    download(version['asset_root'] + '/' + version['index_name'], index_path, None)
    index = load_official_index(index_path)
    coverage = json.loads((root / 'manifests/localization-coverage.json').read_text(encoding='utf-8'))
    families = {}
    for family, info in coverage['images']['families'].items():
        for logical in info['missing_reviewed_bundles']:
            if not logical.endswith('_thumb.unity3d'):
                families[logical] = family
    chosen = sorted(families, key=lambda n: (0 if families[n] in ('ex4c', 'exwb', 'hitokoma') else 1, n))
    if args.limit:
        chosen = chosen[:args.limit]
    jobs = [(root, work, version, n, index[n], families[n]) for n in chosen]
    results, unresolved = [], []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(export_bundle, job): job[3] for job in jobs}
        for done, future in enumerate(as_completed(pending), 1):
            logical = pending[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                unresolved.append({'bundle': logical, 'reason': type(exc).__name__ + ': ' + str(exc)[:500]})
                print('UNRESOLVED', logical, unresolved[-1]['reason'], flush=True)
            if done % 25 == 0 or done == len(jobs):
                print('PROGRESS', done, '/', len(jobs), 'bundles;', sum(len(r['images']) for r in results),
                      'complete originals;', len(unresolved), 'unresolved', flush=True)
    images = sorted((t for r in results for t in r['images']), key=lambda t: t['task_id'])
    report = {'schema_version': 1, 'kind': 'source-complete-originals', 'asset_version': version['asset_version'],
        'algorithm': 1, 'source_index_sha256': sha(index_path), 'selected_bundles': len(chosen),
        'exported_bundles': len(results), 'unresolved': sorted(unresolved, key=lambda r: r['bundle']),
        'images': images}
    # A limited run is diagnostic only and must not replace the complete published catalogue.
    destination = work / 'sample-manifest.json' if args.limit else root / 'manifests/image-originals.manifest.json'
    write(destination, report)
    print('CATALOG', destination, 'images', len(images), 'unresolved', len(unresolved), flush=True)
    if unresolved:
        print('Unresolved sources remain inventory placeholders; see manifest reasons.', flush=True)


if __name__ == '__main__':
    main()
