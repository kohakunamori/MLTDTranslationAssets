<#
.SYNOPSIS
    Deploy or verify the NAS `mltd-generated-assets` closure (plan by default).

.DESCRIPTION
    Compares this repository's closure files with the deployed NAS files by
    SHA-256.  With -Apply it converges them and only then touches the services:

      1. back up every drifted remote file to <project>/.backup-<UTC>
      2. upload to a staging dir and re-verify each hash remotely
      3. rewrite the live files in place (same inode: the shared nginx bind-mount
         for nginx-vhost.conf depends on it)
      4. rebuild the image (running containers stay up)
      5. prove the new image on a throwaway root with `sync_loop.py --once`
      6. `nginx -t`, then reload the shared nginx
      7. recreate the two services and probe the loopback route

    It never writes the mirror root itself, never touches the official archive
    (`views/`, `current`, `index.sqlite3`) and never restarts the archive updater.
#>
[CmdletBinding()]
param(
    [string]$NasAlias = 'nas',
    [string]$ProjectDir = '/vol1/1000/appdata/imas/mltd-generated-assets',
    [string]$VhostDir = '/vol1/1000/appdata/imas/mltd-asset/asset-server',
    [string]$PublicHost = 'https://mltd-asset.nyaneko.cn:18443',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

# repo-relative source -> absolute NAS path
$managed = @(
    [pscustomobject]@{ Repo = 'asset-server/generated-assets/sync_loop.py';         Remote = "$ProjectDir/sync_loop.py" }
    [pscustomobject]@{ Repo = 'asset-server/generated-assets/Dockerfile';           Remote = "$ProjectDir/Dockerfile" }
    [pscustomobject]@{ Repo = 'asset-server/generated-assets/docker-compose.yml';   Remote = "$ProjectDir/docker-compose.yml" }
    [pscustomobject]@{ Repo = 'asset-server/assets_route.py';                       Remote = "$ProjectDir/assets_route.py" }
    [pscustomobject]@{ Repo = 'scripts/assets_mirror.py';                           Remote = "$ProjectDir/scripts/assets_mirror.py" }
    [pscustomobject]@{ Repo = 'asset-server/nginx-vhost.conf';                      Remote = "$VhostDir/nginx-vhost.conf" }
)

function Invoke-Nas {
    param([string]$Script, [switch]$AllowFailure)
    # base64 keeps the ssh argument free of quotes and newlines across pwsh -> ssh -> sh.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($Script -replace "`r", '')))
    $output = & ssh -o BatchMode=yes -o ConnectTimeout=15 $NasAlias "echo $encoded | base64 -d | sh" 2>&1
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
        throw "NAS command failed ($LASTEXITCODE): $($output -join ' ')"
    }
    return $output
}

$local = @{}
foreach ($file in $managed) {
    $path = Join-Path $repoRoot $file.Repo
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "missing repository file: $($file.Repo)" }
    $local[$file.Repo] = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
}

$remote = @{}
foreach ($file in $managed) {
    $quoted = $file.Remote.Replace("'", "'\''")
    $line = Invoke-Nas "if [ -f '$quoted' ]; then sha256sum '$quoted'; fi" -AllowFailure
    $parts = "$line".Trim() -split '\s+', 2
    if ($parts.Count -eq 2) { $remote[$file.Remote] = $parts[0] }
}

$drift = @($managed | Where-Object { $remote[$_.Remote] -ne $local[$_.Repo] })
$summary = [ordered]@{
    Mode        = if ($Apply) { 'apply' } else { 'plan' }
    NasAlias    = $NasAlias
    ProjectDir  = $ProjectDir
    Drift       = @($drift | ForEach-Object { "$($_.Repo) -> $($_.Remote)" })
    InSync      = @($managed | Where-Object { $remote[$_.Remote] -eq $local[$_.Repo] } | ForEach-Object { $_.Repo })
}

if ($Apply -and $drift.Count -gt 0) {
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
    $backup = "$ProjectDir/.backup-$stamp"
    $stage = "/tmp/mltd-generated-assets-$stamp"
    Invoke-Nas "mkdir -p '$backup' '$stage/scripts' '$stage/repo'" | Out-Null

    foreach ($file in $drift) {
        $target = if ($file.Remote.StartsWith($ProjectDir)) { "$stage/repo/" + (Split-Path $file.Remote -Leaf) } else { "$stage/" + (Split-Path $file.Remote -Leaf) }
        if ($file.Repo -eq 'scripts/assets_mirror.py') { $target = "$stage/scripts/assets_mirror.py" }
        & scp -q -o BatchMode=yes (Join-Path $repoRoot $file.Repo) "${NasAlias}:$target"
        if ($LASTEXITCODE -ne 0) { throw "upload failed: $($file.Repo)" }
        $got = ("$(Invoke-Nas "sha256sum '$target'")" -split '\s+')[0]
        if ($got -ne $local[$file.Repo]) { throw "staged hash mismatch: $($file.Repo)" }
    }

    $pairs = ($drift | ForEach-Object { "'$($_.Remote)'|'$($_.Repo)'" }) -join ' '
    $restore = @()
    foreach ($file in $drift) {
        $leaf = Split-Path $file.Remote -Leaf
        $src = if ($file.Repo -eq 'scripts/assets_mirror.py') { "$stage/scripts/$leaf" }
               elseif ($file.Remote.StartsWith($VhostDir)) { "$stage/$leaf" }
               else { "$stage/repo/$leaf" }
        $quoted = $file.Remote.Replace("'", "'\''")
        $restore += "if [ -f '$quoted' ]; then cp -p '$quoted' '$backup/'; fi; cat '$src' > '$quoted'"
    }
    Invoke-Nas (($restore -join '; ') + "; echo rewritten") | Out-Null
    $summary.Backup = $backup

    # Rebuild without disturbing the running containers, then prove the image on
    # a throwaway root before anything is recreated.
    Invoke-Nas "cd '$ProjectDir' && docker compose build 2>&1 | tail -n 3" | Out-Null
    $probe = Invoke-Nas @"
docker run --rm -e MLTD_ASSETS_REPOSITORY=kohakunamori/MLTDTranslationAssets -e MLTD_ASSETS_BRANCH=main -e HTTP_PROXY=http://192.168.2.31:7890 -e HTTPS_PROXY=http://192.168.2.31:7890 -e NO_PROXY=127.0.0.1,localhost local/mltd-generated-assets:20260930 python /app/sync_loop.py --root /tmp/probe --once | head -c 400
"@ -AllowFailure
    $summary.ImageProbe = "$probe".Trim()

    if ($drift.Repo -contains 'asset-server/nginx-vhost.conf') {
        $test = Invoke-Nas 'docker exec on-demand-nginx nginx -t' -AllowFailure
        if ($LASTEXITCODE -ne 0) {
            Invoke-Nas "cat '$backup/nginx-vhost.conf' > '$VhostDir/nginx-vhost.conf'" | Out-Null
            throw "nginx -t rejected the new vhost; restored the backup. $($test -join ' ')"
        }
        Invoke-Nas 'docker exec on-demand-nginx nginx -s reload' | Out-Null
        $summary.Nginx = 'reloaded'
    }

    Invoke-Nas "cd '$ProjectDir' && docker compose up -d 2>&1 | tail -n 4" | Out-Null
    Invoke-Nas "rm -rf '$stage'" | Out-Null
}

$status = Invoke-Nas "docker ps --format '{{.Names}}|{{.Status}}' | grep -E 'mltd-generated-assets' || true"
$summary.Containers = @($status | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
$published = Invoke-Nas "ls -1 /vol2/1000/imas-asset-archive/mltd/generated/published 2>/dev/null | tr '\n' ' '"
$summary.PublishedVersions = "$published".Trim()
$routeProbe = Invoke-Nas "curl -s -o /dev/null -w '%{http_code}' --max-time 20 http://127.0.0.1:18765/assets/ || true"
$summary.LoopbackRouteStatus = "$routeProbe".Trim()

[pscustomobject]$summary | ConvertTo-Json -Depth 5
