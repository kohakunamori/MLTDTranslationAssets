"""The same two-file contract works for LLMs and arbitrary local processors."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image
import pytest

from pipelines.image import translation
import incremental_images as images


def command(tmp_path, body):
    script = tmp_path / 'processor with spaces.py'
    script.write_text(body, encoding='utf-8')
    return {'kind': 'command', 'id': 'test-processor', 'argv': [sys.executable, str(script), '{input}', '{output}'],
            'timeout_seconds': 10}


def source(tmp_path):
    path = tmp_path / 'input.png'
    Image.new('RGBA', (12, 8), (80, 20, 30, 127)).save(path)
    return path


def test_command_requires_only_images_and_no_model_key(tmp_path, monkeypatch):
    monkeypatch.delenv('MLTD_LLM_API_KEY', raising=False)
    spec = command(tmp_path, "from PIL import Image\nimport sys\nim=Image.open(sys.argv[1]); im.save(sys.argv[2])\n")
    original = source(tmp_path)
    before = original.read_bytes()
    output = tmp_path / 'output.png'
    metadata = translation.create({'translator': spec}, tmp_path).translate(original, output)
    assert output.read_bytes() == before == original.read_bytes()
    assert metadata['mode'] == 'image_command'


@pytest.mark.parametrize('body,match', [
    ("raise SystemExit(3)", 'code 3'),
    ("import sys\nopen(sys.argv[2],'wb').write(b'not a PNG')", 'cannot identify'),
    ("import sys\nopen(sys.argv[1],'wb').write(b'mutated')\nopen(sys.argv[2],'wb').write(b'invalid')", 'mutated'),
])
def test_failed_processor_preserves_source_and_previous_output(tmp_path, body, match):
    original = source(tmp_path)
    before = original.read_bytes()
    output = tmp_path / 'output.png'; output.write_bytes(b'previous output')
    backend = translation.create({'translator': command(tmp_path, body)}, tmp_path)
    with pytest.raises((RuntimeError, ValueError, OSError), match=match):
        backend.translate(original, output)
    assert original.read_bytes() == before
    assert output.read_bytes() == b'previous output'
    assert not output.with_name(output.name + '.writing').exists()


def test_timeout_does_not_accept_partial_output(tmp_path):
    spec = command(tmp_path, "import time\ntime.sleep(5)")
    spec['timeout_seconds'] = 1
    with pytest.raises(RuntimeError, match='timed out'):
        translation.create({'translator': spec}, tmp_path).translate(source(tmp_path), tmp_path / 'out.png')
    assert not (tmp_path / 'out.png').exists()


def test_llm_uses_identical_file_contract_and_existing_transport(tmp_path, monkeypatch):
    import provider_config
    original = source(tmp_path)
    raw = original.read_bytes()
    calls = []
    def edit(config, data, prompt):
        calls.append((data, prompt))
        return raw, {'requested_image_model': 'mock'}
    monkeypatch.setattr(provider_config, 'image_edit', edit)
    result = translation.create({}, config={}, prompt='MLTD logo protected').translate(original, tmp_path / 'out.png')
    assert calls == [(raw, 'MLTD logo protected')]
    assert result['requested_image_model'] == 'mock'


def test_single_image_cli_runs_external_processor_with_no_queue_metadata(tmp_path):
    spec = command(tmp_path, "import shutil,sys\nshutil.copyfile(sys.argv[1],sys.argv[2])")
    config = tmp_path / 'config.json';config.write_text(json.dumps({'images': {'translator': spec}}), encoding='utf-8')
    output = tmp_path / 'out.png'
    result = subprocess.run([sys.executable, str(Path(translation.__file__)), '--input', str(source(tmp_path)),
        '--output', str(output), '--config', str(config)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert Image.open(output).size == (12, 8)


def test_adapter_canonicalizes_jpeg_input_and_output_to_png(tmp_path):
    original = tmp_path / 'input.jpg'; Image.new('RGB', (8, 8), 'red').save(original)
    before = original.read_bytes()
    spec = command(tmp_path, "from PIL import Image\nimport sys\nim=Image.open(sys.argv[1]); assert im.format=='PNG'; im.convert('RGB').save(sys.argv[2],format='JPEG')")
    output = tmp_path / 'out.png'
    translation.create({'translator': spec}, tmp_path).translate(original, output)
    assert Image.open(output).format == 'PNG' and original.read_bytes() == before


def test_incremental_command_backend_needs_no_generation_credentials_and_resumes(tmp_path, monkeypatch):
    from test_incremental_images import task_fixture
    work, original, _, _ = task_fixture(tmp_path, monkeypatch)
    spec = command(tmp_path, "import shutil,sys\nshutil.copyfile(sys.argv[1],sys.argv[2])")
    policy = {'translator': spec, 'concurrency': 1, 'restoration_mode': 'whole_image',
              'prompt_profile': 'preserve_layout', 'visual_acceptance': {'enabled': False}}
    images.write(tmp_path / 'configs/localization-pipeline.json', {'images': policy})
    images.write(tmp_path / 'configs/api-models.example.json', {'defaults': {'base_url': 'https://unused.test'}})
    monkeypatch.delenv('MLTD_LLM_API_KEY', raising=False)
    images.generate(work)
    result = images.read(work / 'results.json')['tasks'][images.sha(original)]
    assert result['provider']['mode'] == 'image_command'
    # Removing the command proves resumption consumes the saved output.
    Path(spec['argv'][1]).unlink()
    images.generate(work)
    assert images.read(work / 'results.json')['tasks'][images.sha(original)]['sha256'] == result['sha256']


@pytest.mark.parametrize('spec', [{'kind': 'unknown'}, {'kind': 'command', 'argv': ['tool', '{input}']},
    {'kind': 'command', 'argv': 'tool {input} {output}'}])
def test_invalid_adapter_contract_fails_before_execution(spec):
    with pytest.raises(ValueError):
        translation.validate(spec)
