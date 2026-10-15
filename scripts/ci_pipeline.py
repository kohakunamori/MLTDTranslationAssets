"""Plan the single production run without relying on workflow dispatch chains."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

STAGES = {'watch', 'translate', 'images', 'release', 'gateway', 'full'}
GATEWAY_PATHS = ('asset-server/cloudflare/', 'asset-server/r2-publisher/',
                 'configs/r2-storage.json', '.github/workflows/cloudflare-gateway.yml', 'generated/')


def plan(event, stage='watch', changed=()):
    if event == 'push':
        stage = 'release'
    if stage not in STAGES:
        raise ValueError(f'Unknown production stage: {stage}')
    return {'stage': stage, 'track': stage in ('watch', 'translate', 'full'),
            'deploy': stage in ('full', 'gateway') or
            (event == 'push' and any(path.startswith(GATEWAY_PATHS) for path in changed))}


def main():
    event = os.environ.get('CI_EVENT', 'workflow_dispatch')
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    changed = []
    if event == 'push':
        before = os.environ.get('CI_BEFORE', '')
        if not re.fullmatch('[0-9a-f]{40}', before) or before == '0' * 40:
            changed = ['asset-server/cloudflare/']  # first push: deploy conservatively
        else:
            changed = subprocess.check_output(['git', 'diff', '--name-only', before, source], text=True).splitlines()
    result = plan(event, os.environ.get('CI_STAGE') or 'watch', changed)
    result['source_commit'] = source
    print(json.dumps(result, sort_keys=True))
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
            for key, value in result.items():
                stream.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')


if __name__ == '__main__':
    main()
