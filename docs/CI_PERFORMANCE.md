# CI efficiency

The generated-release workflow first checks out source files and release
manifests with a blob-filtered clone. It omits generated CAS objects, parsed
bundle inputs, PNGs, and image overlays until the release gate says a build is
needed. A text/lyrics build then materializes the generated tree while keeping
PNGs and image overlays excluded when image generation is paused; publication
reuses prior image objects from generated CAS after verifying the current source.
An enabled image-generation build materializes all inputs. Sparse checkout does
not remove paths from the committed-tree comparison.

The gate reads the input trees twice, once for the release's source commit and
once for HEAD. Previously it invoked `git ls-tree` once per input path and commit
(34 invocations for 17 paths). Derived outputs remain excluded, and a Git error
still requires rebuilding. A real temporary Git repository regression checks
both the skip decision with omitted binaries and subsequent materialization.

The builder counts source-client provenance during its translation scan,
including rows that have no accepted translation. It uses the same library glob
and majority/tie rule as the standalone provenance reader. This removes a second
JSONL scan; source hashes of accepted rows are also calculated once instead of
three times. Bundle dependency resolution and content validation remain in place.

Official source caches use the asset version rather than the complete version
manifest, so timestamp/provenance-only edits do not create another snapshot. A
cache is saved after a successful build only when its exact key was absent;
older keys remain valid restore fallbacks. GitHub caches are immutable: adding a
previously uncached bundle later in the same version can require a fresh source
download. Committed parsed-input metadata still prevents reparsing unchanged
bundles, and hashes/declared sizes are checked by the existing builder.

Image progress artifacts contain JSON/JSONL reports rather than the entire
repack directory and expire after seven days. The optional image stage remains
disabled until `configs/localization-pipeline.json` enables it. Its resumable
work cache is retained separately from diagnostic artifacts.

The offline CI suite includes the per-bundle incremental regressions. Editing
the feature policy also triggers validation.

## Storage investigation

Artifacts and caches are different quotas. Artifact storage shares an allowance
with GitHub Packages; caches have a separate per-repository allowance. Deleting
an artifact stops future accrual but does not erase storage already accrued in
the billing period. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

The 2026-10-12 investigation found only 1.27 MiB of active artifacts and 295.62
MiB of cache in this repository. The main storage consumers were other
repositories. The account-wide metadata snapshot and investigation notes are
kept locally under `work/actions-storage-audit.json` and
`work/ACTIONS_STORAGE_AUDIT.md`, outside tracked source because they include
private repository information. No remote artifact or cache was deleted.
