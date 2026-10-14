#!/usr/bin/env python3
"""Transfer synchronized text sources, never official bundles, to a small runner."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile

TREES = ('locales', 'lyrics', 'manifests')
REPORTS = ('.localization-features.json', '.llm-asset-check.json', '.llm-event-units.json',
           '.llm-discovery.json', '.llm-discovery-summary.json', '.llm-lyrics.json',
           '.llm-coverage.json', '.llm-reuse.json')
MAX_BYTES = 512 * 1024**2
MAX_ARCHIVE = 128 * 1024**2


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            result.update(chunk)
    return result.hexdigest()


def source_paths(root):
    manifest = json.loads((root / 'manifests/asset-version.json').read_text())
    index = str(manifest['index_name'])
    version = str(manifest['asset_version'])
    if Path(index).name != index or not version.isdigit():
        raise ValueError('Invalid source index/version identity')
    required = root / '.llm-official-work' / index
    if not required.is_file():
        raise ValueError('Missing synchronized official index')
    paths = []
    for tree in TREES:
        paths.extend(path for path in (root / tree).rglob('*') if path.is_file())
    paths.extend(root / report for report in REPORTS if (root / report).is_file())
    paths.append(required)
    generated = root / 'generated' / version / 'manifest.json'
    if generated.is_file():
        paths.append(generated)
    if any(path.is_symlink() for path in paths):
        raise ValueError('Symlink source refused')
    if sum(path.stat().st_size for path in paths) > MAX_BYTES:
        raise ValueError('Text handoff exceeds 512 MiB expanded budget')
    return sorted(paths)


def pack(root, directory, source_commit):
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / 'text-sources.tar.gz'
    with tarfile.open(archive, 'w:gz') as stream:
        for path in source_paths(root):
            stream.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError('Text handoff exceeds 128 MiB archive budget')
    identity = {'source_commit': source_commit, 'sha256': digest(archive)}
    (directory / 'identity.json').write_text(json.dumps(identity) + '\n')
    return identity


def allowed(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        return False
    return (path.parts[0] in TREES or name in REPORTS or
            (len(path.parts) == 2 and path.parts[0] == '.llm-official-work') or
            (len(path.parts) == 3 and path.parts[0] == 'generated' and
             path.parts[1].isdigit() and path.parts[2] == 'manifest.json'))


def restore(root, directory, source_commit, sha256):
    archive = directory / 'text-sources.tar.gz'
    identity = json.loads((directory / 'identity.json').read_text())
    if identity != {'source_commit': source_commit, 'sha256': sha256} or digest(archive) != sha256:
        raise ValueError('Text handoff does not match this run/source/hash')
    with tarfile.open(archive, 'r:gz') as stream:
        members = stream.getmembers()
        if sum(member.size for member in members) > MAX_BYTES:
            raise ValueError('Expanded text handoff exceeds budget')
        names = [member.name for member in members]
        if len(set(names)) != len(names) or any(not member.isfile() or not allowed(member.name) for member in members):
            raise ValueError('Unsafe or non-text member in handoff')
        # Validate every target before deleting the old source snapshot. Existing
        # symlinks must never redirect writes outside the checkout.
        for member in members:
            target = root / member.name
            if not target.resolve().is_relative_to(root.resolve()):
                raise ValueError('Handoff target escapes checkout')
        for tree in TREES:
            path = root / tree
            if path.is_symlink():
                raise ValueError('Source tree is a symlink')
            if path.exists():
                shutil.rmtree(path)
        for member in members:
            target = root / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['pack', 'restore'])
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--sha256')
    args = parser.parse_args()
    if args.operation == 'pack':
        identity = pack(args.root, args.directory, args.source_commit)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
                stream.write('sha256=' + identity['sha256'] + '\n')
                stream.write('source_commit=' + identity['source_commit'] + '\n')
    else:
        if not args.sha256:
            parser.error('restore requires --sha256 from the prepare job output')
        identity = restore(args.root, args.directory, args.source_commit, args.sha256)
    print(json.dumps(identity))


if __name__ == '__main__':
    main()
