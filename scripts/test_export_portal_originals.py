"""Offline layout and publication regressions; no CDN or model calls."""
from pathlib import Path
from types import SimpleNamespace as S

from PIL import Image
import pytest

import export_portal_originals as originals


def sprite(name, size=(4, 3), color='red', x=0, y=0, texture_id=1, path_id=1):
    return S(m_Name=name, path_id=path_id, image=Image.new('RGBA', size, color),
             m_Rect=S(x=x, y=y, width=size[0], height=size[1]), m_Pivot=S(X=.5, Y=.5),
             m_RD=S(texture=S(path_id=texture_id, file_id=0),
                    settingsRaw=S(packed=0, packingRotation=0)))


def env(sprites):
    return S(objects=[S(type=S(name='Sprite'), read=lambda s=s: s) for s in sprites])


def test_comic_uses_explicit_reading_order_and_all_six_parts():
    colors = ['red', 'blue', 'green', 'yellow', 'white', 'black']
    sprites = [sprite(f'ex4c_00001_{n}', (4, 2), c) for n, c in enumerate(colors)]
    identity, image, layout = next(originals.complete_images(env(list(reversed(sprites))), 'ex4c00001.unity3d', 'ex4c'))
    assert image.size == (4, 12)
    assert [image.getpixel((0, n*2)) for n in range(6)] == [s.image.getpixel((0, 0)) for s in sprites]
    assert [p['name'] for p in layout['parts']] == [s.m_Name for s in sprites]
    with pytest.raises(ValueError, match='six explicit'):
        list(originals.complete_images(env(sprites[:-1]), 'ex4c00001.unity3d', 'ex4c'))


def test_comic_preserves_slight_source_width_variation_without_stretching():
    image = originals.join_parts([Image.new('RGBA', (4, 2), 'red'), Image.new('RGBA', (6, 2), 'blue')], 'vertical')
    assert image.size == (6, 4)
    assert image.getpixel((0, 0))[3] == 0
    assert image.getpixel((1, 0)) == (255, 0, 0, 255)
    assert image.getpixel((5, 2)) == (0, 0, 255, 255)
    with pytest.raises(ValueError, match='widths disagree'):
        originals.join_parts([Image.new('RGBA', (3, 2)), Image.new('RGBA', (6, 2))], 'vertical')
    footer = originals.join_parts([Image.new('RGBA', (640, 2)), Image.new('RGBA', (632, 2), 'red')], 'vertical')
    assert footer.getpixel((3, 2))[3] == 0
    assert footer.getpixel((4, 2)) == (255, 0, 0, 255)


@pytest.mark.parametrize('suffixes', [('_00', '_01'), ('_1', '_2'), ('_0', '_1')])
def test_split_continuation_rotates_counterclockwise_to_right(suffixes):
    main = sprite('board' + suffixes[0], (4, 3), y=5)
    part = sprite('board' + suffixes[1], (3, 2), 'blue', y=0)
    part.image.putpixel((0, 0), (0, 255, 0, 255))
    _, image, layout = next(originals.complete_images(env([part, main]), 'exwb1.unity3d', 'exwb'))
    assert image.size == (6, 3)
    assert image.getpixel((0, 0)) == (255, 0, 0, 255)
    assert image.getpixel((4, 2)) == (0, 255, 0, 255)
    assert layout['method'] == 'split-sprites-right-ccw90'
    part.m_RD.settingsRaw.packed = 1
    with pytest.raises(ValueError, match='Packed'):
        list(originals.complete_images(env([part, main]), 'exwb1.unity3d', 'exwb'))


def test_stamps_are_independent_complete_sprites_and_unknown_atlases_are_held():
    members = [sprite('stamp_a'), sprite('stamp_b', color='blue', path_id=2)]
    assert len(list(originals.complete_images(env(members), 'chat_stamp.unity3d', 'talk_stamps'))) == 2
    with pytest.raises(ValueError, match='grouping'):
        list(originals.complete_images(env(members), 'unknown.unity3d', 'tutorial'))
    with pytest.raises(ValueError, match='No Sprite'):
        list(originals.complete_images(env([]), 'unknown.unity3d', 'tutorial'))


def test_lossless_webp_deduplicates_without_changing_transparent_pixels(tmp_path):
    image = Image.new('RGBA', (4, 3), (12, 34, 56, 0))
    image.putpixel((1, 1), (123, 45, 67, 128))
    reference = originals.publish_image(image, tmp_path)
    assert originals.publish_image(image.copy(), tmp_path) == reference
    path = tmp_path / reference['relative_path']
    assert originals.sha(path) == reference['sha256']
    with Image.open(path) as decoded:
        assert decoded.convert('RGBA').tobytes() == image.tobytes()
    assert len(list((tmp_path / 'images/original/complete').iterdir())) == 1


def test_simultaneous_duplicate_publication_keeps_one_verified_file(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    image = Image.new('RGBA', (64, 64), (123, 45, 67, 128))
    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(lambda _: originals.publish_image(image.copy(), tmp_path), range(12)))
    assert all(record == records[0] for record in records)
    assert originals.sha(tmp_path / records[0]['relative_path']) == records[0]['sha256']
    assert len(list((tmp_path / 'images/original/complete').iterdir())) == 1
