import hashlib
import json
from pathlib import Path

import pytest

from scripts.consume_translation_portal_snapshot import SnapshotError, consume


# The asset axis, not a combined tag. `9.0.200+1077500` used to sit here and is
# now output: see test_a_combined_identity_is_refused_instead_of_normalized.
BASE = "1077500"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write(path: Path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def rows(tmp_path):
    source = "通信に失敗しました {0}"
    catalogue = {"base_version": BASE, "bundle": "bundle-a", "key": "title", "source": source, "source_sha256": digest(source)}
    snapshot = {**catalogue, "translation": "通信失败 {0}", "status": "accepted"}
    cat = tmp_path / "catalogue.jsonl"; snap = tmp_path / "snapshot.jsonl"
    write(cat, [catalogue]); write(snap, [snapshot])
    return cat, snap


def test_consumes_exact_catalogue_rows_into_candidate_only_run(tmp_path):
    cat, snap = rows(tmp_path)
    result = consume(snap, cat, tmp_path / "out", BASE)
    assert result["catalogue_rows"] == 1
    assert result["accepted_rows"] == 1
    manifest = json.loads((tmp_path / "out" / "portal-consumption-manifest.json").read_text(encoding="utf-8"))
    assert manifest["candidate_only"] is True
    assert manifest["release_ready"] is False
    assert json.loads((tmp_path / "out" / "candidates.jsonl").read_text(encoding="utf-8"))["status"] == "community_candidate"


def test_normalizes_legacy_catalogue_rows_with_cli_pinned_version(tmp_path):
    cat, snap = rows(tmp_path)
    legacy = json.loads(cat.read_text(encoding="utf-8"))
    legacy.pop("base_version")
    legacy.pop("source_sha256")
    write(cat, [legacy])
    result = consume(snap, cat, tmp_path / "out", BASE)
    assert result["catalogue_rows"] == 1
    normalized = json.loads((tmp_path / "out" / "universe.jsonl").read_text(encoding="utf-8"))
    assert normalized["base_version"] == BASE
    assert normalized["source_sha256"] == digest(normalized["source"])


def test_rejects_stale_catalogue_source(tmp_path):
    cat, snap = rows(tmp_path)
    doc = json.loads(snap.read_text(encoding="utf-8"))
    doc["source"] = "別の原文"
    doc["source_sha256"] = digest(doc["source"])
    write(snap, [doc])
    with pytest.raises(SnapshotError, match="exact catalogue"):
        consume(snap, cat, tmp_path / "out", BASE)


def test_rejects_nonaccepted_or_duplicate_rows(tmp_path):
    cat, snap = rows(tmp_path)
    doc = json.loads(snap.read_text(encoding="utf-8"))
    doc["status"] = "pending"
    write(snap, [doc])
    with pytest.raises(SnapshotError, match="non-accepted"):
        consume(snap, cat, tmp_path / "out", BASE)

    write(snap, [dict(doc, status="accepted"), dict(doc, status="accepted")])
    with pytest.raises(SnapshotError, match="duplicate snapshot"):
        consume(snap, cat, tmp_path / "out-duplicate", BASE)


def test_rejects_structural_token_corruption(tmp_path):
    cat, snap = rows(tmp_path)
    doc = json.loads(snap.read_text(encoding="utf-8"))
    doc["translation"] = "通信失败"
    write(snap, [doc])
    with pytest.raises(SnapshotError, match="structural validation"):
        consume(snap, cat, tmp_path / "out", BASE)

    doc["translation"] = "通信失败|记录 {0}"
    write(snap, [doc])
    with pytest.raises(SnapshotError, match="reserved structural separator"):
        consume(snap, cat, tmp_path / "out-separator", BASE)


def test_a_combined_identity_is_refused_instead_of_normalized(tmp_path):
    """The consumer used to accept `9.0.200+1077100` by splitting on `+`.

    That tolerance was the last place a combined identity could still be read
    as if it were a version. Refusing is the behavior under test: the row is
    not silently reduced to its asset half.
    """
    cat, snap = rows(tmp_path)
    doc = json.loads(cat.read_text(encoding="utf-8"))
    doc["base_version"] = "9.0.200+1077500"
    write(cat, [doc])
    with pytest.raises(SnapshotError, match="!= "):
        consume(snap, cat, tmp_path / "out", "1077500")
    with pytest.raises(SnapshotError, match="!= "):
        consume(snap, cat, tmp_path / "out", "9.0.200+1077500")
