#!/usr/bin/env python3
"""Tests for the strict JP 9.0.200 / 1077100 MD.mld write-back materializer.

Test ledgers are built here and are NEVER release evidence: the encrypted
TextAsset write-back, the AES re-encryption and the reload audit run against the
real frozen bundle copied into a pytest tmp directory, but the translations are
manufactured in this file.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts import materialize_mld_90200 as mod
from scripts.mltd_localize_gtx import KANA_RE, decrypt_payload, encrypt_payload
from scripts.mltd_translation_quality import evaluate_row, load_glossary, source_id


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
        "provenance": {"provider": "synthetic", "model_id": "test-model",
                       "model": "test-model"},
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
    """Real frozen MD.mld artifacts; absent artifacts skip the module."""
    for path in (mod.INDEX, mod.SNAPSHOT, mod.SOURCE, mod.AUDIT, mod.QUEUE,
                 mod.PLAIN_SNAPSHOT, mod.DECODED, mod.GLOSSARY):
        if not path.exists():
            pytest.skip(f"frozen MD.mld artifact missing: {path}")
    if mod.sha_file(mod.SOURCE) != mod.BUNDLE_SHA:
        pytest.skip("frozen MD.mld bundle is not the audited file")
    queue = mod.read_frozen_queue()
    glossary = load_glossary(mod.GLOSSARY if mod.GLOSSARY.is_file() else None)
    return {"queue": queue, "glossary": glossary}


def synthetic_rows(queue: dict, glossary: dict
                   ) -> tuple[list[dict], dict[str, str]]:
    """Manufacture QA-PASS rows for the allowlisted 15 sources only."""
    rows: list[dict] = []
    expected: dict[str, str] = {}
    for sid, row in queue.items():
        source = str(row["source"])
        translation = KANA_RE.sub("呀", source) or source + "呀"
        candidate = review_row(sid, source, translation)
        if evaluate_row({"source_sha256": sid, "source": source}, candidate,
                        glossary)["qa_verdict"] != "PASS":
            raise AssertionError(f"synthetic MD row unexpectedly failed QA: {source!r}")
        rows.append(candidate)
        expected[sid] = translation
    return rows, expected


def mirror_source(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mod.SOURCE, source_root / mod.REMOTE)
    return source_root, tmp_path / "out" / "jp-android"


def plain_map(plain: bytes) -> dict[str, bytes]:
    _fields, _separators, mapping = mod.parse_plain(plain)
    return mapping


# ------------------------------------------------------------------ identity gates


def test_frozen_version_identity_is_fail_closed():
    identity = mod.frozen_version("9.0.200", "1077100", mod.INDEX)
    assert identity["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert identity["asset_index_sha256"] == mod.INDEX_SHA
    with pytest.raises(mod.GateError, match="client version"):
        mod.frozen_version("9.0.100", "1077100", mod.INDEX)
    for wrong in ("1077500", "1077460", "1077550"):
        with pytest.raises(ValueError, match="assets version mismatch"):
            mod.frozen_version("9.0.200", wrong, mod.INDEX)


def test_frozen_index_row_and_audit_are_pinned():
    assert mod.read_frozen_index_row()[1] == mod.REMOTE
    assert mod.read_frozen_audit()["status"] == mod.AUDIT_STATUS


def test_wrong_audit_sha_is_refused(monkeypatch):
    monkeypatch.setattr(mod, "AUDIT_SHA", "0" * 64)
    with pytest.raises(mod.GateError, match="independent MD.mld source audit changed"):
        mod.read_frozen_audit()


# ------------------------------------------------------------------ allowlist


def test_allowlist_is_exactly_15_sources_and_18_bound_keys(frozen):
    queue = frozen["queue"]
    assert len(queue) == 15
    bound = mod.bound_keys(queue)
    assert len(bound) == 18
    for key, (sid, source) in bound.items():
        assert sid in queue
        assert queue[sid]["source"] == source


def test_ledger_row_outside_the_allowlist_is_refused(tmp_path, frozen):
    foreign = review_row(source_id("対象外の設定"), "対象外の設定", "范围外的设定")
    ledger = write_jsonl(tmp_path / "foreign.jsonl", [foreign])
    with pytest.raises(mod.GateError, match="not in the allowlisted 15 MD.mld"):
        mod.load_gate_accepted([ledger], frozen["queue"], frozen["glossary"],
                               label="allowlisted 15 MD.mld UI data_map sources")


def test_machine_jsonl_without_release_gate_is_refused(frozen):
    machine = mod.REPO / "build" / "localization-90200" / "machine-translations-nongtx-api.jsonl"
    if not machine.is_file():
        pytest.skip("production non-GTX machine JSONL absent")
    with pytest.raises(mod.GateError, match="no explicit release_gate"):
        mod.load_gate_accepted([machine], frozen["queue"], frozen["glossary"],
                               label="allowlisted 15 MD.mld UI data_map sources")


def test_named_non_production_evidence_is_refused(tmp_path, frozen):
    for name in ("launch-smoke.jsonl", "synthetic.jsonl", "legacy-seed.jsonl",
                 "benchmark.jsonl", "mld-pilot.jsonl"):
        path = write_jsonl(tmp_path / name, [])
        with pytest.raises(mod.GateError, match="non-production translation evidence"):
            mod.load_gate_accepted([path], frozen["queue"], frozen["glossary"],
                                   label="allowlisted 15 MD.mld UI data_map sources")


def test_accepted_row_without_reviewer_identity_is_refused(tmp_path, frozen):
    sid = next(iter(frozen["queue"]))
    source = str(frozen["queue"][sid]["source"])
    ledger = write_jsonl(
        tmp_path / "fake-ledger.jsonl",
        [review_row(sid, source, KANA_RE.sub("呀", source), reviewer=None)],
    )
    with pytest.raises(mod.GateError, match="no independent reviewer identity"):
        mod.load_gate_accepted([ledger], frozen["queue"], frozen["glossary"],
                               label="allowlisted 15 MD.mld UI data_map sources")


# ------------------------------------------------------------------ payload model


def test_frozen_plaintext_is_the_decoded_63639_entry_table():
    plain = mod.PLAIN_SNAPSHOT.read_bytes()
    fields, separators, mapping = mod.parse_plain(plain)
    assert len(fields) == 63639
    assert separators.count(b"<ssp>") == 1
    assert len(mapping) == 63573 + 66
    mod.assert_plain_matches_decoded(mapping, mod.read_decoded_snapshot())


def test_source_cipher_decrypts_and_reencrypts_exactly():
    import UnityPy

    env = UnityPy.load(str(mod.SOURCE))
    _obj, data = mod.mld_textasset(env)
    cipher = bytes(data.m_Script)
    assert len(cipher) == mod.SCRIPT_BYTES
    plain = decrypt_payload(cipher)
    assert plain == mod.PLAIN_SNAPSHOT.read_bytes()
    assert encrypt_payload(plain) == cipher


def test_interior_separator_delimiter_is_refused(frozen):
    """A translation may not smuggle <cn>/<cm>/<sp>/<ssp> into a bound value.

    The owner ledger loader already refuses ``<cn>`` as a protected-token
    mismatch, so this exercises the payload gate directly with a hand-built
    translations mapping: defence in depth for a future ledger format.
    """
    sid, row = next(iter(frozen["queue"].items()))
    plain = mod.PLAIN_SNAPSHOT.read_bytes()
    for payload in ("坏<cn>值", "坏<cm>值", "坏<sp>值", "坏<ssp>值"):
        translations = {sid: {"translation": payload, "source": row["source"]}}
        key = next(iter(row["examples"]))["key"]
        assert mod.bound_keys(frozen["queue"])[key][0] == sid
        with pytest.raises(mod.GateError, match="in-band delimiter"):
            mod.mutate_plain(plain, mod.read_decoded_snapshot(), frozen["queue"],
                             translations)


def test_unrelated_bound_key_value_change_is_refused(frozen):
    """A bound key that no longer holds its queued source must fail closed."""
    plain = mod.PLAIN_SNAPSHOT.read_bytes()
    decoded = mod.read_decoded_snapshot()
    key, (sid, source) = next(iter(mod.bound_keys(frozen["queue"]).items()))
    key_bytes = key.encode("utf-8")
    tampered = plain.replace(key_bytes + b"<cn>", key_bytes + b"<cn>X", 1)
    assert tampered != plain, "bound key is present in the audited payload"
    with pytest.raises(mod.GateError, match="differs from decoded snapshot"):
        mod.mutate_plain(tampered, decoded, frozen["queue"], {})
    # Keep the decoded snapshot consistent with the tampered payload so the
    # audited-table comparison passes and the bound-key gate itself is exercised.
    consistent = json.loads(json.dumps(decoded, ensure_ascii=False))
    if key in consistent["data_map"]:
        consistent["data_map"][key] = "X" + consistent["data_map"][key]
    else:
        pytest.skip("bound key is not a data_map scalar")
    with pytest.raises(mod.GateError, match="no longer holds its queued source"):
        mod.mutate_plain(tampered, consistent, frozen["queue"], {})


# ------------------------------------------------------------------ write-back


def test_write_back_roundtrips_and_preserves_everything_else(frozen, tmp_path):
    import UnityPy

    rows, expected = synthetic_rows(frozen["queue"], frozen["glossary"])
    ledger = write_jsonl(tmp_path / "ledger.jsonl", rows)
    translations = mod.load_gate_accepted(
        [ledger], frozen["queue"], frozen["glossary"],
        label="allowlisted 15 MD.mld UI data_map sources",
    )
    assert len(translations) == 15
    source_root, output_root = mirror_source(tmp_path)
    original = source_root / mod.REMOTE
    before_bytes = original.read_bytes()
    before_plain = mod.PLAIN_SNAPSHOT.read_bytes()
    _fields, before_separators, before_mapping = mod.parse_plain(before_plain)

    record = mod.materialize_bundle(
        original, output_root / mod.REMOTE, translations, True
    )
    # 15 unique sources bind 18 occurrences: one source owns 4 keys.
    assert record["modified_occurrences"] == 18
    assert record["bound_occurrences_rewritten"] == 18
    assert record["unchanged_MD_keys"] == 63639 - 18
    assert record["AES192_CBC_PKCS7_exact_roundtrip"] is True
    assert record["non_text_unity_objects_byte_identical"] is True
    assert record["unityfs_reload_raw_bytes_audited"] is True
    assert original.read_bytes() == before_bytes, "frozen source must not be modified"

    written = UnityPy.load(str(output_root / mod.REMOTE))
    _obj, data = mod.mld_textasset(written)
    after_plain = decrypt_payload(bytes(data.m_Script))
    _fields, after_separators, after_mapping = mod.parse_plain(after_plain)
    assert after_separators == before_separators
    assert list(after_mapping) == list(before_mapping)
    bound = mod.bound_keys(frozen["queue"])
    changed_keys = {change["key"] for change in record["changes"]}
    assert changed_keys == set(bound), "every allowlisted bound key must be rewritten"
    for key, value in before_mapping.items():
        if key in changed_keys:
            sid, _source = bound[key]
            assert after_mapping[key] == expected[sid].encode("utf-8")
        else:
            assert after_mapping[key] == value, f"untouched MD.mld key changed: {key}"


def test_non_text_unity_object_raw_bytes_are_identical(frozen, tmp_path):
    import UnityPy

    rows, _expected = synthetic_rows(frozen["queue"], frozen["glossary"])
    translations = mod.load_gate_accepted(
        [write_jsonl(tmp_path / "ledger.jsonl", rows)], frozen["queue"], frozen["glossary"],
        label="allowlisted 15 MD.mld UI data_map sources",
    )
    source_root, output_root = mirror_source(tmp_path)
    mod.materialize_bundle(source_root / mod.REMOTE, output_root / mod.REMOTE,
                           translations, True)
    baseline = None
    for path in (source_root / mod.REMOTE, output_root / mod.REMOTE):
        env = UnityPy.load(str(path))
        non_text = {
            (int(o.path_id), o.type.name): mod.sha_bytes(o.get_raw_data())
            for o in env.objects if o.type.name != "TextAsset"
        }
        if baseline is None:
            baseline = non_text
            assert baseline, "the audited bundle must contain a non-text object"
        else:
            assert non_text == baseline


def test_partial_ledger_is_refused_by_require_complete(frozen, tmp_path):
    rows, _expected = synthetic_rows(frozen["queue"], frozen["glossary"])
    ledger = write_jsonl(tmp_path / "partial.jsonl", rows[:-1])
    translations = mod.load_gate_accepted(
        [ledger], frozen["queue"], frozen["glossary"],
        label="allowlisted 15 MD.mld UI data_map sources",
    )
    source_root, output_root = mirror_source(tmp_path)
    with pytest.raises(mod.GateError, match="no gate-accepted translation"):
        mod.materialize_bundle(source_root / mod.REMOTE, output_root / mod.REMOTE,
                               translations, True)
    assert not (output_root / mod.REMOTE).exists()


# ------------------------------------------------------------------ driver safety


def test_output_root_inside_read_only_roots_is_refused():
    for forbidden in (
        mod.REPO / "work" / "local-assets" / "mld-candidate",
        mod.REPO / "build" / "localization-90200" / "mld-candidate",
        mod.SOURCE_ROOT / "candidate",
    ):
        with pytest.raises(mod.GateError, match="read-only/canonical|frozen MD.mld source"):
            mod.refuse_output_root(forbidden)


def test_existing_output_root_is_refused(tmp_path):
    existing = tmp_path / "already-there"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="NEW and isolated"):
        mod.refuse_output_root(existing)
    (tmp_path / "stale.incomplete").mkdir()
    with pytest.raises(FileExistsError, match="NEW and isolated"):
        mod.refuse_output_root(tmp_path / "stale")


def _args(translations: Path, output_root: Path, *, preflight_only: bool = True,
          require_complete: bool = True):
    return type("Args", (), {
        "client_version": "9.0.200",
        "asset_version": "1077100",
        "translations": [translations],
        "output_root": output_root,
        "preflight_only": preflight_only,
        "require_complete": require_complete,
    })()


def test_preflight_reports_zero_accepted_and_creates_nothing(tmp_path, capsys):
    output_root = tmp_path / "never-created"
    empty = write_jsonl(tmp_path / "empty-but-valid.jsonl", [])
    assert mod.run(_args(empty, output_root)) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["release_accepted_source_values"] == 0
    assert report["missing_release_accepted"] == 15
    assert report["allowlisted_bound_keys"] == 18
    assert report["output_created"] is False
    assert not output_root.exists()


def test_main_reports_a_refusal_for_the_machine_jsonl(tmp_path, capsys, monkeypatch):
    machine = mod.REPO / "build" / "localization-90200" / "machine-translations-nongtx-api.jsonl"
    if not machine.is_file():
        pytest.skip("production non-GTX machine JSONL absent")
    output_root = tmp_path / "never-created"
    monkeypatch.setattr(mod.sys, "argv", [
        "materialize_mld_90200.py",
        "--client-version", "9.0.200", "--asset-version", "1077100",
        "--translations", str(machine), "--output-root", str(output_root),
        "--preflight-only",
    ])
    assert mod.main() == 4
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "refused"
    assert report["output_created"] is False
    assert not output_root.exists()
