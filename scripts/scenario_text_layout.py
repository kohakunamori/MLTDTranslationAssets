"""Candidate Scenario layout policy; widths are EAW estimates, not font metrics.

Never change characters or message boundaries. Unknown categories are excluded.
54 units is an opt-in candidate ceiling, NOT a verified client limit.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from pipelines.core.hashing import sha256_text  # noqa: E402
from pipelines.text.scenario_text_layout import (  # noqa: E402
    NON_BLOCKING,
    NO_END,
    NO_START,
    PUNCT,
    Policy,
    TOKEN,
    atoms,
    blocking,
    infer_layout_category,
    lint,
    policy_for,
    reflow,
    resolve_row_policy,
    try_reflow_candidate,
    width,
)
from pipelines.text.translation_tokens import protected_tokens  # noqa: E402


def resolve(category: str, row: dict, entry: dict | None) -> tuple[Policy | None, str]:
    """Return the effective policy and how the exception (if any) applies.

    ``reflow_extra_lines`` binds to the Japanese source hash only: the whole
    point is to change ``zh`` newlines, so a ``zh``-bound key would go stale the
    moment the reflow succeeds.  ``exclude`` additionally binds to ``zh`` so a
    repaired translation invalidates its exemption.
    """
    base = policy_for(category, row)
    if base is None or entry is None:
        return base, 'normal'
    if entry.get('ja_sha256') not in (None, sha256_text(row.get('ja', ''))):
        return base, 'stale'
    mode = entry.get('mode', 'exclude')
    if mode == 'exclude':
        if entry.get('zh_sha256') not in (None, sha256_text(row['zh'])):
            return base, 'stale'
        return base, 'exclude'
    if mode == 'reflow_extra_lines':
        max_lines = int(entry.get('max_lines') or 0)
        if max_lines <= base.lines:
            raise ValueError(f"exception max_lines for {row.get('item_key')!r} must exceed {base.lines}")
        return replace(base, lines=max_lines), 'normal'
    raise ValueError(f'unknown exception mode {mode!r}')


def load_exceptions(path: Path | None) -> dict[tuple[str, str], dict]:
    """Explicit, hash-bound allowlist; a changed row makes its exception stale."""
    if path is None:
        return {}
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    entries = raw.get('entries') if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError('exception file must be a list or contain an entries list')
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f'exception entry must be an object: {entry!r}')
        for key in ('path', 'item_key', 'ja_sha256', 'reason'):
            if not entry.get(key):
                raise ValueError(f'exception entry missing {key}: {entry!r}')
        if entry.get('mode', 'exclude') == 'exclude' and not entry.get('zh_sha256'):
            raise ValueError(f"exclude entry needs zh_sha256: {entry!r}")
        result[(entry['path'], entry['item_key'])] = entry
    return result


def in_category(relative: str, category: str) -> bool:
    """True when a repo-relative locale path belongs to ``locales/<category>``."""
    parts = relative.split('/')
    return len(parts) >= 3 and parts[0] == 'locales' and parts[1] == category


def audit(root: Path, category: str | None = None,
          exceptions: dict[tuple[str, str], dict] | None = None) -> list[dict]:
    records = []
    exceptions = exceptions or {}
    used = set()
    for path in sorted((root / 'locales').glob('*/*.jsonl')):
        if category and path.parent.name != category: continue
        relative = path.relative_to(root).as_posix()
        for number, line in enumerate(path.open(encoding='utf-8'), 1):
            if not line.strip(): continue
            row = json.loads(line)
            entry = exceptions.get((relative, row['item_key']))
            if entry is not None:
                used.add((relative, row['item_key']))
            policy, mode = resolve(path.parent.name, row, entry)
            if policy is None or not row.get('zh'): continue
            source_lines = str(row.get('ja', '')).count('\n') + 1
            base_issues = lint(row['zh'], policy_for(path.parent.name, row), source_lines)
            if mode == 'exclude':
                if base_issues:
                    records.append({'path': relative, 'line': number, 'item_key': row['item_key'],
                                    'issues': [], 'excepted_issues': base_issues,
                                    'excepted': entry['reason'], 'widths': [],
                                    'budget': policy.__dict__})
                continue
            issues = lint(row['zh'], policy, source_lines)
            if mode == 'stale':
                issues = issues + ['stale_exception']
            if not issues: continue
            records.append({'path': relative, 'line': number, 'item_key': row['item_key'],
                            'issues': issues, 'widths': [width(s, policy) for s in row['zh'].split('\n')],
                            'budget': policy.__dict__,
                            **({'excepted': entry['reason']} if entry else {})})
    for key, entry in exceptions.items():
        if category and not in_category(key[0], category):
            # A scoped audit only owns its own category; another category's
            # entry is not stale merely because its files were not read here.
            continue
        if key not in used:
            records.append({'path': key[0], 'line': 0, 'item_key': key[1],
                            'issues': ['stale_exception'], 'widths': [],
                            'budget': {}, 'excepted': entry['reason']})
    return records


def apply_category(root: Path, category: str,
                   exceptions: dict[tuple[str, str], dict] | None = None) -> dict:
    """Propose reflows for one category; write none unless every row is valid."""
    plans = {}
    counts = Counter()
    exceptions = exceptions or {}
    for path in sorted((root / 'locales' / category).glob('*.jsonl')):
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        eol = '\r\n' if b'\r\n' in raw else '\n'
        lines = raw.decode('utf-8').replace('\r\n', '\n').split('\n')
        changed = False
        for index, line in enumerate(lines):
            if not line.strip(): continue
            row = json.loads(line)
            entry = exceptions.get((relative, row['item_key']))
            policy, mode = resolve(category, row, entry)
            if policy is None or not row.get('zh'): continue
            if mode == 'exclude':
                counts['excepted'] += 1
                continue
            if mode == 'stale':
                counts['stale_exception'] += 1
                continue
            source_lines = str(row.get('ja', '')).count('\n') + 1
            issues = lint(row['zh'], policy, source_lines)
            if not blocking(issues): continue
            counts['candidates'] += 1
            if entry is not None:
                counts['override_reflow'] += 1
            before = row['zh']
            try:
                after = reflow(before, policy, source_lines)
                if after.replace('\n', '') != before.replace('\n', ''):
                    raise ValueError('text changed')
                if protected_tokens(before) != protected_tokens(after):
                    raise ValueError('protected token changed')
                probe = dict(row, zh=after)
                if 'over_target' in lint(after, policy): counts['still_over_target'] += 1
                if blocking(lint(after, policy, source_lines)):
                    raise ValueError('post-layout lint failed')
                compact = ': ' not in line
                lines[index] = json.dumps(probe, ensure_ascii=False, separators=(',', ':') if compact else None)
                changed = True
                counts['reflowed'] += 1
            except ValueError as exc:
                counts['blocked'] += 1
                counts[f'blocked:{exc}'] += 1
        if changed:
            plans[path] = eol.join(lines).encode('utf-8')
    if counts['blocked']:
        return {'written': 0, **dict(counts), 'note': 'all-or-nothing: blocked row prevented every write'}
    for path, payload in plans.items():
        temporary = path.with_suffix(path.suffix + '.layout-tmp')
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    return {'written': len(plans), **dict(counts)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--report', type=Path)
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--category')
    parser.add_argument('--exceptions', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    exceptions = load_exceptions(args.exceptions)
    records = audit(args.root, args.category, exceptions)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    counts = Counter(issue for row in records for issue in row['issues'])
    print(f'Candidate layout rows={len(records)} issues={dict(counts)}')
    if args.apply:
        if not args.category: parser.error('--apply requires --category')
        result = apply_category(args.root, args.category, exceptions)
        print(f'apply category={args.category} {result}')
        return 1 if result.get('blocked') else 0
    return int(args.strict and any(blocking(r['issues']) for r in records))


if __name__ == '__main__': raise SystemExit(main())
