#!/usr/bin/env python3
"""Tests for the strict JP 9.0.200 / 1077100 event-unit write-back materializer.

Test ledgers are labelled ``synthetic-test-only`` and are NEVER release
evidence: the write-back path is exercised against real frozen bundles copied
into a pytest tmp directory exactly as the production command would, but the
translations themselves are manufactured here.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from scripts import materialize_event_unit_90200 as mod
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id

SOURCE_TEXT_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")
KANA_RE = re.compile(r"[぀-ゟ゠-ヿ]")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def review_row(sid: str, source: str, translation: str, *, gate: str = "accepted",
               reviewer: str | None = "rev-synthetic") -> dict:
    row = {
        "source_sha256": sid,
        "source": source,
        "translation": translation,
        "status": "machine_translated",
        "provenance": {"provider": "synthetic", "model_id": "test-model", "model": "test-model"},
        "release_gate": gate,
        "qa_verdict": "PASS",
    }
    if reviewer is not None:
        row["review"] = {
            "source_sha256": sid, "source": source, "translation": translation,
            "verdict": "PASS",
            "scores": {"semantic_accuracy": 5, "terminology": 5, "fluency": 5,
                       "character_voice": 5, "context_consistency": 5},
            "blocking_errors": [], "reviewer_id": reviewer,
        }
    return row


@pytest.fixture(scope="module")
def frozen():
    """Real frozen cohort, queue and glossary; absent artifacts skip the module."""
    for path in (mod.INDEX, mod.SNAPSHOT, mod.COHORT, mod.AUDIT, mod.QUEUE,
                 mod.GLOSSARY, mod.SOURCE_ROOT):
        if not path.exists():
            pytest.skip(f"frozen event-unit artifact missing: {path}")
    if mod.sha_file(mod.COHORT) != mod.COHORT_SHA:
        pytest.skip("frozen event-unit cohort index is not the audited file")
    cohort = mod.read_frozen_cohort()
    queue = mod.read_frozen_queue()
    glossary = load_glossary(mod.GLOSSARY if mod.GLOSSARY.is_file() else None)
    return {"cohort": cohort, "queue": queue, "glossary": glossary}


def bundle_document(remote: str) -> dict:
    import UnityPy

    env = UnityPy.load(str(mod.SOURCE_ROOT / remote))
    obj = [o for o in env.objects if o.type.name == "TextAsset"][0]
    return json.loads(bytes(obj.read().m_Script).decode("utf-8-sig"))


def translatable_entries(document: dict) -> list[tuple[int, str]]:
    return [
        (index, command["text"])
        for index, command in enumerate(document["commands"])
        if command["text"] and SOURCE_TEXT_RE.search(command["text"])
    ]


def synthetic_ledger(entries: list[tuple[int, str]]) -> list[dict]:
    """Manufacture QA-PASS, reviewer-bound rows for every translatable command.

    Existing tests still use this helper, so it stays permissive.  It writes the
    summary ledger for the complete-cohort test below; the real write-back tests
    use :func:`strict_synthetic_ledger`, which mirrors the production quantity
    gate instead of leniently returning "no rows".
    """
    rows = []
    for _index, text in entries:
        sid = source_id(text)
        translation = text.replace("……", "…") if "……" in text else text + "。"
        if translation == text or not ANY_HAN_RE.search(translation):
            continue
        candidate = review_row(sid, text, translation)
        if evaluate_row({"source_sha256": sid, "source": text}, candidate, CFG["glossary"]) \
                ["qa_verdict"] == "PASS":
            rows.append(candidate)
    return rows


def strict_synthetic_ledger(entries: list[tuple[int, str]]) -> list[dict]:
    """One QA-PASS row per translatable command; fail loudly when none survives.

    Strict so that a test can never silently skip the write-back path it was
    written to exercise: the whole translatable surface of a frozen bundle is
    walked and the row count is asserted by the caller.
    """
    rows = []
    for _index, text in entries:
        sid = source_id(text)
        translation = KANA_RE.sub("呀", text)
        translation = translation.replace("プロデューサー", "制作人")
        translation = translation.replace("アイドル", "偶像")
        if JP_FINAL_ADVERSATIVE_RE.search(text) and not ZH_ADVERSATIVE_RE.search(translation):
            translation = translation.rstrip("…。！？!?♪～〜 \n") + "……不过。"
        if translation == text or not ANY_HAN_RE.search(translation):
            raise AssertionError(f"synthetic translation is not usable: {text!r}")
        candidate = review_row(sid, text, translation)
        if evaluate_row({"source_sha256": sid, "source": text}, candidate,
                        CFG["glossary"])["qa_verdict"] != "PASS":
            raise AssertionError(f"synthetic row unexpectedly failed QA: {text!r}")
        rows.append(candidate)
    return rows


ANY_HAN_RE = re.compile(r"[一-鿿]")
JP_FINAL_ADVERSATIVE_RE = re.compile(
    r"(?:けど(?:も)?|けれど(?:も)?|ですが|だが)\s*[………。！？!?♪～〜]*\s*$"
)
ZH_ADVERSATIVE_RE = re.compile(r"(?:不过|但是|可是|但|然而|只是|倒是|虽然)")
CFG: dict = {}


# ------------------------------------------------------------------ identity gates


def test_frozen_version_identity_is_fail_closed():
    identity = mod.frozen_version("9.0.200", "1077100", mod.INDEX)
    assert identity["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert identity["asset_index_sha256"] == mod.INDEX_SHA
    with pytest.raises(mod.GateError, match="client version"):
        mod.frozen_version("9.0.100", "1077100", mod.INDEX)
    with pytest.raises(ValueError, match="assets version mismatch"):
        mod.frozen_version("9.0.200", "1077500", mod.INDEX)
    with pytest.raises(ValueError, match="assets version mismatch"):
        mod.frozen_version("9.0.200", "1077460", mod.INDEX)


def test_asset_version_is_derived_not_assumed():
    """A snapshot whose upstream is 1077100 must never be relabelled 1077550."""
    with pytest.raises(ValueError, match="assets version mismatch"):
        mod.frozen_version("9.0.200", "1077550", mod.INDEX)


# ------------------------------------------------------------------ source cohort


def test_frozen_cohort_is_the_audited_852_bundle_set(frozen):
    cohort = frozen["cohort"]
    assert len(cohort) == 852
    assert len({row["remote"] for row in cohort}) == 852
    assert len({row["logical"] for row in cohort}) == 852
    assert mod.read_frozen_audit()["status"] == "passed"


def test_every_frozen_bundle_is_one_textasset_with_the_indexed_name(frozen):
    import UnityPy

    for row in frozen["cohort"]:
        env = UnityPy.load(str(mod.SOURCE_ROOT / row["remote"]))
        objects = list(env.objects)
        assert len(objects) == 2, row["logical"]
        text_assets = [o for o in objects if o.type.name == "TextAsset"]
        assert len(text_assets) == 1, row["logical"]
        assert str(text_assets[0].read().m_Name) == row["logical"].removesuffix(".unity3d")


def test_frozen_cohort_rejects_a_wrong_index_sha(monkeypatch, frozen):
    monkeypatch.setattr(mod, "COHORT_SHA", "0" * 64)
    with pytest.raises(mod.GateError, match="source index changed"):
        mod.read_frozen_cohort()


def test_frozen_source_bytes_match_the_indexed_sha(frozen):
    checked = 0
    for row in frozen["cohort"][:25]:
        path = mod.SOURCE_ROOT / row["remote"]
        assert path.stat().st_size == row["declared_bytes"]
        assert mod.sha_file(path) == row["sha256"]
        checked += 1
    assert checked == 25


# ------------------------------------------------------------------ release gate


def test_machine_jsonl_without_release_gate_is_refused(frozen):
    """The production machine queue is not a ledger; zero rows carry release_gate."""
    machine = Path("build/localization-90200/machine-translations-nongtx-api.jsonl")
    if not machine.is_file():
        pytest.skip("production non-GTX machine JSONL absent")
    with pytest.raises(mod.GateError, match="non-production translation evidence"):
        mod.load_gate_accepted([machine], frozen["queue"], frozen["glossary"])


def test_smoke_and_synthetic_named_evidence_is_refused(tmp_path, frozen):
    for name in ("launch-smoke.jsonl", "run-synthetic.jsonl", "legacy-seed.jsonl",
                 "benchmark-rows.jsonl", "pilot-accepted.jsonl"):
        path = write_jsonl(tmp_path / name, [])
        with pytest.raises(mod.GateError, match="non-production translation evidence"):
            mod.load_gate_accepted([path], frozen["queue"], frozen["glossary"])


def test_accepted_row_without_reviewer_identity_is_refused(tmp_path, frozen):
    sid, row = next(iter(frozen["queue"].items()))
    ledger = write_jsonl(
        tmp_path / "fake-ledger.jsonl",
        [review_row(sid, row["source"], row["source"], reviewer=None)],
    )
    with pytest.raises(mod.GateError, match="no independent reviewer identity"):
        mod.load_gate_accepted([ledger], frozen["queue"], frozen["glossary"])


def test_gate_string_alone_is_not_approval(tmp_path, frozen):
    sid, row = next(iter(frozen["queue"].items()))
    unapproved = review_row(sid, row["source"], row["source"], gate="needs_review")
    ledger = write_jsonl(tmp_path / "not-accepted.jsonl", [unapproved])
    # load_release_translations drops non-accepted rows, so the row never lands;
    # the failure must be an explicit "nothing accepted" refusal, not silence.
    assert mod.load_gate_accepted([ledger], frozen["queue"], frozen["glossary"]) == {}


def test_row_outside_the_frozen_queue_is_refused(tmp_path, frozen):
    ledger = write_jsonl(
        tmp_path / "outside.jsonl",
        [review_row(source_id("外の台詞"), "外の台詞", "外面的台词")],
    )
    with pytest.raises(mod.GateError, match="not in the frozen event-unit queue"):
        mod.load_gate_accepted([ledger], frozen["queue"], frozen["glossary"])


def test_stale_review_payload_is_refused(tmp_path, frozen):
    sid, row = next(iter(frozen["queue"].items()))
    candidate = review_row(sid, row["source"], "不同的译文")
    candidate["review"]["translation"] = "被掉包的旧译文"
    ledger = write_jsonl(tmp_path / "stale-review.jsonl", [candidate])
    with pytest.raises(mod.GateError, match="stale review payload"):
        mod.load_gate_accepted([ledger], frozen["queue"], frozen["glossary"])


def test_untranslated_bundle_is_rejected_by_require_complete(frozen, tmp_path):
    """A ledger missing one source must fail closed for that bundle."""
    CFG["glossary"] = frozen["glossary"]
    row = frozen["cohort"][0]
    document = bundle_document(row["remote"])
    entries = translatable_entries(document)
    rows = strict_synthetic_ledger(entries)
    assert len(rows) >= 2, "need at least two sources to drop one"
    ledger = write_jsonl(tmp_path / "partial.jsonl", rows[1:])
    translations = mod.load_gate_accepted([ledger], frozen["queue"], frozen["glossary"])
    assert translations
    source_root, output_root = _mirror_source(tmp_path, row)
    with pytest.raises(mod.GateError, match="no gate-accepted translation"):
        mod.materialize_bundle(
            row["logical"], row["remote"], row["declared_bytes"],
            source_root / row["remote"], output_root / row["remote"],
            translations, True,
        )
    assert not (output_root / row["remote"]).exists()


def test_partial_ledger_driver_refuses_before_writing_any_bundle(frozen, tmp_path):
    """--require-complete with an incomplete ledger must not create the root."""
    CFG["glossary"] = frozen["glossary"]
    row = frozen["cohort"][0]
    rows = strict_synthetic_ledger(translatable_entries(bundle_document(row["remote"])))
    ledger = write_jsonl(tmp_path / "partial.jsonl", rows)
    output_root = tmp_path / "never-created"
    with pytest.raises(mod.GateError, match="no gate-accepted translation"):
        mod.run(_args(ledger, output_root, preflight_only=False))
    assert not output_root.exists()
    assert not output_root.with_name(output_root.name + ".incomplete").exists()


# ------------------------------------------------------------------ write-back


def _mirror_source(tmp_path: Path, row: dict) -> tuple[Path, Path]:
    """Copy the frozen bundle into the tmp tree so nothing can touch the cohort."""
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mod.SOURCE_ROOT / row["remote"], source_root / row["remote"])
    return source_root, tmp_path / "out" / "jp-android"


def test_write_back_roundtrips_and_preserves_non_text_fields(frozen, tmp_path):
    import UnityPy

    CFG["glossary"] = frozen["glossary"]
    row = frozen["cohort"][0]
    document = bundle_document(row["remote"])
    entries = translatable_entries(document)
    rows = strict_synthetic_ledger(entries)
    ledger = write_jsonl(tmp_path / "ledger.jsonl", rows)
    translations = mod.load_gate_accepted([ledger], frozen["queue"], frozen["glossary"])
    source_root, output_root = _mirror_source(tmp_path, row)
    original = source_root / row["remote"]
    before = original.read_bytes()

    record = mod.materialize_bundle(
        row["logical"], row["remote"], row["declared_bytes"], original,
        output_root / row["remote"], translations, False,
    )
    assert record["roundtrip_verified"] is True
    assert record["non_text_objects_byte_identical"] is True
    assert record["canonical_json_roundtrip_verified"] is True
    assert record["changes"], "expected at least one localized command"
    assert original.read_bytes() == before, "frozen source must not be modified"

    source_env = UnityPy.load(str(original))
    written_env = UnityPy.load(str(output_root / row["remote"]))
    source_doc = bundle_document(row["remote"])
    written_obj = [o for o in written_env.objects if o.type.name == "TextAsset"][0]
    written_doc = json.loads(bytes(written_obj.read().m_Script).decode("utf-8-sig"))
    assert written_obj.read().m_Name == row["logical"].removesuffix(".unity3d")
    assert len(written_env.objects) == len(source_env.objects) == 2
    changed = {change["command_index"]: change["localized"] for change in record["changes"]}
    for index, (before_command, after_command) in enumerate(
            zip(source_doc["commands"], written_doc["commands"])):
        assert {k: v for k, v in before_command.items() if k != "text"} == {
            k: v for k, v in after_command.items() if k != "text"}
        if index in changed:
            assert after_command["text"] == changed[index]
        else:
            assert after_command["text"] == before_command["text"]


def test_non_text_unity_object_bytes_are_identical(frozen, tmp_path):
    import UnityPy

    CFG["glossary"] = frozen["glossary"]
    row = frozen["cohort"][0]
    document = bundle_document(row["remote"])
    rows = strict_synthetic_ledger(translatable_entries(document))
    translations = mod.load_gate_accepted(
        [write_jsonl(tmp_path / "ledger.jsonl", rows)], frozen["queue"], frozen["glossary"]
    )
    source_root, output_root = _mirror_source(tmp_path, row)
    mod.materialize_bundle(
        row["logical"], row["remote"], row["declared_bytes"], source_root / row["remote"],
        output_root / row["remote"], translations, False,
    )
    first = None
    for env_path, label in (
        (source_root / row["remote"], "source"),
        (output_root / row["remote"], "written"),
    ):
        env = UnityPy.load(str(env_path))
        non_text = {
            (int(o.path_id), o.type.name): sha(o.get_raw_data())
            for o in env.objects if o.type.name != "TextAsset"
        }
        if first is None:
            first = non_text
            assert first, f"{label}: expected at least one non-text Unity object"
        else:
            assert non_text == first


def test_empty_and_non_japanese_text_fields_are_untouched(frozen, tmp_path):
    CFG["glossary"] = frozen["glossary"]
    row = frozen["cohort"][0]
    document = bundle_document(row["remote"])
    rows = strict_synthetic_ledger(translatable_entries(document))
    translations = mod.load_gate_accepted(
        [write_jsonl(tmp_path / "ledger.jsonl", rows)], frozen["queue"], frozen["glossary"]
    )
    source_root, output_root = _mirror_source(tmp_path, row)
    record = mod.materialize_bundle(
        row["logical"], row["remote"], row["declared_bytes"], source_root / row["remote"],
        output_root / row["remote"], translations, False,
    )
    touched = {change["command_index"] for change in record["changes"]}
    for index, command in enumerate(document["commands"]):
        text = command["text"]
        if index in touched:
            continue
        if not text or not SOURCE_TEXT_RE.search(text):
            assert text == document["commands"][index]["text"]


# ------------------------------------------------------------------ driver safety


def test_allow_partial_trial_writes_isolated_real_bundles(frozen, tmp_path, capsys):
    """The isolated trial path must actually write bundles and never claim release."""
    CFG["glossary"] = frozen["glossary"]
    document = bundle_document(frozen["cohort"][0]["remote"])
    rows = strict_synthetic_ledger(translatable_entries(document))
    assert len(rows) >= 10, "need a partial ledger smaller than the 13177-source cohort"
    ledger = write_jsonl(tmp_path / "partial.jsonl", rows)
    output_root = tmp_path / "trial-out"
    exit_code = mod.run(_args(
        ledger, output_root, preflight_only=False, require_complete=False,
        bundle_limit=2,
    ))
    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert '"isolated_partial_trial_never_release"' in stdout
    assert '"output_created": false' in stdout  # preflight never staged anything
    manifest = json.loads((output_root / "event-unit-90200-manifest.json").read_text("utf-8"))
    assert manifest["bundles_written"] == 2
    assert manifest["release_ready"] is False
    assert manifest["isolated_partial_trial"] is True
    assert manifest["status"] == "isolated_partial_trial_never_release"
    assert manifest["source_archive_modified"] is False
    assert manifest["translation_production_modified"] is False
    assert len(manifest["bundles"]) == 2
    for record in manifest["bundles"]:
        written = Path(record["output_path"])
        assert written.is_file()
        assert written.name == record["remote"]
        assert record["roundtrip_verified"] is True
        assert record["non_text_objects_byte_identical"] is True


def test_output_root_inside_read_only_roots_is_refused(tmp_path):
    for forbidden in (
        mod.REPO / "work" / "local-assets" / "jp-android-candidate",
        mod.REPO / "build" / "localization-90200" / "event-unit-candidate",
        mod.SOURCE_ROOT / "candidate",
    ):
        with pytest.raises(mod.GateError, match="read-only/canonical|frozen event-unit source"):
            mod.refuse_output_root(forbidden)


def test_existing_output_root_is_refused(tmp_path):
    existing = tmp_path / "already-there"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="NEW and isolated"):
        mod.refuse_output_root(existing)
    (tmp_path / "stale.incomplete").mkdir()
    with pytest.raises(FileExistsError, match="NEW and isolated"):
        mod.refuse_output_root(tmp_path / "stale")


def test_preflight_never_creates_output(frozen, tmp_path, capsys):
    """The real production machine JSONL has no accepted rows: expect refusal."""
    machine = mod.REPO / "build" / "localization-90200" / "machine-translations-nongtx-api.jsonl"
    if not machine.is_file():
        pytest.skip("production non-GTX machine JSONL absent")
    output_root = tmp_path / "never-created"
    with pytest.raises(mod.GateError, match="non-production translation evidence"):
        mod.run(_args(machine, output_root, preflight_only=True))
    assert not output_root.exists()

    # A technically acceptable but empty ledger reports 13177 missing and exits 3.
    ledger = write_jsonl(tmp_path / "empty-but-valid.jsonl", [])
    exit_code = mod.run(_args(ledger, output_root, preflight_only=True))
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert report["release_accepted_source_values"] == 0
    assert report["missing_release_accepted"] == 13177
    assert report["output_created"] is False
    assert not output_root.exists()


def _args(translations: Path, output_root: Path, *, preflight_only: bool,
          require_complete: bool = True, bundle_limit: int | None = None):
    return type("Args", (), {
        "client_version": "9.0.200",
        "asset_version": "1077100",
        "translations": [translations],
        "output_root": output_root,
        "preflight_only": preflight_only,
        "require_complete": require_complete,
        "bundle_limit": bundle_limit,
    })()


def test_bundle_limit_forces_partial_trial(tmp_path):
    parser = mod.build_parser()
    args = parser.parse_args([
        "--client-version", "9.0.200", "--asset-version", "1077100",
        "--translations", "x.jsonl", "--output-root", "y",
        "--allow-partial", "--bundle-limit", "1",
    ])
    assert args.bundle_limit == 1
    assert args.require_complete is False


def test_bundle_limit_over_the_cohort_is_refused(frozen, tmp_path, capsys):
    ledger = write_jsonl(tmp_path / "ledger.jsonl", [])
    args = _args(ledger, tmp_path / "out", preflight_only=False, require_complete=False)
    args.bundle_limit = 853
    with pytest.raises(mod.GateError, match=r"--bundle-limit must be within 1\.\.852"):
        mod.run(args)
    assert not (tmp_path / "out").exists()
