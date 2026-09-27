param(
    [switch]$Open
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $repo
try {
    & python -X utf8 tools/mltd_image_localization/build_mltd_image25_normalized.py
    if ($LASTEXITCODE -ne 0) { throw "Normalize failed: $LASTEXITCODE" }
    & python -X utf8 tools/mltd_image_localization/build_mltd_image25_review_pairs.py
    if ($LASTEXITCODE -ne 0) { throw "Review pair generation failed: $LASTEXITCODE" }
    & python -X utf8 tools/mltd_image_localization/build_mltd_image25_review_gallery.py
    if ($LASTEXITCODE -ne 0) { throw "Review gallery generation failed: $LASTEXITCODE" }
    $gallery = Join-Path $repo "work/image-localization-25/review/index.html"
    Write-Host "Review page: $gallery"
    Write-Host "Originals: $(Join-Path $repo 'work/image-localization-25/original')"
    Write-Host "Native-size edits: $(Join-Path $repo 'work/image-localization-25/normalized')"
    Write-Host "Original GPT Image outputs: $(Join-Path $repo 'work/image-localization-25/edited')"
    Write-Host "Side-by-side PNGs: $(Join-Path $repo 'work/image-localization-25/review-pairs')"
    if ($Open) { Start-Process -FilePath $gallery }
}
finally {
    Pop-Location
}
