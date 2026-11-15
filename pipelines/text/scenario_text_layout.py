"""Shared Scenario/dialogue layout policy and atom-safe reflow helpers.

Widths are East Asian Width estimates, not font metrics. Never change characters
or message boundaries. Unknown categories are excluded. 54 units is an opt-in
candidate ceiling, NOT a verified client limit.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from pipelines.text.translation_tokens import protected_tokens

TOKEN = re.compile(
    r'\{[^{}\n]+\}|%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z]|<[^<>\n]+>|\\[nrt]|\\\d{2}\\|__MLTD_[A-Za-z0-9_]+__|[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*'
)
NO_START = set('，。、；：？！!?;:,.）》」』】〕〉…')
NO_END = set('（《「『【〔〈(')
PUNCT = set('，。、；：？！!?;:,')
NON_BLOCKING = {'over_target', 'line_count_mismatch'}
_KEEP_TOGETHER_WORDS = (
    '马场木实', '木实', '伊吹翼', '北上丽花', '春日未来', '最上静香', '箱崎星梨花',
    '星梨花', '木下日向', '日向', '茱莉亚', '望月杏奈', '杏奈', '百濑莉绪', '莉绪',
    '二阶堂千鹤', '千鹤', '矢吹可奈', '可奈', '永吉昴', '福田法子', '法子',
    '高山纱代子', '纱代子', '野野原茜', '高坂海美', '海美', '真壁瑞希', '瑞希',
    '德川茉莉', '茉莉', '松田亚利沙', '亚利沙', '岛原艾琳娜', '艾琳娜',
    '横山奈绪', '奈绪', '篠宫可怜', '可怜', '北泽志保', '志保', '舞浜步',
    '佐竹美奈子', '美奈子', '丰川风花', '风花', '七尾百合子', '百合子',
    '所惠美', '惠美', '田中琴叶', '琴叶', '中谷育', '天空桥朋花', '朋花',
    '艾米莉', '周防桃子', '桃子', '大神环', '宫尾美也', '美也', '白石䌷',
    '樱守歌织', '歌织', '水濑伊织', '伊织', '高槻弥生', '弥生', '秋月律子',
    '律子', '菊地真', '天海春香', '春香', '如月千早', '千早', '星井美希',
    '美希', '三浦梓', '萩原雪步', '雪步', '我那霸响', '四条贵音', '贵音',
    '双海亚美', '亚美', '双海真美', '真美', '青羽美咲', '美咲', '音无小鸟',
    '小鸟', '高木顺二朗', '黑井崇男', '诗花', '玲音', '日葵', '小此木',
    '先生', '制作人', '小姐',
)
_KEEP_TOGETHER_SPLITS = {
    (word[:k], word[k:])
    for word in _KEEP_TOGETHER_WORDS
    for k in range(1, len(word))
}
_KEEP_LEFT_MAX = max(len(left) for left, _ in _KEEP_TOGETHER_SPLITS)
_KEEP_RIGHT_MAX = max(len(right) for _, right in _KEEP_TOGETHER_SPLITS)
_TOKEN_HONORIFIC_SPLIT_RE = re.compile(
    r'\{[^{}\n]+\}(?:\n(?:先生|制作人)|先\n生|制\n作人|制作\n人)'
)


def _join_split_penalty(aa: list[str], end: int) -> int:
    """Discourage breaking inside {token}+honorific suffixes or multi-char names."""
    penalty = 0
    left = ''.join(aa[max(0, end - _KEEP_LEFT_MAX):end])
    right = ''.join(aa[end:min(len(aa), end + _KEEP_RIGHT_MAX)])
    if end >= 2 and aa[end - 2].startswith('{'):
        if (aa[end - 1], aa[end]) == ('先', '生') or (
            end + 1 < len(aa) and aa[end - 1] == '制' and ''.join(aa[end:end + 2]) == '作人'
        ):
            return 20000
    if end >= 3 and aa[end - 3].startswith('{') and aa[end - 2] == '制' and aa[end - 1] == '作' and aa[end] == '人':
        return 20000
    if aa[end - 1].startswith('{') and (right.startswith('先生') or right.startswith('制作人')):
        penalty = max(penalty, 800)
    for l_len in range(1, min(len(left), _KEEP_LEFT_MAX) + 1):
        prefix = left[-l_len:]
        for r_len in range(1, min(len(right), _KEEP_RIGHT_MAX) + 1):
            if (prefix, right[:r_len]) in _KEEP_TOGETHER_SPLITS:
                return max(penalty, 5000)
    return penalty


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
            result.append(match.group())
            i = match.end()
        elif unicodedata.combining(text[i]) and result:
            result[-1] += text[i]
            i += 1
        else:
            result.append(text[i])
            i += 1
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
            total += sum(
                0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1
                for c in atom
            )
    return total


def lint(text: str, policy: Policy, source_lines: int | None = None) -> list[str]:
    issues = []
    lines = text.split('\n')
    if source_lines is not None and len(lines) != min(policy.lines, max(1, source_lines)):
        issues.append('line_count_mismatch')
    if len(lines) > policy.lines:
        issues.append('line_count')
    if any(width(line, policy) > policy.ceiling for line in lines):
        issues.append('over_ceiling')
    elif any(width(line, policy) > policy.target for line in lines):
        issues.append('over_target')
    if any(not line for line in lines):
        issues.append('empty_line')
    if any(line and line[0] in NO_START for line in lines[1:]):
        issues.append('kinsoku_start')
    if any(line and line[-1] in NO_END for line in lines[:-1]):
        issues.append('kinsoku_end')
    if _TOKEN_HONORIFIC_SPLIT_RE.search(text):
        issues.append('token_honorific_split')
    if '\r' in text:
        issues.append('carriage_return')
    return issues


def reflow(text: str, policy: Policy, preferred_lines: int | None = None) -> str:
    """DP over indivisible atoms; refuse infeasible candidates instead of truncating."""
    if '\r' in text:
        raise ValueError('carriage return requires manual review')
    flat = text.replace('\n', '')
    aa = atoms(flat)
    if not aa:
        raise ValueError('empty text')
    # Unknown control tags may change rendering; do not move them automatically.
    if any(a.startswith('<') or a.startswith('\\') for a in aa):
        raise ValueError('unknown control or markup semantics')
    prefix = [0]
    for a in aa:
        prefix.append(prefix[-1] + width(a, policy))
    n = len(aa)
    original_breaks = set()
    offset = 0
    for line in text.split('\n')[:-1]:
        offset += len(line)
        original_breaks.add(offset)
    chars = [0]
    for a in aa:
        chars.append(chars[-1] + len(a))
    best = None
    for count in range(1, policy.lines + 1):
        dp = {(0, 0): (0.0, [])}
        ideal = prefix[-1] / count
        split_penalties = [0, *(_join_split_penalty(aa, idx) for idx in range(1, n)), 0]
        for line_no in range(1, count + 1):
            for end in range(1, n + 1):
                if end < n and (aa[end][0] in NO_START or aa[end - 1][-1] in NO_END):
                    continue
                for start in range(end - 1, -1, -1):
                    if start == end:
                        continue
                    units = prefix[end] - prefix[start]
                    if units > policy.ceiling:
                        break
                    previous = dp.get((line_no - 1, start))
                    if previous is None:
                        continue
                    penalty = (units - ideal) ** 2 + max(0, units - policy.target) ** 2 * 100
                    if end < n:
                        penalty += (
                            0 if chars[end] in original_breaks else 3 if aa[end - 1][-1] in PUNCT else 30
                        ) + split_penalties[end]
                    candidate = (previous[0] + penalty, previous[1] + [end])
                    old = dp.get((line_no, end))
                    if old is None or candidate[0] < old[0]:
                        dp[line_no, end] = candidate
        candidate = dp.get((count, n))
        if candidate:
            preferred = len(text.split('\n')) if preferred_lines is None else preferred_lines
            preferred = min(policy.lines, max(1, preferred))
            score = candidate[0] + count * 10 + (0 if count == preferred else 500)
            if best is None or score < best[0]:
                best = (score, candidate[1])
    if best is None:
        raise ValueError('no token-safe layout within candidate budget')
    lines = []
    start = 0
    for end in best[1]:
        lines.append(''.join(aa[start:end]))
        start = end
    result = '\n'.join(lines)
    if result.replace('\n', '') != flat:
        raise AssertionError('reflow changed characters')
    if any(issue not in NON_BLOCKING for issue in lint(result, policy, preferred_lines)):
        raise ValueError('candidate failed post-layout lint')
    return result


def infer_layout_category(category: str | None, row: dict) -> str:
    """Infer scenario layout category for master rows based on bundle prefix."""
    normalized = str(category or '')
    if normalized != 'master':
        return normalized
    bundle = str(row.get('bundle') or '')
    if bundle.startswith('event_unit_talk_'):
        return 'event-unit'
    if bundle.startswith('card_'):
        return 'card'
    if bundle.startswith('birth_'):
        return 'birth'
    if bundle.startswith(('event_', 'main_', 'job_', 'special_')):
        return 'story'
    return normalized


def resolve_row_policy(category: str | None, row: dict) -> tuple[Policy | None, int]:
    """Resolve candidate layout policy and source line count from either repo or queue row shape."""
    if not category:
        return None, 1
    source = str(row.get('ja', row.get('source', '')) or '')
    layout_row = {
        'ja': source,
        'item_key': str(row.get('item_key') or row.get('key') or ''),
        'bundle': str(row.get('bundle') or ''),
    }
    inferred = infer_layout_category(category, layout_row)
    return policy_for(inferred, layout_row), source.count('\n') + 1


def try_reflow_candidate(
    category: str | None,
    row: dict,
    candidate: str,
) -> tuple[str, list[str]]:
    """Return ``(possibly_reflowed_text, remaining_blocking_issues)`` without dropping tokens or characters."""
    policy, source_lines = resolve_row_policy(category, row)
    if policy is None or not candidate:
        return candidate, []
    blocking_issues = blocking(lint(candidate, policy, source_lines))
    if not blocking_issues:
        return candidate, []
    try:
        reflowed = reflow(candidate, policy, source_lines)
    except ValueError:
        return candidate, blocking_issues
    if (
        reflowed.replace('\n', '') != candidate.replace('\n', '')
        or protected_tokens(reflowed) != protected_tokens(candidate)
    ):
        return candidate, blocking_issues
    reflowed_blocking = blocking(lint(reflowed, policy, source_lines))
    if reflowed_blocking:
        return candidate, reflowed_blocking
    return reflowed, []
