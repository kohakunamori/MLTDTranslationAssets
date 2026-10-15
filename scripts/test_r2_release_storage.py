"""R2 publication failure/recovery invariants, without network access."""
import hashlib
import io
import json
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
