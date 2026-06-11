from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from types import SimpleNamespace

from zapret_zen.bootstrap import bootstrap_application
from zapret_zen.ui.theme import load_theme_registry, list_available_themes
from zapret_zen.ui.main_window import MainWindow


def main():
    try:
        app = QApplication(sys.argv)
        ctx = bootstrap_application()
        # Run main window in hidden-launch mode to avoid priming dialogs that require additional services
        load_theme_registry(ctx.paths.themes_dir)
        win = MainWindow(ctx, launch_hidden=True, startup_show_onboarding=False, startup_snapshot={})
        win.show()

        themes = [tid for tid, _ in list_available_themes(ctx.paths.themes_dir, "ru")]
        out_dir = Path("screenshots")
        out_dir.mkdir(exist_ok=True)

        idx = 0

        def save_and_next(tid: str):
            try:
                p = out_dir / f"theme_{tid.replace(' ', '_')}.png"
                pix = win.grab()
                pix.save(str(p))
                print("SAVED", p)
            except Exception:
                print("ERROR saving for", tid)
                traceback.print_exc()

        def step():
            nonlocal idx
            if idx >= len(themes):
                QTimer.singleShot(500, app.quit)
                return
            tid = themes[idx]
            print("APPLY", tid)
            try:
                ctx.settings.update(theme=tid)
            except Exception:
                print("Failed to update settings for", tid)
            try:
                # ensure theme applied
                win._apply_theme()
            except Exception:
                print("_apply_theme failed for", tid)
            QTimer.singleShot(500, lambda t=tid: (save_and_next(t), _after_save()))
            idx += 1

        def _after_save():
            QTimer.singleShot(200, step)

        QTimer.singleShot(1000, step)
        app.exec()
    except Exception:
        traceback.print_exc()


if __name__ == '__main__':
    main()
