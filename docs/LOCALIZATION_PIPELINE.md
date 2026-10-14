# Unified localization and packaging

`auto-track-jp-assets.yml` wakes `llm-translate-assets.yml` for version updates
and daily retries. The tracker no longer dispatches an independent image task.

The translation workflow owns three stages:

1. `draft`: refresh text and lyrics, translate and publish validated rows;
2. `images`: optionally call the reusable image workflow, resume generation,
   audit candidates, inject textures, independently audit the Unity bundles and
   commit a version-bound overlay;
3. `package`: dispatch one generated release after the enabled stages finish.

The image stage is currently **disabled** in
`configs/localization-pipeline.json`: no suitable model provider has been
selected. A skipped image stage does not block text/lyric releases. The generated
build also reads this policy and omits the image overlay while disabled. Existing
image source files, reviewed PNGs, progress and overlays are preserved.

Within one generated-release run, the release gate owns the selected
`asset_version` and the materialization step owns the `images_enabled` policy
result. Build, verification and Portal manifest steps consume those outputs.
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
shared policy to `images.enabled=true`. An incomplete image batch keeps its
progress and postpones unified packaging; a failed independent audit cannot
publish an overlay. `publish_image_overlay.py` checks the audit/report digests,
source/output bytes and current official catalogue before exporting the bundles
and patched image index under `image-overlay/<asset_version>/`.
