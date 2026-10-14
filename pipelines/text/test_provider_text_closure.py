"""The provider's plain-text closure must not reach the GTX bundle codec.

``pipelines/text/translate_mltd_api_pool.py`` is executed by the Client's
``llm-translate-client`` job, which installs only ``httpx`` and ``msgpack``.  This
repository's regression environment installs UnityPy and pycryptodome, so a
re-introduced ``mltd_localize_gtx`` import anywhere in the provider's closure
would keep the suite green here and break the consumer's job.

The check therefore runs in a child interpreter and asks the import system,
rather than reading source text: a module can pull the codec in through another
module without naming it, which is exactly how the defect survived review.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CODEC = "pipelines.text.mltd_localize_gtx"

#: Imported by the provider; none of them may reach the bundle codec.
PROVIDER_MODULES = (
    "pipelines.text.translate_mltd_api_pool",
    "pipelines.text.translate_gtx_queue",
    "pipelines.text.mltd_translation_quality",
    "pipelines.text.review_gtx_translations",
    "pipelines.core.jsonl",
    "pipelines.text.translation_tokens",
)

CLOSURE_PROBE = f"""
import json, sys
for name in {PROVIDER_MODULES!r}:
    __import__(name)
import pipelines.core.jsonl as core_jsonl
import pipelines.text.translation_tokens as tokens
import pipelines.text.mltd_translation_quality as quality
import pipelines.text.translate_gtx_queue as queue
import pipelines.text.translate_mltd_api_pool as pool
import pipelines.text.review_gtx_translations as review
print(json.dumps({{
    "codec_loaded": {CODEC!r} in sys.modules,
    "unitypy_loaded": "UnityPy" in sys.modules,
    "crypto_loaded": any(n == "Crypto" or n.startswith("Crypto.") for n in sys.modules),
    "jsonl_reader_shared": pool.read_jsonl is core_jsonl.read_jsonl
        and queue.read_jsonl is core_jsonl.read_jsonl
        and quality.read_jsonl is core_jsonl.read_jsonl
        and review.read_jsonl is core_jsonl.read_jsonl,
    "jsonl_appender_shared": queue.append_rows is core_jsonl.append_rows
        and review.append_rows is core_jsonl.append_rows,
    "validator_shared": pool.validate_translation is tokens.validate_translation
        and queue.validate_translation is tokens.validate_translation
        and quality.validate_translation is tokens.validate_translation,
    "kana_shared": quality.KANA_RE is getattr(tokens, "KANA_RE", None),
}}))
"""

CODEC_PROBE = f"""
import json, sys
import pipelines.text.translation_tokens as tokens
import {CODEC} as codec
print(json.dumps({{
    "codec_loaded": {CODEC!r} in sys.modules,
    "kana_reexported": codec.KANA_RE is tokens.KANA_RE,
    "public_names": sorted(
        name for name in ("read_jsonl", "validate_translation", "KANA_RE",
                          "PROTECTED_TOKEN_RE", "protected_tokens")
        if hasattr(codec, name)
    ),
}}))
"""


def run_probe(code: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"probe failed with {completed.returncode}:\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


class ProviderTextClosureTests(unittest.TestCase):
    def test_provider_closure_never_loads_the_bundle_codec(self):
        report = run_probe(CLOSURE_PROBE)
        self.assertFalse(
            report["codec_loaded"],
            f"{CODEC} is reachable from the provider's imports; the Client's "
            "httpx/msgpack-only job would fail with ModuleNotFoundError",
        )
        self.assertFalse(report["unitypy_loaded"], "UnityPy was imported by the provider")
        self.assertFalse(report["crypto_loaded"], "Crypto was imported by the provider")

    def test_the_shared_helpers_are_the_same_objects(self):
        report = run_probe(CLOSURE_PROBE)
        self.assertTrue(report["jsonl_reader_shared"], "read_jsonl is not shared")
        self.assertTrue(report["validator_shared"], "validate_translation is not shared")
        self.assertTrue(report["kana_shared"], "KANA_RE is not shared")
        self.assertTrue(report["jsonl_appender_shared"], "append_rows is not shared")

    def test_the_probe_would_see_a_loaded_codec(self):
        """Guard the guard: the probe must report a codec that really is loaded."""
        report = run_probe(CODEC_PROBE)
        self.assertTrue(report["codec_loaded"])
        self.assertTrue(report["kana_reexported"], "the codec no longer re-exports KANA_RE")
        self.assertEqual(
            report["public_names"],
            ["KANA_RE", "PROTECTED_TOKEN_RE", "protected_tokens", "read_jsonl",
             "validate_translation"],
            "the codec's historical public names changed",
        )


if __name__ == "__main__":
    unittest.main()
