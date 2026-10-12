"""Per-package equivalence, including memory misses and cross-version routing."""
import copy
import hashlib
import json
import contextlib
import io
from argparse import Namespace
from pathlib import Path
from unittest import mock

import pytest

import build_generated_release as build
import bundle_incremental as incremental
from pipelines.text import mltd_localization_pipeline as pipeline
from assets_generated_index import GeneratedStore


def accepted(bundle, key, source, zh):
    return {"bundle": bundle.removesuffix(".unity3d"), "key": key,
            "source": source, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "translation": zh, "status": "accepted", "asset_version": "100"}


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "ROOT", tmp_path)
    store = GeneratedStore(tmp_path / "generated")
    rows = {name: {"remote": name + ".remote.unity3d", "declared_size": len(name.encode()),
                   "catalog_hash": hashlib.sha256(name.encode()).hexdigest()}
            for name in ("a.gtx.unity3d", "b.gtx.unity3d", "c.gtx.unity3d")}
    texts = {"a.gtx.unity3d": "k^原文|missing^共有文|unresolved^新しい文",
             "b.gtx.unity3d": "k^共有文", "c.gtx.unity3d": "k^別の原文"}
    grouped = {"a.gtx.unity3d": [accepted("a.gtx", "k", "原文", "译文")],
               "b.gtx.unity3d": [accepted("b.gtx", "k", "共有文", "共享译文")],
               "c.gtx.unity3d": [accepted("c.gtx", "k", "別の原文", "其他译文")]}
    downloads, parses = [], []

    def download(url, destination, *_args):
        name = next(name for name, row in rows.items() if url.endswith(row["remote"]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(name.encode())
        downloads.append(name)
        return "fetched"

    def parse(source):
        name = source.read_text()
        parses.append(name)
        return name.removesuffix(".unity3d"), texts[name], b"cipher"

    monkeypatch.setattr(build, "download", download)
    monkeypatch.setattr(build, "read_gtx", parse)

    def prepare(previous=None):
        work = tmp_path / f"work-{len(list(tmp_path.glob('work-*')))}"
        work.mkdir()
        result = build.prepare_text_inputs(
            work=work, store=store, version={"asset_version": "100", "asset_root": "https://test"},
            targets=rows, grouped=grouped, previous=previous, official_cache=None)
        return result

    return store, rows, grouped, texts, downloads, parses, prepare


def publish_state(store, records):
    entries = []
    for name, record in records.items():
        payload = name.encode()
        artifact = hashlib.sha256(payload).hexdigest()
        path = store.object_path(artifact)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        entries.append({"logical_key": name, "resource_kind": "bundle",
                        "translation_status": "modified", "artifact_sha256": artifact,
                        "source_sha256": record["source_sha256"], "translated_sha256": artifact,
                        "runtime_path": "production/2018/Android/old.unity3d"})
    incremental.finish_records(records, entries)
    return {"build_status": "success", "asset_version": "99", "entries": entries,
            incremental.STATE_KEY: {"schema": incremental.SCHEMA, "text": copy.deepcopy(records)}}


def test_only_memory_dependents_and_edited_bundle_change(library):
    store, rows, grouped, _texts, downloads, parses, prepare = library
    _selected, before, _ledger, _stats = prepare()
    previous = publish_state(store, before)
    grouped["b.gtx.unity3d"][0]["translation"] = "共享新译文"
    selected, after, ledger, stats = prepare(previous)
    plan = incremental.plan(surface="text", records=after, rows=rows,
                            previous=previous, object_file=store.object_file)
    assert set(plan.rebuild) == {"a.gtx.unity3d", "b.gtx.unity3d"}
    assert set(plan.reusable) == {"c.gtx.unity3d"}
    assert len(downloads) == len(parses) == 3  # No Unity reads on the second pass.
    assert stats["source_records_reused"] == 3
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 3  # Complete resolver input.


def test_unresolved_lookup_becomes_translated(library):
    store, rows, grouped, _texts, _downloads, _parses, prepare = library
    _, before, _, _ = prepare()
    previous = publish_state(store, before)
    grouped["b.gtx.unity3d"].append(accepted("b.gtx", "extra", "新しい文", "新译文"))
    # An unrelated stale row is not eligible for the global resolver.
    _, unchanged, _, _ = prepare(previous)
    assert unchanged["a.gtx.unity3d"]["fingerprint"] == before["a.gtx.unity3d"]["fingerprint"]
    # A matching new source supplies memory for the formerly unresolved query.
    library[3]["b.gtx.unity3d"] += "|extra^新しい文"
    rows["b.gtx.unity3d"]["catalog_hash"] = "new-official-source"
    _, changed, _, _ = prepare(previous)
    assert changed["a.gtx.unity3d"]["fingerprint"] != before["a.gtx.unity3d"]["fingerprint"]


def test_remote_rename_reuses_objects_and_updates_routing(library):
    store, rows, _grouped, _texts, downloads, _parses, prepare = library
    _, before, _, _ = prepare()
    previous = publish_state(store, before)
    for row in rows.values():
        row["remote"] = "renamed-" + row["remote"]
    _, after, _, _ = prepare(previous)
    plan = incremental.plan(surface="text", records=after, rows=rows,
                            previous=previous, object_file=store.object_file)
    assert not plan.rebuild
    assert len(plan.reusable) == 3
    assert len(downloads) == 3
    for name, entry in plan.reusable.items():
        assert entry["runtime_path"] == "production/2018/Android/" + rows[name]["remote"]


def test_corrupt_source_cache_reparses_only_its_bundle(library):
    store, rows, _grouped, _texts, downloads, parses, prepare = library
    _, before, _, _ = prepare()
    previous = publish_state(store, before)
    record = before["a.gtx.unity3d"]
    path = store.root / "build-inputs/text" / record["parser"] / (record["source_sha256"] + ".json.gz")
    path.write_bytes(b"corrupt")
    _, after, _, _ = prepare(previous)
    assert downloads[-1:] == parses[-1:] == ["a.gtx.unity3d"]
    assert len(downloads) == 4
    assert after["a.gtx.unity3d"]["fingerprint"] == before["a.gtx.unity3d"]["fingerprint"]


def test_missing_artifact_rebuilds_one_package(library):
    store, rows, _grouped, _texts, _downloads, _parses, prepare = library
    _, records, _, _ = prepare()
    previous = publish_state(store, records)
    store.object_path(previous["entries"][0]["artifact_sha256"]).unlink()
    plan = incremental.plan(surface="text", records=records, rows=rows,
                            previous=previous, object_file=store.object_file)
    assert set(plan.rebuild) == {"a.gtx.unity3d"}
    forced = incremental.plan(surface="text", records=records, rows=rows,
                              previous=previous, object_file=store.object_file, force=True)
    assert len(forced.rebuild) == 3


def test_negative_results_are_reused_without_publishing_an_old_overlay(library):
    store, rows, _grouped, _texts, _downloads, _parses, prepare = library
    _, records, _, _ = prepare()
    previous = publish_state(store, records)
    previous["entries"] = previous["entries"][1:]
    previous[incremental.STATE_KEY]["text"]["a.gtx.unity3d"]["written"] = False
    plan = incremental.plan(surface="text", records=records, rows=rows,
                            previous=previous, object_file=store.object_file)
    assert "a.gtx.unity3d" not in plan.rebuild
    assert "a.gtx.unity3d" not in plan.reusable
    assert plan.negative_reused == 1


def test_unrelated_test_code_does_not_change_writer_fingerprint(tmp_path):
    before = incremental.writer_fingerprint(tmp_path, incremental.TEXT_FILES)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/test_tracker.py").write_text("# changed")
    assert incremental.writer_fingerprint(tmp_path, incremental.TEXT_FILES) == before
    (tmp_path / "scripts/build_lyric_overlay.py").write_text("# changed lyric writer")
    assert incremental.writer_fingerprint(tmp_path, incremental.TEXT_FILES) == before
    assert incremental.writer_fingerprint(tmp_path, incremental.LYRIC_FILES) != before


def test_incremental_outputs_equal_full_overlay_with_memory_and_misses(library, monkeypatch):
    """Exercise the real overlay resolver/record writer independently of planning."""
    store, rows, grouped, texts, _downloads, _parses, prepare = library

    def read_source(path):
        name = next(name for name, row in rows.items() if row["remote"] == Path(path).name)
        return name.removesuffix(".unity3d"), texts[name], b"cipher"

    def save_bundle(source, output, translated):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(translated.encode("utf-8"))
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        return {"source_bundle_sha256": source_sha,
                "output_plain_sha256": hashlib.sha256(translated.encode()).hexdigest()}

    monkeypatch.setattr(pipeline, "read_gtx", read_source)
    monkeypatch.setattr(pipeline, "save_localized_bundle", save_bundle)
    monkeypatch.setattr(pipeline, "version_identity", lambda *args, **kwargs: {})

    def overlay(snapshot, archive, ledger, output, *_args):
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.cmd_build_overlay(Namespace(
                snapshot=snapshot, archive_root=archive, translations=[ledger],
                output_root=output, client_version="9.0.200", asset_version="100",
                limit=0, progress_every=0))

    monkeypatch.setattr(build, "run_overlay", overlay)

    def run(targets, selected, ledger, directory):
        return build.run_text_surface(
            work=ledger.parent, version={"asset_version": "100", "asset_root": "https://test",
                                        "index_name": "index.data"}, targets=targets, grouped=grouped,
            source_client="9.0.200", official_cache=None, overlay=directory,
            selected_grouped=selected, full_ledger=ledger)

    selected, records, ledger, _ = prepare()
    first = run(rows, selected, ledger, ledger.parent / "first")
    previous = publish_state(store, records)
    before_payloads = {name: (first.overlay / "jp-android" / row["remote"]).read_bytes()
                       for name, row in rows.items()}
    grouped["b.gtx.unity3d"][0]["translation"] = "共享新译文"
    selected, records, ledger, _ = prepare(previous)
    plan = incremental.plan(surface="text", records=records, rows=rows,
                            previous=previous, object_file=store.object_file)
    subset = run(plan.rebuild, selected, ledger, ledger.parent / "subset")
    full = run(rows, selected, ledger, ledger.parent / "full")
    for name, row in rows.items():
        incremental_payload = ((subset.overlay / "jp-android" / row["remote"]).read_bytes()
                               if name in plan.rebuild else before_payloads[name])
        assert incremental_payload == (full.overlay / "jp-android" / row["remote"]).read_bytes()
    assert set(plan.rebuild) == {"a.gtx.unity3d", "b.gtx.unity3d"}
    assert subset.document["resolved_memory"] == 1
    assert subset.document["source_candidates"] > subset.document["resolved_exact"]
