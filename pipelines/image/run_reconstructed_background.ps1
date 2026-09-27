# Start once via the user's own Windows Task Scheduler interactive-token task.
# This process belongs to Windows Task Scheduler, not to the short-lived AgentDock
# command session. Do not start a second copy while the named task is running.
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dir=Join-Path $repo 'work\image-localization-25\reconstructed-preprocess'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$out=Join-Path $dir 'scheduled-production-stdout.log'
$err=Join-Path $dir 'scheduled-production-stderr.log'
Set-Location $repo
$env:PYTHONIOENCODING='utf8'
$started=Get-Date -Format o
"START $started on $env:COMPUTERNAME as $env:USERNAME" | Out-File -LiteralPath $out -Append -Encoding utf8
try {
    & python -u -X utf8 'tools/mltd_image_localization/run_reconstructed_batch.py' 1>> $out 2>> $err
    $result=$LASTEXITCODE
    "EXIT $(Get-Date -Format o) status=$result" | Out-File -LiteralPath $out -Append -Encoding utf8
    exit $result
} catch {
    "WRAPPER_EXCEPTION $(Get-Date -Format o): $($_.Exception.Message)" | Out-File -LiteralPath $err -Append -Encoding utf8
    exit 3
}
