#!/usr/bin/env python3
"""Where the client version in a release comes from: the rows, not a field.

The client axis and the assets axis are independent identities.  A release is
identified by ``asset_version`` alone; its manifest records
``client_version: null`` and keeps the client version only as
``source_client_version``, meaning "the text in this release was extracted from
client X".  The composite ``9.0.200+1077100`` is rejected outright.

That provenance belongs to the data: every locale row already carries its own
``source_client_version``, and the schema requires it.  Keeping a second copy in
``manifests/asset-version.json`` gave it the look of a version to keep in step
with the game -- on 2026-10-10 client 9.0.300 shipped while all 395,673 rows
still said 9.0.200, and the field looked stale when it was in fact correct.

So the field is gone.  Callers ask the rows:

* ``from_rows`` -- for the release build, which has already read every row;
* ``from_library`` -- for the nightly catalogue refresh, which needs the value
  before it writes the rows it is about to append.

Both fall back to ``None`` rather than inventing a value; the caller decides
whether a missing provenance is fatal.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping

LIBRARY_GLOB = "locales/*/*.jsonl"


def _value(row: Mapping[str, object]) -> str | None:
    value = row.get("source_client_version")
    return value if isinstance(value, str) and value else None


def _pick(counts: Counter) -> str | None:
    """A tie goes to the higher version: the newer capture describes the library.

    When half the rows come from 9.0.100 and half from 9.0.200, the release tells
    the truth about the text it carries by naming the newer of the two.
    """
    if not counts:
        return None
    top = max(counts.values())
    return max(value for value, count in counts.items() if count == top)


def from_rows(rows: Iterable[Mapping[str, object]]) -> tuple[str | None, Counter]:
    """The library's provenance as a majority vote, with the full tally."""
    counts: Counter = Counter(value for row in rows if (value := _value(row)))
    return _pick(counts), counts


def from_library(root: Path) -> tuple[str | None, Counter]:
    """Stream the locale library and vote; the nightly job can afford the scan."""
    counts: Counter = Counter()
    for path in sorted(root.glob(LIBRARY_GLOB)):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                value = _value(json.loads(line))
                if value:
                    counts[value] += 1
    return _pick(counts), counts
