"""Exact same-texture Sprite reconstruction, no visual pairing guesses."""
import copy
from types import SimpleNamespace

import pytest
from PIL import Image

import incremental_images as images
import image_visual_review as visual
from discover_sprite_layout import sprite_pair_layout
from internal_texture_image25 import compose_regions, restore_regions


def sprite_rows():
    return [(11, {'m_Name': 'info_00_00', 'm_Rect': {'x': 1., 'y': 145., 'width': 510., 'height': 366.},
                  'm_RD': {'texture': {'m_FileID': 0, 'm_PathID': 7},
                           'textureRect': {'x': 1., 'y': 145., 'width': 510., 'height': 366.}, 'settingsRaw': 64}}),
            (12, {'m_Name': 'info_00_01', 'm_Rect': {'x': 1., 'y': 1., 'width': 366., 'height': 140.},
                  'm_RD': {'texture': {'m_FileID': 0, 'm_PathID': 7},
                           'textureRect': {'x': 1., 'y': 1., 'width': 366., 'height': 140.}, 'settingsRaw': 64}})]


def fixture():
    original = Image.new('RGBA', (512, 512), (91, 32, 13, 17))
    original.paste((10, 20, 30, 128), (1, 1, 511, 367))
    original.paste((40, 50, 60, 222), (1, 371, 367, 511))
    original.putpixel((1, 371), (255, 0, 0, 77))
    original.putpixel((366, 510), (0, 255, 0, 99))
    layout = sprite_pair_layout('info_00', 7, original.size, sprite_rows())
    canvas, mapping = compose_regions(original, layout['regions'], layout['compose_direction'])
    return original, layout, canvas, mapping


def test_exact_sprite_pair_order_rotation_and_lossless_roundtrip():
    original, layout, canvas, mapping = fixture()
    assert canvas.size == (650, 366)
    assert canvas.getpixel((510, 365)) == original.getpixel((1, 371))
    assert canvas.getpixel((649, 0)) == original.getpixel((366, 510))
    assert restore_regions(original, canvas, mapping).tobytes() == original.tobytes()
    assert [m['canvas_box'] for m in mapping] == [[0, 0, 510, 366], [510, 0, 650, 366]]


def test_edits_inverse_rotate_but_keep_alpha_and_atlas_padding():
    original, _, canvas, mapping = fixture()
    canvas.paste((1, 2, 3, 0), (0, 0, *canvas.size))
    restored = restore_regions(original, canvas, mapping)
    assert restored.getpixel((1, 371)) == (1, 2, 3, 77)
    assert restored.getpixel((0, 0)) == original.getpixel((0, 0))
    assert restored.getpixel((400, 400)) == original.getpixel((400, 400))
    assert restored.getchannel('A').tobytes() == original.getchannel('A').tobytes()


@pytest.mark.parametrize('kind', ['duplicate', 'missing', 'foreign', 'external', 'packing', 'fraction', 'rect'])
def test_ambiguous_or_unsupported_pair_fails_closed(kind):
    rows = sprite_rows()
    if kind == 'duplicate': rows.append(copy.deepcopy(rows[0]))
    if kind == 'missing': rows.pop()
    if kind == 'foreign': rows[0][1]['m_RD']['texture']['m_PathID'] = 8
    if kind == 'external': rows[0][1]['m_RD']['texture']['m_FileID'] = 1
    if kind == 'packing': rows[0][1]['m_RD']['settingsRaw'] = 0
    if kind == 'fraction':
        rows[0][1]['m_Rect']['x'] = 1.5
        rows[0][1]['m_RD']['textureRect']['x'] = 1.5
    if kind == 'rect': rows[0][1]['m_RD']['textureRect']['x'] = 2
    with pytest.raises(ValueError): sprite_pair_layout('info_00', 7, (512, 512), rows)


def test_no_sprite_evidence_leaves_ordinary_texture_alone():
    assert sprite_pair_layout('unrelated', 7, (512, 512), sprite_rows()) is None


def prepare_fixture(tmp_path, monkeypatch):
    import UnityPy
    import build_generated_release as release
    original, _, _, _ = fixture()
    monkeypatch.setattr(images, 'ROOT', tmp_path)
    images.write(tmp_path / 'manifests/asset-version.json', {'asset_version': '100'})
    images.write(tmp_path / 'configs/localization-pipeline.json', {'images': {
        'model': 'gpt-image-2.5', 'api_protocol': 'images_edits', 'prompt_profile': 'preserve_layout',
        'requests_per_minute': 2, 'concurrency': 1,
        'restoration_mode': 'whole_image', 'visual_acceptance': {'enabled': True, 'model': visual.MODEL,
                                                             'api_protocol': 'gemini_generate_content'}}})
    (tmp_path / 'configs/image-translation-prompt.txt').write_text('Translate this whole page', encoding='utf-8')
    version = {'asset_version': '100', 'asset_root': 'https://example.invalid', 'index_name': 'index.data'}
    row = {'catalog_hash': 'h', 'remote': 'remote.unity3d', 'declared_size': 7}
    monkeypatch.setattr(release, 'load_version_manifest', lambda _: version)
    monkeypatch.setattr(release, 'load_official_index', lambda _: {'event_0454_info.unity3d': row})
    def download(url, path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'archive')
    monkeypatch.setattr(release, 'download', download)
    texture = SimpleNamespace(type=SimpleNamespace(name='Texture2D'), path_id=7,
                              read=lambda: SimpleNamespace(m_Name='info_00', image=original))
    objects = [texture] + [SimpleNamespace(type=SimpleNamespace(name='Sprite'), path_id=pid,
                                           read_typetree=lambda data=data: data) for pid, data in sprite_rows()]
    monkeypatch.setattr(UnityPy, 'load', lambda _: SimpleNamespace(objects=objects))
    seen = []
    def ocr(path):
        seen.append(Image.open(path).size)
        return [{'box': [1, 1, 20, 20], 'source_text': '日本', 'confidence': 99}]
    monkeypatch.setattr(images, 'text_regions', ocr)
    work = tmp_path / 'work'
    work.mkdir()
    queue = images.prepare(work, 1, only=['event_0454_info.unity3d'])
    return work, queue, original, seen


def test_prepare_records_unsupported_layout_without_implicit_composition(tmp_path, monkeypatch, capsys):
    def unsupported(*args):
        raise ValueError('unsupported packing')
    import discover_sprite_layout
    monkeypatch.setattr(discover_sprite_layout, 'sprite_pair_layout', unsupported)
    work, queue, _, seen = prepare_fixture(tmp_path, monkeypatch)
    task = queue['bundles'][0]['tasks'][0]
    assert seen == [(512, 512)]
    assert 'sprite_composition' not in task
    assert task['layout'][0]['sprite_composition_rejected'] == 'unsupported packing'
    assert 'SPRITE_LAYOUT_UNSUPPORTED' in capsys.readouterr().out


def test_prepare_ocr_uses_reconstructed_page_and_binds_source(tmp_path, monkeypatch):
    work, queue, original, seen = prepare_fixture(tmp_path, monkeypatch)
    task = queue['bundles'][0]['tasks'][0]
    assert seen == [(650, 366)]
    assert Image.open(work / task['input']).size == (650, 366)
    composition = task['sprite_composition']
    assert composition['texture_path_id'] == 7
    assert composition['archive_sha256'] == queue['bundles'][0]['archive_sha256']
    assert composition['source_sha256'] == task['layout'][0]['source_sha256']
    assert task['sha256'] != composition['source_sha256']  # old square cache cannot approve this page


def test_package_approved_page_restores_original_texture_and_keeps_provenance(tmp_path, monkeypatch):
    work, queue, original, _ = prepare_fixture(tmp_path, monkeypatch)
    task = queue['bundles'][0]['tasks'][0]
    source = work / task['input']
    candidate = work / 'candidate.png'
    changed = Image.open(source).convert('RGBA')
    changed.putpixel((0, 0), (1, 2, 3, changed.getpixel((0, 0))[3]))
    changed.save(candidate)
    prompt = images.image_prompt(task, 'preserve_layout')
    review = {'status': 'reviewed', 'approved': True,
              'binding': visual.binding(source.read_bytes(), candidate.read_bytes(), prompt),
              'assessment': {'verdict': 'pass', 'checks': {key: True for key in visual.CHECKS}, 'issues': []}}
    images.write(work / 'results.json', {'tasks': {task['sha256']: {
        'status': 'generated', 'output': 'candidate.png', 'sha256': images.sha(candidate),
        'normalized_output': 'candidate.png', 'normalized_sha256': images.sha(candidate),
        'provider': {'requested_image_model': 'gpt-image-2.5', 'mode': 'images_edits'},
        'normalization': {'changed_pixels': 1}, 'visual_review': review}}})
    images.package(work)
    record = images.read(work / 'next-state.json')['bundles']['event_0454_info.unity3d']
    assert record['status'] == 'translated'
    restored = Image.open(tmp_path / record['textures'][0]['localized'])
    assert restored.size == (512, 512)
    assert restored.getpixel((1, 1))[:3] == (1, 2, 3)
    assert restored.getpixel((0, 0)) == original.getpixel((0, 0))
    assert restored.getchannel('A').tobytes() == original.getchannel('A').tobytes()
    assert record['text_maps'][task['sha256']]['sprite_composition'] == task['sprite_composition']
    assert len((work / 'install.jsonl').read_text().splitlines()) == 1


@pytest.mark.parametrize('field', ['archive_sha256', 'source_sha256', 'texture_path_id', 'region_map'])
def test_inverse_restore_refuses_tampered_source_or_mapping(tmp_path, monkeypatch, field):
    work, queue, original, _ = prepare_fixture(tmp_path, monkeypatch)
    task = queue['bundles'][0]['tasks'][0]
    task['sprite_composition'][field] = 'tampered'
    with pytest.raises(ValueError):
        images.restore_task_texture(task, task['layout'][0], original, Image.open(work/task['input']),
                                    Image.open(work/task['input']), queue['bundles'][0]['archive_sha256'])
