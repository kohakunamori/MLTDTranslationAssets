# Bounded OpenAI image comparison

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
