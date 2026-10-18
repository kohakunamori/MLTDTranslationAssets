"""Live acceptance: verify two distinct client paths are served from one verified pack."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def select_two(index):
    packs = index.get('packs', {})
    by_pack = {}
    for path, digest in sorted(index['objects'].items()):
        pin = packs.get(digest)
        if pin is None:
            continue
        by_pack.setdefault(pin[0], []).append((path, digest))
    for group in by_pack.values():
        # Avoid testing a single digest's alias twice.
        chosen = []
        seen = set()
        for path, digest in group:
            if digest not in seen:
                seen.add(digest)
                chosen.append((path, digest))
        if len(chosen) >= 2:
            return chosen[:2]
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--index', type=Path, default=ROOT / 'asset-server/cloudflare/src/index.json')
    args = parser.parse_args()
    index = json.loads(args.index.read_text(encoding='utf8'))
    chosen = select_two(index)
    if not index.get('packs'):
        print('No packed objects in this release; old single-object layout is unchanged')
        return
    if len(chosen) != 2:
        raise SystemExit('Packed release does not provide two distinct objects in one pack')
    for path, digest in chosen:
        url = args.base.rstrip('/') + '/' + index['asset_version'] + '/' + path
        for attempt in range(4):
            try:
                with urlopen(Request(url, headers={'User-Agent': 'mltd-pack-live-acceptance/1'}),
                             timeout=30) as response:
                    payload = response.read()
                    marker = response.headers.get('x-mltd-cache', '')
                    etag = response.headers.get('etag', '')
                    if (response.status == 200
                            and marker in ('pack-hit', 'pack-miss')
                            and etag == '"' + digest + '"'
                            and hashlib.sha256(payload).hexdigest() == digest):
                        print(f'Live R2 pack verified {path}: {marker}; sha256={digest[:16]}')
                        break
                    raise ValueError(f'unverified pack response status={response.status}, cache={marker}')
            except (OSError, ValueError) as error:
                if attempt == 3:
                    raise SystemExit(f'Packed live acceptance failed for {path}: {error}')
                time.sleep(3)


if __name__ == '__main__':
    main()
