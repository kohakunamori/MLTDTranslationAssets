#!/usr/bin/env python3
"""Validation script for the MLTD Localization GitHub Repository.

Checks:
1. Every JSONL line in locales/ matches the JSON schema and passes source SHA-256 verification,
   including the version identity: an entry carries the independent axis fields
   `asset_version` (digits only) / `client_version` (null on this axis) /
   `source_client_version` (`X.Y.Z`). The retired composite `base_version` is refused.
2. No reserved delimiters (| or ^) are present in any translation.
3. Every category directory (story, card, dialogue, birth, master) is present and non-empty.
4. Glossaries (authoritative-terms.json, idols.json) are valid JSON and contain expected terms.
5. The image manifest is valid and consistent.
6. Lyrics files are valid JSONL and match schema.

The APK built-in surfaces (bottom bar atlas, BI text, font) are deliberately
absent: they are delivered by the client repository instead.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

PROTECTED_TOKEN_RE = re.compile(
    r"\{[^{}]+\}"
    r"|%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z]"
    r"|<[^<>\r\n]+>"
    r"|\\[nrt]"
    r"|\\[0-9]{2}\\"
)
#: Placeholder classes that must survive verbatim in every surface.  Bare
#: ``<...>`` is excluded here on purpose: in lyric text it is a *display*
#: quotation mark (``<いつの間にかこんなに>`` -> ``<不知不觉间已经如此>``), so
#: comparing its text would flag three correct rows in
#: ``lyrics/songs/scrobj_homesf.unity3d.jsonl``; the bracket *balance* is checked
#: instead, which still catches a dropped engine tag such as ``<size=24>``.
NON_ANGLE_TOKEN_RE = re.compile(
    r"\{[^{}]+\}"
    r"|%[-+0 #]*\d*(?:\.\d+)?[a-zA-Z]"
    r"|\\[nrt]"
    r"|\\[0-9]{2}\\"
)
ROOT = Path(__file__).resolve().parents[1]
RESERVED_DELIMITERS = ("|", "^")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
VALID_STATUSES = {"untranslated", "pending", "accepted"}
VALID_TRANSLATION_STAGES = {"untranslated", "llm_translated", "human_translated"}
# `status` answers one question only: may this row enter the generated build?
# `translation_stage` carries the provenance, so `accepted` + `llm_translated`
# is the deliberate machine-published state produced by
# `scripts/llm_translate_untranslated.py publish`; only provenance-native
# stages are admissible there.
ACCEPTED_STAGES = {"llm_translated", "human_translated"}
CATEGORIES = ("story", "card", "dialogue", "birth", "master")

# Version identity. Client and Assets are independent release axes
# (docs: schema/entry.schema.json and the repository spec, §2):
#   asset_version         this axis's identity, digits only
#   client_version        must be null on the assets axis
#   source_client_version provenance, `<X.Y.Z>`
ASSET_VERSION_RE = re.compile(r"^[0-9]+$")
CLIENT_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().lower()


def validate_translation_tokens(source: str, translated: str) -> None:
    before = Counter(PROTECTED_TOKEN_RE.findall(source))
    after = Counter(PROTECTED_TOKEN_RE.findall(translated))
    if before != after:
        raise ValueError(f"protected token mismatch: source={dict(before)!r} translation={dict(after)!r}")


def validate_lyric_tokens(source: str, translated: str) -> None:
    """Token rule for lyric lines: placeholders verbatim, brackets balanced.

    Lyric strings use ``<...>`` as emphasis punctuation rather than as an engine
    field, so the exact-text comparison used for locale rows would reject correct
    translations.  The placeholder classes that do carry meaning are compared
    verbatim, and the number of angle brackets must be preserved so a dropped
    ``<size=24>``-style tag is still caught.
    """
    before = Counter(NON_ANGLE_TOKEN_RE.findall(source))
    after = Counter(NON_ANGLE_TOKEN_RE.findall(translated))
    if before != after:
        raise ValueError(f"protected token mismatch: source={dict(before)!r} translation={dict(after)!r}")
    if source.count("<") != translated.count("<") or source.count(">") != translated.count(">"):
        raise ValueError(
            "angle bracket count changed: "
            f"source=({source.count('<')},{source.count('>')}) "
            f"translation=({translated.count('<')},{translated.count('>')})"
        )


def validate_locales(root: Path) -> dict[str, int]:
    locales_dir = root / "locales"
    if not locales_dir.is_dir():
        print(f"ERROR: Missing locales/ directory at {locales_dir}", file=sys.stderr)
        sys.exit(1)

    counts = {"total_rows": 0, "accepted": 0, "pending": 0, "untranslated": 0, "bundles": 0}

    for cat in CATEGORIES:
        cat_dir = locales_dir / cat
        if not cat_dir.is_dir():
            print(f"ERROR: Missing category directory {cat_dir}", file=sys.stderr)
            sys.exit(1)

        jsonl_files = list(cat_dir.glob("*.jsonl"))
        if not jsonl_files:
            print(f"WARNING: No .jsonl files in {cat_dir}", file=sys.stderr)
            continue

        for jf in jsonl_files:
            counts["bundles"] += 1
            with jf.open("r", encoding="utf-8") as f:
                for line_idx, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    counts["total_rows"] += 1
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        print(f"ERROR: {jf}:{line_idx} invalid JSON: {exc}", file=sys.stderr)
                        sys.exit(1)

                    # Required keys. The version identity is the independent
                    # axis fields; a composite `base_version` is not a version
                    # and is refused below rather than tolerated here.
                    for req_key in ("asset_version", "client_version", "source_client_version",
                                    "bundle", "item_key", "source_sha256", "ja", "zh",
                                    "status", "updated_at"):
                        if req_key not in row:
                            print(f"ERROR: {jf}:{line_idx} missing key '{req_key}'", file=sys.stderr)
                            sys.exit(1)

                    # Version axis check.
                    if "base_version" in row:
                        print(
                            f"ERROR: {jf}:{line_idx} composite 'base_version' "
                            f"{row['base_version']!r} is retired; carry the independent "
                            "fields 'asset_version' + 'client_version' + 'source_client_version'",
                            file=sys.stderr,
                        )
                        sys.exit(1)

                    asset_version = row["asset_version"]
                    # `client_version` is null on this axis, and the schema admits
                    # only null there; a string would mean a row claiming both axes.
                    if not isinstance(asset_version, str) or not ASSET_VERSION_RE.match(asset_version):
                        print(
                            f"ERROR: {jf}:{line_idx} 'asset_version' must be a digits-only "
                            f"string, got {asset_version!r}",
                            file=sys.stderr,
                        )
                        sys.exit(1)

                    if row["client_version"] is not None:
                        print(
                            f"ERROR: {jf}:{line_idx} 'client_version' must be null on the assets "
                            f"axis, got {row['client_version']!r}",
                            file=sys.stderr,
                        )
                        sys.exit(1)

                    source_client_version = row["source_client_version"]
                    if not isinstance(source_client_version, str) or not CLIENT_VERSION_RE.match(source_client_version):
                        print(
                            f"ERROR: {jf}:{line_idx} 'source_client_version' must be `<X.Y.Z>`, "
                            f"got {source_client_version!r}",
                            file=sys.stderr,
                        )
                        sys.exit(1)

                    ja = row["ja"]
                    zh = row["zh"]
                    declared_sha = row["source_sha256"]
                    status = row["status"]
                    stage = row.get("translation_stage")

                    # Status check
                    if status not in VALID_STATUSES:
                        print(f"ERROR: {jf}:{line_idx} invalid status '{status}'", file=sys.stderr)
                        sys.exit(1)
                    if stage is not None:
                        if stage not in VALID_TRANSLATION_STAGES:
                            print(f"ERROR: {jf}:{line_idx} invalid translation_stage '{stage}'", file=sys.stderr)
                            sys.exit(1)
                        if status == "untranslated" and stage != "untranslated":
                            print(f"ERROR: {jf}:{line_idx} untranslated row must have translation_stage=untranslated", file=sys.stderr)
                            sys.exit(1)
                        if status == "pending" and stage != "llm_translated":
                            print(f"ERROR: {jf}:{line_idx} pending row must have translation_stage=llm_translated", file=sys.stderr)
                            sys.exit(1)
                        # An accepted row must declare where the text came from.
                        # Machine output is admitted but never relabelled as
                        # human-reviewed: the stage stays `llm_translated`.
                        if status == "accepted" and stage not in ACCEPTED_STAGES:
                            print(
                                f"ERROR: {jf}:{line_idx} accepted row must have "
                                f"translation_stage in {sorted(ACCEPTED_STAGES)}, got {stage!r}",
                                file=sys.stderr,
                            )
                            sys.exit(1)
                    counts[status] += 1

                    # Untranslated must be empty zh
                    if status == "untranslated" and zh != "":
                        print(f"ERROR: {jf}:{line_idx} status is untranslated but zh is not empty", file=sys.stderr)
                        sys.exit(1)

                    # Source hash check
                    computed_sha = sha256_text(ja)
                    if declared_sha.lower() != computed_sha:
                        print(f"ERROR: {jf}:{line_idx} source hash mismatch: declared {declared_sha} != {computed_sha}", file=sys.stderr)
                        sys.exit(1)

                    # Delimiter check
                    for d in RESERVED_DELIMITERS:
                        if d in zh:
                            print(f"ERROR: {jf}:{line_idx} translation contains illegal reserved delimiter '{d}': {zh}", file=sys.stderr)
                            sys.exit(1)

                    # Runtime format tokens are part of the source contract.
                    # A row with a missing/duplicated token must not remain
                    # `accepted`: it would make UnityFS generation unsafe.
                    if status == "accepted":
                        try:
                            validate_translation_tokens(ja, zh)
                        except ValueError as exc:
                            print(
                                f"ERROR: {jf}:{line_idx} protected-token validation failed: {exc}",
                                file=sys.stderr,
                            )
                            sys.exit(1)

    return counts


def validate_lyrics(root: Path) -> dict[str, int]:
    """Validate the song lyric library, which is no longer hand-built only.

    ``lyrics/songs/*.jsonl`` is now written by ``refresh_lyrics_catalogue.py``
    and promoted by ``llm_translate_untranslated.py publish --scope lyrics``, so
    the same source-binding and control-character rules that protect the locale
    rows must protect lyric rows too.  The derived counts in
    ``lyrics_manifest.json`` are deliberately *not* checked against the rows:
    they are regenerated from the songs, and requiring agreement would block a
    translator's one-row pull request.
    """
    songs_dir = root / "lyrics" / "songs"
    if not songs_dir.is_dir():
        print(f"WARNING: no lyrics/songs directory at {songs_dir}", file=sys.stderr)
        return {"files": 0, "total_rows": 0, "accepted": 0, "pending": 0, "untranslated": 0}

    counts = {"files": 0, "total_rows": 0, "accepted": 0, "pending": 0, "untranslated": 0}
    for path in sorted(songs_dir.glob("*.jsonl")):
        counts["files"] += 1
        bundle = path.name[: -len(".jsonl")]
        with path.open("r", encoding="utf-8") as handle:
            # Iterating the handle splits on real newlines only; a raw U+2028 in
            # a JSON string is legal and must not be treated as a line break.
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                counts["total_rows"] += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(f"ERROR: {path}:{line_no} invalid JSON: {exc}", file=sys.stderr)
                    sys.exit(1)
                for req_key in ("bundle", "index", "tick", "abs_time", "source_sha256",
                                "ja", "zh", "status", "updated_at"):
                    if req_key not in row:
                        print(f"ERROR: {path}:{line_no} missing key '{req_key}'", file=sys.stderr)
                        sys.exit(1)
                if str(row["bundle"]) != bundle:
                    print(
                        f"ERROR: {path}:{line_no} bundle '{row['bundle']}' does not match "
                        f"the file name '{bundle}'",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                declared = str(row["source_sha256"]).lower()
                actual = sha256_text(str(row["ja"]))
                if declared != actual:
                    print(
                        f"ERROR: {path}:{line_no} source_sha256 does not match ja "
                        f"({declared} != {actual})",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                status = str(row["status"])
                if status not in VALID_STATUSES:
                    print(f"ERROR: {path}:{line_no} invalid status '{status}'", file=sys.stderr)
                    sys.exit(1)
                counts[status] += 1
                translated = str(row["zh"])
                if status == "untranslated" and translated:
                    print(
                        f"ERROR: {path}:{line_no} untranslated row carries text",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if status != "untranslated" and not translated:
                    print(
                        f"ERROR: {path}:{line_no} {status} row has no translation",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                for delimiter in RESERVED_DELIMITERS:
                    if delimiter in translated:
                        print(
                            f"ERROR: {path}:{line_no} translation contains the reserved "
                            f"engine delimiter '{delimiter}'",
                            file=sys.stderr,
                        )
                        sys.exit(1)
                if translated:
                    try:
                        validate_lyric_tokens(str(row["ja"]), translated)
                    except ValueError as exc:
                        print(f"ERROR: {path}:{line_no} {exc}", file=sys.stderr)
                        sys.exit(1)
    return counts


def validate_glossary(root: Path) -> None:
    glossary_dir = root / "glossary"
    terms_file = glossary_dir / "authoritative-terms.json"
    idols_file = glossary_dir / "idols.json"

    if not terms_file.is_file():
        print(f"ERROR: Missing {terms_file}", file=sys.stderr)
        sys.exit(1)

    terms_data = json.loads(terms_file.read_text(encoding="utf-8"))
    entries = terms_data.get("entries", {})
    if len(entries) < 90:
        print(f"ERROR: authoritative-terms.json entries count {len(entries)} < 90", file=sys.stderr)
        sys.exit(1)

    if not idols_file.is_file():
        print(f"ERROR: Missing {idols_file}", file=sys.stderr)
        sys.exit(1)

    idols_data = json.loads(idols_file.read_text(encoding="utf-8"))
    idols = idols_data.get("idols", [])
    if len(idols) != 52:
        print(f"ERROR: idols.json idols count {len(idols)} != 52", file=sys.stderr)
        sys.exit(1)


def validate_manifests(root: Path) -> None:
    manifests_dir = root / "manifests"
    images_file = manifests_dir / "images.manifest.json"

    if not images_file.is_file():
        print(f"ERROR: Missing {images_file}", file=sys.stderr)
        sys.exit(1)

    images_data = json.loads(images_file.read_text(encoding="utf-8"))
    images = images_data.get("images", [])
    if len(images) < 937:
        print(f"ERROR: images.manifest.json images count {len(images)} < 937", file=sys.stderr)
        sys.exit(1)

    # Bottom-bar atlases are APK built-ins now; this repository must not carry them.
    stray = [i.get("id") for i in images if i.get("kind") == "bottom_bar_atlas"]
    if stray:
        print(
            f"ERROR: images.manifest.json still lists APK built-in atlas(es) {stray}; "
            "they belong to the client repository",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> int:
    print(f"Validating repository at: {ROOT}")
    locales_counts = validate_locales(ROOT)
    lyrics_counts = validate_lyrics(ROOT)
    validate_glossary(ROOT)
    validate_manifests(ROOT)

    print("\nRepository validation SUCCESSFUL!")
    print(f"Total locales rows: {locales_counts['total_rows']}")
    print(f"  Accepted: {locales_counts['accepted']}")
    print(f"  Pending: {locales_counts['pending']}")
    print(f"  Untranslated: {locales_counts['untranslated']}")
    print(f"  Bundles: {locales_counts['bundles']}")
    print(f"Total lyric songs: {lyrics_counts['files']} ({lyrics_counts['total_rows']} rows)")
    print(f"  Accepted: {lyrics_counts['accepted']}")
    print(f"  Pending: {lyrics_counts['pending']}")
    print(f"  Untranslated: {lyrics_counts['untranslated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
