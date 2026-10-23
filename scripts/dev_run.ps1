#Requires -Version 5.1
<#
    Zapret-Zen · DEV RUN
    --------------------
    Локальный запуск приложения из исходников.

    Скрипт сам подготовит окружение:
      1. найдёт системный Python 3.11+;
      2. создаст .venv, если его нет;
      3. установит зависимости проекта (pip install -e .);
      4. запустит приложение.

    Флаги (можно передавать через dev_run.bat):
      -Auto       пропустить меню, сразу запустить приложение;
      -Reinstall  перевставить зависимости проекта;
      -Rebuild    полностью пересоздать .venv;
      -Console    запускать с консолью (python.exe вместо pythonw.exe),
                  чтобы видеть логи приложения.
      Любые остальные аргументы передаются приложению как есть.
#>
[CmdletBinding()]
param(
    [switch]$Reinstall,
    [switch]$Rebuild,
    [switch]$Auto,
    [switch]$Console,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArgs
)

$ErrorActionPreference = 'Stop'
if (-not $AppArgs) { $AppArgs = @() }

# ---------------------------------------------------------------- пути ----
$Root     = Split-Path -Parent $PSScriptRoot          # корень репозитория
$VenvDir  = Join-Path $Root '.venv'
$VenvPy   = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPyw  = Join-Path $VenvDir 'Scripts\pythonw.exe'
$MinMajor = 3
$MinMinor = 11

# --------------------------------------------------------------- терминал --
# UTF-8, чтобы кириллица и рамки отображались корректно.
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
$script:Vt = $false
try { $script:Vt = [bool]$Host.UI.SupportsVirtualTerminal } catch { $script:Vt = $false }

# Кадры спиннера: braille в современных терминалах, ASCII — в legacy conhost.
$script:Frames = if ($script:Vt) { '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'.ToCharArray() } else { @('|', '/', '-', '\') }

$script:LastResult = $null

# ------------------------------------------------------------- элементы TUI --
function Write-Banner {
    $w = 48
    $title = '  ZAPRET-ZEN  ·  DEV RUN  '
    $pad = $w - $title.Length
    Write-Host ''
    Write-Host ('╔' + ('═' * $w) + '╗') -ForegroundColor Cyan
    Write-Host ('║' + $title + (' ' * $pad) + '║') -ForegroundColor Cyan
    Write-Host ('╚' + ('═' * $w) + '╝') -ForegroundColor Cyan
    Write-Host '  Автонастройка окружения и запуск приложения' -ForegroundColor DarkGray
    Write-Host ''
}

function Write-Step([string]$Text) { Write-Host ('  » ' + $Text) -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host ('  ✓ ' + $Text) -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host ('  ! ' + $Text) -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host ('  ✗ ' + $Text) -ForegroundColor Red }
function Write-Dim([string]$Text)  { Write-Host ('    ' + $Text) -ForegroundColor DarkGray }

function Show-LogTail([hashtable]$Result, [int]$Lines = 12) {
    if (-not $Result) { return }
    foreach ($log in @($Result.ErrLog, $Result.OutLog)) {
        if (-not $log -or -not (Test-Path $log)) { continue }
        $tail = Get-Content -LiteralPath $log -Tail $Lines -ErrorAction SilentlyContinue
        if ($tail) {
            Write-Dim ('── ' + (Split-Path $log -Leaf) + ' ' + ('─' * 30))
            foreach ($line in $tail) { Write-Dim $line }
        }
    }
}

function Wait-AnyKey {
    Write-Host ''
    Write-Host '  Нажмите любую клавишу для выхода...' -ForegroundColor DarkGray
    if ([Console]::IsInputRedirected) { return }   # не блокируем CI/пайпы
    try { [void]$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch {}
}

# --------------------------------------------------------- запуск с анимацией --
# Запускает внешний процесс в фоне, рисует спиннер, возвращает код выхода и логи.
function Invoke-Animated {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$Label = ''
    )
    $stamp = [guid]::NewGuid().ToString('N')
    $outLog = Join-Path $env:TEMP ("zz_dev_out_" + $stamp + '.log')
    $errLog = Join-Path $env:TEMP ("zz_dev_err_" + $stamp + '.log')

    Write-Host -NoNewline ('  ' + $Label + ' ')
    $proc = Start-Process -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $Root `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog

    $i = 0
    while (-not $proc.HasExited) {
        Write-Host -NoNewline ($script:Frames[$i % $script:Frames.Count] + "`b")
        $i++
        Start-Sleep -Milliseconds 120
    }
    Write-Host -NoNewline "`b "   # стереть кадр спиннера
    try { [void]$proc.WaitForExit() } catch {}

    # NB: на части систем свойство ExitCode у Start-Process -PassThru
    # возвращает null (перехват создания процессов), поэтому успех шагов
    # ниже проверяется по их результатам, а не по коду выхода.
    $exitCode = $null
    try { $exitCode = $proc.ExitCode } catch { $exitCode = $null }

    $result = @{
        ExitCode = $exitCode
        OutLog   = $outLog
        ErrLog   = $errLog
    }
    $script:LastResult = $result
    return $result
}

function Remove-AnimatedLogs {
    if (-not $script:LastResult) { return }
    foreach ($log in @($script:LastResult.OutLog, $script:LastResult.ErrLog)) {
        if ($log) { Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue }
    }
    $script:LastResult = $null
}

# -------------------------------------------------------------- поиск Python --
function Find-SystemPython {
    $candidates = New-Object System.Collections.ArrayList
    foreach ($ver in @('3.14', '3.13', '3.12', '3.11')) {
        [void]$candidates.Add(@{ File = 'py'; Args = @("-$ver"); Label = "py -$ver" })
    }
    [void]$candidates.Add(@{ File = 'py';        Args = @('-3'); Label = 'py -3' })
    [void]$candidates.Add(@{ File = 'python';    Args = @();     Label = 'python' })
    [void]$candidates.Add(@{ File = 'python3';   Args = @();     Label = 'python3' })

    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate.File -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $probe = @($candidate.Args) + @('-c', "import sys; print('%d.%d' % sys.version_info[:2])")
            $raw = (& $cmd.Source @probe 2>$null | Select-Object -First 1)
        } catch { continue }
        if (-not $raw) { continue }
        $raw = "$raw".Trim()
        $parts = $raw.Split('.')
        if ($parts.Count -lt 2) { continue }
        $major = 0; $minor = 0
        if (-not [int]::TryParse($parts[0], [ref]$major)) { continue }
        if (-not [int]::TryParse($parts[1], [ref]$minor)) { continue }
        if ($major -gt $MinMajor -or ($major -eq $MinMajor -and $minor -ge $MinMinor)) {
            return @{ Path = $cmd.Source; Version = $raw; Label = $candidate.Label }
        }
    }
    return $null
}

# ---------------------------------------------------------------- окружение --
$script:FreshVenv = $false

function Ensure-Venv {
    param([switch]$ForceRebuild)

    if ($ForceRebuild -and (Test-Path -LiteralPath $VenvDir)) {
        Write-Step 'Пересоздание окружения: удаление старого .venv'
        # winws/приложение может держать файлы — пробуем до победы.
        for ($i = 1; $i -le 5; $i++) {
            try { Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction Stop; break }
            catch { Start-Sleep -Milliseconds 500 }
        }
        if (Test-Path -LiteralPath $VenvDir) {
            throw 'Не удалось удалить .venv — закройте запущенные экземпляры приложения и повторите.'
        }
        Write-Ok 'Старое окружение удалено'
        $script:FreshVenv = $true
    }

    if (-not (Test-Path -LiteralPath $VenvPy)) {
        Write-Step "Поиск системного Python ($MinMajor.$MinMinor+)"
        $sysPython = Find-SystemPython
        if (-not $sysPython) {
            Write-Fail "Не найден Python $MinMajor.$MinMinor или новее."
            Write-Dim 'Установите Python: https://www.python.org/downloads/'
            Write-Dim 'При установке не забудьте галочку "Add python.exe to PATH".'
            throw 'Python не найден'
        }
        Write-Ok ("Найден " + $sysPython.Label + ' — версия ' + $sysPython.Version)

        Write-Step 'Создание виртуального окружения (.venv)'
        $result = Invoke-Animated -FilePath $sysPython.Path `
            -ArgumentList @('-m', 'venv', $VenvDir) `
            -Label 'python -m venv .venv'
        if (-not (Test-Path -LiteralPath $VenvPy)) {
            Show-LogTail $result
            throw 'Не удалось создать виртуальное окружение.'
        }
        Write-Ok 'Окружение создано'
        $script:FreshVenv = $true
    } else {
        Write-Dim '.venv уже существует — пропускаю создание'
    }
}

# ------------------------------------------------------------ зависимости --
function Test-PackageInstalled {
    try {
        $out = & $VenvPy -m pip show zapret-zen 2>$null
        return [bool]($out -match 'Name:\s*zapret-zen')
    } catch { return $false }
}

function Install-Deps {
    param([switch]$Force)

    $need = $Force -or $Reinstall -or $script:FreshVenv -or -not (Test-PackageInstalled)
    if (-not $need) {
        Write-Dim 'Зависимости уже установлены — пропускаю'
        return
    }

    if ($script:FreshVenv) {
        Write-Step 'Обновление pip / setuptools / wheel'
        $result = Invoke-Animated -FilePath $VenvPy `
            -ArgumentList @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '--upgrade', 'pip', 'setuptools', 'wheel') `
            -Label 'pip install --upgrade pip setuptools wheel'
        if ($null -ne $result.ExitCode -and $result.ExitCode -ne 0) {
            Write-Warn 'Не удалось обновить pip — продолжаю с текущей версией'
        } else {
            Write-Ok 'pip обновлён'
        }
    }

    Write-Step 'Установка зависимостей проекта (pip install -e .)'
    $result = Invoke-Animated -FilePath $VenvPy `
        -ArgumentList @('-m', 'pip', 'install', '--disable-pip-version-check', '-e', $Root) `
        -Label 'pip install -e .'
    # Успех проверяем по наличию пакета в окружении (ExitCode ненадёжен).
    if (-not (Test-PackageInstalled)) {
        Show-LogTail $result
        throw 'Не удалось установить зависимости проекта.'
    }
    Write-Ok 'Зависимости установлены'
    Remove-AnimatedLogs
}

# ------------------------------------------------------------------- запуск --
function Start-App {
    $argList = @('-m', 'zapret_zen.main') + $AppArgs
    if ($Console) {
        Write-Step 'Запуск приложения (консольный режим, Ctrl+C — остановить)'
        & $VenvPy @argList
        Write-Dim ('Приложение завершилось с кодом ' + $LASTEXITCODE)
    } else {
        Start-Process -FilePath $VenvPyw -ArgumentList $argList -WorkingDirectory $Root | Out-Null
        Write-Ok 'Приложение запущено — удачной разработки!'
    }
}

# --------------------------------------------------------------------- меню --
function Show-Menu {
    while ($true) {
        Write-Host ''
        Write-Host ('    ' + ('─' * 44)) -ForegroundColor DarkGray
        Write-Host -NoNewline '    Enter ' -ForegroundColor Green
        Write-Host '— запустить приложение'
        Write-Host -NoNewline '    D     ' -ForegroundColor Yellow
        Write-Host '— перевставить зависимости'
        Write-Host -NoNewline '    R     ' -ForegroundColor Yellow
        Write-Host '— пересоздать .venv'
        Write-Host -NoNewline '    Q     ' -ForegroundColor Red
        Write-Host '— выход'
        Write-Host ('    ' + ('─' * 44)) -ForegroundColor DarkGray
        Write-Host -NoNewline '    Ваш выбор: ' -ForegroundColor White

        try { $key = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { return 'RUN' }
        Write-Host ''

        switch ($key.VirtualKeyCode) {
            13 { return 'RUN' }        # Enter
            27 { return 'QUIT' }       # Esc
            81 { return 'QUIT' }       # Q
            68 { return 'DEPS' }       # D
            82 { return 'REBUILD' }    # R
            default { }                # любая другая клавиша — показать меню снова
        }
    }
}

# --------------------------------------------------------------------- main --
try {
    Set-Location -LiteralPath $Root
    Write-Banner

    Ensure-Venv -ForceRebuild:$Rebuild
    Install-Deps -Force:$Reinstall

    if (-not $Auto) {
        if ([Console]::IsInputRedirected) {
            # Нет интерактивного терминала — просто запускаем.
            Start-App
            exit 0
        }
        while ($true) {
            $action = Show-Menu
            switch ($action) {
                'RUN' { break }
                'QUIT' { Write-Host '  До встречи!' -ForegroundColor DarkGray; exit 0 }
                'DEPS' {
                    Install-Deps -Force
                    Remove-AnimatedLogs
                }
                'REBUILD' {
                    Ensure-Venv -ForceRebuild
                    Install-Deps -Force
                    Remove-AnimatedLogs
                }
            }
            if ($action -eq 'RUN') { break }
        }
    }

    Start-App
    Remove-AnimatedLogs
    exit 0
} catch {
    Write-Host ''
    Write-Fail ('Ошибка: ' + $_.Exception.Message)
    Wait-AnyKey
    exit 1
}
