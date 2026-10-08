# Central Assets orchestration — local research candidate

This directory receives the central orchestration source for **JP 9.0.200 arm64 / frozen Assets 1077100**. It is not connected to active CI. Nothing here has been released, pushed, deployed, mirrored to NAS, or tested with production/player/private inputs.

## Entry and root semantics

`scripts/materialize_generated_release.py` is the research entry. Its `REPO` and default `--repository-root` resolve to **this `research-tools/` directory**. The original `scripts/` and `tools/` relative shape is retained. Relative snapshot/index paths used by `localization_version_identity.py` also resolve here, never to the retired main repository.

The product's existing `scripts/assets_generated_index.py` (flat writer), `scripts/assets_mirror.py`, `scripts/build_generated_release.py`, schemas and active workflows remain the sole maintained product implementations. No copies of those implementations were introduced here. Research modules import the existing `pipelines.text.mltd_localize_gtx`; the two image adapters delegate to existing `pipelines.image` entry points. They do not discover sibling checkouts or import anything from the retired main repository.

Supply the product checkout explicitly through `PYTHONPATH` for pipeline imports. Writer selection is a **separate mandatory pair** in every mode, including preflight: `--assets-writer-root <approved-product-checkout>` and `--assets-writer-pin <independently-approved-raw-byte-SHA256>`. A hash recorded in the migration inventory is provenance only; it is **not** a trusted pin. Do not compute a checkout hash at runtime and call it trusted. Missing/half/wrong pairs refuse before producers/store construction; there is no legacy writer fallback.

Bounded checks (replace the placeholder with the chosen product checkout; these commands do not generate assets):

```powershell
Set-Location '<product-checkout>/research-tools'
$env:PYTHONPATH = '<product-checkout>'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B scripts/materialize_generated_release.py --help
python -B scripts/test_research_orchestration.py -v
```

All surface commands require their explicit reviewed inputs before any real producer may be run. `--repository-root` must stay at the research root to select the migrated text producers and research image adapters. `--assets-writer-root` identifies the product writer checkout, not the research root. Output, approved ledgers and compatible-source records remain independently supplied locations; neither the product checkout nor old main inputs are silently substituted.

## Explicit input slots — not copied or inspected

| Slot | Contract / location |
| --- | --- |
| Trusted writer | Mandatory root + independently approved pin of `scripts/assets_generated_index.py`; no trusted pin supplied by this migration |
| Accepted ledgers | `--translated-ledger` (repeatable), or explicit `--input-root/ledgers/*.jsonl`; reviewed source-bound `release_gate=accepted` evidence |
| Reviewed image locators | `--image-install-manifest` / adapter `--install-manifest`; private **user-approved** source-bound locators, no environment/default selection through the adapter |
| Reviewed image originals | `--image-original-root` / adapter `--original-root`; mandatory for this research injector adapter, including preflight; original PNGs, source bundles and localized PNGs referenced by the manifest remain external inputs |
| Frozen identity | Under research root: `work/local-assets/jp-android/d8e17c28b47a711a5008ee87345e49db7a2b2559.data`; `build/localization-90200/jp-gtx-cache-snapshot.json` |
| Frozen event-unit sources | `work/agents/image-localization/reviewed937-texture-stage/event-unit-source1077100/`, its `event-unit-source1077100-index.json` and `event-unit-source1077100-independent-audit.json`; `build/localization-90200/event-unit-translation-queue.jsonl` |
| Frozen MD.mld sources | Same stage's `mld-source1077100/150d85ca7071086175de38f3ee95d87e1e3ae673.unity3d` and `mld-source1077100-independent-audit.json`; `build/localization-90200/mld-translation-queue.jsonl`; `build/live-consistency-reverse-90200/MD.mld.plain.bin` and `MD.mld.decoded.json` |
| Quality reference inputs | `localization/quality/glossary.json` and `localization/quality/traditional-to-simplified-char-map.json`; not included in this source-only slice |
| Other metadata | Explicit source/translation/generated commit identities, `ci_run_id`, optional `--verified-compatible-records`; optional FontRender speaker evidence if that separate helper is used |

The frozen producers retain their original **research-root-relative** slots and embedded expected hashes; supplying a root does not rewrite archived snapshot internal paths or make private inputs public. No frozen payload/queue, player data, private manifest, credentials, `.local.json` or TOML was read or copied. Do not provision or synthesize missing production data merely to make migration tests pass.

## Issues and acceptance boundary

- Basic source closure, syntax/help and synthetic refusal checks are separate from a real multi-surface run. Missing approved data and trusted writer pin leave full generation **external-blocked / unverified**.
- Source migration does not retire every old execution entry. Main reported task metadata for `MLTD_ImageLocalization_20260919` as Ready + Enabled with an old-repository path reference. That scheduler/documentation handoff remains main-owned; this slice did not inspect its argv/private inputs or modify the task.
- Pipeline implementation reuse is intentional. `PYTHONPATH` must explicitly provide the product checkout; no retired-main path is inserted. Missing product pipeline or Python dependencies is an entry blocker, not permission to restore duplicate writers. Required packages already used by these public modules include UnityPy, msgpack, numpy, Pillow and pycryptodome; this migration installs nothing.
- Product GTX differs from the main historical file. Its exported helpers are reused; behavioral equivalence, text QA/regex behavior, Unity repack and image labels were not repaired or certified here. Any known bug or future red test belongs to the target owner.
- Quality map is absent in this slice; the inherited quality module has a built-in fallback when that file is absent. Basic help can use the fallback, but this does **not** establish release QA equivalence. The target owner must provide/approve exact quality inputs before production acceptance.
- Frozen producers still require their original data layout. Relocating snapshots that embed old absolute paths needs an approved input adapter/data decision later; this migration does not rewrite private payloads or add a generic loader.
- The materializer retains an explicit prune option for historical API compatibility. Only default/no-prune refusal paths were checked; no store, generated transaction, GC, official downloads, producer, production write, device or port was used.
- Finite source credential/account-payload checks report counts/locations only. Zero matches is a limited static finding, **not** public-distribution permission or proof that all private information can be detected. This remains a local candidate.

## Historical archive

`archive/.github/workflows/assets-generated.yml.example` and `archive/docs/ASSETS_GENERATED_CI.md` preserve the historical central multi-surface template and its explanation byte-for-byte. The template is **non-active**, depends on external reviewed image/ledger/writer inputs, and is not wired to the product workflows. Its nested `.github` is documentation storage, not repository-root Actions configuration. Its commit/push/network steps were not executed. It is the old **no-GC** template, not evidence that the product's active pipeline has central surface closure.

`archive/scripts/test_materialize_generated_release.py` is the historical full harness. It imports the main legacy writer and tests historical fixture contracts; it is not the default migrated test and was not run. No legacy writer or schema was copied to make it green. The bounded maintained check is `scripts/test_research_orchestration.py`, which uses synthetic metadata, deliberately wrong literal pins and missing-input refusal paths; it never derives a trusted pin from the current product file.

Main historical GTX/image implementations remain preserved at their original source paths, classified as excluded historical implementations in the inventory. Existing maintained product implementations are reused without overwriting them.

`migration-inventory.json` lists the finite exact source/target paths, raw-byte SHA-256 values, adaptation decisions, input exclusions and validation results. Detailed before snapshots and evidence belong to the main agent's assigned `repo-retirement-assets-orchestration-20261003` handoff/run, not this active product pipeline.
