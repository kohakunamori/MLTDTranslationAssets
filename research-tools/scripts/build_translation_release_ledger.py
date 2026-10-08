#!/usr/bin/env python3
"""Build a source-bound MLTD text release ledger (docs/LOCALIZATION_RELEASE_LEDGER.md step 5).

Reuses the existing release gate ``classify()`` and adds the ledger-level rules from
the spec: frozen-universe coverage, reviewer != translator independence, input
SHA-256 pinning (abort if any input changes during the build), a manifest, and a
stratified human-audit sample.  Writes only to ``--out-dir`` (never the production
``build/localization-90200`` directory).  Never calls any API.

Modes:
  default           build ledger + needs-review queue + audit sample + manifest
  --coverage-only   read-only; print counts JSON to stdout, write nothing
  --verify-ledger   re-apply the overlay assembler's staging checks to a ledger
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import random
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipelines.text.mltd_localize_gtx import read_jsonl
from scripts.mltd_translation_quality import evaluate_row, index_unique, load_glossary
from scripts.mltd_translation_release_gate import classify, is_official, review_index, write_jsonl

SCHEMA = "mltd-translation-release-ledger/v1"
FROZEN_IDENTITY = {"client": "9.0.200", "assets": "1077100"}
PRODUCTION_DIR = REPO / "build" / "localization-90200"
GATE_PATH = REPO / "scripts" / "mltd_translation_release_gate.py"
DEFAULT_GLOSSARY = REPO / "localization" / "quality" / "glossary.json"
SURFACES = {
    # Same frozen universes as assemble_frozen1077100_overlay.expected_frozen_sources().
    "gtx": (PRODUCTION_DIR / "translation-memory.jsonl", 319640),
    "nongtx": (PRODUCTION_DIR / "machine-translation-nongtx-queue.jsonl", 12811),
}
RISK_LEVELS = ("low", "medium", "high", "critical")
AUDIT_QUOTA = {"low": 40, "medium": 40, "high": 60, "critical": 60}
AUDIT_JUDGEMENTS = {"ok", "minor", "critical"}
AUDIT_MINOR_MAX_RATIO = 0.04
DEFAULT_AUDIT_SEED = 1077100
ATTACHED_KEYS = ("release_gate", "release_reasons", "qa_verdict", "qa_issues",
                 "review", "second_review", "risk")

EXIT_OK = 0
EXIT_VERIFY_FAIL = 1
EXIT_INVALID = 2
EXIT_INPUT_CHANGED = 3


class InputChanged(RuntimeError):
    pass


# --------------------------------------------------------------------------- utils

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO).as_posix()
    except ValueError:
        return str(resolved)


def is_under_production(path: Path) -> bool:
    resolved = path.resolve()
    prod = PRODUCTION_DIR.resolve()
    return resolved == prod or prod in resolved.parents


def load_universe(path: Path, expected: int) -> tuple[dict[str, str], dict[str, dict]]:
    """Replicates the assembler's expected_frozen_sources() validation for one surface."""
    selected: dict[str, str] = {}
    rows: dict[str, dict] = {}
    for row in read_jsonl(path):
        sid = row.get("source_sha256")
        src = row.get("source")
        if (not isinstance(sid, str) or not isinstance(src, str)
                or hashlib.sha256(src.encode("utf8")).hexdigest() != sid or sid in selected):
            raise ValueError(f"invalid frozen original source queue: {path}")
        selected[sid] = src
        rows[sid] = row
    if len(selected) != expected:
        raise ValueError(f"original text universe drift: {path}: {len(selected)} != {expected}")
    return selected, rows


# --------------------------------------------------------------- identity helpers

_PROV_FIELD_RE = {
    key: re.compile(r"""['"]%s['"]\s*:\s*(?:['"]([^'"]*)['"]|None|null)""" % key)
    for key in ("provider", "model_id", "model")
}


def _norm(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_provenance(value: object) -> dict[str, str]:
    """Return {provider, model_id, model} from a dict, JSON/py-repr string, or {}."""
    data: object = value
    if isinstance(value, str):
        text = value.strip()
        data = None
        if text.startswith("{"):
            for loader in (json.loads, ast.literal_eval):
                try:
                    data = loader(text)
                    break
                except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                    continue
            if not isinstance(data, dict):
                data = {}
                for key, rx in _PROV_FIELD_RE.items():
                    match = rx.search(text)
                    if match:
                        data[key] = match.group(1) or ""
    if not isinstance(data, dict):
        return {}
    return {key: _norm(data.get(key)) for key in ("provider", "model_id", "model")}


def translator_identity(candidate: dict) -> tuple[bool, set[str], dict[str, str]]:
    """(known, normalized identity strings, parsed fields) for the candidate translator."""
    prov = parse_provenance(candidate.get("provenance"))
    provider = prov.get("provider", "")
    models = [m for m in (prov.get("model_id", ""), prov.get("model", "")) if m]
    known = bool(provider) and (bool(models) or provider.lower().startswith("deterministic:"))
    forms: set[str] = set()
    if known:
        for model in models or [""]:
            for sep in ("|", ":"):
                forms.add(f"{provider}{sep}{model}".casefold())
    return known, forms, prov


def reviewer_identity(review: dict | None) -> tuple[bool, str]:
    """Mirrors the assembler: reviewer_id, else reviewer_provenance + reviewer_model."""
    if not isinstance(review, dict):
        return False, ""
    explicit = _norm(review.get("reviewer_id"))
    if explicit:
        return True, explicit
    prov = _norm(review.get("reviewer_provenance"))
    model = _norm(review.get("reviewer_model"))
    if prov and model:
        return True, f"{prov}|{model}"
    return False, ""


def reviewer_matches_translator(review: dict, forms: set[str], prov: dict[str, str]) -> bool:
    _, ident = reviewer_identity(review)
    if ident.casefold() in forms:
        return True
    r_prov = _norm(review.get("reviewer_provenance"))
    if r_prov.lower().startswith("review:"):
        r_prov = r_prov[len("review:"):]
    r_model = _norm(review.get("reviewer_model")).casefold()
    provider = prov.get("provider", "").casefold()
    models = {m.casefold() for m in (prov.get("model_id", ""), prov.get("model", "")) if m}
    return bool(provider) and r_prov.casefold() == provider and bool(r_model) and r_model in models


def independence_reasons(candidate: dict, review: dict | None,
                         second_review: dict | None) -> list[str]:
    """NEW rule (spec v1): reviewer identity must differ from the candidate translator."""
    if is_official(candidate):
        return []
    reasons: list[str] = []
    t_known, forms, prov = translator_identity(candidate)
    for prefix, rev in (("", review), ("second_", second_review)):
        if rev is None:
            continue
        r_known, _ = reviewer_identity(rev)
        if not r_known:
            reasons.append(f"{prefix}reviewer_identity_unknown")
        elif t_known and reviewer_matches_translator(rev, forms, prov):
            reasons.append(f"{prefix}reviewer_not_independent_of_translator")
    if not t_known and (review is not None or second_review is not None):
        reasons.append("translator_identity_unknown")
    return reasons


def review_is_fresh(review: object, sid: str, source: str, translation: str) -> bool:
    return (isinstance(review, dict) and review.get("source_sha256") == sid
            and review.get("source") == source and review.get("translation") == translation)


# ------------------------------------------------------------------------ builder

class Inputs:
    """Hash every input before reading; re-hash later to detect concurrent writes."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.paths: dict[str, Path] = {}

    def pin(self, role: str, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"{role} input missing: {path}")
        self.paths[role] = path
        self.records[role] = {"path": display_path(path), "sha256": sha256_file(path), "rows": 0}

    def set_rows(self, role: str, rows: int) -> None:
        self.records[role]["rows"] = rows

    def changed(self) -> list[str]:
        out = []
        for role, path in self.paths.items():
            if not path.is_file() or sha256_file(path) != self.records[role]["sha256"]:
                out.append(role)
        return out


def load_candidates(paths: list[Path], inputs: Inputs) -> tuple[dict[str, dict], dict]:
    merged: dict[str, dict] = {}
    stats = {"candidate_conflicts": 0, "candidate_duplicates_identical": 0}
    for i, path in enumerate(paths, 1):
        role = f"candidates_{i}"
        inputs.pin(role, path)
        rows = index_unique(read_jsonl(path), role)
        inputs.set_rows(role, len(rows))
        for sid, row in rows.items():
            if sid in merged:
                if str(merged[sid].get("translation", "")) != str(row.get("translation", "")):
                    stats["candidate_conflicts"] += 1
                else:
                    stats["candidate_duplicates_identical"] += 1
                continue
            merged[sid] = row
    return merged, stats


def load_index(role: str, path: Path | None, inputs: Inputs) -> dict[str, dict]:
    if path is None:
        return {}
    inputs.pin(role, path)
    rows = read_jsonl(path)
    inputs.set_rows(role, len(rows))
    return review_index(rows)


def classify_source(sid: str, source: str, universe_row: dict, candidate: dict | None,
                    qa_row: dict | None, review: dict | None, second_review: dict | None,
                    risk: dict | None, glossary: dict) -> tuple[str, list[str], dict]:
    if candidate is None or not str(candidate.get("translation", "")).strip():
        row = {k: v for k, v in universe_row.items() if k not in ATTACHED_KEYS}
        row["release_gate"] = "missing_candidate"
        row["release_reasons"] = ["missing_candidate"]
        if risk is not None:
            row["risk"] = risk
        return "missing_candidate", ["missing_candidate"], row

    translation = str(candidate.get("translation", ""))
    official = is_official(candidate)
    fresh_review = review if review_is_fresh(review, sid, source, translation) else None
    fresh_second = second_review if review_is_fresh(second_review, sid, source, translation) else None
    stale: list[str] = []
    if not official:
        if review is not None and fresh_review is None:
            stale.append("independent_review_stale")
        if second_review is not None and fresh_second is None:
            stale.append("second_independent_review_stale")

    if qa_row is None:
        bucket, reasons = "rejected", ["deterministic_qa_missing"]
    elif ("translation" in qa_row and str(qa_row.get("translation")) != translation) or (
            "source" in qa_row and str(qa_row.get("source")) != source):
        bucket, reasons = "needs_review", ["deterministic_qa_stale"]
    else:
        bucket, reasons = classify(candidate, qa_row, fresh_review, risk, fresh_second)
        reasons = list(reasons)
        if bucket != "rejected":
            reasons.extend(stale)
        if bucket == "accepted":
            # The assembler re-runs current QA; never emit a row it would refuse.
            current = evaluate_row({"source_sha256": sid, "source": source}, candidate, glossary)
            verdict = str(current.get("qa_verdict", "")).upper()
            if verdict == "REJECT":
                bucket, reasons = "rejected", ["deterministic_qa_current_reject"]
            elif verdict != "PASS":
                bucket, reasons = "needs_review", ["deterministic_qa_current_not_pass"]
        if bucket != "rejected":
            extra = independence_reasons(candidate, fresh_review, fresh_second)
            reasons.extend(extra)
            if bucket == "accepted" and extra:
                bucket = "needs_review"

    row = {k: v for k, v in candidate.items() if k not in ATTACHED_KEYS}
    row["release_gate"] = bucket
    row["release_reasons"] = reasons
    if qa_row is not None:
        row["qa_verdict"] = qa_row.get("qa_verdict")
        row["qa_issues"] = qa_row.get("issues", [])
    if fresh_review is not None:
        row["review"] = fresh_review
    if fresh_second is not None:
        row["second_review"] = fresh_second
    if risk is not None:
        row["risk"] = risk
    return bucket, reasons, row


def risk_level(row: dict) -> str:
    level = str((row.get("risk") or {}).get("risk_level", "low")).lower()
    return level if level in RISK_LEVELS else "low"


def score_disagreement(row: dict) -> bool:
    """Spec: rows whose *two* independent reviews both PASS but score differently."""
    first, second = row.get("review"), row.get("second_review")
    if not (isinstance(first, dict) and isinstance(second, dict)):
        return False
    if str(first.get("verdict", "")).upper() != "PASS" or str(second.get("verdict", "")).upper() != "PASS":
        return False
    return first.get("scores") != second.get("scores")


def audit_sample(accepted: list[dict], seed: int) -> list[dict]:
    rng = random.Random(seed)
    strata: dict[str, list[dict]] = {level: [] for level in RISK_LEVELS}
    for row in sorted(accepted, key=lambda r: str(r["source_sha256"])):
        strata[risk_level(row)].append(row)
    picked: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for level in RISK_LEVELS:
        pool = strata[level]
        chosen = pool if len(pool) <= AUDIT_QUOTA[level] else rng.sample(pool, AUDIT_QUOTA[level])
        for row in chosen:
            picked.append((level, row))
            seen.add(row["source_sha256"])
    # Spec: every row whose two reviews PASS with differing scores joins the sample on top of the
    # 200-row quota, so it never has to compete for a stratified seat.
    for row in sorted(accepted, key=lambda r: str(r["source_sha256"])):
        if row["source_sha256"] not in seen and score_disagreement(row):
            picked.append(("score_disagreement", row))
            seen.add(row["source_sha256"])
    out = []
    for stratum, row in picked:
        item = {
            "source_sha256": row["source_sha256"],
            "source": row.get("source"),
            "translation": row.get("translation"),
            "risk_level": risk_level(row),
            "audit_stratum": stratum,
        }
        if score_disagreement(row):
            item["score_disagreement"] = True
        for key in ("examples", "occurrences"):
            if key in row:
                item[key] = row[key]
        if isinstance(row.get("review"), dict):
            item["review_scores"] = row["review"].get("scores")
        if isinstance(row.get("second_review"), dict):
            item["second_review_scores"] = row["second_review"].get("scores")
        out.append(item)
    return out


def evaluate_audit(sample: list[dict], results_path: Path | None, inputs: Inputs) -> dict:
    audit = {"status": "not_started", "sampled": len(sample), "critical_errors": 0,
             "minor_errors": 0, "judged": 0, "failed_strata": []}
    if results_path is None:
        return audit
    inputs.pin("audit_results", results_path)
    rows = read_jsonl(results_path)
    inputs.set_rows("audit_results", len(rows))
    judgements: dict[str, str] = {}
    for row in rows:
        sid = str(row.get("source_sha256", ""))
        judgement = str(row.get("judgement", "")).strip().lower()
        if not sid or judgement not in AUDIT_JUDGEMENTS:
            raise ValueError(f"invalid audit result row: {sid!r} judgement={row.get('judgement')!r}")
        if sid in judgements:
            raise ValueError(f"duplicate audit result: {sid}")
        judgements[sid] = judgement
    failed = set()
    for item in sample:
        judgement = judgements.get(item["source_sha256"])
        if judgement is None:
            continue
        audit["judged"] += 1
        if judgement == "critical":
            audit["critical_errors"] += 1
        elif judgement == "minor":
            audit["minor_errors"] += 1
        if judgement != "ok":
            failed.add(item["risk_level"])
    audit["failed_strata"] = [level for level in RISK_LEVELS if level in failed]
    if not sample:
        audit["status"] = "not_started"
    elif audit["judged"] < len(sample):
        audit["status"] = "in_progress"
    elif audit["critical_errors"] == 0 and audit["minor_errors"] <= AUDIT_MINOR_MAX_RATIO * len(sample):
        audit["status"] = "passed"
    else:
        audit["status"] = "failed"
    return audit


def build(args: argparse.Namespace,
          before_final_check: Callable[[], None] | None = None) -> int:
    surface = args.surface
    universe_path = args.universe or SURFACES[surface][0]
    expected = args.expected_count if args.expected_count is not None else SURFACES[surface][1]
    inputs = Inputs()

    inputs.pin("universe", universe_path)
    universe, universe_rows = load_universe(universe_path, expected)
    inputs.set_rows("universe", len(universe))
    glossary_path = args.glossary
    inputs.pin("glossary", glossary_path)
    glossary = load_glossary(glossary_path)
    inputs.set_rows("glossary", len(glossary.get("entries", {})))
    candidates, cand_stats = load_candidates(args.candidates, inputs)
    qa = load_index("qa", args.qa, inputs)
    reviews = load_index("reviews", args.reviews, inputs)
    second_reviews = load_index("second_reviews", args.second_reviews, inputs)
    risks = load_index("risk", args.risk, inputs)
    risk_level_counts = Counter(str(row.get("risk_level", "low")).lower() for row in risks.values())
    risk_warning = None
    if len(risks) > 1000 and risk_level_counts.get("high", 0) + risk_level_counts.get("critical", 0) == 0:
        risk_warning = ("risk index contains no high/critical rows; machine sources are expected to use the "
                        "queue-based authority build/localization-90200/translation-risk.jsonl")

    buckets: dict[str, list[dict]] = {
        "accepted": [], "needs_review": [], "rejected": [], "missing_candidate": []}
    reason_counts: Counter[str] = Counter()
    for sid, source in universe.items():
        bucket, reasons, row = classify_source(
            sid, source, universe_rows[sid], candidates.get(sid), qa.get(sid),
            reviews.get(sid), second_reviews.get(sid), risks.get(sid), glossary)
        buckets[bucket].append(row)
        reason_counts.update(reasons)

    by_risk = Counter(risk_level(row) for row in buckets["accepted"])
    counts = {
        "accepted": len(buckets["accepted"]),
        "needs_review": len(buckets["needs_review"]),
        "rejected": len(buckets["rejected"]),
        "missing_candidate": len(buckets["missing_candidate"]),
        "by_risk": {level: by_risk.get(level, 0) for level in RISK_LEVELS},
        "universe": len(universe),
        "candidates_unique": len(candidates),
        "extraneous_candidates": sum(1 for sid in candidates if sid not in universe),
        "accepted_without_risk_row": sum(1 for row in buckets["accepted"] if "risk" not in row),
        "universe_without_risk_row": sum(1 for sid in universe if sid not in risks),
        **cand_stats,
        "reasons": dict(sorted(reason_counts.items())),
    }
    complete = counts["accepted"] == expected

    if args.coverage_only:
        if before_final_check is not None:
            before_final_check()
        changed = inputs.changed()
        if changed:
            raise InputChanged(f"inputs changed during build: {changed}")
        print(json.dumps({"surface": surface, "expected_sources": expected, "complete": complete,
                          "counts": counts}, ensure_ascii=False, indent=2))
        return EXIT_OK

    seed = args.audit_sample_seed
    sample = audit_sample(buckets["accepted"], seed)
    audit = evaluate_audit(sample, args.audit_results, inputs)

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = out_dir / f".ledger-staging-{surface}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    names = {
        "ledger": f"release-ledger-{surface}.jsonl",
        "needs_review": f"needs-review-{surface}.jsonl",
        "audit_sample": f"audit-sample-{surface}.jsonl",
    }
    manifest_name = f"release-ledger-{surface}.manifest.json"
    try:
        write_jsonl(staging / names["ledger"], buckets["accepted"])
        write_jsonl(staging / names["needs_review"],
                    buckets["needs_review"] + buckets["rejected"] + buckets["missing_candidate"])
        write_jsonl(staging / names["audit_sample"], sample)
        outputs = {key: {"path": display_path(out_dir / name),
                         "sha256": sha256_file(staging / name)} for key, name in names.items()}
        manifest = {
            "schema": SCHEMA,
            "surface": surface,
            "frozen_identity": {"client": str(args.client_version), "assets": str(args.asset_version)},
            "expected_sources": expected,
            "run_id": args.run_id or out_dir.resolve().name,
            "inputs": inputs.records,
            "policy": {
                "gate": "scripts/mltd_translation_release_gate.py",
                "gate_sha256": sha256_file(GATE_PATH),
                "glossary_sha256": inputs.records["glossary"]["sha256"],
                "reviewer_must_differ_from_translator": True,
                "current_qa_rechecked": True,
                "candidate_precedence": [inputs.records[f"candidates_{i}"]["path"]
                                         for i in range(1, len(args.candidates) + 1)],
                "risk_authority": {
                    "path": display_path(args.risk) if args.risk else None,
                    "rows": len(risks),
                    "levels": {level: risk_level_counts.get(level, 0) for level in RISK_LEVELS},
                    "missing_risk_allowed": bool(args.allow_missing_risk),
                    "warning": risk_warning,
                },
            },
            "counts": counts,
            "outputs": outputs,
            "human_audit": {
                "status": audit["status"],
                "sample_path": outputs["audit_sample"]["path"],
                "results_path": display_path(args.audit_results) if args.audit_results else None,
                "sampled": audit["sampled"],
                "critical_errors": audit["critical_errors"],
                "minor_errors": audit["minor_errors"],
                "judged": audit["judged"],
                "failed_strata": audit["failed_strata"],
                "seed": seed,
                "quota": dict(AUDIT_QUOTA),
                "minor_max_ratio": AUDIT_MINOR_MAX_RATIO,
                "score_disagreement_rows": sum(1 for item in sample if item.get("score_disagreement")),
                "score_disagreement_outside_quota": sum(1 for item in sample
                                                        if item.get("audit_stratum") == "score_disagreement"),
                "score_disagreement_requires_two_passing_reviews": True,
            },
            "complete": complete,
            "release_ready": complete and audit["status"] == "passed",
        }
        (staging / manifest_name).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if before_final_check is not None:
            before_final_check()
        changed = inputs.changed()
        if changed:
            raise InputChanged(f"inputs changed during build: {changed}")
        for name in list(names.values()) + [manifest_name]:
            os.replace(staging / name, out_dir / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(json.dumps({"surface": surface, "complete": manifest["complete"],
                      "release_ready": manifest["release_ready"], "counts": counts,
                      "human_audit": manifest["human_audit"]["status"],
                      "manifest": display_path(out_dir / manifest_name)},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


# ------------------------------------------------------------------------ verify

def verify_ledger(ledger: Path, expected: dict[str, str], glossary: dict, label: str) -> dict:
    """Same checks as assemble_frozen1077100_overlay.verify_release_ledger, plus the
    reviewer-vs-translator independence rule this builder enforces."""
    seen: set[str] = set()
    if not ledger.is_file():
        raise FileNotFoundError(f"{label} accepted ledger missing: {ledger}")
    with ledger.open(encoding="utf-8-sig") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            sid = str(item.get("source_sha256", ""))
            src = str(item.get("source", ""))
            text = str(item.get("translation", ""))
            if sid in seen:
                raise ValueError(f"{label} duplicate source SHA {sid}")
            if sid not in expected or expected[sid] != src:
                raise ValueError(f"{label} invalid frozen source identity at row {lineno}: {sid}")
            if hashlib.sha256(src.encode("utf8")).hexdigest() != sid or not text.strip():
                raise ValueError(f"{label} missing/stale source or empty translation: {sid}")
            if item.get("release_gate") != "accepted" or item.get("qa_verdict") != "PASS":
                raise ValueError(f"{label} no explicit approved QA+release gate: {sid}")
            for key in ("review", "second_review"):
                review = item.get(key)
                if review is None:
                    continue
                if not review_is_fresh(review, sid, src, text):
                    raise ValueError(f"{label} stale/unrelated independent review payload: {sid}")
            if not is_official(item):
                first = item.get("review") or {}
                if not first.get("reviewer_id") and not (
                        first.get("reviewer_provenance") and first.get("reviewer_model")):
                    raise ValueError(f"{label} independent reviewer identity missing: {sid}")
            qa = evaluate_row({"source_sha256": sid, "source": src}, item, glossary)
            verdict, _ = classify(item, qa, item.get("review"), item.get("risk"),
                                  item.get("second_review"))
            if qa.get("qa_verdict") != "PASS" or verdict != "accepted":
                raise ValueError(f"{label} current QA/independent release rules did not accept {sid}")
            extra = independence_reasons(item, item.get("review"), item.get("second_review"))
            if extra:
                raise ValueError(f"{label} reviewer independence failed {sid}: {extra}")
            seen.add(sid)
    if len(seen) != len(expected):
        raise ValueError(f"{label} incomplete accepted ledger: {len(seen)}/{len(expected)}")
    return {"surface": label, "accepted_unique": len(seen), "source_bound": True,
            "current_deterministic_QA_pass": True,
            "independent_review_release_policy_verified": True,
            "reviewer_independent_of_translator": True,
            "ledger": str(ledger), "ledger_sha256": sha256_file(ledger)}


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--surface", choices=sorted(SURFACES), required=True)
    ap.add_argument("--candidates", type=Path, action="append", default=[],
                    help="candidate JSONL; repeatable, earlier files take precedence")
    ap.add_argument("--qa", type=Path, help="mltd_translation_quality.py output JSONL")
    ap.add_argument("--reviews", type=Path)
    ap.add_argument("--second-reviews", type=Path)
    ap.add_argument("--risk", type=Path, help="classify_translation_risk.py output JSONL")
    ap.add_argument("--allow-missing-risk", action="store_true",
                    help="allow building without --risk; every row then routes as low, so the "
                         "high/critical policies (strict scores, second independent review) are skipped")
    ap.add_argument("--universe", type=Path, help="override frozen source universe JSONL")
    ap.add_argument("--expected-count", type=int, help="override expected universe size")
    ap.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY)
    ap.add_argument("--out-dir", type=Path,
                    help="run directory, e.g. build/runs/text-localization/9.0.200/<run-id>")
    ap.add_argument("--run-id")
    ap.add_argument("--client-version", default=FROZEN_IDENTITY["client"],
                    help="client identity recorded in the ledger manifest (default: legacy frozen client)")
    ap.add_argument("--asset-version", default=FROZEN_IDENTITY["assets"],
                    help="asset identity recorded in the ledger manifest; pin 1077500 for the current local track")
    ap.add_argument("--audit-sample-seed", type=int, default=DEFAULT_AUDIT_SEED)
    ap.add_argument("--audit-results", type=Path,
                    help="JSONL of {source_sha256, judgement: ok|minor|critical}")
    ap.add_argument("--coverage-only", action="store_true",
                    help="read-only: print counts JSON, write nothing")
    ap.add_argument("--verify-ledger", type=Path,
                    help="verify an existing ledger with the assembler's staging checks")
    return ap


def main(argv: list[str] | None = None,
         before_final_check: Callable[[], None] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        if args.verify_ledger is not None:
            universe_path = args.universe or SURFACES[args.surface][0]
            expected = (args.expected_count if args.expected_count is not None
                        else SURFACES[args.surface][1])
            universe, _ = load_universe(universe_path, expected)
            glossary = load_glossary(args.glossary)
            try:
                result = verify_ledger(args.verify_ledger, universe, glossary, args.surface)
            except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
                print(f"FAIL: {exc}")
                return EXIT_VERIFY_FAIL
            print("PASS")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return EXIT_OK
        if not args.candidates:
            ap.error("--candidates is required")
        if args.qa is None:
            ap.error("--qa is required")
        if args.risk is None and not args.allow_missing_risk and not args.coverage_only:
            ap.error("--risk is required: without a risk index every row falls back to risk_level=low and the "
                     "high/critical policies (strict scores, second independent review) are silently skipped; "
                     "pass --risk build/localization-90200/translation-risk.jsonl (queue-based authority) "
                     "or --allow-missing-risk to accept low-only routing explicitly")
        if not args.coverage_only:
            if args.out_dir is None:
                ap.error("--out-dir is required unless --coverage-only/--verify-ledger")
            if is_under_production(args.out_dir):
                print(f"refusing to write under production directory: {args.out_dir}",
                      file=sys.stderr)
                return EXIT_INVALID
        return build(args, before_final_check)
    except InputChanged as exc:
        print(f"ABORT: {exc}; no outputs kept", file=sys.stderr)
        return EXIT_INPUT_CHANGED
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
