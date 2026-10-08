param(
    [int]$MaxItems = 0,
    [string[]]$ModelId = @(),
    [switch]$DryRun,
    [int]$PreviewItems = 3,
    [ValidateSet("dynamic", "single", "dynamic-mixed")][string]$BatchMode = "dynamic",
    [switch]$ArtifactPrompt,
    [switch]$MainOnly,
    [switch]$AllowDeferredExit,
    [string]$Config = "localization\api-models.local.json",
    [string]$Output = "build\localization-90200\machine-translations-api.jsonl",
    [string]$FailedOutput = "build\localization-90200\machine-translations-api.failed.jsonl",
    [string]$Summary = "build\localization-90200\machine-translations-api.summary.json"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

if ($MaxItems -lt 0) { throw "MaxItems must be >= 0" }
if ($PreviewItems -lt 0) { throw "PreviewItems must be >= 0" }
if (-not (Test-Path $Config)) { throw "Missing API model config: $Config" }

# Never create two writers for the same production output.
if (-not $DryRun) {
    $others = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(?:\.exe)?$' -and
        $_.CommandLine -match '(?:^|[\\/ ])translate_mltd_api_pool\.py(?:\s|$)'
    })
    if ($others.Count) {
        throw "Translation pool already running (PID $($others[0].ProcessId)); do not launch a competing writer."
    }
}

$argsList = @(
    "scripts\translate_mltd_api_pool.py",
    "--config", $Config,
    "--output", $Output,
    "--failed-output", $FailedOutput,
    "--summary", $Summary,
    "--batch-mode", $BatchMode
)

if ($MaxItems -gt 0) {
    $argsList += @("--max-items", "$MaxItems")
}
foreach ($id in $ModelId) {
    if (-not [string]::IsNullOrWhiteSpace($id)) {
        $argsList += @("--model-id", $id)
    }
}
if ($ArtifactPrompt) {
    $argsList += @("--prompt-source", "artifact")
}
if ($DryRun) {
    $argsList += @("--dry-run", "--preview-items", "$PreviewItems")
}

$startedUtc = [DateTime]::UtcNow
& python @argsList
$rc = $LASTEXITCODE
if ($rc -ne 0 -and $rc -ne 2) {
    throw "Localization API pool exited with code $rc"
}

if (-not $DryRun) {
    if (-not (Test-Path $Summary) -or
        (Get-Item $Summary).LastWriteTimeUtc -lt $startedUtc.AddSeconds(-2)) {
        throw "Main translation summary missing/stale; companion will NOT start."
    }
    $result = Get-Content -Raw $Summary | ConvertFrom-Json
    $selected = [int]$result.selected_pending
    $accepted = [int]$result.accepted
    $failed = [int]$result.failed
    $deferred = [int]$result.deferred
    & python "scripts\report_localization_progress.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Translation finished, but progress reporting failed."
    }
    if ($selected -ne ($accepted + $failed + $deferred)) {
        throw "Main queue accounting mismatch; companion will NOT start."
    }
    if ($deferred -gt 0) {
        if ($AllowDeferredExit -and $MainOnly) {
            Write-Warning "Main queue provider-deferred $deferred source IDs; returning control to sequential retry controller."
            exit 0
        }
        throw "Main queue provider-deferred $deferred source IDs; companion will NOT compete. Resume main first."
    }
    if ($rc -eq 2 -and $failed -gt 0) {
        Write-Warning "Main queue has $failed individual QA failures (recorded separately); continuing other surfaces."
    } elseif ($rc -ne 0) {
        throw "Main translation exited with code $rc; companion will NOT start."
    }
    if ($MaxItems -gt 0 -and $selected -ge $MaxItems) {
        Write-Output "Main run was capped at MaxItems=$MaxItems; companion not automatically started yet."
    } elseif (-not $MainOnly) {
        Write-Output "Main GTX run completed; starting non-GTX companion sequentially."
        $companionArgs = @("-Config", $Config, "-BatchMode", $BatchMode)
        if ($ModelId.Count -gt 1) {
            throw "Automatic companion chaining supports one explicit ModelId; use no ModelId for the enabled pool."
        }
        if ($ModelId.Count -eq 1) { $companionArgs += @("-ModelId", $ModelId[0]) }
        & pwsh -NoLogo -NoProfile -File "scripts\run-localization-companion.ps1" @companionArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Companion localization failed with code $LASTEXITCODE"
        }
    }
}

exit 0
