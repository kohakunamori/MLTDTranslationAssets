# Unity3D package incremental builds

`scripts/build_generated_release.py` plans text and lyrics independently, at
logical Unity3D bundle granularity. JSONL file boundaries and unrelated CI tests
do not invalidate a bundle. `--no-reuse` still forces every writer to run.

## Text inputs

The release's `bundle_build.text` record binds each bundle to its official
catalogue content fingerprint and declared size, verified source SHA-256,
checksummed source parse cache, writer/toolchain fingerprint, and actual
replacement-set fingerprint. The source parse cache is deterministic gzip JSON
under `generated/build-inputs/text/<parser-fingerprint>/<source-sha256>.json.gz`.
It is committed with the release, so a fresh runner needs no official download
or Unity parse for unchanged text bundles. Missing or corrupt parse data is
reconstructed for that bundle; generated objects are still checked by the store.

Every run selects source-bound accepted translations against these cached
official records and builds the **complete** translation resolver. It resolves
all source keys cheaply, including memory matches, ambiguities and misses, then
compares per-bundle replacement fingerprints. Only bundles whose resulting
bytes can change enter the Unity writer. An incremental overlay receives the
complete ledger, so its memory semantics match a full overlay.

The manifest also records bundles that produced no translated overlay. An
unchanged negative result needs no repeated Unity parse or write. Removed
accepted translations remove their old overlays from the next release.

## Lyrics and images

`bundle_build.lyrics` fingerprints each song's effective `SlotTranslations`,
official content identity and lyric writer/toolchain. Text resolver counters,
other songs, aggregate files and timestamps do not invalidate it. Song writers
retain their existing source binding and round-trip checks.

Images enter this release as independently audited, already generated overlay
bundles. The release builder verifies and merges these artifacts; it does not
run image generation or inject unchanged textures again. `image-overlay/` is
included in release triggers and the release gate.

## Version changes and migration

The planner uses a successful release for the current version, or the newest
successful older version. A remote filename change updates `runtime_path`, not
the output object. Source content, effective replacements or the relevant
writer/toolchain changing rebuilds the affected packages. The official index and
release metadata are published for the current version even when every output
bundle was carried over.

Releases predating `bundle_build` require one baseline build to establish the
verified records. No old `every_key_resolved_exactly` claim is required after
migration. The legacy `reuse` counters remain diagnostic metadata.

Regression coverage includes shared JSONL files, cross-bundle translation memory,
unresolved-to-translated transitions, corrupt parse caches, missing output
objects, remote renames, single-song changes, removed translations, forced
builds and incremental/full overlay equivalence.
