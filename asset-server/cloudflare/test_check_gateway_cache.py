'''Gateway live acceptance must recognize pack hits as real cache savings.'''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_gateway


def test_packed_hit_stops_retry_without_extra_requests(monkeypatch):
    calls = []

    class Runner:
        def request(self, url):
            calls.append(url)
            return 200, {'x-mltd-cache': 'pack-miss' if len(calls) == 1 else 'pack-hit'}, b'bytes'

    monkeypatch.setattr(check_gateway.time, 'sleep', lambda _: None)
    status, headers, body, _ = check_gateway.read_until_cached(Runner(), 'https://example.test/asset')
    assert status == 200
    assert body == b'bytes'
    assert headers['x-mltd-cache'] == 'pack-hit'
    assert len(calls) == 2


def test_single_object_hit_still_accepted():
    class Runner:
        def request(self, url):
            return 200, {'x-mltd-cache': 'hit'}, b'bytes'

    _, headers, _, _ = check_gateway.read_until_cached(Runner(), 'https://example.test/asset')
    assert headers['x-mltd-cache'] == 'hit'
