#!/usr/bin/env python3
"""Decide which official asset bundles belong to a localizable surface.

The 2026-10 outage had one root cause: the download selection was derived from
the bundles this repository *already* had, so a bundle that had never been seen
could never be selected -- the tracked set could only ever describe itself
(11,816 tracked == 11,816 known, while the official index carries 168,391
entries).  A new event's story bundles and a new song's lyrics were therefore
invisible to every automated step.

This module replaces "is it already in the repository?" with "does it match a
localizable family?"  A family is a declared, reviewed name pattern tied to a
pipeline that can actually write the surface back:

* the pattern decides *selection*, so a brand-new bundle inside a known family
  is picked up the first time the official index lists it;
* the pipeline name decides *who extracts it*, so adding a surface is an
  explicit registry edit rather than a silent widening of the download set;
* anything that matches no family is reported as ``unclassified`` -- never
  downloaded by default -- so an entirely new resource type shows up in a report
  instead of being either silently ignored or blindly fetched (154,919 of the
  168,391 official bundles are textures, audio and other non-text resources).

The registry is data, not code: ``manifests/localizable-bundle-families.json``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "manifests" / "localizable-bundle-families.json"

SCHEMA_VERSION = 1

#: Match operators a family may declare.  Kept intentionally small: a pattern
#: that needs globbing or regex would be hard to review, and reviewability is the
#: whole point of moving the selection rule out of the script.
MATCH_OPERATORS = ("exact", "prefix", "suffix", "contains")

_DIGITS = re.compile(r"\d+")


class RegistryError(ValueError):
    """The family registry is missing, malformed or self-contradictory."""


def logical_bundle_name(value: str) -> str:
    """Normalize a bundle name to the official logical form (``*.unity3d``)."""
    name = str(value).strip()
    if not name:
        return ""
    return name if name.endswith(".unity3d") else name + ".unity3d"


def family_signature(logical: str) -> str:
    """Stable family key of a logical bundle name: digits collapsed to ``#``.

    ``event_0448_story_01_jp.gtx.unity3d`` and ``event_0450_story_12_jp.gtx``
    share one signature, so the inventory can track resource *types* without
    storing 168k names.
    """
    name = logical_bundle_name(logical).casefold()
    if name.endswith(".unity3d"):
        name = name[: -len(".unity3d")]
    return _DIGITS.sub("#", name)


def load_registry(path: Path | str = DEFAULT_REGISTRY) -> dict:
    """Read and validate the localizable-family registry."""
    path = Path(path)
    if not path.is_file():
        raise RegistryError(f"localizable family registry not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RegistryError(f"{path}: expected an object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise RegistryError(
            f"{path}: schema_version {document.get('schema_version')!r} is not {SCHEMA_VERSION}"
        )
    families = document.get("families")
    if not isinstance(families, list) or not families:
        raise RegistryError(f"{path}: families[] is empty")
    seen: set[str] = set()
    for family in families:
        if not isinstance(family, dict):
            raise RegistryError(f"{path}: family entry is not an object")
        family_id = str(family.get("id", "")).strip()
        pipeline = str(family.get("pipeline", "")).strip()
        if not family_id or not pipeline:
            raise RegistryError(f"{path}: family needs both id and pipeline")
        if family_id in seen:
            raise RegistryError(f"{path}: duplicate family id {family_id!r}")
        seen.add(family_id)
        match = family.get("match")
        if not isinstance(match, dict) or not match:
            raise RegistryError(f"{path}: family {family_id!r} has no match rule")
        unknown = sorted(set(match) - set(MATCH_OPERATORS))
        if unknown:
            raise RegistryError(
                f"{path}: family {family_id!r} uses unsupported match keys {unknown}"
            )
        for operator, value in match.items():
            if not isinstance(value, str) or not value.strip():
                raise RegistryError(
                    f"{path}: family {family_id!r} has an empty {operator} pattern"
                )
    for key in ("exclude", "reviewed_unclassified"):
        value = document.get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise RegistryError(f"{path}: {key} must be a list of strings")
    return document


def _matches(match: dict, name: str) -> bool:
    lowered = name.casefold()
    for operator, pattern in match.items():
        needle = str(pattern).strip().casefold()
        if operator == "exact" and lowered != needle:
            return False
        if operator == "prefix" and not lowered.startswith(needle):
            return False
        if operator == "suffix" and not lowered.endswith(needle):
            return False
        if operator == "contains" and needle not in lowered:
            return False
    return True


def classify(registry: dict, logical: str) -> str | None:
    """Return the family id a logical bundle belongs to, or None.

    A name listed in ``exclude`` is never classified: an explicitly excluded
    bundle must not be re-admitted by adding a broader family later.
    """
    name = logical_bundle_name(logical)
    if not name:
        return None
    excluded = {str(value).casefold() for value in registry.get("exclude", [])}
    if name.casefold() in excluded:
        return None
    for family in registry.get("families", []):
        if _matches(family["match"], name):
            return str(family["id"])
    return None


def registry_families(registry: dict) -> dict[str, dict]:
    """Family id -> family definition."""
    return {str(family["id"]): family for family in registry.get("families", [])}


def families_for_pipeline(registry: dict, pipeline: str) -> set[str]:
    """Family ids whose declared pipeline is ``pipeline``.

    Selection is split by pipeline on purpose: ``refresh_latest_official_catalogue``
    may only download surfaces whose extractor it actually runs (GTX text), while
    lyric bundles need the scrobj reader.  A family that matches but belongs to a
    different pipeline must never be pulled into the wrong extractor.
    """
    wanted = str(pipeline)
    return {
        str(family["id"])
        for family in registry.get("families", [])
        if str(family.get("pipeline")) == wanted
    }


def reviewed_unclassified(registry: dict) -> set[str]:
    """Family signatures a human already reviewed and set aside.

    Matching is by prefix, so ``costume_icon`` covers the whole
    ``costume_icon_#`` branch a reader sees in a report.  Suppression only ever
    silences *reporting*; it never selects a bundle for download.
    """
    return {str(value).strip().casefold() for value in registry.get("reviewed_unclassified", [])}


def is_reviewed(signature: str, reviewed: set[str]) -> bool:
    """Whether a family signature was explicitly set aside by a human."""
    folded = str(signature).casefold()
    return any(folded.startswith(prefix) for prefix in reviewed if prefix)
