#!/usr/bin/env python3
"""Scheduled dual-track APK pipeline: probe, build, gate, publish, notify.

Tracks:
- ``official`` follows the latest Hotplay APK (package unchanged, assets from
  the official server, only APK-built-in localization);
- ``local`` is pinned to an exact client/assets pair (``9.0.200``/``1077500``)
  served by the NAS asset-server ``/cn/<assets>/`` namespace, package ``.local``.

Each run is idempotent: builds happen only when a track's input fingerprint
changes; candidates go to the NAS ``candidate`` channel (never canonical);
the ``release`` channel additionally needs the track gate report, the private
release signer, and ``publish.release=true``. Success is silent; failures,
version incompatibility, missing signing inputs and review needs are sent once
per distinct problem via ``client.pipeline_notify``. The private config
(template: ``configs/dual-track-pipeline.example.json``) holds paths only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TRACKED_ASSET_PIN = ROOT / 'configs' / 'local-track-asset-pin.json'
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from client import hotplay_source, release_channel  # noqa: E402
from client.pipeline_notify import Notifier  # noqa: E402

SIGNING_ENV = ('MLTD_KEYSTORE', 'MLTD_KEY_ALIAS', 'MLTD_KEYSTORE_PASSWORD', 'MLTD_KEY_PASSWORD')
TOOL_ENV = ('MLTD_BUILD_TOOLS', 'MLTD_APKTOOL_JAR', 'MLTD_APKANALYZER')


def _load_windows_user_env() -> None:
    if sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Environment') as key:
                for name in SIGNING_ENV + TOOL_ENV + ('MLTD_RELEASE_SIGNER_SHA256', 'MLTD_NOTIFY_CONFIG'):
                    if not os.environ.get(name):
                        try:
                            val, _ = winreg.QueryValueEx(key, name)
                            if val:
                                os.environ[name] = str(val)
                        except FileNotFoundError:
                            pass
        except OSError:
            pass


_load_windows_user_env()
LOCK_STALE_SECONDS = 12 * 3600
HARD_KINDS = {'failure', 'incompatible', 'signing'}


class PipelineError(RuntimeError):
    def __init__(self, kind: str, key: str, title: str, detail: str = ''):
        super().__init__(title)
        self.kind, self.key, self.title, self.detail = kind, key, title, detail or title


def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def sha256(path: Path) -> str:
    return release_channel.sha256(path)


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def repo_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(os.path.expandvars(value))
    return path if path.is_absolute() else ROOT / path


class Lock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - self.path.stat().st_mtime
            if age > LOCK_STALE_SECONDS:
                raise PipelineError('failure', 'lock', 'Pipeline lock is stale',
                                    f'lock older than {int(age // 3600)}h; inspect the previous run before removing it')
            return None
        os.write(fd, json.dumps({'pid': os.getpid(), 'at_utc': now()}).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def ssh_json(target: str, script: str, *args: str, timeout: int = 60) -> dict:
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', target,
               'python3 -c ' + shlex.quote(script) + ' ' + ' '.join(shlex.quote(a) for a in args)]
    proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=timeout, check=False)
    if proc.returncode:
        raise PipelineError('failure', 'nas', 'NAS query failed', (proc.stderr or proc.stdout)[-300:])
    return json.loads(proc.stdout)


PIN_SCRIPT = r'''
import json, os, sys
root, version, client = sys.argv[1:4]
release = json.load(open(root + '/manifest.json'))['releases'].get(version)
if release:
    overlay_rec = root + '/cn-version/' + version + '/PUBLISH-RECORD.json'
    if os.path.exists(overlay_rec):
        try:
            pub = json.load(open(overlay_rec))
            pub_sha = pub.get('index', {}).get('published_sha256')
            if pub_sha:
                release['pristine_index_sha256'] = release.get('index_sha256')
                release['index_sha256'] = pub_sha
            if not release.get('app_version'):
                release['app_version'] = pub.get('frozen_identity', {}).get('client')
        except Exception:
            pass
print(json.dumps({'release': release}))
'''


def tracked_local_pin(client_version: str) -> dict | None:
    """Return the tracked local-track asset pin for a client version.

    ``configs/local-track-asset-pin.json`` is the single tracked source of truth for the
    local-server track's asset identity (JP 9.0.200 + assets 1077500). The runtime launcher
    reads the same file, so a private pipeline config that disagrees is an incompatible
    finding rather than something to build around.
    """
    if not TRACKED_ASSET_PIN.is_file():
        return None
    doc = json.loads(TRACKED_ASSET_PIN.read_text(encoding='utf-8'))
    if doc.get('kind') != 'mltd-local-track-asset-pin':
        raise PipelineError('incompatible', 'local-assets',
                            f'Unexpected tracked asset pin kind: {doc.get("kind")}')
    if str(doc.get('client_version')) != str(client_version):
        return None
    return doc


def assert_tracked_local_pin(client_version: str, asset_version: str, pin: dict | None) -> None:
    """Fail closed when the local track drifts from the tracked asset pin."""
    tracked = tracked_local_pin(client_version)
    if tracked is None:
        return
    want_version = str(tracked.get('asset_version'))
    if str(asset_version) != want_version:
        raise PipelineError('incompatible', 'local-assets',
                            f'local track pins assets {asset_version}, tracked pin is {want_version} '
                            f'({TRACKED_ASSET_PIN.name})')
    if not pin:
        # A private config may omit asset_pin; the launcher still reads the tracked pin
        # and refuses to start unless the served index matches it.
        return
    for field in ('index_name', 'index_sha256'):
        got = str(pin.get(field) or '').lower()
        if not got:
            continue
        want = str(tracked.get(field) or '').lower()
        if want != got:
            raise PipelineError('incompatible', 'local-assets',
                                f'local track asset_pin.{field} {got} != tracked pin {want}')


def check_local_assets(nas: dict, client_version: str, asset_version: str, http_head,
                       pin: dict | None = None) -> dict:
    """Verify the pinned asset release.

    The NAS control path (ssh) is authoritative for ``complete/materialized``; when it is
    unreachable the failure detail still probes the *serving* path so the notification
    separates "NAS unreachable for publishing" from "pinned assets are not being served".
    """
    assert_tracked_local_pin(client_version, asset_version, pin)
    try:
        release = ssh_json(nas['ssh_target'], PIN_SCRIPT, nas['archive_root'], asset_version, client_version)['release']
    except PipelineError as exc:
        probe = 'serving-path probe skipped (no pinned index recorded)'
        if pin and pin.get('index_name'):
            url = (f"{nas['asset_host'].rstrip('/')}/cn/{asset_version}"
                   f"/production/2018/Android/{pin['index_name']}")
            try:
                status = http_head(url, nas.get('asset_host_resolve'))
            except Exception:
                status = 'error'
            probe = f'serving-path probe of the pinned index returned HTTP {status} (pin may be stale)'
        raise PipelineError('failure', 'nas',
                            f'NAS control path unreachable (ssh {nas["ssh_target"]})',
                            f'{exc.detail}; {probe}') from exc
    if pin and pin.get('index_name') and pin['index_name'] != release.get('index_name'):
        raise PipelineError('incompatible', 'local-assets',
                            f'Pinned assets {asset_version} index changed on NAS: '
                            f'config pin {pin["index_name"]} != NAS {release.get("index_name")}')
    if (pin and pin.get('index_sha256')
            and str(release.get('index_sha256') or '').lower() != str(pin['index_sha256']).lower()):
        raise PipelineError('incompatible', 'local-assets',
                            f'Pinned assets {asset_version} index hash changed on NAS: '
                            f'config pin {pin["index_sha256"]} != NAS {release.get("index_sha256")}')
    if not release or release.get('complete') is not True or release.get('materialized') is not True:
        raise PipelineError('failure', 'local-assets', f'Pinned assets {asset_version} are not complete/materialized on NAS')
    if str(release.get('app_version')) != client_version:
        raise PipelineError('incompatible', 'local-assets',
                            f'Pinned assets {asset_version} belong to app {release.get("app_version")}, not {client_version}')
    url = f"{nas['asset_host'].rstrip('/')}/cn/{asset_version}/production/2018/Android/{release['index_name']}"
    status = http_head(url, nas.get('asset_host_resolve'))
    if status != 200:
        raise PipelineError('failure', 'local-assets', f'NAS /cn/{asset_version}/ index returned HTTP {status}')
    return {'asset_version': asset_version, 'index_name': release['index_name'],
            'index_sha256': release.get('index_sha256'), 'http_status': status}


def http_head(url: str, resolve: str | None = None) -> int:
    """Return the HTTP status for ``url`` (HEAD, no redirects).

    ``resolve`` is an optional curl-style ``host:port:ip`` pin. The shared asset host is
    AAAA-only in DNS, so runners whose IPv6 path to the NAS is unavailable (or blocked)
    must pin the IPv4 endpoint; see ``nas.asset_host_resolve`` in the pipeline config.
    """
    if resolve:
        command = ['curl', '-sS', '-I', '-o', os.devnull, '-w', '%{http_code}', '--max-time', '30',
                   '--resolve', resolve, url]
        proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                              timeout=60, check=False)
        code = (proc.stdout or '').strip()
        return int(code) if proc.returncode == 0 and code.isdigit() else 0
    import requests
    session = requests.Session()
    session.trust_env = False
    try:
        return session.head(url, timeout=30, allow_redirects=False).status_code
    except requests.RequestException:
        return 0


def resolve_official_source(track: dict, run_dir: Path, mode: str, probe) -> dict:
    """Return the pipeline config to use; new Hotplay versions need a reviewed per-version config."""
    known = read_json(repo_path(track['source_manifest']))
    latest = probe(run_dir.parent / 'hotplay-cache')
    known_md5 = {k: v['provider_md5'].lower() for k, v in known['artifacts'].items()}
    latest_md5 = {k: v['provider_md5'] for k, v in latest['artifacts'].items()}
    if latest_md5 == known_md5 and latest['client_version'] == known['client_version']:
        return {'client_version': known['client_version'], 'pipeline_config': track['pipeline_config'], 'new_version': False}
    version = latest['client_version']
    reviewed = track.get('versions', {}).get(version)
    if reviewed:
        return {'client_version': version, 'pipeline_config': reviewed, 'new_version': True}
    raise PipelineError('incompatible', 'official-source',
                        f'Hotplay now serves {version} ({latest["version_code"]}); no reviewed build config',
                        f'Official track stays on {known["client_version"]}. Add a client-patch profile and '
                        f'tracks.official.versions["{version}"] after native/metadata review.')


def input_fingerprint(pipeline_config: Path) -> str:
    config = read_json(pipeline_config)
    digest = hashlib.sha256(pipeline_config.read_bytes())
    for key in ('source_manifest', 'extraction_manifest', 'builtin_content', 'bottom_bar_manifest'):
        value = config.get(key)
        if value and Path(value).is_file():
            digest.update(Path(value).read_bytes())
    # The footer manifest points at an external payload: a swapped footer must
    # invalidate the fingerprint even when the manifest file itself is unchanged.
    footer = config.get('bottom_bar_manifest')
    if footer and Path(footer).is_file():
        payload = (read_json(Path(footer)).get('patch') or {}).get('path')
        if payload and Path(payload).is_file():
            digest.update(sha256(Path(payload)).encode())
    for value in config.get('translations', []):
        if Path(value).is_file():
            digest.update(sha256(Path(value)).encode())
    return digest.hexdigest()


def build_candidate(pipeline_config: Path, out: Path) -> dict:
    missing = [n for n in SIGNING_ENV + TOOL_ENV if not os.environ.get(n)]
    if missing:
        raise PipelineError('signing', 'signing-inputs', 'Private build/signing inputs are missing on this runner',
                            'missing environment: ' + ', '.join(missing))
    proc = subprocess.run([sys.executable, '-m', 'client.apk_candidate_pipeline', '--config', str(pipeline_config),
                           '--mode', 'build', '--output-dir', str(out)], cwd=ROOT, capture_output=True,
                          text=True, encoding='utf-8', errors='replace', check=False)
    (out.parent / f'{out.name}.log').write_text(proc.stdout + proc.stderr, encoding='utf-8')
    if proc.returncode:
        raise PipelineError('failure', f'build-{out.name}', f'{out.name} candidate build failed',
                            'see failure.private.json in the run directory')
    return read_json(out / 'candidate-receipt.json')


def run_gate(track_name: str, track: dict, client_version: str, out: Path) -> Path:
    bundle = repo_path(track.get('gate_bundle'))
    if not bundle or not bundle.is_file():
        raise PipelineError('review', f'gate-{track_name}',
                            f'{track_name} track awaits release review; candidate stays candidate',
                            'no release bundle yet: surfaces, device dual-install and rollback evidence are required')
    report = out / f'gate-{track_name}.json'
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts/validate_localization_release_bundle.py'),
                           '--bundle', str(bundle), '--client-version', client_version,
                           '--asset-version', str(track['asset_version']), '--track', track_name,
                           '--report', str(report)], capture_output=True, text=True, encoding='utf-8', check=False)
    if proc.returncode:
        raise PipelineError('review', f'gate-{track_name}', f'{track_name} release gate blocked', proc.stderr.strip())
    return report


def commit_release_record(config: dict, record_path: Path) -> str:
    git = config.get('git', {})
    if not git.get('commit'):
        return 'disabled'
    paths = [str(record_path.relative_to(ROOT)).replace('\\', '/')] + list(git.get('extra_paths', []))
    def call(*args):
        return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', check=False)
    if not call('status', '--porcelain', '--', *paths).stdout.strip():
        return 'unchanged'
    staged_before = call('diff', '--cached', '--name-only').stdout.split()
    if staged_before:
        raise PipelineError('failure', 'git', 'Git index already has staged changes; refusing auto-commit')
    for step in (('add', '--', *paths), ('commit', '-m', 'chore(release): update dual-track release record', '--', *paths)):
        proc = call(*step)
        if proc.returncode:
            raise PipelineError('failure', 'git', 'Automatic release-record commit failed', proc.stderr[-300:])
    if git.get('push'):
        proc = call('push', git.get('remote', 'origin'), f"HEAD:{git.get('branch', 'main')}")
        if proc.returncode:
            raise PipelineError('failure', 'git', 'Automatic git push failed', proc.stderr[-300:])
        return 'pushed'
    return 'committed'


def process_track(name: str, track: dict, config: dict, state: dict, mode: str, run_root: Path,
                  notifier: Notifier, probe, head) -> dict:
    summary = {'track': name, 'status': 'ok'}
    track_state = state.setdefault('tracks', {}).setdefault(name, {})
    if name == 'official':
        source = resolve_official_source(track, run_root, mode, probe)
        notifier.resolve('official-source')
    else:
        source = {'client_version': track['client_version'], 'pipeline_config': track['pipeline_config'], 'new_version': False}
        summary['assets'] = check_local_assets(config['nas'], track['client_version'], str(track['asset_version']),
                                                  head, track.get('asset_pin'))
        notifier.resolve('local-assets')
        notifier.resolve('nas')
    pipeline_config = repo_path(source['pipeline_config'])
    fingerprint = input_fingerprint(pipeline_config)
    summary.update(client_version=source['client_version'], input_fingerprint=fingerprint)
    if mode == 'check':
        summary['status'] = 'checked'
        summary['build_needed'] = track_state.get('input_fingerprint') != fingerprint
        summary['build_inputs_ready'] = all(os.environ.get(n) for n in SIGNING_ENV + TOOL_ENV)
        return summary
    receipt_path = Path(track_state['receipt']) if track_state.get('receipt') else None
    if track_state.get('input_fingerprint') != fingerprint or not receipt_path or not receipt_path.is_file():
        out = run_root / now() / name
        receipt = build_candidate(pipeline_config, out)
        receipt_path = out / 'candidate-receipt.json'
        track_state.update(input_fingerprint=fingerprint, receipt=str(receipt_path),
                           delivery=str(out / 'delivery'), built_at=now())
        summary['built'] = receipt['status']
    notifier.resolve('signing-inputs')
    notifier.resolve(f'build-{name}')
    summary['receipt'] = str(receipt_path)
    if mode != 'publish':
        return summary
    publish = config.get('publish', {})
    root = config['nas'].get('release_root', release_channel.DEFAULT_ROOT)
    transport = release_channel.Transport(publish.get('transport', 'ssh'), config['nas']['ssh_target'])
    delivery = Path(track_state['delivery'])
    asset_version = str(track['asset_version'])
    receipt_sha = sha256(receipt_path)
    if publish.get('candidate', True) and track_state.get('candidate_published') != receipt_sha:
        plan = release_channel.plan_release(receipt_path, delivery, name, 'candidate', asset_version)
        summary['candidate'] = release_channel.publish(plan, transport, root)['release_id']
        track_state['candidate_published'] = receipt_sha
    gate = run_gate(name, track, source['client_version'], receipt_path.parent)
    notifier.resolve(f'gate-{name}')
    if not publish.get('release'):
        summary['status'] = 'gate-passed-release-disabled'
        return summary
    signer = os.environ.get(publish.get('release_signer_env', 'MLTD_RELEASE_SIGNER_SHA256'), '')
    plan = release_channel.plan_release(receipt_path, delivery, name, 'release', asset_version, gate, signer)
    if track_state.get('release_published') != plan['release_id']:
        result = release_channel.publish(plan, transport, root)
        track_state['release_published'] = plan['release_id']
        summary['release'] = result['release_id']
        summary['previous_release'] = (result.get('pointer') or {}).get('previous')
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', type=Path, required=True)
    ap.add_argument('--mode', choices=('check', 'build', 'publish'), default='check')
    ap.add_argument('--track', choices=('official', 'local', 'all'), default='all')
    args = ap.parse_args(argv)
    config = read_json(args.config)
    if config.get('schema_version') != 1 or config.get('kind') != 'mltd-dual-track-pipeline':
        print('unsupported pipeline config', file=sys.stderr)
        return 2
    state_path = repo_path(config.get('state_path', 'work/agents/client/dual-track-pipeline/state.json'))
    run_root = repo_path(config.get('run_root', 'build/runs/client/9.0.200/dual-track-pipeline'))
    notifier = Notifier(state_path.with_name('notify-state.json'), repo_path(config.get('notify_config')))
    state = read_json(state_path) if state_path.is_file() else {}
    summaries = []
    try:
        with Lock(state_path.with_name('pipeline.lock')) as held:
            if held is None:
                print(json.dumps({'status': 'skipped', 'reason': 'another run holds the lock'}))
                return 0
            notifier.resolve('lock')
            for name, track in config['tracks'].items():
                if not track.get('enabled', True) or args.track not in ('all', name):
                    continue
                notifier.resolve(f'unexpected-{name}')
                try:
                    summaries.append(process_track(name, track, config, state, args.mode, run_root,
                                                   notifier, hotplay_source.probe, http_head))
                except PipelineError as exc:
                    notifier.problem(exc.kind, exc.key, exc.title, exc.detail)
                    summaries.append({'track': name, 'status': exc.kind, 'key': exc.key, 'title': exc.title})
                except release_channel.TransportError as exc:
                    notifier.problem('failure', 'nas', 'NAS control path unreachable (release-channel transport)', str(exc))
                    summaries.append({'track': name, 'status': 'failure', 'key': 'nas'})
                except release_channel.ReleaseChannelError as exc:
                    notifier.problem('failure', f'release-{name}', f'{name} release channel rejected the publish', str(exc))
                    summaries.append({'track': name, 'status': 'failure', 'key': f'release-{name}'})
                except Exception as exc:  # unexpected: report class only, details stay local
                    notifier.problem('failure', f'unexpected-{name}', f'{name} track crashed', type(exc).__name__)
                    summaries.append({'track': name, 'status': 'failure', 'error_type': type(exc).__name__})
            if args.mode == 'publish' and any('release' in row for row in summaries):
                record = ROOT / 'configs/dual-track-releases.json'
                current = read_json(record) if record.is_file() else {'schema_version': 1, 'kind': 'mltd-dual-track-release-record', 'tracks': {}}
                for row in summaries:
                    if 'release' in row:
                        current['tracks'][row['track']] = {'release_id': row['release'], 'previous': row.get('previous_release'),
                                                           'client_version': row['client_version']}
                write_json(record, current)
                try:
                    summaries.append({'git': commit_release_record(config, record)})
                    notifier.resolve('git')
                except PipelineError as exc:
                    notifier.problem(exc.kind, exc.key, exc.title, exc.detail)
            state['last_run'] = {'at_utc': now(), 'mode': args.mode, 'summaries': summaries}
            write_json(state_path, state)
    except PipelineError as exc:
        notifier.problem(exc.kind, exc.key, exc.title, exc.detail)
    finally:
        notifier.save()
    print(json.dumps({'mode': args.mode, 'summaries': summaries,
                      'open_problems': sorted(notifier.state['open'])}, ensure_ascii=False, indent=2))
    hard = [k for k, v in notifier.state['open'].items() if v.get('kind') in HARD_KINDS]
    return 1 if hard else 0


if __name__ == '__main__':
    raise SystemExit(main())
