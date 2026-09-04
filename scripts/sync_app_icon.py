from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    png_path = root / "ui_assets" / "icons" / "app.png"
    ico_path = root / "ui_assets" / "icons" / "app.ico"
    shell_png_path = root / "ui_assets" / "icons" / "installer_runtime_icon.png"
    shell_ico_path = root / "ui_assets" / "icons" / "app_shell.ico"

    # Build the single-resolution runtime ICO from the main PNG.  Uses Pillow
    # (already a dependency) instead of importing PySide6.QtGui so the build
    # script does not require Qt/OpenGL system libraries (libEGL/libGL) just to
    # re-encode an icon.
    image = Image.open(str(png_path)).convert("RGBA")
    image.save(str(ico_path), format="ICO")

    # Build the multi-resolution launcher/shell ICO (16..256px).
    shell_source = shell_png_path if shell_png_path.exists() else png_path
    base = Image.open(shell_source).convert("RGBA")
    if base.width < 256 or base.height < 256:
        base = base.resize((256, 256), Image.Resampling.LANCZOS)
    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (96, 96), (128, 128), (256, 256)]
    base.save(str(shell_ico_path), format="ICO", sizes=sizes)
    print(f"Synchronized app icons: runtime={ico_path} shell={shell_ico_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
