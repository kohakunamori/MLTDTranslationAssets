# Bounded native image comparison

Automatic generation stays paused while samples are reviewed. The comparison
command edits one existing PNG with the same prompt through each explicitly
selected model. It never approves or publishes a result and does not apply the
OCR restoration mask, resize the model output, or substitute another model.

```powershell
$env:MLTD_LLM_API_KEY = '<existing service credential>'
python scripts/compare_image_models.py --input source.png --prompt-file prompt.txt --output comparison
```

The defaults are `gpt-image-2.5-flare`, `gpt-image-2.5-sunburst`, and
`gpt-image-2.5`, requested over OpenAI `POST /v1/images/edits` multipart with
`n=1`, `quality=high`, `size=auto`, and PNG output. Repeat `--model` to choose
a subset. `--base-url` changes the service endpoint. Credentials are read from
the environment and are never stored in the comparison directory.

Calls are serial with starts at least 30 seconds apart. The tool uses only
the Python standard library and the existing provider transport; it does not
install Unity or OCR on the light runner. Preparation and visual inspection
belong on the desktop or hosted runner.

`summary.json` binds the source, prompt, endpoint and parameters with hashes.
Reusing the same directory skips completed model calls after verifying the
saved PNG SHA. Failures and uncertain in-flight attempts are also retained
without automatic retry. Changed inputs or corrupted outputs fail closed
instead of generating again. Latency includes client pacing, particularly
the first 30-second gate; it is not a pure server benchmark.

The raw model dimensions and aspect-ratio error are recorded. A generated
image can still fail geometry, translation, character or artwork preservation
checks. All outputs remain unapproved candidates. The first comparison uses
the existing `ex4c00964` source and unchanged Gemini sample prompt, so the
previous Gemini result can be inspected alongside it without another call.

To compare GPT with the original Gemini model using fixed Chinese reference
text, select both models explicitly:

```powershell
python scripts/compare_image_models.py --input source.png --prompt-file manifests/image-model-comparisons/2026-10-16-ex4c00964-fixed-text-prompt.txt --output fixed-text-comparison --model gpt-image-2.5 --model gemini-3.1-flash-image
```

GPT uses OpenAI multipart image edits. Gemini uses native
`/v1beta/models/gemini-3.1-flash-image:generateContent`, with the exact same
PNG bytes and prompt text in `inlineData`/`text`. Gemini requests a 2K image
with automatic aspect ratio; GPT retains `quality=high` and `size=auto`.
The comparison deliberately bypasses the production Gemini padding and crop
helper so the saved raw outputs can be judged directly. It performs no
input padding, output resizing or OCR masking. The mixed request manifest
records each model's protocol and both models' settings. Reference wording
is an experiment input, not an approval to publish the resulting image.
Gemini may return JPEG instead of PNG; native JPEG bytes are retained with
their original extension and dimensions. Decoded images with unsupported
formats are retained as `.bin` for offline diagnosis before validation fails,
so a format issue does not require another generation merely to inspect it.

## 2026-10-16 sample review

Each model returned one real PNG from the same `ex4c00964` input and unchanged
prompt. No fallback, retry, OCR restoration mask or production model change
was used. The request format follows the [OpenAI image edit API](https://developers.openai.com/api/reference/resources/images/methods/edit);
these three model IDs are advertised by the configured compatible service.

| Model | Observations from this comic |
| --- | --- |
| `gpt-image-2.5-flare` | Readable lettering, but changes sour flesh to juicy flesh, renders 馬場このみ as 马场木之美 instead of the glossary's 马场木实, and omits the fourth-panel handwritten aside. |
| `gpt-image-2.5-sunburst` | Retains sour flesh and the correct character name, but changes the opening question into a conclusion and also omits the handwritten aside. Some lettering is awkward. |
| `gpt-image-2.5` | Retains the handwritten aside and correct character name. Still paraphrases dialogue, changes sour to refreshing, and contains a malformed/wrong character. Best next candidate for a fixed-translation rendering trial on this sample. |

This is one comic, not a general model ranking. None is approved for release.
The next experiment should supply reviewed Chinese dialogue and glossary
names verbatim, then assess lettering and artwork preservation separately.
Bulk generation remains disabled pending that review.

All raw outputs are `724×2172`, versus the original `640×2193`. Much of this
difference is newly added full-height black side padding. Inspection-only
previews remove edge columns whose maximum channel value over the entire
column is below 16, then resize to the original canvas without any OCR mask:

| Model | Removed columns, left/right | Aspect-ratio error after trimming |
| --- | --- | --- |
| Flare | 44 / 45 | 0.178% |
| Sunburst | 44 / 46 | 0.020% |
| Standard | 46 / 46 | 0.295% |

This is promising for a whole-image workflow but does not prove unchanged
artwork or approve automatic normalization. The raw PNGs remain unchanged
under `images/candidates/`. Input/output hashes, parameters, preview geometry
and approval state are recorded in
[`2026-10-16-ex4c00964.json`](../manifests/image-model-comparisons/2026-10-16-ex4c00964.json),
with the exact prompt alongside it. Comparison records are separate from the
production generation cache, so they cannot replace a completed translation.

## Fixed-text GPT / Gemini follow-up

The second experiment uses the identical original PNG and a new fixed Chinese
reference prompt for both models, including every bubble, sound effect,
character name and the handwritten aside. Neither model receives a mask or
input padding. Raw outputs and the first failed Gemini attempt are recorded in
[`2026-10-16-ex4c00964-fixed-text.json`](../manifests/image-model-comparisons/2026-10-16-ex4c00964-fixed-text.json).

| Check | GPT-image-2.5 | Gemini-3.1-flash-image |
| --- | --- | --- |
| Raw output | PNG, 724×2172, newly added side black bars | JPEG, 1120×3838, no black bars |
| Raw aspect-ratio error | 14.22%; 0.178% after preview-only black-margin removal | 0.0065% without cropping |
| Main dialogue | Keeps the sour-flesh wording; fourth-panel question repeats 知道 and omits 味道 | Repeats 柠檬蛋糕 in panel 3, 验 in 体验, and parts of the fourth-panel question |
| Handwritten aside | Present; 那时可真是纯情啊…… follows the reference wording | Present but repeats 真是 |
| Artwork / labels | Overall composition retained by eye; fine details and labels still require review | Overall composition retained by eye; Latin name label beneath 马场木实 is visibly corrupted |

GPT is the better lettering candidate on this sample; Gemini follows the
original canvas ratio more closely. Fixed Chinese text reduces semantic
paraphrasing but does not ensure exact transcription. Both remain unapproved,
and this single comic is insufficient to select a production model.

There was one GPT request. The first Gemini response failed the client's
PNG-only validation with `ValueError`; those decoded bytes were not retained,
so its exact format cannot now be proven. After adding raw JPEG preservation,
one explicitly reviewed Gemini-only retry returned JPEG successfully. GPT
was reused. The failure and retry remain in the manifest's attempt history;
there were no automatic retries or fallback models. Future unsupported
decoded formats are saved before validation, avoiding this data loss.

## Stronger prompt follow-up

The next bounded experiment changes only the prompt: Chinese instructions,
17 explicitly named regions with one literal string per region, horizontal
left-to-right lettering, one copy of each region's text, and an internal
duplication/omission check. A preflight comparison verified that all Chinese
reference text is unchanged apart from wrapping. Original PNG bytes and
native API parameters match the preceding fixed-text experiment.

Each model was requested once. GPT returned a new PNG; Gemini returned
HTTP 429, stating model capacity was exhausted with a reset after roughly
2h28m. There was no retry or fallback, so this round cannot establish whether
the stronger prompt helps Gemini.

On the GPT sample, the fourth-panel question now contains the complete
`知道初恋的味道了吗` without the previous repeated `知道` or missing `味道`.
The checked third-panel text and handwritten aside also contain the expected
wording. Most main bubbles follow the horizontal layout. The left reply and
small aside remain vertical, and the same new black side margins remain.
Overall composition looks similar, but this does not prove unchanged artwork.

This one result supports using the stronger prompt for further small-sample
review; it does not establish repeatable accuracy or approve bulk generation.
The prompt, raw output, baseline reference and Gemini quota failure are in
[`2026-10-16-ex4c00964-strong-prompt.json`](../manifests/image-model-comparisons/2026-10-16-ex4c00964-strong-prompt.json)
and the adjacent prompt file. Preview-only black-edge trimming/resizing is
recorded separately; the raw image remains unapproved.

## Selected generator and visual acceptance

The active test configuration uses `gpt-image-2.5` over OpenAI image edits
with the preferred `preserve_layout` prompt. It retains the source's lettering
orientation rather than forcing horizontal text. `gemini-3.8-flash-high`
independently compares the original and normalized candidate over native
Gemini, checking completeness, meaning, lettering, artwork, layout, branding
and overflow. Reports bind both image hashes, reference text, model and review
prompt. A pass label alone cannot approve an image: all seven checks must be
boolean true, the issue list must be empty, and the binding must still match.

| Live test | Outcome |
| --- | --- |
| Reuse old GPT `ex4c00964`, with repeated 知道 and omitted 味道 | Rejected; the reviewer quoted the precise incorrect sentence and expected text. |
| Reuse existing corrected `ex4c00964` sample | Passed visual acceptance. This is a model decision, not publication of the sample. |
| Generate previously uncached `ex4c00959` once with the preferred layout | Rejected: six issues, including mistranslated Bravo, omitted manga, fire-sea mistranslation and remaining Japanese sound-effect lettering. |

Only one new generation and three review calls were made. Existing samples
were not regenerated; there were no fallback models or automatic correction
loops. The rejected new generation is durably checkpointed, so testing or
resuming the queue cannot silently translate it again. Bulk generation stays
disabled during these bounded tests; no new overlay was published.

The new comic used an English self-translation prompt with advisory OCR
fragments, unlike the earlier fixed-Chinese lettering comparison. These are
different tasks, so their outcomes must not be treated as a prompt-only model
benchmark. After the user requested a general reusable prompt, the default
template moved to `configs/image-translation-prompt.txt`: it contains no
sample-specific wording, names or OCR fragments. That final template has not
triggered additional model calls; the live reports retain their actual tested
reference hashes and the rejected generation remains reusable.

The portable case ledger is
[`image-acceptance-tests/2026-10-16.json`](../manifests/image-acceptance-tests/2026-10-16.json).
Source-bound decisions live in `manifests/image-visual-reviews.json`. For an
existing normalized PNG pair, the manual test command is:

```powershell
python scripts/image_visual_review.py --source original.png --candidate normalized.png --reference-file reference.txt --output review.json --base-url https://cpa.nyaneko.cn/v1
```

It reads the existing `MLTD_LLM_API_KEY` environment secret. Reusing the same
report skips the request; changed image, reference or rule hashes fail closed.
All image inputs are data to inspect, never instructions for the reviewer.
