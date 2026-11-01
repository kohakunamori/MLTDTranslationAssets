import asyncio
import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

import httpx

from pipelines.core.hashing import sha256_text
from pipelines.text.mltd_given_name_hints import (
    applicable_given_name_hints,
    load_default_given_name_hints,
    load_given_name_hints,
    render_given_name_hints,
)
from pipelines.text.translate_mltd_api_pool import (
    ModelConfig,
    build_parser,
    build_user_prompt,
    translate_with_model,
)

ROOT = Path(__file__).resolve().parents[1]
HINTS_PATH = ROOT / "glossary" / "given-name-hints.json"


def _config() -> ModelConfig:
    return ModelConfig(
        id="test",
        model="alias",
        api_protocol="chat_completions",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key_env="",
        concurrency=1,
        reasoning_effort="none",
        temperature=0.1,
        timeout=10,
        retries=1,
        cache_anchor="",
        cache_warmup=False,
    )


def _row(source: str, key: str = "commands[0].text", **extra):
    return {
        "source": source,
        "source_sha256": sha256_text(source),
        "key": key,
        "task_hint": "DIALOGUE",
        **extra,
    }


class GivenNameHintTests(unittest.TestCase):
    def test_honorific_gate_and_compound_prefix_guard(self):
        document = load_given_name_hints(HINTS_PATH)
        positives = {
            "このみさん、何か悩んでるんですか？": "このみ",
            "やよいちゃんは決まりそう？": "やよい",
            "あずささん、今、鼻歌を歌っていましたか？": "あずさ",
            "まつりさん、おはよう。": "まつり",
            "莉緒とこのみさん、企画を進めます。": "このみ",
        }
        for source, expected in positives.items():
            selected = applicable_given_name_hints(_row(source), document)
            self.assertEqual([entry.source_form for entry in selected], [expected], source)

        negatives = [
            "このみ",
            "このみのドアデコ",
            "きのこのみちゃん",
            "なつまつりちゃん",
            "夏まつりちゃん",
            "その好みは人それぞれだ。",
            "明るい未来の舞台を夢見ている。",
            "一歩ずつ歩いていこう。",
            "秋祭りは来週だ。",
        ]
        for source in negatives:
            self.assertEqual(applicable_given_name_hints(_row(source), document), (), source)

        context_only = _row(
            "こんにちは。",
            context_examples=[{"context": [{"relative": 0, "source": "このみさん"}]}],
        )
        self.assertEqual(applicable_given_name_hints(context_only, document), ())

    def test_typed_key_and_mail_reply_source_gate(self):
        document = load_given_name_hints(HINTS_PATH)
        konomi = next(entry for entry in document.entries if entry.source_form == "このみ")
        self.assertEqual(
            [entry.source_form for entry in applicable_given_name_hints(
                _row("このみ", key=konomi.typed_record_key), document
            )],
            ["このみ"],
        )
        self.assertEqual(
            applicable_given_name_hints(
                _row("このみ", key=konomi.typed_record_key, surface_hint="MAIL_REPLY"),
                document,
            ),
            (),
        )
        self.assertEqual(
            [entry.source_form for entry in applicable_given_name_hints(
                _row("このみさん", key="reply.choice", surface_hint="MAIL_REPLY"),
                document,
            )],
            ["このみ"],
        )
        self.assertEqual(
            applicable_given_name_hints(
                _row("やよい", key=konomi.typed_record_key), document
            ),
            (),
        )
        for entry in document.entries:
            self.assertEqual(
                [item.source_form for item in applicable_given_name_hints(
                    _row(entry.source_form, key=f"ld_idol_firstname_{entry.idol_id}"),
                    document,
                )],
                [entry.source_form],
            )

    def test_loader_rejects_duplicate_unknown_and_malformed_documents(self):
        base = json.loads(HINTS_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hints.json"

            def assert_rejected(doc_or_text) -> None:
                text = (
                    doc_or_text
                    if isinstance(doc_or_text, str)
                    else json.dumps(doc_or_text, ensure_ascii=False)
                )
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_given_name_hints(path)

            assert_rejected('{"schema_version": 1, "schema_version": 1}')
            unknown = json.loads(json.dumps(base))
            unknown["entries"][0]["persona"] = "invented"
            assert_rejected(unknown)
            duplicate = json.loads(json.dumps(base))
            duplicate["entries"].append(dict(duplicate["entries"][0]))
            assert_rejected(duplicate)
            contradictory = json.loads(json.dumps(base))
            contradictory["entries"][0]["target"] = "错误"
            assert_rejected(contradictory)
            incomplete = json.loads(json.dumps(base))
            del incomplete["entries"][0]["typed_record_key"]
            assert_rejected(incomplete)

    def test_explicit_loader_reloads_but_default_cache_is_readonly(self):
        base = json.loads(HINTS_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hints.json"
            path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
            first = load_given_name_hints(path)
            cached_first = load_default_given_name_hints(path)

            base["entries"][0]["evidence"].append("fixture reload evidence")
            path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")

            second = load_given_name_hints(path)
            cached_second = load_default_given_name_hints(path)
            self.assertNotEqual(first.entries[0].evidence, second.entries[0].evidence)
            self.assertIs(cached_first, cached_second)
            self.assertNotEqual(cached_second.entries[0].evidence, second.entries[0].evidence)

    def test_prompt_places_hash_block_before_source(self):
        document = load_given_name_hints(HINTS_PATH)
        row = _row("このみさん、何か悩んでるんですか？")
        prompt = build_user_prompt(
            row,
            row["source"],
            style="legacy",
            given_name_hints=document,
        )
        self.assertIn("このみ => 木实", prompt)
        self.assertIn(f"NAME_HINTS_AUTHORITY_SHA256={document.sha256}", prompt)
        self.assertLess(prompt.index("NAME_HINTS"), prompt.index("SOURCE:"))
        block = render_given_name_hints(row, document)
        self.assertEqual(block.count("このみ => 木实"), 1)

    def test_cli_defaults_to_committed_hint_file(self):
        args = build_parser().parse_args(["--config", "models.json"])
        self.assertEqual(args.given_name_hints, Path("glossary/given-name-hints.json"))

    def test_mock_transport_receives_hint_and_preserves_protected_token(self):
        document = load_given_name_hints(HINTS_PATH)
        requests = []

        def handle(request):
            body = json.loads(request.content)
            user_prompt = body["messages"][-1]["content"]
            requests.append(user_prompt)
            translation = (
                "木实小姐，也请告诉__MLTD_TOKEN_000__。"
                if "このみ" in user_prompt
                else "梦想着光明的未来舞台。"
            )
            return httpx.Response(
                200,
                json={
                    "model": "reported-backend",
                    "choices": [{"message": {"content": translation}}],
                    "usage": {"total_tokens": 10},
                },
            )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                stats = defaultdict(lambda: defaultdict(float))
                hinted, error = await translate_with_model(
                    client,
                    _config(),
                    None,
                    "SYSTEM",
                    _row("このみさん、{$P$}にも伝えてください。"),
                    {},
                    {},
                    stats,
                    given_name_hints=document,
                )
                self.assertEqual(error, "")
                self.assertEqual(hinted["translation"], "木实小姐，也请告诉{$P$}。")

                ordinary, error = await translate_with_model(
                    client,
                    _config(),
                    None,
                    "SYSTEM",
                    _row("明るい未来の舞台を夢見ている。"),
                    {},
                    {},
                    stats,
                    given_name_hints=document,
                )
                self.assertEqual(error, "")
                self.assertEqual(ordinary["translation"], "梦想着光明的未来舞台。")

        asyncio.run(run())
        self.assertEqual(len(requests), 2)
        self.assertIn("NAME_HINTS", requests[0])
        self.assertIn("このみ => 木实", requests[0])
        self.assertIn("__MLTD_TOKEN_000__", requests[0])
        self.assertNotIn("{$P$}", requests[0])
        self.assertNotIn("NAME_HINTS", requests[1])
        self.assertNotIn("このみ => 木实", requests[1])


if __name__ == "__main__":
    unittest.main()
