"""
Screenshot capture script for Zapret-Zen README.
Takes PNG screenshots of each main page in light and dark themes, with rounded corners.
Usage: .venv\Scripts\python.exe scripts\take_screenshots.py
"""

import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "assets"
OUTPUT.mkdir(parents=True, exist_ok=True)

RADIUS = 16
PAGES = [(0, "dashboard"), (1, "services"), (3, "mods")]
THEMES = ["light", "dark"]


def main():
    import multiprocessing
    multiprocessing.freeze_support()

    from PySide6.QtCore import QTimer, QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Zapret-Zen")
    app.setOrganizationName("ZapretZen")

    from zapret_zen.bootstrap import bootstrap_application, build_startup_snapshot

    print("Bootstrapping...")
    try:
        ctx = bootstrap_application()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 1

    print("Building startup snapshot...")
    try:
        snap = build_startup_snapshot(ctx)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 1

    ctx.backend = None
    ctx.settings.update(theme="light", accent_color="#3b82f6")

    print("Creating MainWindow...")
    from zapret_zen.ui.main_window import MainWindow

    window = MainWindow(
        ctx,
        launch_hidden=False,
        startup_show_onboarding=False,
        startup_snapshot=snap if isinstance(snap, dict) else None,
        skip_autosettings=True,
    )
    app._screenshot_window = window

    window._submit_backend_task = lambda *a, **kw: None
    window._settings_dialog = None
    window._prime_cached_dialogs = lambda: None

    from PIL import Image, ImageDraw

    def grab_pil(widget):
        pixmap = widget.grab()
        qimg = pixmap.toImage()
        w, h = qimg.width(), qimg.height()
        ptr = qimg.constBits()
        arr = bytes(ptr)
        return Image.frombuffer("RGBA", (w, h), arr, "raw", "BGRA", 0, 1)

    def round_corners(img, radius=RADIUS):
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, img.width, img.height), radius=radius, fill=255)
        result = img.copy()
        result.putalpha(mask)
        return result

    theme_queue = list(THEMES)
    page_queue = []

    def start_theme_cycle():
        if not theme_queue:
            print("All screenshots done, quitting...")
            QTimer.singleShot(200, app.quit)
            return
        theme = theme_queue.pop()
        print(f"\n=== Theme: {theme} ===")
        ctx.settings.update(theme=theme, accent_color="#3b82f6")
        window._apply_theme()
        if hasattr(window, "_pages_host") and window._pages_host is not None:
            window._pages_host.set_accent_color("#3b82f6")
        if hasattr(window, "power_button") and window.power_button is not None:
            window.power_button.set_power_theme(theme, "#3b82f6")
        page_queue.extend([(idx, name, theme) for idx, name in reversed(PAGES)])
        QTimer.singleShot(500, capture_next)

    def capture_next():
        if not page_queue:
            QTimer.singleShot(200, start_theme_cycle)
            return
        idx, name, theme = page_queue.pop()
        print(f"  Switching to page {idx} ({name})...")
        window._switch_page(idx)
        QTimer.singleShot(1000, lambda n=name, t=theme: do_capture(n, t))

    def do_capture(name, theme):
        if name == "dashboard" and hasattr(window, "power_button"):
            window.power_button.setProperty("state", "on")
            window.power_button.set_active_state(True, animate=False)
            window.power_button.set_power_theme(theme, "#3b82f6")
        QCoreApplication.processEvents()
        try:
            img = grab_pil(window)
            rounded = round_corners(img)
            path = OUTPUT / f"screenshot_{name}_{theme}.png"
            rounded.save(str(path), "PNG")
            print(f"    Saved: {path} ({img.width}x{img.height})")
        except Exception as e:
            import traceback
            print(f"    Error: {e}")
            traceback.print_exc()
        if name == "dashboard" and hasattr(window, "power_button"):
            window.power_button.setProperty("state", "off")
            window.power_button.set_active_state(False, animate=False)
        QTimer.singleShot(200, capture_next)

    print("Showing window...")
    window.show()
    QTimer.singleShot(3000, start_theme_cycle)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
