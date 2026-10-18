"""R2 data access + sampled gateway cache telemetry aggregation tests."""
import json

import analyze_r2_observability as analyzer


def test_r2_data_access_by_worker_action_and_colo():
    records = [
        {
            "dataset": "r2",
            "$metadata": {"namespace": "r2", "service": "mltd-localized-assets"},
            "source": {
                "action": "GetObject", "scriptName": "mltd-assets",
                "requestMetadata": {"colo": "AMS"},
                "resource": {"key": "packs/sha256/private"}
            },
        },
        {
            "dataset": "r2",
            "source": {
                "action": "ListObjectsV2", "bucket": "mltd-localized-assets",
                "scriptName": "mltd-localized-publisher",
                "requestMetadata": {"colo": "SJC"}
            }
        },
        {
            "dataset": "r2",
            "source": {
                "action": "DeleteObjects", "bucket": "mltd-localized-assets",
                "scriptName": "mltd-localized-publisher"
            }
        },
        {
            "dataset": "r2",
            "source": {
                "action": "GetObject", "bucket": "another-bucket",
                "scriptName": "other"
            }
        },
    ]
    r2 = analyzer.summarize(records)["r2"]
    assert r2["total_events"] == 3
    assert r2["class_b_observed"] == 1
    assert r2["class_a_observed"] == 1
    assert r2["free_deletes_observed"] == 1
    assert r2["by_script"]["mltd-assets"] == 1
    assert r2["by_colo"]["AMS"] == 1
    assert "private" not in json.dumps(r2)


def test_sampled_worker_cache_only_counts_anonymized_metrics():
    record = {
        "dataset": "cloudflare-workers",
        "source": {
            "message": "mltd_gateway_metrics " + json.dumps({
                "event": "mltd_gateway_cache",
                "cache": "pack-miss",
                "colo": "AMS",
                "r2_pack_gets": 1,
                "r2_object_gets": 0,
                "shared_pack_inflight": False,
            })
        },
        "$workers": {"event": {"request": {
            "url": "https://example.com/PRIVATE_RESOURCE",
            "headers": {"cf-connecting-ip": "192.0.2.1"}
        }}},
    }
    result = analyzer.summarize([record])
    assert result["r2"]["total_events"] == 0
    assert result["gateway"]["sampled_requests"] == 1
    assert result["gateway"]["cache_outcomes"] == {"pack-miss": 1}
    assert result["gateway"]["sampled_r2_get_attempts"]["pack_get_attempts"] == 1
    output = json.dumps(result)
    assert "192.0.2.1" not in output
    assert "PRIVATE_RESOURCE" not in output


def test_json_array_and_json_lines(tmp_path):
    record = {"dataset": "r2", "source": {
        "action": "HeadObject", "bucket": "mltd-localized-assets"
    }}
    a = tmp_path / "array.json"
    a.write_text(json.dumps([record]), encoding="utf8")
    b = tmp_path / "lines.jsonl"
    b.write_text(json.dumps(record) + "\n", encoding="utf8")
    assert analyzer.load(a) == analyzer.load(b) == [record]
