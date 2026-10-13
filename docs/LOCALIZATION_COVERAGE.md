# Localization coverage and recovery

The observed official asset version is not a source synchronization marker.
`llm-translate-assets.yml` always refreshes the current official index, even
when `track_jp_assets.py` reports `has_update=false`. Each extractor verifies
its own memo before skipping an unchanged bundle. `full_rescan` runs independently
of a version update and applies to text, event-unit dialogue and song lyrics.
A missing official index fails the workflow instead of producing an empty queue.

Supported text surfaces:

| Surface | Source | Extraction / release writer |
| --- | --- | --- |
| GTX | `_jp.gtx.unity3d` TextAsset | `refresh_latest_official_catalogue.py` / GTX overlay |
| Lyrics | `scrobj_*` scenario slots | `refresh_lyrics_catalogue.py` / `build_lyric_overlay.py` |
| Event units | `event_unit_talk_<digits>.unity3d` JSON `commands[index].text` | `event_unit_localization.py` |

Event-unit locale keys bind the exact command position and source SHA-256.
Only accepted rows whose original text still matches that position can replace
the text. The writer reloads its saved UnityFS bundle, compares the entire JSON
with the expected document and verifies every other Unity object's raw bytes.
The regular generated-release builder packages these objects and aligns their
sizes in the official index. Build records reuse unchanged outputs by source
content, accepted replacements and writer fingerprint.

`manifests/localization-coverage.json` records the official index hash, each
surface's missing/stale source bundles, translation statuses, high-confidence
repair candidates, and image-family inventory. `source_sync_complete` means
all registered text sources were checked; it does **not** mean all rows or
images are translated. An intentionally truncated source refresh cannot pass
`--require-source-sync`. The coverage report is a derived report excluded from
the release-input fingerprint, so refreshing it alone does not force a rebuild.

Accepted rows are normally preserved. The collector prioritizes narrowly
identified copied Japanese sentences and identical Japanese verb questions
for repair. Names, song titles, numbered album track titles and short
interjections are allowed. Failed or still-Japanese repair drafts leave the old
accepted text intact; successful drafts pass token validation and enter the
existing machine-draft publication stage. General kana counts remain review
candidates, not an automatic instruction to overwrite every Japanese name.

Default translation runs process at most 500 unique sources, commit validated
progress, and dispatch the next batch only when untranslated work remains and
the previous apply step actually updated rows. Explicit `max_items` smoke runs
stop after one batch. This prevents a large recovered source surface from
losing all its drafts at the three-hour job timeout, and prevents no-progress
provider/validation failures from endlessly dispatching themselves.

## Images

Automatic image localization remains disabled in
`configs/localization-pipeline.json`. Inventory detection counts monitored official
bundles (including thumbnails), reviewed-image manifest coverage and published
bundle coverage. It does not assert that all counted bundles contain Japanese,
and it does not perform generation or blanket OCR.

`image-model-smoke.yml` is a separate, manually dispatched experiment using
the existing `MLTD_LLM_API_KEY` secret and provider. It unpacks two official
comic samples, exports lossless PNGs, assembles the six textures of a four-panel
comic in order, and calls the explicitly named image model. For Gemini the
request uses native `/v1beta/models/<model>:generateContent` with
`contents/parts/inlineData` and `generationConfig.responseModalities`.
`guided=true` supplies exact Chinese dialogue to test rendering independently
of translation. Source images, prompts, raw generated images, normalized
comparison previews and credential-free metadata are uploaded as artifacts.
No generated sample is approved, injected or published automatically.

Initial `gemini-3.1-flash-image` experiments succeeded through the existing
provider on 2026-10-13. The model can replace vertical text and retain comic
composition, but the unguided run changed glyphs in names and re-rendered an
English logo. A successful API call is therefore not an image-release gate.
Two further experiments supplied exact Chinese dialogue and stronger protected
name/logo/artwork instructions. Logo lettering improved, but character mouths
and facial expressions still changed. Prompt instructions alone did not enforce
pixel preservation. Before production use, text-region masks and deterministic
restoration from the source image are necessary; the experimental output stays
outside the approved image library.

`scripts/image_text_regions.py` demonstrates source-bound restoration on the
exact sampled `ex4c00968` archive. It checks input/output hashes, verifies the
comparison original against the submitted input pixels, applies explicit text
masks, preserves source alpha, and proves zero changed pixels outside the masks
and in protected name/logo/face regions. It also splits the result back into
the original six PNG texture sizes. The map is deliberately restricted to this
verified source archive; a changed upstream source is rejected. The resulting
candidate remains `generated_unreviewed`, because pixel preservation does not
validate Chinese glyphs, translation quality or mask-edge appearance.
