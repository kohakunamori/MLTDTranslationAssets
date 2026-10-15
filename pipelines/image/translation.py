"""Replaceable image -> image translators. No Unity, OCR, review or publication.

A configured translator exposes translate(input_path, output_path). The command
adapter requires only {input} and {output} argv substitutions, never queue data,
model names, OCR boxes or review records. Only the LLM adapter imports transport.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def validate(spec):
    kind = spec.get('kind', 'llm')
    if kind == 'command':
        argv = spec.get('argv')
        if (not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv)
                or not any('{input}' in a for a in argv) or not any('{output}' in a for a in argv)
                or type(spec.get('timeout_seconds', 600)) is not int
                or not 1 <= spec.get('timeout_seconds', 600) <= 7200):
            raise ValueError('Command translator requires argv, {input}, {output} and a bounded timeout')
    elif kind != 'llm':
        raise ValueError('Unknown image translator kind')


def png(raw):
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 33_554_432 or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('Translator must return one bounded image')
        image.load()
        if image.format == 'PNG':
            return raw
        output = io.BytesIO()
        image.convert('RGBA').save(output, format='PNG')
        return output.getvalue()


class Translator:
    def translate(self, input_path: Path, output_path: Path) -> dict:
        """Publish one valid PNG atomically; never mutate the caller's source."""
        source, output = Path(input_path).resolve(), Path(output_path).resolve()
        if source == output:
            raise ValueError('Translator input and output must be distinct')
        before = source.read_bytes()
        canonical = png(before)  # Backends always receive PNG, even for a JPEG input.
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='image-translate-') as directory:
            isolated = Path(directory) / 'input.png'
            candidate = Path(directory) / 'output.png'
            isolated.write_bytes(canonical)
            metadata = self._run(isolated, candidate)
            if isolated.read_bytes() != canonical or source.read_bytes() != before:
                raise ValueError('Translator mutated its input image')
            raw = png(candidate.read_bytes())
            temporary = output.with_name(output.name + '.writing')
            try:
                temporary.write_bytes(raw)
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
        return metadata


class LlmTranslator(Translator):
    def __init__(self, config, prompt):
        self.config, self.prompt = config, prompt

    def _run(self, source, output):
        # Lazy import: a non-LLM processor does not need a provider or API key.
        sys.path.insert(0, str(ROOT / 'pipelines/image'))
        import provider_config
        raw, metadata = provider_config.image_edit(self.config, source.read_bytes(), self.prompt)
        output.write_bytes(raw)
        return metadata


class CommandTranslator(Translator):
    def __init__(self, spec, root=ROOT):
        validate(spec)
        self.spec, self.root = spec, Path(root)

    def _run(self, source, output):
        argv = [a.replace('{input}', str(source)).replace('{output}', str(output)) for a in self.spec['argv']]
        # argv, no shell; child output is not copied to CI logs (may contain secrets).
        try:
            result = subprocess.run(argv, cwd=self.root, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=self.spec.get('timeout_seconds', 600), check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Image processor timed out; no output accepted') from None
        if result.returncode:
            raise RuntimeError(f'Image processor exited with code {result.returncode}; no output accepted')
        return {'requested_image_model': self.spec.get('id', 'external-image-processor'),
                'mode': 'image_command', 'translator_sha256': hashlib.sha256(
                    json.dumps(self.spec, sort_keys=True).encode()).hexdigest()}


def create(policy, root=ROOT, *, config=None, prompt=None):
    spec = policy.get('translator', {'kind': 'llm'})
    validate(spec)
    if spec.get('kind', 'llm') == 'command':
        return CommandTranslator(spec, root)
    if config is None:
        sys.path.insert(0, str(ROOT / 'pipelines/image'))
        import provider_config
        config = copy.deepcopy(provider_config.DEFAULTS)
        public = json.loads((Path(root) / 'configs/api-models.example.json').read_text(encoding='utf-8'))
        config['image_provider'].update(base_url=public['defaults']['base_url'], model=policy['model'],
            mode=policy['api_protocol'], api_key_env='MLTD_LLM_API_KEY', require_api_key=True,
            min_request_interval_seconds=30)
    if prompt is None:
        prompt = (Path(root) / 'configs/image-translation-prompt.txt').read_text(encoding='utf-8').strip()
    return LlmTranslator(config, prompt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/localization-pipeline.json')
    args = parser.parse_args()
    policy = json.loads(args.config.read_text(encoding='utf-8'))['images']
    # This explicit single-image CLI is independent of the automatic CI switch.
    create(policy).translate(args.input, args.output)


if __name__ == '__main__':
    main()
