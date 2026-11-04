#!/usr/bin/env python3
"""Предустановка бинарников Cloudflare WARP в payload сборки релиза.

Скачивает официальный GA-MSI Cloudflare WARP для Windows (у Cloudflare
существует только x64-дистрибутив; под ARM64 бинарники работают под
эмуляцией Windows — так же, как их ставит кнопка «Установить» в UI),
извлекает его административной установкой `msiexec /a` (без установки
в систему) и раскладывает минимальный рабочий headless-набор файлов:

    warp-cli.exe, warp-svc.exe, aws_lc_fips_0_14_2_crypto.dll,
    wintun.dll, msvcp140*.dll, vcruntime140*.dll, concrt140.dll,
    vccorlib140.dll

плюс манифест version.json (тот же формат, что пишет
WarpUpdateManager._write_version_manifest).

GUI-часть Cloudflare WARP (Flutter-приложение, warp-dex/warp-diag/
warp-updater, sentry) НЕ нужна: компонент Zapret-Zen работает только
через warp-cli + warp-svc, поэтому в дистрибутив не идут ~100 МБ
графических файлов.

Используется из scripts/prepare_nuitka_release.py перед зипованием
payload, чтобы пользователь получал WARP «в комплекте»: после установки
или обновления приложения компонент уже установлен, дальше его
обновляет встроенное автообновление (WarpUpdateManager).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

WARP_DOWNLOAD_URL = "https://downloads.cloudflareclient.com/v1/download/windows/ga"

_VERSION_RE = re.compile(r"(\d{4}\.\d+(?:\.\d+){1,3})")

MSI_MIN_BYTES = 5 * 1024 * 1024
DOWNLOAD_TIMEOUT = 600
EXTRACT_TIMEOUT = 300

# Минимальный рабочий набор для headless-режима (warp-cli + warp-svc).
# aws_lc — единственный локальный статический импорт warp-svc.
REQUIRED_FILES = (
    "warp-cli.exe",
    "warp-svc.exe",
    "aws_lc_fips_0_14_2_crypto.dll",
)
# wintun упоминается warp-svc (динамическая загрузка), VC-runtime — на
# машинах без установленного Microsoft Visual C++ Redistributable.
OPTIONAL_PATTERNS = (
    "wintun.dll",
    "msvcp140*.dll",
    "vcruntime140*.dll",
    "concrt140.dll",
    "vccorlib140.dll",
)

PE_MACHINE_X64 = 0x8664


class BundlingError(RuntimeError):
    """Ошибка шага предустановки WARP (машинно-читаемая причина)."""


def _subprocess_kwargs() -> dict:
    kwargs: dict = {"capture_output": True, "check": False}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        kwargs["startupinfo"] = startup
    return kwargs


# ── Release metadata / download ──────────────────────────────────────────────
def fetch_release_version() -> str:
    """Версия текущего GA-релиза по Content-Disposition/финальному URL."""
    request = urllib.request.Request(
        WARP_DOWNLOAD_URL, method="HEAD", headers={"User-Agent": "ZapretZen-build"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        disposition = str(response.headers.get("Content-Disposition") or "")
        final_url = str(response.geturl() or "")
    match = _VERSION_RE.search(disposition) or _VERSION_RE.search(final_url)
    return match.group(1) if match else ""


def download_msi(destination: Path) -> None:
    """Скачивает GA-MSI в кэш (повторно использует уже скачанный файл)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size >= MSI_MIN_BYTES:
        print(f"[fetch_warp] using cached MSI: {destination}")
        return
    temp_path = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(
        WARP_DOWNLOAD_URL, headers={"User-Agent": "ZapretZen-build"}
    )
    print(f"[fetch_warp] downloading {WARP_DOWNLOAD_URL}")
    started = time.monotonic()
    received = 0
    next_report = 16 * 1024 * 1024
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temp_path.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                if received >= next_report:
                    elapsed = max(0.001, time.monotonic() - started)
                    print(
                        f"[fetch_warp]   {received // (1024 * 1024)} MB"
                        f" ({received // elapsed // (1024 * 1024)} MB/s)"
                    )
                    next_report += 16 * 1024 * 1024
    except Exception as error:  # noqa: BLE001 - любая сетевая ошибка -> в BundlingError
        temp_path.unlink(missing_ok=True)
        raise BundlingError(f"WARP MSI download failed: {error}") from error
    if received < MSI_MIN_BYTES:
        temp_path.unlink(missing_ok=True)
        raise BundlingError(f"WARP MSI is too small ({received} bytes); refusing to bundle")
    temp_path.replace(destination)
    print(f"[fetch_warp] downloaded {received // (1024 * 1024)} MB in {time.monotonic() - started:.0f}s")


# ── MSI extraction / payload collection ──────────────────────────────────────
def extract_msi(msi_path: Path, target_dir: Path) -> None:
    """Административная установка MSI во временный каталог (без установки)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "msiexec",
                "/a",
                str(msi_path),
                "/qn",
                f"TARGETDIR={target_dir}",
                "/norestart",
            ],
            timeout=EXTRACT_TIMEOUT,
            **_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as error:
        raise BundlingError("msiexec extraction timed out") from error
    except OSError as error:
        raise BundlingError(f"msiexec launch failed: {error}") from error
    # 0 = ok, 3010 = reboot required (файлы при этом скопированы).
    if int(proc.returncode or 0) not in (0, 3010):
        raise BundlingError(f"msiexec exited with code {proc.returncode}")


def product_version(exe_path: Path) -> str:
    """ProductVersion файла (авторитетный источник версии, как в приложении)."""
    script = f"(Get-Item -LiteralPath '{str(exe_path).replace(chr(39), chr(39) * 2)}').VersionInfo.ProductVersion"
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            timeout=30,
            **_subprocess_kwargs(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    value = (proc.stdout or b"").decode("utf-8", errors="ignore").strip()
    return value.splitlines()[-1].strip() if value else ""


def pe_machine(path: Path) -> int:
    """Машина PE-заголовка (0x8664 = x64, 0xAA64 = arm64)."""
    try:
        with path.open("rb") as handle:
            head = handle.read(0x1000)
            if head[:2] != b"MZ":
                return 0
            pe_offset = struct.unpack_from("<I", head, 0x3C)[0]
            if pe_offset + 6 <= len(head):
                if head[pe_offset : pe_offset + 4] != b"PE\x00\x00":
                    return 0
                return struct.unpack_from("<H", head, pe_offset + 4)[0]
            handle.seek(pe_offset)
            header = handle.read(8)
    except OSError:
        return 0
    if len(header) < 8 or header[:4] != b"PE\x00\x00":
        return 0
    return struct.unpack("<H", header[4:6])[0]


def collect_payload(source_dir: Path, staging: Path) -> list[str]:
    """Копирует минимальный рабочий набор WARP в staging-каталог."""
    missing = [name for name in REQUIRED_FILES if not (source_dir / name).is_file()]
    if missing:
        raise BundlingError(f"MSI payload is missing: {', '.join(missing)}")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    copied: list[str] = []
    for name in REQUIRED_FILES:
        shutil.copy2(source_dir / name, staging / name)
        copied.append(name)
    seen = set(copied)
    for pattern in OPTIONAL_PATTERNS:
        for entry in sorted(source_dir.glob(pattern)):
            if entry.is_file() and entry.name not in seen:
                shutil.copy2(entry, staging / entry.name)
                seen.add(entry.name)
                copied.append(entry.name)
    if not (staging / "warp-cli.exe").is_file() or not (staging / "warp-svc.exe").is_file():
        raise BundlingError("staging validation failed: warp-cli/warp-svc missing")
    return copied


def write_manifest(warp_dir: Path, version: str, source: str) -> None:
    payload = {
        "version": version or "unknown",
        "source": source,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "bundled": True,
    }
    (warp_dir / "version.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── Main entry point ─────────────────────────────────────────────────────────
def ensure_bundled_warp(target_dir: Path, cache_dir: Path, msi_path: Path | None = None) -> str:
    """Собирает runtime/warp в target_dir. Возвращает итоговую версию.

    Каталог target_dir пересобирается целиком (детерминированно), при
    ошибках бросает BundlingError — вызывающая сторона решает, падать
    или собирать дистрибутив без предустановки.
    """
    if not sys.platform.startswith("win"):
        raise BundlingError("WARP bundling requires Windows (msiexec /a)")

    if msi_path is not None:
        if not msi_path.is_file():
            raise BundlingError(f"local MSI not found: {msi_path}")
        source_label = str(msi_path)
        version_hint = ""
    else:
        version_hint = fetch_release_version()
        if not version_hint:
            raise BundlingError("unable to resolve current WARP version from the GA endpoint")
        msi_path = cache_dir / f"Cloudflare-WARP-{version_hint}.msi"
        download_msi(msi_path)
        source_label = WARP_DOWNLOAD_URL

    with tempfile.TemporaryDirectory(prefix="zz_warp_bundle_") as temp_root:
        extract_root = Path(temp_root) / "extract"
        extract_msi(msi_path, extract_root)
        cli = next(extract_root.rglob("warp-cli.exe"), None)
        if cli is None:
            raise BundlingError("warp-cli.exe not found in extracted MSI")
        machine = pe_machine(cli)
        if machine != PE_MACHINE_X64:
            raise BundlingError(f"unexpected warp-cli architecture: 0x{machine:04X} (expected x64)")
        staging = Path(temp_root) / "staging"
        copied = collect_payload(cli.parent, staging)
        version = product_version(staging / "warp-cli.exe") or version_hint
        if not version:
            raise BundlingError("unable to determine WARP version")
        write_manifest(staging, version, source_label)

        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        # staging живёт в %TEMP% (возможно, другой диск) — move, не rename.
        shutil.move(str(staging), str(target_dir))

    total_bytes = sum(
        entry.stat().st_size for entry in target_dir.rglob("*") if entry.is_file()
    )
    print(
        f"[fetch_warp] bundled Cloudflare WARP {version}: "
        f"{len(copied)} files, {total_bytes / (1024 * 1024):.1f} MB -> {target_dir}"
    )
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bundle Cloudflare WARP binaries into a build payload")
    parser.add_argument("--target-dir", required=True, help="Каталог назначения (например <payload>/runtime/warp)")
    parser.add_argument(
        "--cache-dir",
        default=str(Path(__file__).resolve().parents[1] / ".release_cache" / "warp_msi"),
        help="Кэш скачанных MSI",
    )
    parser.add_argument("--msi", default="", help="Локальный MSI вместо скачивания (offline/тесты)")
    parser.add_argument(
        "--allow-skip",
        action="store_true",
        help="При ошибке выйти с кодом 0 (для локальных сборок; релизный пайплайн падает без флага)",
    )
    args = parser.parse_args(argv)
    try:
        ensure_bundled_warp(
            Path(args.target_dir),
            Path(args.cache_dir),
            Path(args.msi) if args.msi else None,
        )
        return 0
    except Exception as error:  # noqa: BLE001 - сообщаем и кодом возврата
        print(f"[fetch_warp] ERROR: {error}", file=sys.stderr)
        if args.allow_skip:
            print("[fetch_warp] WARNING: --allow-skip set; continuing without bundled WARP", file=sys.stderr)
            return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
