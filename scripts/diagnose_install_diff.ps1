<#
.synopsis
    File-by-file diff of two Zapret-Zen install directories.

    Purpose: identify exactly which files differ between a CLEAN install
    and a UPDATED (in-app updated) install, to find stale Qt / Python
    runtime files that survive the updater.

.example
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\diagnose_install_diff.ps1 `
        -Clean "C:\Program Files\Zapret-Zen.clean" `
        -Updated "C:\Program Files\Zapret-Zen" `
        -Out "zapret_zen_install_diff.txt"

    -Clean   the freshly installed (never updated) directory
    -Updated the directory that was updated in place via the in-app updater
    -Out     text report path (optional; also printed to console)
#>
param(
    [Parameter(Mandatory = $true)][string]$Clean,
    [Parameter(Mandatory = $true)][string]$Updated,
    [string]$Out = "",
    [switch]$Fast
)

$ErrorActionPreference = "Stop"

function Get-FileSet([string]$Root) {
    $items = Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue
    $map = @{}
    foreach ($item in $items) {
        $rel = $item.FullName.Substring($Root.Length).TrimStart('\', '/')
        if ($Fast) {
            $map[$rel] = [pscustomobject]@{ Size = $item.Length; Stamp = $item.LastWriteTimeUtc.ToBinary(); SizeHash = "$($item.Length)|$($item.LastWriteTimeUtc.ToBinary())" }
        } else {
            $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
            $map[$rel] = [pscustomobject]@{ Size = $item.Length; Stamp = $item.LastWriteTimeUtc.ToBinary(); SizeHash = "$($item.Length)|$hash" }
        }
    }
    return $map
}

function Add-ReportLine([string]$Line) {
    $script:ReportLines += $Line
    Write-Output $Line
}

$ReportLines = @()
Add-ReportLine "== Zapret-Zen install diff =="
Add-ReportLine "clean:   $Clean"
Add-ReportLine "updated: $Updated"
if ($Fast) { Add-ReportLine "mode: fast (size+timestamp only)" } else { Add-ReportLine "mode: SHA256 file hashes" }
Add-ReportLine ""

Add-ReportLine "scanning..."
$cleanSet = Get-FileSet $Clean
$updatedSet = Get-FileSet $Updated
Add-ReportLine ("clean files:   {0}" -f $cleanSet.Count)
Add-ReportLine ("updated files: {0}" -f $updatedSet.Count)
Add-ReportLine ""

$isQtPath = {
    param($rel)
    $rel -match '(?i)(\\|/)platforms($|/)|(\\|/)styles($|/)|(\\|/)imageformats($|/)|(\\|/)iconengines($|/)|\bQt6.*\.dll$|\bqt\.conf$|\bPySide6($|/)|\.pyd$|python3[\d]*\.dll$'
}

$onlyUpdated = $updatedSet.Keys | Where-Object { -not $cleanSet.ContainsKey($_) } | Sort-Object
$onlyClean = $cleanSet.Keys | Where-Object { -not $updatedSet.ContainsKey($_) } | Sort-Object
$changed = @()
foreach ($key in ($cleanSet.Keys | Where-Object { $updatedSet.ContainsKey($_) })) {
    if ($cleanSet[$key].SizeHash -ne $updatedSet[$key].SizeHash) {
        $changed += $key
    }
}
$changed = $changed | Sort-Object

Add-ReportLine "== ONLY IN UPDATED (stale survivers - CULPRIT CANDIDATES) =="
Add-ReportLine ("count: {0}" -f $onlyUpdated.Count)
foreach ($rel in $onlyUpdated) {
    $flag = if (& $isQtPath $rel) { "  [QTRT]" } else { "" }
    Add-ReportLine ("  {0}{1}  ({2} bytes)" -f $rel, $flag, $updatedSet[$rel].Size)
}

Add-ReportLine ""
Add-ReportLine "== ONLY IN CLEAN (files the update removed) =="
Add-ReportLine ("count: {0}" -f $onlyClean.Count)
foreach ($rel in $onlyClean) {
    $flag = if (& $isQtPath $rel) { "  [QTRT]" } else { "" }
    Add-ReportLine ("  {0}{1}  ({2} bytes)" -f $rel, $flag, $cleanSet[$rel].Size)
}

Add-ReportLine ""
Add-ReportLine "== CHANGED (same path, different content) =="
Add-ReportLine ("count: {0}" -f $changed.Count)
foreach ($rel in $changed) {
    $alert = if (& $isQtPath $rel) { "  [QTRT]" } else { "" }
    $line = "  {0}{1}  clean={2}B updated={3}B" -f $rel, $alert, $cleanSet[$rel].Size, $updatedSet[$rel].Size
    if ($rel -match '(?i)\.dll$|\.exe$|\.pyd$') {
        try {
            $cv = (Get-Item -LiteralPath (Join-Path $Clean $rel)).VersionInfo.FileVersion
            $uv = (Get-Item -LiteralPath (Join-Path $Updated $rel)).VersionInfo.FileVersion
            if ($cv -ne $uv) {
                $line += "  version clean={0} updated={1}  <<< VERSION DRIFT" -f $cv, $uv
            }
        } catch { }
    }
    Add-ReportLine $line
}

Add-ReportLine ""
Add-ReportLine "== SUMMARY (top-level component participation) =="
$summary = @{}
foreach ($rel in ($onlyUpdated + $onlyClean + $changed)) {
    $top = ($rel -split '[\\/]')[0]
    if ($top -match '(?i)\.(py|pyd|dll|conf)') { $top = "_internal" }
    $summary[$top] = ($summary[$top] | Select-Object -First 1) + 1
}
foreach ($key in ($summary.Keys | Sort-Object)) {
    Add-ReportLine ("  {0}: {1}" -f $key, $summary[$key])
}

if ($Out) {
    $ReportLines | Set-Content -LiteralPath $Out -Encoding UTF8
    Write-Output ""
    Write-Output "Report written to: $Out"
}