from __future__ import annotations

import json

from tools import asset_version


def test_progress_prefers_live_telemetry(tmp_path):
    root = tmp_path / "archive"
    progress = root / "versions" / "1077340" / "archive-progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text(
        json.dumps(
            {
                "game": "mltd",
                "version": "1077340",
                "scope": "jp-android",
                "total_objects": 100,
                "successful_objects": 37,
                "missing_objects": 63,
                "processed_bytes": 123456,
                "percent": 37.0,
                "running": True,
            }
        ),
        encoding="utf-8",
    )

    report = asset_version.progress_of(root, "1077340")

    assert report["source"] == "live-progress"
    assert report["registered"] == 100
    assert report["mapped"] == 37
    assert report["missing"] == 63
    assert report["logical_bytes"] == 123456
    assert report["percent"] == 37.0
    assert report["running"] is True


def test_progress_falls_back_to_manifest_observed_state(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "game": "mltd",
                "active_version": None,
                "releases": {
                    "1077100": {
                        "version": "1077100",
                        "store": {
                            "registered": 200,
                            "mapped": 50,
                            "logical_bytes": 98765,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    report = asset_version.progress_of(root, "1077100")

    assert report["source"] == "manifest-observed"
    assert report["registered"] == 200
    assert report["mapped"] == 50
    assert report["missing"] == 150
    assert report["percent"] == 25.0
