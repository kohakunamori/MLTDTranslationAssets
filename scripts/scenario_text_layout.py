"""Candidate Scenario layout policy; widths are EAW estimates, not font metrics.

Never change characters or message boundaries. Unknown categories are excluded.
54 units is an opt-in candidate ceiling, NOT a verified client limit.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from pipelines.text.translation_tokens import protected_tokens  # noqa: E402

TOKEN = re.compile(r'\{[^{}\n]+\}|%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z]|<[^<>\n]+>|\\[nrt]|\\\d{2}\\|__MLTD_[A-Za-z0-9_]+__|[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*')
NO_START = set('，。、；：？！!?;:,.）》」』】〕〉…')
NO_END = set('（《「『【〔〈(')
PUNCT = set('，。、；：？！!?;:,')
NON_BLOCKING = {'over_target', 'line_count_mismatch'}


def blocking(issues: list[str]) -> list[str]:
    """Candidate findings that are not yet safe to publish."""
    return [issue for issue in issues if issue not in NON_BLOCKING]


@dataclass(frozen=True)
class Policy:
    target: int = 50
    ceiling: int = 54
    lines: int = 2
    name_units: int = 20  # conservative candidate reservation; not verified max


def policy_for(category: str, row: dict) -> Policy | None:
    if category not in {'story', 'card', 'birth', 'dialogue', 'event-unit'}:
        return None
    key = str(row.get('item_key', ''))
    if key.endswith('_title'):
        return None
    source_lines = str(row.get('ja', '')).count('\n') + 1
    if 'synopsis' in key:
        return Policy(40, 40, 5)
    if '_chat' in str(row.get('bundle', '')):
        return Policy(34, 34, 3)
    if category in {'card', 'event-unit'}:
        return Policy()
    return Policy(lines=max(2, min(5, source_lines)))


def atoms(text: str) -> list[str]:
    result = []
    i = 0
    while i < len(text):
        match = TOKEN.match(text, i)
        if match:
            result.append(match.group()); i = match.end()
        elif unicodedata.combining(text[i]) and result:
            result[-1] += text[i]; i += 1
        else:
            result.append(text[i]); i += 1
    return result


def width(text: str, policy: Policy = Policy()) -> int:
    total = 0
    for atom in atoms(text):
        if atom.startswith('{') or atom.startswith('%') or atom.startswith('__MLTD_'):
            total += policy.name_units
        elif atom.startswith('<') or atom.startswith('\\'):
            # Unknown markup/control semantics: conservative, never zero width.
            total += sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in atom)
        else:
            total += sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in atom)
    return total


def lint(text: str, policy: Policy, source_lines: int | None = None) -> list[str]:
    issues = []
    lines = text.split('\n')
    if source_lines is not None and len(lines) != min(policy.lines, max(1, source_lines)):
        issues.append('line_count_mismatch')
    if len(lines) > policy.lines: issues.append('line_count')
    if any(width(line, policy) > policy.ceiling for line in lines): issues.append('over_ceiling')
    elif any(width(line, policy) > policy.target for line in lines): issues.append('over_target')
    if any(not line for line in lines): issues.append('empty_line')
    if any(line and line[0] in NO_START for line in lines[1:]): issues.append('kinsoku_start')
    if any(line and line[-1] in NO_END for line in lines[:-1]): issues.append('kinsoku_end')
    if '\r' in text: issues.append('carriage_return')
    return issues


def reflow(text: str, policy: Policy, preferred_lines: int | None = None) -> str:
    """DP over indivisible atoms; refuse infeasible candidates instead of truncating."""
    if '\r' in text: raise ValueError('carriage return requires manual review')
    flat = text.replace('\n', '')
    aa = atoms(flat)
    if not aa: raise ValueError('empty text')
    # Unknown control tags may change rendering; do not move them automatically.
    if any(a.startswith('<') or a.startswith('\\') for a in aa):
        raise ValueError('unknown control or markup semantics')
    prefix = [0]
    for a in aa: prefix.append(prefix[-1] + width(a, policy))
    n = len(aa)
    original_breaks = set()
    offset = 0
    for line in text.split('\n')[:-1]:
        offset += len(line); original_breaks.add(offset)
    chars = [0]
    for a in aa: chars.append(chars[-1] + len(a))
    best = None
    for count in range(1, policy.lines + 1):
        dp = {(0, 0): (0.0, [])}
        ideal = prefix[-1] / count
        for line_no in range(1, count + 1):
            for end in range(1, n + 1):
                if end < n and (aa[end][0] in NO_START or aa[end - 1][-1] in NO_END): continue
                for start in range(end - 1, -1, -1):
                    if start == end: continue
                    units = prefix[end] - prefix[start]
                    if units > policy.ceiling: break
                    previous = dp.get((line_no - 1, start))
                    if previous is None: continue
                    penalty = (units - ideal) ** 2 + max(0, units - policy.target) ** 2 * 100
                    if end < n:
                        penalty += 0 if chars[end] in original_breaks else 3 if aa[end - 1][-1] in PUNCT else 30
                    candidate = (previous[0] + penalty, previous[1] + [end])
                    old = dp.get((line_no, end))
                    if old is None or candidate[0] < old[0]: dp[line_no, end] = candidate
        candidate = dp.get((count, n))
        if candidate:
            preferred = len(text.split('\n')) if preferred_lines is None else preferred_lines
            preferred = min(policy.lines, max(1, preferred))
            score = candidate[0] + count * 10 + (0 if count == preferred else 500)
            if best is None or score < best[0]: best = (score, candidate[1])
    if best is None: raise ValueError('no token-safe layout within candidate budget')
    lines = []; start = 0
    for end in best[1]: lines.append(''.join(aa[start:end])); start = end
    result = '\n'.join(lines)
    if result.replace('\n', '') != flat: raise AssertionError('reflow changed characters')
    if any(issue not in NON_BLOCKING for issue in lint(result, policy, preferred_lines)):
        raise ValueError('candidate failed post-layout lint')
    return result


def audit(root: Path, category: str | None = None) -> list[dict]:
    records = []
    for path in sorted((root / 'locales').glob('*/*.jsonl')):
        if category and path.parent.name != category: continue
        for number, line in enumerate(path.open(encoding='utf-8'), 1):
            if not line.strip(): continue
            row = json.loads(line)
            policy = policy_for(path.parent.name, row)
            if policy is None or not row.get('zh'): continue
            issues = lint(row['zh'], policy, str(row.get('ja', '')).count('\n') + 1)
            if issues:
                records.append({'path': path.relative_to(root).as_posix(), 'line': number,
                                'item_key': row['item_key'], 'issues': issues,
                                'widths': [width(s, policy) for s in row['zh'].split('\n')],
                                'budget': policy.__dict__})
    return records


def apply_category(root: Path, category: str) -> dict:
    """Propose reflows for one category; write none unless every row is valid."""
    plans = {}
    counts = Counter()
    for path in sorted((root / 'locales' / category).glob('*.jsonl')):
        raw = path.read_bytes()
        eol = '\r\n' if b'\r\n' in raw else '\n'
        lines = raw.decode('utf-8').replace('\r\n', '\n').split('\n')
        changed = False
        for index, line in enumerate(lines):
            if not line.strip(): continue
            row = json.loads(line)
            policy = policy_for(category, row)
            if policy is None or not row.get('zh'): continue
            issues = lint(row['zh'], policy, str(row.get('ja', '')).count('\n') + 1)
            if not blocking(issues): continue
            counts['candidates'] += 1
            before = row['zh']
            try:
                after = reflow(before, policy, str(row.get('ja', '')).count('\n') + 1)
                if after.replace('\n', '') != before.replace('\n', ''):
                    raise ValueError('text changed')
                if protected_tokens(before) != protected_tokens(after):
                    raise ValueError('protected token changed')
                probe = dict(row, zh=after)
                if 'over_target' in lint(after, policy): counts['still_over_target'] += 1
                if blocking(lint(after, policy, str(row.get('ja', '')).count('\n') + 1)):
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
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    records = audit(args.root, args.category)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    counts = Counter(issue for row in records for issue in row['issues'])
    print(f'Candidate layout rows={len(records)} issues={dict(counts)}')
    if args.apply:
        if not args.category: parser.error('--apply requires --category')
        result = apply_category(args.root, args.category)
        print(f'apply category={args.category} {result}')
        return 1 if result.get('blocked') else 0
    return int(args.strict and any(blocking(r['issues']) for r in records))


if __name__ == '__main__': raise SystemExit(main())
