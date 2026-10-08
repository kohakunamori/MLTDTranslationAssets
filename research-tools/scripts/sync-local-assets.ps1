[CmdletBinding()]
param(
    [string]$Profile = 'server\profiles\local-bootstrap-online-1077100.json',
    [string]$ArchiveRoot = 'work\local-assets',
    [string]$Scope = 'jp-android',
    [int]$Workers = 16,
    [string]$Proxy = '',
    [switch]$VerifyExisting,
    [switch]$Durable,
    [switch]$ManifestOnly,
    [switch]$Force,
    [int]$Limit = -1,
    [string]$Contains = '',
    [switch]$VerifyAfter
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    if (-not (Test-Path -LiteralPath $Profile)) {
        throw "Profile not found: $Profile"
    }
    $profileJson = Get-Content -Raw -LiteralPath $Profile | ConvertFrom-Json
    $asset = $profileJson.overrides.GetAssetVersionReply
    if (-not $asset) {
        throw "Profile does not contain overrides.GetAssetVersionReply"
    }
    $assetRoot = [string]$asset.asset_url
    $manifest = [string]$asset.asset_index_name
    $version = [string]$asset.asset_version
    if (-not $assetRoot -or -not $manifest) {
        throw "Profile asset_url/asset_index_name is incomplete"
    }

    $args = @(
        'tools\cache_assets.py', 'sync',
        '--root', $ArchiveRoot,
        '--scope', $Scope,
        '--manifest', $manifest,
        '--asset-root', $assetRoot,
        '--workers', "$Workers"
    )
    if ($Proxy) { $args += @('--proxy', $Proxy) }
    if ($VerifyExisting) { $args += '--verify-existing' }
    if ($Durable) { $args += '--durable' }
    if ($ManifestOnly) { $args += '--manifest-only' }
    if ($Force) { $args += '--force' }
    if ($Limit -ge 0) { $args += @('--limit', "$Limit") }
    if ($Contains) { $args += @('--contains', $Contains) }

    Write-Host 'MLTD_ASSET_SYNC_START'
    Write-Host "scope=$Scope"
    Write-Host "asset_version=$version"
    Write-Host "manifest=$manifest"
    Write-Host "archive_root=$ArchiveRoot"
    & python @args
    if ($LASTEXITCODE -ne 0) {
        throw "asset sync failed with code $LASTEXITCODE"
    }

    if ($VerifyAfter -and -not $ManifestOnly) {
        $verifyArgs = @(
            'tools\cache_assets.py', 'verify',
            '--root', $ArchiveRoot,
            '--scope', $Scope,
            '--manifest', $manifest
        )
        if ($Limit -ge 0) { $verifyArgs += @('--limit', "$Limit") }
        if ($Contains) { $verifyArgs += @('--contains', $Contains) }
        & python @verifyArgs
        if ($LASTEXITCODE -ne 0) {
            throw "asset verification failed with code $LASTEXITCODE"
        }
    }
    Write-Host 'MLTD_ASSET_SYNC_OK'
}
finally {
    Pop-Location
}
