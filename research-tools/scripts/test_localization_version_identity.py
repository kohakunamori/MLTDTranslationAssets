"""Pairwise version binding and fail-closed snapshot checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.localization_version_identity import version_identity


@pytest.fixture
def snapshot(tmp_path: Path) -> tuple[Path, Path]:
    index = tmp_path / "a1b2.data"
    index.write_bytes(b"indexed assets")
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps({
            "complete": True,
            "scope": "jp-android",
            "upstream_root": "https://example.invalid/1077100/production/2018/Android",
            "asset_index": str(index),
            "objects": [{"logical": "abc", "remote": "def.unity3d"}],
        }),
        encoding="utf-8",
    )
    return path, index


def test_pair_and_index_bound(snapshot: tuple[Path, Path]) -> None:
    path, index = snapshot
    result = version_identity(
        path, client_version="9.0.200", asset_version="1077100", asset_index=index
    )
    assert result["version_key"] == "jp-client-9.0.200-assets-1077100"
    assert result["asset_index_sha256"] == hashlib.sha256(b"indexed assets").hexdigest()
    assert result["snapshot_objects"] == 1


@pytest.mark.parametrize("assets", ["1077340", "107710", ""])
def test_cross_asset_version_rejected(
    snapshot: tuple[Path, Path], assets: str
) -> None:
    with pytest.raises(ValueError):
        version_identity(snapshot[0], client_version="9.0.200", asset_version=assets)


def test_cross_index_rejected(snapshot: tuple[Path, Path], tmp_path: Path) -> None:
    other = tmp_path / "other.data"
    other.write_bytes(b"other index")
    with pytest.raises(ValueError, match="asset index mismatch"):
        version_identity(
            snapshot[0], client_version="9.0.200", asset_version="1077100",
            asset_index=other,
        )


def test_incomplete_snapshot_rejected(snapshot: tuple[Path, Path]) -> None:
    path, _ = snapshot
    value = json.loads(path.read_text(encoding="utf-8"))
    value["complete"] = False
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="not complete"):
        version_identity(path, client_version="9.0.200", asset_version="1077100")


def test_invalid_client_version_rejected(snapshot: tuple[Path, Path]) -> None:
    with pytest.raises(ValueError, match="invalid client version"):
        version_identity(
            snapshot[0], client_version="9.0.200+1077100", asset_version="1077100"
        )
