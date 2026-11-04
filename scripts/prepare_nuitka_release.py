from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


VERSION = "2.1.0"


def _should_skip_path(path: Path, source_dir: Path) -> bool:
    try:
        rel = path.relative_to(source_dir)
    except Exception:
        return False
    parts = rel.parts
    if any(part.startswith("tg-ws-proxy.bak.") for part in parts):
        return True
    lowered = tuple(part.lower() for part in parts)
    if "docs" in lowered and rel.name.lower() == "readme.md":
        return True
    return False


def _zip_with_root(source_dir: Path, zip_path: Path, root_name: str = "zapret_zen") -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(source_dir.rglob("*")):
            if item.is_dir():
                continue
            if not item.exists():
                continue
            if _should_skip_path(item, source_dir):
                continue
            rel = item.relative_to(source_dir)
            try:
                archive.write(item, Path(root_name) / rel)
            except (PermissionError, FileNotFoundError):
                continue


def _parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--x64-source", default=str(root / "dist_nuitka" / "main.dist"))
    parser.add_argument("--arm64-source", default=str(root / ".release_cache" / "win_arm64"))
    parser.add_argument("--payload-dir", default=str(root / "installer_payload"))
    parser.add_argument("--release-dir", default=str(root / f"release_{VERSION}"))
    parser.add_argument("--version", default=VERSION)
    parser.add_argument(
        "--skip-warp",
        action="store_true",
        help="Не предустанавливать бинарники Cloudflare WARP (runtime/warp) в payload",
    )
    parser.add_argument(
        "--warp-cache-dir",
        default=str(root / ".release_cache" / "warp_msi"),
        help="Кэш скачанных MSI Cloudflare WARP",
    )
    return parser.parse_args()


def _bundle_warp(root: Path, sources: list[Path], cache_dir: Path) -> None:
    """Кладёт предустановку WARP (runtime/warp) в каждый payload перед зипованием.

    Cloudflare выпускает MSI только под x64 — одни и те же бинарники идут и в
    x64-, и в arm64-payload (на ARM64 Windows они работают под эмуляцией,
    как и при установке кнопкой в UI). Ошибка шага валит сборку: релиз без
    предустановки WARP не должен уйти пользователю (см. --skip-warp для
    локальных/аварийных сборок).
    """
    script = root / "scripts" / "fetch_warp.py"
    for source in sources:
        target = source / "runtime" / "warp"
        if (target / "warp-cli.exe").is_file() and (target / "version.json").is_file():
            print(f"Bundled WARP already present, skipping: {target}")
            continue
        print(f"Bundling Cloudflare WARP into {target} ...")
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--target-dir",
                str(target),
                "--cache-dir",
                str(cache_dir),
            ]
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"WARP bundling failed for {target} (exit {proc.returncode}); "
                "rerun with --skip-warp to package without the bundled component"
            )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    args = _parse_args()
    version = str(args.version)
    x64_source = Path(args.x64_source).resolve()
    arm64_source = Path(args.arm64_source).resolve()
    payload_dir = Path(args.payload_dir).resolve()
    release_dir = Path(args.release_dir).resolve()

    if not x64_source.exists():
        raise FileNotFoundError(f"x64 Nuitka source not found: {x64_source}")
    if not arm64_source.exists():
        raise FileNotFoundError(f"arm64 source not found: {arm64_source}")

    if not args.skip_warp:
        _bundle_warp(root, [x64_source, arm64_source], Path(args.warp_cache_dir).resolve())

    payload_dir.mkdir(parents=True, exist_ok=True)
    _zip_with_root(x64_source, payload_dir / "win_x64.zip")
    _zip_with_root(arm64_source, payload_dir / "win_arm64.zip")

    release_dir.mkdir(parents=True, exist_ok=True)
    portable_x64_dir = release_dir / f"zapret_zen_{version}_portable_win_x64"
    portable_arm64_dir = release_dir / f"zapret_zen_{version}_portable_win_arm64"

    if portable_x64_dir.exists():
        shutil.rmtree(portable_x64_dir, ignore_errors=True)
    if portable_arm64_dir.exists():
        shutil.rmtree(portable_arm64_dir, ignore_errors=True)

    shutil.copytree(x64_source, portable_x64_dir, dirs_exist_ok=True)
    shutil.copytree(arm64_source, portable_arm64_dir, dirs_exist_ok=True)
    for backup_dir in portable_x64_dir.rglob("tg-ws-proxy.bak.*"):
        if backup_dir.is_dir():
            shutil.rmtree(backup_dir, ignore_errors=True)
    for backup_dir in portable_arm64_dir.rglob("tg-ws-proxy.bak.*"):
        if backup_dir.is_dir():
            shutil.rmtree(backup_dir, ignore_errors=True)
    _zip_with_root(portable_x64_dir, release_dir / f"zapret_zen_{version}_portable_win_x64.zip")
    _zip_with_root(portable_arm64_dir, release_dir / f"zapret_zen_{version}_portable_win_arm64.zip")

    note = release_dir / "README_RELEASE.txt"
    note.write_text(
        "x64 and ARM64 portable builds are prepared for the Nuitka release pipeline.\n"
        "The universal installer is expected to include both win_x64.zip and win_arm64.zip payloads.\n"
        "Cloudflare WARP binaries are bundled under runtime/warp (x64; runs under\n"
        "emulation on ARM64 Windows) and are updated in-app by the WARP component.\n",
        encoding="utf-8",
    )

    print(f"Prepared Nuitka payloads in: {payload_dir}")
    print(f"Prepared release folder in: {release_dir}")


if __name__ == "__main__":
    main()
