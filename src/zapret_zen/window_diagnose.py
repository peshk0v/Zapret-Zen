"""Runtime window diagnostics for Zapret-Zen.

Used by `--diagnose-window` (packaged builds) and by the local
diagnostic harness.  Never changes window behavior; it only inspects
and reports.

The report intentionally mirrors the fields requested for the
"white rectangle below the window frame" investigation:

  * top-level widget list and full widget tree (class / objectName /
    visibility / geometry / flags / size policies)
  * window flags and window attributes of interest
  * per-monitor DPI data and the active Qt platform plugin path
  * a pixel-band analysis of window.grab() to detect an unpainted
    opaque-white region inside the window
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QWidget, QMainWindow
from zapret_zen.runtime_env import is_packaged_runtime

_ATTRS_OF_INTEREST = (
    "WA_TranslucentBackground",
    "WA_NoSystemBackground",
    "WA_OpaquePaintEvent",
    "WA_UpdatesDisabled",
    "WA_DeleteOnClose",
    "WA_StyledBackground",
    "WA_StaticContents",
    "WA_ForceUpdatesDisabled",
    "WA_DontShowOnScreen",
    "WA_QuitOnClose",
    "WA_ShowWithoutActivating",
)


def _bool_or_dash(value: object) -> str:
    return "yes" if value is True else ("no" if value is not None else "-")


def _geo(widget: QWidget) -> str:
    g = widget.geometry()
    return f"{g.x()},{g.y()} {g.width()}x{g.height()}"


def _qflags(flags: object) -> str:
    parts = []
    for member in (
        Qt.WindowType.Window,
        Qt.WindowType.Dialog,
        Qt.WindowType.Popup,
        Qt.WindowType.Tool,
        Qt.WindowType.FramelessWindowHint,
        Qt.WindowType.WindowTitleHint,
        Qt.WindowType.WindowSystemMenuHint,
        Qt.WindowType.WindowMinimizeButtonHint,
        Qt.WindowType.WindowMaximizeButtonHint,
        Qt.WindowType.WindowCloseButtonHint,
        Qt.WindowType.WindowContextHelpButtonHint,
        Qt.WindowType.WindowStaysOnTopHint,
    ):
        if flags & member:
            parts.append(str(member.name))
    return ",".join(parts) or "0"


def _widget_row(widget: QWidget, depth: int) -> str:
    attrs = " ".join(
        f"{name}={_bool_or_dash(widget.testAttribute(getattr(Qt.WidgetAttribute, name)))}"
        for name in _ATTRS_OF_INTEREST
    )
    return (
        f"{'  ' * depth}{type(widget).__name__} objectName={widget.objectName()!r} "
        f"visible={_bool_or_dash(widget.isVisible())} geo={_geo(widget)} "
        f"min={widget.minimumWidth()}x{widget.minimumHeight()} "
        f"max={widget.maximumWidth()}x{widget.maximumHeight()} "
        f"windowType={_qflags(widget.windowFlags())} {attrs}"
    )


def _candidate_plugin_paths() -> list[str]:
    candidate_roots = list(QApplication.libraryPaths())
    exe_dir = str(Path(sys.executable).resolve().parent)
    if exe_dir not in candidate_roots:
        candidate_roots.insert(0, exe_dir)
    return candidate_roots


def _platform_locations() -> list[str]:
    lines = []
    for index, path in enumerate(QApplication.libraryPaths()):
        lines.append(f"libraryPath[{index}]: {path}")
    platform_name = QApplication.platformName()
    lines.append(f"platformName: {platform_name}")
    plugin_name = "qwindows.dll" if sys.platform.startswith("win") else "qwindows.so"
    found = []
    for root in _candidate_plugin_paths():
        candidates = [Path(root) / "platforms" / plugin_name]
        if root != str(Path(sys.executable).resolve().parent):
            candidates.append(Path(root) / plugin_name)
        for plugin_file in candidates:
            if plugin_file.exists():
                stat = plugin_file.stat()
                found.append(f"  {plugin_file} size={stat.st_size} mtime={stat.st_mtime:.0f}")
                break
    if found:
        lines.append("qwindows plugin candidates (exe dir first = highest priority):")
        lines.extend(found)
    else:
        lines.append("qwindows plugin candidates: NOT FOUND in libraryPaths/exe dir")
    for name in ("QT_QPA_PLATFORM", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_PLUGIN_PATH", "QT_OPENGL", "QT_ENABLE_HIGHDPI_SCALING", "QT_SCALE_FACTOR"):
        import os
        if os.environ.get(name):
            lines.append(f"env {name}={os.environ[name]!r}")
    return lines


def _screen_dump(app: QApplication) -> list[str]:
    lines = []
    try:
        primary = app.primaryScreen()
        lines.append(
            f"primaryScreen: {primary.name()!r} geometry={primary.geometry().x()},{primary.geometry().y()} "
            f"{primary.geometry().width()}x{primary.geometry().height()} "
            f"dpr={primary.devicePixelRatio()} "
            f"logical={primary.logicalDotsPerInch():.1f} phys={primary.physicalDotsPerInch():.1f}"
        )
        for index, screen in enumerate(app.screens()):
            lines.append(
                f"screen[{index}]: {screen.name()!r} geometry={screen.geometry().x()},{screen.geometry().y()} "
                f"{screen.geometry().width()}x{screen.geometry().height()} dpr={screen.devicePixelRatio()}"
            )
    except Exception as error:
        lines.append(f"screens error: {error}")
    return lines


def _window_dump(window: QMainWindow, app: QApplication) -> list[str]:
    lines = []
    lines.append(f"platform: {QApplication.platformName()}")
    lines.append(f"qtVersion: {QLibraryInfo.version().toString()}")
    frozen = getattr(sys, "frozen", False)
    lines.append(f"python: {sys.version}")
    lines.append(f"packaged (is_packaged_runtime): {_bool_or_dash(is_packaged_runtime())}")
    lines.append(f"frozen (sys.frozen): {_bool_or_dash(frozen)}")
    if frozen and getattr(sys, "_MEIPASS", False):
        lines.append(f"MEIPASS: {sys._MEIPASS}")
    if getattr(sys, "nuitka_version", None):
        lines.append(f"nuitka: {sys.nuitka_version}")
    if getattr(sys, "argv0", None):
        lines.append(f"argv0: {sys.argv0}")

    g = window.geometry()
    fg = window.frameGeometry()
    lines.append(
        f"window.geometry: {g.x()},{g.y()} {g.width()}x{g.height()} "
        f"frameGeometry: {fg.x()},{fg.y()} {fg.width()}x{fg.height()} "
        f"size={window.width()}x{window.height()}"
    )
    lines.append(f"window.devicePixelRatio: {window.devicePixelRatio()}")
    lines.append(f"window.windowState: {window.windowState()}")
    lines.append(f"window.isWindow: {_bool_or_dash(window.isWindow())} isVisible={_bool_or_dash(window.isVisible())}")
    lines.append(f"window.flags: {_qflags(window.windowFlags())}")
    lines.append(f"window.sizeIncrement: {window.sizeIncrement().width()}x{window.sizeIncrement().height()}")

    top_level = []
    for widget in QApplication.topLevelWidgets():
        top_level.append(_widget_row(widget, 0))
    if not top_level:
        top_level.append("(no top-level widgets)")
    lines.append("topLevelWidgets:")
    lines.extend("  " + item for item in top_level)

    return lines


def _tree_lines(window: QMainWindow, app: QApplication, limit: int = 2500) -> list[str]:
    lines = []
    lines.append("widgetTree:")
    queue = [(window, 0)]
    count = 0
    while queue and count < limit:
        widget, depth = queue.pop(0)
        lines.append("  " + _widget_row(widget, depth))
        count += 1
        for child in widget.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            queue.append((child, depth + 1))
    if count >= limit:
        lines.append("  ...(tree truncated)")
    lines.append(f"treeNodes: {count}")
    return lines


def _screen_window_analysis(window: QWidget, output: Path | None = None) -> list[str]:
    """Grab the on-screen region behind/around the window corners using the
    system mask (SetWindowRgn), which widget.grab() cannot reveal."""
    lines = []
    try:
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            lines.append("screenWindow: no primary screen")
            return lines
        if not window.isVisible():
            lines.append("screenWindow: window not visible")
            return lines
        geo = window.geometry()
        top_left = window.mapToGlobal(QPoint(0, 0))
        shot = screen.grabWindow(0, top_left.x(), top_left.y(), geo.width(), geo.height())
        if shot.isNull():
            lines.append("screenWindow: grabWindow empty")
            return lines
        try:
            target_dir = output.parent if output is not None else None
            if target_dir is None:
                target_dir = Path.home()
            screenshot_path = Path(target_dir) / "zapret_zen_screen_window.png"
            recorded = shot.save(str(screenshot_path))
            lines.append(f"screenWindowSnapshot: {recorded} -> {screenshot_path}")
        except Exception as error:
            lines.append("screenWindowSnapshot failed: " + repr(error))
        image = shot.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        w = image.width()
        h = image.height()
        margin = min(8, w // 2, h // 2)

        def px(x: int, y: int):
            c = image.pixelColor(x, y)
            return (c.red(), c.green(), c.blue(), c.alpha())

        lines.append(f"screenWindow: {w}x{h}")
        corner_samples = []
        for cx, cy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            corner_samples.append(f"{px(cx, cy)}")
        lines.append("screenCornerRGBA: " + " | ".join(corner_samples))
        alpha_hits = 0
        for y in range(margin):
            for x in range(margin):
                if px(x, y)[3] < 255:
                    alpha_hits += 1
        lines.append(f"screenCornersWithAlpha: {alpha_hits}")
        edge_line = []
        cx0 = w // 2
        cy0 = h // 2
        left_sample = [px(0, cy0), px(1, cy0), px(2, cy0), px(6, cy0), px(8, cy0)]
        top_sample = [px(cx0, 0), px(cx0, 1), px(cx0, 2), px(cx0, 6), px(cx0, 8)]
        lines.append("screenLeftEdge: " + " | ".join(str(s) for s in left_sample))
        lines.append("screenTopEdge: " + " | ".join(str(s) for s in top_sample))
        from collections import Counter
        border_counter = Counter()
        for dy in range(h):
            border_counter[px(1, dy)] += 1
        for dx in range(w):
            border_counter[px(dx, 1)] += 1
        lines.append("screenBorder1pxTop2: " + str(border_counter.most_common(3)))
        diag = []
        for k in (0, 4, 8, 10, 12, 14, 15, 16, 17, 18, 20, 24, 28):
            diag.append(f"{k}:{px(min(k, w - 1), min(k, h - 1))}")
        lines.append("screenCornerDiagTL: " + " | ".join(diag))
        edge_row_profile = []
        for k in range(12):
            edge_row_profile.append(f"{k}:{px(k, 0)}")
        lines.append("screenRow0Profile: " + " | ".join(edge_row_profile))
        edge_row1_profile = []
        for k in range(0, 60, 4):
            edge_row1_profile.append(f"{k}:{px(k, 1)}")
        lines.append("screenRow1Profile: " + " | ".join(edge_row1_profile))
        mid_row = []
        wmid = w // 2
        for k in range(0, 10, 2):
            mid_row.append(f"{k}:{px(wmid, k)}")
        lines.append("screenTopMidCol: " + " | ".join(mid_row))
        return lines
    except Exception as error:
        lines.append("screenWindow failed: " + repr(error))
        return lines


def _band_analysis(image: QImage) -> list[str]:
    lines = []
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    width = image.width()
    height = image.height()
    lines.append(f"grab: {width}x{height} format={image.format()}")
    if width <= 0 or height <= 0:
        lines.append("grab: EMPTY")
        return lines
    try:
        bits = bytes(image.constBits())
    except Exception:
        bits = None
    bpp = 4
    stride = image.bytesPerLine()

    def pixel(x: int, y: int):
        if bits is None:
            color = image.pixelColor(x, y)
            return (color.red(), color.green(), color.blue(), color.alpha())
        offset = y * stride + x * bpp
        b, g, r, a = bits[offset], bits[offset + 1], bits[offset + 2], bits[offset + 3]
        return (r, g, b, a)

    transparent = 0
    opaque_white = 0
    opaque_dark = 0
    white_min_x = width
    white_min_y = height
    white_max_x = -1
    white_max_y = -1
    first_opaque_band = -1
    last_opaque_band = -1
    fully_opaque_rows = 0
    nonempty_alpha_rows = 0
    for y in range(height):
        row_has_alpha = False
        row_all_opaque = True
        for x in range(width):
            r, g, b, a = pixel(x, y)
            if a == 0:
                transparent += 1
                row_has_alpha = True
                row_all_opaque = False
            elif a == 255:
                row_all_opaque = row_all_opaque
                if r >= 245 and g >= 245 and b >= 245:
                    opaque_white += 1
                    if x < white_min_x:
                        white_min_x = x
                    if x > white_max_x:
                        white_max_x = x
                    if y < white_min_y:
                        white_min_y = y
                    if y > white_max_y:
                        white_max_y = y
                elif r < 60 and g < 60 and b < 60:
                    opaque_dark += 1
            else:
                row_has_alpha = True
                row_all_opaque = False
        if row_has_alpha:
            nonempty_alpha_rows += 1
            if first_opaque_band == -1:
                first_opaque_band = y
            last_opaque_band = y
        elif row_all_opaque:
            fully_opaque_rows += 1
    lines.append(f"pixels: total={width * height} transparent={transparent} opaqueWhite={opaque_white} opaqueDark={opaque_dark}")
    if opaque_white:
        lines.append(
            f"opaqueWhite bbox: x[{white_min_x}..{white_max_x}] y[{white_min_y}..{white_max_y}] "
            f"size={white_max_x - white_min_x + 1}x{white_max_y - white_min_y + 1}"
        )
    lines.append(
        f"alpha rows: rowsWithAnyAlpha={nonempty_alpha_rows} fullyOpaqueRows={fully_opaque_rows} "
        f"alphaRowRange y[{first_opaque_band}..{last_opaque_band}]"
    )
    samples = []
    for y in (0, 1, 2, 3, 4, 10, 50, 200, height // 2, height - 200, height - 50, height - 10, height - 5, height - 4, height - 3, height - 2, height - 1):
        if 0 <= y < height:
            r = g = b = a = 0
            if width:
                r, g, b, a = pixel(width // 2, y)
            samples.append(f"y={y}: mid=({r},{g},{b},a={a}) edge=({pixel(0, y)[0]},{pixel(0, y)[1]},{pixel(0, y)[2]},a={pixel(0, y)[3]})")
    lines.append("rowSamples:")
    lines.extend("  " + item for item in samples)
    return lines


def build_report(app: QApplication, window: QMainWindow, output: Path | None = None) -> str:
    lines = [f"# Zapret-Zen window diagnostic {QApplication.platformName()}"]
    try:
        lines.extend(_platform_locations())
        lines.extend(_screen_dump(app))
        lines.extend(_window_dump(window, app))
        lines.extend(_tree_lines(window, app))
        try:
            lines.append("app.styleSheet length: " + str(len(app.styleSheet())))
            window_qss = window.centralWidget().styleSheet() if window.centralWidget() is not None else ""
            lines.append("centralWidget.styleSheet length: " + str(len(window_qss)))
        except Exception:
            pass
        try:
            shell = window.findChild(QWidget, "WindowShell") or window.findChild(QWidget, "RootFrame")
            if shell is not None:
                lines.append(f"shell({shell.objectName()}) geo={_geo(shell)} visible={_bool_or_dash(shell.isVisible())}")
            wrapper = window.findChild(QWidget, "FullWindowGlow")
            if wrapper is not None:
                lines.append(f"glow({wrapper.objectName()}) geo={_geo(wrapper)}")
            fallback = bool(getattr(window, "_compositor_fallback_applied", False))
            mask_radius = getattr(window, "_opaque_window_mask_radius", None)
            rounded = getattr(window, "_native_corner_rounding", None)
            lines.append(
                "opaqueFallback: applied=" + str(fallback)
                + " windowMask=" + str(not window.mask().isNull())
                + " maskRadius=" + (str(mask_radius) if mask_radius is not None else "-")
                + " roundedCorners=" + (str(rounded) if rounded is not None else "-")
            )
        except Exception:
            pass
        try:
            lines.extend(_band_analysis(window.grab().toImage()))
            lines.extend(_screen_window_analysis(window, output=output))
        except Exception as error:
            lines.append("grab failed: " + repr(error))
    except Exception as error:
        lines.append("REPORT BUILD FAILED: " + repr(error))
        lines.extend(traceback.format_exc().splitlines())
    report = "\n".join(lines)
    if output is not None:
        try:
            output.write_text(report, encoding="utf-8")
        except Exception:
            pass
    return report


def run_diagnose(app: QApplication, window: QMainWindow, output: Path | None = None) -> int:
    report = build_report(app, window, output=output)
    print(report)
    try:
        snapshot_name = "zapret_zen_window_diagnose.png"
        target_dir = output.parent if output is not None else Path.home()
        snapshot = Path(target_dir) / snapshot_name
        image = window.grab().toImage()
        saved = image.save(str(snapshot))
        print(f"[diagnose] snapshot saved={saved} -> {snapshot}")
    except Exception as error:
        print(f"[diagnose] snapshot failed: {error!r}")
    return 0