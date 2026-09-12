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

from PySide6.QtCore import QLibraryInfo, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QWidget, QMainWindow

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
    lines.append(f"frozen: {_bool_or_dash(frozen)}")
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
    lines = []
    lines.append(f"# Zapret-Zen window diagnostic {QApplication.platformName()}")
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
    except Exception:
        pass
    try:
        lines.extend(_band_analysis(window.grab().toImage()))
    except Exception as error:
        lines.append("grab failed: " + repr(error))
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