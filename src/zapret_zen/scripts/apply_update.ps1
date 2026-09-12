$ErrorActionPreference = 'SilentlyContinue'
$pidToWait = {{PID}}
$src = '{{SRC}}'
$dst = '{{DST}}'
$launch = '{{LAUNCH}}'
$tempRoot = '{{TEMP_ROOT}}'
$logPath = '{{LOG_PATH}}'
$preserve = @('data', 'mods', 'configs', 'cache', 'logs', 'backups')
Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] updater started')

function Remove-PathRobust([string]$targetPath) {
  if (-not (Test-Path $targetPath)) { return $true }
  for ($i = 0; $i -lt 6; $i++) {
    try {
      attrib -r -s -h $targetPath /s /d *> $null
    } catch {}
    try {
      Remove-Item $targetPath -Recurse -Force -ErrorAction Stop
      return $true
    } catch {
      Start-Sleep -Milliseconds 300
    }
  }
  $quarantineRoot = Join-Path $env:TEMP 'zapret_zen_update_quarantine'
  New-Item -ItemType Directory -Path $quarantineRoot -Force | Out-Null
  $moved = Join-Path $quarantineRoot ((Split-Path $targetPath -Leaf) + '_' + [guid]::NewGuid().ToString('N'))
  try {
    Move-Item $targetPath $moved -Force -ErrorAction Stop
    return $true
  } catch {
    return $false
  }
}

function Add-UpdateLog([string]$message) {
  try {
    Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] ' + $message)
  } catch {}
}

function Get-PythonRuntimeDlls([string]$dir) {
  return @(Get-ChildItem -LiteralPath $dir -File -Force -Filter 'python3*.dll' -ErrorAction SilentlyContinue)
}

function Test-StandalonePayload([string]$sourceDir) {
  return (Test-Path (Join-Path $sourceDir 'zapret_zen.exe')) -and
         (Test-Path (Join-Path $sourceDir 'python3.dll')) -and
         (@(Get-PythonRuntimeDlls $sourceDir).Count -gt 0)
}

function Test-InstalledStandalone([string]$targetDir) {
  return (Test-Path (Join-Path $targetDir 'zapret_zen.exe')) -and
         (Test-Path (Join-Path $targetDir 'python3.dll')) -and
         (@(Get-PythonRuntimeDlls $targetDir).Count -gt 0)
}

function Is-Preserved([string]$name) {
  if ($preserve -contains $name) { return $true }
  return $name.StartsWith('unins000')
}

function Clean-Replace([string]$sourceDir, [string]$targetDir) {
  New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
  Get-ChildItem -LiteralPath $targetDir -Force -ErrorAction SilentlyContinue | ForEach-Object {
    if (Is-Preserved $_.Name) { return }
    if (-not (Remove-PathRobust $_.FullName)) {
      Add-UpdateLog ('stale item NOT removed from target: ' + $_.FullName)
    }
  }
  Get-ChildItem -LiteralPath $sourceDir -Force -ErrorAction SilentlyContinue | ForEach-Object {
    if (Is-Preserved $_.Name) { return }
    $dest = Join-Path $targetDir $_.Name
    try {
      if ($_.PSIsContainer) {
        Copy-Item $_.FullName $dest -Recurse -Force -ErrorAction Stop
      } else {
        Copy-Item $_.FullName $dest -Force -ErrorAction Stop
      }
    } catch {
      Add-UpdateLog ('copy failed: ' + $_.FullName + ' -> ' + $dest + ' | ' + $_.Exception.Message)
    }
  }
}

for ($i = 0; $i -lt 40; $i++) {
  if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }
  Start-Sleep -Milliseconds 250
}

if (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) {
  Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] forcing old process stop')
  Stop-Process -Id $pidToWait -Force -ErrorAction SilentlyContinue
  for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
  }
}

try { sc stop zapret *> $null } catch {}
try { sc delete zapret *> $null } catch {}
foreach ($image in @('zapret_zen.exe', 'TgWsProxy_windows.exe', 'winws.exe')) {
  try { taskkill /F /T /IM $image *> $null } catch {}
}

New-Item -ItemType Directory -Path $dst -Force | Out-Null
Add-UpdateLog ('preserved items are kept in place: ' + (($preserve + @('unins000.*')) -join ', '))

$sourceIsStandalone = Test-StandalonePayload $src
if ($sourceIsStandalone) {
  Add-UpdateLog 'standalone payload detected: performing full clean replacement of the install directory'
} else {
  Add-UpdateLog 'payload not recognized as standalone; still performing full clean replacement'
}

Clean-Replace $src $dst
Add-UpdateLog 'install directory fully replaced (old runtime files removed, new payload copied)'

$runtimeRetry = @('_internal')
$runtimeRetry += @('zapret_zen.exe', 'python3.dll')
$runtimeRetry += @(Get-PythonRuntimeDlls $src | ForEach-Object { $_.Name })

if ($sourceIsStandalone -and -not (Test-InstalledStandalone $dst)) {
  Add-UpdateLog 'standalone validation failed after clean replace, retrying top-level runtime files'
  foreach ($fileName in $runtimeRetry) {
    $sourceFile = Join-Path $src $fileName
    $targetFile = Join-Path $dst $fileName
    if (Test-Path $sourceFile) {
      [void](Remove-PathRobust $targetFile)
      try {
        if (Test-Path $sourceFile -PathType Container) {
          New-Item -ItemType Directory -Path $targetFile -Force | Out-Null
          Copy-Item (Join-Path $sourceFile '*') $targetFile -Recurse -Force -ErrorAction Stop
        } else {
          New-Item -ItemType Directory -Path (Split-Path $targetFile -Parent) -Force | Out-Null
          Copy-Item $sourceFile $targetFile -Force -ErrorAction Stop
        }
        Add-UpdateLog ('runtime item copied: ' + $fileName)
      } catch {
        Add-UpdateLog ('runtime item copy failed: ' + $fileName + ' | ' + $_.Exception.Message)
      }
    }
  }
}

if ($sourceIsStandalone -and -not (Test-InstalledStandalone $dst)) {
  Add-UpdateLog 'standalone validation failed, aborting relaunch to avoid broken install'
  exit 2
}

Start-Sleep -Milliseconds 400

$qpaPlugin = Get-ChildItem -LiteralPath $dst -File -Recurse -ErrorAction SilentlyContinue |
             Where-Object { $_.Name -eq 'qwindows.dll' -and (Split-Path $_.DirectoryName -Leaf) -eq 'platforms' } |
             Select-Object -First 1
if (-not $qpaPlugin) {
  Add-UpdateLog 'ERROR: Qt Windows platform plugin (platforms/qwindows.dll) missing after update'
  Start-Process -FilePath (Join-Path $dst 'zapret_zen.exe') -WorkingDirectory $dst
  exit 3
}
Add-UpdateLog ('Qt platform plugin found: ' + $qpaPlugin.FullName)

$launch = Join-Path $dst 'zapret_zen.exe'
Start-Process -FilePath $launch -WorkingDirectory $dst
Add-Content -LiteralPath $logPath -Value ('[' + (Get-Date -Format s) + '] relaunched app')
Remove-Item $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
Remove-Item '{{SELF_DELETE}}' -Force -ErrorAction SilentlyContinue