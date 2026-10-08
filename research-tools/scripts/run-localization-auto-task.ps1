# Windows Task Scheduler entrypoint. Captures persistent on-machine diagnostics.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$folder = Join-Path $repo "build\localization-90200"
New-Item -ItemType Directory -Force $folder | Out-Null
$log = Join-Path $folder "localization-auto.task.log"
Start-Transcript -Path $log -Append -Force | Out-Null
try {
    Write-Output "LOCALIZATION_AUTO_TASK_START $(Get-Date -Format o) PID=$PID"
    & (Join-Path $PSScriptRoot "run-localization-auto.ps1")
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "Auto controller returned $LASTEXITCODE"
    }
    Write-Output "LOCALIZATION_AUTO_TASK_COMPLETE $(Get-Date -Format o)"
} catch {
    Write-Error "LOCALIZATION_AUTO_TASK_FAILED $($_.Exception.Message)"
    throw
} finally {
    Stop-Transcript | Out-Null
}
