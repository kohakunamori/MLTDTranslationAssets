#!/usr/bin/env python3
"""Decide which published bundles a rebuild may reuse instead of regenerating.

The generated release is content-addressed and a pure function of its inputs, so
a rebuild only has to regenerate the bundles whose inputs actually moved.  This
module answers that one question for one asset version, and it is deliberately
fail-closed: every condition it cannot *prove* answers "regenerate", and the
cheap wrong answer (regenerating a bundle that did not change) only costs time.

What makes a bundle reusable
----------------------------
* the previous release is readable, successful, for the same asset version, and
  was written by a builder that left a ``reuse`` record this module understands;
* that record says the whole release resolved every source key through the key's
  own source-bound row (see "translation memory" below);
* no release input outside ``locales/`` changed since the commit the previous
  release was built from -- a changed writer, schema, manifest or image input can
  change any bundle's bytes, so a change there disables reuse for the whole build;
* every ``locales/`` file that feeds the bundle is unchanged since that commit,
  including the working tree (``promote_merged_locales.py`` rewrites locales
  before the build, so "unchanged in HEAD" is not enough);
* the official object the bundle was built from is still the one the catalogue
  names for it: the runtime path is the catalogue's own content address, and a
  renamed object means different bytes;
* the published object is still in the store, where the store re-hashes it before
  the entry is accepted.

Translation memory is the one subtle input
------------------------------------------
``build-overlay`` resolves a key that has no usable source-bound row of its own
through a *global* table of accepted translations keyed by source text.  That
makes a bundle's bytes depend on rows belonging to other bundles, which is
exactly what a per-bundle decision cannot see.  When the previous release
recorded that every key resolved through its own row, no bundle consulted that
table, so a bundle whose own rows and official object are unchanged cannot be
affected by another bundle's edit -- and the argument holds inductively for a
reused bundle that was itself published by an incremental build.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

#: Schema of the ``reuse`` block this module reads and the builder writes.
REUSE_SCHEMA = 1
#: Manifest key carrying that block.
REUSE_KEY = "reuse"

#: The tree whose changes are decided per bundle.
LOCALE_TREE = "locales"
#: Release inputs that are not per-bundle.  A change to any of them can change
#: any bundle's bytes, so one change here falls back to a full rebuild.
#:
#: ``scripts`` and ``pipelines`` are here because they *write* the bundles: a
#: changed writer must regenerate every bundle, however little the translations
#: moved.  ``should_build_release`` fingerprints a narrower script list, but that
#: list only decides *whether* to build; this one decides whether bytes may be
#: carried over, and it has to cover the code that produces them.
#:
#: ``lyrics`` is deliberately absent: the lyric surface is a separate set of
#: bundles that this release rebuilds on every run, so a lyric edit cannot change
#: a text bundle's bytes.
GLOBAL_INPUT_TREES = ("manifests", "pipelines", "schema", "images", "scripts")
#: Files the build itself rewrites on every run, so they always differ between
#: two builds and must never be read as an input change.  Deliberately identical
#: to ``should_build_release.DERIVED_OUTPUTS``: two gates that disagree about
#: what counts as an input would make one of them wrong.
#: The lyric library: ``lyrics/songs/<bundle>.jsonl`` is the only input a lyric
#: bundle has of its own, the way a locale file is for a text bundle.
LYRIC_TREE = "lyrics"

DERIVED_OUTPUTS = ("manifests/portal-resource-manifest.json",)

HEX64_RE = re.compile(r"[0-9a-f]{64}")
RUNTIME_PREFIX = "production/2018/Android/"


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def changed_paths(root: Path, built_from: str, paths: tuple[str, ...]) -> set[str] | None:
    """Working-tree paths under ``paths`` that differ from ``built_from``.

    Returns ``None`` -- "cannot prove" -- when git cannot answer: an unknown or
    unreadable commit, a git failure, or a path that is no longer a plain file in
    the working tree.  A deletion or a rename cannot be shown harmless from the
    path list alone, and guessing "unchanged" there would reuse bytes whose
    source is gone, so it answers "cannot prove" and the caller rebuilds.

    ``DERIVED_OUTPUTS`` are dropped from the answer: the build rewrites them
    every run, so they always differ and are never an input.
    """
    if not built_from:
        return None
    if git(root, "cat-file", "-e", f"{built_from}^{{commit}}").returncode != 0:
        return None
    diff = git(root, "diff", "--name-only", built_from, "HEAD", "--", *paths)
    if diff.returncode != 0:
        return None
    status = git(root, "status", "--porcelain", "--", *paths)
    if status.returncode != 0:
        return None

    changed: set[str] = set()
    for line in diff.stdout.splitlines():
        name = line.strip()
        if name and name not in DERIVED_OUTPUTS:
            changed.add(name)
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        # Porcelain v1: two status columns, a space, then the path.  A rename is
        # reported as "old -> new"; the old path no longer exists, and the whole
        # point here is to refuse what cannot be proven.
        payload = line[3:].strip()
        if " -> " in payload:
            return None
        if len(payload) > 1 and payload.startswith('"') and payload.endswith('"'):
            payload = payload[1:-1]
        if not payload or payload in DERIVED_OUTPUTS:
            continue
        changed.add(payload)

    for name in sorted(changed):
        if not (root / name).is_file():
            return None
    return changed


@dataclass(frozen=True)
class Resolution:
    """How the overlay resolved the source keys it looked at.

    ``exclusive`` is the fact reuse depends on: every key answered with its own
    bundle's source-bound row, so no key consulted the global translation-memory
    table.  A release that is not exclusive is never reused as a whole, because
    nothing cheaper can tell which bundles its answers came from.
    """

    exact: int = 0
    memory: int = 0
    stale_exact: int = 0
    unresolved: int = 0

    @property
    def exclusive(self) -> bool:
        return self.memory == 0 and self.stale_exact == 0 and self.unresolved == 0

    def to_dict(self) -> dict[str, int]:
        return {"exact": self.exact, "memory": self.memory,
                "stale_exact": self.stale_exact, "unresolved": self.unresolved}

    @classmethod
    def from_overlay(cls, document: Mapping[str, Any]) -> "Resolution | None":
        """Read the counters ``build-overlay`` prints; ``None`` when absent.

        The two route counters arrived with incremental reuse, so an overlay
        written by an older revision answers ``None`` and the caller rebuilds
        rather than guessing which route an unknown key took.
        """
        if not isinstance(document, Mapping):
            return None
        try:
            exact = int(document["resolved_exact"])
            memory = int(document["resolved_memory"])
            candidates = int(document["source_candidates"])
            stale = int(document["stale_exact"])
        except (KeyError, TypeError, ValueError):
            return None
        if exact < 0 or memory < 0 or stale < 0:
            return None
        # ``source_candidates`` also counts keys that resolved to nothing; the
        # overlay has no separate counter for those, and the arithmetic is exact
        # because the three routes partition the candidate set.
        unresolved = candidates - exact - memory - stale
        if unresolved < 0:
            return None
        return cls(exact=exact, memory=memory, stale_exact=stale, unresolved=unresolved)

    @classmethod
    def from_reuse_block(cls, block: Mapping[str, Any] | None) -> "Resolution | None":
        """The counters a previous build recorded; ``None`` when unreadable.

        Parsing the block is not the same as trusting it: ``plan_reuse`` separately
        requires the recorded ``every_key_resolved_exactly`` claim, so a block
        whose claim and whose counters disagree is refused rather than believed.
        """
        if not isinstance(block, Mapping):
            return None
        if block.get("schema") != REUSE_SCHEMA:
            return None
        text = block.get("text_resolution")
        if not isinstance(text, Mapping):
            return None
        try:
            return cls(exact=int(text["exact"]), memory=int(text["memory"]),
                       stale_exact=int(text["stale_exact"]),
                       unresolved=int(text["unresolved"]))
        except (KeyError, TypeError, ValueError):
            return None


def reuse_block(*, index_sha256: str, resolution: Resolution, scope: str) -> dict[str, Any]:
    """The record a later build reads to decide what it may reuse.

    ``scope`` is ``"release"`` when the counters describe every published bundle
    and ``"regenerated"`` when they describe only the subset this run rewrote;
    either way ``every_key_resolved_exactly`` describes the release as a whole,
    because a reused bundle was itself exclusive when it was published.
    """
    return {REUSE_KEY: {
        "schema": REUSE_SCHEMA,
        "asset_index_sha256": str(index_sha256),
        "text_resolution": resolution.to_dict(),
        "text_scope": str(scope),
        "every_key_resolved_exactly": bool(resolution.exclusive),
    }}


@dataclass
class ReusePlan:
    """Which bundles a build must regenerate, and which it may publish as they are."""

    #: logical bundle -> catalogue row, for every bundle the build must regenerate.
    rebuild: dict[str, dict] = field(default_factory=dict)
    #: logical bundle -> store entry, for every bundle read back from the store.
    reusable: dict[str, dict] = field(default_factory=dict)
    #: Why reuse is off entirely, or ``"incremental"`` when it is on.
    reason: str = ""
    #: Locale files that differ from the commit the previous release was built
    #: from, whether or not any bundle was declined because of them.
    changed_locale_files: tuple[str, ...] = ()
    #: Bundle count per reason it was not reused, for the build report.
    declined: dict[str, int] = field(default_factory=dict)

    @property
    def incremental(self) -> bool:
        return bool(self.reusable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "incremental" if self.incremental else "full",
            "reason": self.reason,
            "reused_bundles": len(self.reusable),
            "rebuilt_bundles": len(self.rebuild),
            "changed_locale_files": len(self.changed_locale_files),
            "not_reused": dict(sorted(self.declined.items())),
        }


def _full_rebuild(index: Mapping[str, Mapping[str, Any]], reason: str) -> ReusePlan:
    return ReusePlan(rebuild={name: dict(row) for name, row in index.items()}, reason=reason)


def _reuse_refusal(previous: Mapping[str, Any], logical: str, remote: str,
                   sources: set[str], changed_locale: set[str],
                   object_file: Callable[[str], Path | None],
                   label: str = "locale") -> str | None:
    """Why ``logical`` may not be reused, or ``None`` when it may.

    ``sources`` are the repository files that fed this bundle and ``changed_locale``
    the ones that moved since the published release: the locale JSONL for a text
    bundle, the song's own ``lyrics/songs/*.jsonl`` for a lyric bundle.  ``label``
    only names them in the refusal, so a report reads honestly for both surfaces.
    """
    if previous.get("resource_kind") != "bundle":
        return "previous entry is not a text bundle"
    if previous.get("reuse_status") != "exact" or previous.get("translation_status") != "modified":
        return "previous entry was not a modified overlay"
    if str(previous.get("runtime_path") or "") != f"{RUNTIME_PREFIX}{remote}":
        return "the official object the client requests changed"
    if not sources:
        return f"no {label} file feeds this bundle"
    if sources & changed_locale:
        # Bucketed, not named: one build moves a handful of files normally, but a
        # promotion run can touch hundreds, and a reason per file would turn the
        # report into a directory listing.  `changed_locale_files` has the count.
        return f"{label} source changed"
    digest = str(previous.get("artifact_sha256") or "")
    if not HEX64_RE.fullmatch(digest):
        return "previous entry has no usable artifact digest"
    if object_file(digest) is None:
        return "the published object is no longer in the store"
    return None


def _reused_entry(previous: Mapping[str, Any], logical: str, remote: str,
                  asset_version: str) -> dict[str, Any]:
    """A store entry that republishes the previous bytes without regenerating them.

    No ``artifact_file`` is set on purpose: the store then resolves the digest
    against its own object pool and re-hashes the bytes before accepting the
    entry, which is the check that makes reuse safe rather than merely fast.
    """
    entry = {
        "logical_key": logical,
        "logical_path": str(previous.get("logical_path") or f"{RUNTIME_PREFIX}{logical}"),
        "runtime_path": f"{RUNTIME_PREFIX}{remote}",
        "resource_kind": "bundle",
        "channel": "assets",
        "asset_version": str(asset_version),
        "client_version": None,
        "source_sha256": str(previous.get("source_sha256") or ""),
        "translated_sha256": str(previous.get("translated_sha256") or ""),
        "reuse_status": "exact",
        "translation_status": "modified",
        "artifact_sha256": str(previous.get("artifact_sha256") or ""),
        "object_path": str(previous.get("object_path") or ""),
    }
    return entry


def _carried_over_release(*, root: Path, manifest: Mapping[str, Any] | None,
                          asset_version: Any, index_sha256: str
                          ) -> tuple[str | None, dict[str, Mapping[str, Any]], str]:
    """The gates that decide whether *anything* of the published release may be reused.

    Returns ``(reason, entries_by_logical_key, built_from)``.  A ``reason`` means the
    whole surface is rebuilt, and the caller must not carry over a single bundle:
    these are the conditions under which the previous release cannot prove what its
    own bytes contain, so its objects are evidence of nothing.

    Both surfaces ask the same questions here, because both are published by the
    same build: the same official catalogue fed them, the same writer produced
    them, and the same store holds their objects.
    """
    if not isinstance(manifest, Mapping):
        return "no published release to reuse for this asset version", {}, ""
    if manifest.get("build_status") != "success":
        return "the published release is not a successful build", {}, ""
    if str(manifest.get("asset_version") or "") != str(asset_version):
        return "the published release tracks another asset version", {}, ""

    block = manifest.get(REUSE_KEY)
    resolution = Resolution.from_reuse_block(block)
    if resolution is None:
        return ("the published release carries no reuse record this builder understands",
                {}, "")
    if block.get("every_key_resolved_exactly") is not True:
        return ("the published release does not claim that every key resolved through "
                "its own row", {}, "")
    if not resolution.exclusive:
        return ("the published release answered some keys without their own row, so a "
                "bundle's bytes may depend on another bundle's rows", {}, "")

    recorded_index = str(block.get("asset_index_sha256") or "")
    if recorded_index != str(index_sha256):
        return "the official catalogue changed since the published release", {}, ""

    built_from = str(manifest.get("translation_commit") or "")
    changed_global = changed_paths(root, built_from, GLOBAL_INPUT_TREES)
    if changed_global is None:
        return ("cannot tell whether the release inputs outside locales/ changed", {}, "")
    if changed_global:
        listed = ", ".join(sorted(changed_global)[:3])
        return f"release inputs outside locales/ changed: {listed}", {}, ""

    entries: dict[str, Mapping[str, Any]] = {}
    for item in manifest.get("entries") or []:
        if isinstance(item, Mapping) and isinstance(item.get("logical_key"), str):
            entries.setdefault(str(item["logical_key"]), item)
    return None, entries, built_from


def plan_reuse(*, root: Path, manifest: Mapping[str, Any] | None, asset_version: Any,
               index: Mapping[str, Mapping[str, Any]],
               bundle_sources: Mapping[str, set[str]], index_sha256: str,
               object_file: Callable[[str], Path | None]) -> ReusePlan:
    """Split ``index`` into the bundles to regenerate and the bundles to reuse.

    ``manifest`` is the release the store already holds for ``asset_version`` (or
    ``None`` when there is none, which is the first build of a version).
    ``bundle_sources`` maps each logical bundle to the locale files that fed it,
    as the current scan saw them.
    """
    reason, entries, built_from = _carried_over_release(
        root=root, manifest=manifest, asset_version=asset_version, index_sha256=index_sha256)
    if reason is not None:
        return _full_rebuild(index, reason)

    changed_locale = changed_paths(root, built_from, (LOCALE_TREE,))
    if changed_locale is None:
        return _full_rebuild(index, "cannot tell which locale files changed")

    plan = ReusePlan(reason="incremental",
                     changed_locale_files=tuple(sorted(changed_locale)))
    declined: dict[str, int] = {}
    for logical, row in index.items():
        previous = entries.get(logical)
        if previous is None:
            reason = "not in the published release"
        else:
            reason = _reuse_refusal(previous, logical, str(row["remote"]),
                                    set(bundle_sources.get(logical) or ()),
                                    changed_locale, object_file)
        if reason is None:
            plan.reusable[logical] = _reused_entry(previous, logical, str(row["remote"]),
                                                   str(asset_version))
        else:
            declined[reason] = declined.get(reason, 0) + 1
            plan.rebuild[logical] = dict(row)
    plan.declined = declined
    return plan


def lyric_sources(lyrics_root: Path) -> dict[str, set[str]]:
    """Each song bundle's own lyric file, in the names the release publishes.

    ``lyrics/songs/<bundle>.jsonl`` is the only input a lyric bundle has that is
    not shared with the whole release, so it is what a per-song reuse decision
    turns on.
    """
    songs = Path(lyrics_root) / "songs"
    if not songs.is_dir():
        return {}
    return {
        path.name[: -len(".jsonl")]: {f"{LYRIC_TREE}/songs/{path.name}"}
        for path in sorted(songs.glob("*.jsonl"))
    }


def plan_lyric_reuse(*, root: Path, manifest: Mapping[str, Any] | None,
                     asset_version: Any, bundles: Mapping[str, Mapping[str, Any]],
                     index_sha256: str,
                     object_file: Callable[[str], Path | None]) -> ReusePlan:
    """Split the lyric bundles into the songs to patch and the songs to carry over.

    ``bundles`` maps every song the library can patch today to its official
    catalogue row (``{"remote": ...}``), which is the same shape ``plan_reuse``
    takes for the text surface, so the two decisions read alike and refuse for the
    same stated reasons.  A song left out of ``rebuild`` is not downloaded and not
    rewritten: its bytes are the ones the published release already carries.
    """
    reason, entries, built_from = _carried_over_release(
        root=root, manifest=manifest, asset_version=asset_version, index_sha256=index_sha256)
    if reason is not None:
        return _full_rebuild(bundles, reason)

    changed_lyrics = changed_paths(root, built_from, (LYRIC_TREE,))
    if changed_lyrics is None:
        return _full_rebuild(bundles, "cannot tell which lyric files changed")

    sources = lyric_sources(root / LYRIC_TREE)
    plan = ReusePlan(reason="incremental",
                     changed_locale_files=tuple(sorted(changed_lyrics)))
    declined: dict[str, int] = {}
    for logical in sorted(bundles):
        row = bundles[logical]
        previous = entries.get(logical)
        if previous is None:
            why = "not in the published release"
        else:
            why = _reuse_refusal(previous, logical, str(row["remote"]),
                                 set(sources.get(logical) or ()), changed_lyrics,
                                 object_file, label="lyric")
        if why is None:
            plan.reusable[logical] = _reused_entry(previous, logical, str(row["remote"]),
                                                   str(asset_version))
        else:
            declined[why] = declined.get(why, 0) + 1
            plan.rebuild[logical] = dict(row)
    plan.declined = declined
    return plan
