"""R2 publication failure/recovery invariants, without network access."""
import hashlib
from collections import Counter
import threading
import io
import json
import random
from pathlib import Path
import tarfile
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import r2_release_storage as tool


class Remote:
    def __init__(self):
        self.objects = {}
        self.fail = False

    def inventory(self, prefix):
        return {k: len(v) for k, v in self.objects.items() if k.startswith(prefix)}

    def delete(self, keys):
        for key in keys:
            self.objects.pop(key, None)

    def request(self, key, *, path=None, put=False):
        if self.fail:
            raise OSError('remote unavailable')
        if put:
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            assert digest == key.rsplit('/', 1)[-1]
            self.objects.setdefault(key, body)
            return {'sha256': digest, 'size': len(body)}
        path.write_bytes(self.objects[key])


def fixture(root):
    digest = hashlib.sha256(b'translated').hexdigest()
    obj = root / 'generated/objects/sha256' / digest
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b'translated')
    for version in ('100', '101'):
        path = root / 'generated' / version / 'manifest.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'build_status': 'success', 'entries': [{'artifact_sha256': digest}]}))
    cache = root / 'generated/build-inputs/text' / ('a' * 64) / ('b' * 64 + '.json.gz')
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'parsed-source-cache')
    return obj, cache


def test_publish_deduplicates_across_versions_and_restores_verified_cache(tmp_path):
    obj, cache = fixture(tmp_path)
    remote = Remote()
    tool.publish(tmp_path, remote, workers=2)
    state = (tmp_path / 'generated/r2-storage.json').read_bytes()
    assert len(remote.objects) == 2  # one shared artifact, one parsed cache archive
    tool.publish(tmp_path, remote)
    assert (tmp_path / 'generated/r2-storage.json').read_bytes() == state
    obj.write_bytes(b'corrupted local copy')
    cache.unlink()
    tool.restore(tmp_path, remote)
    assert obj.read_bytes() == b'translated'
    assert cache.read_bytes() == b'parsed-source-cache'


def test_failed_upload_never_changes_publication_metadata(tmp_path):
    obj, _ = fixture(tmp_path)
    remote = Remote()
    tool.publish(tmp_path, remote)
    marker = tmp_path / 'generated/r2-storage.json'
    previous = marker.read_bytes()
    remote.fail = True
    remote.objects.clear()
    with pytest.raises(OSError):
        tool.publish(tmp_path, remote)
    assert marker.read_bytes() == previous
    obj.write_bytes(b'tampered')
    remote.fail = False
    with pytest.raises(ValueError, match='wrong SHA-256'):
        tool.publish(tmp_path, remote)
    assert marker.read_bytes() == previous


def test_corrupt_download_never_replaces_existing_file(tmp_path):
    obj, _ = fixture(tmp_path)
    digest = obj.name
    remote = Remote()
    remote.objects['objects/sha256/' + digest] = b'wrong'
    obj.write_bytes(b'old')
    with pytest.raises(ValueError, match='wrong checksum'):
        tool.fetch(remote, 'objects/sha256/' + digest, obj)
    assert obj.read_bytes() == b'old'
    assert not obj.with_suffix('.tmp').exists()


@pytest.mark.parametrize('name,kind', [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                                     ('C:/escape', tarfile.REGTYPE), ('text/a/b.json.gz', tarfile.SYMTYPE)])
def test_cache_rejects_traversal_and_links(tmp_path, name, kind):
    archive = tmp_path / 'unsafe.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = '../../escape'
        tar.addfile(info, io.BytesIO(b''))
    with pytest.raises(ValueError, match='Unsafe build cache'):
        tool.unpack_cache(archive, tmp_path / 'out')


def test_legacy_audited_overlay_is_preserved_in_r2_inventory(tmp_path):
    fixture(tmp_path)
    path = tmp_path / 'image-overlay/100/jp-android/legacy.unity3d'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'old-audited-overlay')
    remote = Remote()
    tool.publish(tmp_path, remote)
    state = json.loads((tmp_path / 'generated/r2-storage.json').read_text())
    digest = state['legacy_overlay'][path.relative_to(tmp_path).as_posix()]
    assert remote.objects['objects/sha256/' + digest] == path.read_bytes()
    path.unlink()
    tool.publish(tmp_path, remote)
    assert json.loads((tmp_path / 'generated/r2-storage.json').read_text())['legacy_overlay'] == state['legacy_overlay']


@pytest.mark.parametrize('mode', ['valid', 'tampered', 'wrong-header', 'truncated'])
def test_batched_download_checks_framing_and_each_file_before_install(tmp_path, monkeypatch, mode):
    body = b'translated'
    digest = hashlib.sha256(body).hexdigest()
    key = 'objects/sha256/' + digest
    header = json.dumps([{'key': key if mode != 'wrong-header' else 'wrong', 'size': len(body)}]).encode()
    payload = b'corrupted!' if mode == 'tampered' else body
    if mode == 'truncated':
        payload = payload[:-1]
    frame = len(header).to_bytes(4, 'big') + header + payload
    transport = object.__new__(tool.Transport)
    transport.url, transport.token = 'https://publisher.test', 'test-only'
    transport.session = lambda: SimpleNamespace(post=lambda *args, **kwargs:
        nullcontext(SimpleNamespace(status_code=200, raw=io.BytesIO(frame))))
    target = tmp_path / digest
    monkeypatch.setattr(tool.time, 'sleep', lambda _: None)
    if mode == 'valid':
        transport.download_batch([(digest, len(body), target)])
        assert target.read_bytes() == body
    else:
        with pytest.raises((ValueError, RuntimeError)):
            transport.download_batch([(digest, len(body), target)])
        assert not target.exists()
        assert not target.with_suffix('.tmp').exists()


def test_audited_overlay_transport_restores_new_outputs_before_unified_build(tmp_path):
    fixture(tmp_path)
    remote = Remote()
    tool.publish(tmp_path, remote)
    folder = tmp_path / 'image-overlay/100'
    scope = folder / 'jp-android'
    scope.mkdir(parents=True)
    image = scope / ('a' * 40 + '.unity3d')
    image.write_bytes(b'audited-image')
    index = scope / ('b' * 40 + '.data')
    index.write_bytes(b'patched-index')
    audit = folder / 'repack-independent-audit.json'
    audit.write_text('{}')
    manifest = {'asset_version': '100', 'audit_sha256': tool.sha256(audit), 'client_manifest_name': index.name,
                'records': [{'remote': image.name, 'translated_sha256': tool.sha256(image),
                             'translated_bytes': image.stat().st_size}]}
    (folder / 'overlay-manifest.json').write_text(json.dumps(manifest))
    tool.publish_overlay(tmp_path, remote, '100')
    image.unlink()
    index.unlink()
    tool.restore(tmp_path, remote, cache=False, overlay_version='100')
    assert image.read_bytes() == b'audited-image'
    assert index.read_bytes() == b'patched-index'
    audit.write_text('tampered')
    with pytest.raises(ValueError, match='independent audit'):
        tool.publish_overlay(tmp_path, remote, '100')


def test_third_version_prunes_old_exclusive_bytes_but_keeps_shared_and_sources(tmp_path):
    obj, _ = fixture(tmp_path)
    remote = Remote()
    old = tmp_path / 'generated/objects/sha256' / hashlib.sha256(b'old-only').hexdigest()
    old.write_bytes(b'old-only')
    manifest = tmp_path / 'generated/100/manifest.json'
    doc = json.loads(manifest.read_text())
    doc['entries'].append({'artifact_sha256': old.name})
    manifest.write_text(json.dumps(doc))
    tool.publish(tmp_path, remote)
    assert 'objects/sha256/' + old.name in remote.objects
    newest = tmp_path / 'generated/102/manifest.json'
    newest.parent.mkdir()
    newest.write_text(json.dumps({'build_status': 'success', 'entries': [{'artifact_sha256': obj.name}]}))
    overlay = tmp_path / 'image-overlay/100/jp-android/old.data'
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b'historical-overlay')
    source = tmp_path / 'images/localized/source.png'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'never delete source files')
    tool.publish(tmp_path, remote)
    preview = tool.prune(tmp_path, remote)
    assert preview['retained_versions'] == ['102', '101']
    assert preview['delete_objects'] == 2  # retired blob and its immutable small-object pack
    assert 'objects/sha256/' + old.name in remote.objects
    tool.prune(tmp_path, remote, apply=True)
    assert 'objects/sha256/' + old.name not in remote.objects
    assert 'objects/sha256/' + obj.name in remote.objects
    assert old.exists() and manifest.exists() and overlay.exists() and source.exists()


def test_gc_refuses_missing_live_bytes_and_stale_publication_proof(tmp_path):
    obj, _ = fixture(tmp_path)
    remote = Remote()
    tool.publish(tmp_path, remote)
    saved = remote.objects.pop('objects/sha256/' + obj.name)
    with pytest.raises(ValueError, match='live objects are missing'):
        tool.prune(tmp_path, remote, apply=True)
    remote.objects['objects/sha256/' + obj.name] = saved
    newest = tmp_path / 'generated/102/manifest.json'
    newest.parent.mkdir()
    newest.write_text((tmp_path / 'generated/101/manifest.json').read_text())
    with pytest.raises(ValueError, match='stale'):
        tool.prune(tmp_path, remote, apply=True)


def test_large_cache_chunks_round_trip_and_gc_retains_every_piece(tmp_path, monkeypatch):
    fixture(tmp_path)
    # A small threshold exercises the same partition path as a real >8 MiB archive.
    monkeypatch.setattr(tool, 'CACHE_UPLOAD_CHUNK_BYTES', 256)
    cache = next((tmp_path / 'generated/build-inputs').glob('text/*/*.json.gz'))
    payload = random.Random(20261018).randbytes(8192)
    cache.write_bytes(payload)
    remote = Remote()
    tool.publish(tmp_path, remote)
    state = json.loads((tmp_path / 'generated/r2-storage.json').read_text())
    record = state['build_cache']
    assert len(record['chunks']) > 1
    assert sum(part['size'] for part in record['chunks']) == record['size']
    assert all(part['size'] <= 256 for part in record['chunks'])
    assert 'build-cache/sha256/' + record['sha256'] not in remote.objects
    assert all('build-cache/sha256/' + part['sha256'] in remote.objects
               for part in record['chunks'])
    first_metadata = (tmp_path / 'generated/r2-storage.json').read_bytes()
    tool.publish(tmp_path, remote)
    assert (tmp_path / 'generated/r2-storage.json').read_bytes() == first_metadata
    tool.prune(tmp_path, remote, apply=True)
    cache.unlink()
    tool.restore(tmp_path, remote)
    assert cache.read_bytes() == payload


def test_corrupt_cache_chunk_fails_closed_before_extracting(tmp_path, monkeypatch):
    fixture(tmp_path)
    monkeypatch.setattr(tool, 'CACHE_UPLOAD_CHUNK_BYTES', 256)
    cache = next((tmp_path / 'generated/build-inputs').glob('text/*/*.json.gz'))
    cache.write_bytes(random.Random(20261019).randbytes(8192))
    remote = Remote()
    tool.publish(tmp_path, remote)
    state = json.loads((tmp_path / 'generated/r2-storage.json').read_text())
    chunk = state['build_cache']['chunks'][0]
    remote.objects['build-cache/sha256/' + chunk['sha256']] = b'tampered'
    cache.unlink()
    with pytest.raises(ValueError, match='wrong checksum'):
        tool.restore(tmp_path, remote)
    assert not cache.exists()


def test_invalid_chunk_lengths_fail_before_remote_download(tmp_path, monkeypatch):
    fixture(tmp_path)
    monkeypatch.setattr(tool, 'CACHE_UPLOAD_CHUNK_BYTES', 256)
    cache = next((tmp_path / 'generated/build-inputs').glob('text/*/*.json.gz'))
    cache.write_bytes(random.Random(20261020).randbytes(8192))
    remote = Remote()
    tool.publish(tmp_path, remote)
    statefile = tmp_path / 'generated/r2-storage.json'
    state = json.loads(statefile.read_text())
    state['build_cache']['chunks'][0]['size'] -= 1
    statefile.write_text(json.dumps(state))
    cache.unlink()
    with pytest.raises(ValueError, match='chunk lengths'):
        tool.restore(tmp_path, remote)
    assert not cache.exists()


def test_immutable_edge_packs_share_many_small_objects_and_gc_them(tmp_path, monkeypatch):
    fixture(tmp_path)
    monkeypatch.setattr(tool, 'PACK_TARGET_BYTES', 35)
    monkeypatch.setattr(tool, 'PACK_MAX_MEMBER_BYTES', 16)
    samples = {}
    for i in range(12):
        body = f'piece-{i:02d}'.encode('ascii')
        digest = hashlib.sha256(body).hexdigest()
        path = tmp_path / 'generated/objects/sha256' / digest
        path.write_bytes(body)
        samples[digest] = body
        for version in ('100', '101'):
            manifest = tmp_path / 'generated' / version / 'manifest.json'
            doc = json.loads(manifest.read_text())
            doc['entries'].append({'artifact_sha256': digest})
            manifest.write_text(json.dumps(doc))
    remote = Remote()
    tool.publish(tmp_path, remote)
    metadata = tmp_path / 'generated/r2-storage.json'
    state = json.loads(metadata.read_text())
    assert len(state['packed_objects']) == 13  # includes shared original fixture
    assert len(state['pack_sizes']) > 1
    assert all(n <= 35 for n in state['pack_sizes'].values())
    assert all('objects/sha256/' + d in remote.objects for d in samples)
    for digest, (pack, offset) in state['packed_objects'].items():
        blob = remote.objects['packs/sha256/' + pack]
        assert hashlib.sha256(blob).hexdigest() == pack
        assert blob[offset:offset + state['objects'][digest]] == (
            samples.get(digest) or b'translated')
    before = metadata.read_bytes()
    tool.publish(tmp_path, remote)
    assert metadata.read_bytes() == before
    tool.prune(tmp_path, remote, apply=True)
    assert all('packs/sha256/' + d in remote.objects for d in state['pack_sizes'])
    # A subsequent CI job restores all missing originals using one R2 GET
    # per packed object, instead of one Class B read for each original.
    for digest in samples:
        (tmp_path / 'generated/objects/sha256' / digest).unlink()
    get_keys = []
    original_request = remote.request

    def counted(key, **kwargs):
        if not kwargs.get('put'):
            get_keys.append(key)
        return original_request(key, **kwargs)
    monkeypatch.setattr(remote, 'request', counted)
    tool.restore(tmp_path, remote)
    assert len([k for k in get_keys if k.startswith('packs/sha256/')]) == len(state['pack_sizes'])
    assert not [k for k in get_keys if k.startswith('objects/sha256/')]
    assert all((tmp_path / 'generated/objects/sha256' / d).read_bytes() == body
               for d, body in samples.items())


def test_packed_publish_failure_leaves_old_metadata_untouched(tmp_path, monkeypatch):
    fixture(tmp_path)
    remote = Remote()
    tool.publish(tmp_path, remote)
    marker = tmp_path / 'generated/r2-storage.json'
    original = marker.read_bytes()
    extra = b'add-packed-content'
    digest = hashlib.sha256(extra).hexdigest()
    (tmp_path / 'generated/objects/sha256' / digest).write_bytes(extra)
    manifest = tmp_path / 'generated/101/manifest.json'
    doc = json.loads(manifest.read_text())
    doc['entries'].append({'artifact_sha256': digest})
    manifest.write_text(json.dumps(doc))
    original_request = remote.request

    def fail_on_packs(key, **kwargs):
        if key.startswith('packs/sha256/'):
            raise OSError('pack upload unavailable')
        return original_request(key, **kwargs)
    monkeypatch.setattr(remote, 'request', fail_on_packs)
    with pytest.raises(OSError, match='pack upload unavailable'):
        tool.publish(tmp_path, remote)
    assert marker.read_bytes() == original


def test_medium_200k_objects_are_packed_without_packing_oversized_objects(tmp_path):
    fixture(tmp_path)
    remote = Remote()
    members = {}
    for index, size in enumerate((120 * 1024, 200 * 1024, 260 * 1024)):
        body = bytes([index + 11]) * size
        digest = hashlib.sha256(body).hexdigest()
        (tmp_path / 'generated/objects/sha256' / digest).write_bytes(body)
        manifest_path = tmp_path / 'generated/101/manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['entries'].append({'artifact_sha256': digest})
        manifest_path.write_text(json.dumps(manifest))
        members[digest] = (body, size)
    tool.publish(tmp_path, remote)
    state = json.loads((tmp_path / 'generated/r2-storage.json').read_text())
    for digest, (body, size) in members.items():
        if size <= tool.PACK_MAX_MEMBER_BYTES:
            pack, offset = state['packed_objects'][digest]
            assert remote.objects['packs/sha256/' + pack][offset:offset + size] == body
        else:
            assert digest not in state['packed_objects']
            assert remote.objects['objects/sha256/' + digest] == body


def test_r2_ci_metrics_count_each_batched_get_and_avoid_http_under_count(tmp_path):
    first, second = b'alpha', b'bravo!'
    rows = [(hashlib.sha256(data).hexdigest(), len(data), tmp_path / str(i))
            for i, data in enumerate((first, second))]
    keys = ['objects/sha256/' + digest for digest, _, _ in rows]
    header = json.dumps([{'key': key, 'size': row[1]} for key, row in zip(keys, rows)]).encode()
    frame = len(header).to_bytes(4, 'big') + header + first + second
    calls = []
    transport = object.__new__(tool.Transport)
    transport.url = 'https://publisher.test'
    transport.token = 'not-a-real-token'
    transport.usage = Counter()
    transport.usage_lock = threading.Lock()
    transport.class_b_limit = 5

    def post(*args, **kwargs):
        calls.append(args[0])
        return nullcontext(SimpleNamespace(status_code=200, raw=io.BytesIO(frame)))

    transport.session = lambda: SimpleNamespace(post=post)
    transport.download_batch(rows)
    assert len(calls) == 1
    assert transport.usage['batch_http'] == 1
    assert transport.usage['class_b_get'] == 2
    assert rows[0][2].read_bytes() == first
    assert rows[1][2].read_bytes() == second


def test_r2_ci_budget_stops_oversized_batch_before_any_network(tmp_path):
    transport = object.__new__(tool.Transport)
    transport.url = 'https://publisher.test'
    transport.token = 'not-a-real-token'
    transport.usage = Counter()
    transport.usage_lock = threading.Lock()
    transport.class_b_limit = 1
    calls = []
    transport.session = lambda: SimpleNamespace(post=lambda *args, **kwargs: calls.append(args))
    digests = [hashlib.sha256(x).hexdigest() for x in (b'a', b'b')]
    with pytest.raises(RuntimeError, match='Class B budget exceeded'):
        transport.download_batch([(digest, 1, tmp_path / digest) for digest in digests])
    assert not calls
    assert transport.usage['class_b_get'] == 0


def test_r2_ci_cost_report_emits_summary_without_exposing_tokens(tmp_path, monkeypatch, capsys):
    report = tmp_path / 'summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(report))
    transport = object.__new__(tool.Transport)
    transport.usage = Counter({'class_b_get': 101, 'class_a_list': 3,
                               'class_a_put': 2, 'skipped_put': 13000,
                               'batch_http': 4, 'free_delete': 1})
    transport.report()
    body = report.read_text()
    assert '101' in body
    assert '13,000' in body
    assert 'estimated' in body.lower()
    assert 'not-a-real-token' not in body
    assert 'R2 CI operations' in capsys.readouterr().out


def test_gc_verifies_only_mutated_prefix_without_relisting_all_objects(tmp_path):
    fixture(tmp_path)
    remote = Remote()
    tool.publish(tmp_path, remote)
    obsolete = hashlib.sha256(b'obsolete-immutable-pack').hexdigest()
    remote.objects['packs/sha256/' + obsolete] = b'obsolete-immutable-pack'
    listings = []
    inventory = remote.inventory

    def measured_inventory(prefix):
        listings.append(prefix)
        return inventory(prefix)

    remote.inventory = measured_inventory
    result = tool.prune(tmp_path, remote, apply=True)
    assert result['delete_objects'] == 1
    assert listings == [
        'objects/sha256/', 'build-cache/sha256/', 'packs/sha256/',
        'packs/sha256/',
    ]
    assert 'packs/sha256/' + obsolete not in remote.objects
    assert any(name.startswith('objects/sha256/') for name in remote.objects)


def test_gc_detects_accidental_deletion_of_retained_item_in_touched_prefix(tmp_path):
    fixture(tmp_path)
    second = b'other-content'
    digest = hashlib.sha256(second).hexdigest()
    (tmp_path / 'generated/objects/sha256' / digest).write_bytes(second)
    manifest = tmp_path / 'generated/101/manifest.json'
    state = json.loads(manifest.read_text())
    state['entries'].append({'artifact_sha256': digest})
    manifest.write_text(json.dumps(state))
    remote = Remote()
    tool.publish(tmp_path, remote)
    current = json.loads((tmp_path / 'generated/r2-storage.json').read_text())
    live_pack = next(iter(current['pack_sizes']))
    obsolete = hashlib.sha256(b'obsolete-pack').hexdigest()
    remote.objects['packs/sha256/' + obsolete] = b'obsolete-pack'
    original_delete = remote.delete

    def flawed_delete(keys):
        original_delete(keys)
        remote.objects.pop('packs/sha256/' + live_pack)

    remote.delete = flawed_delete
    with pytest.raises(ValueError, match='retention verification'):
        tool.prune(tmp_path, remote, apply=True)


def test_gc_detects_acknowledged_but_not_deleted_keys(tmp_path):
    fixture(tmp_path)
    remote = Remote()
    tool.publish(tmp_path, remote)
    obsolete = hashlib.sha256(b'undeleted-pack').hexdigest()
    remote.objects['packs/sha256/' + obsolete] = b'undeleted-pack'
    remote.delete = lambda _: None
    with pytest.raises(ValueError, match='retention verification'):
        tool.prune(tmp_path, remote, apply=True)
