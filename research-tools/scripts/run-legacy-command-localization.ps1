param(
    [int]$BatchSize = 40,
    [int]$MaxItems = 0,
    [int]$SampleSize = 30,
    [string]$Output = "build\localization-90200\machine-translations-codex.jsonl"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

$Queue = "build\localization-90200\machine-translation-queue-context.jsonl"
$EffectiveQueue = $Queue
if ($MaxItems -gt 0) {
    $SubsetQueue = "build\localization-90200\simple-production-subset-$MaxItems.jsonl"
    $lines = Get-Content $Queue -Encoding UTF8 -TotalCount $MaxItems
    [IO.File]::WriteAllLines(
        (Join-Path $Repo $SubsetQueue),
        $lines,
        (New-Object Text.UTF8Encoding($false))
    )
    $EffectiveQueue = $SubsetQueue
}

$Summary = [System.IO.Path]::ChangeExtension($Output, ".summary.json")
$Sample = [System.IO.Path]::ChangeExtension($Output, ".sample.jsonl")
$SampleSummary = [System.IO.Path]::ChangeExtension($Output, ".sample-summary.json")

$required = @(
    $Queue,
    "localization\quality\glossary.json",
    "localization\quality\style-guide.md",
    "build\localization-90200\character-voice-evidence.json",
    "scripts\codex_luna_json_adapter.py"
)
foreach ($path in $required) {
    if (-not (Test-Path $path)) { throw "Missing required localization input: $path" }
}

$translateArgs = @(
    "scripts\translate_gtx_queue.py",
    "--queue", $EffectiveQueue,
    "--output", $Output,
    "--summary", $Summary,
    "--provider", "command",
    "--command", "python scripts\codex_luna_json_adapter.py --timeout 180 --reasoning-effort high",
    "--batch-size", "$BatchSize",
    "--timeout", "180",
    "--retries", "2",
    "--retry-delay", "2",
    "--glossary", "localization\quality\glossary.json",
    "--style-guide", "localization\quality\style-guide.md",
    "--speaker-evidence", "build\localization-90200\character-voice-evidence.json",
    "--max-style-samples", "2",
    "--max-examples", "1",
    "--max-context-examples", "1",
    "--qa-mode", "basic"
)
if ($MaxItems -gt 0) { $translateArgs += @("--max-items", "$MaxItems") }

& python @translateArgs
$translateExit = $LASTEXITCODE

if ($translateExit -ne 0 -and $BatchSize -gt 24) {
    Write-Warning "Primary batch size $BatchSize failed; resuming remaining source IDs with fallback batch size 24."
    $fallbackArgs = @($translateArgs)
    $batchArgIndex = [Array]::IndexOf($fallbackArgs, "--batch-size")
    if ($batchArgIndex -lt 0) { throw "Internal error: --batch-size argument not found" }
    $fallbackArgs[$batchArgIndex + 1] = "24"
    & python @fallbackArgs
    $translateExit = $LASTEXITCODE
}

& python "scripts\sample_translation_output.py" "--queue" $EffectiveQueue "--translations" $Output "--output" $Sample "--summary" $SampleSummary "--sample-size" "$SampleSize"
if ($LASTEXITCODE -ne 0) { throw "Sample generation failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "Simple localization production artifacts:"
Write-Host "  translations: $Output"
Write-Host "  summary:      $Summary"
Write-Host "  sample:       $Sample"
Write-Host "  sample summary: $SampleSummary"
Write-Host ""
Write-Host "Policy: no holdout/hidden/reviewer promotion gate. Inspect the sample for usability; rerun this script to resume after any failed batch."

exit $translateExit
