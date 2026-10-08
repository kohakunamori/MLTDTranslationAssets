param(
    [int]$MaxItems = 0,
    [string[]]$ModelId = @(),
    [switch]$DryRun,
    [int]$PreviewItems = 3,
    [ValidateSet("dynamic", "single", "dynamic-mixed")][string]$BatchMode = "dynamic",
    [string]$Config = "localization\api-models.local.json",
    [string]$QueuePath = "build\localization-90200\machine-translation-nongtx-queue.jsonl",
    [string]$Output = "build\localization-90200\machine-translations-nongtx-api.jsonl",
    [string]$FailedOutput = "build\localization-90200\machine-translations-nongtx-api.failed.jsonl",
    [string]$Summary = "build\localization-90200\machine-translations-nongtx-api.summary.json"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

if ($MaxItems -lt 0) { throw "MaxItems must be >= 0" }
if ($PreviewItems -lt 0) { throw "PreviewItems must be >= 0" }
if (-not (Test-Path $Config)) { throw "Missing API model config: $Config" }
if (-not (Test-Path $QueuePath)) { throw "Missing companion queue: $QueuePath" }

if (-not $DryRun) {
    $others = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(?:\.exe)?$' -and
        $_.CommandLine -match '(?:^|[\\/ ])translate_mltd_api_pool\.py(?:\s|$)'
    })
    if ($others.Count) {
        throw "Another translation pool is running (PID $($others[0].ProcessId)); companion will not compete."
    }
}

$argsList = @(
    "scripts\translate_mltd_api_pool.py",
    "--config", $Config,
    "--input", $QueuePath,
    "--output", $Output,
    "--failed-output", $FailedOutput,
    "--summary", $Summary,
    "--batch-mode", $BatchMode,
    "--no-default-resume",
    "--resume-from", $Output
)

if ($MaxItems -gt 0) {
    $argsList += @("--max-items", "$MaxItems")
}
foreach ($id in $ModelId) {
    if (-not [string]::IsNullOrWhiteSpace($id)) {
        $argsList += @("--model-id", $id)
    }
}
if ($DryRun) {
    $argsList += @("--dry-run", "--preview-items", "$PreviewItems")
}

& python @argsList
$rc = $LASTEXITCODE
if ($rc -ne 0) {
    throw "Companion localization pool exited with code $rc"
}

if (-not $DryRun) {
    & python "scripts\report_localization_resource_gaps.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Companion translation completed, but resource-gap reporting failed with exit code $LASTEXITCODE"
    }
}

exit 0
