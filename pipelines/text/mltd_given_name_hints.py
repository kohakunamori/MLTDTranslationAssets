"""Scoped, source-gated given-name spelling hints for translation prompts.

Hints are advisory prompt text only.  They never mask, replace, or restore
names, never approve a translation, and never add voice or personality data.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

DEFAULT_GIVEN_NAME_HINTS_PATH = Path("glossary/given-name-hints.json")
REVIEW_BASIS = "project_evidence+agent_review_not_human_reviewed"

_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_TYPED_KEY_RE = re.compile(r"^ld_idol_firstname_(\d+)$")
_PARTICLES = frozenset("とものにはがをへでやかねよさぞぜ")

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "kind",
    "status",
    "policy",
    "honorifics",
    "entries",
}
_POLICY_FIELDS = {
    "application",
    "never_mask",
    "never_restore_or_replace",
    "never_translate_honorific_by_fixed_rule",
    "never_add_persona",
    "typed_record_gate_excludes_mail_reply",
}
_ENTRY_FIELDS = {
    "source_form",
    "target",
    "idol_id",
    "typed_record_key",
    "canonical_source",
    "canonical_target",
    "review_basis",
    "evidence",
}


@dataclass(frozen=True)
class GivenNameHint:
    source_form: str
    target: str
    idol_id: int
    typed_record_key: str
    canonical_source: str
    canonical_target: str
    review_basis: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class GivenNameHints:
    entries: tuple[GivenNameHint, ...]
    honorifics: tuple[str, ...]
    sha256: str
    path: str
    by_typed_key: Mapping[str, GivenNameHint]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{label} fields mismatch: missing={missing} extra={extra}")


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _validate_document(doc: Any, path: Path) -> tuple[tuple[GivenNameHint, ...], tuple[str, ...]]:
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a JSON object")
    _require_exact_fields(doc, _TOP_LEVEL_FIELDS, f"{path}: top level")
    if doc.get("schema_version") != 1:
        raise ValueError(f"{path}: schema_version must be 1")
    if doc.get("kind") != "mltd-scoped-given-name-hints":
        raise ValueError(f"{path}: unsupported kind")
    if doc.get("status") != "project_evidence_and_agent_review_not_human_reviewed":
        raise ValueError(f"{path}: status must not claim human review or omit agent review")

    policy = doc.get("policy")
    if not isinstance(policy, dict):
        raise ValueError(f"{path}: policy must be an object")
    _require_exact_fields(policy, _POLICY_FIELDS, f"{path}: policy")
    if policy.get("application") != "per_row_prompt_hint_only":
        raise ValueError(f"{path}: policy.application must be per_row_prompt_hint_only")
    for field in (
        "never_mask",
        "never_restore_or_replace",
        "never_translate_honorific_by_fixed_rule",
        "never_add_persona",
        "typed_record_gate_excludes_mail_reply",
    ):
        if policy.get(field) is not True:
            raise ValueError(f"{path}: policy.{field} must be true")

    raw_honorifics = doc.get("honorifics")
    if not isinstance(raw_honorifics, list) or not raw_honorifics:
        raise ValueError(f"{path}: honorifics must be a non-empty list")
    honorifics: list[str] = []
    for index, value in enumerate(raw_honorifics):
        honorific = _require_text(value, f"{path}: honorifics[{index}]")
        if honorific in honorifics:
            raise ValueError(f"{path}: duplicate honorific {honorific!r}")
        honorifics.append(honorific)

    raw_entries = doc.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError(f"{path}: entries must be a non-empty list")
    entries: list[GivenNameHint] = []
    source_forms: set[str] = set()
    idol_ids: set[int] = set()
    typed_keys: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: entries[{index}] must be an object")
        _require_exact_fields(raw, _ENTRY_FIELDS, f"{path}: entries[{index}]")
        source_form = _require_text(raw.get("source_form"), f"{path}: entries[{index}].source_form")
        target = _require_text(raw.get("target"), f"{path}: entries[{index}].target")
        idol_id = raw.get("idol_id")
        if type(idol_id) is not int or idol_id <= 0:
            raise ValueError(f"{path}: entries[{index}].idol_id must be a positive integer")
        typed_record_key = _require_text(
            raw.get("typed_record_key"), f"{path}: entries[{index}].typed_record_key"
        )
        canonical_source = _require_text(
            raw.get("canonical_source"), f"{path}: entries[{index}].canonical_source"
        )
        canonical_target = _require_text(
            raw.get("canonical_target"), f"{path}: entries[{index}].canonical_target"
        )
        review_basis = raw.get("review_basis")
        if review_basis != REVIEW_BASIS:
            raise ValueError(
                f"{path}: entries[{index}].review_basis must be {REVIEW_BASIS!r}"
            )
        raw_evidence = raw.get("evidence")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise ValueError(f"{path}: entries[{index}].evidence must be a non-empty list")
        evidence = tuple(
            _require_text(value, f"{path}: entries[{index}].evidence[{pos}]")
            for pos, value in enumerate(raw_evidence)
        )
        if not canonical_source.endswith(source_form):
            raise ValueError(
                f"{path}: canonical_source must end with source_form for {source_form!r}"
            )
        if not canonical_target.endswith(target):
            raise ValueError(
                f"{path}: canonical_target must end with target for {source_form!r}"
            )
        if source_form in source_forms:
            raise ValueError(f"{path}: duplicate source_form {source_form!r}")
        if idol_id in idol_ids:
            raise ValueError(f"{path}: duplicate idol_id {idol_id}")
        if typed_record_key in typed_keys:
            raise ValueError(f"{path}: duplicate typed_record_key {typed_record_key!r}")
        source_forms.add(source_form)
        idol_ids.add(idol_id)
        typed_keys.add(typed_record_key)
        entries.append(
            GivenNameHint(
                source_form=source_form,
                target=target,
                idol_id=idol_id,
                typed_record_key=typed_record_key,
                canonical_source=canonical_source,
                canonical_target=canonical_target,
                review_basis=review_basis,
                evidence=evidence,
            )
        )
    return tuple(entries), tuple(honorifics)


def load_given_name_hints(path: str | Path) -> GivenNameHints:
    """Load and strictly validate one hint authority file.

    This explicit loader always rereads the file, which keeps test fixtures and
    deliberate tooling reloads honest.  Production uses the cached default
    loader below and therefore reads at most once per path per process.
    """
    hint_path = Path(path)
    raw = hint_path.read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{hint_path}: invalid UTF-8 JSON: {exc}") from exc
    entries, honorifics = _validate_document(doc, hint_path)
    by_typed_key: dict[str, GivenNameHint] = {}
    for entry in entries:
        for key in (entry.typed_record_key, f"ld_idol_firstname_{entry.idol_id}"):
            existing = by_typed_key.get(key)
            if existing is not None and existing != entry:
                raise ValueError(f"{hint_path}: conflicting typed key {key!r}")
            by_typed_key[key] = entry
    return GivenNameHints(
        entries=entries,
        honorifics=honorifics,
        sha256=hashlib.sha256(raw).hexdigest(),
        path=str(hint_path),
        by_typed_key=MappingProxyType(by_typed_key),
    )


@lru_cache(maxsize=4)
def _load_cached(resolved_path: str) -> GivenNameHints:
    return load_given_name_hints(resolved_path)


def load_default_given_name_hints(
    path: str | Path = DEFAULT_GIVEN_NAME_HINTS_PATH,
) -> GivenNameHints:
    """Return the immutable per-process production index for ``path``."""
    return _load_cached(str(Path(path).resolve()))


def _is_kana(character: str) -> bool:
    return bool(_KANA_RE.fullmatch(character))


def _has_compound_prefix(text: str, start: int) -> bool:
    """Reject kanji or multi-kana word-internal prefixes before a given name."""
    if start == 0:
        return False
    previous = text[start - 1]
    if previous.isspace():
        return False
    if _is_kana(previous):
        run_start = start
        while run_start > 0 and _is_kana(text[run_start - 1]):
            run_start -= 1
        run = text[run_start:start]
        return not (len(run) == 1 and run in _PARTICLES)
    return previous.isalnum()


def _select_given_name_hints(
    row: Mapping[str, Any],
    masked_source: str,
    hints: GivenNameHints | None,
) -> tuple[GivenNameHint, ...]:
    """Select hints from masked SOURCE only; context and speaker metadata are ignored."""
    if hints is None or not masked_source:
        return ()
    surface = str(row.get("surface_hint", ""))
    selected: list[GivenNameHint] = []
    seen: set[str] = set()

    def add(entry: GivenNameHint) -> None:
        if entry.source_form not in seen:
            seen.add(entry.source_form)
            selected.append(entry)

    if surface != "MAIL_REPLY":
        exact_source = masked_source.strip()
        typed_entry = _typed_entry_for_row(row, hints, exact_source)
        if typed_entry is not None:
            add(typed_entry)

    for entry in hints.entries:
        start = masked_source.find(entry.source_form)
        while start >= 0:
            end = start + len(entry.source_form)
            if not _has_compound_prefix(masked_source, start) and any(
                masked_source.startswith(honorific, end)
                for honorific in hints.honorifics
            ):
                add(entry)
                break
            start = masked_source.find(entry.source_form, start + 1)
    return tuple(selected)


def _typed_entry_for_key(hints: GivenNameHints, key: str) -> GivenNameHint | None:
    """Resolve one reviewed typed-name key, with the legacy firstname alias."""
    entry = hints.by_typed_key.get(key)
    if entry is not None:
        return entry
    typed_match = _TYPED_KEY_RE.fullmatch(key)
    if typed_match is None:
        return None
    typed_id = int(typed_match.group(1))
    return next((entry for entry in hints.entries if entry.idol_id == typed_id), None)


def _typed_entry_for_row(
    row: Mapping[str, Any],
    hints: GivenNameHints,
    exact_source: str,
) -> GivenNameHint | None:
    """Apply the typed gate only when every recorded occurrence is compatible."""
    usage = row.get("usage_profile")
    if isinstance(usage, Mapping) and "name_occurrence_keys" in usage:
        raw_keys = usage.get("name_occurrence_keys")
        if not isinstance(raw_keys, list):
            return None
        if not raw_keys or not all(isinstance(key, str) and key for key in raw_keys):
            return None
        keys = list(raw_keys)
        entries: list[GivenNameHint] = []
        for occurrence_key in keys:
            entry = hints.by_typed_key.get(occurrence_key)
            if entry is None or entry.source_form != exact_source:
                return None
            entries.append(entry)
        if any(entry.idol_id != entries[0].idol_id for entry in entries[1:]):
            return None
        return entries[0]

    key = str(row.get("key", row.get("item_key", "")) or "")
    typed_entry = _typed_entry_for_key(hints, key)
    if typed_entry is not None and exact_source == typed_entry.source_form:
        return typed_entry
    return None


def applicable_given_name_hints(
    row: Mapping[str, Any],
    hints: GivenNameHints | None,
) -> tuple[GivenNameHint, ...]:
    """Select from ``row["source"]``, which callers must set to masked SOURCE."""
    return _select_given_name_hints(row, str(row.get("source", "")), hints)


def _render_given_name_hints(
    selected: tuple[GivenNameHint, ...],
    hints: GivenNameHints | None,
) -> str:
    """Render one advisory block, with the authority hash for request audit."""
    if not selected or hints is None:
        return ""
    lines = [
        "NAME_HINTS (project-reviewed given-name spelling evidence; apply only where "
        "SOURCE uses the exact form as a person's name; preserve honorific meaning "
        "from context; do not infer personality):"
    ]
    lines.extend(f"{entry.source_form} => {entry.target}" for entry in selected)
    lines.append(f"NAME_HINTS_AUTHORITY_SHA256={hints.sha256}")
    return "\n".join(lines)


def render_given_name_hints(
    row: Mapping[str, Any],
    hints: GivenNameHints | None,
) -> str:
    """Render the advisory block for a masked-SOURCE row."""
    return _render_given_name_hints(applicable_given_name_hints(row, hints), hints)


__all__ = [
    "DEFAULT_GIVEN_NAME_HINTS_PATH",
    "GivenNameHint",
    "GivenNameHints",
    "applicable_given_name_hints",
    "load_default_given_name_hints",
    "load_given_name_hints",
    "render_given_name_hints",
]
