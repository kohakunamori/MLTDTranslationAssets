param(
    [int]$BatchSize = 40,
    [int]$MaxItems = 0,
    [int]$SampleSize = 30,
    [string]$Output = "build\localization-90200\machine-translations-codex.jsonl"
)

Write-Warning "run-simple-localization-production.ps1 is a legacy compatibility shim. Current production uses scripts\run-localization-production.ps1 -> translate_mltd_api_pool.py."

$Legacy = Join-Path $PSScriptRoot "run-legacy-command-localization.ps1"
& powershell -ExecutionPolicy Bypass -File $Legacy `
    -BatchSize $BatchSize `
    -MaxItems $MaxItems `
    -SampleSize $SampleSize `
    -Output $Output
exit $LASTEXITCODE
