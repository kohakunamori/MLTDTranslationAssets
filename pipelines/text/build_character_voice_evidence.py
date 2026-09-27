#!/usr/bin/env python3
import sys
from pathlib import Path
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))
"""Build source-backed MLTD speaker registry and official voice-style evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mltd_localize_gtx import read_jsonl, validate_translation

FULL_NAME_RE = re.compile(r"^ld_idol_fullname_(\d+)$")
SPEAKER_SUFFIX_RE = re.compile(r"(?:^|_)(?P<code>\d{3}[a-z]{3}|p)$", re.IGNORECASE)


def stable_rank(seed: str, *parts: str) -> str:
    return hashlib.sha256((seed + ":" + ":".join(parts)).encode("utf-8")).hexdigest()


def normalize_name(value: str) -> str:
    return value.replace(" ", "").replace("\u3000", "").strip()


def extract_idol_names(catalogue: list[dict]) -> dict[int, dict]:
    names: dict[int, Counter[str]] = defaultdict(Counter)
    sources: dict[int, list[dict]] = defaultdict(list)
    for row in catalogue:
        match = FULL_NAME_RE.match(str(row.get("key", "")))
        if not match:
            continue
        idol_id = int(match.group(1))
        name = normalize_name(str(row.get("source", "")))
        if not name:
            continue
        names[idol_id][name] += 1
        sources[idol_id].append({
            "logical": row.get("logical", ""),
            "key": row.get("key", ""),
            "source": row.get("source", ""),
        })

    result: dict[int, dict] = {}
    for idol_id, counter in names.items():
        # A duplicated identical value is normal. Conflicting names remain visible in evidence.
        selected, selected_count = counter.most_common(1)[0]
        result[idol_id] = {
            "idol_id": idol_id,
            "name_jp": selected,
            "name_candidates": dict(counter),
            "selected_support": selected_count,
            "source_rows": sources[idol_id][:10],
            "ambiguous_name": len(counter) != 1,
        }
    return result


def infer_speaker_code(key: str) -> str | None:
    match = SPEAKER_SUFFIX_RE.search(key)
    return match.group("code").lower() if match else None


def load_character_evidence(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    speakers = value.get("speakers", {}) if isinstance(value, dict) else {}
    if not isinstance(speakers, dict):
        raise ValueError("character evidence must contain an object field 'speakers'")
    return speakers


def _char_bigrams(text: str) -> set[str]:
    compact = "".join(text.split())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def select_official_style_samples(
    source: str,
    usage_profile: dict,
    speakers: dict[str, dict],
    max_samples: int = 2,
) -> list[dict]:
    """Return historical official style evidence only for unambiguous single-speaker use."""
    if max_samples <= 0:
        return []
    codes = usage_profile.get("speaker_codes", [])
    if not isinstance(codes, list) or len(codes) != 1:
        return []
    code = str(codes[0])
    speaker = speakers.get(code)
    if not isinstance(speaker, dict):
        return []
    samples = speaker.get("official_style_samples", [])
    if not isinstance(samples, list) or not samples:
        return []
    source_grams = _char_bigrams(source)

    def rank(sample: dict) -> tuple[float, str]:
        candidate = str(sample.get("source", ""))
        grams = _char_bigrams(candidate)
        union = source_grams | grams
        similarity = len(source_grams & grams) / len(union) if union else 0.0
        stable = stable_rank(
            "mltd-style-retrieval-v1",
            code,
            str(sample.get("logical", "")),
            str(sample.get("key", "")),
        )
        return (-similarity, stable)

    selected = sorted(
        (sample for sample in samples if isinstance(sample, dict)),
        key=rank,
    )[:max_samples]
    return [
        {
            "speaker_code": code,
            "speaker_name_jp": speaker.get("name_jp", ""),
            "source": sample.get("source", ""),
            "historical_official_zh": sample.get("official_translation", ""),
            "category": sample.get("category", ""),
            "provenance": sample.get("official_provenance", "official-legacy-zh"),
        }
        for sample in selected
    ]


def build_official_index(legacy_rows: list[dict]) -> dict[tuple[str, str], dict]:
    index: dict[tuple[str, str], dict] = {}
    conflicts: set[tuple[str, str]] = set()
    for row in legacy_rows:
        if str(row.get("status", "")) != "official_legacy":
            continue
        logical = str(row.get("current_logical", ""))
        key = str(row.get("key", ""))
        source = str(row.get("source", ""))
        translation = str(row.get("translation", ""))
        if not logical or not key or not source or not translation:
            continue
        try:
            validate_translation(source, translation)
        except Exception:
            continue
        identity = (logical, key)
        prior = index.get(identity)
        if prior is not None and (
            prior.get("source") != source or prior.get("translation") != translation
        ):
            conflicts.add(identity)
            continue
        index[identity] = row
    for identity in conflicts:
        index.pop(identity, None)
    return index


def build_registry(
    catalogue: list[dict],
    legacy_rows: list[dict],
    sample_count: int,
    seed: str,
) -> tuple[dict, dict]:
    idol_names = extract_idol_names(catalogue)
    official = build_official_index(legacy_rows)
    observed: dict[str, dict] = {}
    evidence: dict[str, list[dict]] = defaultdict(list)
    counts = Counter()

    for row in catalogue:
        key = str(row.get("key", ""))
        code = infer_speaker_code(key)
        if not code:
            continue
        logical = str(row.get("logical", ""))
        source = str(row.get("source", ""))
        if not source:
            continue

        if code == "p":
            speaker = {
                "speaker_code": "p",
                "idol_id": 404 if 404 in idol_names else None,
                "name_jp": idol_names.get(404, {}).get("name_jp", "プロデューサー"),
                "identity_source": "key_suffix:p",
            }
        else:
            idol_id = int(code[:3])
            name = idol_names.get(idol_id)
            speaker = {
                "speaker_code": code,
                "idol_id": idol_id,
                "name_jp": name.get("name_jp", "") if name else "",
                "identity_source": (
                    f"key_suffix:{code}+ld_idol_fullname_{idol_id}"
                    if name
                    else f"key_suffix:{code}"
                ),
            }

        prior = observed.get(code)
        if prior is not None and (
            prior.get("idol_id") != speaker.get("idol_id")
            or prior.get("name_jp") != speaker.get("name_jp")
        ):
            raise ValueError(f"speaker identity conflict for {code}: {prior!r} vs {speaker!r}")
        observed[code] = speaker
        counts[f"speaker:{code}"] += 1

        old = official.get((logical, key))
        if old is None:
            continue
        if str(old.get("source", "")) != source:
            continue
        evidence[code].append({
            "logical": logical,
            "key": key,
            "category": logical.split("_", 1)[0] if logical else "",
            "source": source,
            "official_translation": old.get("translation", ""),
            "official_provenance": old.get("provenance", "official-legacy-zh"),
        })

    speakers: dict[str, dict] = {}
    for code in sorted(observed):
        row = dict(observed[code])
        samples = sorted(
            evidence.get(code, []),
            key=lambda x: stable_rank(seed, code, str(x["logical"]), str(x["key"])),
        )[:sample_count]
        row["catalogue_dialogue_rows"] = counts[f"speaker:{code}"]
        row["official_evidence_available"] = len(evidence.get(code, []))
        row["official_style_samples"] = samples
        row["profile_status"] = "evidence_only"
        speakers[code] = row

    named = sum(bool(row.get("name_jp")) for row in speakers.values())
    summary = {
        "schema_version": 1,
        "catalogue_rows": len(catalogue),
        "legacy_rows": len(legacy_rows),
        "idol_name_ids": len(idol_names),
        "speaker_codes": len(speakers),
        "speaker_codes_with_name": named,
        "speaker_codes_without_name": len(speakers) - named,
        "speaker_rows": sum(counts[k] for k in counts if k.startswith("speaker:")),
        "speakers_with_official_evidence": sum(
            bool(row["official_evidence_available"]) for row in speakers.values()
        ),
        "official_evidence_rows": sum(len(rows) for rows in evidence.values()),
        "sample_count_per_speaker": sample_count,
        "seed": seed,
        "profile_status": "evidence_only_not_agent_authored",
    }
    return {
        "schema_version": 1,
        "speakers": speakers,
        "idol_names": {str(k): v for k, v in sorted(idol_names.items())},
    }, summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalogue", type=Path, required=True)
    ap.add_argument("--legacy", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--sample-count", type=int, default=24)
    ap.add_argument("--seed", default="mltd-character-evidence-v1")
    args = ap.parse_args()
    if args.sample_count <= 0:
        raise SystemExit("--sample-count must be > 0")

    registry, summary = build_registry(
        read_jsonl(args.catalogue),
        read_jsonl(args.legacy),
        args.sample_count,
        args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
