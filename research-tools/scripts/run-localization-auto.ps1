param(
    [string]$Config = "localization\api-models.local.json",
    [ValidateSet("dynamic", "single", "dynamic-mixed")][string]$BatchMode = "dynamic",
    [string[]]$ModelId = @(),
    [int]$RetryDelaySeconds = 600,
    [int]$MaxRetryCycles = 0,
    [switch]$DryRun
)

# Run GTX to completion, then non-GTX; resume each writer by source SHA.
# MaxRetryCycles=0 means the local controller keeps checking provider availability.
# No simultaneous providers/extra workers: each child is invoked synchronously.
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
if ($RetryDelaySeconds -lt 1 -or $MaxRetryCycles -lt 0) {
    throw "Invalid retry settings"
}
if (-not (Test-Path $Config)) { throw "Missing API model config: $Config" }
if ($BatchMode -eq "dynamic-mixed") {
    Write-Warning "Mixed-speaker routing requires human voice review."
}

if ($ModelId.Count -gt 1) { throw 'Automatic mode allows one explicit ModelId; omit for the configured pool.' }
$commonArgs = @('-BatchMode', $BatchMode, '-Config', $Config)
if ($ModelId.Count -eq 1) { $commonArgs += @('-ModelId', $ModelId[0]) }

if ($DryRun) {
    & pwsh -NoLogo -NoProfile -File scripts\run-localization-production.ps1 -DryRun -MainOnly @commonArgs
    if ($LASTEXITCODE -ne 0) { throw "GTX dry-run failed" }
    & pwsh -NoLogo -NoProfile -File scripts\run-localization-companion.ps1 -DryRun @commonArgs
    if ($LASTEXITCODE -ne 0) { throw "Non-GTX dry-run failed" }
    return
}

$lockPath = Join-Path $Repo "build\localization-90200\localization-auto.lock"
New-Item -ItemType Directory -Force (Split-Path $lockPath) | Out-Null
$lock = $null
try {
    # File lock prevents two orchestration loops even during provider backoff.
    $lock = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    throw "Another localization auto-controller already owns $lockPath"
}

function Get-TranslatorProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(?:\.exe)?$' -and
        $_.CommandLine -match '(?:^|[\\/ ])translate_mltd_api_pool\.py(?:\s|$)'
    })
}

function Read-FreshSummary([string]$path, [datetime]$startedUtc) {
    if (-not (Test-Path $path) -or
        (Get-Item $path).LastWriteTimeUtc -lt $startedUtc.AddSeconds(-2)) {
        throw "Missing/stale translation summary: $path"
    }
    $value = Get-Content -Raw $path | ConvertFrom-Json
    foreach ($field in @('selected_pending','accepted','failed','deferred')) {
        if ($null -eq $value.$field) {
            throw "Missing $field in translation summary: $path"
        }
    }
    $selected = [int]$value.selected_pending
    $accounted = [int]$value.accepted + [int]$value.failed + [int]$value.deferred
    if ($selected -ne $accounted) {
        throw "Summary accounting mismatch: selected=$selected accounted=$accounted"
    }
    return $value
}

try {
    $cycles = 0
    $attached = @(Get-TranslatorProcesses)
    if ($attached.Count -gt 1) {
        throw "Multiple translation workers already active; refusing to attach."
    }
    $attachPid = 0
    $attachWrapperPid = 0
    if ($attached.Count -eq 1) {
        if ($attached[0].CommandLine -match '(?:nongtx|non-gtx)') {
            throw "A non-GTX pool is already running; cannot safely take over the main phase."
        }
        $attachPid = [int]$attached[0].ProcessId
        $baselineUtc = ([datetime]$attached[0].CreationDate).ToUniversalTime()
        # A directly launched production wrapper may start companion itself
        # after its Python child exits. Wait for the wrapper too, so attaching
        # the auto-controller cannot race it to launch a second companion.
        $parentId = [int]$attached[0].ParentProcessId
        if ($parentId -gt 0) {
            $parentProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $parentId"
            if ($parentProcess -and
                $parentProcess.Name -match '^pwsh(?:\.exe)?$' -and
                $parentProcess.CommandLine -match 'run-localization-production\.ps1') {
                $attachWrapperPid = $parentId
            }
        }
        Write-Output "Attaching to existing GTX process PID=$attachPid; will not restart it."
        if ($attachWrapperPid) {
            Write-Output "Also waiting for existing production wrapper PID=$attachWrapperPid before chaining companion."
        }
    }
    while ($true) {
        if ($attachPid -ne 0) {
            try { Wait-Process -Id $attachPid -ErrorAction Stop } catch {
                if (Get-Process -Id $attachPid -ErrorAction SilentlyContinue) { throw }
            }
            $attachPid = 0
            if ($attachWrapperPid -ne 0) {
                try { Wait-Process -Id $attachWrapperPid -ErrorAction Stop } catch {
                    if (Get-Process -Id $attachWrapperPid -ErrorAction SilentlyContinue) { throw }
                }
                $attachWrapperPid = 0
                Write-Output "Existing production wrapper exited; checking its GTX summary."
            } else {
                Write-Output "Existing GTX process exited; checking its summary."
            }
        } else {
            $baselineUtc = [datetime]::UtcNow
            $active = @(Get-TranslatorProcesses)
            if ($active.Count) { throw "Another pool started while controller was preparing GTX." }
            $probe = @'
import asyncio
from pathlib import Path
from scripts.translate_mltd_api_pool import load_model_config, select_models
from scripts.mltd_batch_v2_benchmark import check_model_catalog
import sys
config, *ids = sys.argv[1:]
models = select_models(load_model_config(Path(config)), ids)
async def main():
    for model in models:
        await check_model_catalog(model)
asyncio.run(main())
print("gateway-model-catalogue-ok")
'@
            & python -c $probe $Config @ModelId
            if ($LASTEXITCODE -ne 0) {
                $cycles++
                if ($MaxRetryCycles -gt 0 -and $cycles -ge $MaxRetryCycles) {
                    throw "Provider unavailable for $cycles checks; GTX remains pending."
                }
                Write-Warning "Provider/model unavailable. Retrying GTX preflight after $RetryDelaySeconds seconds."
                Start-Sleep -Seconds $RetryDelaySeconds
                continue
            }
            Write-Output "Starting/resuming main GTX translation with $BatchMode."
            & pwsh -NoLogo -NoProfile -File scripts\run-localization-production.ps1 -MainOnly -AllowDeferredExit @commonArgs
            $rc = $LASTEXITCODE
        }
        $summaryPath = "build\localization-90200\machine-translations-api.summary.json"
        $summary = Read-FreshSummary $summaryPath $baselineUtc
        Write-Output "GTX summary: selected=$($summary.selected_pending) accepted=$($summary.accepted) failed=$($summary.failed) deferred=$($summary.deferred)"
        if ([int]$summary.deferred -gt 0) {
            $cycles++
            if ($MaxRetryCycles -gt 0 -and $cycles -ge $MaxRetryCycles) {
                throw "Provider-deferred GTX rows still pending after $cycles cycles."
            }
            Write-Warning "GTX provider deferred rows; never launch companion concurrently. Retrying after $RetryDelaySeconds seconds."
            Start-Sleep -Seconds $RetryDelaySeconds
            continue
        }
        if ($rc -and $rc -ne 0) {
            throw "Main GTX wrapper failed with exit code $rc"
        }
        if ([int]$summary.failed -gt 0) {
            Write-Warning "$($summary.failed) individual main GTX QA failures were preserved for review; other surfaces continue."
        }
        break
    }

    $cycles = 0
    while ($true) {
        if (@(Get-TranslatorProcesses).Count) {
            throw "Another translation pool is active; companion will not compete."
        }
        $baselineUtc = [datetime]::UtcNow
        Write-Output "GTX phase finished; starting/resuming non-GTX companion with $BatchMode."
        & pwsh -NoLogo -NoProfile -File scripts\run-localization-companion.ps1 @commonArgs
        $rc = $LASTEXITCODE
        $summary = Read-FreshSummary "build\localization-90200\machine-translations-nongtx-api.summary.json" $baselineUtc
        Write-Output "Non-GTX summary: selected=$($summary.selected_pending) accepted=$($summary.accepted) failed=$($summary.failed) deferred=$($summary.deferred)"
        if ([int]$summary.deferred -gt 0) {
            $cycles++
            if ($MaxRetryCycles -gt 0 -and $cycles -ge $MaxRetryCycles) {
                throw "Provider-deferred non-GTX rows still pending after $cycles cycles."
            }
            Write-Warning "Non-GTX provider unavailable; retrying source-SHA pending IDs after $RetryDelaySeconds seconds."
            Start-Sleep -Seconds $RetryDelaySeconds
            continue
        }
        if ($rc -ne 0) {
            if ($rc -ne 1 -or [int]$summary.failed -eq 0) {
                throw "Non-GTX wrapper failed with exit code $rc"
            }
            Write-Warning "Non-GTX individual QA failed rows remain for review."
        }
        Write-Output "Both GTX and confirmed non-GTX queues processed; check QA and materialization reports."
        & python scripts\report_localization_progress.py
        if ($LASTEXITCODE -ne 0) { Write-Warning "Final progress report failed" }
        break
    }
} finally {
    if ($lock) { $lock.Dispose() }
}
