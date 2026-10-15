"""One registry for image discovery, reporting and thumbnail dependencies."""
import json
import re
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = json.loads((ROOT / 'configs/image-resources.json').read_text(encoding='utf-8'))
if REGISTRY.get('schema_version') != 1:
    raise ValueError('Unsupported image resource registry')
FAMILIES = REGISTRY['translation_families']
THUMBNAILS = REGISTRY['thumbnail_rules']
for pattern in (*FAMILIES.values(), *THUMBNAILS.values()):
    re.compile(pattern)


def thumbnail_parent(logical):
    for pattern in THUMBNAILS.values():
        match = re.fullmatch(pattern, logical)
        if match:
            return match['parent'] + '.unity3d'
    return None


def classify(logical):
    for family, pattern in THUMBNAILS.items():
        if re.fullmatch(pattern, logical):
            return family
    return next((f for f, p in FAMILIES.items() if re.fullmatch(p, logical)), None)


def thumbnail_dependency(logical, index, state, published, published_fingerprints=None):
    parent = thumbnail_parent(logical)
    previous = state.get(parent, {})
    evidence = {'algorithm': 'full-texture-lanczos-v1', 'parent': parent,
        'identity': index.get(parent), 'published': published.get(parent),
        'approved_archive': previous.get('archive_sha256'),
        'published_artifact': (published_fingerprints or {}).get(parent)
                              if previous.get('status') != 'translated' else None,
        'approved_textures': [t.get('sha256') for t in previous.get('textures', [])]
                            if previous.get('status') == 'translated' else []}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
