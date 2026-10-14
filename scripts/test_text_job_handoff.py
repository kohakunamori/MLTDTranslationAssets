import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import text_job_handoff as handoff


class HandoffTests(unittest.TestCase):
    def test_expanded_budget_refuses_transfer_before_any_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root / 'source')
            with patch.object(handoff, 'MAX_BYTES', 1), self.assertRaises(ValueError):
                handoff.pack(root / 'source', root / 'transfer', 'abc')
            self.assertFalse((root / 'transfer/identity.json').exists())

    def fixture(self, root):
        for name, content in {
            'manifests/asset-version.json': json.dumps({'asset_version': '1077750', 'index_name': 'index.data'}),
            'locales/master/new.jsonl': '{"source":"new"}\n',
            'lyrics/songs/new.jsonl': '{"source":"song"}\n',
            '.llm-official-work/index.data': 'index',
            '.llm-official-work/huge.unity3d': 'must not transfer',
            'generated/1077750/manifest.json': '{}',
            'generated/1077750/bundle.unity3d': 'must not transfer',
        }.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def test_restores_exact_sources_including_deletions_without_bundles(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, target, transfer = base / 'source', base / 'target', base / 'transfer'
            self.fixture(source)
            (target / 'locales').mkdir(parents=True)
            (target / 'locales/stale.jsonl').write_text('stale')
            identity = handoff.pack(source, transfer, 'abc')
            handoff.restore(target, transfer, 'abc', identity['sha256'])
            self.assertFalse((target / 'locales/stale.jsonl').exists())
            self.assertTrue((target / 'locales/master/new.jsonl').is_file())
            self.assertTrue((target / '.llm-official-work/index.data').is_file())
            self.assertFalse((target / '.llm-official-work/huge.unity3d').exists())
            self.assertFalse((target / 'generated/1077750/bundle.unity3d').exists())

    def test_wrong_source_or_corrupt_archive_is_refused_before_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root / 'source')
            identity = handoff.pack(root / 'source', root / 'transfer', 'abc')
            with self.assertRaises(ValueError):
                handoff.restore(root / 'target', root / 'transfer', 'def', identity['sha256'])
            with (root / 'transfer/text-sources.tar.gz').open('ab') as stream:
                stream.write(b'corrupt')
            with self.assertRaises(ValueError):
                handoff.restore(root / 'target', root / 'transfer', 'abc', identity['sha256'])
            self.assertFalse((root / 'target').exists())

    def test_path_traversal_and_links_are_refused(self):
        for name, type_ in [('../escape', tarfile.REGTYPE), ('locales/link', tarfile.SYMTYPE),
                            ('.git/config', tarfile.REGTYPE), ('generated/1/bundle', tarfile.REGTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / 'text-sources.tar.gz'
                with tarfile.open(archive, 'w:gz') as stream:
                    member = tarfile.TarInfo(name)
                    member.type = type_
                    member.size = 1 if type_ == tarfile.REGTYPE else 0
                    stream.addfile(member, io.BytesIO(b'x') if member.size else None)
                sha = handoff.digest(archive)
                (root / 'identity.json').write_text(json.dumps({'source_commit': 'abc', 'sha256': sha}))
                with self.assertRaises(ValueError):
                    handoff.restore(root / 'target', root, 'abc', sha)


if __name__ == '__main__':
    unittest.main()
