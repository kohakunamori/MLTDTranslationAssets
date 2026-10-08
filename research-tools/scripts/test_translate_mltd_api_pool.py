#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import httpx

from scripts.mltd_translation_prompt import compile_prompt_bundle, snapshot_status
from scripts.repair_mltd_stale_deterministic import deterministic_normalize
from scripts.revalidate_mltd_api_output import load_existing_stale, stale_reason_signature
from scripts.translate_gtx_queue import mask_tokens, restore_tokens
from scripts.translate_mltd_api_pool import (
    APIRequestError,
    AsyncRequestRateLimiter,
    ModelConfig,
    WorkItem,
    adaptive_retry_params,
    api_error_is_quota_exhausted,
    build_parser,
    build_user_prompt,
    load_model_config,
    mask_authoritative_terms,
    mask_numeric_literals,
    normalize_endpoint,
    normalize_response_usage,
    request_translation,
    resolve_system_prompt,
    response_request_params,
    select_models,
    restore_authoritative_terms,
    restore_numeric_literals,
    retry_delay_seconds,
    should_retry_transport_error,
    source_id,
    task_request_params_for_row,
    validate_candidate,
    worker,
)


def make_config(protocol: str, *, request_params: dict | None = None) -> ModelConfig:
    return ModelConfig(
        id="mock",
        model="mock-model",
        api_protocol=protocol,
        endpoint=f"https://example.invalid/{'responses' if protocol == 'responses' else 'chat/completions'}",
        api_key_env="",
        concurrency=1,
        reasoning_effort="medium",
        temperature=0.1,
        timeout=10.0,
        retries=2,
        request_params=dict(request_params or {}),
        request_params_by_task={},
        request_headers={},
        cache_anchor="",
        cache_warmup=False,
        cache_partition_by_reasoning=False,
    )


class TranslatorAdapterTests(unittest.TestCase):
    def test_openrouter_endpoint_normalization(self) -> None:
        self.assertEqual(
            normalize_endpoint(
                {"base_url": "https://openrouter.ai/v1/chat/completions"},
                "responses",
            ),
            "https://openrouter.ai/api/v1/responses",
        )
        self.assertEqual(
            normalize_endpoint(
                {"base_url": "https://openrouter.ai/api/v1"},
                "chat_completions",
            ),
            "https://openrouter.ai/api/v1/chat/completions",
        )

    def test_gemini_openai_compatible_endpoint(self) -> None:
        self.assertEqual(
            normalize_endpoint(
                {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"},
                "chat_completions",
            ),
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )
        self.assertEqual(
            normalize_endpoint(
                {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"},
                "chat_completions",
            ),
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )

    def test_usage_normalization_for_both_protocol_shapes(self) -> None:
        responses = normalize_response_usage(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 64},
                "output_tokens_details": {"reasoning_tokens": 7},
            }
        )
        self.assertEqual(responses["cached_tokens"], 64)
        self.assertEqual(responses["prompt_cache_miss_tokens"], 36)
        self.assertEqual(responses["reasoning_tokens"], 7)

        chat = normalize_response_usage(
            {
                "prompt_tokens": 80,
                "completion_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 32},
                "completion_tokens_details": {"reasoning_tokens": 5},
            }
        )
        self.assertEqual(chat["cached_tokens"], 32)
        self.assertEqual(chat["prompt_cache_miss_tokens"], 48)
        self.assertEqual(chat["reasoning_tokens"], 5)

    def test_responses_parameter_parity_and_overrides(self) -> None:
        cfg = make_config(
            "responses",
            request_params={
                "session_id": "stable-session",
                "prompt_cache_key": "fixed-system",
            },
        )
        params = response_request_params(
            cfg,
            {"reasoning_effort": "low", "max_tokens": 321},
        )
        self.assertEqual(params["temperature"], 0.1)
        self.assertEqual(params["session_id"], "stable-session")
        self.assertEqual(params["prompt_cache_key"], "fixed-system")
        self.assertEqual(params["reasoning"], {"effort": "low"})
        self.assertEqual(params["max_output_tokens"], 321)

    def test_marker_restore_and_empty_retry(self) -> None:
        masked, targets, applied = mask_authoritative_terms(
            "ジュリアは{0}個", {"ジュリア": "茱莉亚"}
        )
        self.assertEqual(applied, ["ジュリア"])
        token_masked, tokens = mask_tokens(masked)
        rendered = token_masked.replace("は", "有")
        rendered = restore_authoritative_terms(rendered, targets)
        rendered = restore_tokens(rendered, tokens)
        self.assertIn("茱莉亚", rendered)
        self.assertIn("{0}", rendered)

        retried = adaptive_retry_params(
            {"reasoning_effort": "low", "max_tokens": 1024},
            "high",
            "empty_translation",
        )
        self.assertEqual(retried["reasoning_effort"], "none")
        self.assertEqual(retried["max_tokens"], 2048)

    def test_dialogue_prompt_uses_speaker_code_not_unreliable_name(self) -> None:
        row = {
            "source": "テスト",
            "usage_profile": {
                "speaker_codes": ["001har"],
                "speaker_names": ["エミリースチュアート"],
            },
        }
        prompt = build_user_prompt(row, "テスト")
        self.assertIn("SPEAKER=001har", prompt)
        self.assertNotIn("エミリースチュアート", prompt)

    def test_repair_prompt_includes_previous_candidate_and_failure_reason(self) -> None:
        row = {
            "source": "そー・ぷれじゃー！なのです♪",
            "previous_translation": "超级·快乐！なのです♪",
            "stale_reasons": [
                {
                    "code": "japanese_grammar_residual",
                    "detail": "residual=なのです♪",
                }
            ],
        }
        prompt = build_user_prompt(row, "そー・ぷれじゃー！なのです♪")
        self.assertIn("PREVIOUS_TRANSLATION_TO_REPAIR:", prompt)
        self.assertIn("超级·快乐！なのです♪", prompt)
        self.assertIn("japanese_grammar_residual: residual=なのです♪", prompt)
        self.assertIn("Translate SOURCE from scratch", prompt)

    def test_japanese_grammar_residual_is_blocking_without_banning_titles(self) -> None:
        from scripts.mltd_translation_quality import evaluate_row
        from scripts.translate_gtx_queue import BASIC_BLOCKING_QA_CODES

        glossary = {"entries": {}, "kana_allowlist": []}
        bad = (
            ("自分がついてるから、なんくるないさー！", "有自己在，总会有办法的さー！"),
            ("そー・ぷれじゃー！なのです♪", "超级·快乐！なのです♪"),
            ("お祝いしましょー！", "一起庆祝吧ー！"),
            ("甲斐がありましたっ！", "这份报告没有白写っ！"),
        )
        for source, translation in bad:
            qa = evaluate_row(
                {"source_sha256": source_id(source), "source": source},
                {"translation": translation},
                glossary,
            )
            codes = {issue["code"] for issue in qa["issues"]}
            self.assertIn("japanese_grammar_residual", codes, (source, translation, qa))
        self.assertIn("japanese_grammar_residual", BASIC_BLOCKING_QA_CODES)

        # An unchanged proper title remains reviewable rather than being hard
        # rejected merely because its official/unlocalized form contains kana.
        title = "なんくるないさー！"
        qa = evaluate_row(
            {"source_sha256": source_id(title), "source": title},
            {"translation": title},
            glossary,
        )
        codes = {issue["code"] for issue in qa["issues"]}
        self.assertNotIn("japanese_grammar_residual", codes)
        self.assertIn("unchanged_translation", codes)

        # A preserved proper title may contain the same character sequence as a
        # Japanese sentence particle (だよ inside だより); do not substring-match it.
        source = "14thLIVE DAY1「織々の花だより」応援セットにて獲得"
        translation = "通过14thLIVE DAY1「織々の花だより」应援套装获得"
        qa = evaluate_row(
            {"source_sha256": source_id(source), "source": source},
            {"translation": translation},
            glossary,
        )
        codes = {issue["code"] for issue in qa["issues"]}
        self.assertNotIn("japanese_grammar_residual", codes)

    def test_sentence_final_de_shuo_is_blocking_but_normal_shuofa_is_not(self) -> None:
        from scripts.mltd_translation_quality import evaluate_row
        from scripts.translate_gtx_queue import BASIC_BLOCKING_QA_CODES

        glossary = {"entries": {}, "kana_allowlist": []}
        bad_cases = (
            ("初夢なのです！", "这是新年第一个美梦的说！"),
            ("困ったのです…", "好苦恼的说…"),
            ("みんなであそびたいのに", "明明想和大家一起玩的说"),
            ("ぷんすかなのです\\18\\", "气呼呼的说\\18\\"),
            ("メイクス、ハピネス、なのです☆", "Makes、Happiness、的说☆"),
        )
        for source, translation in bad_cases:
            qa = evaluate_row(
                {"source_sha256": source_id(source), "source": source},
                {"translation": translation},
                glossary,
            )
            codes = {issue["code"] for issue in qa["issues"]}
            self.assertIn("sentence_final_de_shuo_calque", codes, (source, translation, qa))
        self.assertIn("sentence_final_de_shuo_calque", BASIC_BLOCKING_QA_CODES)

        good = evaluate_row(
            {"source_sha256": source_id("そういう言い方"), "source": "そういう言い方"},
            {"translation": "这样的说法"},
            glossary,
        )
        self.assertNotIn(
            "sentence_final_de_shuo_calque",
            {issue["code"] for issue in good["issues"]},
        )

    def test_rank_glossary_rejects_literal_agent_extension(self) -> None:
        from scripts.mltd_translation_quality import evaluate_row
        from scripts.translate_gtx_queue import BASIC_BLOCKING_QA_CODES

        glossary = {
            "entries": {
                "ゴールドランカー": {
                    "preferred": "黄金排名",
                    "forbidden": ["黄金排名者"],
                }
            },
            "kana_allowlist": [],
        }
        source = "M@STERPIECE ゴールドランカー"
        qa = evaluate_row(
            {"source_sha256": source_id(source), "source": source},
            {"translation": "M@STERPIECE 黄金排名者"},
            glossary,
        )
        codes = {issue["code"] for issue in qa["issues"]}
        self.assertIn("forbidden_term_present", codes)
        self.assertIn("forbidden_term_present", BASIC_BLOCKING_QA_CODES)

    def test_authoritative_revalidation_matches_non_overlapping_mask_selection(self) -> None:
        from scripts.revalidate_mltd_api_output import authoritative_issues

        terms = {
            "茜ちゃん": "小茜",
            "野々原茜": "野野原茜",
        }
        # Both source terms overlap. Production masking selects 茜ちゃん first
        # (same length, insertion order), so revalidation must not also require
        # the overlapping full-name target.
        self.assertEqual(
            authoritative_issues("野々原茜ちゃんです", "野野原小茜来啦", terms),
            [],
        )

    def test_authoritative_anniversary_stale_repair_is_narrow(self) -> None:
        from scripts.repair_mltd_authoritative_stale import repair_anniversary_translation

        self.assertEqual(
            repair_anniversary_translation(
                "5thアニバーサリー！\nまだまだこれから！",
                "5th周年！\n接下来才刚开始！",
                "5thアニバーサリー",
                "5周年纪念",
            ),
            "5周年纪念！\n接下来才刚开始！",
        )
        self.assertEqual(
            repair_anniversary_translation(
                "6thアニバーサリー！\nそー・ぷれじゃー！",
                "6th周年庆！\n超级开心！",
                "6thアニバーサリー",
                "6周年纪念",
            ),
            "6周年纪念！\n超级开心！",
        )
        self.assertIsNone(
            repair_anniversary_translation(
                "普通文本", "5th周年！", "5thアニバーサリー", "5周年纪念"
            )
        )

    def test_ranking_label_repair_is_source_scoped_and_preserves_numbers(self) -> None:
        from scripts.repair_mltd_ranking_labels import repair_translation

        cases = [
            (
                "Song ハイスコア ランキング5001位～10000位入賞",
                "歌曲 高分榜 排名第5001位～10000位获奖",
                "最高分排行榜",
            ),
            (
                "Song イベント ランキング101位～2500位入賞",
                "歌曲 活动排名荣获第101名～第2500名",
                "活动排行榜",
            ),
            (
                "Song ラウンジ ランキング51位～100位入賞",
                "歌曲 公会排名第51～100名",
                "社交厅排行榜",
            ),
            (
                "Song イベント ランキング5001位～10000位入賞",
                "歌曲 活动排行榜第5001～10000名获奖",
                "活动排行榜",
            ),
            (
                "M@STERPIECE ゴールドランカー",
                "M@STERPIECE 黄金排名者",
                "黄金排名",
            ),
        ]
        for source, before, target in cases:
            proposal = repair_translation(source, before)
            self.assertIsNotNone(proposal)
            after, _ = proposal
            self.assertIn(target, after)
            for number in re.findall(r"\d+", source):
                self.assertIn(number, after)
        self.assertIsNone(repair_translation("普通标题", "高分榜第1名"))

    def test_japanese_honorific_residual_is_blocking(self) -> None:
        source = "茜ちゃん、がんばろう！"
        row = {"source_sha256": source_id(source), "source": source}
        candidate, error = validate_candidate(
            row,
            "茜ちゃん，一起加油吧！",
            [],
            [],
            {"entries": {}, "kana_allowlist": []},
        )
        self.assertIsNone(candidate)
        self.assertIn("japanese_honorific_residual", error)

    def test_chan_to_jiang_calque_is_blocking_but_food_sauce_is_not(self) -> None:
        glossary = {"entries": {}, "kana_allowlist": []}
        source = "未来ちゃん、行こう！"
        row = {"source_sha256": source_id(source), "source": source}
        candidate, error = validate_candidate(row, "未来酱，走吧！", [], [], glossary)
        self.assertIsNone(candidate)
        self.assertIn("honorific_chan_jiang_calque", error)

        sauce_source = "茜ちゃん、そのジャム、ちゃんと持って帰ってね。"
        sauce_row = {"source_sha256": source_id(sauce_source), "source": sauce_source}
        candidate, error = validate_candidate(
            sauce_row,
            "小茜，那个果酱记得带回去哦。",
            [],
            [],
            glossary,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(error, "")

    def test_numeric_literals_are_masked_and_restored_without_touching_markers(self) -> None:
        source = "Cloverは4人で1つ。最大100着 __MLTD_TERM_000__ __MLTD_TOKEN_001__"
        masked, values = mask_numeric_literals(source)
        self.assertEqual(values, ["4", "1", "100"])
        self.assertIn("__MLTD_NUMBER_000__", masked)
        self.assertIn("__MLTD_NUMBER_001__", masked)
        self.assertIn("__MLTD_NUMBER_002__", masked)
        self.assertIn("__MLTD_TERM_000__", masked)
        self.assertIn("__MLTD_TOKEN_001__", masked)
        self.assertEqual(restore_numeric_literals(masked, values), source)

    def test_deterministic_stale_repair_handles_known_mechanical_quality_failures(self) -> None:
        terms = {
            "アナザー衣装": "异色服装",
            "キミさけ": "キミさけ",
            "茜ちゃん": "小茜",
        }
        costume = {
            "source": "『衣装A』のアナザー衣装です。",
            "translation": "这是《服装A》的Another服装。",
            "stale_reasons": [
                {
                    "code": "authoritative_term_missing",
                    "source_term": "アナザー衣装",
                    "target": "异色服装",
                }
            ],
        }
        after, repairs = deterministic_normalize(costume, terms)
        self.assertEqual(after, "这是《服装A》的异色服装。")
        self.assertIn("authoritative:アナザー衣装", repairs)

        brand = {
            "source": "『キミさけ』の話だよ。",
            "translation": "是在说《KimiSake》哦。",
            "stale_reasons": [
                {
                    "code": "authoritative_term_missing",
                    "source_term": "キミさけ",
                    "target": "キミさけ",
                }
            ],
        }
        after, _ = deterministic_normalize(brand, terms)
        self.assertEqual(after, "是在说《キミさけ》哦。")

        nickname = {
            "source": "茜ちゃんだよ！",
            "translation": "茜酱来啦！",
            "stale_reasons": [
                {
                    "code": "authoritative_term_missing",
                    "source_term": "茜ちゃん",
                    "target": "小茜",
                },
                {"code": "honorific_chan_jiang_calque"},
            ],
        }
        after, _ = deterministic_normalize(nickname, terms)
        self.assertEqual(after, "小茜来啦！")

    def test_stale_evidence_tracks_latest_reason_signature_per_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stale.jsonl"
            rows = [
                {
                    "source_sha256": "abc",
                    "stale_reasons": [{"code": "old_rule", "severity": "reject"}],
                },
                {
                    "source_sha256": "abc",
                    "stale_reasons": [
                        {"severity": "reject", "code": "new_rule"},
                        {"code": "second_rule"},
                    ],
                },
            ]
            path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            latest = load_existing_stale(path)
        self.assertEqual(
            latest["abc"],
            stale_reason_signature(rows[-1]["stale_reasons"]),
        )
        self.assertNotEqual(
            latest["abc"],
            stale_reason_signature(rows[0]["stale_reasons"]),
        )

    def test_contextual_title_gets_low_reasoning_without_global_title_cost(self) -> None:
        cfg = replace(
            make_config("responses"),
            request_params_by_task={
                "TITLE": {"reasoning_effort": "none", "max_tokens": 256}
            },
        )
        plain = {
            "source": "普通タイトル",
            "examples": [{"key": "event_x_title"}],
            "usage_profile": {},
        }
        task, params = task_request_params_for_row(cfg, plain)
        self.assertEqual(task, "TITLE")
        self.assertEqual(params["reasoning_effort"], "none")
        self.assertEqual(params["max_tokens"], 256)

        contextual = {
            "source": "そればかりでは……",
            "examples": [{"key": "event_x_title"}],
            "usage_profile": {},
            "context_examples": [
                {
                    "context": [
                        {"relative": -1, "source": "前の章"},
                        {"relative": 0, "source": "そればかりでは……"},
                        {"relative": 1, "source": "次の章"},
                    ]
                }
            ],
        }
        task, params = task_request_params_for_row(cfg, contextual)
        self.assertEqual(task, "TITLE")
        self.assertEqual(params["reasoning_effort"], "low")
        self.assertEqual(params["max_tokens"], 512)

    def test_model_id_selection_reuses_one_multi_model_config(self) -> None:
        models = [
            replace(make_config("responses"), id="gemini", model="gemini-3.8-flash-high"),
            replace(make_config("responses"), id="luna", model="gpt-5.6-luna"),
        ]
        self.assertEqual([cfg.id for cfg in select_models(models, [])], ["gemini", "luna"])
        self.assertEqual([cfg.id for cfg in select_models(models, ["luna"])], ["luna"])
        self.assertEqual(
            [cfg.id for cfg in select_models(models, ["luna", "gemini"])],
            ["gemini", "luna"],
        )
        with self.assertRaisesRegex(ValueError, "unknown --model-id"):
            select_models(models, ["missing"])

    def test_model_preset_keeps_local_config_small_and_overridable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "presets.json").write_text(
                json.dumps(
                    {
                        "presets": {
                            "balanced": {
                                "reasoning_effort": "high",
                                "temperature": 0.1,
                                "timeout": 180,
                                "retries": 2,
                                "cache_warmup": False,
                                "request_params_by_task": {
                                    "UI": {"reasoning_effort": "none", "max_tokens": 256}
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = root / "local.json"
            config.write_text(
                json.dumps(
                    {
                        "_comment": "ignored documentation",
                        "preset_file": "presets.json",
                        "models": [
                            {
                                "preset": "balanced",
                                "id": "primary",
                                "base_url": "https://example.invalid/v1",
                                "api_protocol": "responses",
                                "model": "gemini-test",
                                "concurrency": 7,
                                "timeout": 99,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_model_config(config)

        self.assertEqual(len(loaded), 1)
        cfg = loaded[0]
        self.assertEqual(cfg.model, "gemini-test")
        self.assertEqual(cfg.endpoint, "https://example.invalid/v1/responses")
        self.assertEqual(cfg.concurrency, 7)
        self.assertEqual(cfg.reasoning_effort, "high")
        self.assertEqual(cfg.temperature, 0.1)
        self.assertEqual(cfg.timeout, 99.0)
        self.assertFalse(cfg.cache_warmup)
        self.assertEqual(
            cfg.request_params_by_task["UI"],
            {"reasoning_effort": "none", "max_tokens": 256},
        )

    def test_schema3_defaults_and_model_map_keep_multi_model_config_compact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "api-models.presets.json").write_text(
                json.dumps(
                    {
                        "presets": {
                            "mltd-balanced": {
                                "reasoning_effort": "high",
                                "temperature": 0.1,
                                "timeout": 180,
                                "retries": 2,
                                "cache_warmup": False,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = root / "api-models.local.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "defaults": {
                            "preset": "mltd-balanced",
                            "base_url": "https://gateway.invalid/v1",
                            "api_protocol": "responses",
                            "api_key_env": "MLTD_API_KEY",
                            "concurrency": 10,
                            "request_params": {"gateway_hint": "shared"},
                        },
                        "models": {
                            "gemini": "gemini-3.8-flash-high",
                            "second": {
                                "model": "second-model",
                                "concurrency": 3,
                                "request_params": {},
                            },
                            "disabled": {
                                "model": "disabled-model",
                                "enabled": False,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_model_config(config)

        self.assertEqual([cfg.id for cfg in loaded], ["gemini", "second"])
        self.assertEqual([cfg.model for cfg in loaded], ["gemini-3.8-flash-high", "second-model"])
        self.assertEqual([cfg.concurrency for cfg in loaded], [10, 3])
        self.assertTrue(all(cfg.endpoint == "https://gateway.invalid/v1/responses" for cfg in loaded))
        self.assertTrue(all(cfg.reasoning_effort == "high" for cfg in loaded))
        self.assertTrue(all(cfg.cache_warmup is False for cfg in loaded))
        self.assertEqual(loaded[0].request_params, {"gateway_hint": "shared"})
        self.assertEqual(loaded[1].request_params, {})

    def test_runtime_local_config_supplies_machine_secret_without_polluting_model_config(self) -> None:
        env_name = "MLTD_TEST_RUNTIME_KEY"
        old = os.environ.pop(env_name, None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "api-runtime.local.json").write_text(
                    json.dumps({"environment": {"api_key": "runtime-secret-test"}}),
                    encoding="utf-8",
                )
                config = root / "api-models.local.json"
                config.write_text(
                    json.dumps(
                        {
                            "schema_version": 3,
                            "defaults": {
                                "base_url": "https://gateway.invalid/v1",
                                "api_protocol": "responses",
                                "api_key_env": env_name,
                                "concurrency": 1,
                            },
                            "models": {"one": "model-one"},
                        }
                    ),
                    encoding="utf-8",
                )
                loaded = load_model_config(config)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(os.environ.get(env_name), "runtime-secret-test")
        finally:
            os.environ.pop(env_name, None)
            if old is not None:
                os.environ[env_name] = old

    def test_cli_can_disable_default_resume_for_isolated_benchmarks(self) -> None:
        parser = build_parser()
        normal = parser.parse_args(["--config", "local.json"])
        isolated = parser.parse_args(
            [
                "--config",
                "local.json",
                "--no-default-resume",
                "--resume-from",
                "custom.jsonl",
            ]
        )
        self.assertEqual(normal.resume_from, [])
        self.assertFalse(normal.no_default_resume)
        self.assertTrue(isolated.no_default_resume)
        self.assertEqual(isolated.resume_from, [Path("custom.jsonl")])

    def test_status_aware_backoff(self) -> None:
        self.assertEqual(retry_delay_seconds(APIRequestError(429, "", "x"), 1), 5.0)
        self.assertEqual(retry_delay_seconds(APIRequestError(429, "", "x"), 2), 10.0)
        self.assertEqual(retry_delay_seconds(APIRequestError(503, "", "x"), 1), 2.0)
        self.assertEqual(retry_delay_seconds(APIRequestError(429, "12", "x"), 1), 12.0)

    def test_retry_classification_distinguishes_capacity_and_daily_quota(self) -> None:
        daily = APIRequestError(
            429,
            "",
            '{"limit_source":"openrouter_free_tier_daily","message":"free-models-per-day-stealth"}',
        )
        shared = APIRequestError(
            429, "", '{"limit_source":"openrouter_shared_capacity"}'
        )
        auth = APIRequestError(401, "", "bad key")
        self.assertTrue(api_error_is_quota_exhausted(daily))
        self.assertFalse(should_retry_transport_error(daily))
        self.assertFalse(api_error_is_quota_exhausted(shared))
        self.assertTrue(should_retry_transport_error(shared))
        self.assertFalse(should_retry_transport_error(auth))


class TranslatorHttpMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limiter_is_shared_by_request_starts(self) -> None:
        limiter = AsyncRequestRateLimiter(6000)
        loop = __import__("asyncio").get_running_loop()
        started = loop.time()
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()
        self.assertGreaterEqual(loop.time() - started, 0.018)

    async def test_daily_quota_circuit_breaker_defers_without_more_requests(self) -> None:
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "free-models-per-day-stealth",
                        "metadata": {"limit_source": "openrouter_free_tier_daily"},
                    }
                },
            )

        cfg = make_config("chat_completions")
        work_queue: __import__("asyncio").Queue = __import__("asyncio").Queue()
        result_queue: __import__("asyncio").Queue = __import__("asyncio").Queue()
        for index in range(3):
            source = f"これはテストです{index}"
            await work_queue.put(
                WorkItem(row={"source_sha256": source_id(source), "source": source})
            )
        await work_queue.put(None)
        disabled: set[str] = set()
        stats = defaultdict(lambda: defaultdict(float))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker(
                cfg,
                client,
                None,
                work_queue,
                result_queue,
                "SYSTEM",
                {"entries": {}, "kana_allowlist": []},
                {},
                {cfg.id},
                disabled,
                stats,
            )

        results = [await result_queue.get() for _ in range(result_queue.qsize())]
        self.assertEqual(requests, 1)
        self.assertEqual(disabled, {cfg.id})
        self.assertEqual([kind for kind, _ in results], ["deferred"] * 3)
        self.assertEqual(stats[cfg.id]["quota_circuit_breaker_trips"], 1)

    async def test_quota_breaker_stops_workers_waiting_behind_rate_limiter(self) -> None:
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "free-models-per-day-stealth",
                        "metadata": {"limit_source": "openrouter_free_tier_daily"},
                    }
                },
            )

        cfg = make_config("chat_completions")
        work_queue: __import__("asyncio").Queue = __import__("asyncio").Queue()
        result_queue: __import__("asyncio").Queue = __import__("asyncio").Queue()
        worker_count = 8
        for index in range(worker_count):
            source = f"これは並列テストです{index}"
            await work_queue.put(
                WorkItem(row={"source_sha256": source_id(source), "source": source})
            )
        for _ in range(worker_count):
            await work_queue.put(None)
        disabled: set[str] = set()
        stats = defaultdict(lambda: defaultdict(float))
        limiter = AsyncRequestRateLimiter(6000)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await __import__("asyncio").gather(
                *[
                    worker(
                        cfg,
                        client,
                        limiter,
                        work_queue,
                        result_queue,
                        "SYSTEM",
                        {"entries": {}, "kana_allowlist": []},
                        {},
                        {cfg.id},
                        disabled,
                        stats,
                    )
                    for _ in range(worker_count)
                ]
            )

        results = [await result_queue.get() for _ in range(result_queue.qsize())]
        self.assertEqual(requests, 1)
        self.assertEqual(disabled, {cfg.id})
        self.assertEqual(len(results), worker_count)
        self.assertEqual([kind for kind, _ in results], ["deferred"] * worker_count)
        self.assertEqual(stats[cfg.id]["requests"], 1)
        self.assertEqual(stats[cfg.id]["quota_circuit_breaker_trips"], 1)

    async def test_responses_request_and_usage(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "译文"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "input_tokens_details": {"cached_tokens": 64},
                        "output_tokens_details": {"reasoning_tokens": 7},
                    },
                },
            )

        cfg = make_config(
            "responses",
            request_params={"session_id": "s1", "prompt_cache_key": "p1"},
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            text, meta = await request_translation(
                client,
                cfg,
                "FIXED SYSTEM",
                "TASK=UI\nSOURCE=確認",
                {"reasoning_effort": "low", "max_tokens": 123},
            )

        self.assertEqual(text, "译文")
        self.assertEqual(len(captured), 1)
        body = captured[0]
        self.assertEqual(body["instructions"], "FIXED SYSTEM")
        self.assertEqual(body["input"], "TASK=UI\nSOURCE=確認")
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["max_output_tokens"], 123)
        self.assertEqual(body["session_id"], "s1")
        self.assertEqual(meta["usage"]["cached_tokens"], 64)
        self.assertEqual(meta["usage"]["reasoning_tokens"], 7)

    async def test_chat_request_and_usage(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "译文"}}],
                    "usage": {
                        "prompt_tokens": 80,
                        "completion_tokens": 12,
                        "prompt_tokens_details": {"cached_tokens": 32},
                    },
                },
            )

        cfg = make_config("chat_completions", request_params={"session_id": "s1"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            text, meta = await request_translation(
                client,
                cfg,
                "FIXED SYSTEM",
                "TASK=UI\nSOURCE=確認",
                {"reasoning_effort": "low", "max_tokens": 123},
            )

        self.assertEqual(text, "译文")
        body = captured[0]
        self.assertEqual(body["messages"][0], {"role": "system", "content": "FIXED SYSTEM"})
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["max_tokens"], 123)
        self.assertEqual(body["session_id"], "s1")
        self.assertEqual(meta["usage"]["cached_tokens"], 32)

    async def test_responses_cache_hint_fallback(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured.append(body)
            if len(captured) == 1:
                return httpx.Response(400, json={"error": "unsupported cache hint"})
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output_text": "译文",
                    "usage": {"input_tokens": 20, "output_tokens": 2},
                },
            )

        cfg = make_config(
            "responses",
            request_params={
                "session_id": "sticky",
                "prompt_cache_key": "cache-key",
                "prompt_cache_options": {"ttl": "30m"},
            },
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            text, meta = await request_translation(
                client, cfg, "SYSTEM", "SOURCE", {"reasoning_effort": "none"}
            )

        self.assertEqual(text, "译文")
        self.assertTrue(meta["cache_hint_fallback"])
        self.assertEqual(len(captured), 2)
        self.assertIn("prompt_cache_key", captured[0])
        self.assertNotIn("prompt_cache_key", captured[1])
        self.assertNotIn("prompt_cache_options", captured[1])
        self.assertEqual(captured[1]["session_id"], "sticky")


class PromptCompilerTests(unittest.TestCase):
    def test_canonical_snapshot_matches_shared_compiler(self) -> None:
        prompt_path = Path("localization/prompts/mltd-zhcn-system.md")
        manifest_path = Path("localization/prompts/mltd-zhcn-system.manifest.json")
        bundle = compile_prompt_bundle(
            Path("localization/quality/glossary.json"),
            Path("build/localization-90200/character-voice-evidence.json"),
            output_path=prompt_path,
        )
        self.assertEqual(prompt_path.read_text(encoding="utf-8"), bundle.text)
        self.assertEqual(snapshot_status(bundle, prompt_path, manifest_path)["status"], "current")
        self.assertEqual(bundle.manifest["schema_version"], 3)
        self.assertTrue(bundle.manifest["policy_sha256"])

    def test_translator_defaults_to_compiled_prompt_and_can_replay_artifact(self) -> None:
        parser = build_parser()
        compiled_args = parser.parse_args(["--config", "localization/api-models.local.json", "--dry-run"])
        self.assertEqual(compiled_args.prompt_source, "compiled")
        compiled = resolve_system_prompt(compiled_args)
        self.assertEqual(compiled[3]["status"], "current")

        artifact_args = parser.parse_args([
            "--config", "localization/api-models.local.json",
            "--prompt-source", "artifact",
            "--dry-run",
        ])
        artifact = resolve_system_prompt(artifact_args)
        self.assertEqual(artifact[3]["status"], "artifact")
        self.assertEqual(compiled[0], artifact[0])
        self.assertEqual(compiled[2], artifact[2])


if __name__ == "__main__":
    unittest.main()
