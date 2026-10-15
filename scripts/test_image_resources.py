"""Registry coverage and deterministic, approved-parent thumbnail derivation."""
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

import image_resources as resources
import image_thumbnails as thumbs
import incremental_images as images


@pytest.mark.parametrize('name,family', [
    ('achievement_999.unity3d', 'achievement'), ('event_0451.unity3d', 'event_ui'),
    ('loginbonus_panel_282_1.unity3d', 'login_bonus'), ('mycard_logo_event_0448.unity3d', 'mycard_event_logo'),
    ('gasha99998.unity3d', 'gasha'), ('item_9992.unity3d', 'item_icons'),
    ('blog60002.unity3d', 'blog'), ('event_talk_thumb_1806.unity3d', 'event_talk_preview'),
    ('blog60002_thumb.unity3d', 'blog_thumbnail'), ('titlebg_01105_thumb.unity3d', 'titlebg_thumbnail'),
])
def test_reviewed_families_are_discovered(name, family):
    assert resources.classify(name) == family


def test_thumbnails_wait_for_parent_and_never_enter_standalone_translation():
    row = {'catalog_hash': 'h', 'remote': 'r', 'declared_size': 1}
    index = {'ex4c00959.unity3d': row, 'ex4c00959_thumb.unity3d': row}
    pending, counts, _ = images.select(index, {}, {}, 10)
    assert [n for n, _ in pending] == ['ex4c00959.unity3d']
    assert counts['comic_thumbnail']['deferred'] == 1
    pending, _, _ = images.select(index, {}, {'ex4c00959.unity3d': 'r'}, 10)
    assert [n for n, _ in pending] == ['ex4c00959_thumb.unity3d']
    assert resources.thumbnail_parent('event_talk_thumb_1806.unity3d') is None


def noise(size=(120, 80), seed=7):
    return Image.fromarray(np.random.default_rng(seed).integers(0, 256, (size[1], size[0], 3), dtype=np.uint8)).convert('RGBA')


def test_thumbnail_finds_correct_panel_and_holds_unknown_or_ambiguous_layout():
    panel = noise()
    thumbnail = panel.resize((60, 40), Image.Resampling.LANCZOS)
    key, metrics = thumbs.match_thumbnail(thumbnail, {1: noise(seed=12), 2: panel})
    assert key == 2 and metrics['mean_absolute_rgb_error'] == 0
    with pytest.raises(ValueError, match='Ambiguous'):
        thumbs.match_thumbnail(thumbnail, {1: panel, 2: panel.copy()})
    with pytest.raises(ValueError, match='No reliable'):
        thumbs.match_thumbnail(thumbnail, {1: noise(seed=12)})


def fixture(tmp_path):
    root, work = tmp_path / 'root', tmp_path / 'work'
    root.mkdir(); work.mkdir()
    source = noise(); original = work / 'parent.png';source.save(original)
    preview = work / 'thumb.png';source.resize((60, 40), Image.Resampling.LANCZOS).save(preview)
    localized = root / 'images/localized/parent.png';localized.parent.mkdir(parents=True)
    Image.new('RGBA', source.size, (10, 70, 20, 180)).save(localized)
    archive = work / 'thumb.unity3d';archive.write_bytes(b'thumb source')
    parent_archive = work / 'parent.unity3d';parent_archive.write_bytes(b'parent source')
    part = {'source_sha256': thumbs.sha(original), 'localized': 'images/localized/parent.png', 'sha256': thumbs.sha(localized)}
    images.write(root / 'manifests/image-incremental.json', {'bundles': {'parent.unity3d': {
        'status': 'translated', 'archive_sha256': thumbs.sha(parent_archive), 'textures': [{'id': 10, **part}]}}})
    bundle = {'logical': 'preview.unity3d', 'archive': archive.name, 'archive_sha256': thumbs.sha(archive),
        'identity': ['h', 'remote.unity3d', 12], 'tasks': [], 'derivation': {'status': 'ready',
        'parent': 'parent.unity3d', 'parent_archive': parent_archive.name,
        'parent_candidates': [{'id': 10, 'original': original.name, 'sha256': thumbs.sha(original)}],
        'parent_archive_sha256': thumbs.sha(parent_archive), 'textures': [{'id': 20, 'parent_texture_id': 10,
        'original': preview.name, 'parent_original': original.name, 'size': [60, 40],
        'thumbnail_source_sha256': thumbs.sha(preview), 'match': {}, **part}]}}
    return root, work, bundle


def test_derivation_inherits_only_approved_pixels_preserves_target_alpha_and_never_calls_models(tmp_path):
    root, work, bundle = fixture(tmp_path)
    record, rows = thumbs.package_derivation(root, work, bundle, '100')
    image = Image.open(root / record['textures'][0]['localized']).convert('RGBA')
    assert image.size == (60, 40) and image.getpixel((4, 4)) == (10, 70, 20, 255)
    assert record['derivation']['model_called'] is False
    assert rows[0]['original_png_sha256'] == thumbs.sha(work / 'thumb.png')


@pytest.mark.parametrize('changed', ['parent.png', 'thumb.png', 'parent.unity3d', 'thumb.unity3d', 'localized'])
def test_modified_parent_or_thumbnail_cannot_be_packaged(tmp_path, changed):
    root, work, bundle = fixture(tmp_path)
    path = root / 'images/localized/parent.png' if changed == 'localized' else work / changed
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        thumbs.package_derivation(root, work, bundle, '100')


def test_package_accepts_derived_previews_without_fabricating_a_visual_model_pass(tmp_path, monkeypatch):
    root, work, bundle = fixture(tmp_path)
    monkeypatch.setattr(images, 'ROOT', root)
    images.write(root / 'manifests/asset-version.json', {'asset_version': '100'})
    images.write(work / 'queue.json', {'asset_version': '100', 'selected_bundles': 1,
        'pending_bundles': 1, 'bundles': [bundle]})
    images.package(work)
    record = images.read(work / 'next-state.json')['bundles'][bundle['logical']]
    assert record['status'] == 'translated'
    assert not record.get('visual_reviews')
    assert len((work / 'install.jsonl').read_text().splitlines()) == 1


def test_parent_translation_change_requeues_an_unchanged_official_thumbnail():
    row = {'catalog_hash': 'h', 'remote': 'r', 'declared_size': 1}
    name, parent = 'ex4c00959_thumb.unity3d', 'ex4c00959.unity3d'
    index = {name: row, parent: row}
    state = {parent: {'identity': images.identity(row), 'status': 'translated',
        'archive_sha256': 'archive', 'textures': [{'sha256': 'first-translation'}]}}
    dependency = resources.thumbnail_dependency(name, index, state, {})
    state[name] = {'identity': images.identity(row), 'status': 'translated', 'derivation': {'dependency': dependency}}
    assert images.select(index, state, {}, 10)[0] == []
    state[parent]['textures'][0]['sha256'] = 'new-translation'
    assert [n for n, _ in images.select(index, state, {}, 10)[0]] == [name]
    assert [n for n, _ in images.select(index, state, {name: 'r'}, 10)[0]] == [name]


def test_unknown_thumbnail_match_is_recorded_for_review_not_repeated_or_sent_to_model(tmp_path):
    root, work, bundle = fixture(tmp_path)
    bundle['derivation'].update(status='deferred', reason='No reliable full-size texture match', dependency='dep')
    record, rows = thumbs.package_derivation(root, work, bundle, '100')
    assert record['status'] == 'manual_review' and not rows
    assert record['derivation']['model_called'] is False


def test_screened_artwork_is_not_reported_as_translated():
    row = {'catalog_hash': 'h', 'remote': 'r', 'declared_size': 1}
    name = 'item_9992.unity3d'
    state = {name: {'identity': images.identity(row), 'status': 'ocr_no_text'}}
    pending, counts, _ = images.select({name: row}, state, {}, 10)
    assert not pending and counts['item_icons']['ocr_no_text'] == 1
    assert counts['item_icons']['completed'] == 0
