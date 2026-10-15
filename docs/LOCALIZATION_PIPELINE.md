# Unified localization and packaging

`auto-track-jp-assets.yml` is the single production entry. Tracking, translation,
R2 publication and gateway deployment are reusable jobs in one run; see
[the CI mechanism and stage diagram](CI.md). Quality checks and manual model
smoke tests are the other two entry points.
See [incremental images](IMAGE_INCREMENTAL.md) for discovery scope, OCR masks,
serial provider pacing, runner split and progress recovery.

The translation workflow owns three stages:

1. `draft`: refresh text and lyrics, translate and publish validated rows;
2. `images`: optionally call the reusable image workflow, resume generation,
   audit candidates, inject textures, independently audit the Unity bundles and
   commit a version-bound overlay;
3. `package`: return readiness/publication outputs; the caller builds the release
   after enabled stages finish, using a fresh main checkout.

The image policy has two independent switches in
`configs/localization-pipeline.json` (schema 2): `generation_enabled=true` and
`publication_enabled=true`. Generation is temporarily paused for sample and prompt review.
The provider is `gemini-3.1-flash-image`, concurrency 1,
RPM 2 (every edit, including retries, passes the 30-second gate). Existing source
files, reviewed PNGs, progress and overlays are preserved.

The generated builder always uses `--reuse-published-images` when publication is enabled,
then merges a new audited overlay if one exists.
It finds each image's newest successful published entry, including older releases
when the latest release was text-only. It verifies the translated CAS object's
SHA-256, downloads/caches the current official source and checks its full SHA-256
against the source bound to that image. Compatible objects are reused byte-for-byte
with the current catalogue's remote name. The final unified catalogue aligns their
sizes together with text and lyrics; old image catalogues are never reused.
Changed or removed sources are explicitly reported and fall back to official
content. Missing/corrupt translated objects and network failures fail the build.
No provider, PNG processing or texture injection is called on this path.
Image and event-unit download caches include the logical bundle name as well as
the catalogue fingerprint: distinct official bundles can share the opaque
catalogue field and byte size, so that field alone cannot identify their bytes.

Within one generated-release run, the release gate owns the selected
`asset_version` and the materialization step owns both image policy
results. Build, verification and Portal manifest steps consume those outputs.
They do not independently select another version or reinterpret the image
policy. Publication still checks the latest upstream inputs before committing;
the frozen step outputs do not replace the stale-input and retry guards.

Queue translation and independent review share the durable JSONL appender in
`pipelines/core/jsonl.py`. Their historical `append_rows` exports remain usable.
Appending still flushes and fsyncs each batch; it is distinct from the atomic
whole-file writer. The reviewer reads JSONL through this core module rather
than importing the Unity bundle codec.

The image workflow has only `workflow_call`, with an `enabled` input defaulting
to false and its own check of the shared policy before any provider-related
steps. It cannot be dispatched independently by the timer or a manual button.
Its old GitHub workflow is also administratively disabled during this pause.

When a provider is selected, configure its runtime and a source-bound prepared
image cohort, re-enable the GitHub image workflow if necessary, and change the
shared policy to `images.generation_enabled=true`. Keep
`images.publication_enabled=true` to publish its output. An incomplete image batch keeps its
progress and postpones unified packaging; a failed independent audit cannot
publish an overlay. `publish_image_overlay.py` checks the audit/report digests,
source/output bytes and current official catalogue before exporting the bundles
and patched image index under `image-overlay/<asset_version>/`.
