"""Copy immutable release objects to/from the private R2 bucket.

Only manifests, checksums and the verified inventory stay in Git. Publication
uploads and verifies every referenced digest before publishing that metadata.
The existing main-branch compare-and-swap then selects the release; gateway
deployment selects the corresponding table. Stale uploads are harmless.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext as _null_stream
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.parse

import requests
from urllib3.exceptions import HTTPError as StreamError

ROOT = Path(__file__).resolve().parents[1]
HEX = re.compile(r'[0-9a-f]{64}')


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def retained_versions(root):
    """Release payload retention is independent of the source library/history."""
    config = root / 'configs/r2-storage.json'
    limit = json.loads(config.read_text()).get('retain_releases', 2) if config.exists() else 2
    if type(limit) is not int or not 1 <= limit <= 2:
        raise ValueError('R2 must retain one or two releases')
    return sorted((p.parent.name for p in (root / 'generated').glob('*/manifest.json')
                   if p.parent.name.isdigit()), key=int, reverse=True)[:limit]


def references(root, versions=None):
    """Retain the union of explicitly retained, successful releases."""
    result = set()
    for version in retained_versions(root) if versions is None else versions:
        path = root / 'generated' / version / 'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('build_status') != 'success':
            raise ValueError(f'Unsuccessful retained release: {path}')
        for entry in manifest['entries']:
            digest = entry['artifact_sha256']
            if not HEX.fullmatch(digest):
                raise ValueError(f'Invalid digest: {digest}')
            result.add(digest)
    if not result:
        raise ValueError('No retained release objects')
    return sorted(result)


class Transport:
    def __init__(self, root=ROOT):
        config = json.loads((root / 'configs/r2-storage.json').read_text(encoding='utf-8'))
        self.url = config['publisher_url'].rstrip('/')
        self.token = os.environ.get('R2_PUBLISH_TOKEN', '')
        if not self.token:
            raise ValueError('R2_PUBLISH_TOKEN is required (the dedicated publisher credential)')
        self.local = threading.local()

    def session(self):
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session()
        return self.local.session

    def request(self, key, *, path=None, put=False):
        """Stream files, bound retries, and never put credentials in URLs/logs."""
        for attempt in range(5):
            try:
                with (path.open('rb') if put else _null_stream()) as stream:
                    headers = {'Authorization': f'Bearer {self.token}', 'User-Agent': 'mltd-r2-publisher/1'}
                    if put:
                        headers['Content-Length'] = str(path.stat().st_size)
                        headers['Content-Type'] = 'application/octet-stream'
                    with self.session().request('PUT' if put else 'GET', self.url + '/' + key,
                            data=stream if put else None, headers=headers, stream=True,
                            timeout=(30, 180), allow_redirects=False) as response:
                        if response.status_code >= 400:
                            if response.status_code not in (408, 429, 500, 502, 503, 504) or attempt == 4:
                                raise ValueError(f'R2 {"PUT" if put else "GET"} {key}: HTTP {response.status_code}')
                            raise OSError('transient R2 response')
                        if response.status_code not in (200, 201):
                            raise ValueError(f'Unexpected R2 HTTP {response.status_code}')
                        if path is not None and not put:
                            with path.open('wb') as output:
                                for chunk in response.iter_content(1024 * 1024):
                                    output.write(chunk)
                            return None
                        return response.json()
            except (OSError, TimeoutError, requests.RequestException):
                if attempt == 4:
                    raise RuntimeError(f'R2 transport failed: {key}') from None
            time.sleep(min(2 ** attempt, 8))

    def inventory(self, prefix):
        result, cursor = {}, None
        while True:
            query = {'prefix': prefix}
            if cursor:
                query['cursor'] = cursor
            page = self.request('inventory?' + urllib.parse.urlencode(query))
            for item in page['objects']:
                if item.get('sha256') != item['key'].rsplit('/', 1)[-1]:
                    raise ValueError('R2 inventory contains an unverified object')
                result[item['key']] = item['size']
            cursor = page['cursor']
            if not cursor:
                return result

    def delete(self, keys):
        """Called only while CI holds the shared r2-mutation lock."""
        for offset in range(0, len(keys), 100):
            batch = keys[offset:offset + 100]
            response = self.session().post(self.url + '/delete-batch', json=batch,
                headers={'Authorization': f'Bearer {self.token}'},
                timeout=(30, 180), allow_redirects=False)
            if response.status_code != 200 or response.json().get('deleted') != batch:
                raise ValueError('R2 deletion was not acknowledged; safe to retry')

    def download_batch(self, items):
        """Amortize HTTPS round trips without duplicating objects in R2."""
        keys = ['objects/sha256/' + digest for digest, _, _ in items]
        expected = [{'key': key, 'size': item[1]} for key, item in zip(keys, items)]
        for attempt in range(5):
            try:
                with self.session().post(self.url + '/batch', json=keys, stream=True, timeout=(30, 180),
                        allow_redirects=False, headers={'Authorization': f'Bearer {self.token}',
                                                       'Accept-Encoding': 'identity',
                                                       'User-Agent': 'mltd-r2-publisher/1'}) as response:
                    if response.status_code != 200:
                        if response.status_code in (408, 429, 500, 502, 503, 504):
                            raise OSError('transient batch response')
                        raise ValueError(f'R2 batch HTTP {response.status_code}')
                    length = int.from_bytes(response.raw.read(4), 'big')
                    if not 0 < length <= 32768:
                        raise ValueError('Invalid R2 batch header')
                    metadata = json.loads(response.raw.read(length))
                    if metadata != expected:
                        raise ValueError('R2 batch differs from verified inventory')
                    for digest, size, path in items:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_name(path.name + '.tmp')
                        try:
                            remaining, checksum = size, hashlib.sha256()
                            with temporary.open('wb') as output:
                                while remaining:
                                    chunk = response.raw.read(min(remaining, 1024 * 1024))
                                    if not chunk:
                                        raise OSError('truncated R2 batch')
                                    output.write(chunk)
                                    checksum.update(chunk)
                                    remaining -= len(chunk)
                            if checksum.hexdigest() != digest:
                                raise ValueError('R2 batch object checksum mismatch')
                            temporary.replace(path)
                        finally:
                            temporary.unlink(missing_ok=True)
                    if response.raw.read(1):
                        raise ValueError('Unexpected trailing R2 batch bytes')
                    return
            except (OSError, requests.RequestException, StreamError):
                if attempt == 4:
                    raise RuntimeError('R2 batch transport failed') from None
            time.sleep(min(2 ** attempt, 8))


def upload(transport, key, path, existing):
    digest = key.rsplit('/', 1)[-1]
    if sha256(path) != digest:
        raise ValueError(f'Local object has wrong SHA-256: {path}')
    size = path.stat().st_size
    if key in existing:
        if existing[key] != size:
            raise ValueError(f'R2 size mismatch: {key}')
    else:
        result = transport.request(key, path=path, put=True)
        if result.get('sha256') != digest or result.get('size') != size:
            raise ValueError(f'R2 did not acknowledge verified bytes: {key}')
    return digest, size


def fetch(transport, key, path):
    digest = key.rsplit('/', 1)[-1]
    if not HEX.fullmatch(digest):
        raise ValueError('Invalid content address')
    if path.is_file() and sha256(path) == digest:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    try:
        transport.request(key, path=temporary)
        if sha256(temporary) != digest:
            raise ValueError(f'R2 download has wrong checksum: {key}')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def unpack_cache(archive, destination):
    # Python 3.11 compatible, no links, absolute paths, or archive traversal.
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or '..' in name.parts
                    or '\\' in member.name or ':' in member.name
                    or not re.fullmatch(r'text/[0-9a-f]{64}/[0-9a-f]{64}\.json\.gz', member.name)):
                raise ValueError(f'Unsafe build cache member: {member.name}')
            output = destination.joinpath(*name.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, output.open('wb') as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)


def restore(root, transport, workers=12, cache=True, samples=None, overlay_version=None):
    state = json.loads((root / 'generated/r2-storage.json').read_text(encoding='utf-8'))
    digests = references(root, state.get('release_versions'))
    overlay = {}
    if overlay_version is not None:
        if not str(overlay_version).isdigit():
            raise ValueError('Overlay version must be numeric')
        prefix = f'image-overlay/{overlay_version}/jp-android/'
        overlay = {p: d for p, d in state.get('legacy_overlay', {}).items() if p.startswith(prefix)}
        if not overlay or any('/' in p[len(prefix):] or '..' in p[len(prefix):] or '\\' in p for p in overlay):
            raise ValueError('Missing or invalid R2 overlay mapping')
        digests = sorted(set(digests) | set(overlay.values()))
    if samples is not None:
        # Gateway acceptance needs only sampled assets and the official index.
        import importlib.util
        spec = importlib.util.spec_from_file_location('r2_gateway_check', root / 'asset-server/cloudflare/check_gateway.py')
        import sys
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        version = json.loads((root / 'manifests/asset-version.json').read_text())['asset_version']
        release = module.load_release(root, str(version))
        chosen = module.pick_translated(release['manifest'], release['pool'], samples, 12 * 1024**2, 20261011)
        chosen += [e for e in release['manifest']['entries'] if e['logical_key'] == '__official_asset_index__']
        digests = sorted({e['artifact_sha256'] for e in chosen})
    if any(d not in state['objects'] for d in digests):
        raise ValueError('R2 inventory does not cover the retained release')
    if hasattr(transport, 'download_batch'):
        batches, batch, byte_count, large = [], [], 0, []
        for digest in digests:
            path = root / 'generated/objects/sha256' / digest
            if path.is_file() and sha256(path) == digest:
                continue
            size = state['objects'][digest]
            if size > 4 * 1024**2:
                large.append((digest, path))
                continue
            if batch and (len(batch) == 32 or byte_count + size > 4 * 1024**2):
                batches.append(batch)
                batch, byte_count = [], 0
            batch.append((digest, size, path))
            byte_count += size
        if batch:
            batches.append(batch)
        with ThreadPoolExecutor(max_workers=min(workers, 4)) as pool:
            list(pool.map(lambda item: fetch(transport, 'objects/sha256/' + item[0], item[1]), large))
        print(f'R2 restore: {len(large)} large objects verified; {len(batches)} bounded batches remain', flush=True)
        with ThreadPoolExecutor(max_workers=min(workers, 4)) as pool:
            for position, _ in enumerate(pool.map(transport.download_batch, batches), 1):
                if position % 32 == 0 or position == len(batches):
                    print(f'R2 restore: batch {position}/{len(batches)} verified', flush=True)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda d: fetch(transport, 'objects/sha256/' + d, root / 'generated/objects/sha256' / d), digests))
    if cache and state.get('build_cache'):
        with tempfile.TemporaryDirectory(prefix='mltd-r2-cache-') as directory:
            archive = Path(directory) / 'cache.tar.gz'
            fetch(transport, 'build-cache/sha256/' + state['build_cache']['sha256'], archive)
            unpack_cache(archive, root / 'generated/build-inputs')
    for name, digest in overlay.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp')
        shutil.copyfile(root / 'generated/objects/sha256' / digest, temporary)
        temporary.replace(target)
    print(f'Restored {len(digests)} verified R2 objects')


def publish_overlay(root, transport, version):
    """Upload the audited generation stage before its small metadata commit."""
    if not str(version).isdigit():
        raise ValueError('Overlay version must be numeric')
    folder = root / 'image-overlay' / str(version)
    manifest = json.loads((folder / 'overlay-manifest.json').read_text(encoding='utf-8'))
    if str(manifest['asset_version']) != str(version) or sha256(folder / 'repack-independent-audit.json') != manifest['audit_sha256']:
        raise ValueError('Overlay is not bound to its independent audit')
    rows = [(r['remote'], r['translated_sha256'], r['translated_bytes']) for r in manifest['records']]
    index_name = manifest['client_manifest_name']
    if not rows or any(Path(name).name != name or '\\' in name or '..' in name for name in [index_name, *(r[0] for r in rows)]):
        raise ValueError('Invalid overlay filenames')
    index_path = folder / 'jp-android' / index_name
    rows.append((index_name, sha256(index_path), index_path.stat().st_size))
    state_path = root / 'generated/r2-storage.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    existing = transport.inventory('objects/sha256/')
    for name, digest, size in rows:
        if not HEX.fullmatch(digest):
            raise ValueError('Invalid overlay digest')
        path = folder / 'jp-android' / name
        if path.stat().st_size != size:
            raise ValueError('Overlay byte size mismatch')
        upload(transport, 'objects/sha256/' + digest, path, existing)
        state['objects'][digest] = size
        state.setdefault('legacy_overlay', {})[path.relative_to(root).as_posix()] = digest
    state['objects'] = dict(sorted(state['objects'].items()))
    state['legacy_overlay'] = dict(sorted(state['legacy_overlay'].items()))
    temporary = state_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(state_path)
    print(f'R2 verified {len(rows)} audited overlay objects for {version}')


def publish(root, transport, workers=12):
    existing = transport.inventory('objects/sha256/')
    digests = references(root)
    sources = {d: root / 'generated/objects/sha256' / d for d in digests}
    # Retain the retired audited overlay bytes in R2 too, before untracking them.
    old_state = root / 'generated/r2-storage.json'
    versions = retained_versions(root)
    legacy = json.loads(old_state.read_text()).get('legacy_overlay', {}) if old_state.exists() else {}
    legacy = {p: d for p, d in legacy.items() if p.split('/')[1] in versions}
    for pattern in ('*/*/*.unity3d', '*/*/*.data'):
        for path in (root / 'image-overlay').glob(pattern):
            if path.relative_to(root / 'image-overlay').parts[0] not in versions:
                continue
            digest = sha256(path)
            legacy[path.relative_to(root).as_posix()] = digest
            sources[digest] = path
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pairs = list(pool.map(lambda item: upload(transport, 'objects/sha256/' + item[0], item[1], existing), sources.items()))
    sizes = dict(pairs)
    for digest in legacy.values():
        if digest not in sizes:
            key = 'objects/sha256/' + digest
            if key not in existing:
                raise ValueError('Missing retained overlay object in R2')
            sizes[digest] = existing[key]
    cache_record = None
    cache_root = root / 'generated/build-inputs'
    files = sorted(cache_root.glob('text/*/*.json.gz'))
    if files:
        with tempfile.TemporaryDirectory(prefix='mltd-r2-cache-') as directory:
            archive = Path(directory) / 'cache.tar.gz'
            # Stable tar and gzip headers preserve the address on an unchanged cache.
            import gzip
            with archive.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0, filename='') as compressed:
                with tarfile.open(fileobj=compressed, mode='w') as tar:
                    for path in files:
                        info = tar.gettarinfo(str(path), path.relative_to(cache_root).as_posix())
                        info.mtime = info.uid = info.gid = 0
                        info.uname = info.gname = ''
                        info.mode = 0o644
                        with path.open('rb') as stream:
                            tar.addfile(info, stream)
            digest = sha256(archive)
            _, size = upload(transport, 'build-cache/sha256/' + digest, archive, transport.inventory('build-cache/sha256/'))
            cache_record = {'sha256': digest, 'size': size, 'files': len(files)}
    state = {'schema_version': 1, 'bucket': 'mltd-localized-assets', 'release_versions': versions,
             'objects': dict(sorted(sizes.items())),
             'build_cache': cache_record, 'legacy_overlay': dict(sorted(legacy.items()))}
    # Write the publication proof only after every upload has succeeded.
    target = root / 'generated/r2-storage.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(target)
    print(f'R2 verified: {len(sizes)} unique objects, {sum(sizes.values()) / 1024**2:.1f} MiB; '
          f'{len(files)} parsed build inputs')


def prune(root, transport, *, apply=False):
    """Sweep only after live acceptance, with no concurrent CI publisher.

    Git source/history and local files are never deleted. A failed deployment
    never reaches this command. Fail closed on stale metadata or missing pins.
    """
    state = json.loads((root / 'generated/r2-storage.json').read_text())
    versions = retained_versions(root)
    if state.get('release_versions', versions) != versions:
        raise ValueError('Publication inventory is stale; publish before pruning')
    keep = {'objects/sha256/' + d for d in references(root, versions)}
    for path, digest in state.get('legacy_overlay', {}).items():
        if path.split('/')[1] in versions:
            keep.add('objects/sha256/' + digest)
    # A new image overlay can be staged before the unified release is built.
    # It is committed inventory and must survive a gateway-only repair run.
    keep.update('objects/sha256/' + d for d in state['objects'])
    if state.get('build_cache'):
        keep.add('build-cache/sha256/' + state['build_cache']['sha256'])
    existing = {**transport.inventory('objects/sha256/'),
                **transport.inventory('build-cache/sha256/')}
    missing = keep - existing.keys()
    if missing:
        raise ValueError(f'Refusing GC: {len(missing)} live objects are missing')
    remove = sorted(existing.keys() - keep)
    report = {'retained_versions': versions, 'delete_objects': len(remove),
              'delete_bytes': sum(existing[k] for k in remove), 'applied': apply}
    if apply and remove:
        transport.delete(remove)
        remaining = {**transport.inventory('objects/sha256/'),
                     **transport.inventory('build-cache/sha256/')}
        if not keep <= remaining.keys() or any(k in remaining for k in remove):
            raise ValueError('R2 retention verification failed')
    print(json.dumps(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['publish', 'restore', 'publish-overlay', 'prune'])
    parser.add_argument('--apply', action='store_true', help='Actually delete unreferenced R2 payloads')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--workers', type=int, default=12)
    parser.add_argument('--no-cache', action='store_true')
    parser.add_argument('--samples', type=int)
    parser.add_argument('--overlay-version')
    args = parser.parse_args()
    transport = Transport(args.root)
    if args.action == 'publish':
        publish(args.root, transport, args.workers)
    elif args.action == 'publish-overlay':
        if args.overlay_version is None:
            parser.error('publish-overlay requires --overlay-version')
        publish_overlay(args.root, transport, args.overlay_version)
    elif args.action == 'prune':
        prune(args.root, transport, apply=args.apply)
    else:
        restore(args.root, transport, args.workers, not args.no_cache, args.samples, args.overlay_version)


if __name__ == '__main__':
    main()
