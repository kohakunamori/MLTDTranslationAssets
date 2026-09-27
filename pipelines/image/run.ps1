param(
    [ValidateSet('status','prepare','preprocess','triage','batch','review','test','pilot')]
    [string]$Action='status',
    [string]$TaskId='',
    [int]$MaxSheets=0,
    [switch]$Open
)
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$tool='tools/mltd_image_localization'
$work='work/image-localization-25'
Push-Location $repo
try {
    switch ($Action) {
        'status' {
            $work='work/image-localization-25'
            foreach($name in @('prepare-summary.json','preprocess/preprocess-summary.json','batch-progress.json')) {
                $p=Join-Path $work $name
                if(Test-Path $p) { Write-Host "=== $name ===";Get-Content $p }
            }
            foreach($name in @('reconstructed-preprocess/summary.json','reconstructed-preprocess/batch-progress.json')) {
                $p=Join-Path $work $name
                if(Test-Path $p){ Write-Host "=== $name ==="; Get-Content $p }
            }
            $task=Get-ScheduledTask -TaskName 'MLTD_ImageLocalization_20260919' -ErrorAction SilentlyContinue
            if($task){ Write-Host ('Windows scheduled task state: '+$task.State) }
            Write-Host 'Review page:' (Join-Path $repo "$work/review/index.html")
        }
        'prepare' {
            & python -X utf8 "$tool/prepare_mltd_image25_review.py"
            if($LASTEXITCODE -ne 0){throw 'Prepare failed'}
        }
        'preprocess' {
            throw 'The old raw-atlas preprocessing is disabled. Use -Action triage to classify reconstructed images instead.'
        }
        'triage' {
            $argsList=@('-u','-X','utf8',"$tool/preprocess_reconstructed.py",'--classify','--workers','3')
            if($MaxSheets -gt 0){$argsList+=@('--max-sheets',"$MaxSheets")}
            & python @argsList
            if($LASTEXITCODE -ne 0){throw 'Reconstructed-image classification failed'}
        }
        'batch' {
            $task=Get-ScheduledTask -TaskName 'MLTD_ImageLocalization_20260919' -ErrorAction SilentlyContinue
            if(-not $task){ throw 'Scheduled batch task missing; do not start a second foreground process' }
            if($task.State -eq 'Running'){
                Write-Host 'MLTD image batch is already running; inspect reconstructed-preprocess/batch-progress.json'
            } else {
                Start-ScheduledTask -TaskName 'MLTD_ImageLocalization_20260919'
                Write-Host 'Started the existing resumable Windows Task Scheduler batch'
            }
        }
        'review' {
            & powershell -NoProfile -ExecutionPolicy Bypass -File "$tool/refresh_mltd_image25_review.ps1"
            if($LASTEXITCODE -ne 0){throw 'Review refresh failed'}
            if($Open){Start-Process (Join-Path $repo "$work/review/index.html")}
        }
        'test' {
            & python -m pytest "$tool/test_mltd_internal_texture_image25.py" -q
            if($LASTEXITCODE -ne 0){throw 'Image pipeline tests failed'}
        }
        'pilot' {
            if(-not $TaskId){throw 'Specify -TaskId for one prepared internal-composite task'}
            & python -u -X utf8 "$tool/run_mltd_internal_image25.py" --task-id $TaskId
            if($LASTEXITCODE -ne 0){throw 'Pilot failed or requires geometry review'}
        }
    }
} finally {
    Pop-Location
}
