from __future__ import annotations

import ctypes
import math
import os
import platform
import re
import time
import sys
import threading
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from zapret_zen import __version__
from zapret_zen.domain import ComponentDefinition, ComponentState, FileRecord, NotificationEntry
from zapret_zen.services.service_catalog import (
    FORTNITE_GENERAL_PRIORITY,
    SERVICE_PRESETS,
    ServicePreset,
    prioritize_generals_for_services,
)
from PySide6.QtCore import QCoreApplication, QEasingCurve, QEvent, QEventLoop, QObject, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt, QTimer, Signal, QPropertyAnimation, QParallelAnimationGroup, Property, QByteArray
from PySide6.QtGui import QAction, QActionGroup, QColor, QCloseEvent, QFont, QFontDatabase, QFontMetrics, QIcon, QImage, QKeyEvent, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient, QRegion, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QScrollArea,
    QStackedLayout,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QInputDialog,
    QLayout,
    QProgressBar,
    QToolButton,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from zapret_zen.bootstrap import ApplicationContext
from zapret_zen.ui.theme import _get_theme, build_stylesheet, is_light_theme, list_available_themes, load_theme_registry


class WindowsTaskbarIntegration:
    TBPF_NOPROGRESS = 0
    TBPF_INDETERMINATE = 1
    TBPF_NORMAL = 2
    TBPF_ERROR = 4
    TBPF_PAUSED = 8

    FLASHW_STOP = 0
    FLASHW_TRAY = 0x00000002
    FLASHW_TIMERNOFG = 0x0000000C

    def __init__(self) -> None:
        self._available = platform.system().lower() == "windows"
        self._taskbar: ctypes.c_void_p | None = None
        if not self._available:
            return
        try:
            self._init_taskbar()
        except Exception:
            self._available = False
            self._taskbar = None

    def _init_taskbar(self) -> None:
        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

            def __init__(self, value: str) -> None:
                import uuid

                item = uuid.UUID(str(value).strip("{}"))
                bytes_le = item.bytes_le
                self.Data1 = int.from_bytes(bytes_le[0:4], "little")
                self.Data2 = int.from_bytes(bytes_le[4:6], "little")
                self.Data3 = int.from_bytes(bytes_le[6:8], "little")
                self.Data4 = (ctypes.c_ubyte * 8).from_buffer_copy(bytes_le[8:16])

        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        clsid_taskbar = GUID("{56FDF344-FD6D-11D0-958A-006097C9A090}")
        iid_taskbar = GUID("{602D4995-B13A-429B-A66E-1935E44F4317}")
        taskbar = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid_taskbar),
            None,
            0x1,
            ctypes.byref(iid_taskbar),
            ctypes.byref(taskbar),
        )
        if hr != 0 or not taskbar.value:
            raise OSError(f"ITaskbarList3 unavailable: HRESULT {hr}")
        self._taskbar = taskbar
        self._call_taskbar(3, ctypes.c_long)

    def _call_taskbar(self, index: int, restype: object, *args: object) -> int:
        if self._taskbar is None:
            return -1
        vtable = ctypes.cast(self._taskbar, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        argtypes = [ctypes.c_void_p]
        for arg in args:
            if isinstance(arg, int):
                argtypes.append(ctypes.c_void_p if arg > 0xFFFFFFFF else ctypes.c_uint)
            else:
                argtypes.append(type(arg))
        prototype = ctypes.WINFUNCTYPE(restype, *argtypes)
        method = prototype(vtable[index])
        return int(method(self._taskbar, *args))

    def set_progress_state(self, hwnd: int, state: int) -> None:
        if not self._available or not hwnd:
            return
        try:
            prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
            vtable = ctypes.cast(self._taskbar, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents if self._taskbar else None
            if vtable is None:
                return
            method = prototype(vtable[10])
            method(self._taskbar, ctypes.c_void_p(hwnd), int(state))
        except Exception:
            self._available = False

    def set_progress_value(self, hwnd: int, value: int, maximum: int = 100) -> None:
        if not self._available or not hwnd:
            return
        try:
            prototype = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_ulonglong,
                ctypes.c_ulonglong,
            )
            vtable = ctypes.cast(self._taskbar, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents if self._taskbar else None
            if vtable is None:
                return
            method = prototype(vtable[9])
            method(self._taskbar, ctypes.c_void_p(hwnd), max(0, int(value)), max(1, int(maximum)))
        except Exception:
            self._available = False

    def flash_attention(self, hwnd: int) -> None:
        if not self._available or not hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint),
            ]

        try:
            info = FLASHWINFO(
                ctypes.sizeof(FLASHWINFO),
                ctypes.c_void_p(hwnd),
                self.FLASHW_TRAY | self.FLASHW_TIMERNOFG,
                0,
                0,
            )
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def clear_flash(self, hwnd: int) -> None:
        if not self._available or not hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint),
            ]

        try:
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), ctypes.c_void_p(hwnd), self.FLASHW_STOP, 0, 0)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass


@dataclass(slots=True)
class NavItem:
    key: str
    icon_file: str
    tooltip: str


@dataclass(slots=True)
class StatusBadge:
    key: str
    icon_file: str
    title: str
    title_label: QLabel
    icon_label: QLabel
    value_label: QLabel


class _UiSignals(QObject):
    toggle_done = Signal()
    component_action_done = Signal(str)
    general_test_progress = Signal(object)
    general_test_done = Signal(object)
    update_check_done = Signal(object, bool)
    update_prepare_done = Signal(object)
    page_payload_ready = Signal(str, object)


class SidebarPanel(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._border_color = QColor("#24304a")
        self._cut_size = 18
        self._highlight_rect = QRect(0, 0, 0, 0)
        self._highlight_fill = QColor(69, 81, 109, 72)
        self._highlight_border = QColor("#4f73b3")
        self._highlight_animation: QPropertyAnimation | None = None

    def set_theme(self, theme: str) -> None:
        light = is_light_theme(theme)
        if light:
            self._border_color = QColor("#d2ddeb")
            self._highlight_fill = QColor(191, 211, 243, 118)
            self._highlight_border = QColor("#9cb7ea")
        elif theme == "night":
            self._border_color = QColor("#24304a")
            self._highlight_fill = QColor(79, 115, 179, 68)
            self._highlight_border = QColor("#4f73b3")
        else:
            self._border_color = QColor("#2f333a")
            self._highlight_fill = QColor(96, 108, 124, 66)
            self._highlight_border = QColor("#717a87")
        self.update()

    def paintEvent(self, event: QEvent) -> None:
        super().paintEvent(event)
        if not self._highlight_rect.isNull():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(self._highlight_border, 1))
            painter.setBrush(self._highlight_fill)
            painter.drawRoundedRect(QRectF(self._highlight_rect), 12, 12)

    def _get_highlight_rect(self) -> QRect:
        return QRect(self._highlight_rect)

    def _set_highlight_rect(self, rect: QRect) -> None:
        self._highlight_rect = QRect(rect)
        self.update()

    highlightRect = Property(QRect, _get_highlight_rect, _set_highlight_rect)

    def move_highlight(self, rect: QRect, *, animated: bool = True) -> None:
        target = QRect(rect)
        if target.isNull():
            return
        if self._highlight_animation is not None:
            self._highlight_animation.stop()
        if not animated or self._highlight_rect.isNull():
            self._highlight_rect = target
            self.update()
            return
        animation = QPropertyAnimation(self, b"highlightRect", self)
        animation.setDuration(260)
        animation.setStartValue(self._highlight_rect)
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        animation.start()
        self._highlight_animation = animation

    def clear_highlight(self) -> None:
        if self._highlight_animation is not None:
            self._highlight_animation.stop()
            self._highlight_animation = None
        self._highlight_rect = QRect()
        self.update()


class AnimatedNavButton(QToolButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover_progress = 0.0
        self._icon_dx = 0.0
        self._icon_dy = 0.0
        self._icon_scale = 1.0
        self._glow_pos = QPointF(22.0, 22.0)
        self._light_theme = False
        self._theme_name = "night"
        self._anims: list[QPropertyAnimation] = []

    def set_nav_theme(self, theme: str) -> None:
        self._theme_name = theme
        self._light_theme = is_light_theme(theme)
        self.update()

    def _stop_anims(self) -> None:
        for anim in self._anims:
            anim.stop()
        self._anims.clear()

    def _animate_property(self, name: bytes, start: float, end: float, duration: int) -> None:
        animation = QPropertyAnimation(self, name, self)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setDuration(duration)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        animation.finished.connect(lambda: self._anims.remove(animation) if animation in self._anims else None)
        self._anims.append(animation)
        animation.start()

    def enterEvent(self, event: QEvent) -> None:
        self._animate_property(b"hoverProgress", self._hover_progress, 1.0, 220)
        self._animate_property(b"iconScale", self._icon_scale, 1.035, 240)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._animate_property(b"hoverProgress", self._hover_progress, 0.0, 220)
        self._animate_property(b"iconScale", self._icon_scale, 1.0, 220)
        self._animate_property(b"iconDx", self._icon_dx, 0.0, 180)
        self._animate_property(b"iconDy", self._icon_dy, 0.0, 180)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        self._glow_pos = QPointF(pos.x(), pos.y())
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        dx = max(-1.0, min(1.0, (pos.x() - center.x()) / max(8.0, center.x())))
        dy = max(-1.0, min(1.0, (pos.y() - center.y()) / max(8.0, center.y())))
        self._icon_dx += (dx * 1.1 - self._icon_dx) * 0.18
        self._icon_dy += (dy * 1.1 - self._icon_dy) * 0.18
        self.update()
        super().mouseMoveEvent(event)

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 12.0
        checked = self.isChecked()

        base_icon_dx = float(self.property("baseIconDx") or 0.0)
        if self._light_theme:
            base_fill = QColor(181, 204, 242, 34)
            hover_fill = QColor(194, 214, 245, int(30 * self._hover_progress))
            checked_fill = QColor(0, 0, 0, 0)
            border = QColor(191, 210, 240, int(88 * self._hover_progress))
            glow_color = QColor(255, 255, 255, int(44 * self._hover_progress))
        elif self._theme_name == "night":
            base_fill = QColor(90, 112, 152, 22)
            hover_fill = QColor(95, 124, 177, int(26 * self._hover_progress))
            checked_fill = QColor(0, 0, 0, 0)
            border = QColor(102, 132, 191, int(84 * self._hover_progress))
            glow_color = QColor(126, 164, 255, int(58 * self._hover_progress))
        else:
            base_fill = QColor(126, 133, 145, 20)
            hover_fill = QColor(144, 151, 165, int(24 * self._hover_progress))
            checked_fill = QColor(0, 0, 0, 0)
            border = QColor(154, 162, 174, int(78 * self._hover_progress))
            glow_color = QColor(208, 216, 232, int(34 * self._hover_progress))

        fill = QColor(checked_fill if checked else base_fill)
        if not checked and self._hover_progress > 0:
            mix = max(0.0, min(1.0, self._hover_progress))
            fill = QColor(
                int(base_fill.red() + (hover_fill.red() - base_fill.red()) * mix),
                int(base_fill.green() + (hover_fill.green() - base_fill.green()) * mix),
                int(base_fill.blue() + (hover_fill.blue() - base_fill.blue()) * mix),
                int(base_fill.alpha() + (hover_fill.alpha() - base_fill.alpha()) * mix),
            )
        painter.setPen(QPen(border if (border.alpha() > 0 and not checked) else QColor(0, 0, 0, 0), 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)

        if self._hover_progress > 0:
            glow = QRadialGradient(self._glow_pos, max(self.width(), self.height()) * 0.75)
            glow.setColorAt(0.0, glow_color)
            glow.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawRoundedRect(rect, radius, radius)

        icon_size = max(20, round(26 * self._icon_scale))
        requested_icon = self.iconSize()
        if requested_icon.isValid():
            icon_size = max(18, round(max(requested_icon.width(), requested_icon.height()) * self._icon_scale))
        pixmap = self.icon().pixmap(icon_size, icon_size)
        target = QRectF(
            (self.width() - icon_size) / 2.0 + self._icon_dx + base_icon_dx,
            (self.height() - icon_size) / 2.0 + self._icon_dy,
            icon_size,
            icon_size,
        )
        painter.drawPixmap(target, pixmap, QRectF(0, 0, pixmap.width(), pixmap.height()))

    def _get_hover_progress(self) -> float:
        return self._hover_progress

    def _set_hover_progress(self, value: float) -> None:
        self._hover_progress = float(value)
        self.update()

    def _get_icon_dx(self) -> float:
        return self._icon_dx

    def _set_icon_dx(self, value: float) -> None:
        self._icon_dx = float(value)
        self.update()

    def _get_icon_dy(self) -> float:
        return self._icon_dy

    def _set_icon_dy(self, value: float) -> None:
        self._icon_dy = float(value)
        self.update()

    def _get_icon_scale(self) -> float:
        return self._icon_scale

    def _set_icon_scale(self, value: float) -> None:
        self._icon_scale = float(value)
        self.update()

    hoverProgress = Property(float, _get_hover_progress, _set_hover_progress)
    iconDx = Property(float, _get_icon_dx, _set_icon_dx)
    iconDy = Property(float, _get_icon_dy, _set_icon_dy)
    iconScale = Property(float, _get_icon_scale, _set_icon_scale)


class ClickSelectComboBox(QComboBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        view = self.view()
        if view is not None:
            view.viewport().installEventFilter(self)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()

    def showPopup(self) -> None:
        super().showPopup()
        view = self.view()
        if view is not None:
            view.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        view = self.view()
        if view is not None and watched is view.viewport() and event.type() == QEvent.Type.MouseButtonRelease:
            index = view.indexAt(event.pos())
            if index.isValid():
                self.setCurrentIndex(index.row())
                self.hidePopup()
                self.activated.emit(index.row())
                return True
        return super().eventFilter(watched, event)


class NotificationBellButton(QToolButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._has_unread = False

    def set_unread(self, has_unread: bool) -> None:
        if self._has_unread == bool(has_unread):
            return
        self._has_unread = bool(has_unread)
        self.update()

    def paintEvent(self, event: QEvent) -> None:
        super().paintEvent(event)
        if not self._has_unread:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ef4444"))
        size = 7
        painter.drawEllipse(self.width() - 11, 5, size, size)


class GitHubSidebarButton(QToolButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hover_progress = 0.0
        self._theme_name = "dark"
        self._hover_anim: QPropertyAnimation | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def set_button_theme(self, theme: str) -> None:
        self._theme_name = theme
        self.update()

    def enterEvent(self, event: QEvent) -> None:
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def _animate_hover(self, target: float) -> None:
        if self._hover_anim is not None:
            self._hover_anim.stop()
        anim = QPropertyAnimation(self, b"hoverProgress", self)
        anim.setDuration(170)
        anim.setStartValue(self._hover_progress)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._hover_anim = anim
        anim.start()

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        opacity = 0.30 + 0.40 * self._hover_progress
        scale = 1.0 + 0.08 * self._hover_progress
        icon_size = int(22 * scale)
        pixmap = self.icon().pixmap(icon_size, icon_size)
        target = QRectF(
            (self.width() - icon_size) / 2.0,
            (self.height() - icon_size) / 2.0,
            icon_size,
            icon_size,
        )
        if not pixmap.isNull():
            tint = QColor("#1f2a3d" if is_light_theme(self._theme_name) else "#d7deea")
            pixmap.setDevicePixelRatio(1.0)
            tinted = QPixmap(pixmap.size())
            tinted.fill(Qt.GlobalColor.transparent)
            tinted.setDevicePixelRatio(1.0)
            tint_painter = QPainter(tinted)
            tint_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            tint_painter.drawPixmap(0, 0, pixmap)
            tint_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            tint_painter.fillRect(tinted.rect(), tint)
            tint_painter.end()
            pixmap = tinted
        painter.setOpacity(opacity)
        source = QRectF(0.0, 0.0, float(pixmap.width()), float(pixmap.height()))
        painter.drawPixmap(target, pixmap, source)

    def _get_hover_progress(self) -> float:
        return self._hover_progress

    def _set_hover_progress(self, value: float) -> None:
        self._hover_progress = max(0.0, min(1.0, float(value)))
        self.update()

    hoverProgress = Property(float, _get_hover_progress, _set_hover_progress)


class ModCardFrame(QFrame):
    clicked = Signal(str)

    def __init__(self, mod_id: str, editable: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mod_id = mod_id
        self._editable = editable
        if editable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        if self._editable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._mod_id)


class ServiceCardFrame(QFrame):
    toggled = Signal(str, bool)

    def __init__(self, preset: ServicePreset, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.preset = preset
        self._selected = False
        self._theme = "dark"
        self._icon_pixmap = QPixmap()
        self._check_pixmap = QPixmap()
        self._visual_scope = "main"
        self._burst_progress = 0.0
        self._press_progress = 0.0
        self._burst_anim: QPropertyAnimation | None = None
        self._press_anim: QPropertyAnimation | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(136)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(9)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)

        self._icon_badge = QFrame()
        self._icon_badge.setFixedSize(36, 36)
        self._icon_badge.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        badge_layout = QVBoxLayout(self._icon_badge)
        badge_layout.setContentsMargins(0, 0, 0, 0)
        badge_layout.setSpacing(0)
        self._icon_label = QLabel()
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge_layout.addWidget(self._icon_label)
        top.addWidget(self._icon_badge, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        top.addStretch(1)

        self._selected_label = QLabel()
        self._selected_label.setFixedSize(20, 20)
        self._selected_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self._selected_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        root.addLayout(top)

        self._title_label = QLabel()
        self._title_label.setWordWrap(True)
        self._title_label.setProperty("class", "title")
        self._title_label.setMaximumHeight(48)
        root.addWidget(self._title_label)

        self._desc_label = QLabel()
        self._desc_label.setWordWrap(True)
        self._desc_label.setProperty("class", "muted")
        self._desc_label.setMaximumHeight(62)
        root.addWidget(self._desc_label)

        root.addStretch(1)

    def set_card_width(self, width: int) -> None:
        self.setFixedWidth(max(132, width))

    def set_visual_scope(self, scope: str) -> None:
        self._visual_scope = "onboarding" if scope == "onboarding" else "main"
        self.setFixedHeight(156 if self._visual_scope == "onboarding" else 138)
        root = self.layout()
        if isinstance(root, QVBoxLayout):
            if self._visual_scope == "onboarding":
                root.setContentsMargins(17, 15, 17, 15)
                root.setSpacing(8)
            else:
                root.setContentsMargins(14, 12, 14, 12)
                root.setSpacing(9)
        self._sync_style()
        self.updateGeometry()
        self.update()

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self._sync_style()
        self.update()

    def set_icon_pixmap(self, pixmap: QPixmap) -> None:
        self._icon_pixmap = pixmap
        self._icon_label.setPixmap(self._compose_slot_pixmap(pixmap, self._icon_badge.size(), 1.0))

    def set_check_pixmap(self, pixmap: QPixmap) -> None:
        self._check_pixmap = pixmap
        if self._selected:
            self._selected_label.setPixmap(self._compose_slot_pixmap(pixmap, self._selected_label.size(), 0.56))

    def set_selected(self, selected: bool) -> None:
        if self._selected == bool(selected):
            return
        self._selected = bool(selected)
        self._sync_style()
        self.update()

    def _compose_slot_pixmap(self, pixmap: QPixmap, slot_size: QSize, fill_ratio: float) -> QPixmap:
        if pixmap.isNull() or not slot_size.isValid():
            return pixmap
        dpr = max(1.0, float(pixmap.devicePixelRatio()))
        logical_width = float(slot_size.width())
        logical_height = float(slot_size.height())
        physical_width = max(1, int(round(logical_width * dpr)))
        physical_height = max(1, int(round(logical_height * dpr)))
        canvas = QPixmap(physical_width, physical_height)
        canvas.fill(Qt.GlobalColor.transparent)
        canvas.setDevicePixelRatio(dpr)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if hasattr(QPainter.RenderHint, "LosslessImageRendering"):
            painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
        source_size = pixmap.deviceIndependentSize() if hasattr(pixmap, "deviceIndependentSize") else QSizeF(
            float(pixmap.width()) / max(1.0, float(pixmap.devicePixelRatio())),
            float(pixmap.height()) / max(1.0, float(pixmap.devicePixelRatio())),
        )
        target_width = float(source_size.width())
        target_height = float(source_size.height())
        max_box = min(logical_width, logical_height) * max(0.1, min(1.0, float(fill_ratio)))
        if target_width > 0.0 and target_height > 0.0:
            scale = min(max_box / target_width, max_box / target_height, 1.0)
            target_width *= scale
            target_height *= scale
        painter.drawPixmap(
            QRectF((logical_width - target_width) / 2.0, (logical_height - target_height) / 2.0, target_width, target_height),
            pixmap,
            QRectF(0, 0, pixmap.width(), pixmap.height()),
        )
        painter.end()
        return canvas

    def set_texts(self, title: str, description: str) -> None:
        self._title_label.setText(title)
        self._desc_label.setText(description)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._play_select_feedback()
            self.toggled.emit(self.preset.id, not self._selected)

    def paintEvent(self, event: QEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        shrink = 1.8 * self._press_progress
        glow_pad = 6.0 if self._visual_scope == "onboarding" else 0.0
        rect = QRectF(self.rect()).adjusted(0.5 + glow_pad + shrink, 0.5 + glow_pad + shrink, -0.5 - glow_pad - shrink, -0.5 - glow_pad - shrink)
        card_radius = 12.0
        accent = QColor(self.preset.accent)
        light = self._visual_scope == "onboarding" or is_light_theme(self._theme)
        base_fill = QColor("#ffffff") if light else QColor("#141922" if self._theme == "night" else "#171b20")
        if self._theme == "oled" and not self._visual_scope == "onboarding":
            base_fill = QColor("#111418")
        fill = QColor(base_fill)
        border = QColor("#d9e3f1" if light else "#252d38")
        if self._selected and self._visual_scope == "onboarding":
            fill = QColor(base_fill.lighter(102 if light else 106))
            border = QColor(accent)
            border.setAlpha(112 if light else 96)
        elif self._selected:
            fill = QColor(base_fill.lighter(102 if light else 106))
            border = QColor(accent)
            border.setAlpha(112 if light else 96)
        if self._selected and self._visual_scope == "onboarding":
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            glow_spread = 4.0
            outer_radius = card_radius + glow_spread * 1.35
            outer_glow_rect = rect.adjusted(-glow_spread, -glow_spread, glow_spread, glow_spread)
            glow = QRadialGradient(
                rect.center(),
                max(outer_glow_rect.width(), outer_glow_rect.height()) * 0.86,
            )
            glow_color = QColor(accent)
            glow_color.setAlpha(36 if light else 48)
            glow.setColorAt(0.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), max(12, glow_color.alpha() // 3)))
            glow.setColorAt(0.50, glow_color)
            glow.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0))
            painter.setBrush(glow)
            painter.drawRoundedRect(outer_glow_rect, outer_radius, outer_radius)
            painter.restore()
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, card_radius, card_radius)

        glow = QRadialGradient(rect.left() + 40, rect.top() + 30, max(rect.width(), rect.height()) * 0.72)
        glow_color = QColor(accent)
        glow_color.setAlpha(18 if self._selected else 0)
        glow.setColorAt(0.0, glow_color)
        glow.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawRoundedRect(rect, card_radius, card_radius)
        if self._burst_progress > 0.0:
            self._paint_burst(painter, accent)

    def _paint_burst(self, painter: QPainter, accent: QColor) -> None:
        progress = max(0.0, min(1.0, self._burst_progress))
        opacity = int(145 * (1.0 - progress))
        if opacity <= 0:
            return
        origin = QPointF(30.0, 28.0)
        try:
            icon_center = self._icon_badge.mapTo(self, self._icon_badge.rect().center())
            origin = QPointF(float(icon_center.x()), float(icon_center.y()))
        except Exception:
            pass
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(7):
            angle = (-140 + index * 46) * math.pi / 180.0
            distance = 8.0 + 28.0 * progress
            radius = 2.8 - 1.1 * progress + (0.35 if index % 2 else 0.0)
            color = QColor(accent)
            color.setAlpha(max(0, opacity - index * 6))
            point = QPointF(
                origin.x() + math.cos(angle) * distance,
                origin.y() + math.sin(angle) * distance,
            )
            painter.setBrush(color)
            painter.drawEllipse(point, max(1.1, radius), max(1.1, radius))
        painter.restore()

    def _sync_style(self) -> None:
        onboarding = self._visual_scope == "onboarding"
        accent = QColor(self.preset.accent)
        selected = bool(self._selected)
        text_color = "#142033" if (onboarding or is_light_theme(self._theme)) else ("#f2f6ff" if selected else "#d2d9e5")
        muted_color = "#5f6f86" if (onboarding or is_light_theme(self._theme)) else ("#c0ccdc" if selected else "#8d99aa")
        if self._selected:
            muted_color = "#334154" if (onboarding or is_light_theme(self._theme)) else "#d5def0"
        title_size = 15 if self._visual_scope == "onboarding" else 15
        desc_size = 13 if self._visual_scope == "onboarding" else 13
        self._title_label.setStyleSheet(f"color: {text_color}; background: transparent; font-size: {title_size}px; font-weight: 700;")
        self._desc_label.setStyleSheet(f"color: {muted_color}; background: transparent; font-size: {desc_size}px;")
        if onboarding or is_light_theme(self._theme):
            badge_fill = QColor(0, 0, 0, 0)
        else:
            badge_fill = QColor(0, 0, 0, 0)
        self._icon_badge.setStyleSheet(
            "QFrame {"
            f"background: {badge_fill.name(QColor.NameFormat.HexArgb)};"
            "border: none;"
            "border-radius: 0px;"
            "}"
        )
        if self._selected:
            self._selected_label.setText("")
            if not self._check_pixmap.isNull():
                self._selected_label.setPixmap(
                    self._compose_slot_pixmap(self._check_pixmap, self._selected_label.size(), 0.56)
                )
            self._selected_label.setStyleSheet(
                f"background: {accent.name(QColor.NameFormat.HexArgb)};"
                "border-radius: 10px;"
                "padding: 0px;"
                "margin: 0px;"
            )
        else:
            self._selected_label.setText("")
            self._selected_label.setPixmap(QPixmap())
            self._selected_label.setStyleSheet("background: transparent;")

    def _play_select_feedback(self) -> None:
        if self._press_anim is not None:
            self._press_anim.stop()
        press = QPropertyAnimation(self, b"pressProgress", self)
        press.setDuration(170)
        press.setStartValue(0.0)
        press.setKeyValueAt(0.45, 1.0)
        press.setEndValue(0.0)
        press.setEasingCurve(QEasingCurve.Type.OutCubic)
        press.start()
        self._press_anim = press

        if self._burst_anim is not None:
            self._burst_anim.stop()
        burst = QPropertyAnimation(self, b"burstProgress", self)
        burst.setDuration(420)
        burst.setStartValue(0.0)
        burst.setEndValue(1.0)
        burst.setEasingCurve(QEasingCurve.Type.OutCubic)
        burst.start()
        self._burst_anim = burst

    def _get_burst_progress(self) -> float:
        return self._burst_progress

    def _set_burst_progress(self, value: float) -> None:
        self._burst_progress = float(value)
        self.update()

    def _get_press_progress(self) -> float:
        return self._press_progress

    def _set_press_progress(self, value: float) -> None:
        self._press_progress = float(value)
        self.update()

    burstProgress = Property(float, _get_burst_progress, _set_burst_progress)
    pressProgress = Property(float, _get_press_progress, _set_press_progress)


class ServiceGridPanel(QWidget):
    def __init__(
        self,
        *,
        base_columns: int,
        min_card_width: int,
        offset_pattern: tuple[int, ...],
        horizontal_spacing: int = 14,
        vertical_spacing: int = 10,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._base_columns = max(1, base_columns)
        self._min_card_width = max(96, min_card_width)
        self._offset_pattern = offset_pattern or (0,)
        self._cards: list[ServiceCardFrame] = []
        self._wrappers: dict[ServiceCardFrame, QWidget] = {}
        self._last_columns = self._base_columns
        self._last_minimum_height = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(max(6, horizontal_spacing))
        self._grid.setVerticalSpacing(max(0, vertical_spacing))

    def set_cards(self, cards: list[ServiceCardFrame]) -> None:
        self._cards = list(cards)
        self._relayout_cards()

    def resizeEvent(self, event: QEvent) -> None:
        super().resizeEvent(event)
        self._relayout_cards()

    def minimumSizeHint(self) -> QSize:
        return QSize(self._min_card_width * min(self._base_columns, max(1, len(self._cards))), max(1, self._last_minimum_height))

    def sizeHint(self) -> QSize:
        columns = max(1, min(self._base_columns, max(1, len(self._cards))))
        width = columns * self._min_card_width + (columns - 1) * self._grid.horizontalSpacing()
        return QSize(width, max(1, self._last_minimum_height))

    def _relayout_cards(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        if not self._cards:
            return
        available = max(320, self.width() - self._grid.contentsMargins().left() - self._grid.contentsMargins().right())
        columns = max(1, min(self._base_columns, available // self._min_card_width))
        columns = min(columns, len(self._cards))
        if columns <= 0:
            columns = 1
        self._last_columns = columns
        cell_width = int((available - self._grid.horizontalSpacing() * max(0, columns - 1)) / columns)
        for column in range(max(self._base_columns, columns)):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, card in enumerate(self._cards):
            card.set_card_width(cell_width)
            wrapper = self._wrappers.get(card)
            if wrapper is None:
                wrapper = QWidget(self)
                wrapper.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
                wrapper.setStyleSheet("background: transparent; border: none;")
                layout = QVBoxLayout(wrapper)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(0)
                layout.addWidget(card, 0, Qt.AlignmentFlag.AlignTop)
                self._wrappers[card] = wrapper
            offset = self._offset_pattern[index % len(self._offset_pattern)]
            layout = wrapper.layout()
            if isinstance(layout, QVBoxLayout):
                layout.setContentsMargins(0, offset, 0, 0)
            row = index // columns
            col = index % columns
            self._grid.addWidget(wrapper, row, col, Qt.AlignmentFlag.AlignTop)
        rows = (len(self._cards) + columns - 1) // columns
        card_height = max(
            (max(card.height(), card.minimumHeight(), card.sizeHint().height()) for card in self._cards),
            default=136,
        )
        max_offset = max((max(0, int(value)) for value in self._offset_pattern), default=0)
        row_heights: list[int] = []
        for row in range(rows):
            row_offsets = [
                max(0, int(self._offset_pattern[index % len(self._offset_pattern)]))
                for index in range(row * columns, min(len(self._cards), (row + 1) * columns))
            ]
            row_heights.append(card_height + max(row_offsets, default=0))
        self._last_minimum_height = sum(row_heights) + max(0, rows - 1) * self._grid.verticalSpacing()
        self.setMinimumHeight(self._last_minimum_height)
        self.updateGeometry()
        self._grid.setRowStretch(rows, 1)

class AnimatedPowerButton(QToolButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._light_theme = False
        self._theme_name = "night"
        self._active = False
        self._visual_mode = "off"
        self._visual_scale = 1.0
        self._hover_progress = 0.0
        self._glow_pos = QPointF(66.0, 66.0)
        self._wave_progress = 0.0
        self._wave_strength = 0.0
        self._wave_outward = True
        self._scale_anim: QPropertyAnimation | None = None
        self._hover_anim: QPropertyAnimation | None = None
        self._wave_progress_anim: QPropertyAnimation | None = None
        self._wave_strength_anim: QPropertyAnimation | None = None

    def set_power_theme(self, theme: str) -> None:
        self._theme_name = theme
        self._light_theme = is_light_theme(theme)
        self.update()

    def set_active_state(self, active: bool, *, animate: bool = True) -> None:
        self._active = active
        self._visual_mode = "on" if active else "off"
        target = 1.14 if active else 1.0
        if self._scale_anim is not None:
            self._scale_anim.stop()
        if not animate:
            self._visual_scale = target
            self.update()
            return
        anim = QPropertyAnimation(self, b"visualScale", self)
        anim.setDuration(220)
        anim.setStartValue(self._visual_scale)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.start()
        self._scale_anim = anim

    def set_loading_state(self, loading: bool, *, animate: bool = True) -> None:
        self._visual_mode = "loading" if loading else ("on" if self._active else "off")
        target = 1.06 if loading else (1.14 if self._active else 1.0)
        if self._scale_anim is not None:
            self._scale_anim.stop()
        if not animate:
            self._visual_scale = target
            self.update()
            return
        anim = QPropertyAnimation(self, b"visualScale", self)
        anim.setDuration(190)
        anim.setStartValue(self._visual_scale)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.start()
        self._scale_anim = anim

    def enterEvent(self, event: QEvent) -> None:
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._glow_pos = event.position()
        self.update()
        super().mouseMoveEvent(event)

    def _animate_hover(self, target: float) -> None:
        if self._hover_anim is not None:
            self._hover_anim.stop()
        anim = QPropertyAnimation(self, b"hoverProgress", self)
        anim.setDuration(240)
        anim.setStartValue(self._hover_progress)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.start()
        self._hover_anim = anim

    def play_wave(self, outward: bool) -> None:
        self._wave_outward = outward
        if self._wave_progress_anim is not None:
            self._wave_progress_anim.stop()
        if self._wave_strength_anim is not None:
            self._wave_strength_anim.stop()
        self._wave_progress = 0.0
        self._wave_strength = 0.22
        prog = QPropertyAnimation(self, b"waveProgress", self)
        prog.setDuration(560)
        prog.setStartValue(0.0)
        prog.setEndValue(1.0)
        prog.setEasingCurve(QEasingCurve.Type.OutCubic)
        strength = QPropertyAnimation(self, b"waveStrength", self)
        strength.setDuration(560)
        strength.setStartValue(0.24)
        strength.setEndValue(0.0)
        strength.setEasingCurve(QEasingCurve.Type.OutCubic)
        prog.start()
        strength.start()
        self._wave_progress_anim = prog
        self._wave_strength_anim = strength

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRectF(self.rect())
        center = rect.center()
        base_radius = min(rect.width(), rect.height()) * 0.39
        radius = base_radius * self._visual_scale

        if self._light_theme:
            off_top = QColor("#f7f9ff")
            off_bottom = QColor("#dfe8f7")
            off_border = QColor("#bfd2f0")
            on_top = QColor("#7b86ff")
            on_bottom = QColor("#4c58d8")
            on_border = QColor("#7b87ff")
            loading_top = QColor("#c7d3e6")
            loading_bottom = QColor("#9ba8bd")
            loading_border = QColor("#b9c6db")
        else:
            off_top = QColor("#5a5f67")
            off_bottom = QColor("#3c4148")
            off_border = QColor("#70757d")
            on_top = QColor("#7380ff")
            on_bottom = QColor("#4551cb")
            on_border = QColor("#7b87ff")
            loading_top = QColor("#707785")
            loading_bottom = QColor("#565d69")
            loading_border = QColor("#8b94a3")
            if self._theme_name == "night":
                off_top = QColor("#45506a")
                off_bottom = QColor("#313a4d")
                off_border = QColor("#56627d")
            elif self._theme_name == "oled":
                off_top = QColor("#2a2d33")
                off_bottom = QColor("#181b20")
                off_border = QColor("#3d424b")
                loading_top = QColor("#4f535b")
                loading_bottom = QColor("#353941")
                loading_border = QColor("#5b626d")

        gradient = QRadialGradient(center.x(), center.y() - radius * 0.36, radius * 1.3)
        if self._visual_mode == "loading":
            gradient.setColorAt(0.0, loading_top)
            gradient.setColorAt(1.0, loading_bottom)
            border = loading_border
        elif self._active:
            gradient.setColorAt(0.0, on_top)
            gradient.setColorAt(1.0, on_bottom)
            border = on_border
        else:
            gradient.setColorAt(0.0, off_top)
            gradient.setColorAt(1.0, off_bottom)
            border = off_border
        painter.setPen(QPen(border, 2))
        painter.setBrush(gradient)
        painter.drawEllipse(center, radius, radius)

        if self._hover_progress > 0.001:
            if self._light_theme:
                if self._active or self._visual_mode == "loading":
                    glow_color = QColor(232, 243, 255, int(62 * self._hover_progress))
                else:
                    glow_color = QColor(109, 154, 255, int(34 * self._hover_progress))
            else:
                glow_color = QColor(148, 206, 255, int(34 * self._hover_progress))
            dx = self._glow_pos.x() - center.x()
            dy = self._glow_pos.y() - center.y()
            distance = max(1.0, (dx * dx + dy * dy) ** 0.5)
            max_offset = radius * 0.34
            focus = QPointF(
                center.x() + dx / distance * min(distance, max_offset),
                center.y() + dy / distance * min(distance, max_offset),
            )
            button_path = QPainterPath()
            button_path.addEllipse(center, radius, radius)
            painter.save()
            painter.setClipPath(button_path)
            glow = QRadialGradient(focus, radius * (1.08 if self._light_theme else 0.98))
            glow.setColorAt(0.0, glow_color)
            glow.setColorAt(0.65, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), max(0, glow_color.alpha() // 2)))
            glow.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(center, radius, radius)
            painter.restore()

        icon_size = 48 if self._active else 44
        if self._visual_mode == "loading":
            icon_size = 46
        pixmap = self.icon().pixmap(icon_size, icon_size)
        target = QRectF(center.x() - icon_size / 2.0, center.y() - icon_size / 2.0, icon_size, icon_size)
        painter.drawPixmap(target, pixmap, QRectF(0, 0, pixmap.width(), pixmap.height()))

    def _get_visual_scale(self) -> float:
        return self._visual_scale

    def _set_visual_scale(self, value: float) -> None:
        self._visual_scale = float(value)
        self.update()

    def _get_wave_progress(self) -> float:
        return self._wave_progress

    def _set_wave_progress(self, value: float) -> None:
        self._wave_progress = float(value)
        self.update()

    def _get_wave_strength(self) -> float:
        return self._wave_strength

    def _set_wave_strength(self, value: float) -> None:
        self._wave_strength = float(value)
        self.update()

    def _get_hover_progress(self) -> float:
        return self._hover_progress

    def _set_hover_progress(self, value: float) -> None:
        self._hover_progress = float(value)
        self.update()

    visualScale = Property(float, _get_visual_scale, _set_visual_scale)
    waveProgress = Property(float, _get_wave_progress, _set_wave_progress)
    waveStrength = Property(float, _get_wave_strength, _set_wave_strength)
    hoverProgress = Property(float, _get_hover_progress, _set_hover_progress)


class PowerAuraWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._light_theme = False
        self._theme_name = "night"
        self._wave_progress = 0.0
        self._wave_strength = 0.0
        self._wave_outward = True
        self._center_point = QPointF()
        self._wave_base_radius = 74.0
        self._wave_travel_radius = 124.0
        self._idle_enabled = False
        self._status_glow_enabled = False
        self._status_glow_breath = 0.0
        self._status_glow_phase = 0.0
        self._status_glow_presence = 0.0
        self._idle_pulse_timer = QTimer(self)
        self._idle_pulse_timer.setInterval(1480)
        self._idle_pulse_timer.timeout.connect(self._play_idle_pulse)
        self._status_glow_timer = QTimer(self)
        self._status_glow_timer.setInterval(42)
        self._status_glow_timer.timeout.connect(self._advance_status_glow_breath)
        self._wave_progress_anim: QPropertyAnimation | None = None
        self._wave_strength_anim: QPropertyAnimation | None = None
        self._status_glow_presence_anim: QPropertyAnimation | None = None

    def set_power_theme(self, theme: str) -> None:
        self._theme_name = theme
        self._light_theme = is_light_theme(theme)
        self.update()

    def set_center_point(self, point: QPointF) -> None:
        self._center_point = QPointF(point)
        self.update()

    def set_idle_pulse_enabled(self, enabled: bool) -> None:
        self._idle_enabled = enabled
        if enabled:
            if not self._idle_pulse_timer.isActive():
                self._idle_pulse_timer.start()
            if self._wave_strength <= 0.02:
                self._play_idle_pulse()
        else:
            self._idle_pulse_timer.stop()

    def set_status_glow_enabled(self, enabled: bool) -> None:
        if self._status_glow_enabled == bool(enabled):
            return
        self._status_glow_enabled = bool(enabled)
        if self._status_glow_enabled:
            if not self._status_glow_timer.isActive():
                self._status_glow_timer.start()
        else:
            if not self._status_glow_timer.isActive():
                self._status_glow_timer.start()
        if self._status_glow_presence_anim is not None:
            self._status_glow_presence_anim.stop()
        anim = QPropertyAnimation(self, b"statusGlowPresence", self)
        anim.setDuration(360 if enabled else 520)
        anim.setStartValue(self._status_glow_presence)
        anim.setEndValue(1.0 if enabled else 0.0)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        if not enabled:
            def _finish_off() -> None:
                if not self._status_glow_enabled:
                    self._status_glow_timer.stop()
                    self._status_glow_breath = 0.0
            anim.finished.connect(_finish_off)
        self._status_glow_presence_anim = anim
        anim.start()
        self.update()

    def _advance_status_glow_breath(self) -> None:
        if not self._status_glow_enabled:
            return
        # Irregular "campfire" breathing: slow overall motion with small phase drift.
        wobble = 0.5 + 0.5 * math.sin(self._status_glow_phase * 0.71 + 0.8)
        self._status_glow_phase = (self._status_glow_phase + 0.026 + 0.018 * wobble) % (math.pi * 2.0)
        wave = math.sin(self._status_glow_phase + 0.28 * math.sin(self._status_glow_phase * 1.9))
        self._status_glow_breath = 0.5 + 0.5 * wave
        self.update()

    def _play_idle_pulse(self) -> None:
        if not self._idle_enabled or self._wave_strength > 0.08:
            return
        self._play_wave_internal(outward=True, strength=0.30, duration=1450, base_radius=62.0, travel_radius=62.0)

    def _play_wave_internal(self, *, outward: bool, strength: float, duration: int, base_radius: float, travel_radius: float) -> None:
        self._wave_outward = outward
        if self._wave_progress_anim is not None:
            self._wave_progress_anim.stop()
        if self._wave_strength_anim is not None:
            self._wave_strength_anim.stop()
        self._wave_progress = 0.0
        self._wave_strength = strength
        self._wave_base_radius = base_radius
        self._wave_travel_radius = travel_radius
        prog = QPropertyAnimation(self, b"waveProgress", self)
        prog.setDuration(duration)
        prog.setStartValue(0.0)
        prog.setEndValue(1.0)
        prog.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade = QPropertyAnimation(self, b"waveStrength", self)
        fade.setDuration(duration)
        fade.setStartValue(strength)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        prog.start()
        fade.start()
        self._wave_progress_anim = prog
        self._wave_strength_anim = fade

    def play_wave(self, outward: bool) -> None:
        self._play_wave_internal(outward=outward, strength=0.48, duration=820, base_radius=74.0, travel_radius=118.0)

    def paintEvent(self, event: QEvent) -> None:
        if self._wave_strength <= 0.001 and self._status_glow_presence <= 0.001:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()).adjusted(2.0, 2.0, -2.0, -2.0), 18.0, 18.0)
        painter.setClipPath(clip)
        center = self._center_point if not self._center_point.isNull() else QRectF(self.rect()).center()
        if self.width() <= 0 or self.height() <= 0 or center.x() <= 1.0 or center.y() <= 1.0:
            return
        if self._status_glow_presence > 0.001:
            if self._theme_name == "oled":
                aura_color = QColor(104, 118, 210, 70)
            elif self._light_theme:
                aura_color = QColor(19, 58, 142, 66)
            else:
                aura_color = QColor(100, 172, 255, 74)
            breath = 0.36 + 0.64 * self._status_glow_breath
            presence = max(0.0, min(1.0, self._status_glow_presence))
            radius = 98.0 + 48.0 * breath
            aura_color.setAlpha(int(aura_color.alpha() * presence * (0.44 + 0.56 * breath)))
            aura = QRadialGradient(center, radius)
            aura.setColorAt(0.0, aura_color)
            aura.setColorAt(0.42, QColor(aura_color.red(), aura_color.green(), aura_color.blue(), max(12, aura_color.alpha() // 2)))
            aura.setColorAt(1.0, QColor(aura_color.red(), aura_color.green(), aura_color.blue(), 0))
            painter.setBrush(aura)
            painter.drawEllipse(center, radius, radius)
        if self._theme_name == "oled":
            color = QColor(124, 134, 182, int(132 * self._wave_strength))
        elif self._light_theme:
            color = QColor(64, 116, 255, int(176 * self._wave_strength))
        else:
            color = QColor(122, 214, 255, int(168 * self._wave_strength))
        base = self._wave_base_radius
        travel = self._wave_travel_radius * (self._wave_progress if self._wave_outward else (1.0 - self._wave_progress))
        for factor, width, alpha_factor in ((1.0, 14.0, 1.0), (0.8, 9.0, 0.78), (0.62, 5.5, 0.52)):
            radius = base * factor + travel
            ring = QColor(color)
            ring.setAlpha(int(color.alpha() * alpha_factor))
            pen = QPen(ring, max(1.4, width * self._wave_strength))
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius, radius)

    def _get_wave_progress(self) -> float:
        return self._wave_progress

    def _set_wave_progress(self, value: float) -> None:
        self._wave_progress = float(value)
        self.update()

    def _get_wave_strength(self) -> float:
        return self._wave_strength

    def _set_wave_strength(self, value: float) -> None:
        self._wave_strength = float(value)
        self.update()

    waveProgress = Property(float, _get_wave_progress, _set_wave_progress)
    waveStrength = Property(float, _get_wave_strength, _set_wave_strength)

    def _get_status_glow_presence(self) -> float:
        return self._status_glow_presence

    def _set_status_glow_presence(self, value: float) -> None:
        self._status_glow_presence = max(0.0, min(1.0, float(value)))
        self.update()

    statusGlowPresence = Property(float, _get_status_glow_presence, _set_status_glow_presence)


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, margin: int = 0, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self.spacing()
            if line_height > 0 and next_x - self.spacing() > effective.right() + 1:
                x = effective.x()
                y += line_height + self.spacing()
                next_x = x + hint.width() + self.spacing()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class ClickableCard(QFrame):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("class", "fileModeCard")
        self.setProperty("hovered", False)
        self._hover_progress = 0.0
        self._hover_anim: QPropertyAnimation | None = None

    def enterEvent(self, event: QEvent) -> None:
        self.setProperty("hovered", True)
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self.setProperty("hovered", False)
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _animate_hover(self, target: float) -> None:
        if self._hover_anim is not None:
            self._hover_anim.stop()
        anim = QPropertyAnimation(self, b"hoverProgress", self)
        anim.setDuration(170)
        anim.setStartValue(self._hover_progress)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        anim.start()
        self._hover_anim = anim

    def paintEvent(self, event: QEvent) -> None:
        super().paintEvent(event)
        if self._hover_progress <= 0.001:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        light = self.palette().window().color().lightnessF() > 0.72
        if light:
            fill = QColor(142, 169, 223, int(26 * self._hover_progress))
            border = QColor(111, 145, 210, int(74 * self._hover_progress))
        else:
            fill = QColor(92, 122, 183, int(24 * self._hover_progress))
            border = QColor(109, 145, 221, int(82 * self._hover_progress))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 14.0, 14.0)

    def _get_hover_progress(self) -> float:
        return self._hover_progress

    def _set_hover_progress(self, value: float) -> None:
        self._hover_progress = float(value)
        self.update()

    hoverProgress = Property(float, _get_hover_progress, _set_hover_progress)


class ExpandableDescriptionLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = text
        self._expanded = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("class", "modBody")
        self._sync_text()

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self._sync_text()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._expanded = not self._expanded
            self._sync_text()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event: QEvent) -> None:
        super().resizeEvent(event)
        if not self._expanded:
            self._sync_text()

    def _sync_text(self) -> None:
        if self._expanded:
            self.setWordWrap(True)
            self.setText(self._full_text)
            self.setToolTip(self._full_text)
            return
        self.setWordWrap(False)
        available = max(100, self.width() - 6)
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, available)
        self.setText(elided)
        self.setToolTip(self._full_text if elided != self._full_text else "")


class ButtonInteractionOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._progress = 0.0
        self._pressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.hide()

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self.setVisible(self._progress > 0.001)
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def set_pressed(self, pressed: bool) -> None:
        self._pressed = bool(pressed)
        self.update()

    def paintEvent(self, event: QEvent) -> None:
        if self._progress <= 0.001:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        base = self.parentWidget().palette().button().color() if self.parentWidget() is not None else QColor('#1f2430')
        lightness = base.lightness()
        if lightness < 128:
            overlay = QColor(255, 255, 255)
            max_alpha = 28 if not self._pressed else 42
        else:
            overlay = QColor(31, 41, 55)
            max_alpha = 14 if not self._pressed else 22
        overlay.setAlpha(int(max_alpha * self._progress))
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = self._resolve_radius(rect)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(overlay)
        painter.drawRoundedRect(rect, radius, radius)

    def _resolve_radius(self, rect: QRectF) -> float:
        parent = self.parentWidget()
        if parent is None:
            return min(18.0, max(8.0, min(rect.width(), rect.height()) / 2.0))
        explicit = parent.property("hoverRadius")
        if explicit is not None:
            try:
                return float(explicit)
            except Exception:
                pass
        if isinstance(parent, QPushButton):
            return 10.0
        button_class = str(parent.property("class") or "")
        if button_class in {"nav", "window", "action"}:
            return 12.0
        return min(18.0, max(8.0, min(rect.width(), rect.height()) / 2.0))


class ButtonInteractionFilter(QObject):
    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        self._widget = widget
        self._overlay = ButtonInteractionOverlay(widget)
        self._overlay.setGeometry(widget.rect())
        self._animation: QPropertyAnimation | None = None
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._widget:
            if event.type() in {QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Move}:
                self._overlay.setGeometry(self._widget.rect())
                self._overlay.raise_()
            elif event.type() == QEvent.Type.Enter:
                self._overlay.raise_()
                self._overlay.set_pressed(False)
                self._animate_to(1.0, 180)
            elif event.type() == QEvent.Type.Leave:
                self._overlay.set_pressed(False)
                self._animate_to(0.0, 180)
            elif event.type() == QEvent.Type.MouseButtonPress:
                self._overlay.raise_()
                self._overlay.set_pressed(True)
                self._animate_to(1.0, 90)
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._overlay.set_pressed(False)
                target = 1.0 if self._widget.underMouse() else 0.0
                self._animate_to(target, 150)
        return super().eventFilter(watched, event)

    def _animate_to(self, value: float, duration: int) -> None:
        if self._animation is not None:
            self._animation.stop()
        animation = QPropertyAnimation(self._overlay, b"progress", self)
        animation.setDuration(duration)
        animation.setStartValue(self._overlay.progress)
        animation.setEndValue(value)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.start()
        self._animation = animation


class ScrollFadeOverlay(QWidget):
    def __init__(self, scrollable: QAbstractScrollArea) -> None:
        super().__init__(scrollable.viewport())
        self._scrollable = scrollable
        self._theme_name = "night"
        self._surface_override: QColor | None = None
        self._onboarding_background_frame: QWidget | None = None
        self._connected_onboarding_frame: QWidget | None = None
        self._top_visible = False
        self._bottom_visible = False
        self._fade_height = 18
        self._paint_top = True
        self._paint_bottom = True
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()
        scrollable.viewport().installEventFilter(self)
        scrollable.verticalScrollBar().valueChanged.connect(self._sync_state)
        scrollable.verticalScrollBar().rangeChanged.connect(lambda *_: self._sync_state())
        self._sync_geometry()
        self._sync_state()

    def set_theme(self, theme: str) -> None:
        self._theme_name = theme
        self.update()

    def set_surface_color(self, color: QColor | None) -> None:
        self._surface_override = QColor(color) if isinstance(color, QColor) else None
        self.update()

    def set_onboarding_background_frame(self, frame: QWidget | None) -> None:
        if self._connected_onboarding_frame is not None:
            signal = getattr(self._connected_onboarding_frame, "glowChanged", None)
            if signal is not None:
                try:
                    signal.disconnect(self.update)
                except Exception:
                    pass
        self._connected_onboarding_frame = frame
        self._onboarding_background_frame = frame
        signal = getattr(frame, "glowChanged", None) if frame is not None else None
        if signal is not None:
            try:
                signal.connect(self.update)
            except Exception:
                pass
        self.update()

    def set_fade_height(self, height: int) -> None:
        self._fade_height = max(10, int(height))
        self.update()

    def set_edges(self, *, top: bool = True, bottom: bool = True) -> None:
        self._paint_top = bool(top)
        self._paint_bottom = bool(bottom)
        self._sync_state()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._scrollable.viewport() and event.type() in {QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Paint}:
            self._sync_geometry()
            if event.type() != QEvent.Type.Paint:
                QTimer.singleShot(0, self._sync_state)
        return super().eventFilter(watched, event)

    def _surface_color(self) -> QColor:
        if self._surface_override is not None:
            return QColor(self._surface_override)
        if self._theme_name == "light":
            return QColor("#f4f7fc")
        if self._theme_name == "light blue":
            return QColor("#e4f0ff")
        if self._theme_name == "oled":
            return QColor("#101215")
        if self._theme_name == "dark":
            return QColor("#15171a")
        if is_light_theme(self._theme_name):
            return QColor("#f4f7fc")
        return QColor("#0d1320")

    def _sync_geometry(self) -> None:
        viewport = self._scrollable.viewport()
        self.setGeometry(viewport.rect())
        self.raise_()

    def _sync_state(self) -> None:
        bar = self._scrollable.verticalScrollBar()
        maximum = max(0, int(bar.maximum()))
        value = max(0, int(bar.value()))
        self._top_visible = value > 0
        self._bottom_visible = maximum > 0 and value < maximum
        visible = (self._top_visible and self._paint_top) or (self._bottom_visible and self._paint_bottom)
        self.setVisible(visible)
        if visible:
            self.raise_()
            self.update()

    def paintEvent(self, event: QEvent) -> None:
        if not (self._top_visible or self._bottom_visible):
            return
        if self._onboarding_background_frame is not None and self._paint_onboarding_background_fade():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = self._surface_color()
        width = self.width()
        fade_height = min(self._fade_height, max(10, self.height() // 5))
        if self._top_visible and self._paint_top:
            top = QLinearGradient(0, 0, 0, fade_height)
            top.setColorAt(0.0, color)
            top.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
            painter.fillRect(QRectF(0, -1, width, fade_height + 2), top)
        if self._bottom_visible and self._paint_bottom:
            bottom = QLinearGradient(0, self.height() - fade_height, 0, self.height())
            bottom.setColorAt(0.0, QColor(color.red(), color.green(), color.blue(), 0))
            bottom.setColorAt(1.0, color)
            painter.fillRect(QRectF(0, self.height() - fade_height - 1, width, fade_height + 2), bottom)

    def _paint_onboarding_background_fade(self) -> bool:
        frame = self._onboarding_background_frame
        if frame is None or self.width() <= 0 or self.height() <= 0:
            return False
        background = getattr(frame, "_background_color", None)
        if not isinstance(background, QColor) or background.alpha() <= 0:
            return False

        pixmap = QPixmap(self.size())
        pixmap.fill(Qt.GlobalColor.transparent)
        top_left = self.mapTo(frame, QPoint(0, 0))
        frame_rect = QRectF(frame.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(-top_left)
        painter.fillRect(frame_rect, background)
        light = background.lightnessF() > 0.6
        glow_x = float(getattr(frame, "_glow_x", 0.5) or 0.5)
        glow_y = float(getattr(frame, "_glow_y", 1.18) or 1.18)
        glow = QRadialGradient(
            frame_rect.left() + frame_rect.width() * glow_x,
            frame_rect.top() + frame_rect.height() * glow_y,
            max(frame_rect.width() * 0.96, frame_rect.height() * 1.2),
        )
        if light:
            center = QColor(92, 140, 255, 60)
            middle = QColor(92, 140, 255, 35)
            far = QColor(92, 140, 255, 12)
            edge = QColor(93, 139, 255, 0)
        else:
            center = QColor(76, 128, 235, 48)
            middle = QColor(76, 128, 235, 25)
            far = QColor(76, 128, 235, 7)
            edge = QColor(88, 146, 255, 0)
        glow.setColorAt(0.0, center)
        glow.setColorAt(0.24, QColor(center.red(), center.green(), center.blue(), max(0, center.alpha() - 5)))
        glow.setColorAt(0.48, middle)
        glow.setColorAt(0.72, far)
        glow.setColorAt(1.0, edge)
        painter.fillRect(frame_rect, glow)
        side = QLinearGradient(frame_rect.left(), 0, frame_rect.right(), 0)
        side_color = QColor(70, 118, 210, 10 if not light else 8)
        side.setColorAt(0.0, side_color)
        side.setColorAt(0.34, QColor(side_color.red(), side_color.green(), side_color.blue(), 0))
        side.setColorAt(0.66, QColor(side_color.red(), side_color.green(), side_color.blue(), 0))
        side.setColorAt(1.0, side_color)
        painter.fillRect(frame_rect, side)
        painter.end()

        mask = QPixmap(self.size())
        mask.fill(Qt.GlobalColor.transparent)
        fade_height = min(self._fade_height, max(10, self.height() // 3))
        mask_painter = QPainter(mask)
        if self._top_visible and self._paint_top:
            top = QLinearGradient(0, 0, 0, fade_height)
            top.setColorAt(0.0, QColor(255, 255, 255, 255))
            top.setColorAt(1.0, QColor(255, 255, 255, 0))
            mask_painter.fillRect(QRectF(0, 0, self.width(), fade_height), top)
        if self._bottom_visible and self._paint_bottom:
            bottom = QLinearGradient(0, self.height() - fade_height, 0, self.height())
            bottom.setColorAt(0.0, QColor(255, 255, 255, 0))
            bottom.setColorAt(1.0, QColor(255, 255, 255, 255))
            mask_painter.fillRect(QRectF(0, self.height() - fade_height, self.width(), fade_height), bottom)
        mask_painter.end()

        clip = QPainter(pixmap)
        clip.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        clip.drawPixmap(0, 0, mask)
        clip.end()

        out = QPainter(self)
        out.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        out.drawPixmap(0, 0, pixmap)
        return True


def _content_surface_color(theme: str) -> QColor:
    if theme == "light":
        return QColor("#f4f7fc")
    if theme == "light blue":
        return QColor("#e4f0ff")
    if theme == "oled":
        return QColor("#101215")
    if theme == "dark":
        return QColor("#15171a")
    if is_light_theme(theme):
        return QColor("#f4f7fc")
    return QColor("#0d1320")


def _files_inner_surface_color(theme: str) -> QColor:
    if theme == "light":
        return QColor("#ffffff")
    if theme == "light blue":
        return QColor("#f7fbff")
    if theme == "oled":
        return QColor("#13161a")
    if theme == "night":
        return QColor("#19263f")
    if theme == "dark":
        return QColor("#1a1c20")
    if is_light_theme(theme):
        return QColor("#ffffff")
    return QColor("#1a1c20")


def _files_inner_surface_css(theme: str) -> str:
    return _files_inner_surface_color(theme).name()


def _dialog_surface_color(theme: str) -> QColor:
    if theme == "light blue":
        return QColor("#ffffff")
    if theme == "light":
        return QColor("#ffffff")
    if theme == "oled":
        return QColor("#101215")
    if theme == "dark":
        return QColor("#181b1f")
    if is_light_theme(theme):
        return QColor("#ffffff")
    return QColor("#151f33")


def _chrome_surface_color(theme: str) -> QColor:
    if theme == "dark":
        return QColor("#181a1d")
    if theme == "oled":
        return QColor("#0f1012")
    if theme == "light blue":
        return QColor("#eef4ff")
    if is_light_theme(theme):
        return QColor("#f3f6fd")
    return QColor("#101726")


def _load_ui_font_family(ui_assets_dir: Path) -> str:
    font_path = ui_assets_dir / "fonts" / "JetBrainsSans[wght]-VF.ttf"
    family = "JetBrains Sans"
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if families:
            family = str(families[0])
    return family


def _onboarding_text_color(theme: str) -> str:
    return "#16202f" if is_light_theme(theme) else "#f6f8fc"


def _onboarding_muted_color(theme: str) -> str:
    return "#4b5d78" if is_light_theme(theme) else "#9db2d8"


def _theme_badge_name(theme_id: str, language: str = "en") -> str:
    td = _get_theme(theme_id)
    if td is not None:
        return td.name.get(language, td.name.get("en", theme_id))
    return theme_id.title()


def _language_display_name(language: str, ui_language: str = "en") -> str:
    if language == "ru":
        return "Русский" if ui_language == "ru" else "Russian"
    if language == "en":
        return "English"
    return language


def _render_widget_snapshot(widget: QWidget) -> QPixmap:
    size = widget.size()
    if size.isEmpty():
        return QPixmap()
    return widget.grab()


class PageTransitionOverlay(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._background_color = QColor(0, 0, 0, 0)
        self._old_pixmap = QPixmap()
        self._new_pixmap = QPixmap()
        self._old_opacity = 0.0
        self._new_opacity = 0.0
        self._content_rect = QRect()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAutoFillBackground(False)
        self.hide()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def set_background_color(self, color: QColor) -> None:
        self._background_color = QColor(color)
        self.update()

    def set_old_pixmap(self, pixmap: QPixmap | None) -> None:
        self._old_pixmap = QPixmap() if pixmap is None else QPixmap(pixmap)
        self.update()

    def set_new_pixmap(self, pixmap: QPixmap | None) -> None:
        self._new_pixmap = QPixmap() if pixmap is None else QPixmap(pixmap)
        self.update()

    def clear_transition(self) -> None:
        self._old_pixmap = QPixmap()
        self._new_pixmap = QPixmap()
        self._old_opacity = 0.0
        self._new_opacity = 0.0
        self._content_rect = QRect()
        self.update()

    def set_content_rect(self, rect: QRect) -> None:
        self._content_rect = QRect(rect)
        self.update()

    def _get_old_opacity(self) -> float:
        return self._old_opacity

    def _set_old_opacity(self, value: float) -> None:
        self._old_opacity = float(value)
        self.update()

    def _get_new_opacity(self) -> float:
        return self._new_opacity

    def _set_new_opacity(self, value: float) -> None:
        self._new_opacity = float(value)
        self.update()

    oldOpacity = Property(float, _get_old_opacity, _set_old_opacity)
    newOpacity = Property(float, _get_new_opacity, _set_new_opacity)

    def paintEvent(self, event: QEvent) -> None:
        if self._old_opacity <= 0.0 and self._new_opacity <= 0.0 and self._background_color.alpha() == 0:
            return
        painter = QPainter(self)
        target_rect = self._content_rect if not self._content_rect.isNull() else self.rect()
        if self._background_color.alpha() > 0:
            painter.fillRect(target_rect, self._background_color)
        if not self._old_pixmap.isNull() and self._old_opacity > 0.0:
            painter.save()
            painter.setOpacity(self._old_opacity)
            painter.drawPixmap(target_rect.topLeft(), self._old_pixmap)
            painter.restore()
        if not self._new_pixmap.isNull() and self._new_opacity > 0.0:
            painter.save()
            painter.setOpacity(self._new_opacity)
            painter.drawPixmap(target_rect.topLeft(), self._new_pixmap)
            painter.restore()


class OnboardingPageWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._background_color = QColor(0, 0, 0, 0)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def set_background_color(self, color: QColor) -> None:
        self._background_color = QColor(color)
        self.update()

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.0, 0.0, -1.0, -1.0)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        if self._background_color.alpha() <= 0:
            return
        radius = 16.0
        path = QPainterPath()
        path.moveTo(rect.left(), rect.top())
        path.lineTo(rect.right(), rect.top())
        path.lineTo(rect.right(), rect.bottom() - radius)
        path.quadTo(rect.right(), rect.bottom(), rect.right() - radius, rect.bottom())
        path.lineTo(rect.left() + radius, rect.bottom())
        path.quadTo(rect.left(), rect.bottom(), rect.left(), rect.bottom() - radius)
        path.lineTo(rect.left(), rect.top())
        path.closeSubpath()
        painter.fillPath(path, self._background_color)
        painter.save()
        painter.setClipPath(path)
        light = self._background_color.lightnessF() > 0.6
        glow = QRadialGradient(
            rect.center().x(),
            rect.bottom() + rect.height() * 0.18,
            max(rect.width() * 0.96, rect.height() * 1.2),
        )
        if light:
            center = QColor(92, 140, 255, 60)
            middle = QColor(92, 140, 255, 35)
            far = QColor(92, 140, 255, 12)
            edge = QColor(93, 139, 255, 0)
        else:
            center = QColor(76, 128, 235, 48)
            middle = QColor(76, 128, 235, 25)
            far = QColor(76, 128, 235, 7)
            edge = QColor(88, 146, 255, 0)
        glow.setColorAt(0.0, center)
        glow.setColorAt(0.24, QColor(center.red(), center.green(), center.blue(), max(0, center.alpha() - 5)))
        glow.setColorAt(0.48, middle)
        glow.setColorAt(0.72, far)
        glow.setColorAt(1.0, edge)
        painter.fillRect(rect, glow)
        side = QLinearGradient(rect.left(), 0, rect.right(), 0)
        side_color = QColor(70, 118, 210, 10 if not light else 8)
        side.setColorAt(0.0, side_color)
        side.setColorAt(0.34, QColor(side_color.red(), side_color.green(), side_color.blue(), 0))
        side.setColorAt(0.66, QColor(side_color.red(), side_color.green(), side_color.blue(), 0))
        side.setColorAt(1.0, side_color)
        painter.fillRect(rect, side)
        painter.restore()


class OnboardingFrame(QFrame):
    glowChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._background_color = QColor(0, 0, 0, 0)
        self._onboarding_active = False
        self._glow_x = 0.5
        self._glow_y = 1.18
        self._glow_target = (self._glow_x, self._glow_y)
        self._glow_animation: QParallelAnimationGroup | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def set_onboarding_background(self, color: QColor, active: bool) -> None:
        self._background_color = QColor(color)
        self._onboarding_active = bool(active)
        self.update()

    def set_glow_position(self, x: float, y: float, *, animated: bool = True) -> None:
        x = max(-0.35, min(1.35, float(x)))
        y = max(-0.35, min(1.45, float(y)))
        if abs(self._glow_target[0] - x) < 0.001 and abs(self._glow_target[1] - y) < 0.001:
            return
        self._glow_target = (x, y)
        if self._glow_animation is not None:
            self._glow_animation.stop()
            self._glow_animation.deleteLater()
            self._glow_animation = None
        if not animated:
            self._glow_x = x
            self._glow_y = y
            self.update()
            self.glowChanged.emit()
            return
        group = QParallelAnimationGroup(self)
        for prop, start, end in ((b"glowX", self._glow_x, x), (b"glowY", self._glow_y, y)):
            anim = QPropertyAnimation(self, prop, group)
            anim.setDuration(720)
            anim.setStartValue(start)
            anim.setEndValue(end)
            anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            group.addAnimation(anim)
        group.finished.connect(lambda: setattr(self, "_glow_animation", None))
        self._glow_animation = group
        group.start()

    def _get_glow_x(self) -> float:
        return self._glow_x

    def _set_glow_x(self, value: float) -> None:
        self._glow_x = float(value)
        self.update()
        self.glowChanged.emit()

    def _get_glow_y(self) -> float:
        return self._glow_y

    def _set_glow_y(self, value: float) -> None:
        self._glow_y = float(value)
        self.update()
        self.glowChanged.emit()

    glowX = Property(float, _get_glow_x, _set_glow_x)
    glowY = Property(float, _get_glow_y, _set_glow_y)

    def paintEvent(self, event: QEvent) -> None:
        if not self._onboarding_active:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        path = QPainterPath()
        path.addRoundedRect(rect, 16, 16)
        painter.fillPath(path, self._background_color)
        painter.save()
        painter.setClipPath(path)
        light = self._background_color.lightnessF() > 0.6
        glow = QRadialGradient(
            rect.left() + rect.width() * self._glow_x,
            rect.top() + rect.height() * self._glow_y,
            max(rect.width() * 0.96, rect.height() * 1.2),
        )
        if light:
            center = QColor(92, 140, 255, 60)
            middle = QColor(92, 140, 255, 35)
            far = QColor(92, 140, 255, 12)
        else:
            center = QColor(76, 128, 235, 48)
            middle = QColor(76, 128, 235, 25)
            far = QColor(76, 128, 235, 7)
        glow.setColorAt(0.0, center)
        glow.setColorAt(0.24, QColor(center.red(), center.green(), center.blue(), max(0, center.alpha() - 5)))
        glow.setColorAt(0.48, middle)
        glow.setColorAt(0.72, far)
        glow.setColorAt(1.0, QColor(88, 146, 255, 0))
        painter.fillRect(rect, glow)
        side = QLinearGradient(rect.left(), 0, rect.right(), 0)
        side_color = QColor(70, 118, 210, 10 if not light else 8)
        side.setColorAt(0.0, side_color)
        side.setColorAt(0.34, QColor(side_color.red(), side_color.green(), side_color.blue(), 0))
        side.setColorAt(0.66, QColor(side_color.red(), side_color.green(), side_color.blue(), 0))
        side.setColorAt(1.0, side_color)
        painter.fillRect(rect, side)
        painter.restore()
        border = QColor("#24304a" if not light else "#d2ddeb")
        painter.setPen(QPen(border, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 16, 16)


class RoundedProgressBar(QProgressBar):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._track_color = QColor(0, 0, 0, 0)
        self._border_color = QColor(0, 0, 0, 0)
        self._chunk_start = QColor("#59c9ff")
        self._chunk_end = QColor("#46f4ff")
        self.setTextVisible(False)

    def set_theme_colors(
        self,
        *,
        track: QColor,
        border: QColor,
        chunk_start: QColor,
        chunk_end: QColor,
    ) -> None:
        self._track_color = QColor(track)
        self._border_color = QColor(border)
        self._chunk_start = QColor(chunk_start)
        self._chunk_end = QColor(chunk_end)
        self.update()

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        radius = rect.height() / 2.0
        track_path = QPainterPath()
        track_path.addRoundedRect(rect, radius, radius)
        painter.fillPath(track_path, self._track_color)
        if self._border_color.alpha() > 0:
            painter.strokePath(track_path, QPen(self._border_color, 1))

        span = max(0, self.maximum() - self.minimum())
        if span <= 0:
            progress = 0.0
        else:
            progress = max(0.0, min(1.0, (self.value() - self.minimum()) / span))
        if progress <= 0.0:
            return

        fill_width = max(rect.height(), rect.width() * progress)
        fill_rect = QRectF(rect.left(), rect.top(), min(rect.width(), fill_width), rect.height())
        fill_path = QPainterPath()
        fill_path.addRoundedRect(fill_rect, radius, radius)
        gradient = QLinearGradient(fill_rect.left(), fill_rect.top(), fill_rect.right(), fill_rect.top())
        gradient.setColorAt(0.0, self._chunk_start)
        gradient.setColorAt(1.0, self._chunk_end)
        painter.save()
        painter.setClipPath(track_path)
        painter.fillPath(fill_path, gradient)
        painter.restore()


class OnboardingServiceProgressButton(QPushButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme_name = "night"
        self._count = 0
        self._required = 3
        self._fill_progress = 0.0
        self._morph_progress = 0.0
        self._hover_progress = 0.0
        self._target_fill_progress = 0.0
        self._target_morph_progress = 0.0
        self._fill_anim: QPropertyAnimation | None = None
        self._morph_anim: QPropertyAnimation | None = None
        self._hover_anim: QPropertyAnimation | None = None
        self._force_light = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFlat(True)
        self.setFixedSize(224, 44)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setStyleSheet("background: transparent; border: none;")

    def set_theme(self, theme: str) -> None:
        self._theme_name = theme
        self.update()

    def set_force_light(self, force: bool) -> None:
        self._force_light = force
        self.update()

    def set_selection_state(self, count: int, required: int, *, text: str) -> None:
        next_count = max(0, int(count))
        next_required = max(1, int(required))
        ready = next_count >= next_required
        self.setCursor(Qt.CursorShape.PointingHandCursor if ready else Qt.CursorShape.ArrowCursor)
        target_fill = max(0.0, min(1.0, next_count / next_required))
        target_morph = 1.0 if ready else 0.0
        if (
            self._count == next_count
            and self._required == next_required
            and self.text() == text
            and abs(self._target_fill_progress - target_fill) < 0.0005
            and abs(self._target_morph_progress - target_morph) < 0.0005
        ):
            return
        self._count = next_count
        self._required = next_required
        if self.text() != text:
            self.setText(text)
        self._target_fill_progress = target_fill
        self._target_morph_progress = target_morph
        self._animate_to(b"fillProgress", self._fill_progress, target_fill, 220)
        self._animate_to(b"morphProgress", self._morph_progress, target_morph, 260)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._count < self._required:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event: QEvent) -> None:
        self._animate_to(b"hoverProgress", self._hover_progress, 1.0 if self._count >= self._required else 0.0, 160)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._animate_to(b"hoverProgress", self._hover_progress, 0.0, 180)
        super().leaveEvent(event)

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        light = self._force_light or is_light_theme(self._theme_name)
        morph = max(0.0, min(1.0, self._morph_progress))
        fill_progress = max(0.0, min(1.0, self._fill_progress))

        width = 150.0 + (196.0 - 150.0) * morph
        height = 26.0 + (34.0 - 26.0) * morph
        rect = QRectF((self.width() - width) / 2.0, (self.height() - height) / 2.0, width, height)
        radius = height / 2.0

        shadow = QColor(17, 24, 39, 70 if not light else 30)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawRoundedRect(rect.translated(0, 5), radius, radius)

        track = QColor("#e5edf8" if light else "#202838")
        border = QColor("#c9d6eb" if light else "#33415a")
        painter.setBrush(track)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(rect, radius, radius)

        if fill_progress > 0.0:
            fill_rect = QRectF(rect.left(), rect.top(), rect.width() * fill_progress, rect.height())
            track_path = QPainterPath()
            track_path.addRoundedRect(rect, radius, radius)
            gradient = QLinearGradient(fill_rect.left(), fill_rect.top(), fill_rect.right(), fill_rect.bottom())
            gradient.setColorAt(0.0, QColor("#4f73d9" if light else "#5f8cff"))
            gradient.setColorAt(1.0, QColor("#2f65d8" if light else "#55d7ff"))
            painter.save()
            painter.setClipPath(track_path)
            painter.fillRect(fill_rect, gradient)
            painter.restore()

        if self._hover_progress > 0.0 and self._count >= self._required:
            hover = QColor("#ffffff")
            hover.setAlpha(int(34 * self._hover_progress))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(hover)
            painter.drawRoundedRect(rect, radius, radius)

        font = painter.font()
        font.setBold(True)
        font.setPointSizeF(9.4 + 0.8 * morph)
        painter.setFont(font)
        progress_text = f"{min(self._count, self._required)} / {self._required}"
        continue_text = self.text()
        progress_opacity = max(0.0, min(1.0, 1.0 - (morph - 0.52) / 0.24))
        continue_opacity = max(0.0, min(1.0, (morph - 0.68) / 0.26))
        progress_color = QColor("#ffffff") if fill_progress > 0.45 else QColor("#667389" if light else "#b4bfd0")
        continue_color = QColor("#ffffff")
        if progress_opacity > 0.0:
            painter.save()
            painter.setOpacity(progress_opacity)
            painter.setPen(progress_color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, progress_text)
            painter.restore()
        if continue_opacity > 0.0:
            painter.save()
            painter.setOpacity(continue_opacity)
            painter.setPen(continue_color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, continue_text)
            painter.restore()

    def _animate_to(self, name: bytes, start: float, end: float, duration: int) -> None:
        attr = "_fill_anim" if name == b"fillProgress" else "_morph_anim" if name == b"morphProgress" else "_hover_anim"
        current = getattr(self, attr, None)
        if isinstance(current, QPropertyAnimation):
            current.stop()
        if abs(float(start) - float(end)) < 0.0005:
            setter = self._set_fill_progress if name == b"fillProgress" else self._set_morph_progress if name == b"morphProgress" else self._set_hover_progress
            setter(float(end))
            setattr(self, attr, None)
            return
        animation = QPropertyAnimation(self, name, self)
        animation.setDuration(duration)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        animation.start()
        setattr(self, attr, animation)

    def _get_fill_progress(self) -> float:
        return self._fill_progress

    def _set_fill_progress(self, value: float) -> None:
        self._fill_progress = float(value)
        self.update()

    def _get_morph_progress(self) -> float:
        return self._morph_progress

    def _set_morph_progress(self, value: float) -> None:
        self._morph_progress = float(value)
        self.update()

    def _get_hover_progress(self) -> float:
        return self._hover_progress

    def _set_hover_progress(self, value: float) -> None:
        self._hover_progress = float(value)
        self.update()

    fillProgress = Property(float, _get_fill_progress, _set_fill_progress)
    morphProgress = Property(float, _get_morph_progress, _set_morph_progress)
    hoverProgress = Property(float, _get_hover_progress, _set_hover_progress)


class EmojiBadgeButton(QToolButton):
    def __init__(self, emoji: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._emoji = emoji
        self._emoji_color = QColor("#ffffff")
        self._offset = QPoint(0, 0)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)

    def setEmoji(self, emoji: str) -> None:
        self._emoji = emoji
        self.update()

    def setEmojiColor(self, color: str | QColor) -> None:
        self._emoji_color = QColor(color)
        self.update()

    def setEmojiOffset(self, dx: float, dy: float) -> None:
        self._offset = QPoint(int(round(dx)), int(round(dy)))
        self.update()

    def paintEvent(self, event: QEvent) -> None:
        if not self._emoji:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        font = painter.font()
        font.setPointSize(15)
        painter.setFont(font)
        painter.setPen(self._emoji_color)
        draw_rect = self.rect().adjusted(1, 1, -1, -1).translated(self._offset)
        painter.drawText(draw_rect, int(Qt.AlignmentFlag.AlignCenter), self._emoji)


class SmoothScrollController(QObject):
    def __init__(self, scrollable: QAbstractScrollArea, *, duration: int = 170, angle_divisor: float = 2.0) -> None:
        super().__init__(scrollable)
        self._scrollable = scrollable
        self._angle_divisor = max(1.0, float(angle_divisor))
        self._target_value = scrollable.verticalScrollBar().value()
        self._animation = QPropertyAnimation(scrollable.verticalScrollBar(), b"value", self)
        self._animation.setDuration(max(80, int(duration)))
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        scrollable.viewport().installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._scrollable.viewport() and event.type() == QEvent.Type.Wheel:
            wheel = event  # type: ignore[assignment]
            delta = 0
            if hasattr(wheel, "pixelDelta") and wheel.pixelDelta().y() != 0:  # type: ignore[attr-defined]
                delta = int(wheel.pixelDelta().y())  # type: ignore[attr-defined]
            elif hasattr(wheel, "angleDelta"):
                delta = int(wheel.angleDelta().y() / self._angle_divisor)  # type: ignore[attr-defined]
            if delta != 0:
                bar = self._scrollable.verticalScrollBar()
                self._target_value = max(bar.minimum(), min(bar.maximum(), self._target_value - delta))
                self._animation.stop()
                self._animation.setStartValue(bar.value())
                self._animation.setEndValue(self._target_value)
                self._animation.start()
                event.accept()
                return True
        return super().eventFilter(watched, event)


def _disable_native_window_rounding(widget: QWidget) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        hwnd = int(widget.winId())
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_DONOTROUND = 1
        value = ctypes.c_int(DWMWCP_DONOTROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        return


def _bring_widget_to_front(widget: QWidget) -> None:
    widget.raise_()
    widget.activateWindow()
    if not sys.platform.startswith("win"):
        return
    try:
        hwnd = int(widget.winId())
        SW_RESTORE = 9
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)  # type: ignore[attr-defined]
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)  # type: ignore[attr-defined]
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)  # type: ignore[attr-defined]
        ctypes.windll.user32.SetForegroundWindow(hwnd)  # type: ignore[attr-defined]
    except Exception:
        return


class AppDialog(QDialog):
    def __init__(self, parent: QWidget, context: ApplicationContext, title: str) -> None:
        super().__init__(parent)
        self.context = context
        self._drag_pos: QPoint | None = None
        self._fade_animation: QPropertyAnimation | None = None
        self._fade_closing = False
        self._force_done = False
        self._exec_loop: QEventLoop | None = None
        self._exec_result = QDialog.DialogCode.Rejected
        self.setObjectName("AppDialogWindow")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlag(Qt.WindowType.Dialog, True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        root = QFrame()
        root.setObjectName("DialogRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        title_bar = QFrame()
        title_bar.setObjectName("DialogTitleBar")
        title_bar.setFixedHeight(42)
        title_row = QHBoxLayout(title_bar)
        title_row.setContentsMargins(10, 8, 10, 8)
        title_row.setSpacing(8)

        title_label = QLabel(title)
        title_label.setProperty("class", "title")
        title_row.addWidget(title_label)
        title_row.addStretch(1)

        close_btn = QToolButton()
        close_btn.setProperty("class", "window")
        close_btn.setProperty("role", "close")
        suffix = "light" if is_light_theme(context.settings.get().theme) else "dark"
        close_btn.setIcon(QIcon(str(context.paths.ui_assets_dir / "icons" / f"window_close_{suffix}.svg")))
        close_btn.setIconSize(QSize(14, 14))
        close_btn.clicked.connect(self.reject)
        title_row.addWidget(close_btn)

        root_layout.addWidget(title_bar)
        self.body = QWidget()
        self.body.setObjectName("DialogBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(14, 12, 14, 12)
        self.body_layout.setSpacing(10)
        root_layout.addWidget(self.body)
        shell.addWidget(root)
        _disable_native_window_rounding(self)

    def prepare_and_center(self) -> None:
        self.adjustSize()
        if self.parentWidget() is not None:
            parent_rect = self.parentWidget().frameGeometry()
            target = parent_rect.center() - self.rect().center()
            self.move(target)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 42:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Print:
            super().keyPressEvent(event)
            return
        super().keyPressEvent(event)

    def showEvent(self, event: QEvent) -> None:
        _disable_native_window_rounding(self)
        super().showEvent(event)
        self._fade_closing = False
        if self._fade_animation is not None:
            self._fade_animation.stop()
        self.setWindowOpacity(0.0)
        animation = QPropertyAnimation(self, QByteArray(b"windowOpacity"), self)
        animation.setDuration(160)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.start()
        self._fade_animation = animation
        QTimer.singleShot(0, lambda: _bring_widget_to_front(self))

    def _start_close_fade(self, result: int) -> None:
        if self._force_done:
            super().done(result)
            return
        if self._fade_closing:
            return
        self._fade_closing = True
        if self._fade_animation is not None:
            self._fade_animation.stop()
        animation = QPropertyAnimation(self, QByteArray(b"windowOpacity"), self)
        animation.setDuration(120)
        animation.setStartValue(float(self.windowOpacity()))
        animation.setEndValue(0.0)
        animation.setEasingCurve(QEasingCurve.Type.InCubic)

        def _finish() -> None:
            self._force_done = True
            try:
                super(AppDialog, self).done(result)
            finally:
                self._force_done = False
                self._fade_closing = False
                self.setWindowOpacity(1.0)

        animation.finished.connect(_finish)
        animation.start()
        self._fade_animation = animation

    def done(self, result: int) -> None:
        if self._force_done:
            super().done(result)
            return
        self._start_close_fade(result)

    def exec(self) -> int:
        self._exec_result = QDialog.DialogCode.Rejected
        loop = QEventLoop(self)
        self._exec_loop = loop

        def _finish(code: int) -> None:
            self._exec_result = QDialog.DialogCode(code)
            if loop.isRunning():
                loop.quit()

        self.finished.connect(_finish)
        self.prepare_and_center()
        self.show()
        loop.exec()
        try:
            self.finished.disconnect(_finish)
        except Exception:
            pass
        self._exec_loop = None
        return int(self._exec_result)


class SettingsDialog(AppDialog):
    def __init__(self, parent: QWidget, context: ApplicationContext) -> None:
        self.context = context
        self._smooth_scroll_helpers: list[SmoothScrollController] = []
        self._scroll_fade_overlays: list[ScrollFadeOverlay] = []
        self._settings_scroll: QScrollArea | None = None
        self._settings_section_frames: dict[str, QFrame] = {}
        self._pending_scroll_section = ""
        super().__init__(parent, context, self._t("Настройки", "Settings"))
        self.setMinimumWidth(520)
        self.resize(600, 980)
        layout = self.body_layout

        self.theme_combo = ClickSelectComboBox()
        ui_language = self.context.settings.get().language
        for theme_id, theme_name in list_available_themes(self.context.paths.themes_dir, ui_language):
            self.theme_combo.addItem(theme_name, theme_id)
        self.language_combo = ClickSelectComboBox()
        for language_id in ("ru", "en"):
            self.language_combo.addItem(_language_display_name(language_id, ui_language), language_id)
        self.tg_host_input = QLineEdit()
        self.tg_port_input = QLineEdit()
        self.tg_secret_input = QLineEdit()
        self.tg_media_mode_combo = ClickSelectComboBox()
        self.tg_media_mode_combo.addItem(self._t("Стандартный", "Default"), "default")
        self.tg_media_mode_combo.addItem("Media fix", "media_fix")
        self.tg_media_mode_combo.addItem(self._t("Без DC override", "No DC override"), "empty")
        self.tg_dc_ip_input = QTextEdit()
        self.tg_dc_ip_input.setFixedHeight(72)
        self.tg_cfproxy_checkbox = QCheckBox(self._t("Cloudflare fallback", "Cloudflare fallback"))
        self.tg_cfproxy_priority_checkbox = QCheckBox(self._t("Пробовать Cloudflare первым", "Try Cloudflare first"))
        self.tg_cfproxy_domain_input = QLineEdit()
        self.tg_fake_tls_input = QLineEdit()
        self.tg_buf_input = QLineEdit()
        self.tg_pool_input = QLineEdit()
        self.zapret_udp_exclude_input = QLineEdit()
        self.ipset_mode_combo = ClickSelectComboBox()
        self.ipset_mode_combo.addItem("loaded", "loaded")
        self.ipset_mode_combo.addItem("none", "none")
        self.ipset_mode_combo.addItem("any", "any")
        self.game_mode_combo = ClickSelectComboBox()
        self.game_mode_combo.addItem(self._t("выключен", "disabled"), "disabled")
        self.game_mode_combo.addItem(self._t("tcp + udp", "tcp + udp"), "tcpudp")
        self.game_mode_combo.addItem(self._t("только tcp", "tcp only"), "tcp")
        self.game_mode_combo.addItem(self._t("только udp", "udp only"), "udp")
        self.autostart_checkbox = QCheckBox(self._t("Запускать вместе с Windows", "Run with Windows"))
        self.tray_checkbox = QCheckBox(self._t("Стартовать в трее", "Start in tray"))
        self.auto_components_checkbox = QCheckBox(self._t("Автозапуск компонентов", "Auto-run components"))
        self.check_updates_checkbox = QCheckBox(self._t("Проверять наличие обновлений", "Check for updates"))
        self.vpn_subscription_input = QLineEdit()
        self.vpn_subscription_input.setPlaceholderText("https://vpn.peshk0v.ru/sub/...")
        self.vpn_tun_checkbox = QCheckBox(self._t("Использовать TUN-режим", "Use TUN mode"))
        self.vpn_routing_combo = ClickSelectComboBox()
        self.vpn_routing_combo.addItem(self._t("Глобальная", "Global"), "global")
        self.vpn_routing_combo.addItem(self._t("RU чёрный список", "RU blacklist"), "blacklist")
        self.vpn_routing_combo.addItem(self._t("RU белый список", "RU whitelist"), "whitelist")
        self.vpn_proxy_combo = ClickSelectComboBox()
        self.vpn_proxy_combo.addItem(self._t("Очистить системный прокси", "Clear system proxy"), "clear")
        self.vpn_proxy_combo.addItem(self._t("Установить системный прокси", "Set system proxy"), "set")
        self.vpn_proxy_combo.addItem(self._t("Не менять системный прокси", "Do not change system proxy"), "unchanged")
        self.vpn_proxy_combo.addItem(self._t("PAC-режим", "PAC mode"), "pac")
        self.vpn_processes_input = QLineEdit()
        self.vpn_processes_input.setPlaceholderText(
            self._t(
                "Список процессов через запятую",
                "Comma-separated process list",
            )
        )
        self.vpn_refresh_btn = QPushButton(self._t("Обновить подписку", "Refresh subscription"))
        self.vpn_refresh_btn.clicked.connect(self._refresh_vpn_subscription)
        self._vpn_refresh_default_text = self.vpn_refresh_btn.text()

        scroll = QScrollArea()
        scroll.setObjectName("SettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(560)
        scroll.setMaximumHeight(760)
        self._settings_scroll = scroll
        canvas = QWidget()
        canvas.setObjectName("SettingsCanvas")
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(10)
        scroll.setWidget(canvas)
        self._smooth_scroll_helpers.append(SmoothScrollController(scroll))
        fade = ScrollFadeOverlay(scroll)
        fade.set_theme(self.context.settings.get().theme)
        fade.set_surface_color(_dialog_surface_color(self.context.settings.get().theme))
        self._scroll_fade_overlays.append(fade)
        layout.addWidget(scroll, 1)

        app_form = self._settings_section(canvas_layout, self._t("Приложение", "Application"), "app")
        app_form.addRow(self._t("Тема", "Theme"), self.theme_combo)
        app_form.addRow(self._t("Язык", "Language"), self.language_combo)
        app_form.addRow("", self.autostart_checkbox)
        app_form.addRow("", self.tray_checkbox)
        app_form.addRow("", self.auto_components_checkbox)
        app_form.addRow("", self.check_updates_checkbox)

        vpn_form = self._settings_section(canvas_layout, "peshk0v vpn", "peshk0v-vpn")
        self.vpn_section_frame = vpn_form.parentWidget()
        vpn_form.addRow(self._t("Ссылка подписки", "Subscription URL"), self.vpn_subscription_input)
        vpn_form.addRow("", self.vpn_refresh_btn)
        vpn_form.addRow("", self.vpn_tun_checkbox)
        vpn_form.addRow(self._t("Маршрутизация", "Routing"), self.vpn_routing_combo)
        vpn_form.addRow(self._t("Системный прокси", "System proxy"), self.vpn_proxy_combo)
        vpn_form.addRow(self._t("Проксировать процессы", "Proxy processes"), self.vpn_processes_input)

        zapret_form = self._settings_section(canvas_layout, "Zapret", "zapret")
        zapret_form.addRow("IPSet mode", self.ipset_mode_combo)
        zapret_form.addRow(self._t("Gaming mode", "Gaming mode"), self.game_mode_combo)
        zapret_form.addRow(self._t("Исключить UDP-порты", "Exclude UDP ports"), self.zapret_udp_exclude_input)

        tg_form = self._settings_section(canvas_layout, "TG WS Proxy", "tg-ws-proxy")
        tg_form.addRow(self._t("Хост", "Host"), self.tg_host_input)
        tg_form.addRow(self._t("Порт", "Port"), self.tg_port_input)
        tg_form.addRow(self._t("Секрет", "Secret"), self.tg_secret_input)
        tg_form.addRow(self._t("Media mode", "Media mode"), self.tg_media_mode_combo)
        tg_form.addRow("DC -> IP", self.tg_dc_ip_input)
        tg_form.addRow("", self.tg_cfproxy_checkbox)
        tg_form.addRow("", self.tg_cfproxy_priority_checkbox)
        tg_form.addRow(self._t("CF domain", "CF domain"), self.tg_cfproxy_domain_input)
        tg_form.addRow(self._t("Fake TLS domain", "Fake TLS domain"), self.tg_fake_tls_input)
        tg_form.addRow(self._t("Буфер, КБ", "Buffer, KB"), self.tg_buf_input)
        tg_form.addRow(self._t("Pool size", "Pool size"), self.tg_pool_input)

        self.tg_media_mode_combo.currentIndexChanged.connect(self._apply_tg_media_preset)

        restart_onboarding_btn = QPushButton(self._t("Настроить заново", "Configure again"))
        restart_onboarding_btn.setObjectName("RestartOnboardingButton")
        restart_onboarding_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        restart_onboarding_btn.setMinimumHeight(38)
        restart_onboarding_btn.setStyleSheet(
            "QPushButton#RestartOnboardingButton {"
            "background: transparent;"
            "border: 1px solid rgba(239, 68, 68, 95);"
            "border-radius: 12px;"
            "padding: 8px 14px;"
            "color: rgba(248, 113, 113, 210);"
            "font-weight: 650;"
            "}"
            "QPushButton#RestartOnboardingButton:hover {"
            "background: rgba(239, 68, 68, 22);"
            "border: 1px solid rgba(248, 113, 113, 145);"
            "color: rgba(252, 165, 165, 235);"
            "}"
        )
        restart_onboarding_btn.clicked.connect(self._restart_onboarding)
        canvas_layout.addWidget(restart_onboarding_btn)

        credits = QLabel(
            self._t(
                "Благодарности: оригинальный набор zapret и tg-ws-proxy от Flowseal.\n"
                "Оригинальная экосистема zapret от bol-van.\n"
                f"Это приложение является отдельным интерфейсом управления.\nВерсия: {__version__} | Автор: peshk0v",
                "Credits: original zapret bundle and tg-ws-proxy by Flowseal.\n"
                "Original zapret ecosystem by bol-van.\n"
                f"This app is a separate management UI.\nVersion: {__version__} | Author: peshk0v",
            )
        )
        credits.setProperty("class", "muted")
        canvas_layout.addWidget(credits)
        canvas_layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton(self._t("Отмена", "Cancel"))
        save_btn = QPushButton(self._t("Сохранить", "Save"))
        save_btn.setProperty("class", "primary")
        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self.accept)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)
        layout.addLayout(buttons)
        self._load()

    def _settings_section(self, parent_layout: QVBoxLayout, title: str, section_id: str = "") -> QFormLayout:
        frame = QFrame()
        frame.setProperty("class", "settingsSection")
        if section_id:
            frame.setObjectName(f"SettingsSection_{section_id.replace('-', '_')}")
            self._settings_section_frames[section_id] = frame
        section_layout = QVBoxLayout(frame)
        section_layout.setContentsMargins(14, 12, 14, 14)
        section_layout.setSpacing(10)
        label = QLabel(title)
        label.setProperty("class", "title")
        section_layout.addWidget(label)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)
        section_layout.addLayout(form)
        parent_layout.addWidget(frame)
        return form

    def scroll_to_component_settings(self, component_id: str) -> None:
        target = str(component_id or "").strip()
        if target == "tg":
            target = "tg-ws-proxy"
        self._pending_scroll_section = target
        QTimer.singleShot(0, self._scroll_to_pending_section)
        QTimer.singleShot(140, self._scroll_to_pending_section)

    def _scroll_to_pending_section(self) -> None:
        target = self._pending_scroll_section
        if not target or self._settings_scroll is None:
            return
        frame = self._settings_section_frames.get(target)
        if frame is None or not frame.isVisible():
            return
        try:
            self._settings_scroll.ensureWidgetVisible(frame, 18, 18)
        except Exception:
            pass

    def _t(self, ru: str, en: str) -> str:
        return ru if self.context.settings.get().language == "ru" else en

    def _restart_onboarding(self) -> None:
        parent = self.parent()
        self.reject()
        if parent is not None and hasattr(parent, "_restart_onboarding_from_settings"):
            QTimer.singleShot(0, getattr(parent, "_restart_onboarding_from_settings"))

    def _load(self) -> None:
        settings = self.context.settings.get()
        vpn_state = self.context.vpn.state()
        theme_index = self.theme_combo.findData(settings.theme)
        self.theme_combo.setCurrentIndex(theme_index if theme_index >= 0 else 0)
        self._select_combo_value(self.language_combo, settings.language)
        self.tg_host_input.setText(settings.tg_proxy_host)
        self.tg_port_input.setText(str(settings.tg_proxy_port))
        self.tg_secret_input.setText(settings.tg_proxy_secret)
        self.tg_dc_ip_input.setPlainText(settings.tg_proxy_dc_ip)
        self.tg_cfproxy_checkbox.setChecked(settings.tg_proxy_cfproxy_enabled)
        self.tg_cfproxy_priority_checkbox.setChecked(settings.tg_proxy_cfproxy_priority)
        self.tg_cfproxy_domain_input.setText(settings.tg_proxy_cfproxy_domain)
        self.tg_fake_tls_input.setText(settings.tg_proxy_fake_tls_domain)
        self.tg_buf_input.setText(str(settings.tg_proxy_buf_kb))
        self.tg_pool_input.setText(str(settings.tg_proxy_pool_size))
        self._sync_tg_media_mode_from_dc_ip(settings.tg_proxy_dc_ip)
        ipset_idx = self.ipset_mode_combo.findData(settings.zapret_ipset_mode)
        self.ipset_mode_combo.setCurrentIndex(ipset_idx if ipset_idx >= 0 else 0)
        game_idx = self.game_mode_combo.findData(settings.zapret_game_filter_mode)
        self.game_mode_combo.setCurrentIndex(game_idx if game_idx >= 0 else 0)
        self.zapret_udp_exclude_input.setText(settings.zapret_udp_exclude_ports)
        self.autostart_checkbox.setChecked(self.context.autostart.is_enabled())
        self.tray_checkbox.setChecked(settings.start_in_tray)
        self.auto_components_checkbox.setChecked(settings.auto_run_components)
        self.check_updates_checkbox.setChecked(settings.check_updates_on_start)
        self.vpn_subscription_input.setText(
            str(vpn_state.get("subscription_url", "") or settings.peshk0v_vpn_subscription_url)
        )
        self.vpn_tun_checkbox.setChecked(bool(vpn_state.get("tun_enabled", settings.peshk0v_vpn_tun_enabled)))
        self._select_combo_value(
            self.vpn_routing_combo,
            str(vpn_state.get("routing_mode", settings.peshk0v_vpn_routing_mode) or "global"),
        )
        self._select_combo_value(
            self.vpn_proxy_combo,
            str(vpn_state.get("system_proxy_mode", settings.peshk0v_vpn_system_proxy_mode) or "pac"),
        )
        self.vpn_processes_input.setText(
            str(vpn_state.get("processes", settings.peshk0v_vpn_processes) or "")
        )
        if self.vpn_section_frame is not None:
            self.vpn_section_frame.setVisible(str(vpn_state.get("subscription_state", "") or "") == "valid")

    def load_from_payload(self, payload: dict[str, object]) -> None:
        self._load()
        theme_index = self.theme_combo.findData(str(payload.get("theme", self.context.settings.get().theme)))
        self.theme_combo.setCurrentIndex(theme_index if theme_index >= 0 else self.theme_combo.currentIndex())
        language = str(payload.get("language", self.context.settings.get().language))
        if language:
            self._select_combo_value(self.language_combo, language)
        self.tg_host_input.setText(str(payload.get("tg_proxy_host", self.context.settings.get().tg_proxy_host)))
        self.tg_port_input.setText(str(payload.get("tg_proxy_port", self.context.settings.get().tg_proxy_port)))
        self.tg_secret_input.setText(str(payload.get("tg_proxy_secret", self.context.settings.get().tg_proxy_secret)))
        tg_dc_ip = str(payload.get("tg_proxy_dc_ip", self.context.settings.get().tg_proxy_dc_ip))
        self.tg_dc_ip_input.setPlainText(tg_dc_ip)
        self.tg_cfproxy_checkbox.setChecked(bool(payload.get("tg_proxy_cfproxy_enabled", self.context.settings.get().tg_proxy_cfproxy_enabled)))
        self.tg_cfproxy_priority_checkbox.setChecked(bool(payload.get("tg_proxy_cfproxy_priority", self.context.settings.get().tg_proxy_cfproxy_priority)))
        self.tg_cfproxy_domain_input.setText(str(payload.get("tg_proxy_cfproxy_domain", self.context.settings.get().tg_proxy_cfproxy_domain)))
        self.tg_fake_tls_input.setText(str(payload.get("tg_proxy_fake_tls_domain", self.context.settings.get().tg_proxy_fake_tls_domain)))
        self.tg_buf_input.setText(str(payload.get("tg_proxy_buf_kb", self.context.settings.get().tg_proxy_buf_kb)))
        self.tg_pool_input.setText(str(payload.get("tg_proxy_pool_size", self.context.settings.get().tg_proxy_pool_size)))
        self._sync_tg_media_mode_from_dc_ip(tg_dc_ip)
        ipset_idx = self.ipset_mode_combo.findData(str(payload.get("zapret_ipset_mode", self.context.settings.get().zapret_ipset_mode)))
        self.ipset_mode_combo.setCurrentIndex(ipset_idx if ipset_idx >= 0 else self.ipset_mode_combo.currentIndex())
        game_idx = self.game_mode_combo.findData(str(payload.get("zapret_game_filter_mode", self.context.settings.get().zapret_game_filter_mode)))
        self.game_mode_combo.setCurrentIndex(game_idx if game_idx >= 0 else self.game_mode_combo.currentIndex())
        self.zapret_udp_exclude_input.setText(str(payload.get("zapret_udp_exclude_ports", self.context.settings.get().zapret_udp_exclude_ports)))
        self.autostart_checkbox.setChecked(bool(payload.get("autostart_windows", self.context.settings.get().autostart_windows)))
        self.tray_checkbox.setChecked(bool(payload.get("start_in_tray", self.context.settings.get().start_in_tray)))
        self.auto_components_checkbox.setChecked(bool(payload.get("auto_run_components", self.context.settings.get().auto_run_components)))
        self.check_updates_checkbox.setChecked(bool(payload.get("check_updates_on_start", self.context.settings.get().check_updates_on_start)))
        self.vpn_subscription_input.setText(
            str(payload.get("peshk0v_vpn_subscription_url", self.vpn_subscription_input.text()))
        )
        self.vpn_tun_checkbox.setChecked(
            bool(payload.get("peshk0v_vpn_tun_enabled", self.vpn_tun_checkbox.isChecked()))
        )
        self._select_combo_value(
            self.vpn_routing_combo,
            str(payload.get("peshk0v_vpn_routing_mode", self.vpn_routing_combo.currentData() or "global")),
        )
        self._select_combo_value(
            self.vpn_proxy_combo,
            str(payload.get("peshk0v_vpn_system_proxy_mode", self.vpn_proxy_combo.currentData() or "pac")),
        )
        self.vpn_processes_input.setText(
            str(payload.get("peshk0v_vpn_processes", self.vpn_processes_input.text()))
        )

    def payload(self) -> dict[str, object]:
        try:
            tg_port = int(self.tg_port_input.text().strip() or "1443")
        except ValueError:
            tg_port = 1443
        try:
            tg_buf_kb = int(self.tg_buf_input.text().strip() or "256")
        except ValueError:
            tg_buf_kb = 256
        try:
            tg_pool_size = int(self.tg_pool_input.text().strip() or "4")
        except ValueError:
            tg_pool_size = 4
        return {
            "theme": self.theme_combo.currentData() or "night",
            "active_profile_id": self.context.settings.get().active_profile_id,
            "language": self.language_combo.currentData() or self.context.settings.get().language,
            "mods_index_url": self.context.settings.get().mods_index_url,
            "tg_proxy_host": self.tg_host_input.text().strip() or "127.0.0.1",
            "tg_proxy_port": tg_port,
            "tg_proxy_secret": self.tg_secret_input.text().strip(),
            "tg_proxy_dc_ip": self.tg_dc_ip_input.toPlainText().strip(),
            "tg_proxy_cfproxy_enabled": self.tg_cfproxy_checkbox.isChecked(),
            "tg_proxy_cfproxy_priority": self.tg_cfproxy_priority_checkbox.isChecked(),
            "tg_proxy_cfproxy_domain": self.tg_cfproxy_domain_input.text().strip(),
            "tg_proxy_fake_tls_domain": self.tg_fake_tls_input.text().strip(),
            "tg_proxy_buf_kb": max(4, tg_buf_kb),
            "tg_proxy_pool_size": max(0, tg_pool_size),
            "zapret_ipset_mode": self.ipset_mode_combo.currentData() or "loaded",
            "zapret_game_filter_mode": self.game_mode_combo.currentData() or "disabled",
            "zapret_udp_exclude_ports": self.zapret_udp_exclude_input.text().strip(),
            "autostart_windows": self.autostart_checkbox.isChecked(),
            "start_in_tray": self.tray_checkbox.isChecked(),
            "auto_run_components": self.auto_components_checkbox.isChecked(),
            "check_updates_on_start": self.check_updates_checkbox.isChecked(),
            "peshk0v_vpn_subscription_url": self.vpn_subscription_input.text().strip(),
            "peshk0v_vpn_tun_enabled": self.vpn_tun_checkbox.isChecked(),
            "peshk0v_vpn_routing_mode": self.vpn_routing_combo.currentData() or "global",
            "peshk0v_vpn_system_proxy_mode": self.vpn_proxy_combo.currentData() or "pac",
            "peshk0v_vpn_processes": self.vpn_processes_input.text().strip(),
        }

    def _select_combo_value(self, combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _refresh_vpn_subscription(self) -> None:
        parent = self.parent()
        if parent is None or not hasattr(parent, "_refresh_peshk0v_vpn_subscription_from_settings"):
            return
        self.set_vpn_refresh_state("loading")
        getattr(parent, "_refresh_peshk0v_vpn_subscription_from_settings")(self.vpn_subscription_input.text().strip())

    def set_vpn_refresh_state(self, state: str) -> None:
        if state == "loading":
            self.vpn_refresh_btn.setEnabled(False)
            self.vpn_refresh_btn.setText(self._t("Обновление...", "Refreshing..."))
            return
        if state == "done":
            self.vpn_refresh_btn.setEnabled(False)
            self.vpn_refresh_btn.setText(self._t("Обновлено", "Updated"))
            QTimer.singleShot(2000, lambda: self.set_vpn_refresh_state("idle"))
            return
        self.vpn_refresh_btn.setText(self._t("Обновить подписку", "Refresh subscription"))
        self.vpn_refresh_btn.setEnabled(True)

    def _sync_tg_media_mode_from_dc_ip(self, value: str) -> None:
        normalized = "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())
        mapping = {
            "2:149.154.167.220\n4:149.154.167.220": "default",
            "4:149.154.167.220": "media_fix",
            "": "empty",
        }
        mode = mapping.get(normalized, "default")
        index = self.tg_media_mode_combo.findData(mode)
        if index >= 0:
            self.tg_media_mode_combo.blockSignals(True)
            self.tg_media_mode_combo.setCurrentIndex(index)
            self.tg_media_mode_combo.blockSignals(False)

    def _apply_tg_media_preset(self) -> None:
        mode = str(self.tg_media_mode_combo.currentData() or "default")
        if mode == "media_fix":
            self.tg_dc_ip_input.setPlainText("4:149.154.167.220")
        elif mode == "empty":
            self.tg_dc_ip_input.setPlainText("")
        else:
            self.tg_dc_ip_input.setPlainText("2:149.154.167.220\n4:149.154.167.220")


class MainWindow(QMainWindow):
    def __init__(
        self,
        context: ApplicationContext,
        launch_hidden: bool = False,
        startup_show_onboarding: bool = False,
        startup_snapshot: dict[str, object] | None = None,
        skip_autosettings: bool = False,
    ) -> None:
        super().__init__()
        base_font = QFont(_load_ui_font_family(context.paths.ui_assets_dir), 10)
        base_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
        base_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        app = QApplication.instance()
        if app is not None:
            app.setFont(base_font)
        self.setFont(base_font)
        self.context = context
        self._launch_hidden = launch_hidden
        self._startup_show_onboarding = startup_show_onboarding
        self._skip_autosettings = skip_autosettings
        self._skip_next_show_focus = launch_hidden
        self._drag_pos: QPoint | None = None
        self._tray_notifications_shown = False
        self._force_exit = False
        self._shutdown_started = False
        self._nav_buttons: list[QToolButton] = []
        self._github_sidebar_btn: GitHubSidebarButton | None = None
        self._status_badges: dict[str, StatusBadge] = {}
        self._min_btn: QToolButton | None = None
        self._close_btn: QToolButton | None = None
        self._toggle_in_progress = False
        self._vpn_mode_switch_in_progress = False
        self._vpn_switch_power_transition = False
        self._loading_frame = 0
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(220)
        self._loading_timer.timeout.connect(self._advance_loading_caption)
        self._component_loading_timer = QTimer(self)
        self._component_loading_timer.setInterval(200)
        self._component_loading_timer.timeout.connect(self._advance_component_loading)
        self._ui_signals = _UiSignals()
        self._ui_signals.toggle_done.connect(self._on_master_toggle_finished)
        self._ui_signals.component_action_done.connect(self._on_component_action_done)
        self._ui_signals.general_test_progress.connect(self._on_general_test_progress)
        self._ui_signals.general_test_done.connect(self._on_general_test_done)
        self._ui_signals.update_check_done.connect(self._on_update_check_done)
        self._ui_signals.update_prepare_done.connect(self._on_update_prepare_done)
        self._ui_signals.page_payload_ready.connect(self._on_page_payload_ready)
        self._updating_general_combo = False
        self._pending_info_message: tuple[str, str] | None = None
        self._components_cards_root: QWidget | None = None
        self._components_cards_layout: QGridLayout | None = None
        self._components_scroll: QScrollArea | None = None
        self._components_card_by_id: dict[str, QFrame] = {}
        self._components_scroll_target_component_id = ""
        self._component_loading_buttons: dict[str, QPushButton] = {}
        self._component_loading_base_text: dict[str, str] = {}
        self._component_loading_frame = 0
        self._power_caption_base_text = "OFF"
        self._general_loading_combo: QComboBox | None = None
        self._general_loading_label: QLabel | None = None
        self._general_test_dialog: AppDialog | None = None
        self._general_test_status_label: QLabel | None = None
        self._general_test_eta_label: QLabel | None = None
        self._general_test_counter_label: QLabel | None = None
        self._general_test_progress_bar: QProgressBar | None = None
        self._general_test_started_at = 0.0
        self._general_test_current_index = 0
        self._general_test_total = 0
        self._general_test_last_progress_at = 0.0
        self._general_test_options: list[dict[str, str]] = []
        self._general_test_results: list[dict[str, object]] = []
        self._general_test_next_option_index = 0
        self._general_test_target_budget_seconds = 0
        self._general_test_remaining_budget_seconds = 0
        self._general_test_found_working_id = ""
        self._general_test_running = False
        self._general_test_cancelled = False
        self._general_test_show_results = True
        self._general_test_auto_apply = False
        self._general_test_embedded = False
        self._general_test_eta_timer = QTimer(self)
        self._general_test_eta_timer.setInterval(1000)
        self._general_test_eta_timer.timeout.connect(self._update_general_test_eta)
        self._general_test_task_id: str | None = None
        self._general_test_original_general = ""
        self._general_test_waiting_runtime_prepare = False
        self._general_test_runtime_restore_payload: dict[str, object] | None = None
        self._first_general_prompt: AppDialog | None = None
        self._onboarding_active = False
        self._onboarding_running = False
        self._onboarding_widget: QWidget | None = None
        self._onboarding_stage_host: QWidget | None = None
        self._onboarding_stage_layout: QStackedLayout | None = None
        self._onboarding_intro_panel: QWidget | None = None
        self._onboarding_intro_icon: QLabel | None = None
        self._onboarding_intro_title_label: QLabel | None = None
        self._onboarding_intro_desc_label: QLabel | None = None
        self._onboarding_actions_widget: QWidget | None = None
        self._onboarding_services_stage_panel: QWidget | None = None
        self._onboarding_title_label: QLabel | None = None
        self._onboarding_desc_label: QLabel | None = None
        self._onboarding_running_stage_panel: QWidget | None = None
        self._onboarding_running_title_label: QLabel | None = None
        self._onboarding_running_desc_label: QLabel | None = None
        self._onboarding_result_stage_panel: QWidget | None = None
        self._onboarding_result_title_label: QLabel | None = None
        self._onboarding_result_desc_label: QLabel | None = None
        self._onboarding_primary_btn: QPushButton | None = None
        self._onboarding_secondary_btn: QPushButton | None = None
        self._onboarding_service_action_btn: OnboardingServiceProgressButton | None = None
        self._onboarding_result_actions_widget: QWidget | None = None
        self._onboarding_result_primary_btn: QPushButton | None = None
        self._onboarding_progress_label: QLabel | None = None
        self._onboarding_progress_counter_label: QLabel | None = None
        self._onboarding_progress_bar: QProgressBar | None = None
        self._onboarding_result_card: QFrame | None = None
        self._onboarding_result_label: QLabel | None = None
        self._onboarding_found_label: QLabel | None = None
        self._onboarding_wrap_widget: QWidget | None = None
        self._onboarding_intro_transition_overlay: QLabel | None = None
        self._onboarding_entry_overlay: QLabel | None = None
        self._quick_onboarding_entry_pixmap: QPixmap | None = None
        self._quick_onboarding_entry_pixmap_size = QSize()
        self._onboarding_stage = "intro"
        self._onboarding_quick_restart = False
        self._onboarding_back_btn: QToolButton | None = None
        self._onboarding_services_panel: QWidget | None = None
        self._onboarding_services_title_label: QLabel | None = None
        self._onboarding_services_hint_label: QLabel | None = None
        self._onboarding_services_count_label: QLabel | None = None
        self._onboarding_services_grid: ServiceGridPanel | None = None
        self._onboarding_services_scroll: QScrollArea | None = None
        self._onboarding_services_fade: ScrollFadeOverlay | None = None
        self._onboarding_services_minimum = 3
        self._onboarding_transition_busy = False
        self._onboarding_transition_token = 0
        self._onboarding_manual_restart = False
        self._onboarding_services_prewarm_scheduled = False
        self._onboarding_services_prewarm_done = False
        self._onboarding_services_surface_ready = False
        self._onboarding_services_search: QLineEdit | None = None
        self._onboarding_services_prewarm_queue: list[tuple[str, str, int, bool] | tuple[str, int]] = []
        self._onboarding_quick_prewarm_done = False
        self._onboarding_prewarming = False
        self._onboarding_glow_orbit_timer = QTimer(self)
        self._onboarding_glow_orbit_timer.setInterval(16)
        self._onboarding_glow_orbit_timer.timeout.connect(self._advance_onboarding_glow_orbit)
        self._onboarding_glow_orbit_points: list[tuple[float, float]] = [(0.84, 0.16), (0.16, 0.16), (0.16, 0.86), (0.84, 0.86)]
        self._onboarding_glow_orbit_index = 0
        self._onboarding_glow_orbit_phase = -0.88
        self._services_sync_timer = QTimer(self)
        self._services_sync_timer.setSingleShot(True)
        self._services_sync_timer.timeout.connect(self._flush_selected_services_backend_sync)
        self._pending_selected_service_ids: list[str] | None = None
        self._pending_selected_services_revision = 0
        self._optimistic_selected_service_ids: list[str] | None = None
        self._services_selection_revision = 0
        self._services_selection_acked_revision = 0
        self._sidebar_widget: QWidget | None = None
        self._settings_diag_dialog: AppDialog | None = None
        self._settings_diag_status_label: QLabel | None = None
        self._settings_diag_progress_bar: QProgressBar | None = None
        self._settings_diag_task_id: str | None = None
        self._settings_diag_cancelled = False
        self._loading_action = "connect"
        self._notifications_btn: NotificationBellButton | None = None
        self._notifications_popup: QWidget | None = None
        self._notifications_popup_last_closed_at = 0.0
        self._notified_component_errors: set[tuple[str, str]] = set()
        self._windows_taskbar = WindowsTaskbarIntegration()
        self._taskbar_progress_active = False
        self._taskbar_important_attention = False

        self._dashboard_title_label: QLabel | None = None
        self._services_title_label: QLabel | None = None
        self._services_subtitle_label: QLabel | None = None
        self._services_hint_label: QLabel | None = None
        self._services_count_label: QLabel | None = None
        self._services_grid: ServiceGridPanel | None = None
        self._services_scroll: QScrollArea | None = None
        self._components_title_label: QLabel | None = None
        self._mods_title_label: QLabel | None = None
        self._mods_subtitle_label: QLabel | None = None
        self._mods_add_btn: QPushButton | None = None
        self.power_aura: PowerAuraWidget | None = None
        self.power_caption_text: QLabel | None = None
        self.power_vpn_btn: QToolButton | None = None
        self.power_reconfigure_btn: QToolButton | None = None
        self.power_caption_dots: QLabel | None = None
        self._power_caption_dots_opacity: QGraphicsOpacityEffect | None = None
        self._power_caption_dots_blur: QGraphicsBlurEffect | None = None
        self._files_title_label: QLabel | None = None
        self._editor_title_label: QLabel | None = None
        self._logs_title_label: QLabel | None = None
        self._logs_refresh_btn: QPushButton | None = None
        self._logs_source_combo: QComboBox | None = None
        self._logs_stack: QStackedWidget | None = None
        self._logs_loading_label: QLabel | None = None
        self._current_log_source = "all"
        self._pending_logs_payload: dict[str, object] | None = None
        self._logs_force_scroll_bottom = True
        self._logs_live_timer = QTimer(self)
        self._logs_live_timer.setInterval(1000)
        self._logs_live_timer.timeout.connect(self._refresh_logs_live)
        self._tray_show_action: QAction | None = None
        self._tray_quit_action: QAction | None = None
        self._tray_toggle_action: QAction | None = None
        self._tray_general_menu: QMenu | None = None
        self._tray_general_action_group: QActionGroup | None = None
        self._update_check_in_progress = False
        self._update_prepare_dialog: AppDialog | None = None
        self._update_check_dialog: AppDialog | None = None
        self._update_check_label: QLabel | None = None
        self._component_update_dialog: AppDialog | None = None
        self._component_update_label: QLabel | None = None
        self._last_prompted_update_version = ""
        self._resume_component_ids: list[str] = []
        self._resume_restart_pending = False
        self._file_mode_stack: QStackedWidget | None = None
        self._file_home_page: QWidget | None = None
        self._files_home_scroll: QScrollArea | None = None
        self._file_tags_page: QWidget | None = None
        self._file_advanced_page: QWidget | None = None
        self._file_tag_title: QLabel | None = None
        self._file_tag_subtitle: QLabel | None = None
        self._file_tag_input: QLineEdit | None = None
        self._file_tag_canvas: QWidget | None = None
        self._file_tag_flow: FlowLayout | None = None
        self._file_tag_scroll: QScrollArea | None = None
        self._files_intro_label: QLabel | None = None
        self._file_mode_cards: list[dict[str, object]] = []
        self._current_file_collection = "domains"
        self._current_file_list_filter = "all"
        self._favorite_general_buttons: dict[str, QToolButton] = {}
        self._general_options_cache: list[dict[str, str]] | None = None
        self._general_options_refresh_in_progress = False
        self._refresh_dirty_sections = {"dashboard", "services", "components", "mods", "files", "logs", "tray"}
        self._refresh_scheduled = False
        self._initial_refresh_pending = False
        self._merge_ensure_in_progress = False
        self._page_refresh_in_progress: set[str] = set()
        self._page_payload_cache: dict[str, object] = {}
        self._settings_dialog: SettingsDialog | None = None
        self._settings_dialog_signature: tuple[str, str] | None = None
        self._pending_settings_payload: dict[str, object] | None = None
        self._settings_save_revision = 0
        self._settings_save_acked_revision = 0
        self._loading_overlay_fade: QPropertyAnimation | None = None
        self._loading_overlay_context = ""
        self._current_file_values_cache: list[str] = []
        self._file_tag_render_values: list[str] = []
        self._file_tag_render_index = 0
        self._file_tag_render_finish_loading = False
        self._file_tag_render_generation = 0
        self._file_tag_render_summary = ""
        self._file_tag_display_signature: tuple[str, int, str] | None = None
        self._file_tag_display_limit = 900
        self._file_tag_render_timer = QTimer(self)
        self._file_tag_render_timer.setSingleShot(True)
        self._file_tag_render_timer.timeout.connect(self._render_file_tags_chunk)
        self._backend_tasks: dict[str, str] = {}
        self._backend_attached = False
        self._component_defs_cache: dict[str, ComponentDefinition] = {}
        self._component_states_cache: dict[str, ComponentState] = {}
        self._mods_index_cache: list[object] = []
        self._mods_installed_cache: dict[str, object] = {}
        self._startup_snapshot_ready = False
        self._page_blur_effect: QGraphicsBlurEffect | None = None
        self._page_opacity_effect: QGraphicsOpacityEffect | None = None
        self._page_transition_overlay: QWidget | None = None
        self._page_transition_overlay_label: QLabel | None = None
        self._page_transition_overlay_next_label: QLabel | None = None
        self._page_transition_overlay_blur_effect: QGraphicsBlurEffect | None = None
        self._page_transition_overlay_opacity_effect: QGraphicsOpacityEffect | None = None
        self._page_transition_overlay_next_opacity_effect: QGraphicsOpacityEffect | None = None
        self._pages_shell: QWidget | None = None
        self._pages_host: QWidget | None = None
        self._content_surface: QWidget | None = None
        self._content_surface_layout: QVBoxLayout | None = None
        self._page_transition_out: QPropertyAnimation | None = None
        self._page_transition_in: QPropertyAnimation | None = None
        self._page_transition_target = -1
        self._page_transition_running = False
        self._page_transition_started_at = 0.0
        self._window_opacity_animation: QPropertyAnimation | None = None
        self._window_fade_pending_action: str | None = None
        self._nav_highlight_initialized = False
        self._skip_next_show_fade = False
        self._initial_show_completed = False
        self._startup_deferred_refresh_scheduled = False
        self._files_refresh_token = 0
        self._files_loading_timer = QTimer(self)
        self._files_loading_timer.setInterval(170)
        self._files_loading_timer.timeout.connect(self._advance_files_loading_frame)
        self._files_loading_frame = 0
        self._files_tags_loading_label: QLabel | None = None
        self._files_list_loading_label: QLabel | None = None
        self._files_editor_loading_label: QLabel | None = None
        self._files_tags_stack: QStackedWidget | None = None
        self._files_list_stack: QStackedWidget | None = None
        self._files_editor_stack: QStackedWidget | None = None
        self._file_content_refresh_token = 0
        self._pending_file_content_path = ""
        self._preferred_file_path = ""
        self._file_search_shell: QWidget | None = None
        self._file_search_panel: QWidget | None = None
        self._file_search_toggle: QToolButton | None = None
        self._file_search_input: QLineEdit | None = None
        self._file_search_prev_btn: QToolButton | None = None
        self._file_search_next_btn: QToolButton | None = None
        self._file_search_matches: list[tuple[int, int]] = []
        self._file_search_index = -1
        self._file_search_expanded = False
        self._file_search_anim: QPropertyAnimation | None = None
        self._file_search_variants: dict[str, dict[str, QWidget]] = {}
        self._file_search_mode = "document"
        self._file_tag_search_matches: list[QFrame] = []
        self._file_tag_search_index = -1
        self._files_mode_opacity_effect: QGraphicsOpacityEffect | None = None
        self._files_mode_transition_out: QPropertyAnimation | None = None
        self._files_mode_transition_in: QPropertyAnimation | None = None
        self._files_mode_transition_running = False
        self._files_loading_mode_index = 0
        self._button_interactions: list[ButtonInteractionFilter] = []
        self._scroll_fade_overlays: list[ScrollFadeOverlay] = []
        self._smooth_scroll_helpers: list[SmoothScrollController] = []
        self._active_emoji_popup: QWidget | None = None

        self._icons_dir = self.context.paths.ui_assets_dir / "icons"
        self._service_icons_dir = self.context.paths.ui_assets_dir / "service_icons"
        self._icon_cache: dict[str, QIcon] = {}
        self._service_icon_cache: dict[str, QPixmap] = {}
        self._service_check_cache: dict[str, QPixmap] = {}
        self._service_cards_by_id: dict[str, list[ServiceCardFrame]] = {}
        self._nav_items = [
            NavItem("home", "home.svg", self._t("Главная", "Dashboard")),
            NavItem("services", "services.svg", self._t("Сервисы", "Services")),
            NavItem("components", "components.svg", self._t("Компоненты", "Components")),
            NavItem("mods", "mods.svg", self._t("Модификации", "Mods")),
            NavItem("settings", "settings.svg", self._t("Настройки", "Settings")),
        ]

        if isinstance(startup_snapshot, dict):
            self._seed_startup_snapshot(startup_snapshot)

        self.setFixedSize(860, 520)
        self.setWindowTitle("Zapret-Zen")
        self.setWindowIcon(self._runtime_window_icon())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, False)
        self._build_ui()
        self._attach_button_animations_recursive(self.centralWidget())
        self._setup_tray()
        self._prepare_onboarding_services_stage()
        QTimer.singleShot(0, self._prepare_onboarding_services_surface)
        self._ensure_local_runtime_snapshot()
        QTimer.singleShot(0, self.refresh_dashboard)
        QTimer.singleShot(0, self._sync_power_aura_geometry)
        QTimer.singleShot(0, self._prepare_onboarding_services_surface)
        if self._startup_show_onboarding:
            self._set_onboarding_visible(True)
        self._apply_theme()
        if not self._launch_hidden and not self._startup_show_onboarding:
            QTimer.singleShot(0, self._prewarm_quick_onboarding_surface)
            QTimer.singleShot(650, self._cache_quick_onboarding_entry_snapshot)
        self._sync_window_icon()
        if self.context.backend is not None:
            self._connect_backend_signals(self.context.backend)
        if not self._launch_hidden:
            QTimer.singleShot(0, lambda: _bring_widget_to_front(self))

    def _t(self, ru: str, en: str) -> str:
        return ru if self.context.settings.get().language == "ru" else en

    def _connect_backend_signals(self, backend) -> None:
        try:
            backend.task_finished.connect(self._on_backend_task_finished)
            backend.task_failed.connect(self._on_backend_task_failed)
            backend.task_progress.connect(self._on_backend_task_progress)
        except Exception:
            pass

    def attach_backend_client(self, backend) -> None:
        self.context.backend = backend
        self._backend_attached = True
        self._connect_backend_signals(backend)
        self._ensure_local_runtime_snapshot()
        self.refresh_dashboard()
        self._sync_power_aura_geometry()
        if not self._startup_snapshot_ready:
            QTimer.singleShot(0, lambda: self._submit_backend_task("load_startup_snapshot", action_id="__startup_snapshot__"))

    def _themed_icon_color(self, filename: str) -> QColor | None:
        if filename not in {"power.svg", "share.svg", "trash.svg", "search.svg", "refresh.svg", "external.svg", "vpn.svg", "vpn.png"}:
            return None
        theme = self.context.settings.get().theme
        if is_light_theme(theme):
            return QColor("#2d3c57")
        return QColor("#f3f7ff")

    def _compose_icon_slot_pixmap(
        self,
        pixmap: QPixmap,
        slot_size: QSize,
        fill_ratio: float = 1.0,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        device_ratio: float | None = None,
    ) -> QPixmap:
        if pixmap.isNull() or not slot_size.isValid():
            return pixmap
        dpr = max(1.0, float(device_ratio if device_ratio is not None else pixmap.devicePixelRatio()))
        logical_width = float(slot_size.width())
        logical_height = float(slot_size.height())
        physical_width = max(1, int(round(logical_width * dpr)))
        physical_height = max(1, int(round(logical_height * dpr)))
        canvas = QPixmap(physical_width, physical_height)
        canvas.fill(Qt.GlobalColor.transparent)
        canvas.setDevicePixelRatio(dpr)
        source_size = pixmap.deviceIndependentSize() if hasattr(pixmap, "deviceIndependentSize") else QSizeF(
            float(pixmap.width()) / max(1.0, float(pixmap.devicePixelRatio())),
            float(pixmap.height()) / max(1.0, float(pixmap.devicePixelRatio())),
        )
        target_width = float(source_size.width())
        target_height = float(source_size.height())
        max_box_width = logical_width * max(0.0, fill_ratio)
        max_box_height = logical_height * max(0.0, fill_ratio)
        if target_width > 0.0 and target_height > 0.0 and max_box_width > 0.0 and max_box_height > 0.0:
            scale = min(max_box_width / target_width, max_box_height / target_height, 1.0)
            target_width *= scale
            target_height *= scale
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if hasattr(QPainter.RenderHint, "LosslessImageRendering"):
            painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
        painter.drawPixmap(
            QRectF(
                (logical_width - target_width) / 2.0 + offset_x,
                (logical_height - target_height) / 2.0 + offset_y,
                target_width,
                target_height,
            ),
            pixmap,
            QRectF(0, 0, pixmap.width(), pixmap.height()),
        )
        painter.end()
        return canvas

    def _load_trimmed_icon_pixmap(self, icon_path: Path, size: int) -> QPixmap:
        pixmap = QPixmap()
        physical_px = max(64, int(round(size * 3)))
        if icon_path.exists():
            if icon_path.suffix.lower() == ".svg":
                renderer = QSvgRenderer(str(icon_path))
                if renderer.isValid():
                    image = QImage(physical_px, physical_px, QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(image)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                    if hasattr(QPainter.RenderHint, "LosslessImageRendering"):
                        painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
                    renderer.render(painter, QRectF(0, 0, physical_px, physical_px))
                    painter.end()
                    image = self._trim_transparent_bounds(image, padding=max(2, physical_px // 12))
                    pixmap = QPixmap.fromImage(image)
            if pixmap.isNull():
                image = QImage(str(icon_path))
                if not image.isNull():
                    image = self._trim_transparent_bounds(image, padding=max(2, physical_px // 12))
                    pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            pixmap = QIcon(str(icon_path)).pixmap(size, size)
        return pixmap

    def _build_tinted_icon(
        self,
        icon_path: Path,
        color: QColor,
        *,
        fill_ratio: float = 1.0,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
    ) -> QIcon:
        base = QIcon(str(icon_path))
        icon = QIcon()
        for size in (14, 16, 18, 20, 24, 26, 32):
            source = self._load_trimmed_icon_pixmap(icon_path, size)
            if source.isNull():
                continue
            pixmap = self._compose_icon_slot_pixmap(
                source,
                QSize(size, size),
                fill_ratio,
                offset_x,
                offset_y,
                device_ratio=self._service_icon_device_ratio(),
            )
            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), color)
            painter.end()
            icon.addPixmap(pixmap)
        return icon if not icon.isNull() else base

    def _icon(self, filename: str) -> QIcon:
        tint = self._themed_icon_color(filename)
        cache_key = filename if tint is None else f"{filename}|{tint.name(QColor.NameFormat.HexArgb)}"
        cached = self._icon_cache.get(cache_key)
        if cached is not None:
            return cached
        icon_path = self._icons_dir / filename
        icon = self._build_tinted_icon(icon_path, tint) if tint is not None else QIcon(str(icon_path))
        self._icon_cache[cache_key] = icon
        return icon

    def _vpn_icon_name(self) -> str:
        return "vpn.png" if (self._icons_dir / "vpn.png").exists() else "vpn.svg"

    def _component_defs(self) -> dict[str, ComponentDefinition]:
        if self._component_defs_cache:
            return dict(self._component_defs_cache)
        return {}

    def _seed_startup_snapshot(self, payload: dict[str, object]) -> None:
        self._update_runtime_snapshot_from_payload(payload)
        self._update_mods_cache_from_payload(payload)
        self._update_general_options_from_payload(payload)
        self._page_payload_cache["components"] = {
            "components": payload.get("components", []),
            "states": payload.get("states", []),
            "general_options": payload.get("general_options", []),
            "peshk0v_vpn": payload.get("peshk0v_vpn", {}),
        }
        if "index" in payload or "installed" in payload:
            self._page_payload_cache["mods"] = {
                "index": payload.get("index", []),
                "installed": payload.get("installed", []),
            }
        self._startup_snapshot_ready = "components" in payload or "states" in payload

    def _should_show_onboarding(self) -> bool:
        if self._launch_hidden:
            return False
        return (not self._onboarding_seen()) and bool(self._sorted_general_options())

    def _ensure_widget_opacity_ready(self, widget: QWidget | None) -> None:
        if widget is None:
            return
        effect = getattr(widget, "_opacity_effect", None)
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(1.0)
            widget.setGraphicsEffect(effect)
            widget._opacity_effect = effect  # type: ignore[attr-defined]

    def _schedule_onboarding_services_prewarm(self) -> None:
        if self._onboarding_services_prewarm_done or self._onboarding_services_prewarm_scheduled:
            return
        queue: list[tuple[str, str, int, bool] | tuple[str, int]] = []
        for preset in SERVICE_PRESETS:
            queue.append(("icon", preset.id, 34, False))
            queue.append(("icon", preset.id, 34, True))
        queue.append(("check", 10))
        self._onboarding_services_prewarm_queue = queue
        self._onboarding_services_prewarm_scheduled = True
        QTimer.singleShot(0, self._run_onboarding_services_prewarm_batch)

    def _prewarm_quick_onboarding_surface(self) -> None:
        if self._onboarding_quick_prewarm_done or self._launch_hidden or self._startup_show_onboarding:
            return
        if self._onboarding_widget is None:
            return
        self._onboarding_quick_prewarm_done = True
        previous_updates = self.updatesEnabled()
        self._onboarding_prewarming = True
        try:
            self.setUpdatesEnabled(False)
            self._onboarding_quick_restart = True
            self._set_onboarding_visible(True)
            self._jump_onboarding_to_services_stage(lightweight=True)
            self._set_onboarding_visible(False)
        finally:
            self._onboarding_prewarming = False
            self._onboarding_quick_restart = False
            self.setUpdatesEnabled(previous_updates)

    def _run_onboarding_services_prewarm_batch(self) -> None:
        if not self._onboarding_services_prewarm_queue:
            self._onboarding_services_prewarm_scheduled = False
            self._onboarding_services_prewarm_done = True
            return
        batch_size = 2
        processed = 0
        preset_map = {preset.id: preset for preset in SERVICE_PRESETS}
        while self._onboarding_services_prewarm_queue and processed < batch_size:
            item = self._onboarding_services_prewarm_queue.pop(0)
            kind = item[0]
            if kind == "icon":
                _, preset_id, size, selected = item
                preset = preset_map.get(preset_id)
                if preset is not None:
                    self._service_icon_pixmap(preset, int(size), selected=bool(selected))
            elif kind == "check":
                _, size = item
                self._service_check_pixmap(int(size))
            processed += 1
        QTimer.singleShot(8, self._run_onboarding_services_prewarm_batch)

    def _prepare_onboarding_services_surface(self) -> bool:
        """Готовит карточки сервисов до показа быстрого онбординга."""
        if self._onboarding_services_grid is None or self._onboarding_services_panel is None:
            return False
        self._prepare_onboarding_services_stage()
        self.refresh_services()
        self._update_service_selection_summary()
        for widget in (
            self._onboarding_services_panel,
            self._onboarding_services_scroll,
            self._onboarding_services_grid,
        ):
            if widget is None:
                continue
            widget.ensurePolished()
            layout = widget.layout()
            if layout is not None:
                try:
                    layout.activate()
                except Exception:
                    pass
            widget.updateGeometry()
        self._relayout_onboarding_content()
        self._onboarding_services_surface_ready = bool(getattr(self._onboarding_services_grid, "_cards", []))
        return self._onboarding_services_surface_ready

    def _prepare_onboarding_services_stage(self) -> None:
        for widget in (
            self._onboarding_intro_panel,
            self._onboarding_services_stage_panel,
            self._onboarding_running_stage_panel,
            self._onboarding_result_stage_panel,
        ):
            self._ensure_widget_opacity_ready(widget)
        if self._onboarding_services_panel is not None:
            self._onboarding_services_panel.ensurePolished()
        if self._onboarding_services_scroll is not None:
            self._onboarding_services_scroll.ensurePolished()
        if self._onboarding_services_grid is not None:
            self._onboarding_services_grid.ensurePolished()
        if self._onboarding_service_action_btn is not None:
            self._onboarding_service_action_btn.set_theme(self.context.settings.get().theme)
            self._onboarding_service_action_btn.set_force_light(True)
            self._onboarding_service_action_btn.set_selection_state(
                len(self._selected_service_ids()),
                self._onboarding_services_minimum,
                text=self._t("Продолжить", "Continue"),
            )

    def _onboarding_seen_marker_path(self) -> Path:
        return self.context.paths.data_dir / ".services_onboarding_seen_v2"

    def _legacy_onboarding_seen_marker_path(self) -> Path:
        return self.context.paths.data_dir / ".onboarding_seen"

    def _onboarding_seen(self) -> bool:
        try:
            return self._onboarding_seen_marker_path().exists()
        except Exception:
            return False

    def _legacy_onboarding_seen(self) -> bool:
        try:
            if self._legacy_onboarding_seen_marker_path().exists():
                return True
        except Exception:
            pass
        return False

    def _mark_onboarding_seen(self) -> None:
        try:
            self._onboarding_seen_marker_path().write_text("1\n", encoding="utf-8")
        except Exception:
            pass
        self._onboarding_manual_restart = False

    def _restart_onboarding_from_settings(self) -> None:
        self._onboarding_manual_restart = True
        self._set_onboarding_visible(True)
        self._apply_onboarding_style()
        self._relayout_onboarding_content()
        QTimer.singleShot(0, lambda: self._sync_onboarding_back_button_visibility(force=True))

    def _cache_quick_onboarding_entry_snapshot(self) -> None:
        if self._launch_hidden or self._onboarding_active or self._onboarding_entry_overlay is not None:
            return
        if self.isMinimized() or not self.isVisible() or self.width() <= 0 or self.height() <= 0:
            return
        pixmap = self.grab()
        if pixmap.isNull():
            return
        self._quick_onboarding_entry_pixmap = pixmap
        self._quick_onboarding_entry_pixmap_size = QSize(self.size())

    def _restart_onboarding_from_dashboard(self) -> None:
        if (
            self._onboarding_widget is None
            or self._onboarding_transition_busy
            or self._onboarding_entry_overlay is not None
        ):
            return
        self._onboarding_manual_restart = True
        self._onboarding_quick_restart = True
        self._prepare_onboarding_services_surface()
        overlay = QLabel(self)
        overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        cached = self._quick_onboarding_entry_pixmap
        if cached is not None and not cached.isNull() and self._quick_onboarding_entry_pixmap_size == self.size():
            overlay.setPixmap(cached)
        else:
            overlay.setPixmap(self.grab())
        overlay.setGeometry(self.rect())
        overlay.show()
        overlay.raise_()
        self._onboarding_entry_overlay = overlay
        QTimer.singleShot(0, lambda: self._prepare_quick_onboarding_entry(overlay))
        QTimer.singleShot(0, lambda: self._sync_onboarding_back_button_visibility(force=True))

    def _show_quick_onboarding_surface(self) -> None:
        self._onboarding_active = True
        self._onboarding_transition_busy = False
        self._onboarding_transition_token += 1
        self._clear_onboarding_intro_transition_overlay()
        self._apply_onboarding_quick_chrome(self.context.settings.get().theme, True)
        if self._onboarding_widget is not None:
            self._onboarding_widget.setVisible(True)
        if self._pages_shell is not None:
            self._pages_shell.setVisible(False)
        if self._page_transition_overlay is not None:
            self._page_transition_overlay.hide()
            self._page_transition_overlay.clear_transition()
        self._page_transition_running = False
        self._page_transition_started_at = 0.0
        self._page_transition_target = self.pages.currentIndex() if hasattr(self, "pages") else -1
        if self._content_surface_layout is not None:
            self._content_surface_layout.setContentsMargins(0, 0, 0, 0)
            self._content_surface_layout.setSpacing(0)
        for widget in (self._sidebar_widget, self._notifications_btn):
            if widget is not None:
                widget.setVisible(False)
        if self._onboarding_service_action_btn is not None:
            self._position_onboarding_service_action()
            self._onboarding_service_action_btn.raise_()
        if self._onboarding_back_btn is not None:
            self._onboarding_back_btn.move(18, 16)
            self._onboarding_back_btn.raise_()

    def _prepare_quick_onboarding_entry(self, overlay: QLabel) -> None:
        if overlay is not self._onboarding_entry_overlay:
            return
        if not self._prepare_onboarding_services_surface():
            QTimer.singleShot(16, lambda: self._prepare_quick_onboarding_entry(overlay))
            return
        self._show_quick_onboarding_surface()
        if self._onboarding_stage == "services" and self._onboarding_services_stage_panel is not None:
            self._onboarding_services_stage_panel.show()
            self._finish_show_onboarding_services_stage()
        else:
            self._jump_onboarding_to_services_stage(lightweight=True)
        self._prepare_onboarding_services_surface()
        effect = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(effect)
        effect.setOpacity(1.0)
        animation = QPropertyAnimation(effect, b"opacity", overlay)
        animation.setDuration(280)
        animation.setStartValue(1.0)
        animation.setEndValue(0.0)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def _finish() -> None:
            if overlay is self._onboarding_entry_overlay:
                self._onboarding_entry_overlay = None
            overlay.hide()
            overlay.deleteLater()

        animation.finished.connect(_finish)
        overlay._onboarding_entry_animation = animation  # type: ignore[attr-defined]
        animation.start()

    def _cancel_quick_onboarding(self) -> None:
        if not (self._onboarding_quick_restart or self._onboarding_manual_restart) or self._onboarding_running:
            return
        self._onboarding_quick_restart = False
        self._onboarding_manual_restart = False
        if self._onboarding_back_btn is not None:
            self._onboarding_back_btn.hide()
        self._fade_out_onboarding_to_app()

    def _jump_onboarding_to_services_stage(self, *, lightweight: bool = False) -> None:
        self._onboarding_transition_busy = False
        self._onboarding_transition_token += 1
        self._onboarding_stage = "services"
        self._sync_onboarding_background_stage(animated=not lightweight)
        self._prepare_onboarding_services_stage()
        if self._onboarding_stage_layout is not None and self._onboarding_services_stage_panel is not None:
            self._onboarding_stage_layout.setCurrentWidget(self._onboarding_services_stage_panel)
        for panel in (
            self._onboarding_intro_panel,
            self._onboarding_running_stage_panel,
            self._onboarding_result_stage_panel,
        ):
            if panel is not None:
                panel.hide()
        if self._onboarding_services_stage_panel is not None:
            self._reset_widget_opacity(self._onboarding_services_stage_panel)
            self._onboarding_services_stage_panel.show()
        if self._onboarding_back_btn is not None:
            show_back = self._onboarding_quick_restart or self._onboarding_manual_restart
            self._onboarding_back_btn.setVisible(show_back)
            if show_back:
                self._onboarding_back_btn.raise_()
        self._finish_show_onboarding_services_stage()
        self._prepare_onboarding_services_surface()
        if not lightweight:
            self._apply_onboarding_style()
            self._relayout_onboarding_content()

    def _component_states(self) -> dict[str, ComponentState]:
        if self._component_states_cache:
            return dict(self._component_states_cache)
        return {}

    def _ensure_local_runtime_snapshot(self) -> None:
        try:
            if not self._component_defs_cache:
                self._component_defs_cache = {item.id: item for item in self.context.processes.list_components()}
        except Exception:
            pass
        try:
            if not self._component_states_cache:
                self._component_states_cache = {item.component_id: item for item in self.context.processes.list_states()}
        except Exception:
            pass
        if self._component_defs_cache or self._component_states_cache:
            self._startup_snapshot_ready = True

    def _prime_runtime_snapshot_cache(self) -> None:
        self._component_defs_cache = {}
        self._component_states_cache = {}

    def _update_runtime_snapshot_from_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        component_items = payload.get("components")
        if isinstance(component_items, list):
            snapshot: dict[str, ComponentDefinition] = {}
            for item in component_items:
                if isinstance(item, dict) and item.get("id"):
                    try:
                        snapshot[str(item["id"])] = ComponentDefinition(**item)
                    except Exception:
                        continue
            if snapshot:
                self._component_defs_cache = snapshot
        state_items = payload.get("states")
        if isinstance(state_items, list):
            snapshot_states: dict[str, ComponentState] = {}
            for item in state_items:
                if isinstance(item, dict) and item.get("component_id"):
                    try:
                        snapshot_states[str(item["component_id"])] = ComponentState(**item)
                    except Exception:
                        continue
            if snapshot_states:
                self._component_states_cache = snapshot_states

    def _update_mods_cache_from_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        raw_index = payload.get("index")
        raw_installed = payload.get("installed")
        if isinstance(raw_index, list):
            self._mods_index_cache = list(raw_index)
        if isinstance(raw_installed, dict):
            self._mods_installed_cache = {str(key): value for key, value in raw_installed.items()}
        elif isinstance(raw_installed, list):
            snapshot: dict[str, object] = {}
            for item in raw_installed:
                item_id = str(getattr(item, "id", "") or (item.get("id", "") if isinstance(item, dict) else ""))
                if item_id:
                    snapshot[item_id] = item
            self._mods_installed_cache = snapshot

    def _update_general_options_from_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        raw_options = payload.get("general_options")
        if isinstance(raw_options, list):
            normalized = [item for item in raw_options if isinstance(item, dict) and item.get("id")]
            self._general_options_cache = normalized
    def showEvent(self, event: QEvent) -> None:
        super().showEvent(event)
        self._sync_window_icon()
        _disable_native_window_rounding(self)
        self._sync_nav_highlight(animated=self._nav_highlight_initialized)
        if not self._nav_highlight_initialized:
            self._nav_highlight_initialized = True
        if self._skip_next_show_fade:
            self._skip_next_show_fade = False
            self.setWindowOpacity(1.0)
        else:
            self._animate_window_fade(showing=True)
        self._schedule_post_show_sync()
        if not self._initial_show_completed:
            self._initial_show_completed = True
            self._schedule_startup_refresh()
        if self._skip_next_show_focus:
            self._skip_next_show_focus = False
            return
        QTimer.singleShot(0, lambda: _bring_widget_to_front(self))

    def _schedule_post_show_sync(self) -> None:
        def _sync() -> None:
            self._sync_power_aura_geometry()
            self._sync_nav_highlight(animated=self._nav_highlight_initialized)
            if hasattr(self, "pages") and self.pages.currentIndex() == 1:
                self.refresh_services()
            elif hasattr(self, "pages") and self.pages.currentIndex() == 2:
                self._sync_component_card_layout()
            elif hasattr(self, "pages") and self.pages.currentIndex() == 3:
                self._sync_mod_card_layout()

        QTimer.singleShot(0, _sync)
        QTimer.singleShot(120, _sync)

    def _schedule_startup_refresh(self) -> None:
        if self._startup_deferred_refresh_scheduled or self._launch_hidden:
            return
        self._startup_deferred_refresh_scheduled = True

        def _refresh_current() -> None:
            if not self._backend_attached:
                QTimer.singleShot(250, _refresh_current)
                return
            current_index = self.pages.currentIndex() if hasattr(self, "pages") else 0
            section_map = {0: "dashboard", 1: "services", 2: "components", 3: "mods", 4: "settings"}
            current = section_map.get(current_index, "dashboard")
            self._mark_dirty(current, "tray")

        def _refresh_rest() -> None:
            if not self._backend_attached:
                QTimer.singleShot(300, _refresh_rest)
                return
            self._mark_dirty("dashboard", "services", "components", "mods", "files", "logs", "tray")

        QTimer.singleShot(900, _refresh_current)
        QTimer.singleShot(1800, _refresh_rest)
        QTimer.singleShot(2600, self._prime_cached_dialogs)
        if not self._onboarding_active:
            QTimer.singleShot(3600, self._maybe_run_first_general_autotest)
        QTimer.singleShot(4800, self._check_updates_on_start)

    def _runtime_window_icon(self) -> QIcon:
        png_path = self._icons_dir / "app.png"
        if png_path.exists():
            image = QImage(str(png_path))
            if not image.isNull():
                source = QPixmap.fromImage(image)
                if not source.isNull():
                    icon = QIcon()
                    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
                        icon.addPixmap(
                            source.scaled(
                                size,
                                size,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                        )
                    if not icon.isNull():
                        return icon
        return self._icon("app.ico")

    def _sync_window_icon(self) -> None:
        icon = self._runtime_window_icon()
        self.setWindowIcon(icon)
        app = QCoreApplication.instance()
        if app is not None and hasattr(app, "setWindowIcon"):
            try:
                app.setWindowIcon(icon)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _app_title_pixmap(self, size: int) -> QPixmap:
        png_path = self._icons_dir / "app.png"
        if png_path.exists():
            image = QImage(str(png_path))
            if not image.isNull():
                pixmap = QPixmap.fromImage(image)
                if not pixmap.isNull():
                    app = QApplication.instance()
                    dpr = 1.0
                    try:
                        if app is not None and app.primaryScreen() is not None:
                            dpr = max(1.0, float(app.primaryScreen().devicePixelRatio()))
                    except Exception:
                        dpr = 1.0
                    target_px = max(size, int(round(size * dpr)))
                    scaled = pixmap.scaled(
                        target_px,
                        target_px,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    scaled.setDevicePixelRatio(dpr)
                    return scaled
        ico_path = self._icons_dir / "app.ico"
        if ico_path.exists():
            pixmap = QPixmap(str(ico_path))
            if not pixmap.isNull():
                return pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return self._runtime_window_icon().pixmap(size, size)

    def _prepare_page_geometry_for_index(self, index: int) -> None:
        if not hasattr(self, "pages"):
            return
        page = self.pages.widget(index)
        if isinstance(page, QWidget):
            page.resize(self.pages.size())
            if page.layout() is not None:
                page.layout().activate()
        if index == 0:
            self._sync_power_aura_geometry()
        elif index == 1:
            self.refresh_services()
        elif index == 2:
            self._sync_component_card_layout()
        elif index == 3:
            self._sync_mod_card_layout()


    def _build_ui(self) -> None:
        shell = QWidget()
        shell.setObjectName("WindowShell")
        root = QVBoxLayout(shell)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(0)

        frame = OnboardingFrame()
        frame.setObjectName("RootFrame")
        root_frame = QVBoxLayout(frame)
        root_frame.setContentsMargins(0, 0, 0, 0)
        root_frame.setSpacing(0)

        title_bar = self._build_title_bar()
        root_frame.addWidget(title_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root_frame.addLayout(body)

        sidebar = self._build_sidebar()
        self._sidebar_widget = sidebar
        body.addWidget(sidebar)
        body.addWidget(self._build_content(), 1)

        root.addWidget(frame)
        self.setCentralWidget(shell)
        self._build_loading_overlay(shell)

    def _build_loading_overlay(self, parent: QWidget) -> None:
        overlay = QFrame(parent)
        overlay.setObjectName("LoadingOverlay")
        overlay.hide()
        layout = QVBoxLayout(overlay)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addStretch(1)
        card = QFrame()
        card.setObjectName("LoadingCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(26, 24, 26, 24)
        card_layout.setSpacing(10)
        icon = QLabel()
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(self._app_title_pixmap(58))
        self._loading_overlay_title = QLabel(self._t("Запуск Zapret-Zen", "Launching Zapret-Zen"))
        self._loading_overlay_title.setProperty("class", "title")
        self._loading_overlay_label = QLabel(self._t("Загрузка...", "Loading..."))
        self._loading_overlay_label.setProperty("class", "muted")
        self._loading_overlay_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_overlay_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setProperty("class", "loadingLogo")
        card_layout.addWidget(icon)
        card_layout.addWidget(self._loading_overlay_title)
        card_layout.addWidget(self._loading_overlay_label)
        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        self._loading_overlay = overlay
        self._reposition_loading_overlay()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_loading_overlay()
        self._reposition_page_transition_overlay()
        self._position_onboarding_service_action()
        self._apply_content_surface_mask()
        self._relayout_onboarding_content()
        self._sync_power_aura_geometry()
        if hasattr(self, "pages") and self.pages.currentIndex() == 1:
            QTimer.singleShot(0, self.refresh_services)
        elif hasattr(self, "pages") and self.pages.currentIndex() == 2:
            QTimer.singleShot(0, lambda: self._sync_component_card_layout())
        elif hasattr(self, "pages") and self.pages.currentIndex() == 3:
            QTimer.singleShot(0, self._sync_mod_card_layout)
        if self._file_mode_stack is not None:
            if self._file_mode_stack.currentIndex() == 0:
                QTimer.singleShot(0, self._sync_files_home_layout)
            elif self._file_mode_stack.currentIndex() == 1:
                QTimer.singleShot(0, self._sync_file_tag_canvas_geometry)

    def _relayout_onboarding_content(self) -> None:
        if self._onboarding_widget is None:
            return
        page_width = max(1, self._onboarding_widget.width())
        page_height = max(1, self._onboarding_widget.height())
        content_width = max(620, min(930, page_width - 38))
        if self._onboarding_intro_desc_label is not None:
            intro_desc_width = max(420, min(720, content_width - 150))
            self._onboarding_intro_desc_label.setFixedWidth(intro_desc_width)
            intro_fm = self._onboarding_intro_desc_label.fontMetrics()
            intro_rect = intro_fm.boundingRect(0, 0, intro_desc_width, 0, int(Qt.TextFlag.TextWordWrap), self._onboarding_intro_desc_label.text())
            self._onboarding_intro_desc_label.setMinimumHeight(max(34, intro_rect.height() + 4))
        if self._onboarding_desc_label is not None:
            desc_width = max(420, min(720, content_width - 150))
            self._onboarding_desc_label.setFixedWidth(desc_width)
            fm = self._onboarding_desc_label.fontMetrics()
            rect = fm.boundingRect(0, 0, desc_width, 0, int(Qt.TextFlag.TextWordWrap), self._onboarding_desc_label.text())
            self._onboarding_desc_label.setMinimumHeight(max(34, rect.height() + 4))
        if self._onboarding_running_desc_label is not None:
            running_desc_width = max(420, min(720, content_width - 150))
            self._onboarding_running_desc_label.setFixedWidth(running_desc_width)
            running_fm = self._onboarding_running_desc_label.fontMetrics()
            running_rect = running_fm.boundingRect(0, 0, running_desc_width, 0, int(Qt.TextFlag.TextWordWrap), self._onboarding_running_desc_label.text())
            self._onboarding_running_desc_label.setMinimumHeight(max(34, running_rect.height() + 4))
        if self._onboarding_result_desc_label is not None:
            result_desc_width = max(420, min(720, content_width - 150))
            self._onboarding_result_desc_label.setFixedWidth(result_desc_width)
            result_fm = self._onboarding_result_desc_label.fontMetrics()
            result_rect = result_fm.boundingRect(0, 0, result_desc_width, 0, int(Qt.TextFlag.TextWordWrap), self._onboarding_result_desc_label.text())
            self._onboarding_result_desc_label.setMinimumHeight(max(34, result_rect.height() + 4))
        if self._onboarding_result_card is not None:
            self._onboarding_result_card.setFixedWidth(content_width)
        if self._onboarding_result_actions_widget is not None:
            self._onboarding_result_actions_widget.setFixedWidth(content_width)
        if self._onboarding_progress_bar is not None:
            progress_width = max(360, min(560, content_width - 80))
            self._onboarding_progress_bar.setFixedWidth(progress_width)
        if self._onboarding_services_panel is not None:
            scroll_height = max(268, min(356, page_height - 136))
            self._onboarding_services_panel.setFixedSize(content_width, scroll_height)
            if self._onboarding_services_scroll is not None:
                self._onboarding_services_scroll.setFixedHeight(scroll_height)
        if self._onboarding_back_btn is not None:
            self._onboarding_back_btn.move(18, 16)
            if self._onboarding_back_btn.isVisible():
                self._onboarding_back_btn.raise_()
        self._position_onboarding_service_action()

    def _position_onboarding_service_action(self) -> None:
        button = self._onboarding_service_action_btn
        host = self._onboarding_services_stage_panel
        if button is None or host is None:
            return
        x = max(0, int((host.width() - button.width()) / 2))
        y = max(0, host.height() - button.height() - 18)
        button.move(x, y)
        if button.isVisible():
            button.raise_()

    def _format_onboarding_general_line(self, text: str) -> str:
        if self._onboarding_found_label is None:
            return text
        fm = self._onboarding_found_label.fontMetrics()
        max_width = max(340, self._onboarding_found_label.width() - 8)
        if max_width <= 0:
            max_width = 620
        return fm.elidedText(text, Qt.TextElideMode.ElideRight, max_width)

    def _apply_content_surface_mask(self) -> None:
        if self._content_surface is None:
            return
        self._content_surface.clearMask()

    def _sync_power_aura_geometry(self) -> None:
        if self.power_aura is None or not hasattr(self, "_power_aura_host") or not hasattr(self, "power_button"):
            return
        aura_host = getattr(self, "_power_aura_host", None)
        power_button = getattr(self, "power_button", None)
        if aura_host is None or power_button is None:
            return
        self.power_aura.setGeometry(aura_host.rect())
        button_top_left = power_button.mapTo(aura_host, QPoint(0, 0))
        button_center = QPointF(
            float(button_top_left.x()) + power_button.width() / 2.0,
            float(button_top_left.y()) + power_button.height() / 2.0,
        )
        self.power_aura.set_center_point(button_center)

    def _reposition_loading_overlay(self) -> None:
        overlay = getattr(self, "_loading_overlay", None)
        central = self.centralWidget()
        if overlay is None or central is None:
            return
        overlay.setGeometry(0, 0, central.width(), central.height())

    def _reposition_page_transition_overlay(self) -> None:
        overlay = self._page_transition_overlay
        surface = self._content_surface
        pages_shell = self._pages_shell
        if overlay is None or surface is None:
            return
        if pages_shell is not None:
            overlay.setGeometry(pages_shell.geometry())
            overlay.set_content_rect(overlay.rect())
        else:
            overlay.setGeometry(surface.rect())
            overlay.set_content_rect(overlay.rect())

    def _show_loading_overlay(self, text: str | None = None, *, title: str | None = None, context: str = "general") -> None:
        self._loading_overlay_context = context

    def _hide_loading_overlay(self) -> None:
        self._loading_overlay_context = ""

    def _build_title_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TitleBar")
        bar.setFixedHeight(52)
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(8)

        title = QLabel("Zapret-Zen")
        title.setProperty("class", "title")
        row.addWidget(title)

        author = QLabel("by peshk0v")
        author.setProperty("class", "muted")
        author.setContentsMargins(0, 2, 0, 0)
        row.addWidget(author)
        row.addStretch(1)

        notifications_btn = NotificationBellButton()
        notifications_btn.setProperty("class", "action")
        notifications_btn.setIcon(self._icon("bell.svg"))
        notifications_btn.setIconSize(QSize(16, 16))
        notifications_btn.setToolTip(self._t("Уведомления", "Notifications"))
        notifications_btn.clicked.connect(self._toggle_notifications_popup)
        self._notifications_btn = notifications_btn
        row.addWidget(notifications_btn)
        self._refresh_notifications_badge()

        min_btn = self._window_btn("", "min")
        self._min_btn = min_btn
        min_btn.setIconSize(QSize(15, 15))
        min_btn.clicked.connect(self._minimize_window_native)
        self._attach_button_animations(min_btn)
        close_btn = self._window_btn("", "close")
        self._close_btn = close_btn
        close_btn.setIconSize(QSize(15, 15))
        close_btn.clicked.connect(self.close)
        self._attach_button_animations(close_btn)
        row.addWidget(min_btn)
        row.addWidget(close_btn)
        return bar

    def _window_btn(self, text: str, role: str) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setProperty("class", "window")
        btn.setProperty("role", role)
        return btn

    def _window_hwnd(self) -> int:
        try:
            return int(self.winId())
        except Exception:
            return 0

    def _set_windows_taskbar_progress(self, value: int, *, state: int | None = None) -> None:
        hwnd = self._window_hwnd()
        if not hwnd:
            return
        self._taskbar_progress_active = True
        progress_state = WindowsTaskbarIntegration.TBPF_NORMAL if state is None else int(state)
        value = max(0, min(100, int(value)))
        self._windows_taskbar.set_progress_value(hwnd, value, 100)
        self._windows_taskbar.set_progress_state(hwnd, progress_state)

    def _clear_windows_taskbar_progress(self) -> None:
        self._taskbar_progress_active = False
        hwnd = self._window_hwnd()
        if not hwnd:
            return
        if self._taskbar_important_attention:
            self._windows_taskbar.set_progress_value(hwnd, 100, 100)
            self._windows_taskbar.set_progress_state(hwnd, WindowsTaskbarIntegration.TBPF_PAUSED)
        else:
            self._windows_taskbar.set_progress_state(hwnd, WindowsTaskbarIntegration.TBPF_NOPROGRESS)

    def _has_unread_important_notifications(self) -> bool:
        try:
            return any((not item.read) and str(item.level).lower() == "error" for item in self.context.notifications.list())
        except Exception:
            return False

    def _refresh_windows_notification_attention(self) -> None:
        important = self._has_unread_important_notifications()
        self._taskbar_important_attention = important
        hwnd = self._window_hwnd()
        if not hwnd:
            return
        if important:
            if not self._taskbar_progress_active:
                self._windows_taskbar.set_progress_value(hwnd, 100, 100)
                self._windows_taskbar.set_progress_state(hwnd, WindowsTaskbarIntegration.TBPF_PAUSED)
            self._windows_taskbar.flash_attention(hwnd)
        else:
            self._windows_taskbar.clear_flash(hwnd)
            if not self._taskbar_progress_active:
                self._windows_taskbar.set_progress_state(hwnd, WindowsTaskbarIntegration.TBPF_NOPROGRESS)

    def _request_windows_attention(self) -> None:
        hwnd = self._window_hwnd()
        if hwnd:
            self._windows_taskbar.flash_attention(hwnd)

    def _refresh_notifications_badge(self) -> None:
        if self._notifications_btn is None:
            return
        try:
            self._notifications_btn.set_unread(self.context.notifications.unread_count() > 0)
        except Exception:
            self._notifications_btn.set_unread(False)
        self._refresh_windows_notification_attention()

    def _add_notification(
        self,
        level: str,
        title: str,
        message: str,
        *,
        source: str = "app",
        details: dict[str, object] | None = None,
    ) -> None:
        try:
            self.context.notifications.add(level, title, message, source=source, details=details or {})
        except Exception:
            return
        self._refresh_notifications_badge()
        if str(level).lower() == "error":
            self._request_windows_attention()

    def _notify_component_errors_from_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        raw_states = payload.get("states", [])
        items = raw_states.values() if isinstance(raw_states, dict) else raw_states
        if not isinstance(items, list) and not hasattr(items, "__iter__"):
            return
        for raw in items:
            if isinstance(raw, ComponentState):
                state = raw
            elif isinstance(raw, dict):
                try:
                    state = ComponentState(**raw)
                except Exception:
                    continue
            else:
                continue
            if state.status != "error" or not state.last_error:
                continue
            translated_error = self._translate_component_error(state.last_error)
            signature = (state.component_id, state.last_error.strip())
            if signature in self._notified_component_errors:
                continue
            self._notified_component_errors.add(signature)
            self._add_notification(
                "error",
                self._t("Компонент не запустился", "Component failed to start"),
                f"{self._component_display_name(state.component_id)}: {translated_error}",
                source=state.component_id,
                details={"dedupe_key": f"component-error:{state.component_id}:{state.last_error.strip()}"},
            )

    def _notify_telegram_proxy_status_from_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        info = payload.get("telegram_proxy")
        if not isinstance(info, dict) or not bool(info.get("missing")):
            return
        self._add_notification(
            "warning",
            self._t("Telegram Desktop не найден", "Telegram Desktop was not found"),
            self._t(
                "Telegram Desktop не найден на компьютере. Откройте раздел компонентов, скачайте Telegram Desktop и после установки нажмите «Подключить к Telegram».",
                "Telegram Desktop was not found on this PC. Open Components, download Telegram Desktop, and after installation press 'Connect to Telegram'.",
            ),
            source="tg-ws-proxy",
            details={"dedupe_key": "telegram-desktop-missing"},
        )

    def _notify_zapret_restart_from_payload(self, payload: object) -> None:
        if not isinstance(payload, dict) or not bool(payload.get("zapret_restarted")):
            return
        self._add_notification(
            "success",
            self._t("Zapret перезапущен", "Zapret restarted"),
            self._t(
                "Zapret пересобран и запущен заново с вашими текущими настройками.",
                "Zapret was rebuilt and started again with your current settings.",
            ),
            source="zapret",
            details={"dedupe_key": "zapret-reconfigured-restarted"},
        )

    def _component_display_name(self, component_id: str) -> str:
        return {"zapret": "Zapret", "tg-ws-proxy": "TG WS Proxy", "peshk0v-vpn": "peshk0v vpn"}.get(component_id, component_id)

    def _translate_component_error(self, error: str) -> str:
        text = str(error or "").strip()
        lowered = text.lower()
        if "windivert: error opening filter" in lowered and "parameter is incorrect" in lowered:
            return self._t(
                "WinDivert не смог открыть фильтр: один из параметров фильтра некорректен.",
                "WinDivert could not open the filter: one of the filter parameters is invalid.",
            )
        if "winws did not start" in lowered:
            return self._t(
                "winws не запустился. Запустите приложение от имени администратора и проверьте исключения антивируса для WinDivert.",
                "winws did not start. Run the app as Administrator and check antivirus exclusions for WinDivert.",
            )
        if "administrator rights are required" in lowered:
            return self._t(
                "Для winws/WinDivert нужны права администратора.",
                "Administrator rights are required for winws/WinDivert.",
            )
        if "failed to parse winws command" in lowered:
            return self._t(
                "Не удалось разобрать команду winws из выбранной конфигурации.",
                "Failed to parse the winws command from the selected configuration.",
            )
        if "no general script found" in lowered:
            return self._t("Конфигурация Zapret не найдена.", "Zapret configuration was not found.")
        return text

    def _show_notifications_popup(self) -> None:
        if self._notifications_btn is None:
            return
        if time.monotonic() - self._notifications_popup_last_closed_at < 0.18:
            return
        if self._notifications_popup is not None and self._notifications_popup.isVisible():
            self._notifications_popup.close()
            return

        flags = Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        popup = QWidget(self, flags)
        popup.setObjectName("NotificationsWindow")
        popup.setFixedSize(326, 248)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        popup.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        popup.setAutoFillBackground(False)
        popup.setStyleSheet(self.styleSheet())

        root_layout = QVBoxLayout(popup)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        panel = QFrame(popup)
        panel.setObjectName("NotificationsPopup")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root_layout.addWidget(panel)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel(self._t("Уведомления", "Notifications"))
        title.setProperty("class", "title")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setObjectName("NotificationsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        canvas = QWidget()
        canvas.setObjectName("NotificationsCanvas")
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 68)
        canvas_layout.setSpacing(8)

        entries = list(reversed(self.context.notifications.list()))
        if not entries:
            empty = QLabel(self._t("Пока всё тихо. Ошибок и важных событий нет.", "Quiet for now. No errors or important events."))
            empty.setProperty("class", "muted")
            empty.setWordWrap(True)
            canvas_layout.addWidget(empty)
        else:
            for entry in entries:
                canvas_layout.addWidget(self._build_notification_item(entry))
        canvas_layout.addStretch(1)
        scroll.setWidget(canvas)
        theme = self.context.settings.get().theme
        scroll.setStyleSheet(
            "QScrollArea#NotificationsScroll, "
            "QScrollArea#NotificationsScroll > QWidget#qt_scrollarea_viewport, "
            "QWidget#NotificationsCanvas { background: transparent; border: none; }"
        )
        popup_fade = ScrollFadeOverlay(scroll)
        popup_fade.set_surface_color(_dialog_surface_color(theme))
        popup._scroll_fade_overlay = popup_fade  # type: ignore[attr-defined]
        self._register_smooth_scroll(scroll)
        layout.addWidget(scroll, 1)

        self._notifications_popup = popup
        popup.destroyed.connect(self._on_notifications_popup_destroyed)
        pos = self._notifications_btn.mapToGlobal(QPoint(0, self._notifications_btn.height() + 8))
        popup.move(pos.x() - popup.width() + self._notifications_btn.width(), pos.y())
        popup.show()
        _disable_native_window_rounding(popup)
        try:
            self.context.notifications.mark_all_read()
        except Exception:
            pass
        self._refresh_notifications_badge()

    def _toggle_notifications_popup(self) -> None:
        if self._notifications_popup is not None and self._notifications_popup.isVisible():
            self._notifications_popup.close()
            return
        self._show_notifications_popup()

    def _on_notifications_popup_destroyed(self, *_args: object) -> None:
        self._notifications_popup = None
        self._notifications_popup_last_closed_at = time.monotonic()

    def _build_notification_item(self, entry: NotificationEntry) -> QWidget:
        card = QFrame()
        card.setProperty("class", "notificationCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(10, 9, 10, 9)
        row.setSpacing(9)

        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot_color = {
            "error": "#ef4444",
            "warning": "#f59e0b",
            "success": "#22c55e",
            "info": "#60a5fa",
        }.get(entry.level, "#60a5fa")
        dot.setStyleSheet(f"background: {dot_color}; border-radius: 4px;")
        row.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(4)
        header = QLabel(entry.title)
        header.setProperty("class", "title")
        header.setWordWrap(True)
        body = QLabel(entry.message or self._t("Без подробностей", "No details"))
        body.setProperty("class", "muted")
        body.setWordWrap(True)
        meta = QLabel(self._format_notification_time(entry.created_at))
        meta.setProperty("class", "muted")
        text_col.addWidget(header)
        text_col.addWidget(body)
        text_col.addWidget(meta)
        row.addLayout(text_col, 1)
        return card

    def _format_notification_time(self, value: str) -> str:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt.strftime("%d.%m %H:%M")
        except Exception:
            return str(value)[:16]

    def _build_tools_menu(self) -> QMenu:
        menu = QMenu(self)
        run_tests = QAction(self._t("Подобрать конфигурацию", "Find best configuration"), self)
        run_tests.triggered.connect(self._run_general_tests_popup)
        menu.addAction(run_tests)

        tune_settings = QAction(self._t("Подобрать настройки", "Find best settings"), self)
        tune_settings.triggered.connect(self._run_settings_diagnostics_popup)
        menu.addAction(tune_settings)

        run_diag = QAction(self._t("Запустить диагностику", "Run diagnostics"), self)
        run_diag.triggered.connect(self._run_diagnostics_popup)
        menu.addAction(run_diag)

        check_updates = QAction(self._t("Проверить обновления", "Check updates"), self)
        check_updates.triggered.connect(self._check_updates_popup)
        menu.addAction(check_updates)

        rebuild = QAction(self._t("Пересобрать merged", "Rebuild merged"), self)
        rebuild.triggered.connect(self._rebuild_runtime)
        menu.addAction(rebuild)

        refresh = QAction(self._t("Обновить всё", "Refresh all"), self)
        refresh.triggered.connect(self.refresh_all)
        menu.addAction(refresh)
        return menu

    def _build_sidebar(self) -> QWidget:
        side = SidebarPanel()
        side.setObjectName("Sidebar")
        side.setFixedWidth(78)
        col = QVBoxLayout(side)
        col.setContentsMargins(12, 12, 12, 12)
        col.setSpacing(10)

        for idx, item in enumerate(self._nav_items):
            btn = AnimatedNavButton()
            btn.setProperty("class", "nav")
            if item.key == "files":
                btn.setProperty("baseIconDx", 1.0)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setIcon(self._icon(item.icon_file))
            icon_size = 26
            if item.key in {"services", "mods"}:
                icon_size = 28
            elif item.key == "components":
                icon_size = 24
            btn.setIconSize(QSize(icon_size, icon_size))
            btn.setToolTip(item.tooltip)
            btn.clicked.connect(lambda _=False, index=idx: self._switch_page(index))
            self._attach_button_animations(btn)
            self._nav_buttons.append(btn)
            col.addWidget(btn, 0, Qt.AlignmentFlag.AlignHCenter)

        col.addStretch(1)
        github_btn = GitHubSidebarButton()
        github_btn.setIcon(self._icon("github.svg"))
        github_btn.setIconSize(QSize(22, 22))
        github_btn.set_button_theme(self.context.settings.get().theme)
        github_btn.setToolTip(self._t("Открыть репозиторий", "Open repository"))
        github_btn.setFixedSize(44, 44)
        github_btn.setStyleSheet("QToolButton { background: transparent; border: none; }")
        github_btn.clicked.connect(lambda: webbrowser.open("https://github.com/peshk0v/Zapret-Zen/"))
        self._github_sidebar_btn = github_btn
        col.addWidget(github_btn, 0, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        if self._nav_buttons:
            self._nav_buttons[0].setChecked(True)
        QTimer.singleShot(0, lambda: self._sync_nav_highlight(animated=False))
        return side

    def _build_content(self) -> QWidget:
        pane = QFrame()
        pane.setObjectName("Content")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        body = QFrame()
        body.setObjectName("ContentSurface")
        self._content_surface = body
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 12, 12, 0)
        body_layout.setSpacing(8)
        self._content_surface_layout = body_layout

        pages_shell = QWidget()
        pages_shell.setObjectName("PagesShell")
        pages_shell.setProperty("class", "pageCanvas")
        pages_shell.setAutoFillBackground(False)
        pages_shell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        pages_shell_layout = QVBoxLayout(pages_shell)
        pages_shell_layout.setContentsMargins(0, 0, 0, 0)
        pages_shell_layout.setSpacing(0)
        self._pages_shell = pages_shell

        pages_host = QWidget()
        pages_host.setObjectName("PagesHost")
        pages_host.setProperty("class", "pageCanvas")
        pages_host.setAutoFillBackground(False)
        pages_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._pages_host = pages_host
        pages_host_layout = QVBoxLayout(pages_host)
        pages_host_layout.setContentsMargins(0, 0, 0, 0)
        pages_host_layout.setSpacing(0)

        self.pages = QStackedWidget()
        self.pages.setObjectName("PagesStack")
        self.pages.setProperty("class", "pageCanvas")
        self.pages.setAutoFillBackground(False)
        self.pages.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.pages.addWidget(self._build_dashboard_page())
        self.pages.addWidget(self._build_services_page())
        self.pages.addWidget(self._build_components_page())
        self.pages.addWidget(self._build_mods_page())
        self.pages.addWidget(self._build_settings_page())
        self._page_blur_effect = None
        pages_host_layout.addWidget(self.pages)
        pages_shell_layout.addWidget(pages_host)
        self._page_opacity_effect = QGraphicsOpacityEffect(pages_host)
        self._page_opacity_effect.setOpacity(1.0)
        pages_host.setGraphicsEffect(self._page_opacity_effect)
        overlay = PageTransitionOverlay(body)
        overlay.setObjectName("PageTransitionOverlay")
        self._page_transition_overlay = overlay
        self._page_transition_overlay_label = None
        self._page_transition_overlay_next_label = None
        self._page_transition_overlay_blur_effect = None
        self._page_transition_overlay_opacity_effect = None
        self._page_transition_overlay_next_opacity_effect = None
        self._reposition_page_transition_overlay()
        onboarding = self._build_onboarding_page()
        self._onboarding_widget = onboarding
        onboarding.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        onboarding.hide()
        body_layout.addWidget(onboarding, 1)
        body_layout.addWidget(pages_shell)
        layout.addWidget(body, 1)
        return pane

    def _build_onboarding_page(self) -> QWidget:
        page = OnboardingPageWidget()
        page.setObjectName("OnboardingPage")
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        back_btn = QToolButton(page)
        back_btn.setObjectName("OnboardingBackButton")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setIcon(self._icon("arrow_left.svg"))
        back_btn.setIconSize(QSize(17, 17))
        back_btn.setFixedSize(32, 32)
        back_btn.setToolTip(self._t("Назад", "Back"))
        back_btn.clicked.connect(self._cancel_quick_onboarding)
        back_btn.hide()
        self._onboarding_back_btn = back_btn
        self._sync_onboarding_back_button_style()

        wrap = QWidget()
        wrap.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._onboarding_wrap_widget = wrap
        wrap_layout = QVBoxLayout(wrap)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.setSpacing(0)

        stage_host = QWidget()
        stage_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        stage_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._onboarding_stage_host = stage_host
        stage_layout = QStackedLayout(stage_host)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(0)
        stage_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._onboarding_stage_layout = stage_layout
        wrap_layout.addWidget(stage_host, 1)

        intro_panel = QWidget(stage_host)
        intro_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        intro_shell = QVBoxLayout(intro_panel)
        intro_shell.setContentsMargins(0, 0, 0, 0)
        intro_shell.setSpacing(0)
        intro_shell.addStretch(1)
        intro_center = QWidget(intro_panel)
        intro_layout = QVBoxLayout(intro_center)
        intro_layout.setContentsMargins(0, 0, 0, 0)
        intro_layout.setSpacing(10)
        intro_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        intro_shell.addWidget(intro_center, 0, Qt.AlignmentFlag.AlignHCenter)
        intro_shell.addStretch(1)
        self._onboarding_intro_panel = intro_panel
        stage_layout.addWidget(intro_panel)

        intro_title = QLabel(self._t("Добро пожаловать", "Welcome"))
        intro_title.setProperty("class", "title")
        intro_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._onboarding_intro_title_label = intro_title
        intro_icon = QLabel()
        intro_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        intro_icon.setFixedSize(160, 160)
        icon_path = self._icons_dir / "app_large.png"
        if icon_path.exists():
            pixmap = QPixmap(str(icon_path))
            if not pixmap.isNull():
                scaled = pixmap.scaled(160, 160, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                intro_icon.setPixmap(scaled)
        self._onboarding_intro_icon = intro_icon
        intro_layout.addWidget(intro_icon, 0, Qt.AlignmentFlag.AlignCenter)

        intro_layout.addWidget(intro_title, 0, Qt.AlignmentFlag.AlignCenter)

        intro_desc = QLabel(
            self._t(
                "Zapret-Zen - это ваш главный помощник в обходе сервисов. Хотите приступить к первичной настройке?",
                "Zapret-Zen is your ultimate assistant for bypassing restrictions. Ready to run the initial setup?",
            )
        )
        intro_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        intro_desc.setWordWrap(True)
        intro_desc.setMinimumWidth(440)
        intro_desc.setMaximumWidth(680)
        intro_desc.setMinimumHeight(0)
        intro_desc.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._onboarding_intro_desc_label = intro_desc
        intro_layout.addWidget(intro_desc, 0, Qt.AlignmentFlag.AlignCenter)

        actions = QWidget()
        actions_layout = QVBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(12)

        primary = QPushButton(self._t("Пройти первичную настройку", "Run initial setup"))
        primary.setMinimumWidth(320)
        primary.setMinimumHeight(44)
        primary.clicked.connect(self._handle_onboarding_primary_action)
        self._onboarding_primary_btn = primary
        actions_layout.addWidget(primary, 0, Qt.AlignmentFlag.AlignCenter)

        secondary = QPushButton(self._t("Пропустить", "Skip"))
        secondary.setFlat(True)
        secondary.setCursor(Qt.CursorShape.PointingHandCursor)
        secondary.setStyleSheet("background: transparent; border: none; padding: 6px 10px; color: rgba(255,255,255,0.62);")
        secondary.clicked.connect(self._handle_onboarding_secondary_action)
        self._onboarding_secondary_btn = secondary
        actions_layout.addWidget(secondary, 0, Qt.AlignmentFlag.AlignCenter)
        secondary.hide()
        self._onboarding_actions_widget = actions
        intro_layout.addWidget(actions, 0, Qt.AlignmentFlag.AlignCenter)

        services_stage_panel = QWidget(stage_host)
        services_stage_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        services_shell = QVBoxLayout(services_stage_panel)
        services_shell.setContentsMargins(0, 0, 0, 0)
        services_shell.setSpacing(0)
        services_shell.addStretch(1)
        services_center = QWidget(services_stage_panel)
        services_layout = QVBoxLayout(services_center)
        services_layout.setContentsMargins(0, 0, 0, 0)
        services_layout.setSpacing(10)
        services_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        services_shell.addWidget(services_center, 0, Qt.AlignmentFlag.AlignHCenter)
        services_shell.addStretch(1)
        services_stage_panel.hide()
        self._onboarding_services_stage_panel = services_stage_panel
        stage_layout.addWidget(services_stage_panel)

        title = QLabel(self._t("Выберите сервисы", "Choose services"))
        title.setProperty("class", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._onboarding_title_label = title
        services_layout.addWidget(title, 0, Qt.AlignmentFlag.AlignCenter)

        desc = QLabel("")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        desc.setMinimumWidth(440)
        desc.setMaximumWidth(680)
        desc.setMinimumHeight(0)
        desc.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._onboarding_desc_label = desc
        services_layout.addWidget(desc, 0, Qt.AlignmentFlag.AlignCenter)

        services_panel = self._build_onboarding_services_panel()
        self._onboarding_services_panel = services_panel
        services_layout.addWidget(services_panel, 0, Qt.AlignmentFlag.AlignCenter)

        running_stage_panel = QWidget(stage_host)
        running_stage_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        running_shell = QVBoxLayout(running_stage_panel)
        running_shell.setContentsMargins(0, 0, 0, 0)
        running_shell.setSpacing(0)
        running_shell.addStretch(1)
        running_center = QWidget(running_stage_panel)
        running_layout = QVBoxLayout(running_center)
        running_layout.setContentsMargins(0, 0, 0, 0)
        running_layout.setSpacing(8)
        running_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        running_shell.addWidget(running_center, 0, Qt.AlignmentFlag.AlignHCenter)
        running_shell.addStretch(1)
        running_stage_panel.hide()
        self._onboarding_running_stage_panel = running_stage_panel
        stage_layout.addWidget(running_stage_panel)

        running_title = QLabel(self._t("Подбор конфигурации", "Selecting configuration"))
        running_title.setProperty("class", "title")
        running_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._onboarding_running_title_label = running_title
        running_layout.addWidget(running_title, 0, Qt.AlignmentFlag.AlignCenter)

        running_desc = QLabel("")
        running_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        running_desc.setWordWrap(True)
        running_desc.setMinimumWidth(440)
        running_desc.setMaximumWidth(680)
        running_desc.setMinimumHeight(0)
        running_desc.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._onboarding_running_desc_label = running_desc
        running_layout.addWidget(running_desc, 0, Qt.AlignmentFlag.AlignCenter)

        progress_label = QLabel("")
        progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_label.setProperty("class", "muted")
        self._onboarding_progress_label = progress_label
        running_layout.addWidget(progress_label, 0, Qt.AlignmentFlag.AlignCenter)

        progress = RoundedProgressBar()
        progress.setRange(0, 100)
        progress.setValue(0)
        progress.setMinimumWidth(360)
        progress.setMaximumWidth(520)
        progress.setMinimumHeight(12)
        progress.setMaximumHeight(12)
        progress.setTextVisible(False)
        self._onboarding_progress_bar = progress
        running_layout.addWidget(progress, 0, Qt.AlignmentFlag.AlignCenter)

        progress_counter = QLabel("")
        progress_counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_counter.setProperty("class", "muted")
        self._onboarding_progress_counter_label = progress_counter
        running_layout.addWidget(progress_counter, 0, Qt.AlignmentFlag.AlignCenter)

        result_stage_panel = QWidget(stage_host)
        result_stage_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        result_shell = QVBoxLayout(result_stage_panel)
        result_shell.setContentsMargins(0, 0, 0, 0)
        result_shell.setSpacing(0)
        result_shell.addStretch(1)
        result_center = QWidget(result_stage_panel)
        result_layout = QVBoxLayout(result_center)
        result_layout.setContentsMargins(0, 0, 0, 0)
        result_layout.setSpacing(10)
        result_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result_shell.addWidget(result_center, 0, Qt.AlignmentFlag.AlignHCenter)
        result_shell.addStretch(1)
        result_stage_panel.hide()
        self._onboarding_result_stage_panel = result_stage_panel
        stage_layout.addWidget(result_stage_panel)

        result_title = QLabel(self._t("Настройка завершена", "Setup complete"))
        result_title.setProperty("class", "title")
        result_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._onboarding_result_title_label = result_title
        result_layout.addWidget(result_title, 0, Qt.AlignmentFlag.AlignCenter)

        result_desc = QLabel("")
        result_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result_desc.setWordWrap(True)
        result_desc.setMinimumWidth(440)
        result_desc.setMaximumWidth(680)
        result_desc.setMinimumHeight(0)
        result_desc.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._onboarding_result_desc_label = result_desc
        result_layout.addWidget(result_desc, 0, Qt.AlignmentFlag.AlignCenter)

        result_card = QWidget()
        result_card.setMinimumWidth(520)
        result_card.setMaximumWidth(838)
        result_inner = QVBoxLayout(result_card)
        result_inner.setContentsMargins(0, 0, 0, 0)
        result_inner.setSpacing(8)
        result_body = QLabel(self._t("Найдена подходящая конфигурация.", "A suitable configuration has been found."))
        result_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result_body.setWordWrap(False)
        result_body.setMinimumHeight(28)
        result_general = QLabel("")
        result_general.setAlignment(Qt.AlignmentFlag.AlignCenter)
        result_general.setWordWrap(False)
        result_general.setMinimumHeight(28)
        result_inner.addWidget(result_body)
        result_inner.addWidget(result_general)
        self._onboarding_result_card = result_card
        self._onboarding_result_label = result_body
        self._onboarding_found_label = result_general
        result_layout.addWidget(result_card, 0, Qt.AlignmentFlag.AlignCenter)

        result_actions = QWidget()
        result_actions_layout = QVBoxLayout(result_actions)
        result_actions_layout.setContentsMargins(0, 0, 0, 0)
        result_actions_layout.setSpacing(12)
        result_primary = QPushButton(self._t("Далее", "Next"))
        result_primary.setMinimumWidth(320)
        result_primary.setMinimumHeight(44)
        result_primary.clicked.connect(self._handle_onboarding_primary_action)
        result_actions_layout.addWidget(result_primary, 0, Qt.AlignmentFlag.AlignCenter)
        self._onboarding_result_actions_widget = result_actions
        self._onboarding_result_primary_btn = result_primary
        result_layout.addWidget(result_actions, 0, Qt.AlignmentFlag.AlignCenter)

        root.addWidget(wrap, 1)
        service_action = OnboardingServiceProgressButton(services_stage_panel)
        service_action.setText(self._t("Продолжить", "Continue"))
        service_action.clicked.connect(self._handle_onboarding_primary_action)
        service_action.hide()
        self._onboarding_service_action_btn = service_action
        return page

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setProperty("class", "card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 6, 14, 14)
        layout.setSpacing(10)
        return card, layout

    def _build_dashboard_page(self) -> QWidget:
        page = QWidget()
        page.setProperty("class", "pageRoot")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 12)
        root.setSpacing(4)

        top, top_layout = self._card()
        top_layout.setContentsMargins(14, 14, 14, 14)
        title = QLabel(self._t("Быстрый доступ", "Quick Access"))
        title.setObjectName("DashboardTitle")
        title.setProperty("class", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        title.setContentsMargins(0, 0, 0, 0)
        title.setMaximumHeight(22)
        self._dashboard_title_label = title
        top_layout.addWidget(title)

        # настройка general перенесена в компоненты
        general_label = QLabel(self._t("Конфигурация", "General"))
        self.general_combo = ClickSelectComboBox()
        self.general_combo.currentIndexChanged.connect(self._on_general_selected)
        self.general_combo.hide()

        power_block = QWidget()
        power_block.setObjectName("DashboardPowerBlock")
        power_block_layout = QVBoxLayout(power_block)
        power_block_layout.setContentsMargins(0, 0, 0, 0)
        power_block_layout.setSpacing(8)

        self.power_aura = PowerAuraWidget(top)
        self.power_aura.set_power_theme(self.context.settings.get().theme)
        self.power_aura.lower()

        power_stage = QWidget(power_block)
        power_stage.setFixedSize(224, 188)
        power_stage.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        power_stage.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        power_stage.setStyleSheet("background: transparent;")
        power_stage_layout = QVBoxLayout(power_stage)
        power_stage_layout.setContentsMargins(0, 28, 0, 28)
        power_stage_layout.setSpacing(0)
        power_stage_layout.addStretch(1)
        power_button_row = QHBoxLayout()
        power_button_row.setContentsMargins(0, 0, 0, 0)
        power_button_row.setSpacing(0)
        power_button_row.addStretch(1)
        self.power_button = AnimatedPowerButton(power_stage)
        self.power_button.setProperty("class", "power")
        self.power_button.setIcon(self._icon("power.svg"))
        self.power_button.setIconSize(QSize(42, 42))
        self.power_button.setFixedSize(132, 132)
        self.power_button.setEnabled(False)
        self.power_button.clicked.connect(self._toggle_master_runtime)
        self._attach_button_animations(self.power_button)
        self.power_button.set_power_theme(self.context.settings.get().theme)
        power_button_row.addWidget(self.power_button, 0, Qt.AlignmentFlag.AlignHCenter)
        power_button_row.addStretch(1)
        power_stage_layout.addLayout(power_button_row)
        power_stage_layout.addStretch(1)

        self.power_caption = QWidget()
        self.power_caption.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.power_caption.setStyleSheet("background: transparent;")
        self.power_caption.setFixedWidth(power_stage.width())
        caption_layout = QHBoxLayout(self.power_caption)
        caption_layout.setContentsMargins(0, 0, 0, 0)
        caption_layout.setSpacing(8)
        self.power_reconfigure_btn = QToolButton(self.power_caption)
        self.power_reconfigure_btn.setObjectName("PowerReconfigureButton")
        self.power_reconfigure_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.power_reconfigure_btn.setIcon(
            self._build_tinted_icon(
                self._icons_dir / "rerun.svg",
                self._themed_icon_color("refresh.svg") or QColor("#f3f7ff"),
                fill_ratio=0.82,
                offset_x=-0.8,
                offset_y=1.8,
            )
        )
        self.power_reconfigure_btn.setIconSize(QSize(15, 15))
        self.power_reconfigure_btn.setFixedSize(30, 30)
        self.power_reconfigure_btn.setToolTip(self._t("Подобрать настройки", "Find settings"))
        self.power_reconfigure_btn.clicked.connect(self._restart_onboarding_from_dashboard)
        self._attach_button_animations(self.power_reconfigure_btn)
        self._sync_power_reconfigure_button_style()
        self.power_caption_text = QLabel("OFF")
        self.power_caption_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.power_caption_text.setProperty("class", "title")
        self.power_caption_text.setObjectName("PowerStatusPill")
        self.power_caption_text.setMinimumWidth(96)
        self.power_caption_text.setFixedHeight(30)
        self.power_caption_dots = None
        self._power_caption_dots_blur = None
        self._power_caption_dots_opacity = None
        caption_layout.addStretch(1)
        caption_layout.addWidget(self.power_caption_text, 0, Qt.AlignmentFlag.AlignCenter)
        caption_layout.addWidget(self.power_reconfigure_btn, 0, Qt.AlignmentFlag.AlignCenter)
        caption_layout.addStretch(1)
        power_block_layout.addWidget(power_stage, 0, Qt.AlignmentFlag.AlignHCenter)
        power_block_layout.addWidget(self.power_caption, 0, Qt.AlignmentFlag.AlignHCenter)
        self._power_aura_host = top
        self._power_block = power_block
        self._power_stage = power_stage
        QTimer.singleShot(0, self._sync_power_aura_geometry)

        top_layout.addStretch(1)
        top_layout.addWidget(power_block, 0, Qt.AlignmentFlag.AlignHCenter)
        top_layout.addStretch(1)

        badges_row = QHBoxLayout()
        badges_row.setSpacing(10)
        for key, icon_name, title_text in [
            ("app", "status_ok.svg", self._t("Приложение", "App")),
            ("zapret", "status_warn.svg", "Zapret"),
            ("tg", "status_warn.svg", "TG Proxy"),
            ("mods", "status_mod.svg", "Mods"),
        ]:
            badge = self._build_status_badge(key, icon_name, title_text)
            badges_row.addWidget(badge)
        badges_row.setStretch(0, 1)
        badges_row.setStretch(1, 1)
        badges_row.setStretch(2, 1)
        badges_row.setStretch(3, 1)
        top_layout.addLayout(badges_row)
        root.addWidget(top)

        return page

    def _build_status_badge(self, key: str, icon_name: str, title: str) -> QWidget:
        card, layout = self._card()
        card.setMinimumHeight(96)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        head = QHBoxLayout()
        icon_label = QLabel()
        icon_label.setPixmap(self._icon(icon_name).pixmap(18, 18))
        text_label = QLabel(title)
        text_label.setProperty("class", "muted")
        head.addWidget(icon_label)
        head.addWidget(text_label)
        head.addStretch(1)
        layout.addLayout(head)

        value = QLabel("...")
        value.setProperty("class", "title")
        value.setWordWrap(False)
        layout.addWidget(value)
        self._status_badges[key] = StatusBadge(key, icon_name, title, text_label, icon_label, value)
        return card

    def _build_services_page(self) -> QWidget:
        page = QWidget()
        page.setProperty("class", "pageRoot")
        root = QVBoxLayout(page)
        root.setContentsMargins(1, 0, 1, 0)
        root.setSpacing(12)

        hero, hero_layout = self._card()
        hero_layout.setContentsMargins(16, 16, 16, 16)
        hero_layout.setSpacing(10)

        title = QLabel(self._t("Выберите сервисы", "Choose services"))
        title.setProperty("class", "title")
        self._services_title_label = title
        hero_layout.addWidget(title)

        subtitle = QLabel(
            self._t(
                "Выберите приложения, сайты и сервисы, которыми вы пользуетесь.",
                "Choose the apps, sites, and services you actually use.",
            )
        )
        subtitle.setProperty("class", "muted")
        subtitle.setWordWrap(True)
        self._services_subtitle_label = subtitle
        hero_layout.addWidget(subtitle)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 2, 0, 0)
        meta_row.setSpacing(10)
        count_label = QLabel()
        count_label.setObjectName("ServicesCountChip")
        count_label.setProperty("class", "modMeta")
        self._services_count_label = count_label
        meta_row.addWidget(count_label, 0, Qt.AlignmentFlag.AlignLeft)

        hint = QLabel(
            self._t(
                "Приложение автоматически настраивает свою работу для обеспечения доступа к выбранным сервисам.",
                "The app automatically adjusts its behavior to provide access to the selected services.",
            )
        )
        hint.setProperty("class", "muted")
        hint.setWordWrap(True)
        self._services_hint_label = hint
        meta_row.addWidget(hint, 1)
        hero_layout.addLayout(meta_row)
        root.addWidget(hero)

        scroll = QScrollArea()
        scroll.setObjectName("ServicesScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        canvas = QWidget()
        canvas.setObjectName("ServicesCanvas")
        canvas.setProperty("class", "pageCanvas")
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(1, 0, 1, 14)
        canvas_layout.setSpacing(0)
        grid = ServiceGridPanel(base_columns=4, min_card_width=166, offset_pattern=(0,), horizontal_spacing=12, vertical_spacing=12)
        grid.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        grid.set_cards(self._create_service_cards(scope="main"))
        self._services_grid = grid
        canvas_layout.addWidget(grid)
        scroll.setWidget(canvas)
        self._register_scroll_fade(scroll, surface_color=_content_surface_color(self.context.settings.get().theme))
        self._register_smooth_scroll(scroll, duration=250, angle_divisor=3.0)
        self._services_scroll = scroll
        root.addWidget(scroll, 1)
        return page

    def _build_onboarding_services_panel(self) -> QWidget:
        panel = QWidget()
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        panel.hide()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        count = QLabel("")
        count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        count.setProperty("class", "muted")
        count.hide()
        self._onboarding_services_count_label = count

        scroll = QScrollArea()
        scroll.setObjectName("OnboardingServicesScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        scroll.setMinimumHeight(294)
        scroll.setMaximumHeight(364)
        canvas = QWidget()
        canvas.setObjectName("OnboardingServicesCanvas")
        canvas.setProperty("class", "pageCanvas")
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(0, 16, 0, 84)
        canvas_layout.setSpacing(0)
        search_input = QLineEdit()
        search_input.setPlaceholderText(self._t("Поиск сервисов...", "Search services..."))
        search_input.setClearButtonEnabled(True)
        search_input.setFixedWidth(320)
        search_input.setMinimumHeight(34)
        search_input.setStyleSheet(
            "QLineEdit {"
            "background: rgba(255,255,255,0.85);"
            "border: 1px solid #bfd2f0;"
            "border-radius: 10px;"
            "padding: 4px 10px;"
            "color: #111827;"
            "font-size: 10pt;"
            "}"
            "QLineEdit:focus {"
            "border: 1px solid #4f73d9;"
            "}"
        )
        self._onboarding_services_search = search_input
        canvas_layout.addWidget(search_input, 0, Qt.AlignmentFlag.AlignHCenter)

        grid = ServiceGridPanel(base_columns=5, min_card_width=150, offset_pattern=(10, 0, 10, 0, 10), horizontal_spacing=8, vertical_spacing=8)
        grid.set_cards(self._create_service_cards(scope="onboarding"))
        self._onboarding_services_grid = grid
        canvas_layout.addWidget(grid)

        search_input.textChanged.connect(lambda text: self._filter_onboarding_services(text))

        scroll.setWidget(canvas)
        fade = self._register_scroll_fade(scroll, surface_color=_chrome_surface_color(self.context.settings.get().theme))
        fade.set_fade_height(58)
        fade.set_edges(top=True, bottom=True)
        self._onboarding_services_fade = fade
        self._register_smooth_scroll(scroll, duration=260, angle_divisor=3.2)
        self._onboarding_services_scroll = scroll
        layout.addWidget(scroll, 1)
        return panel

    def _filter_onboarding_services(self, text: str) -> None:
        query = str(text or "").strip().lower()
        grid = self._onboarding_services_grid
        if grid is None:
            return
        for card in grid._cards:
            match = not query or query in card.preset.title_ru.lower() or query in card.preset.title_en.lower()
            card.setVisible(match)

    def _create_service_cards(self, *, scope: str) -> list[ServiceCardFrame]:
        cards: list[ServiceCardFrame] = []
        theme = self.context.settings.get().theme
        selected = set(self._selected_service_ids())
        for preset in SERVICE_PRESETS:
            card = ServiceCardFrame(preset)
            card.set_visual_scope(scope)
            title, description = self._service_card_texts(preset, scope=scope)
            is_selected = preset.id in selected
            card.set_texts(title, description)
            card.set_icon_pixmap(self._service_icon_pixmap(preset, 34, selected=is_selected, onboarding=scope == "onboarding"))
            card.set_check_pixmap(self._service_check_pixmap(10))
            card.set_theme(theme)
            card.set_selected(is_selected)
            card.setProperty("serviceScope", scope)
            card.toggled.connect(self._on_service_card_toggled)
            cards.append(card)
            self._service_cards_by_id.setdefault(preset.id, []).append(card)
        return cards

    def _service_card_texts(self, preset: ServicePreset, *, scope: str = "onboarding") -> tuple[str, str]:
        description_ru = preset.description_ru
        description_en = preset.description_en
        if scope == "main":
            description_ru = preset.short_description_ru or description_ru
            description_en = preset.short_description_en or description_en
        return self._t(preset.title_ru, preset.title_en), self._t(description_ru, description_en)

    def _service_title_by_id(self, service_id: str) -> str:
        preset = next((item for item in SERVICE_PRESETS if item.id == service_id), None)
        if preset is None:
            return service_id
        return self._t(preset.title_ru, preset.title_en)

    def _service_ids_from_failed_targets(self, failed_targets: list[object]) -> list[str]:
        selected = self._selected_service_ids()
        if not selected or not failed_targets:
            return []
        aliases = {
            "telegram-desktop": ("telegram",),
            "cloudflare": ("cloudflare", "1.1.1.1"),
            "discord": ("discord",),
            "youtube": ("youtube", "youtu", "googlevideo"),
            "roblox": ("roblox", "rbx"),
            "clouds": ("clouds", "cloudfront", "amazon", "aws", "bunny", "ovh", "fastly", "akamai"),
            "tiktok": ("tiktok",),
            "instagram": ("instagram",),
            "epic-games": ("epic",),
            "battle-net": ("battle", "blizzard"),
            "fortnite": ("fortnite", "epic", "unreal", "launcher", "hcaptcha"),
            "spotify": ("spotify",),
            "reddit": ("reddit",),
            "x-twitter": ("x ", "x/", "twitter"),
            "github": ("github",),
            "riot-games": ("riot",),
            "league-of-legends": ("league", "lol"),
            "figma": ("figma",),
            "netflix": ("netflix",),
            "facebook": ("facebook",),
        }
        failed_text = "\n".join(str(item).lower() for item in failed_targets)
        result: list[str] = []
        for service_id in selected:
            if service_id == "telegram-desktop":
                continue
            tokens = aliases.get(service_id, (service_id.replace("-", " "),))
            if any(token in failed_text for token in tokens):
                result.append(service_id)
        return result

    def _service_icon_pixmap(self, preset: ServicePreset, size: int, *, selected: bool, onboarding: bool = False) -> QPixmap:
        theme = self.context.settings.get().theme
        tint = QColor(preset.accent) if selected else (QColor("#7b8798") if (onboarding or is_light_theme(theme)) else QColor("#6f7a8c"))
        dpr = self._service_icon_device_ratio()
        cache_key = f"{preset.icon_file}|{size}|{dpr:.2f}|{tint.name(QColor.NameFormat.HexArgb)}"
        cached = self._service_icon_cache.get(cache_key)
        if cached is not None and not cached.isNull():
            return cached
        icon_path = self._service_icons_dir / preset.icon_file
        pixmap = QPixmap()
        physical_px = max(64, int(round(size * dpr)))
        if icon_path.exists():
            if icon_path.suffix.lower() == ".svg":
                renderer = QSvgRenderer(str(icon_path))
                if renderer.isValid():
                    image = QImage(physical_px, physical_px, QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(image)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                    renderer.render(painter, QRectF(0, 0, physical_px, physical_px))
                    painter.end()
                    image = self._trim_transparent_bounds(image, padding=max(2, physical_px // 10))
                    pixmap = QPixmap.fromImage(image)
                    pixmap.setDevicePixelRatio(dpr)
            if pixmap.isNull():
                image = QImage(str(icon_path))
                if not image.isNull():
                    image = self._trim_transparent_bounds(image, padding=max(2, physical_px // 10))
                    scaled = image.scaled(
                        physical_px,
                        physical_px,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    pixmap = QPixmap.fromImage(scaled)
                    pixmap.setDevicePixelRatio(dpr)
                else:
                    pixmap = QIcon(str(icon_path)).pixmap(QSize(physical_px, physical_px))
                    if not pixmap.isNull() and pixmap.devicePixelRatio() < dpr:
                        pixmap.setDevicePixelRatio(dpr)
        if pixmap.isNull():
            pixmap = self._fallback_service_icon_pixmap(preset, size)
        if not pixmap.isNull():
            source = QPixmap(physical_px, physical_px)
            source.fill(Qt.GlobalColor.transparent)
            source.setDevicePixelRatio(dpr)
            source_painter = QPainter(source)
            source_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            source_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            if hasattr(QPainter.RenderHint, "LosslessImageRendering"):
                source_painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
            source_size = pixmap.deviceIndependentSize() if hasattr(pixmap, "deviceIndependentSize") else QSizeF(
                float(pixmap.width()) / max(1.0, float(pixmap.devicePixelRatio())),
                float(pixmap.height()) / max(1.0, float(pixmap.devicePixelRatio())),
            )
            target_width = float(source_size.width())
            target_height = float(source_size.height())
            max_box = float(size) * 0.84
            if target_width > 0.0 and target_height > 0.0:
                scale = min(max_box / target_width, max_box / target_height, 1.0)
                target_width *= scale
                target_height *= scale
            source_painter.drawPixmap(
                QRectF((size - target_width) / 2.0, (size - target_height) / 2.0, target_width, target_height),
                pixmap,
                QRectF(0, 0, pixmap.width(), pixmap.height()),
            )
            source_painter.end()
            tinted = QPixmap(source.size())
            tinted.fill(Qt.GlobalColor.transparent)
            tinted.setDevicePixelRatio(dpr)
            painter = QPainter(tinted)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            if hasattr(QPainter.RenderHint, "LosslessImageRendering"):
                painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
            painter.drawPixmap(0, 0, source)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(tinted.rect(), tint)
            painter.end()
            pixmap = tinted
        self._service_icon_cache[cache_key] = pixmap
        return pixmap

    def _service_check_pixmap(self, size: int) -> QPixmap:
        theme = self.context.settings.get().theme
        dpr = self._service_icon_device_ratio()
        cache_key = f"{size}|{theme}|{dpr:.2f}"
        cached = self._service_check_cache.get(cache_key)
        if cached is not None and not cached.isNull():
            return cached
        icon_path = self._icons_dir / "service_check.svg"
        pixmap = QPixmap()
        if icon_path.exists():
            renderer = QSvgRenderer(str(icon_path))
            if renderer.isValid():
                physical_px = max(48, int(round(size * dpr)))
                image = QImage(physical_px, physical_px, QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(Qt.GlobalColor.transparent)
                painter = QPainter(image)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                if hasattr(QPainter.RenderHint, "LosslessImageRendering"):
                    painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering, True)
                renderer.render(painter, QRectF(0, 0, physical_px, physical_px))
                painter.end()
                image = self._trim_transparent_bounds(image, padding=max(2, physical_px // 7))
                pixmap = QPixmap.fromImage(image)
                pixmap.setDevicePixelRatio(dpr)
        self._service_check_cache[cache_key] = pixmap
        return pixmap

    def _service_icon_device_ratio(self) -> float:
        screen = self.windowHandle().screen() if self.windowHandle() is not None else QApplication.primaryScreen()
        if screen is None:
            return 2.0
        return max(2.0, min(4.0, float(screen.devicePixelRatio())))

    def _trim_transparent_bounds(self, image: QImage, *, padding: int = 0) -> QImage:
        if image.isNull():
            return image
        candidate = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        width = candidate.width()
        height = candidate.height()
        left = width
        top = height
        right = -1
        bottom = -1
        for y in range(height):
            for x in range(width):
                if candidate.pixelColor(x, y).alpha() <= 6:
                    continue
                if x < left:
                    left = x
                if y < top:
                    top = y
                if x > right:
                    right = x
                if y > bottom:
                    bottom = y
        if right < left or bottom < top:
            return candidate
        pad = max(0, int(padding))
        left = max(0, left - pad)
        top = max(0, top - pad)
        right = min(width - 1, right + pad)
        bottom = min(height - 1, bottom + pad)
        return candidate.copy(left, top, right - left + 1, bottom - top + 1)

    def _fallback_service_icon_pixmap(self, preset: ServicePreset, size: int) -> QPixmap:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(preset.accent))
        painter.drawRoundedRect(QRectF(0, 0, size, size), max(6.0, size * 0.28), max(6.0, size * 0.28))
        painter.setPen(QColor("#ffffff"))
        font = painter.font()
        font.setBold(True)
        font.setPointSizeF(max(8.0, size * 0.34))
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, (preset.title_en or preset.title_ru or "?")[0].upper())
        painter.end()
        return pixmap

    def _build_components_page(self) -> QWidget:
        page = QWidget()
        page.setProperty("class", "pageRoot")
        root = QVBoxLayout(page)
        root.setContentsMargins(1, 0, 1, 0)
        root.setSpacing(6)
        label = QLabel(self._t("Компоненты", "Components"))
        label.setProperty("class", "title")
        self._components_title_label = label
        root.addWidget(label)

        self.components_list = QListWidget()
        self.components_list.setObjectName("ComponentList")
        self.components_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.components_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.components_list.setSpacing(8)
        self.components_list.hide()
        root.addWidget(self.components_list)
        self._components_scroll = QScrollArea()
        self._components_scroll.setObjectName("ComponentsScroll")
        self._components_scroll.setWidgetResizable(True)
        self._components_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._components_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._components_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._components_cards_root = QWidget()
        self._components_cards_root.setObjectName("ComponentsCanvas")
        self._components_cards_root.setProperty("class", "pageCanvas")
        self._components_cards_layout = QGridLayout(self._components_cards_root)
        self._components_cards_layout.setContentsMargins(1, 0, 1, 12)
        self._components_cards_layout.setHorizontalSpacing(12)
        self._components_cards_layout.setVerticalSpacing(12)
        self._components_cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._components_cards_layout.setColumnStretch(0, 1)
        self._components_cards_layout.setColumnStretch(1, 1)
        self._components_scroll.setWidget(self._components_cards_root)
        self._register_scroll_fade(self._components_scroll)
        self._register_smooth_scroll(self._components_scroll)
        root.addWidget(self._components_scroll, 1)
        return page

    def _build_mods_page(self) -> QWidget:
        page = QWidget()
        page.setProperty("class", "pageRoot")
        root = QVBoxLayout(page)
        root.setContentsMargins(1, 0, 1, 0)
        root.setSpacing(12)

        hero, hero_layout = self._card()
        hero.setProperty("class", "modHero")
        hero_layout.setContentsMargins(14, 14, 14, 14)

        hero_top = QHBoxLayout()
        hero_top.setContentsMargins(0, 0, 0, 0)
        hero_top.setSpacing(10)

        title_wrap = QVBoxLayout()
        title_wrap.setContentsMargins(0, 0, 0, 0)
        title_wrap.setSpacing(4)
        label = QLabel(self._t("Модификации", "Mods"))
        label.setProperty("class", "title")
        self._mods_title_label = label
        subtitle = QLabel(
            self._t(
                "Здесь можно аккуратно подключать свои сборки, не ломая базовую конфигурацию.",
                "This is where you can attach your own packs without touching the base configuration.",
            )
        )
        subtitle.setProperty("class", "muted")
        subtitle.setWordWrap(True)
        self._mods_subtitle_label = subtitle
        title_wrap.addWidget(label)
        title_wrap.addWidget(subtitle)
        hero_top.addLayout(title_wrap, 1)

        import_btn = QPushButton(self._t("Добавить", "Add"))
        import_btn.setProperty("class", "primary")
        import_btn.setIcon(self._icon("plus.svg"))
        import_btn.setIconSize(QSize(14, 14))
        import_btn.setMinimumHeight(38)
        import_btn.clicked.connect(self._import_mod_any)
        self._attach_button_animations(import_btn)
        self._mods_add_btn = import_btn
        create_btn = QPushButton(self._t("Создать", "Create"))
        create_btn.setProperty("class", "primary")
        create_btn.setMinimumHeight(38)
        create_btn.clicked.connect(self._create_mod_dialog)
        self._attach_button_animations(create_btn)
        hero_top.addWidget(create_btn)
        hero_top.addWidget(import_btn)
        hero_layout.addLayout(hero_top)

        summary_row = QHBoxLayout()
        summary_row.setContentsMargins(0, 0, 0, 0)
        summary_row.setSpacing(10)

        self.mods_summary_chip = QLabel()
        self.mods_summary_chip.setObjectName("ModsSummaryChip")
        self.mods_summary_chip.setProperty("class", "modMeta")
        summary_row.addWidget(self.mods_summary_chip)

        self.mods_enabled_chip = QLabel()
        self.mods_enabled_chip.setObjectName("ModsEnabledChip")
        self.mods_enabled_chip.setProperty("class", "modMeta")
        summary_row.addWidget(self.mods_enabled_chip)

        self.mods_import_hint = QLabel(
            self._t(
                "Можно добавить папку, ZIP, отдельные файлы или целый GitHub-репозиторий. Приложение само заберет только совместимые файлы.",
                "You can add a folder, ZIP, selected files, or a full GitHub repository. The app will keep only compatible files.",
            )
        )
        self.mods_import_hint.setProperty("class", "modHint")
        self.mods_import_hint.setWordWrap(True)
        summary_row.addWidget(self.mods_import_hint, 1)
        hero_layout.addLayout(summary_row)
        root.addWidget(hero)

        self.mods_scroll = QScrollArea()
        self.mods_scroll.setObjectName("ModsScroll")
        self.mods_scroll.setWidgetResizable(True)
        self.mods_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.mods_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.mods_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.mods_canvas = QWidget()
        self.mods_canvas.setObjectName("ModsCanvas")
        self.mods_canvas.setProperty("class", "pageCanvas")
        self.mods_cards_layout = QVBoxLayout(self.mods_canvas)
        self.mods_cards_layout.setContentsMargins(1, 0, 1, 12)
        self.mods_cards_layout.setSpacing(12)
        self.mods_scroll.setWidget(self.mods_canvas)
        self._register_scroll_fade(self.mods_scroll)
        self._register_smooth_scroll(self.mods_scroll)
        root.addWidget(self.mods_scroll, 1)
        return page

    def _build_files_page(self) -> QWidget:
        page = QWidget()
        page.setProperty("class", "pageRoot")
        root = QVBoxLayout(page)
        root.setContentsMargins(1, 0, 1, 0)
        root.setSpacing(10)

        title = QLabel(self._t("Файлы", "Files"))
        title.setProperty("class", "title")
        self._files_title_label = title
        root.addWidget(title)

        stack = QStackedWidget()
        self._file_mode_stack = stack

        chooser_scroll = QScrollArea()
        chooser_scroll.setWidgetResizable(True)
        chooser_scroll.setFrameShape(QFrame.Shape.NoFrame)
        chooser_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        chooser_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        chooser_scroll.setProperty("class", "pageCanvas")
        chooser_host = QWidget()
        chooser_host.setProperty("class", "pageCanvas")
        chooser_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        chooser_host_layout = QVBoxLayout(chooser_host)
        chooser_host_layout.setContentsMargins(1, 0, 1, 12)
        chooser_host_layout.setSpacing(0)
        chooser_host_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        chooser, chooser_layout = self._card()
        chooser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        chooser_layout.setContentsMargins(14, 10, 14, 14)
        chooser_layout.setSpacing(8)
        self._file_home_page = chooser_scroll
        self._files_home_host = chooser_host
        self._files_home_card = chooser
        intro = QLabel(
            self._t(
                "Выберите режим: общие и исключающие доменные листы, IP-листы, IP-исключения или полноценное редактирование файлов.",
                "Choose the mode you need: include/exclude domain lists, IP lists, exclude IPs, or full file editing.",
            )
        )
        intro.setWordWrap(True)
        self._files_intro_label = intro
        chooser_layout.addWidget(intro)
        chooser_grid = QGridLayout()
        chooser_grid.setContentsMargins(0, 2, 0, 0)
        chooser_grid.setHorizontalSpacing(12)
        chooser_grid.setVerticalSpacing(12)
        chooser_layout.addLayout(chooser_grid, 1)
        file_modes = [
            (
                self._t("Домены", "Domains"),
                self._t("Добавляйте сервисы, которые нужно направить в общий список обхода.", "Add services that should be placed into the general bypass list."),
                "domains",
                "files_domains.svg",
            ),
            (
                self._t("Исключения", "Exclude domains"),
                self._t("Отдельный список доменов, которые нужно исключить из правил.", "A separate list of domains that should be excluded from rules."),
                "exclude_domains",
                "files_exclude.svg",
            ),
            (
                self._t("IP-листы", "IP lists"),
                self._t("Ручной список IP и подсетей, которые нужно добавить в основной IPSet.", "A manual list of IPs and subnets that should be added into the main IPSet."),
                "all_ips",
                "files_ip.svg",
            ),
            (
                self._t("IP-исключения", "Exclude IPs"),
                self._t("Ручной список IP и подсетей, которые нужно исключить из IPSet.", "A manual list of IPs and subnets to exclude from IPSet."),
                "ips",
                "files_exclude.svg",
            ),
            (
                "General",
                self._t("Редактировать доступные general-конфигурации Zapret.", "Edit available Zapret general configurations."),
                "generals",
                "components.svg",
            ),
            (
                "Hosts",
                self._t(
                    "Открыть локальный файл .service/hosts из встроенного Zapret.",
                    "Open the local .service/hosts file from the bundled Zapret runtime.",
                ),
                "hosts",
                "files.svg",
            ),
            (
                self._t("Редактирование файлов", "Advanced editor"),
                self._t("Открыть полноценный список файлов и текстовый редактор.", "Open the full file list and the text editor."),
                "advanced",
                "files_editor.svg",
            ),
        ]
        self._file_mode_cards = []
        for index, (label, description, kind, icon_name) in enumerate(file_modes):
            card = ClickableCard()
            card.setMinimumHeight(126)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 16, 12)
            card_layout.setSpacing(8)
            card_layout.addStretch(1)

            icon_label = QLabel()
            icon_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            icon_label.setPixmap(self._icon(icon_name).pixmap(28, 28))
            icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            card_layout.addWidget(icon_label)

            title_label = QLabel(label)
            title_label.setProperty("class", "title")
            title_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            card_layout.addWidget(title_label)

            desc_label = QLabel(description)
            desc_label.setProperty("class", "muted")
            desc_label.setWordWrap(True)
            desc_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            desc_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            card_layout.addWidget(desc_label)
            card_layout.addStretch(1)

            card.clicked.connect(lambda target=kind: self._open_files_mode(target))
            chooser_grid.addWidget(card, index // 2, index % 2)
            self._file_mode_cards.append(
                {
                    "kind": kind,
                    "title": title_label,
                    "description": desc_label,
                }
            )
        chooser_grid.setColumnStretch(0, 1)
        chooser_grid.setColumnStretch(1, 1)
        chooser_host_layout.addWidget(chooser)
        chooser_host_layout.addSpacing(10)
        reset_btn = QPushButton(self._t("Сбросить все изменения", "Reset all changes"))
        reset_btn.setProperty("class", "danger")
        reset_btn.setMinimumHeight(40)
        reset_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        reset_btn.clicked.connect(self._reset_all_file_overrides)
        self._attach_button_animations(reset_btn)
        chooser_host_layout.addWidget(reset_btn)
        chooser_host_layout.addStretch(1)
        chooser_scroll.setWidget(chooser_host)
        self._register_scroll_fade(chooser_scroll)
        self._register_smooth_scroll(chooser_scroll)
        self._files_home_scroll = chooser_scroll

        tags_page, tags_layout = self._card()
        self._file_tags_page = tags_page
        back_row = QHBoxLayout()
        back_row.setContentsMargins(0, 0, 0, 0)
        back_row.setSpacing(8)
        back_btn = QToolButton()
        back_btn.setProperty("class", "action")
        back_btn.setIcon(self._icon("back.svg"))
        back_btn.setIconSize(QSize(16, 16))
        back_btn.setToolTip(self._t("Назад", "Back"))
        back_btn.clicked.connect(lambda: self._open_files_mode("home"))
        back_row.addWidget(back_btn, 0)
        tag_title = QLabel()
        tag_title.setProperty("class", "title")
        self._file_tag_title = tag_title
        back_row.addWidget(tag_title, 0)
        back_row.addStretch(1)
        tags_layout.addLayout(back_row)
        tag_subtitle = QLabel()
        tag_subtitle.setProperty("class", "muted")
        tag_subtitle.setWordWrap(True)
        self._file_tag_subtitle = tag_subtitle
        tags_layout.addWidget(tag_subtitle)
        tag_input = QLineEdit()
        tag_input.setPlaceholderText(self._t("Введите домен или IP и нажмите Enter", "Type a domain or IP and press Enter"))
        tag_input.returnPressed.connect(self._commit_tag_input)
        tag_input.installEventFilter(self)
        self._file_tag_input = tag_input
        tags_layout.addWidget(tag_input)
        tag_scroll = QScrollArea()
        tag_scroll.setWidgetResizable(True)
        tag_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tag_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tag_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        tag_canvas = QWidget()
        tag_flow = FlowLayout(tag_canvas, margin=0, spacing=8)
        tag_canvas.setLayout(tag_flow)
        tag_scroll.setWidget(tag_canvas)
        tag_surface = _files_inner_surface_css(self.context.settings.get().theme)
        tag_scroll.setStyleSheet(
            f"QScrollArea, QScrollArea > QWidget#qt_scrollarea_viewport {{ background: {tag_surface}; border: none; }}"
        )
        tag_canvas.setStyleSheet(f"background: {tag_surface}; border: none;")
        self._register_scroll_fade(tag_scroll, surface_color=_files_inner_surface_color(self.context.settings.get().theme))
        self._register_smooth_scroll(tag_scroll)
        tag_canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tag_flow.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self._file_tag_scroll = tag_scroll
        self._file_tag_canvas = tag_canvas
        self._file_tag_flow = tag_flow
        tags_stack = QStackedWidget()
        tags_loading = QLabel(self._t("Загрузка...", "Loading..."))
        tags_loading.setProperty("class", "muted")
        tags_loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._files_tags_loading_label = tags_loading
        self._files_tags_stack = tags_stack
        tags_stack.addWidget(tags_loading)
        tags_stack.addWidget(tag_scroll)
        tags_shell = QWidget(tags_page)
        tags_shell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        tags_shell.setAutoFillBackground(False)
        tags_shell.setStyleSheet("background: transparent;")
        tags_grid = QGridLayout(tags_shell)
        tags_grid.setContentsMargins(0, 0, 0, 0)
        tags_grid.setSpacing(0)
        tags_grid.addWidget(tags_stack, 0, 0)
        tag_search_shell, tag_search_panel, tag_search_toggle, tag_search_input, tag_search_prev_btn, tag_search_next_btn = self._build_file_search_variant(
            tags_shell,
            placeholder=self._t("Найти значение", "Find value"),
        )
        tags_grid.addWidget(tag_search_shell, 0, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        tags_layout.addWidget(tags_shell, 1)
        self._register_file_search_variant(
            "tags",
            shell=tag_search_shell,
            panel=tag_search_panel,
            toggle=tag_search_toggle,
            field=tag_search_input,
            prev_btn=tag_search_prev_btn,
            next_btn=tag_search_next_btn,
        )
        advanced_btn = QPushButton(self._t("Открыть редактор файлов", "Open file editor"))
        advanced_btn.clicked.connect(lambda: self._open_files_mode("advanced"))
        tags_layout.addWidget(advanced_btn)
        tags_layout.addSpacing(12)

        advanced_page = QWidget()
        self._file_advanced_page = advanced_page
        advanced_root = QVBoxLayout(advanced_page)
        advanced_root.setContentsMargins(1, 0, 1, 12)
        advanced_root.setSpacing(12)
        advanced_back = QToolButton()
        advanced_back.setProperty("class", "action")
        advanced_back.setIcon(self._icon("back.svg"))
        advanced_back.setIconSize(QSize(16, 16))
        advanced_back.setToolTip(self._t("Назад", "Back"))
        advanced_back.clicked.connect(lambda: self._open_files_mode("home"))
        advanced_split = QHBoxLayout()
        advanced_split.setContentsMargins(0, 0, 0, 0)
        advanced_split.setSpacing(12)

        left, left_layout = self._card()
        left_title_row = QHBoxLayout()
        left_title_row.setContentsMargins(0, 0, 0, 0)
        left_title_row.setSpacing(8)
        left_title_row.addWidget(advanced_back, 0, Qt.AlignmentFlag.AlignVCenter)
        left_title = QLabel(self._t("Список файлов", "Files list"))
        left_title.setProperty("class", "title")
        left_title_row.addWidget(left_title, 0, Qt.AlignmentFlag.AlignVCenter)
        left_title_row.addStretch(1)
        left_layout.addLayout(left_title_row)
        self.files_list = QListWidget()
        self.files_list.setObjectName("FilesList")
        self.files_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.files_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.files_list.setSpacing(8)
        self.files_list.currentItemChanged.connect(self._load_selected_file)
        list_stack = QStackedWidget()
        list_loading = QLabel(self._t("Загрузка файлов...", "Loading files..."))
        list_loading.setProperty("class", "muted")
        list_loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._files_list_loading_label = list_loading
        self._files_list_stack = list_stack
        list_stack.addWidget(list_loading)
        list_stack.addWidget(self.files_list)
        left_layout.addWidget(list_stack)
        advanced_split.addWidget(left, 1)

        right, right_layout = self._card()
        right_title = QLabel(self._t("Редактор", "Editor"))
        right_title.setProperty("class", "title")
        self._editor_title_label = right_title
        right_layout.addWidget(right_title)
        self.file_path_label = QLabel(self._t("Выберите файл", "Select a file"))
        self.file_path_label.setProperty("class", "muted")
        path_row = QHBoxLayout()
        path_row.addWidget(self.file_path_label, 1)
        self.rename_file_btn = QToolButton()
        self.rename_file_btn.setProperty("class", "action")
        self.rename_file_btn.setIcon(self._icon("edit.svg"))
        self.rename_file_btn.setToolTip(self._t("Переименовать выбранный файл", "Rename selected file"))
        self.rename_file_btn.clicked.connect(self._rename_current_file)
        self._attach_button_animations(self.rename_file_btn)
        path_row.addWidget(self.rename_file_btn)
        right_layout.addLayout(path_row)
        self.file_editor = QTextEdit()
        self.file_editor.setObjectName("FileEditor")
        self.file_editor.textChanged.connect(self._on_file_editor_text_changed)
        editor_stack = QStackedWidget()
        editor_loading = QLabel(self._t("Загрузка файла...", "Loading file..."))
        editor_loading.setProperty("class", "muted")
        editor_loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._files_editor_loading_label = editor_loading
        self._files_editor_stack = editor_stack
        editor_stack.addWidget(editor_loading)
        editor_stack.addWidget(self.file_editor)
        editor_shell = QWidget()
        editor_shell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        editor_shell.setAutoFillBackground(False)
        editor_shell.setStyleSheet("background: transparent;")
        editor_grid = QGridLayout(editor_shell)
        editor_grid.setContentsMargins(0, 0, 0, 0)
        editor_grid.setSpacing(0)
        editor_grid.addWidget(editor_stack, 0, 0)

        search_shell, search_panel, search_toggle, search_input, search_prev_btn, search_next_btn = self._build_file_search_variant(
            editor_shell,
            placeholder=self._t("Найти в файле", "Find in file"),
        )
        editor_grid.addWidget(search_shell, 0, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        self._register_file_search_variant(
            "document",
            shell=search_shell,
            panel=search_panel,
            toggle=search_toggle,
            field=search_input,
            prev_btn=search_prev_btn,
            next_btn=search_next_btn,
        )
        self._use_file_search_variant("document")

        right_layout.addWidget(editor_shell, 1)
        save_btn = QPushButton(self._t("Сохранить файл", "Save file"))
        save_btn.clicked.connect(self._save_current_file)
        self._attach_button_animations(save_btn)
        right_layout.addWidget(save_btn)
        advanced_split.addWidget(right, 2)
        advanced_root.addLayout(advanced_split, 1)

        stack.addWidget(chooser_scroll)
        stack.addWidget(tags_page)
        stack.addWidget(advanced_page)
        self._files_mode_opacity_effect = None
        root.addWidget(stack, 1)
        return page

    def _build_logs_page(self) -> QWidget:
        page = QWidget()
        page.setProperty("class", "pageRoot")
        root = QVBoxLayout(page)
        root.setContentsMargins(1, 0, 1, 12)
        root.setSpacing(10)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        label = QLabel(self._t("Логи", "Logs"))
        label.setProperty("class", "title")
        self._logs_title_label = label
        top.addWidget(label)
        source_combo = QComboBox()
        source_combo.setObjectName("LogsSourceCombo")
        source_combo.setView(QListView())
        source_combo.currentIndexChanged.connect(self._on_logs_source_changed)
        self._logs_source_combo = source_combo
        self._rebuild_logs_source_combo()
        top.addWidget(source_combo)
        top.addStretch(1)
        self._logs_refresh_btn = None
        root.addLayout(top)
        self.logs_text = QTextEdit()
        self.logs_text.setReadOnly(True)
        self.logs_text.selectionChanged.connect(self._on_logs_selection_changed)
        self._register_scroll_fade(self.logs_text)
        self._register_smooth_scroll(self.logs_text)
        logs_stack = QStackedWidget()
        logs_loading = QLabel(self._t("Загрузка логов...", "Loading logs..."))
        logs_loading.setProperty("class", "muted")
        logs_loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._logs_loading_label = logs_loading
        self._logs_stack = logs_stack
        logs_stack.addWidget(logs_loading)
        logs_stack.addWidget(self.logs_text)
        root.addWidget(logs_stack)
        return page

    def _build_settings_page(self) -> QWidget:
        from functools import partial

        page = QWidget()
        page.setObjectName("SettingsPage")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        scroll = QScrollArea()
        scroll.setObjectName("SettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        canvas = QWidget()
        canvas.setObjectName("SettingsCanvas")
        canvas.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        scroll.setWidget(canvas)
        root.addWidget(scroll, 1)

        ctrl: dict[str, object] = {}
        page._settings_ctrl = ctrl

        def _segment(items: list[tuple[str, str]], current: str, key: str) -> tuple[QWidget, QButtonGroup]:
            seg = QWidget()
            seg.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
            row = QHBoxLayout(seg)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            group = QButtonGroup(seg)
            for i, (label, value) in enumerate(items):
                btn = QPushButton(label)
                btn.setCheckable(True)
                btn.setFixedHeight(30)
                btn.setProperty("class", "settingsSegment")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setChecked(value == current)
                btn._seg_value = value
                group.addButton(btn, i)
                row.addWidget(btn)
            group.setExclusive(True)
            ctrl[key] = group
            return seg, group

        def _section(title: str) -> QVBoxLayout:
            frame = QFrame()
            frame.setProperty("class", "settingsSection")
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(16, 14, 16, 14)
            fl.setSpacing(10)
            lbl = QLabel(title)
            lbl.setProperty("class", "title")
            fl.addWidget(lbl)
            layout.addWidget(frame)
            return fl

        settings = self.context.settings.get()
        ui_language = settings.language

        # --- Application section ---
        app_section = _section(self._t("Приложение", "Application"))
        theme_combo = QComboBox()
        theme_combo.setObjectName("SettingsThemeCombo")
        theme_items = list_available_themes(self.context.paths.themes_dir, ui_language)
        for tid, tname in theme_items:
            theme_combo.addItem(tname, tid)
            if tid == settings.theme:
                theme_combo.setCurrentIndex(theme_combo.count() - 1)
        ctrl["theme_combo"] = theme_combo
        app_section.addWidget(QLabel(self._t("Тема", "Theme")))
        app_section.addWidget(theme_combo)
        lang_items = [(_language_display_name(l, ui_language), l) for l in ("ru", "en")]
        lang_w, _ = _segment(lang_items, settings.language, "language")
        app_section.addWidget(lang_w)
        autostart_cb = QCheckBox(self._t("Запускать вместе с Windows", "Run with Windows"))
        autostart_cb.setChecked(settings.autostart_windows)
        ctrl["autostart"] = autostart_cb
        app_section.addWidget(autostart_cb)
        tray_cb = QCheckBox(self._t("Стартовать в трее", "Start in tray"))
        tray_cb.setChecked(settings.start_in_tray)
        ctrl["tray"] = tray_cb
        app_section.addWidget(tray_cb)
        auto_comp_cb = QCheckBox(self._t("Автозапуск компонентов", "Auto-run components"))
        auto_comp_cb.setChecked(settings.auto_run_components)
        ctrl["auto_components"] = auto_comp_cb
        app_section.addWidget(auto_comp_cb)
        check_upd_cb = QCheckBox(self._t("Проверять наличие обновлений", "Check for updates"))
        check_upd_cb.setChecked(settings.check_updates_on_start)
        ctrl["check_updates"] = check_upd_cb
        app_section.addWidget(check_upd_cb)

        # --- Zapret section ---
        zapret_section = _section("Zapret")
        ipset_items = [("loaded", "loaded"), ("none", "none"), ("any", "any")]
        zapret_section.addWidget(QLabel("IPSet mode"))
        ipset_w, _ = _segment(ipset_items, settings.zapret_ipset_mode, "ipset_mode")
        zapret_section.addWidget(ipset_w)
        game_items = [
            (self._t("выключен", "disabled"), "disabled"),
            (self._t("tcp + udp", "tcp + udp"), "tcpudp"),
            (self._t("только tcp", "tcp only"), "tcp"),
            (self._t("только udp", "udp only"), "udp"),
        ]
        zapret_section.addWidget(QLabel(self._t("Gaming mode", "Gaming mode")))
        game_w, _ = _segment(game_items, settings.zapret_game_filter_mode, "gaming_mode")
        zapret_section.addWidget(game_w)
        udp_excl = QLineEdit()
        udp_excl.setText(settings.zapret_udp_exclude_ports or "")
        ctrl["udp_exclude"] = udp_excl
        zapret_section.addWidget(QLabel(self._t("Исключить UDP-порты", "Exclude UDP ports")))
        zapret_section.addWidget(udp_excl)

        # --- TG WS Proxy section ---
        tg_section = _section("TG WS Proxy")
        tg_host = QLineEdit()
        tg_host.setText(settings.tg_proxy_host or "")
        ctrl["tg_host"] = tg_host
        tg_section.addWidget(QLabel(self._t("Хост", "Host")))
        tg_section.addWidget(tg_host)
        tg_port = QLineEdit()
        tg_port.setText(str(settings.tg_proxy_port or ""))
        ctrl["tg_port"] = tg_port
        tg_section.addWidget(QLabel(self._t("Порт", "Port")))
        tg_section.addWidget(tg_port)
        tg_secret = QLineEdit()
        tg_secret.setText(settings.tg_proxy_secret or "")
        ctrl["tg_secret"] = tg_secret
        tg_section.addWidget(QLabel(self._t("Секрет", "Secret")))
        tg_section.addWidget(tg_secret)
        tg_media_items = [
            (self._t("Стандартный", "Default"), "default"),
            ("Media fix", "media_fix"),
            (self._t("Без DC override", "No DC override"), "empty"),
        ]
        tg_section.addWidget(QLabel(self._t("Media mode", "Media mode")))
        media_w, media_grp = _segment(tg_media_items, "default", "tg_media_mode")
        tg_section.addWidget(media_w)
        tg_dc = QTextEdit()
        tg_dc.setFixedHeight(72)
        tg_dc.setText(settings.tg_proxy_dc_ip or "")
        ctrl["tg_dc"] = tg_dc

        def _apply_tg_media_preset(btn_id: int) -> None:
            btn = media_grp.button(btn_id)
            if btn is None:
                return
            mode = str(getattr(btn, "_seg_value", "default"))
            if mode == "media_fix":
                tg_dc.setPlainText("4:149.154.167.220")
            elif mode == "empty":
                tg_dc.setPlainText("")
            else:
                tg_dc.setPlainText("2:149.154.167.220\n4:149.154.167.220")

        media_grp.idClicked.connect(_apply_tg_media_preset)
        tg_section.addWidget(QLabel("DC -> IP"))
        tg_section.addWidget(tg_dc)
        tg_cf_cb = QCheckBox(self._t("Cloudflare fallback", "Cloudflare fallback"))
        tg_cf_cb.setChecked(settings.tg_proxy_cfproxy_enabled)
        ctrl["tg_cfproxy"] = tg_cf_cb
        tg_section.addWidget(tg_cf_cb)
        tg_cf_prio_cb = QCheckBox(self._t("Пробовать Cloudflare первым", "Try Cloudflare first"))
        tg_cf_prio_cb.setChecked(settings.tg_proxy_cfproxy_priority)
        ctrl["tg_cfproxy_priority"] = tg_cf_prio_cb
        tg_section.addWidget(tg_cf_prio_cb)
        tg_cf_domain = QLineEdit()
        tg_cf_domain.setText(settings.tg_proxy_cfproxy_domain or "")
        ctrl["tg_cf_domain"] = tg_cf_domain
        tg_section.addWidget(QLabel(self._t("CF domain", "CF domain")))
        tg_section.addWidget(tg_cf_domain)
        tg_fake_tls = QLineEdit()
        tg_fake_tls.setText(settings.tg_proxy_fake_tls_domain or "")
        ctrl["tg_fake_tls"] = tg_fake_tls
        tg_section.addWidget(QLabel(self._t("Fake TLS domain", "Fake TLS domain")))
        tg_section.addWidget(tg_fake_tls)
        tg_buf = QLineEdit()
        tg_buf.setText(str(settings.tg_proxy_buf_kb or ""))
        ctrl["tg_buf"] = tg_buf
        tg_section.addWidget(QLabel(self._t("Буфер, КБ", "Buffer, KB")))
        tg_section.addWidget(tg_buf)
        tg_pool = QLineEdit()
        tg_pool.setText(str(settings.tg_proxy_pool_size or ""))
        ctrl["tg_pool"] = tg_pool
        tg_section.addWidget(QLabel("Pool size"))
        tg_section.addWidget(tg_pool)

        layout.addStretch(1)

        save_btn = QPushButton(self._t("Сохранить настройки", "Save settings"))
        save_btn.setProperty("class", "primary")
        save_btn.setMinimumHeight(38)
        save_btn.clicked.connect(lambda: self._save_settings_page(page))
        layout.addWidget(save_btn)

        # --- Logs and Files section ---
        logs_files_section = _section(self._t("Логи и файлы", "Logs and Files"))
        logs_files_tabs = QTabWidget()
        logs_files_tabs.setDocumentMode(True)
        logs_files_tabs.setObjectName("SettingsLogsFilesTabs")
        logs_files_tabs.tabBar().setUsesScrollButtons(False)
        logs_page = self._build_logs_page()
        logs_files_tabs.addTab(logs_page, self._t("Логи", "Logs"))
        files_page = self._build_files_page()
        logs_files_tabs.addTab(files_page, self._t("Файлы", "Files"))
        logs_files_section.addWidget(logs_files_tabs)

        tools_section = _section(self._t("Инструменты", "Tools"))
        def _make_tool_btn(text: str, slot) -> QPushButton:
            btn = QPushButton(text)
            btn.setMinimumHeight(34)
            btn.clicked.connect(slot)
            return btn

        tools_section.addWidget(_make_tool_btn(
            self._t("Подобрать конфигурацию", "Find best configuration"),
            self._run_general_tests_popup,
        ))
        tools_section.addWidget(_make_tool_btn(
            self._t("Подобрать настройки", "Find best settings"),
            self._run_settings_diagnostics_popup,
        ))
        tools_section.addWidget(_make_tool_btn(
            self._t("Запустить диагностику", "Run diagnostics"),
            self._run_diagnostics_popup,
        ))
        tools_section.addWidget(_make_tool_btn(
            self._t("Проверить обновления", "Check updates"),
            self._check_updates_popup,
        ))
        tools_section.addWidget(_make_tool_btn(
            self._t("Пересобрать merged", "Rebuild merged"),
            self._rebuild_runtime,
        ))
        tools_section.addWidget(_make_tool_btn(
            self._t("Обновить всё", "Refresh all"),
            self.refresh_all,
        ))

        restart_btn = QPushButton(self._t("Настроить заново", "Configure again"))
        restart_btn.setObjectName("RestartOnboardingButton")
        restart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        restart_btn.setMinimumHeight(38)
        restart_btn.setStyleSheet(
            "QPushButton#RestartOnboardingButton {"
            "background: transparent;"
            "border: 1px solid rgba(239, 68, 68, 95);"
            "border-radius: 12px;"
            "padding: 8px 14px;"
            "color: rgba(248, 113, 113, 210);"
            "font-weight: 650;"
            "}"
            "QPushButton#RestartOnboardingButton:hover {"
            "background: rgba(239, 68, 68, 22);"
            "border: 1px solid rgba(248, 113, 113, 145);"
            "color: rgba(252, 165, 165, 235);"
            "}"
        )
        restart_btn.clicked.connect(self._restart_onboarding_from_settings)
        layout.addWidget(restart_btn)

        credits = QLabel(
            self._t(
                "Данное приложения является fork версией Zapret-Hub от goshkow.",
                "This application is a fork version of Zapret-Hub by goshkow.",
            )
        )
        credits.setProperty("class", "muted")
        credits.setWordWrap(True)
        layout.addWidget(credits)

        return page

    def _reload_settings_page(self) -> None:
        page = self.pages.widget(4) if self.pages.count() > 4 else None
        if page is None:
            return
        ctrl = getattr(page, "_settings_ctrl", {})
        if not ctrl:
            return
        settings = self.context.settings.get()

        def _set_seg(key: str, value: str) -> None:
            grp = ctrl.get(key)
            if isinstance(grp, QButtonGroup):
                for btn in grp.buttons():
                    if hasattr(btn, "_seg_value") and str(btn._seg_value) == value:
                        btn.setChecked(True)
                        break

        tc = ctrl.get("theme_combo")
        if isinstance(tc, QComboBox):
            for i in range(tc.count()):
                if tc.itemData(i) == settings.theme:
                    tc.setCurrentIndex(i)
                    break
        _set_seg("language", settings.language)
        cb = ctrl.get("autostart")
        if isinstance(cb, QCheckBox):
            cb.setChecked(settings.autostart_windows)
        cb = ctrl.get("tray")
        if isinstance(cb, QCheckBox):
            cb.setChecked(settings.start_in_tray)
        cb = ctrl.get("auto_components")
        if isinstance(cb, QCheckBox):
            cb.setChecked(settings.auto_run_components)
        cb = ctrl.get("check_updates")
        if isinstance(cb, QCheckBox):
            cb.setChecked(settings.check_updates_on_start)

        _set_seg("ipset_mode", settings.zapret_ipset_mode)
        _set_seg("gaming_mode", settings.zapret_game_filter_mode)
        inp = ctrl.get("udp_exclude")
        if isinstance(inp, QLineEdit):
            inp.setText(settings.zapret_udp_exclude_ports or "")

        inp = ctrl.get("tg_host")
        if isinstance(inp, QLineEdit):
            inp.setText(settings.tg_proxy_host or "")
        inp = ctrl.get("tg_port")
        if isinstance(inp, QLineEdit):
            inp.setText(str(settings.tg_proxy_port or ""))
        inp = ctrl.get("tg_secret")
        if isinstance(inp, QLineEdit):
            inp.setText(settings.tg_proxy_secret or "")
        _set_seg("tg_media_mode", "default")
        inp = ctrl.get("tg_dc")
        if isinstance(inp, QTextEdit):
            inp.setPlainText(settings.tg_proxy_dc_ip or "")
        cb = ctrl.get("tg_cfproxy")
        if isinstance(cb, QCheckBox):
            cb.setChecked(settings.tg_proxy_cfproxy_enabled)
        cb = ctrl.get("tg_cfproxy_priority")
        if isinstance(cb, QCheckBox):
            cb.setChecked(settings.tg_proxy_cfproxy_priority)
        inp = ctrl.get("tg_cf_domain")
        if isinstance(inp, QLineEdit):
            inp.setText(settings.tg_proxy_cfproxy_domain or "")
        inp = ctrl.get("tg_fake_tls")
        if isinstance(inp, QLineEdit):
            inp.setText(settings.tg_proxy_fake_tls_domain or "")
        inp = ctrl.get("tg_buf")
        if isinstance(inp, QLineEdit):
            inp.setText(str(settings.tg_proxy_buf_kb or ""))
        inp = ctrl.get("tg_pool")
        if isinstance(inp, QLineEdit):
            inp.setText(str(settings.tg_proxy_pool_size or ""))

    def _save_settings_page(self, page: QWidget) -> None:
        ctrl = getattr(page, "_settings_ctrl", {})
        if not ctrl:
            return
        payload: dict[str, object] = {}

        def _read_seg(key: str) -> str | None:
            grp = ctrl.get(key)
            if isinstance(grp, QButtonGroup):
                checked = grp.checkedButton()
                if checked is not None and hasattr(checked, "_seg_value"):
                    return str(checked._seg_value)
            return None

        tc = ctrl.get("theme_combo")
        if isinstance(tc, QComboBox):
            tid = tc.currentData()
            if tid:
                payload["theme"] = tid
        val = _read_seg("language")
        if val:
            payload["language"] = val
        cb = ctrl.get("autostart")
        if isinstance(cb, QCheckBox):
            payload["autostart_windows"] = cb.isChecked()
        cb = ctrl.get("tray")
        if isinstance(cb, QCheckBox):
            payload["start_in_tray"] = cb.isChecked()
        cb = ctrl.get("auto_components")
        if isinstance(cb, QCheckBox):
            payload["auto_run_components"] = cb.isChecked()
        cb = ctrl.get("check_updates")
        if isinstance(cb, QCheckBox):
            payload["check_updates_on_start"] = cb.isChecked()

        val = _read_seg("ipset_mode")
        if val:
            payload["zapret_ipset_mode"] = val
        val = _read_seg("gaming_mode")
        if val:
            payload["zapret_game_filter_mode"] = val
        inp = ctrl.get("udp_exclude")
        if isinstance(inp, QLineEdit):
            payload["zapret_udp_exclude_ports"] = inp.text()

        inp = ctrl.get("tg_host")
        if isinstance(inp, QLineEdit):
            payload["tg_proxy_host"] = inp.text()
        inp = ctrl.get("tg_port")
        if isinstance(inp, QLineEdit):
            payload["tg_proxy_port"] = inp.text()
        inp = ctrl.get("tg_secret")
        if isinstance(inp, QLineEdit):
            payload["tg_proxy_secret"] = inp.text()
        inp = ctrl.get("tg_dc")
        if isinstance(inp, QTextEdit):
            payload["tg_proxy_dc_ip"] = inp.toPlainText()
        cb = ctrl.get("tg_cfproxy")
        if isinstance(cb, QCheckBox):
            payload["tg_proxy_cfproxy_enabled"] = cb.isChecked()
        cb = ctrl.get("tg_cfproxy_priority")
        if isinstance(cb, QCheckBox):
            payload["tg_proxy_cfproxy_priority"] = cb.isChecked()
        inp = ctrl.get("tg_cf_domain")
        if isinstance(inp, QLineEdit):
            payload["tg_proxy_cfproxy_domain"] = inp.text()
        inp = ctrl.get("tg_fake_tls")
        if isinstance(inp, QLineEdit):
            payload["tg_proxy_fake_tls_domain"] = inp.text()
        inp = ctrl.get("tg_buf")
        if isinstance(inp, QLineEdit):
            payload["tg_proxy_buf_kb"] = inp.text()
        inp = ctrl.get("tg_pool")
        if isinstance(inp, QLineEdit):
            payload["tg_proxy_pool_size"] = inp.text()

        before = self.context.settings.get()
        QTimer.singleShot(0, lambda p=payload, b=before: self._apply_settings_payload(b, p))

    def _setup_tray(self) -> None:
        self.tray_icon = QSystemTrayIcon(self._runtime_window_icon(), self)
        menu = QMenu(self)
        show_action = QAction(self._t("Открыть", "Open"), self)
        toggle_action = QAction(self._t("Компоненты", "Components"), self)
        general_menu = QMenu(self._t("Конфигурация Zapret", "Zapret configuration"), self)
        quit_action = QAction(self._t("Выход", "Exit"), self)
        show_action.triggered.connect(self._restore_from_tray)
        toggle_action.triggered.connect(self._tray_toggle_master_runtime)
        quit_action.triggered.connect(self._exit_application)
        self._tray_show_action = show_action
        self._tray_toggle_action = toggle_action
        self._tray_general_menu = general_menu
        self._tray_quit_action = quit_action
        menu.addAction(show_action)
        menu.addAction(toggle_action)
        menu.addMenu(general_menu)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.setToolTip("Zapret-Zen")
        self.tray_icon.show()
        self._rebuild_tray_menu()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._active_emoji_popup is not None and self._active_emoji_popup.isVisible():
            popup_rect = self._active_emoji_popup.geometry()
            if not popup_rect.contains(event.position().toPoint()):
                self._active_emoji_popup.close()
                self._active_emoji_popup = None
                app = QCoreApplication.instance()
                if app is not None:
                    try:
                        app.removeEventFilter(self)
                    except Exception:
                        pass
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 48:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Print:
            super().keyPressEvent(event)
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._window_fade_pending_action is not None:
            event.ignore()
            return
        if self._general_test_running:
            self._cancel_general_tests()
        if not self._force_exit:
            if self._should_minimize_to_tray():
                event.ignore()
                self._animate_window_fade(showing=False, action="tray")
                return
            event.ignore()
            self._begin_fast_exit()
            return
        event.accept()
        super().closeEvent(event)

    def nativeEvent(self, eventType: QByteArray, message: int) -> tuple[bool, int]:
        if sys.platform.startswith("win"):
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))  # type: ignore[attr-defined]
                wm_powerbroadcast = 0x0218
                pbt_apmsuspend = 0x0004
                pbt_apmresumeautomatic = 0x0012
                pbt_apmresumesuspend = 0x0007
                if int(msg.message) == wm_powerbroadcast:
                    if int(msg.wParam) == pbt_apmsuspend:
                        QTimer.singleShot(0, self._handle_system_suspend)
                    elif int(msg.wParam) in {pbt_apmresumeautomatic, pbt_apmresumesuspend}:
                        QTimer.singleShot(1200, self._handle_system_resume)
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def _handle_system_suspend(self) -> None:
        if self._resume_restart_pending:
            return
        running_ids: list[str] = []
        try:
            states = self._component_states()
            components = self._component_defs()
            for component_id, component in components.items():
                state = states.get(component_id)
                if getattr(component, "enabled", False) and state is not None and getattr(state, "status", "") == "running":
                    running_ids.append(component_id)
        except Exception:
            running_ids = []
        self._resume_component_ids = list(running_ids)
        self._resume_restart_pending = bool(running_ids)
        for component_id in running_ids:
            try:
                self.context.processes.stop_component(component_id)
            except Exception:
                continue
        if running_ids:
            self._mark_dirty("dashboard", "components", "tray")

    def _handle_system_resume(self) -> None:
        if not self._resume_restart_pending:
            return
        restart_ids = list(self._resume_component_ids)
        self._resume_component_ids = []
        self._resume_restart_pending = False
        if not restart_ids:
            return
        for component_id in restart_ids:
            try:
                self.context.processes.start_component(component_id)
            except Exception:
                continue
        self._mark_dirty("dashboard", "components", "tray")

    def _restore_from_tray(self) -> None:
        self._sync_window_icon()
        if self._window_opacity_animation is not None:
            self._window_opacity_animation.stop()
        self._window_fade_pending_action = None
        self._skip_next_show_fade = True
        self._skip_next_show_focus = False
        self.setWindowOpacity(1.0)
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self._schedule_post_show_sync()
        QTimer.singleShot(0, lambda: _bring_widget_to_front(self))

    def _tray_toggle_master_runtime(self) -> None:
        if self._toggle_in_progress:
            return
        self._toggle_master_runtime()

    def start_enabled_components_async(self, *, autostart_only: bool = False) -> None:
        if self._toggle_in_progress:
            return
        self._loading_action = "connect"
        self._toggle_in_progress = True
        self._loading_timer.start()
        self._advance_loading_caption()
        self._submit_backend_task("start_enabled_components", {"autostart_only": autostart_only})

    def _tray_select_general(self, general_id: str) -> None:
        if not general_id:
            return
        current = self.context.settings.get().selected_zapret_general
        if general_id == current:
            return
        self.context.settings.get().selected_zapret_general = general_id
        states = self._component_states()
        if states.get("zapret") and states["zapret"].status == "running":
            self._toggle_in_progress = True
            self._loading_action = "connect"
            self._loading_timer.start()
            self._advance_loading_caption()
            self._submit_backend_task("select_general", {"selected": general_id})
        else:
            self._submit_backend_task("select_general", {"selected": general_id})
            self.refresh_all()

    def restore_from_external_launch(self) -> None:
        self._restore_from_tray()

    def _exit_application(self) -> None:
        self._begin_fast_exit()

    def _begin_fast_exit(self) -> None:
        self._force_exit = True
        if self._window_opacity_animation is not None:
            self._window_opacity_animation.stop()
            self._window_opacity_animation = None
        self._skip_next_show_fade = False
        self._skip_next_show_focus = False
        self._animate_window_fade(showing=False, action="exit-fast")

    def _quit_for_update(self) -> None:
        self._force_exit = True
        if self._window_opacity_animation is not None:
            self._window_opacity_animation.stop()
        self._window_fade_pending_action = None
        self.setWindowOpacity(1.0)
        self.hide()
        self._shutdown_runtime(blocking=True)
        app = QCoreApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def _finalize_exit(self) -> None:
        self._shutdown_runtime(blocking=False)
        app = QCoreApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def _shutdown_runtime(self, *, blocking: bool) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._loading_timer.stop()
        self._component_loading_timer.stop()
        self._general_test_eta_timer.stop()
        self._general_test_running = False
        try:
            if self.context.backend is not None:
                if blocking:
                    self.context.backend.stop()
                else:
                    self.context.backend.request_shutdown_background()
            elif blocking:
                self.context.processes.stop_all()
            else:
                threading.Thread(target=self.context.processes.stop_all, daemon=True).start()
        except Exception:
            pass
        if hasattr(self, "tray_icon") and self.tray_icon is not None:
            try:
                self.tray_icon.hide()
                self.tray_icon.setContextMenu(None)
                self.tray_icon.deleteLater()
            except Exception:
                pass

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._restore_from_tray()

    def _rebuild_tray_menu(self) -> None:
        if self._tray_general_menu is None:
            return
        self._tray_general_menu.clear()
        group = QActionGroup(self)
        group.setExclusive(True)
        selected = self.context.settings.get().selected_zapret_general
        for option in self._sorted_general_options():
            action = QAction(self._format_general_option_label(option), self)
            action.setCheckable(True)
            action.setChecked(option["id"] == selected)
            action.triggered.connect(lambda _=False, gid=option["id"]: self._tray_select_general(gid))
            group.addAction(action)
            self._tray_general_menu.addAction(action)
        self._tray_general_action_group = group
        states = self._component_states()
        active_ids = self._master_active_components()
        running_ids = {cid for cid in active_ids if states.get(cid) and states[cid].status == "running"}
        if self._tray_toggle_action is not None:
            fully_running = bool(active_ids) and running_ids == set(active_ids)
            partially_running = bool(running_ids) and not fully_running
            if fully_running:
                icon_name = "status_ok.svg"
                state_text = self._t("Включены", "Enabled")
            elif partially_running:
                icon_name = "status_warn.svg"
                state_text = self._t("Частично", "Partial")
            else:
                icon_name = "status_off.svg"
                state_text = self._t("Выключены", "Disabled")
            self._tray_toggle_action.setIcon(self._icon(icon_name))
            self._tray_toggle_action.setText(f"{self._t('Компоненты', 'Components')}: {state_text}")

    def _should_minimize_to_tray(self) -> bool:
        # В close path используем только последний snapshot, без live runtime вызовов.
        states = self._component_states()
        for component_id in self._master_active_components():
            state = states.get(component_id)
            if state and state.status == "running":
                return True
        return False

    def _attach_button_animations(self, widget: QWidget) -> None:
        if isinstance(widget, AnimatedNavButton):
            widget.set_nav_theme(self.context.settings.get().theme)
            return
        if isinstance(widget, AnimatedPowerButton):
            return
        if isinstance(widget, OnboardingServiceProgressButton):
            return
        if isinstance(widget, (QPushButton, QToolButton)):
            marker = widget.property("_interactionBound")
            if not marker:
                widget.setProperty("_interactionBound", True)
                self._button_interactions.append(ButtonInteractionFilter(widget))

    def _attach_button_animations_recursive(self, root: QWidget | None) -> None:
        if root is None:
            return
        for widget in root.findChildren(QWidget):
            self._attach_button_animations(widget)

    def _animate_button_opacity(self, widget: QWidget, target: float, duration: int) -> None:
        return

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._file_tag_input and isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Comma, Qt.Key.Key_Semicolon):
                self._commit_tag_input()
                return True
        if watched is self._file_search_input and isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self._toggle_file_search(False)
                return True
        if self._file_search_expanded and self._file_search_panel is not None and event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            panel_rect = QRect(self._file_search_panel.mapToGlobal(QPoint(0, 0)), self._file_search_panel.size())
            if not panel_rect.contains(event.globalPosition().toPoint()):
                self._toggle_file_search(False)
        if self._active_emoji_popup is not None:
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                popup = self._active_emoji_popup
                global_pos = event.globalPosition().toPoint()
                popup_rect = QRect(popup.mapToGlobal(QPoint(0, 0)), popup.size())
                if not popup_rect.contains(global_pos):
                    popup.close()
                    self._active_emoji_popup = None
                    app = QCoreApplication.instance()
                    if app is not None:
                        try:
                            app.removeEventFilter(self)
                        except Exception:
                            pass
            elif event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Escape:
                self._active_emoji_popup.close()
                self._active_emoji_popup = None
                app = QCoreApplication.instance()
                if app is not None:
                    try:
                        app.removeEventFilter(self)
                    except Exception:
                        pass
                return True
        return super().eventFilter(watched, event)

    def _switch_page(self, index: int) -> None:
        current_index = self.pages.currentIndex()
        try:
            if self._active_emoji_popup is not None:
                try:
                    self._active_emoji_popup.close()
                except Exception:
                    pass
                self._active_emoji_popup = None
                app = QCoreApplication.instance()
                if app is not None:
                    try:
                        app.removeEventFilter(self)
                    except Exception:
                        pass
            for i, btn in enumerate(self._nav_buttons):
                btn.setChecked(i == index)
            self._sync_nav_highlight(animated=True)
            if index == 1:
                self.refresh_services()
                if self._services_scroll is not None:
                    QTimer.singleShot(0, lambda: self._services_scroll.verticalScrollBar().setValue(0))
            elif index == 2:
                try:
                    cached = self._page_payload_cache.get("components")
                    if isinstance(cached, dict):
                        self.refresh_components(cached)
                    else:
                        self.refresh_components(self._build_components_cached_payload())
                except Exception as error:
                    self.context.logging.log("error", "components_prewarm_failed", error=str(error))
                    try:
                        self.refresh_components({"components": [], "states": []})
                    except Exception:
                        pass
                self._sync_component_card_layout()
                QTimer.singleShot(0, self._sync_component_card_layout)
                try:
                    self._request_page_refresh("components")
                except Exception as error:
                    self.context.logging.log("error", "components_refresh_request_failed", error=str(error))
            elif index == 0:
                self.refresh_dashboard()
                self._sync_power_aura_geometry()
            elif index == 3:
                cached = self._page_payload_cache.get("mods")
                if cached is not None:
                    self.refresh_mods(cached)
                    self._sync_mod_card_layout()
                else:
                    self._request_page_refresh("mods")
                self._sync_mod_card_layout()
            elif index == 4:
                self._reload_settings_page()
                self.refresh_dashboard()
            if index != self.pages.currentIndex():
                self._prepare_page_geometry_for_index(index)
                try:
                    self._animate_page_switch(index)
                except Exception as error:
                    self.context.logging.log("error", "switch_page_animation_failed", index=index, error=str(error))
                    self._cancel_page_transition()
                    self.pages.setCurrentIndex(index)
                    if self._pages_shell is not None:
                        self._pages_shell.show()
            self._set_logs_live_enabled(False)
            section_map = {
                0: "dashboard",
                1: "services",
                2: "components",
                3: "mods",
            }
            section = section_map.get(index)
            if section:
                self._mark_dirty(section)
            else:
                self._schedule_dirty_refresh()
        except Exception as error:
            self.context.logging.log("error", "switch_page_failed", index=index, error=str(error))
            self._cancel_page_transition()
            actual_index = self.pages.currentIndex()
            for i, btn in enumerate(self._nav_buttons):
                btn.setChecked(i == actual_index)
            self._sync_nav_highlight(animated=False)
            self._set_logs_live_enabled(False)

    def _sync_nav_highlight(self, *, animated: bool) -> None:
        sidebar = self.findChild(SidebarPanel, "Sidebar")
        if sidebar is None:
            return
        current = next((btn for btn in self._nav_buttons if btn.isChecked()), None)
        if current is None:
            sidebar.clear_highlight()
            return
        rect = current.geometry()
        if rect.isNull() or not sidebar.contentsRect().adjusted(-6, -6, 6, 6).contains(rect):
            QTimer.singleShot(0, lambda: self._sync_nav_highlight(animated=False))
            return
        sidebar.move_highlight(rect, animated=animated)

    def _cancel_page_transition(self) -> None:
        if self._page_transition_out is not None:
            try:
                self._page_transition_out.stop()
            except Exception:
                pass
        if self._page_transition_in is not None:
            try:
                self._page_transition_in.stop()
            except Exception:
                pass
        self._page_transition_out = None
        self._page_transition_in = None
        self._page_transition_running = False
        self._page_transition_started_at = 0.0
        if self._page_opacity_effect is not None:
            self._page_opacity_effect.setOpacity(1.0)
        if self._page_transition_overlay is not None:
            self._page_transition_overlay.hide()
            self._page_transition_overlay.set_background_color(QColor(0, 0, 0, 0))
            self._page_transition_overlay.clear_transition()
        if self._pages_shell is not None:
            self._pages_shell.show()

    def _animate_page_switch(self, index: int) -> None:
        effect = self._page_opacity_effect
        if effect is None:
            self.pages.setCurrentIndex(index)
            return
        if self._page_transition_running:
            self._cancel_page_transition()
        if self.pages.currentIndex() == index:
            return
        self._page_transition_target = index
        self._page_transition_running = True
        self._page_transition_started_at = time.monotonic()
        fade_out = QPropertyAnimation(effect, b"opacity", self)
        fade_out.setDuration(60)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.0)
        fade_out.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def _finish() -> None:
            if self.pages.currentIndex() == 0:
                self.refresh_dashboard()
                self._sync_power_aura_geometry()
            elif self.pages.currentIndex() == 1:
                self.refresh_services()
            elif self.pages.currentIndex() == 2:
                self._sync_component_card_layout()
            elif self.pages.currentIndex() == 3:
                self._sync_mod_card_layout()
            self._page_transition_running = False
            self._page_transition_started_at = 0.0
            self._page_transition_target = self.pages.currentIndex()
            self._page_transition_out = None
            self._page_transition_in = None

        def _start_fade_in() -> None:
            self.pages.setCurrentIndex(index)
            if index == 0:
                self.refresh_dashboard()
            self._prepare_page_geometry_for_index(index)
            fade_in = QPropertyAnimation(effect, b"opacity", self)
            fade_in.setDuration(110)
            fade_in.setStartValue(0.0)
            fade_in.setEndValue(1.0)
            fade_in.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self._page_transition_in = fade_in
            fade_in.finished.connect(_finish)
            fade_in.start()

        self._page_transition_out = fade_out
        self._page_transition_in = None
        fade_out.finished.connect(_start_fade_in)
        fade_out.start()
        return

    def _animate_window_fade(self, *, showing: bool, action: str | None = None) -> None:
        if self._window_opacity_animation is not None:
            self._window_opacity_animation.stop()
        animation = QPropertyAnimation(self, QByteArray(b"windowOpacity"), self)
        fade_out_duration = 95 if action == "exit-fast" else 130
        animation.setDuration(170 if showing else fade_out_duration)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic if showing else QEasingCurve.Type.InCubic)
        if showing:
            self.setWindowOpacity(0.0)
            animation.setStartValue(0.0)
            animation.setEndValue(1.0)
        else:
            self._window_fade_pending_action = action
            animation.setStartValue(float(self.windowOpacity()))
            animation.setEndValue(0.0)

            def _finish_hide() -> None:
                pending = self._window_fade_pending_action
                self._window_fade_pending_action = None
                if pending == "tray":
                    self.setWindowOpacity(1.0)
                    self.hide()
                    if not self._tray_notifications_shown:
                        self.tray_icon.showMessage("Zapret-Zen", self._t("Приложение свернуто в трей.", "Minimized to tray."), QSystemTrayIcon.MessageIcon.Information, 2200)
                        self._tray_notifications_shown = True
                elif pending == "minimize":
                    self.showMinimized()
                    QTimer.singleShot(0, lambda: self.setWindowOpacity(1.0))
                elif pending in {"exit", "exit-fast"}:
                    self.setWindowOpacity(1.0)
                    self.hide()
                    QTimer.singleShot(0, self._finalize_exit)
                else:
                    self.setWindowOpacity(1.0)

            animation.finished.connect(_finish_hide)
        self._window_opacity_animation = animation
        animation.start()


    def _open_settings_dialog(self, target_component_id: str = "") -> None:
        signature = (self.context.settings.get().theme, self.context.settings.get().language)
        if self._settings_dialog is None or self._settings_dialog_signature != signature:
            if self._settings_dialog is not None:
                self._settings_dialog.deleteLater()
            self._settings_dialog = SettingsDialog(self, self.context)
        self._settings_dialog_signature = signature
        dialog = self._settings_dialog
        if self._pending_settings_payload is not None:
            dialog.load_from_payload(self._pending_settings_payload)
        else:
            dialog._load()
        dialog.prepare_and_center()
        if target_component_id:
            dialog.scroll_to_component_settings(target_component_id)
        if dialog.exec():
            before = self.context.settings.get()
            payload = dialog.payload()
            if signature != (str(payload["theme"]), str(payload["language"])):
                self._settings_dialog = None
                self._settings_dialog_signature = None
            QTimer.singleShot(0, lambda p=payload, b=before: self._apply_settings_payload(b, p))

    def _open_component_settings(self, component_id: str) -> None:
        target = str(component_id or "").strip()
        if target == "zapret":
            QTimer.singleShot(0, self._show_logs_files_dialog)
            return
        if target not in {"tg-ws-proxy", "peshk0v-vpn"}:
            return
        QTimer.singleShot(0, lambda t=target: self._open_settings_dialog(t))

    def _refresh_peshk0v_vpn_subscription_from_settings(self, url: str) -> None:
        current_url = str(self.context.vpn.state().get("subscription_url", "") or "")
        normalized = str(url or "").strip()
        if normalized and normalized != current_url:
            self._submit_backend_task(
                "import_peshk0v_vpn_subscription",
                {"url": normalized},
                action_id="__settings_vpn_refresh__",
            )
            return
        self._submit_backend_task(
            "refresh_peshk0v_vpn_subscription",
            action_id="__settings_vpn_refresh__",
        )

    def _apply_settings_payload(self, before, payload: dict[str, object]) -> None:
        effective_payload = dict(payload)
        if "fortnite" in {str(item) for item in list(self.context.settings.get().selected_service_ids or [])}:
            effective_payload["zapret_ipset_mode"] = "any"
            effective_payload["zapret_game_filter_mode"] = "tcpudp"
        before_theme = str(getattr(before, "theme", self.context.settings.get().theme))
        before_language = str(getattr(before, "language", self.context.settings.get().language))
        next_theme = str(effective_payload.get("theme", before_theme))
        next_language = str(effective_payload.get("language", before_language))
        theme_changed = before_theme != next_theme
        language_changed = before_language != next_language
        self._settings_save_revision += 1
        revision = self._settings_save_revision
        self._pending_settings_payload = dict(effective_payload)
        self.context.settings.update(**effective_payload)
        if theme_changed:
            self._apply_theme()
        if language_changed:
            self._retranslate_ui()
        if theme_changed or language_changed:
            self._schedule_full_locale_theme_refresh()
        backend_payload = dict(effective_payload)
        backend_payload["client_revision"] = revision
        self._submit_backend_task("apply_settings", backend_payload, action_id="__settings__")

    def _schedule_full_locale_theme_refresh(self) -> None:
        QTimer.singleShot(0, self._refresh_ui_after_locale_theme_change)

    def _refresh_ui_after_locale_theme_change(self) -> None:
        previous_updates = self.updatesEnabled()
        self.setUpdatesEnabled(False)
        try:
            if self._settings_dialog is not None:
                self._settings_dialog.deleteLater()
                self._settings_dialog = None
            self._settings_dialog_signature = None
            self._icon_cache.clear()
            self._service_icon_cache.clear()
            self._service_check_cache.clear()
            self._page_payload_cache.clear()
            self._cancel_page_transition()
            self.refresh_dashboard()
            self.refresh_services()
            self.refresh_components()
            self.refresh_mods()
            self._request_page_refresh("files")
            self._request_page_refresh("logs")
            self._force_repolish_widget_tree()
            self._refresh_current_page_after_theme_change()
            self._mark_dirty("dashboard", "services", "components", "mods", "files", "logs", "tray")
        finally:
            self.setUpdatesEnabled(previous_updates)
            self.update()
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            QTimer.singleShot(0, self._force_repolish_widget_tree)

    def _force_repolish_widget_tree(self) -> None:
        for widget in [self, *self.findChildren(QWidget)]:
            try:
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()
            except Exception:
                continue

    def _refresh_current_page_after_theme_change(self) -> None:
        if not hasattr(self, "pages"):
            return
        current_index = self.pages.currentIndex()
        if current_index == 0:
            self.refresh_dashboard()
        elif current_index == 1:
            self.refresh_services()
        elif current_index == 2:
            self.refresh_components()
        elif current_index == 3:
            self.refresh_mods()
        elif current_index == 4:
            self._reload_settings_page()

    def _restore_optimistic_settings_if_needed(self) -> None:
        if self._pending_settings_payload is None:
            return
        if self._settings_save_acked_revision >= self._settings_save_revision:
            self._pending_settings_payload = None
            return
        self.context.settings.update(**self._pending_settings_payload)

    def _run_settings_diagnostics_popup(self) -> None:
        if self._settings_diag_task_id:
            return
        self._settings_diag_cancelled = False
        dialog = AppDialog(self, self.context, self._t("Подобрать настройки", "Find best settings"))
        label = QLabel(
            self._t(
                "Сейчас приложение проверит разные комбинации IPSet mode и Gaming mode для выбранной конфигурации.",
                "The app will now test different IPSet mode and Gaming mode combinations for the selected configuration.",
            )
        )
        label.setWordWrap(True)
        dialog.body_layout.addWidget(label)
        status = QLabel(self._t("Подготовка...", "Preparing..."))
        status.setProperty("class", "muted")
        dialog.body_layout.addWidget(status)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        dialog.body_layout.addWidget(bar)
        dialog.prepare_and_center()
        dialog.show()
        self._settings_diag_dialog = dialog
        self._settings_diag_status_label = status
        self._settings_diag_progress_bar = bar
        dialog.rejected.connect(self._cancel_settings_diagnostics)
        self._settings_diag_task_id = self._submit_backend_task("run_settings_diagnostics", action_id="__settings_diag__")

    def _cancel_settings_diagnostics(self) -> None:
        self._settings_diag_cancelled = True
        if self.context.backend is not None and self._settings_diag_task_id:
            self.context.backend.cancel(self._settings_diag_task_id)

    def _prime_cached_dialogs(self) -> None:
        if self._launch_hidden:
            return
        signature = (self.context.settings.get().theme, self.context.settings.get().language)
        if self._settings_dialog is None or self._settings_dialog_signature != signature:
            self._settings_dialog = SettingsDialog(self, self.context)
            self._settings_dialog_signature = signature

    def _submit_backend_task(self, action: str, payload: dict[str, object] | None = None, *, action_id: str | None = None) -> str:
        if self.context.backend is None:
            raise RuntimeError("Backend worker is not available")
        task_id = self.context.backend.submit(action, payload or {})
        self._backend_tasks[task_id] = action_id or action
        return task_id

    def _on_backend_task_finished(self, message: dict) -> None:
        task_id = str(message.get("id", ""))
        action = str(message.get("action", ""))
        action_id = self._backend_tasks.pop(task_id, action)
        payload = message.get("payload", {})
        service_response_revision = 0
        settings_response_revision = 0
        if action == "set_selected_services" and isinstance(payload, dict):
            try:
                service_response_revision = int(payload.get("client_revision", 0) or 0)
            except (TypeError, ValueError):
                service_response_revision = 0
            if service_response_revision and service_response_revision < self._services_selection_revision:
                return
        if action == "apply_settings" and isinstance(payload, dict):
            try:
                settings_response_revision = int(payload.get("client_revision", 0) or 0)
            except (TypeError, ValueError):
                settings_response_revision = 0
            if settings_response_revision and settings_response_revision < self._settings_save_revision:
                return
        # Store pending theme before reload to restore it if needed
        pending_theme = self._pending_settings_payload.get("theme") if self._pending_settings_payload else None
        self.context.settings.reload()
        if action == "apply_settings" and settings_response_revision:
            self._settings_save_acked_revision = max(self._settings_save_acked_revision, settings_response_revision)
            if settings_response_revision >= self._settings_save_revision:
                self._pending_settings_payload = None
                self._settings_save_acked_revision = self._settings_save_revision
            # Restore theme if it was pending and got reverted by reload
            if pending_theme and self.context.settings.get().theme != pending_theme:
                self.context.settings.update(theme=str(pending_theme))
                self._apply_theme()
        self._restore_optimistic_settings_if_needed()
        self._restore_optimistic_service_selection_if_needed()
        self._update_runtime_snapshot_from_payload(payload)
        self._update_mods_cache_from_payload(payload)
        self._update_general_options_from_payload(payload)
        if action in {"toggle_master_runtime", "start_enabled_components", "start_component", "select_general", "apply_settings", "load_startup_snapshot", "load_components_payload", "select_runtime_mode", "toggle_peshk0v_vpn_mode", "import_peshk0v_vpn_subscription", "refresh_peshk0v_vpn_subscription", "update_peshk0v_vpn_settings", "reset_peshk0v_vpn_traffic"}:
            self._notify_component_errors_from_payload(payload)
        self._notify_telegram_proxy_status_from_payload(payload)
        self._notify_zapret_restart_from_payload(payload)
        if action in {"update_zapret_runtime", "update_tg_ws_proxy_runtime"}:
            self._invalidate_general_options_cache()
            self._page_payload_cache.clear()
        elif action in {"toggle_mod", "toggle_component_enabled", "move_mod", "set_mod_emoji", "install_mod", "remove_mod", "import_mod_from_github", "import_mod_from_paths", "import_mod_from_path", "rebuild_merge_runtime", "set_selected_services", "select_runtime_mode", "toggle_peshk0v_vpn_mode", "import_peshk0v_vpn_subscription", "refresh_peshk0v_vpn_subscription", "update_peshk0v_vpn_settings", "reset_peshk0v_vpn_traffic"}:
            self._page_payload_cache.clear()
        if action == "load_startup_snapshot":
            self._startup_snapshot_ready = True
            self._page_payload_cache["components"] = {
                "components": payload.get("components", []),
                "states": payload.get("states", []),
                "general_options": payload.get("general_options", []),
                "peshk0v_vpn": payload.get("peshk0v_vpn", {}),
            }
            self._page_payload_cache["mods"] = {
                "index": payload.get("index", []),
                "installed": payload.get("installed", []),
            }
            self._mark_dirty("dashboard", "services", "components", "mods", "files", "tray")
            return
        if action == "apply_settings":
            desired_autostart = bool(self.context.settings.get().autostart_windows)
            actual_autostart = self.context.autostart.is_enabled()
            if bool(payload.get("autostart_changed")) or desired_autostart != actual_autostart:
                actual_autostart = self.context.autostart.set_enabled(desired_autostart)
                if actual_autostart != desired_autostart:
                    self.context.settings.update(autostart_windows=actual_autostart)
                    self._add_notification(
                        "error",
                        self._t("Автозапуск Windows", "Windows autostart"),
                        self._t(
                            "Не удалось включить автозапуск. Проверьте права Windows или политики безопасности.",
                            "Could not enable autostart. Check Windows permissions or security policies.",
                        ),
                        source="settings",
                        details={"dedupe_key": "settings-autostart-failed"},
                    )
            if bool(payload.get("theme_changed")):
                self._apply_theme()
            if bool(payload.get("language_changed")):
                self._retranslate_ui()
            if bool(payload.get("theme_changed")) or bool(payload.get("language_changed")):
                self._schedule_full_locale_theme_refresh()
            self._mark_dirty("dashboard", "services", "components", "mods", "files", "logs", "tray")
        if action in {"toggle_master_runtime", "start_enabled_components", "select_general"}:
            self._mark_dirty("dashboard", "components", "tray")
            self._ui_signals.toggle_done.emit()
            if action == "select_general":
                self._ui_signals.component_action_done.emit("__general__")
            return
        if action in {"start_component", "stop_component"}:
            self._mark_dirty("dashboard", "components", "tray")
            self._ui_signals.component_action_done.emit(action_id)
            return
        if action in {"select_runtime_mode", "toggle_peshk0v_vpn_mode", "import_peshk0v_vpn_subscription", "refresh_peshk0v_vpn_subscription", "update_peshk0v_vpn_settings", "reset_peshk0v_vpn_traffic"}:
            self._mark_dirty("dashboard", "components", "tray")
            if action_id == "__settings_vpn_refresh__":
                self._finish_settings_vpn_refresh(success=True)
            self._ui_signals.component_action_done.emit(action_id)
            return
        if action == "prepare_general_autotest_runtime":
            self._on_general_test_runtime_prepared(payload)
            return
        if action == "restore_general_autotest_runtime":
            self._mark_dirty("dashboard", "components", "tray")
            return
        if action == "apply_settings":
            self._ui_signals.component_action_done.emit("__settings__")
            return
        if action == "toggle_component_enabled":
            self._mark_dirty("dashboard", "components", "tray")
            self._ui_signals.component_action_done.emit(action_id)
            return
        if action == "toggle_component_autostart":
            self._mark_dirty("components")
            self._ui_signals.component_action_done.emit(action_id)
            return
        if action == "set_selected_services":
            self._mark_dirty("dashboard", "services", "components", "files", "tray")
            return
        if action == "toggle_mod":
            self._mark_dirty("dashboard", "mods", "files", "logs", "tray")
            return
        if action in {"install_mod", "remove_mod", "import_mod_from_github", "import_mod_from_paths", "import_mod_from_path"}:
            self._mark_dirty("dashboard", "mods", "components", "files", "logs", "tray")
            return
        if action in {"move_mod", "set_mod_emoji"}:
            self._mark_dirty("mods", "components", "files")
            return
        if action == "restart_zapret_if_running":
            self._mark_dirty("dashboard", "components", "tray")
            return
        if action in {"add_collection_values", "remove_collection_value", "reset_user_overrides"}:
            files_payload = payload.get("files_payload") if isinstance(payload, dict) else None
            if isinstance(files_payload, dict):
                self._page_payload_cache["files"] = files_payload
                self.refresh_files(files_payload)
            self._mark_dirty("dashboard", "files", "logs", "tray")
            self._ui_signals.component_action_done.emit("__files_collection__")
            return
        if action == "load_files_payload":
            files_payload = payload.get("files_payload") if isinstance(payload, dict) else None
            if isinstance(files_payload, dict):
                self._page_payload_cache["files"] = files_payload
                self.refresh_files(files_payload)
            return
        if action == "load_components_payload":
            if isinstance(payload, dict):
                self._page_payload_cache["components"] = payload
                if self.pages.currentIndex() == 2:
                    self.refresh_components(payload)
            return
        if action == "write_file_text":
            saved_path = str(payload.get("path", "") or "")
            if saved_path:
                self.context.logging.log("info", "File saved", path=saved_path)
            self._mark_dirty("files", "logs")
            self._ui_signals.component_action_done.emit("__file_saved__")
            return
        if action == "rebuild_merge_runtime":
            self._ui_signals.component_action_done.emit("__merge_rebuild__")
            return
        if action == "run_general_diagnostics":
            self._ui_signals.general_test_done.emit(payload.get("results", []))
            return
        if action == "run_general_diagnostic_single":
            self._ui_signals.general_test_done.emit(payload)
            return
        if action == "run_settings_diagnostics":
            self._show_settings_diagnostics_result(payload)
            return
        if action == "update_zapret_runtime":
            self._close_component_update_dialog()
            status = str(payload.get("status", ""))
            if status == "up-to-date":
                self._show_info("Zapret", self._t("Уже установлена последняя версия Zapret.", "The latest Zapret version is already installed."))
            elif status == "updated":
                self._show_info("Zapret", self._t("Zapret успешно обновлён.", "Zapret was updated successfully."))
                self._add_notification("success", "Zapret", self._t("Zapret успешно обновлён.", "Zapret was updated successfully."), source="zapret")
            else:
                message = str(payload.get("error", self._t("Не удалось обновить Zapret.", "Failed to update Zapret.")))
                self._add_notification("error", "Zapret", message, source="zapret", details={"dedupe_key": f"update-error:zapret:{message}"})
                self._show_error("Zapret", message)
            self._mark_dirty("dashboard", "components", "files", "logs")
            return
        if action == "update_tg_ws_proxy_runtime":
            self._close_component_update_dialog()
            status = str(payload.get("status", ""))
            if status == "up-to-date":
                self._show_info("TG WS Proxy", self._t("Уже установлена последняя версия TG WS Proxy.", "The latest TG WS Proxy version is already installed."))
            elif status == "updated":
                self._show_info("TG WS Proxy", self._t("TG WS Proxy успешно обновлён.", "TG WS Proxy was updated successfully."))
                self._add_notification("success", "TG WS Proxy", self._t("TG WS Proxy успешно обновлён.", "TG WS Proxy was updated successfully."), source="tg-ws-proxy")
            else:
                message = str(payload.get("error", self._t("Не удалось обновить TG WS Proxy.", "Failed to update TG WS Proxy.")))
                self._add_notification("error", "TG WS Proxy", message, source="tg-ws-proxy", details={"dedupe_key": f"update-error:tg-ws-proxy:{message}"})
                self._show_error("TG WS Proxy", message)
            self._mark_dirty("dashboard", "components", "files", "logs")
            return

    def _on_backend_task_failed(self, message: dict) -> None:
        task_id = str(message.get("id", ""))
        action = str(message.get("action", ""))
        action_id = self._backend_tasks.pop(task_id, action)
        source = self._backend_error_source(action, str(message.get("source", "") or ""))
        raw_error = str(message.get("error", self._t("Неизвестная ошибка.", "Unknown error.")))
        error = self._friendly_backend_error(raw_error, source=source, action=action)
        if action == "load_startup_snapshot":
            self.context.logging.log("error", "startup_snapshot_failed", error=error)
            self._ensure_local_runtime_snapshot()
            self._startup_snapshot_ready = True
            self._mark_dirty("dashboard", "components", "tray")
            return
        if action in {"toggle_master_runtime", "start_enabled_components", "select_general"}:
            self._ui_signals.toggle_done.emit()
            if action == "select_general":
                self._ui_signals.component_action_done.emit("__general__")
        if action == "apply_settings":
            self._ui_signals.component_action_done.emit("__settings__")
        if action in {"toggle_component_enabled", "toggle_component_autostart", "start_component", "stop_component", "toggle_peshk0v_vpn_mode"}:
            self._ui_signals.component_action_done.emit(action_id)
        if action_id == "__settings_vpn_refresh__":
            self._finish_settings_vpn_refresh(success=False)
        if action == "prepare_general_autotest_runtime":
            self._general_test_waiting_runtime_prepare = False
            if self._general_test_running and not self._general_test_cancelled:
                self._start_next_general_test()
            return
        if action in {"run_general_diagnostics", "run_general_diagnostic_single"}:
            if self._general_test_cancelled:
                self._general_test_task_id = None
                self._general_test_eta_timer.stop()
                self._general_test_cancelled = False
                self._clear_windows_taskbar_progress()
                self._restore_general_test_runtime_after_run()
                return
            self._general_test_running = False
            self._general_test_task_id = None
            self._general_test_eta_timer.stop()
            self._clear_windows_taskbar_progress()
            if self._general_test_dialog is not None:
                self._general_test_dialog.reject()
            self._general_test_dialog = None
            self._general_test_status_label = None
            self._general_test_eta_label = None
            self._general_test_counter_label = None
            self._general_test_progress_bar = None
            self._restore_general_test_runtime_after_run()
        if action == "run_settings_diagnostics":
            self._settings_diag_task_id = None
            if self._settings_diag_dialog is not None:
                self._settings_diag_dialog.reject()
            self._settings_diag_dialog = None
            self._settings_diag_status_label = None
            self._settings_diag_progress_bar = None
        if action in {"update_zapret_runtime", "update_tg_ws_proxy_runtime"}:
            self._close_component_update_dialog()
        title = self._backend_error_title(source)
        self._add_notification(
            "error",
            title,
            error,
            source=source,
            details={"dedupe_key": f"backend-error:{source}:{action}:{error}"},
        )
        self._show_error(title, error)

    def _backend_error_source(self, action: str, fallback: str = "") -> str:
        source = (fallback or "").strip().lower()
        if source:
            return source
        normalized = (action or "").strip().lower()
        if "tg_ws_proxy" in normalized or "tg-ws-proxy" in normalized or "telegram" in normalized:
            return "tg-ws-proxy"
        if "peshk0v_vpn" in normalized or "peshk0v-vpn" in normalized or "vpn" in normalized:
            return "peshk0v-vpn"
        if "zapret" in normalized or "general" in normalized or "merge" in normalized:
            return "zapret"
        if "mod" in normalized:
            return "mods"
        if "settings" in normalized:
            return "settings"
        if "file" in normalized:
            return "files"
        return "backend"

    def _backend_error_title(self, source: str) -> str:
        label = self._backend_source_label(source)
        return self._t(f"Ошибка {label}", f"{label} error")

    def _backend_source_label(self, source: str) -> str:
        labels = {
            "tg-ws-proxy": "TG WS Proxy",
            "peshk0v-vpn": "peshk0v vpn",
            "zapret": "Zapret",
            "mods": self._t("Модификации", "Mods"),
            "settings": self._t("Настройки", "Settings"),
            "files": self._t("Файлы", "Files"),
            "backend": "Backend",
        }
        return labels.get((source or "").strip().lower(), "Backend")

    def _friendly_backend_error(self, error: str, *, source: str, action: str = "") -> str:
        text = str(error or "").strip() or self._t("Неизвестная ошибка.", "Unknown error.")
        lowered = text.lower()
        if "expecting value" in lowered and "line 1 column 1" in lowered:
            source_label = self._backend_source_label(source)
            return self._t(
                f"{source_label}: получен пустой или повреждённый JSON-ответ. Приложение уже защитило локальные данные, повторите действие. Если ошибка повторится, откройте логи - там будет указан backend action: {action or 'unknown'}.",
                f"{source_label}: an empty or corrupted JSON response was received. Local data is protected; try again. If it repeats, open logs - backend action is: {action or 'unknown'}.",
            )
        return text

    def _finish_settings_vpn_refresh(self, *, success: bool) -> None:
        dialog = self._settings_dialog
        if dialog is None:
            return
        if success:
            dialog.set_vpn_refresh_state("done")
            return
        dialog.set_vpn_refresh_state("idle")

    def _prepare_general_test_runtime_before_run(self) -> None:
        self._general_test_waiting_runtime_prepare = True
        self._general_test_runtime_restore_payload = None
        self._submit_backend_task(
            "prepare_general_autotest_runtime",
            action_id="__general_test_runtime_prepare__",
        )

    def _on_general_test_runtime_prepared(self, payload: object) -> None:
        self._general_test_waiting_runtime_prepare = False
        if isinstance(payload, dict) and isinstance(payload.get("restore_runtime"), dict):
            self._general_test_runtime_restore_payload = dict(payload.get("restore_runtime") or {})
        self._mark_dirty("dashboard", "components", "tray")
        if self._general_test_running and not self._general_test_cancelled:
            self._start_next_general_test()

    def _restore_general_test_runtime_after_run(self) -> None:
        restore_payload = self._general_test_runtime_restore_payload
        self._general_test_runtime_restore_payload = None
        self._general_test_waiting_runtime_prepare = False
        if not restore_payload:
            return
        self._submit_backend_task(
            "restore_general_autotest_runtime",
            {"restore_runtime": restore_payload},
            action_id="__general_test_runtime_restore__",
        )

    def _show_settings_diagnostics_result(self, payload: object) -> None:
        self._settings_diag_task_id = None
        if self._settings_diag_dialog is not None:
            self._settings_diag_dialog.accept()
        self._settings_diag_dialog = None
        self._settings_diag_status_label = None
        self._settings_diag_progress_bar = None
        if self._settings_diag_cancelled:
            self._settings_diag_cancelled = False
            return
        if not isinstance(payload, dict):
            self._show_error(self._t("Подобрать настройки", "Find best settings"), self._t("Не удалось получить результаты.", "Failed to get results."))
            return
        best = payload.get("best") if isinstance(payload.get("best"), dict) else None
        if not best or int(best.get("passed_targets", 0) or 0) <= 0:
            self._show_info(
                self._t("Подобрать настройки", "Find best settings"),
                self._t(
                    "Не удалось подобрать устойчивые настройки. Сначала запустите подбор конфигурации и выберите рабочую конфигурацию, затем повторите попытку.",
                    "Could not find stable settings. Run configuration selection first, choose a working configuration, and try again.",
                ),
            )
            return
        dialog = AppDialog(self, self.context, self._t("Подобрать настройки", "Find best settings"))
        summary = QLabel(
            self._t(
                f"Лучшая комбинация найдена.\n\nIPSet mode: {best.get('ipset_mode')}\nGaming mode: {best.get('game_mode')}\nУспешно: {best.get('passed_targets')}/{best.get('total_targets')}\nВремя: {best.get('elapsed')} сек.\n\nПрименить эти настройки?",
                f"Best combination found.\n\nIPSet mode: {best.get('ipset_mode')}\nGaming mode: {best.get('game_mode')}\nPassed: {best.get('passed_targets')}/{best.get('total_targets')}\nTime: {best.get('elapsed')}s.\n\nApply these settings?",
            )
        )
        summary.setWordWrap(True)
        dialog.body_layout.addWidget(summary)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_btn = QPushButton(self._t("Закрыть", "Close"))
        apply_btn = QPushButton(self._t("Применить лучшие настройки", "Apply best settings"))
        apply_btn.setProperty("class", "primary")
        close_btn.clicked.connect(dialog.reject)
        apply_btn.clicked.connect(dialog.accept)
        buttons.addWidget(close_btn)
        buttons.addWidget(apply_btn)
        dialog.body_layout.addLayout(buttons)
        dialog.prepare_and_center()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._submit_backend_task(
                "apply_settings",
                {
                    "zapret_ipset_mode": str(best.get("ipset_mode", "loaded")),
                    "zapret_game_filter_mode": str(best.get("game_mode", "disabled")),
                },
                action_id="__settings__",
            )

    def _on_backend_task_progress(self, message: dict) -> None:
        action = str(message.get("action", ""))
        payload = message.get("payload", {})
        if action == "run_general_diagnostics" and isinstance(payload, dict):
            self._ui_signals.general_test_progress.emit(
                {
                    "target_current": int(payload.get("current", 0) or 0),
                    "target_total": int(payload.get("total", 0) or 0),
                    "target_name": str(payload.get("name", "") or ""),
                    "config_index": max(1, self._general_test_next_option_index + 1),
                    "config_total": max(1, self._general_test_total),
                }
            )
        if action == "run_general_diagnostic_single" and isinstance(payload, dict):
            self._ui_signals.general_test_progress.emit(
                {
                    "target_current": int(payload.get("current", 0) or 0),
                    "target_total": int(payload.get("total", 0) or 0),
                    "target_name": str(payload.get("name", "") or ""),
                    "config_index": max(1, self._general_test_next_option_index + 1),
                    "config_total": max(1, self._general_test_total),
                }
            )
        if action == "run_settings_diagnostics" and isinstance(payload, dict):
            if self._settings_diag_progress_bar is not None:
                total = max(1, int(payload.get("total", 1) or 1))
                current = max(0, min(total, int(payload.get("current", 0) or 0)))
                self._settings_diag_progress_bar.setMaximum(total)
                self._settings_diag_progress_bar.setValue(current)
            if self._settings_diag_status_label is not None:
                self._settings_diag_status_label.setText(
                    self._t(
                        f"Проверяется: {str(payload.get('name', '') or '')}",
                        f"Checking: {str(payload.get('name', '') or '')}",
                    )
                )

    def _apply_theme(self) -> None:
        load_theme_registry(self.context.paths.themes_dir)
        theme = self.context.settings.get().theme
        self._icon_cache.clear()
        self._service_icon_cache.clear()
        self._service_check_cache.clear()
        chevron = str((self._icons_dir / "chevron_down.svg").resolve())
        check = str((self._icons_dir / "check.svg").resolve())
        self.setStyleSheet(build_stylesheet(theme, chevron_icon=chevron, check_icon=check))
        self._update_power_icon()
        if isinstance(self.power_button, AnimatedPowerButton):
            self.power_button.set_power_theme(theme)
        if self.power_aura is not None:
            self.power_aura.set_power_theme(theme)
        sidebar = self.findChild(SidebarPanel, "Sidebar")
        if sidebar is not None:
            sidebar.set_theme(theme)
        for btn in self._nav_buttons:
            if isinstance(btn, AnimatedNavButton):
                btn.set_nav_theme(theme)
        if self._github_sidebar_btn is not None:
            self._github_sidebar_btn.setIcon(self._icon("github.svg"))
            self._github_sidebar_btn.set_button_theme(theme)
        for overlay in self._scroll_fade_overlays:
            overlay.set_theme(theme)
            if getattr(overlay, "_scrollable", None) is getattr(self, "_file_tag_scroll", None):
                overlay.set_surface_color(_files_inner_surface_color(theme))
            elif getattr(overlay, "_scrollable", None) is getattr(self, "_services_scroll", None):
                overlay.set_surface_color(_content_surface_color(theme))
            elif getattr(overlay, "_scrollable", None) is getattr(self, "_onboarding_services_scroll", None):
                overlay.set_surface_color(_chrome_surface_color(theme))
            overlay._sync_state()
        if self._file_tag_scroll is not None and self._file_tag_canvas is not None:
            tag_surface = _files_inner_surface_css(theme)
            self._file_tag_scroll.setStyleSheet(
                f"QScrollArea, QScrollArea > QWidget#qt_scrollarea_viewport {{ background: {tag_surface}; border: none; }}"
            )
            self._file_tag_canvas.setStyleSheet(f"background: {tag_surface}; border: none;")
        self._sync_nav_highlight(animated=False)
        self._apply_titlebar_icons(theme)
        if self._notifications_btn is not None:
            self._notifications_btn.setIcon(self._icon("bell.svg"))

        if self.power_reconfigure_btn is not None:
            self._sync_power_reconfigure_button_style()

        self._sync_onboarding_back_button_style()
        self._refresh_notifications_badge()
        self._apply_onboarding_style()
        self._apply_file_search_style()
        if self._file_search_toggle is not None:
            self._file_search_toggle.setIcon(self._icon("search.svg"))
        self.refresh_services()
        if hasattr(self, "mods_cards_layout"):
            try:
                self.refresh_mods()
            except Exception:
                pass

    def _apply_onboarding_style(self) -> None:
        if self._content_surface is None:
            return
        theme = self.context.settings.get().theme
        onboarding_active = bool(
            self._onboarding_active
            and self._onboarding_widget is not None
        )
        self._apply_onboarding_chrome(theme, onboarding_active)
        text_color = "#000000" if onboarding_active else _onboarding_text_color(theme)
        muted_color = "#4b5d78" if onboarding_active else _onboarding_muted_color(theme)
        accent = "#4f73d9"
        accent_hover = "#5f83ea"
        chrome = "#ffffff" if onboarding_active else _chrome_surface_color(theme).name()
        if onboarding_active:
            self._apply_titlebar_icons_onboard()
        else:
            self._apply_titlebar_icons(theme)
        if self._onboarding_wrap_widget is not None:
            self._onboarding_wrap_widget.setStyleSheet("background: transparent;")
        if self._onboarding_intro_title_label is not None:
            title_color = "#2563eb" if onboarding_active else text_color
            self._onboarding_intro_title_label.setStyleSheet(
                f"color: {title_color}; background: transparent; font-size: 28px; font-weight: 820;"
            )
        if self._onboarding_intro_desc_label is not None:
            self._onboarding_intro_desc_label.setStyleSheet(f"color: {muted_color}; background: transparent; font-size: 10.5pt;")
        if self._onboarding_title_label is not None:
            title_size = 28 if self._onboarding_stage == "services" else 28
            self._onboarding_title_label.setStyleSheet(
                f"color: {text_color}; background: transparent; font-size: {title_size}px; font-weight: 820;"
            )
        if self._onboarding_desc_label is not None:
            self._onboarding_desc_label.setStyleSheet(f"color: {muted_color}; background: transparent; font-size: 10.5pt;")
        if self._onboarding_running_title_label is not None:
            self._onboarding_running_title_label.setStyleSheet(
                f"color: {text_color}; background: transparent; font-size: 28px; font-weight: 820;"
            )
        if self._onboarding_running_desc_label is not None:
            self._onboarding_running_desc_label.setStyleSheet(f"color: {muted_color}; background: transparent; font-size: 10.5pt;")
        if self._onboarding_result_title_label is not None:
            self._onboarding_result_title_label.setStyleSheet(
                f"color: {text_color}; background: transparent; font-size: 28px; font-weight: 820;"
            )
        if self._onboarding_result_desc_label is not None:
            self._onboarding_result_desc_label.setStyleSheet(f"color: {muted_color}; background: transparent; font-size: 10.5pt;")
        if self._onboarding_services_title_label is not None:
            self._onboarding_services_title_label.setStyleSheet(f"color: {text_color}; background: transparent;")
        if self._onboarding_services_hint_label is not None:
            self._onboarding_services_hint_label.setStyleSheet(f"color: {muted_color}; background: transparent;")
        if self._onboarding_services_scroll is not None:
            self._onboarding_services_scroll.setStyleSheet(
                "QScrollArea#OnboardingServicesScroll, "
                "QScrollArea#OnboardingServicesScroll > QWidget#qt_scrollarea_viewport, "
                "QWidget#OnboardingServicesCanvas { background: transparent; border: none; }"
            )
        if self._services_scroll is not None:
            self._services_scroll.setStyleSheet(
                "QScrollArea#ServicesScroll, "
                "QScrollArea#ServicesScroll > QWidget#qt_scrollarea_viewport, "
                "QWidget#ServicesCanvas { background: transparent; border: none; }"
            )
        if self._onboarding_service_action_btn is not None:
            self._onboarding_service_action_btn.set_theme(theme)
            self._onboarding_service_action_btn.set_force_light(True)
        if self._onboarding_result_card is not None:
            self._onboarding_result_card.setStyleSheet(
                "background: transparent; border: none;"
            )
        if self._onboarding_result_label is not None:
            self._onboarding_result_label.setStyleSheet(f"color: {muted_color}; background: transparent; border: none;")
        if self._onboarding_found_label is not None:
            self._onboarding_found_label.setStyleSheet(f"color: {text_color}; background: transparent; border: none;")
        if self._onboarding_progress_label is not None:
            self._onboarding_progress_label.setStyleSheet(f"color: {muted_color}; background: transparent; border: none;")
        if self._onboarding_progress_counter_label is not None:
            self._onboarding_progress_counter_label.setStyleSheet(f"color: {muted_color}; background: transparent; border: none;")
        if isinstance(self._onboarding_progress_bar, RoundedProgressBar):
            track = QColor(231, 238, 249, 210) if is_light_theme(theme) else QColor(166, 187, 222, 60)
            border = QColor("#c7d4ea") if is_light_theme(theme) else QColor("#2f4467")
            chunk_start = QColor("#4f73d9") if is_light_theme(theme) else QColor("#59c9ff")
            chunk_end = QColor("#7ea5ff") if is_light_theme(theme) else QColor("#46f4ff")
            self._onboarding_progress_bar.set_theme_colors(
                track=track,
                border=border,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
            )
            self._onboarding_progress_bar.setStyleSheet("background: transparent; border: none;")
        elif self._onboarding_progress_bar is not None:
            self._onboarding_progress_bar.setStyleSheet("background: transparent; border: none;")
        if self._onboarding_primary_btn is not None:
            self._onboarding_primary_btn.setStyleSheet(
                "QPushButton {"
                f"background: transparent; border: 1px solid {accent}; border-radius: 14px; padding: 10px 22px; color: {text_color}; font-weight: 700;"
                "}"
                "QPushButton:hover {"
                f"background: rgba(101, 132, 255, 26); border: 1px solid {accent_hover};"
                "}"
            )
        if self._onboarding_result_primary_btn is not None:
            self._onboarding_result_primary_btn.setStyleSheet(
                "QPushButton {"
                f"background: transparent; border: 1px solid {accent}; border-radius: 14px; padding: 10px 22px; color: {text_color}; font-weight: 700;"
                "}"
                "QPushButton:hover {"
                f"background: rgba(101, 132, 255, 26); border: 1px solid {accent_hover};"
                "}"
            )
        if self._onboarding_secondary_btn is not None:
            secondary_color = "rgba(25, 32, 43, 0.58)"
            self._onboarding_secondary_btn.setStyleSheet(
                f"background: transparent; border: none; padding: 6px 10px; color: {secondary_color};"
            )

    def _apply_onboarding_chrome(self, theme: str, onboarding_active: bool) -> None:
        if not onboarding_active:
            self._content_surface.setStyleSheet("")
            root_frame = self.findChild(OnboardingFrame, "RootFrame")
            if root_frame is not None:
                root_frame.set_onboarding_background(_chrome_surface_color(theme), False)
            if self._onboarding_services_fade is not None:
                self._onboarding_services_fade.set_onboarding_background_frame(None)
            title_bar = self.findChild(QFrame, "TitleBar")
            if title_bar is not None:
                title_bar.setAutoFillBackground(False)
                title_bar.setStyleSheet("")
        else:
            color = "#ffffff"
            root_frame = self.findChild(OnboardingFrame, "RootFrame")
            if root_frame is not None:
                root_frame.set_onboarding_background(QColor(color), True)
            if self._onboarding_services_fade is not None:
                self._onboarding_services_fade.set_onboarding_background_frame(root_frame)
            title_bar = self.findChild(QFrame, "TitleBar")
            if title_bar is not None:
                title_bar.setAutoFillBackground(False)
                title_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
                title_bar.setStyleSheet(
                    "QFrame#TitleBar {"
                    "background: transparent;"
                    "border: none;"
                    "border-top-left-radius: 16px;"
                    "border-top-right-radius: 16px;"
                    "}"
                    "QFrame#TitleBar QLabel, QFrame#TitleBar QToolButton {"
                    "background: transparent;"
                    "color: #2563eb;"
                    "}"
                )
            self._content_surface.setStyleSheet(
                "QFrame#ContentSurface {"
                "background: transparent;"
                "border: none;"
                "border-top-left-radius: 18px;"
                "border-top-right-radius: 0px;"
                "border-bottom-left-radius: 16px;"
                "border-bottom-right-radius: 16px;"
                "}"
            )
        chrome = _chrome_surface_color(theme).name()
        if isinstance(self._onboarding_widget, OnboardingPageWidget):
            self._onboarding_widget.set_background_color(QColor(0, 0, 0, 0) if onboarding_active else QColor(chrome))
            if self._onboarding_widget.property("onboardingPageStyleReady") is not True:
                self._onboarding_widget.setStyleSheet("QWidget#OnboardingPage { border: none; }")
                self._onboarding_widget.setProperty("onboardingPageStyleReady", True)
        elif self._onboarding_widget is not None:
            self._onboarding_widget.setStyleSheet(f"QWidget#OnboardingPage {{ background: {chrome}; border: none; }}")

    def _register_scroll_fade(self, scrollable: QAbstractScrollArea, surface_color: QColor | None = None) -> ScrollFadeOverlay:
        overlay = ScrollFadeOverlay(scrollable)
        overlay.set_theme(self.context.settings.get().theme)
        if surface_color is not None:
            overlay.set_surface_color(surface_color)
        self._scroll_fade_overlays.append(overlay)
        return overlay

    def _apply_file_search_style(self) -> None:
        panel = self._file_search_panel
        if panel is None:
            return
        theme = self.context.settings.get().theme
        surface = _content_surface_color(theme)
        bg = f"rgba({surface.red()}, {surface.green()}, {surface.blue()}, 0.94)"
        if is_light_theme(theme):
            border = "rgba(131, 159, 212, 0.95)"
            red = "rgba(239, 68, 68, 0.95)"
        else:
            border = "rgba(90, 122, 186, 0.95)"
            red = "rgba(251, 94, 94, 0.95)"
        panel.setStyleSheet(
            "QFrame#FileSearchPanel {"
            "background: transparent;"
            "border: 1px solid transparent;"
            "border-radius: 18px;"
            "}"
            "QFrame#FileSearchPanel[expanded=\"true\"] {"
            f"background: {bg};"
            f"border: 1px solid {border};"
            "border-radius: 18px;"
            "}"
            "QFrame#FileSearchPanel[expanded=\"true\"][searchState=\"empty\"] {"
            f"border-color: {red};"
            "}"
            "QFrame#FileSearchPanel QLineEdit {"
            "background: transparent;"
            "border: none;"
            "outline: none;"
            "padding: 0px 2px;"
            "margin: 0px;"
            "}"
            "QFrame#FileSearchPanel QToolButton {"
            "background: transparent;"
            "border: none;"
            "padding: 0px;"
            "margin: 0px;"
            "}"
        )

    def _register_smooth_scroll(self, scrollable: QAbstractScrollArea, *, duration: int = 170, angle_divisor: float = 2.0) -> None:
        self._smooth_scroll_helpers.append(SmoothScrollController(scrollable, duration=duration, angle_divisor=angle_divisor))

    def _apply_titlebar_icons(self, theme: str) -> None:
        if self._min_btn is None or self._close_btn is None:
            return
        suffix = "light" if is_light_theme(theme) else "dark"
        self._min_btn.setIcon(self._icon(f"window_min_{suffix}.svg"))
        self._close_btn.setIcon(self._icon(f"window_close_{suffix}.svg"))

    def _apply_titlebar_icons_onboard(self) -> None:
        if self._min_btn is None or self._close_btn is None:
            return
        assets = self.context.paths.ui_assets_dir / "icons"
        self._min_btn.setIcon(QIcon(str(assets / "window_min_onboard.svg")))
        self._close_btn.setIcon(QIcon(str(assets / "window_close_onboard.svg")))

    def _theme_status_icon_name(self) -> str:
        return "status_sun.svg" if is_light_theme(self.context.settings.get().theme) else "status_theme.svg"

    def _update_power_icon(self) -> None:
        if not hasattr(self, "power_button") or self.power_button is None:
            return
        theme = self.context.settings.get().theme
        state = str(self.power_button.property("state") or "off")
        if self._toggle_in_progress or state != "off" or not is_light_theme(theme):
            power_icon = "power_dark.svg"
        else:
            power_icon = "power_light.svg"
        self.power_button.setIcon(self._icon(power_icon))

    def _retranslate_ui(self) -> None:
        nav_tooltips = [
            self._t("Главная", "Dashboard"),
            self._t("Сервисы", "Services"),
            self._t("Компоненты", "Components"),
            self._t("Модификации", "Mods"),
            self._t("Файлы", "Files"),
            self._t("Логи", "Logs"),
        ]
        for index, btn in enumerate(self._nav_buttons):
            if index < len(nav_tooltips):
                btn.setToolTip(nav_tooltips[index])


        if self._notifications_btn is not None:
            self._notifications_btn.setToolTip(self._t("Уведомления", "Notifications"))
        if self._settings_btn is not None:
            self._settings_btn.setToolTip(self._t("Настройки", "Settings"))
        if self.power_reconfigure_btn is not None:
            self.power_reconfigure_btn.setToolTip(self._t("Подобрать настройки", "Find settings"))
        if self._onboarding_back_btn is not None:
            self._onboarding_back_btn.setToolTip(self._t("Назад", "Back"))
        if self._dashboard_title_label is not None:
            self._dashboard_title_label.setText(self._t("Быстрый доступ", "Quick Access"))
        if self._services_title_label is not None:
            self._services_title_label.setText(self._t("Выберите сервисы", "Choose services"))
        if self._services_subtitle_label is not None:
            self._services_subtitle_label.setText(
                self._t(
                    "Выберите приложения, сайты и сервисы, которыми вы пользуетесь.",
                    "Choose the apps, sites, and services you actually use.",
                )
            )
        if self._services_hint_label is not None:
                self._services_hint_label.setText(
                    self._t(
                        "Приложение автоматически настраивает свою работу для обеспечения доступа к выбранным сервисам.",
                        "The app automatically adjusts its behavior to provide access to the selected services.",
                    )
                )
        if self._components_title_label is not None:
            self._components_title_label.setText(self._t("Компоненты", "Components"))
        if self._mods_title_label is not None:
            self._mods_title_label.setText(self._t("Модификации", "Mods"))
        if self._mods_subtitle_label is not None:
            self._mods_subtitle_label.setText(
                self._t(
                    "Здесь можно аккуратно подключать свои сборки, не ломая базовую конфигурацию.",
                    "This is where you can attach your own packs without touching the base configuration.",
                )
            )
        if self._mods_add_btn is not None:
            self._mods_add_btn.setText(self._t("Добавить", "Add"))
        if hasattr(self, "mods_import_hint") and self.mods_import_hint is not None:
            self.mods_import_hint.setText(
                self._t(
                    "Можно добавить папку, ZIP, отдельные файлы или целый GitHub-репозиторий. Приложение само заберет general-файлы, списки и совместимые runtime-конфиги.",
                    "You can add a folder, ZIP, selected files, or a full GitHub repository. The app will keep general files, lists, and compatible runtime configs.",
                )
            )
        if self._files_title_label is not None:
            self._files_title_label.setText(self._t("Файлы", "Files"))
        if self._files_intro_label is not None:
            self._files_intro_label.setText(
                self._t(
                    "Выберите режим: общие и исключающие доменные листы, IP-листы, IP-исключения или полноценное редактирование файлов.",
                    "Choose the mode you need: include/exclude domain lists, IP lists, exclude IPs, or full file editing.",
                )
            )
        file_mode_texts = {
            "domains": (
                self._t("Домены", "Domains"),
                self._t(
                    "Добавляйте сервисы, которые нужно направить в общий список обхода.",
                    "Add services that should be placed into the general bypass list.",
                ),
            ),
            "exclude_domains": (
                self._t("Исключения", "Exclude domains"),
                self._t(
                    "Отдельный список доменов, которые нужно исключить из правил.",
                    "A separate list of domains that should be excluded from rules.",
                ),
            ),
            "all_ips": (
                self._t("IP-листы", "IP lists"),
                self._t(
                    "Ручной список IP и подсетей, которые нужно добавить в основной IPSet.",
                    "A manual list of IPs and subnets that should be added into the main IPSet.",
                ),
            ),
            "ips": (
                self._t("IP-исключения", "Exclude IPs"),
                self._t(
                    "Ручной список IP и подсетей, которые нужно исключить из IPSet.",
                    "A manual list of IPs and subnets to exclude from IPSet.",
                ),
            ),
            "advanced": (
                self._t("Редактирование файлов", "Advanced editor"),
                self._t(
                    "Открыть полноценный список файлов и текстовый редактор.",
                    "Open the full file list and the text editor.",
                ),
            ),
        }
        for entry in self._file_mode_cards:
            kind = str(entry.get("kind", ""))
            title_desc = file_mode_texts.get(kind)
            if not title_desc:
                continue
            title_label = entry.get("title")
            desc_label = entry.get("description")
            if isinstance(title_label, QLabel):
                title_label.setText(title_desc[0])
            if isinstance(desc_label, QLabel):
                desc_label.setText(title_desc[1])
        if self._editor_title_label is not None:
            self._editor_title_label.setText(self._t("Редактор", "Editor"))
        if self._logs_title_label is not None:
            self._logs_title_label.setText(self._t("Логи", "Logs"))
        if self._onboarding_stage == "intro":
            self._reset_onboarding_intro_state()
        elif self._onboarding_stage == "services":
            self._show_onboarding_services_stage()
        self.refresh_services()
        self._rebuild_logs_source_combo()

        title_map = {
            "app": self._t("Приложение", "App"),
            "zapret": "Zapret",
            "tg": "TG Proxy",
            "mods": "Mods",
            "theme": self._t("Тема", "Theme"),
        }
        for key, title in title_map.items():
            badge = self._status_badges.get(key)
            if badge is None:
                continue
            badge.title = title
            badge.title_label.setText(title)

        if self._tray_show_action is not None:
            self._tray_show_action.setText(self._t("Открыть", "Open"))
        if self._tray_toggle_action is not None:
            self._tray_toggle_action.setText(self._t("Компоненты", "Components"))
        if self._tray_general_menu is not None:
            self._tray_general_menu.setTitle(self._t("Конфигурация Zapret", "Zapret configuration"))
        if self._tray_quit_action is not None:
            self._tray_quit_action.setText(self._t("Выход", "Exit"))

        if hasattr(self, "files_list") and self.files_list.currentItem() is None:
            self.file_path_label.setText(self._t("Выберите файл", "Select a file"))

        self._rebuild_tray_menu()

    def _format_general_option_label(self, option: dict[str, str]) -> str:
        favorite = str(option.get("id", "")) in self._favorite_general_ids()
        bundle = (option.get("bundle") or "").strip()
        name = option.get("name", "").strip()
        label = name if not bundle else f"({bundle}) {name}"
        return f"★ {label}" if favorite else label

    def _available_mod_emojis(self) -> list[str]:
        return ["✨", "🪄", "🔥", "⚡", "🧩", "🎮", "🌐", "🛡️", "🚀", "💎", "📦", "🧪"]

    def _resolve_mod_emoji(self, mod_id: str, emoji: str) -> str:
        if mod_id == "unified-by-peshk0v":
            return "🪄"
        if emoji in self._available_mod_emojis():
            return emoji
        return self._available_mod_emojis()[abs(hash(mod_id)) % len(self._available_mod_emojis())]

    def _mod_badge_palette(self, emoji: str) -> tuple[str, str, str]:
        palettes = {
            "✨": ("#3b3115", "#d0b14d", "#fff5c7"),
            "🪄": ("#2a2444", "#7562df", "#f0ebff"),
            "🔥": ("#3b231f", "#cf6f4b", "#ffe7dd"),
            "⚡": ("#3a311b", "#cfa84d", "#fff2cc"),
            "🧩": ("#18343a", "#4ba1b3", "#dff9ff"),
            "🎮": ("#302345", "#8d69da", "#f1e6ff"),
            "🌐": ("#1b3248", "#4d88d8", "#e4f1ff"),
            "🛡️": ("#203544", "#5f8fb4", "#e7f6ff"),
            "🚀": ("#35243d", "#b16ac8", "#fdeaff"),
            "💎": ("#173945", "#5cc6da", "#e3fcff"),
            "📦": ("#3a2d1f", "#c49858", "#fff0db"),
            "🧪": ("#233b2d", "#78c48a", "#e7fff0"),
        }
        return palettes.get(emoji, palettes["🪄"])

    def _mod_badge_offset(self, emoji: str) -> tuple[float, float]:
        if emoji == "🎮":
            return 0.0, -1.0
        return 0.0, 0.0

    def _theme_adjusted_badge_palette(self, bg: str, border: str, fg: str) -> tuple[str, str, str]:
        theme = self.context.settings.get().theme
        if not is_light_theme(theme):
            return bg, border, fg
        bg_color = QColor(bg)
        border_color = QColor(border)
        fg_color = QColor(fg)
        bg_color = bg_color.lighter(168 if theme == "light" else 160)
        bg_color.setAlpha(235 if theme == "light" else 222)
        border_color = border_color.lighter(130)
        fg_color = fg_color.darker(145)
        return bg_color.name(QColor.NameFormat.HexArgb), border_color.name(), fg_color.name()

    def _emoji_popup_palette(self) -> tuple[str, str, str, str, str]:
        theme = self.context.settings.get().theme
        if theme == "light":
            return "#f5f8fe", "#c8d7ee", "#152033", "#e6eefb", "#d6e4fa"
        if theme == "light blue":
            return "#edf6ff", "#bfd6f4", "#16324f", "#dcecff", "#d0e6fb"
        if theme == "oled":
            return "#111317", "#2b3138", "#eef3ff", "#1b2028", "#263041"
        if theme == "dark":
            return "#1a1d23", "#3d4655", "#eef2fb", "#242a34", "#2b3340"
        if is_light_theme(theme):
            return "#f5f8fe", "#c8d7ee", "#152033", "#e6eefb", "#d6e4fa"
        return "#141f32", "#304463", "#eef2fb", "#1d2740", "#273349"

    def _open_mod_emoji_menu(self, mod_id: str, button: QToolButton) -> None:
        if mod_id == "unified-by-peshk0v":
            return
        if self._active_emoji_popup is not None:
            try:
                self._active_emoji_popup.close()
            except Exception:
                pass
            self._active_emoji_popup = None
        popup = QFrame(self)
        popup.setWindowFlags(Qt.WindowType.SubWindow | Qt.WindowType.FramelessWindowHint)
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        bg, border, fg, hover, selected = self._emoji_popup_palette()
        popup.setStyleSheet("QFrame { background: transparent; border: none; }")
        outer = QVBoxLayout(popup)
        outer.setContentsMargins(6, 6, 6, 6)
        frame = QFrame(popup)
        frame.setStyleSheet(
            f"background: {bg}; border: 1px solid {border}; border-radius: 14px;"
        )
        outer.addWidget(frame)
        layout = QGridLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        current = ""
        for item in self._mods_installed_cache.values():
            if item.id == mod_id:
                current = self._resolve_mod_emoji(mod_id, getattr(item, "emoji", "") or "")
                break
        for index, emoji in enumerate(self._available_mod_emojis()):
            emoji_btn = QToolButton(frame)
            emoji_btn.setText(emoji)
            emoji_btn.setCheckable(True)
            emoji_btn.setChecked(emoji == current)
            emoji_btn.setStyleSheet(
                "QToolButton {"
                f"min-width: 44px; min-height: 44px; max-width: 44px; max-height: 44px;"
                f"border-radius: 12px; background: transparent; border: 1px solid transparent;"
                f"font-size: 20px; color: {fg};"
                "}"
                "QToolButton:hover {"
                f"background: {hover}; border: 1px solid {border}; border-radius: 12px;"
                "}"
                "QToolButton:checked {"
                f"background: {selected}; border: 1px solid {border}; border-radius: 12px;"
                "}"
            )
            emoji_btn.clicked.connect(lambda _=False, mid=mod_id, e=emoji, dlg=popup: self._set_mod_emoji_immediate(mid, e, dlg))
            layout.addWidget(emoji_btn, index // 4, index % 4)
        popup.adjustSize()
        local_pos = self.mapFromGlobal(button.mapToGlobal(button.rect().bottomLeft()))
        popup.move(local_pos + QPoint(-4, 6))
        popup.raise_()
        popup.destroyed.connect(lambda *_: setattr(self, "_active_emoji_popup", None))
        self._active_emoji_popup = popup
        app = QCoreApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        popup.show()

    def _set_mod_emoji_immediate(self, mod_id: str, emoji: str, popup: QWidget | None = None) -> None:
        try:
            self._submit_backend_task("set_mod_emoji", {"mod_id": mod_id, "emoji": emoji}, action_id=f"mod-emoji:{mod_id}")
        except Exception as error:
            self._show_error(self._t("Модификации", "Mods"), str(error))
        finally:
            if popup is not None:
                popup.close()
            self._active_emoji_popup = None
            app = QCoreApplication.instance()
            if app is not None:
                try:
                    app.removeEventFilter(self)
                except Exception:
                    pass

    def _move_mod(self, mod_id: str, direction: int) -> None:
        try:
            self._submit_backend_task("move_mod", {"mod_id": mod_id, "direction": direction}, action_id=f"mod-move:{mod_id}")
        except Exception as error:
            self._show_error(self._t("Модификации", "Mods"), str(error))

    def _favorite_general_ids(self) -> list[str]:
        return list(self.context.settings.get().favorite_zapret_generals or [])

    def _is_general_favorite(self, general_id: str) -> bool:
        return general_id in set(self._favorite_general_ids())

    def _set_general_favorite(self, general_id: str, favorite: bool) -> None:
        favorites = [item for item in self._favorite_general_ids() if item]
        if favorite and general_id not in favorites:
            favorites.append(general_id)
        if not favorite:
            favorites = [item for item in favorites if item != general_id]
        self.context.settings.update(favorite_zapret_generals=favorites)

    def _invalidate_general_options_cache(self) -> None:
        self._general_options_cache = None

    def _sorted_general_options(self) -> list[dict[str, str]]:
        if self._general_options_cache is None:
            return []
        options = list(self._general_options_cache)
        favorites = {item for item in self._favorite_general_ids() if item}
        installed_order = {
            item.id: index
            for index, item in enumerate(self._mods_installed_cache.values())
            if getattr(item, "enabled", False)
        }
        def general_number(name: str) -> int:
            lowered = str(name or "").lower()
            match = re.search(r"alt\s*(\d+)", lowered)
            if match:
                return int(match.group(1))
            if lowered == "general.bat":
                return 0
            return -1

        return sorted(
            options,
            key=lambda item: (
                0 if item["id"] in favorites else 1,
                0 if str(item.get("bundle_id", "")) == "unified-general" else 2 if str(item.get("bundle_id", "")) == "base" else 1,
                installed_order.get(str(item.get("bundle_id", "")), 9999),
                -general_number(str(item.get("name", ""))),
                (item.get("name") or "").lower(),
            ),
        )

    def _general_options_for_current_service_tests(self, options: list[dict[str, str]]) -> list[dict[str, str]]:
        return prioritize_generals_for_services(options, self._selected_service_ids())

    def _start_component_loading(self, component_id: str, button: QPushButton, base_text: str) -> None:
        self._component_loading_buttons[component_id] = button
        self._component_loading_base_text[component_id] = base_text
        button.setEnabled(False)
        self._component_loading_frame = 0
        if not self._component_loading_timer.isActive():
            self._component_loading_timer.start()
        self._advance_component_loading()

    def _stop_component_loading(self, component_id: str) -> None:
        button = self._component_loading_buttons.pop(component_id, None)
        base_text = self._component_loading_base_text.pop(component_id, None)
        if button is not None:
            try:
                button.setEnabled(True)
                if base_text is not None:
                    button.setText(base_text)
            except RuntimeError:
                pass
        if not self._component_loading_buttons and self._general_loading_label is None:
            self._component_loading_timer.stop()

    def _animate_label_text(self, label: QLabel, text: str, *, duration: int = 170) -> None:
        try:
            if label.text() == text:
                return
            parent = label.parentWidget()
            if parent is None:
                label.setText(text)
                return
            old = QLabel(parent)
            old.setText(label.text())
            old.setGeometry(label.geometry())
            old.setFont(label.font())
            old.setAlignment(label.alignment())
            old.setObjectName(label.objectName())
            old.setProperty("class", label.property("class"))
            old.setStyleSheet("background: transparent;")
            old.show()
            old.raise_()
            old.style().unpolish(old)
            old.style().polish(old)
            old_opacity = QGraphicsOpacityEffect(old)
            old_opacity.setOpacity(1.0)
            old.setGraphicsEffect(old_opacity)
            fade_old = QPropertyAnimation(old_opacity, b"opacity", self)
            fade_old.setDuration(duration)
            fade_old.setStartValue(1.0)
            fade_old.setEndValue(0.0)
            fade_old.setEasingCurve(QEasingCurve.Type.InCubic)
            blur_effect = getattr(label, "_text_blur_effect", None)
            if blur_effect is None:
                blur_effect = QGraphicsBlurEffect(label)
                blur_effect.setBlurRadius(0.0)
                label.setGraphicsEffect(blur_effect)
                setattr(label, "_text_blur_effect", blur_effect)
            label.setText(text)
            blur_effect.setBlurRadius(7.0)
            blur_anim = QPropertyAnimation(blur_effect, b"blurRadius", self)
            blur_anim.setDuration(duration + 40)
            blur_anim.setStartValue(7.0)
            blur_anim.setEndValue(0.0)
            blur_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            group = QParallelAnimationGroup(self)
            group.addAnimation(fade_old)
            group.addAnimation(blur_anim)
            group.finished.connect(old.deleteLater)
            group.start()
        except Exception:
            label.setText(text)

    def _animate_caption_dots(self, dots: str, *, duration: int = 150) -> None:
        if self.power_caption_dots is None:
            return
        if self.power_caption_dots.text() == dots:
            return
        if self._power_caption_dots_opacity is None or self._power_caption_dots_blur is None:
            self.power_caption_dots.setText(dots)
            return
        fade_out = QPropertyAnimation(self._power_caption_dots_opacity, b"opacity", self)
        fade_out.setDuration(max(70, duration // 2))
        fade_out.setStartValue(float(self._power_caption_dots_opacity.opacity()))
        fade_out.setEndValue(0.0)
        fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        blur_out = QPropertyAnimation(self._power_caption_dots_blur, b"blurRadius", self)
        blur_out.setDuration(max(70, duration // 2))
        blur_out.setStartValue(float(self._power_caption_dots_blur.blurRadius()))
        blur_out.setEndValue(6.0)
        blur_out.setEasingCurve(QEasingCurve.Type.InCubic)
        out_group = QParallelAnimationGroup(self)
        out_group.addAnimation(fade_out)
        out_group.addAnimation(blur_out)

        def _show_new() -> None:
            if self.power_caption_dots is None or self._power_caption_dots_opacity is None or self._power_caption_dots_blur is None:
                return
            self.power_caption_dots.setText(dots)
            self._power_caption_dots_blur.setBlurRadius(6.0)
            fade_in = QPropertyAnimation(self._power_caption_dots_opacity, b"opacity", self)
            fade_in.setDuration(duration)
            fade_in.setStartValue(0.0)
            fade_in.setEndValue(1.0)
            fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
            blur_in = QPropertyAnimation(self._power_caption_dots_blur, b"blurRadius", self)
            blur_in.setDuration(duration)
            blur_in.setStartValue(6.0)
            blur_in.setEndValue(0.0)
            blur_in.setEasingCurve(QEasingCurve.Type.OutCubic)
            in_group = QParallelAnimationGroup(self)
            in_group.addAnimation(fade_in)
            in_group.addAnimation(blur_in)
            in_group.start()

        out_group.finished.connect(_show_new)
        out_group.start()

    def _advance_component_loading(self) -> None:
        frames = ["", ".", "..", "...", "..", "."]
        frame = frames[self._component_loading_frame % len(frames)]
        self._component_loading_frame += 1
        for button in list(self._component_loading_buttons.values()):
            try:
                button.setText(frame)
            except RuntimeError:
                continue
        if self._general_loading_label is not None:
            try:
                self._general_loading_label.setText(f"{self._t('Применение', 'Applying')}{frame}")
            except RuntimeError:
                self._general_loading_label = None
        if not self._component_loading_buttons and self._general_loading_label is None:
            self._component_loading_timer.stop()

    def _minimize_window_native(self) -> None:
        self._animate_window_fade(showing=False, action="minimize")

    def _selected_component_id(self) -> str | None:
        item = self.components_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _selected_mod_id(self) -> str | None:
        if not hasattr(self, "mods_list"):
            return None
        item = self.mods_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _open_files_mode(self, mode: str) -> None:
        if self._file_mode_stack is None:
            return
        if mode == "home":
            self._cancel_file_tag_render()
            self._current_file_list_filter = "all"
            self._file_mode_stack.setCurrentIndex(0)
            self._set_files_mode_loading(False)
            self._toggle_file_search(False)
            if self._files_home_scroll is not None:
                self._files_home_scroll.verticalScrollBar().setValue(0)
            QTimer.singleShot(0, self._sync_files_home_layout)
            return
        if mode in {"advanced", "hosts", "generals"}:
            self._cancel_file_tag_render()
            self._current_file_list_filter = "generals" if mode == "generals" else ("hosts" if mode == "hosts" else "all")
            self._preferred_file_path = str(self.context.files.local_hosts_path()) if mode == "hosts" else ""
            self._file_mode_stack.setCurrentIndex(2)
            self._use_file_search_variant("document")
            self._file_search_mode = "document"
            self.file_path_label.setText(
                self._t("Загрузка General...", "Loading General...")
                if mode == "generals"
                else ("Hosts" if mode == "hosts" else self._t("Загрузка файлов...", "Loading files..."))
            )
            self.file_editor.clear()
            self.files_list.clear()
            self._set_files_mode_loading(True)
            QTimer.singleShot(0, lambda: self._request_page_refresh("files"))
            return
        self._cancel_file_tag_render()
        self._current_file_list_filter = "all"
        self._use_file_search_variant("tags")
        self._file_search_mode = "tags"
        self._current_file_collection = mode
        self._apply_file_collection_meta()
        self._current_file_values_cache = []
        self._render_file_tags([])
        self._file_mode_stack.setCurrentIndex(1)
        self._set_files_mode_loading(True)
        if self._file_tag_scroll is not None:
            self._file_tag_scroll.verticalScrollBar().setValue(0)
        QTimer.singleShot(0, lambda: self._request_page_refresh("files"))

    def _cancel_files_mode_transition(self) -> None:
        if self._files_mode_transition_out is not None:
            try:
                self._files_mode_transition_out.stop()
            except Exception:
                pass
        if self._files_mode_transition_in is not None:
            try:
                self._files_mode_transition_in.stop()
            except Exception:
                pass
        self._files_mode_transition_out = None
        self._files_mode_transition_in = None
        self._files_mode_transition_running = False
        if self._files_mode_opacity_effect is not None:
            self._files_mode_opacity_effect.setOpacity(1.0)

    def _switch_files_mode_index(
        self,
        index: int,
        *,
        before: callable | None = None,
        after: callable | None = None,
    ) -> None:
        stack = self._file_mode_stack
        if stack is None:
            return
        if before is not None:
            before()
        if stack.currentIndex() != index:
            stack.setCurrentIndex(index)
        if index == 1:
            self._refresh_file_collection_view_with_values(self._current_file_values_cache)
        if after is not None:
            QTimer.singleShot(0, after)
        if self._file_search_shell is not None:
            self._file_search_shell.raise_()

    def _refresh_file_collection_view(self) -> None:
        self._refresh_file_collection_view_with_values(self._current_file_values_cache)

    def _sync_files_home_layout(self) -> None:
        if self._files_home_scroll is None:
            return
        host = self._files_home_host if hasattr(self, "_files_home_host") else self._files_home_scroll.widget()
        viewport = self._files_home_scroll.viewport()
        if host is None or viewport is None:
            return
        viewport_width = viewport.width()
        if viewport_width <= 0:
            return
        target_host_width = max(0, viewport_width)
        host.setFixedWidth(target_host_width)
        if host.layout() is not None:
            host.layout().activate()
        viewport.update()

    def _prepare_files_page_geometry(self) -> None:
        if self._file_mode_stack is not None and self._file_mode_stack.layout() is not None:
            self._file_mode_stack.layout().activate()
        self._sync_files_home_layout()
        if self._file_tag_scroll is not None and self._file_mode_stack is not None and self._file_mode_stack.currentIndex() == 1:
            self._sync_file_tag_canvas_geometry()
        if self._file_search_shell is not None:
            self._file_search_shell.raise_()

    def _build_components_payload_sync(self) -> dict[str, object]:
        try:
            if self._component_defs_cache or self._component_states_cache:
                return {
                    "components": [asdict(item) for item in self._component_defs_cache.values()],
                    "states": [asdict(item) for item in self._component_states_cache.values()],
                }
            return {
                "components": [asdict(item) for item in self.context.processes.list_components()],
                "states": [asdict(item) for item in self.context.processes.list_states()],
            }
        except Exception as error:
            self.context.logging.log("error", "Synchronous components payload build failed", error=str(error))
            return {}

    def _build_components_cached_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"components": [], "states": []}
        if self._component_defs_cache:
            payload["components"] = [asdict(item) for item in self._component_defs_cache.values()]
        if self._component_states_cache:
            payload["states"] = [asdict(item) for item in self._component_states_cache.values()]
        return payload

    def _build_files_payload_sync(self, mode_index: int, collection_id: str) -> dict[str, object]:
        file_filter = self._current_file_list_filter
        return {
            "mode_index": mode_index,
            "collection_id": collection_id,
            "file_filter": file_filter,
            "records": self._file_records_for_filter_sync(file_filter) if mode_index == 2 else None,
            "collection_values": self.context.files.read_collection(collection_id) if mode_index == 1 else None,
        }

    def _refresh_file_collection_view_with_values(self, values: list[str] | None, *, finish_loading: bool = False) -> None:
        self._apply_file_collection_meta()
        if self._file_tag_input is not None:
            self._file_tag_input.clear()
        self._render_file_tags(values, finish_loading=finish_loading)
        if self._file_search_shell is not None:
            self._file_search_shell.raise_()

    def _apply_file_collection_meta(self) -> None:
        titles = {
            "domains": (
                self._t("Домены", "Domains"),
                self._t(
                    "Добавляйте домены, которые нужно включить в пользовательский список обхода.",
                    "Add domains that should be included in the user bypass list.",
                ),
            ),
            "exclude_domains": (
                self._t("Исключения", "Exclude domains"),
                self._t(
                    "Здесь можно указать домены, которые нужно исключить из правил Zapret.",
                    "Here you can list domains that should be excluded from Zapret rules.",
                ),
            ),
            "all_ips": (
                self._t("IP-листы", "IP lists"),
                self._t(
                    "Здесь можно указать IP-адреса и подсети, которые должны попадать в основной IPSet.",
                    "Here you can list IP addresses and subnets that should be included in the main IPSet.",
                ),
            ),
            "ips": (
                self._t("IP-исключения", "Exclude IPs"),
                self._t(
                    "Добавляйте IP-адреса и подсети, которые нужно исключить из IPSet.",
                    "Add IP addresses and subnets that should be excluded from IPSet.",
                ),
            ),
        }
        title, subtitle = titles.get(self._current_file_collection, (self._t("Файлы", "Files"), ""))
        if self._file_tag_title is not None:
            self._file_tag_title.setText(title)
        if self._file_tag_subtitle is not None:
            self._file_tag_subtitle.setText(subtitle)
        if self._file_tag_input is not None:
            placeholder = self._t("Введите значение и нажмите Enter", "Type a value and press Enter")
            if self._current_file_collection in {"domains", "exclude_domains"}:
                placeholder = self._t("Введите домен и нажмите Enter", "Type a domain and press Enter")
            elif self._current_file_collection in {"all_ips", "ips"}:
                placeholder = self._t("Введите IP или подсеть и нажмите Enter", "Type an IP or subnet and press Enter")
            self._file_tag_input.setPlaceholderText(placeholder)

    def _cancel_file_tag_render(self) -> None:
        self._file_tag_render_generation += 1
        try:
            self._file_tag_render_timer.stop()
        except Exception:
            pass
        self._file_tag_render_values = []
        self._file_tag_render_index = 0
        self._file_tag_render_finish_loading = False
        self._file_tag_render_summary = ""
        self._file_tag_display_signature = None

    def _clear_file_tag_widgets(self) -> None:
        if self._file_tag_flow is None:
            return
        while self._file_tag_flow.count():
            item = self._file_tag_flow.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _create_file_tag_chip(self, value: str) -> QFrame:
        chip = QFrame()
        chip.setProperty("class", "modMeta")
        chip.setProperty("tagValue", value)
        chip.setProperty("searchState", "idle")
        chip.setMinimumHeight(42)
        chip.setStyleSheet(
            "QFrame { border-radius: 14px; border: 1px solid rgba(79, 96, 128, 0.24); background: rgba(79, 96, 128, 0.12); }"
            "QFrame[searchState=\"match\"] { border-color: rgba(126, 164, 255, 0.62); background: rgba(126, 164, 255, 0.08); }"
            "QFrame[searchState=\"active\"] { border-color: rgba(88, 101, 242, 0.95); background: rgba(88, 101, 242, 0.18); }"
        )
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(12, 6, 8, 6)
        chip_layout.setSpacing(8)
        label = QLabel(value)
        label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        label.setStyleSheet("background: transparent; border: none; padding: 0px; margin: 0px;")
        chip_layout.addWidget(label)
        if not self.context.files.is_managed_collection_value(self._current_file_collection, value):
            remove_btn = QToolButton()
            remove_btn.setProperty("class", "action")
            remove_btn.setText("×")
            remove_btn.setFixedSize(18, 18)
            remove_btn.setProperty("hoverRadius", 9)
            remove_btn.setStyleSheet(
                "QToolButton { background: transparent; border: none; padding: 0px; margin: 0px; font-size: 14px; font-weight: 600; }"
            )
            remove_btn.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
            remove_btn.clicked.connect(lambda _=False, item=value: self._remove_file_tag(item))
            self._attach_button_animations(remove_btn)
            chip_layout.addWidget(remove_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return chip

    def _create_file_tag_summary_chip(self, text: str) -> QFrame:
        chip = QFrame()
        chip.setProperty("class", "modMeta")
        chip.setProperty("searchState", "idle")
        chip.setMinimumHeight(42)
        chip.setStyleSheet(
            "QFrame { border-radius: 14px; border: 1px solid rgba(126, 164, 255, 0.34); background: rgba(126, 164, 255, 0.08); }"
        )
        layout = QHBoxLayout(chip)
        layout.setContentsMargins(12, 6, 12, 6)
        label = QLabel(text)
        label.setProperty("class", "muted")
        label.setStyleSheet("background: transparent; border: none; padding: 0px; margin: 0px;")
        layout.addWidget(label)
        return chip

    def _visible_file_tag_values(self, values: list[str]) -> tuple[list[str], str]:
        limit = max(100, int(self._file_tag_display_limit))
        query = ""
        if (
            self._file_search_mode == "tags"
            and self._file_search_expanded
            and self._file_search_input is not None
        ):
            query = self._file_search_input.text().strip().lower()
        if query:
            matched = [value for value in values if query in value.lower()]
            if len(matched) > limit:
                return matched[:limit], self._t(
                    f"Показано первые {limit} из {len(matched)} совпадений. Уточните поиск, чтобы сузить список.",
                    f"Showing first {limit} of {len(matched)} matches. Refine search to narrow the list.",
                )
            if len(values) > limit:
                return matched, self._t(
                    f"Найдено {len(matched)} из {len(values)} значений.",
                    f"Found {len(matched)} of {len(values)} values.",
                )
            return matched, ""
        if len(values) > limit:
            return values[:limit], self._t(
                f"Показано первые {limit} из {len(values)} значений. Используйте поиск, чтобы быстро найти нужный IP.",
                f"Showing first {limit} of {len(values)} values. Use search to quickly find the IP you need.",
            )
        return values, ""

    def _render_file_tags(self, values: list[str] | None = None, *, finish_loading: bool = False) -> None:
        if self._file_tag_flow is None:
            return
        resolved_values = list(values if values is not None else self._current_file_values_cache)
        visible_values, summary = self._visible_file_tag_values(resolved_values)
        search_query = ""
        if self._file_search_mode == "tags" and self._file_search_input is not None:
            search_query = self._file_search_input.text().strip().lower()
        display_signature = (search_query, len(visible_values), summary)
        if (
            resolved_values == self._current_file_values_cache
            and self._file_tag_flow.count() == len(visible_values) + (1 if summary else 0)
            and self._file_tag_display_signature == display_signature
            and len(resolved_values) > 0
        ):
            if self._file_search_mode == "tags" and self._file_search_expanded and self._file_search_input is not None and self._file_search_input.text().strip():
                self._refresh_file_search_matches()
            if finish_loading:
                self._set_files_mode_loading(False)
            return
        self._cancel_file_tag_render()
        self._clear_file_tag_widgets()
        self._current_file_values_cache = resolved_values
        self._file_tag_display_signature = display_signature
        self._file_tag_render_values = list(visible_values)
        self._file_tag_render_index = 0
        self._file_tag_render_finish_loading = finish_loading
        self._file_tag_render_summary = summary
        if not self._file_tag_render_values:
            if summary:
                self._file_tag_flow.addWidget(self._create_file_tag_summary_chip(summary))
            if finish_loading:
                self._set_files_mode_loading(False)
            self._sync_file_tag_canvas_geometry()
            if self._file_search_mode == "tags" and self._file_search_expanded and self._file_search_input is not None and self._file_search_input.text().strip():
                self._refresh_file_search_matches()
            return
        self._file_tag_render_timer.start(0)

    def _render_file_tags_chunk(self) -> None:
        if self._file_tag_flow is None or self._file_tag_canvas is None:
            return
        render_generation = self._file_tag_render_generation
        values = self._file_tag_render_values
        if not values:
            return
        chunk_size = 120
        start = self._file_tag_render_index
        end = min(start + chunk_size, len(values))
        for value in values[start:end]:
            self._file_tag_flow.addWidget(self._create_file_tag_chip(value))
        self._file_tag_render_index = end
        if start == 0 and self._file_tag_render_finish_loading:
            self._set_files_mode_loading(False)
        if start == 0 or end >= len(values) or end % 600 == 0:
            self._sync_file_tag_canvas_geometry()
        self._file_tag_canvas.update()
        if self._file_tag_scroll is not None:
            self._file_tag_scroll.viewport().update()
        if end < len(values):
            if render_generation == self._file_tag_render_generation:
                self._file_tag_render_timer.start(0)
            return
        if self._file_tag_render_summary:
            self._file_tag_flow.addWidget(self._create_file_tag_summary_chip(self._file_tag_render_summary))
            self._file_tag_render_summary = ""
        self._file_tag_render_values = []
        self._file_tag_render_index = 0
        finish_loading = self._file_tag_render_finish_loading
        self._file_tag_render_finish_loading = False
        if finish_loading:
            self._set_files_mode_loading(False)
        if self._file_search_mode == "tags" and self._file_search_expanded and self._file_search_input is not None and self._file_search_input.text().strip():
            self._refresh_file_search_matches()

    def _sync_file_tag_canvas_geometry(self) -> None:
        if self._file_tag_scroll is None or self._file_tag_canvas is None or self._file_tag_flow is None:
            return
        self._file_tag_canvas.adjustSize()
        target_width = max(0, self._file_tag_scroll.viewport().width())
        if target_width > 0 and self._file_tag_canvas.width() != target_width:
            self._file_tag_canvas.resize(target_width, self._file_tag_canvas.sizeHint().height())
        self._file_tag_canvas.setMinimumHeight(self._file_tag_canvas.sizeHint().height())
        self._file_tag_scroll.viewport().update()

    def _commit_tag_input(self) -> None:
        if self._file_tag_input is None:
            return
        raw = self._file_tag_input.text().strip()
        if not raw:
            return
        self._file_tag_input.clear()
        self._submit_backend_task(
            "add_collection_values",
            {
                "collection_id": self._current_file_collection,
                "raw": raw,
            },
            action_id="__files_collection__",
        )

    def _remove_file_tag(self, value: str) -> None:
        self._submit_backend_task(
            "remove_collection_value",
            {
                "collection_id": self._current_file_collection,
                "value": value,
            },
            action_id="__files_collection__",
        )

    def _reset_all_file_overrides(self) -> None:
        confirmed = self._ask_yes_no(
            self._t("Сбросить изменения", "Reset changes"),
            self._t(
                "Точно вы хотите сбросить все изменения? Это удалит все пользовательские правки, сделанные в разделе Файлы.",
                "Are you sure you want to reset all changes? This will remove all user edits made in the Files section.",
            ),
        )
        if not confirmed:
            return
        self._current_file_values_cache = []
        self._submit_backend_task(
            "reset_user_overrides",
            {"collection_id": self._current_file_collection},
            action_id="__files_collection__",
        )

    def _restart_zapret_if_running(self) -> None:
        try:
            states = self._component_states()
            if states.get("zapret") and states["zapret"].status == "running":
                self._submit_backend_task("restart_zapret_if_running")
        except Exception:
            return

    def _selected_file_path(self) -> str | None:
        item = self.files_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _toggle_master_runtime(self) -> None:
        if self._toggle_in_progress or not self._startup_snapshot_ready:
            return
        self._sync_power_aura_geometry()
        states = self._component_states()
        active_ids = self._master_active_components()
        running_ids = {cid for cid in active_ids if states.get(cid) and states[cid].status == "running"}
        self._loading_action = "disconnect" if running_ids else "connect"
        self._toggle_in_progress = True
        self.power_button.setEnabled(False)
        if isinstance(self.power_button, AnimatedPowerButton):
            self.power_button.play_wave(outward=self._loading_action == "connect")
        if self.power_aura is not None:
            self.power_aura.play_wave(outward=self._loading_action == "connect")
        self._loading_frame = 0
        self._loading_timer.start()
        self._advance_loading_caption()
        self._submit_backend_task("toggle_master_runtime")

    def _toggle_master_runtime_worker(self) -> None:
        try:
            states = self._component_states()
            active_ids = self._master_active_components()
            if not active_ids:
                return
            running_ids = {cid for cid in active_ids if states.get(cid) and states[cid].status == "running"}
            if running_ids:
                for cid in list(running_ids):
                    self.context.processes.stop_component(cid)
            else:
                for cid in active_ids:
                    self.context.processes.start_component(cid)
        finally:
            self._ui_signals.toggle_done.emit()

    def _on_master_toggle_finished(self) -> None:
        self._loading_timer.stop()
        self._toggle_in_progress = False
        self.power_button.setEnabled(bool(self._startup_snapshot_ready))
        self._update_power_icon()
        self.refresh_all()
        if self._pending_info_message is not None:
            title, text = self._pending_info_message
            self._pending_info_message = None
            self._show_info(title, text)

    def _advance_loading_caption(self) -> None:
        if not self._toggle_in_progress:
            return
        base = self._t("Подключение", "Connecting") if self._loading_action == "connect" else self._t("Отключение", "Disconnecting")
        dots_frames = ["", ".", "..", "...", "..", "."]
        full_text = f"{base}{dots_frames[self._loading_frame % len(dots_frames)]}"
        if self.power_caption_dots is not None:
            self.power_caption_dots.setText("")
            self.power_caption_dots.hide()
        if self.power_caption_text is not None:
            self._set_power_status_pill(full_text, "loading")
        self._power_caption_base_text = base
        self._loading_frame += 1
        self.power_button.setProperty("state", "loading")
        if isinstance(self.power_button, AnimatedPowerButton):
            self.power_button.set_loading_state(True, animate=True)
        if self.power_aura is not None:
            self.power_aura.set_idle_pulse_enabled(False)
            self.power_aura.set_status_glow_enabled(True)
        self._update_power_icon()

    def _start_selected_component(self) -> None:
        component_id = self._selected_component_id()
        if component_id:
            self._submit_backend_task("start_component", {"component_id": component_id}, action_id=component_id)

    def _stop_selected_component(self) -> None:
        component_id = self._selected_component_id()
        if component_id:
            self._submit_backend_task("stop_component", {"component_id": component_id}, action_id=component_id)

    def _toggle_selected_component_enabled(self) -> None:
        component_id = self._selected_component_id()
        if component_id:
            self._submit_backend_task("toggle_component_enabled", {"component_id": component_id}, action_id=component_id)

    def _toggle_selected_component_autostart(self) -> None:
        component_id = self._selected_component_id()
        if component_id:
            self._submit_backend_task("toggle_component_autostart", {"component_id": component_id}, action_id=component_id)

    def _toggle_component_card(self, component_id: str, button: QPushButton) -> None:
        if component_id in self._component_loading_buttons:
            return
        self._start_component_loading(component_id, button, button.text())
        self._submit_backend_task("toggle_component_enabled", {"component_id": component_id}, action_id=component_id)

    def _toggle_component_card_worker(self, component_id: str) -> None:
        self._submit_backend_task("toggle_component_enabled", {"component_id": component_id}, action_id=component_id)

    def _install_selected_mod(self) -> None:
        mod_id = self._selected_mod_id()
        if mod_id:
            self._submit_backend_task("install_mod", {"mod_id": mod_id}, action_id=f"mod-install:{mod_id}")

    def _toggle_selected_mod(self) -> None:
        mod_id = self._selected_mod_id()
        if not mod_id:
            return
        installed = dict(self._mods_installed_cache)
        if mod_id not in installed:
            self._show_info(self._t("Модификация", "Mod"), self._t("Сначала установите модификацию, затем включайте её.", "Install selected mod before enabling it."))
            return
        self._submit_backend_task("toggle_mod", {"mod_id": mod_id}, action_id=f"mod:{mod_id}")

    def _remove_selected_mod(self) -> None:
        mod_id = self._selected_mod_id()
        if mod_id:
            self._submit_backend_task("remove_mod", {"mod_id": mod_id}, action_id=f"mod-remove:{mod_id}")

    def _import_mod_any(self) -> None:
        previous_selected_general = str(self.context.settings.get().selected_zapret_general or "")
        chooser = AppDialog(self, self.context, self._t("Добавить модификацию", "Add modification"))
        chooser.setMinimumWidth(520)
        chooser_text = QLabel(
            self._t(
                "Выберите удобный источник. Хаб сам вытащит только совместимые TXT, PS1 и BAT-файлы.",
                "Choose the source you want. The hub will keep only compatible TXT, PS1, and BAT files.",
            )
        )
        chooser_text.setWordWrap(True)
        chooser_text.setProperty("class", "muted")
        chooser.body_layout.addWidget(chooser_text)

        buttons = QGridLayout()
        buttons.setHorizontalSpacing(10)
        buttons.setVerticalSpacing(10)
        folder_btn = QPushButton(self._t("Папка", "Folder"))
        folder_btn.setProperty("class", "primary")
        zip_btn = QPushButton(self._t("ZIP-архив", "ZIP archive"))
        zip_btn.setProperty("class", "primary")
        files_btn = QPushButton(self._t("Файл(ы)", "File(s)"))
        files_btn.setProperty("class", "primary")
        github_btn = QPushButton(self._t("GitHub", "GitHub"))
        github_btn.setProperty("class", "primary")
        cancel_btn = QPushButton(self._t("Отмена", "Cancel"))
        self._attach_button_animations(folder_btn)
        self._attach_button_animations(zip_btn)
        self._attach_button_animations(files_btn)
        self._attach_button_animations(github_btn)
        self._attach_button_animations(cancel_btn)
        buttons.addWidget(folder_btn, 0, 0)
        buttons.addWidget(zip_btn, 0, 1)
        buttons.addWidget(files_btn, 1, 0)
        buttons.addWidget(github_btn, 1, 1)
        buttons.addWidget(cancel_btn, 2, 0, 1, 2)
        chooser.body_layout.addLayout(buttons)

        selected_kind: dict[str, str] = {"kind": ""}
        folder_btn.clicked.connect(lambda: (selected_kind.__setitem__("kind", "folder"), chooser.accept()))
        zip_btn.clicked.connect(lambda: (selected_kind.__setitem__("kind", "zip"), chooser.accept()))
        files_btn.clicked.connect(lambda: (selected_kind.__setitem__("kind", "files"), chooser.accept()))
        github_btn.clicked.connect(lambda: (selected_kind.__setitem__("kind", "github"), chooser.accept()))
        cancel_btn.clicked.connect(chooser.reject)
        chooser.prepare_and_center()
        if chooser.exec() != QDialog.DialogCode.Accepted:
            return

        path = ""
        paths: list[str] = []
        if selected_kind["kind"] == "folder":
            path = QFileDialog.getExistingDirectory(self, self._t("Выберите папку модификации", "Select modification folder"))
            if path:
                paths = [path]
        elif selected_kind["kind"] == "zip":
            path, _ = QFileDialog.getOpenFileName(
                self,
                self._t("Выберите ZIP-архив модификации", "Select modification ZIP archive"),
                filter=self._t("ZIP-архив (*.zip)", "ZIP archive (*.zip)"),
            )
            if path:
                paths = [path]
        elif selected_kind["kind"] == "files":
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                self._t("Выберите файлы модификации", "Select modification files"),
                filter=self._t(
                    "Совместимые файлы (*.txt *.ps1 *.bat);;Все файлы (*.*)",
                    "Compatible files (*.txt *.ps1 *.bat);;All files (*.*)",
                ),
            )
        elif selected_kind["kind"] == "github":
            repo_url = self._ask_text_value(
                self._t("GitHub-модификация", "GitHub modification"),
                self._t("Вставьте ссылку на GitHub-репозиторий.", "Paste a GitHub repository link."),
                self._t("Например: https://github.com/user/repo", "Example: https://github.com/user/repo"),
            )
            if not repo_url:
                return
            try:
                self._submit_backend_task(
                    "import_mod_from_github",
                    {
                        "repo_url": repo_url,
                        "previous_selected_general": previous_selected_general,
                    },
                    action_id="__mods_import__",
                )
            except Exception as error:
                self._show_error(self._t("Модификации", "Mods"), f"{self._t('Не удалось импортировать репозиторий', 'Failed to import repository')}:\n{error}")
            return

        if not paths:
            return
        try:
            self._submit_backend_task(
                "import_mod_from_paths",
                {
                    "paths": paths,
                    "previous_selected_general": previous_selected_general,
                },
                action_id="__mods_import__",
            )
        except Exception as error:
            self._show_error(self._t("Модификации", "Mods"), f"{self._t('Не удалось импортировать модификацию', 'Failed to import modification')}:\n{error}")

    def _create_mod_dialog(self) -> None:
        name = self._ask_text_value(
            self._t("Новая модификация", "New modification"),
            self._t("Введите название модификации.", "Enter modification name."),
            self._t("Например: My game fix", "Example: My game fix"),
        )
        if not name:
            return
        author = self._ask_text_value(
            self._t("Автор модификации", "Modification author"),
            self._t("Кого указать автором? Если оставить пустым, будет указано «неизвестен».", "Who should be listed as author? Leave empty to use \"unknown\"."),
            self._t("неизвестен", "unknown"),
        ) or self._t("неизвестен", "unknown")
        try:
            entry = self.context.mods.create_empty(name=name, author=author)
            self._mark_dirty("mods", "components", "files")
            self._request_page_refresh("mods")
            self._open_mod_editor(entry.id)
        except Exception as error:
            self._show_error(self._t("Модификации", "Mods"), str(error))

    def _open_mod_editor(self, mod_id: str) -> None:
        if mod_id == "unified-by-peshk0v":
            return
        try:
            installed = {item.id: item for item in self.context.mods.list_installed()}
            entry = installed[mod_id]
            files = self.context.mods.list_files(mod_id)
        except Exception as error:
            self._show_error(self._t("Модификации", "Mods"), str(error))
            return

        dialog = AppDialog(self, self.context, self._t("Редактор модификации", "Modification editor"))
        dialog.setMinimumSize(760, 560)

        form = QFormLayout()
        name_input = QLineEdit(entry.name or entry.id)
        author_input = QLineEdit(entry.author or self._t("неизвестен", "unknown"))
        version_input = QLineEdit(entry.version or datetime.utcnow().strftime("%Y.%m.%d"))
        description_input = QTextEdit(entry.description or "")
        description_input.setFixedHeight(86)
        form.addRow(self._t("Название", "Name"), name_input)
        form.addRow(self._t("Автор", "Author"), author_input)
        form.addRow(self._t("Версия", "Version"), version_input)
        form.addRow(self._t("Описание", "Description"), description_input)
        dialog.body_layout.addLayout(form)

        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(12)
        files_list = QListWidget()
        files_list.setObjectName("ModFilesList")
        files_list.setMinimumWidth(260)
        files_list.setMaximumWidth(300)
        files_list.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        files_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        files_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        files_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        editor = QTextEdit()
        editor.setObjectName("FileEditor")
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        current_path: dict[str, str] = {"path": ""}
        modified: dict[str, bool] = {"value": False}

        def reload_files() -> None:
            files_list.clear()
            try:
                fresh = self.context.mods.list_files(mod_id)
            except Exception:
                fresh = []
            for item in fresh:
                rel = str(item.get("path", ""))
                row = QListWidgetItem(f"{rel}\n{self._t('Размер', 'Size')}: {item.get('size', 0)}")
                row.setData(Qt.ItemDataRole.UserRole, rel)
                row.setToolTip(rel)
                files_list.addItem(row)

        def select_file(item: QListWidgetItem | None) -> None:
            rel = str(item.data(Qt.ItemDataRole.UserRole) if item else "")
            current_path["path"] = rel
            if not rel:
                editor.clear()
                return
            try:
                editor.setPlainText(self.context.mods.read_file(mod_id, rel))
            except Exception as error:
                self._show_error(self._t("Файл модификации", "Mod file"), str(error))

        files_list.currentItemChanged.connect(lambda item, _prev=None: select_file(item))
        reload_files()
        files_fade = ScrollFadeOverlay(files_list)
        files_fade.set_surface_color(_dialog_surface_color(self.context.settings.get().theme))
        editor_fade = ScrollFadeOverlay(editor)
        editor_fade.set_surface_color(_dialog_surface_color(self.context.settings.get().theme))
        dialog._scroll_fade_overlays = [files_fade, editor_fade]  # type: ignore[attr-defined]
        self._smooth_scroll_helpers.append(SmoothScrollController(files_list))
        self._smooth_scroll_helpers.append(SmoothScrollController(editor))
        split.addWidget(files_list, 1)
        split.addWidget(editor, 2)
        dialog.body_layout.addLayout(split, 1)

        buttons = QHBoxLayout()
        save_meta_btn = QPushButton(self._t("Сохранить данные", "Save details"))
        add_file_btn = QPushButton(self._t("Добавить файл", "Add file"))
        save_file_btn = QPushButton(self._t("Сохранить файл", "Save file"))
        delete_file_btn = QPushButton(self._t("Удалить файл", "Delete file"))
        close_btn = QPushButton(self._t("Закрыть", "Close"))
        for btn in (save_meta_btn, add_file_btn, save_file_btn, delete_file_btn, close_btn):
            self._attach_button_animations(btn)
            buttons.addWidget(btn)
        dialog.body_layout.addLayout(buttons)

        def save_metadata() -> None:
            try:
                self.context.mods.update_metadata(
                    mod_id,
                    name=name_input.text(),
                    description=description_input.toPlainText(),
                    author=author_input.text(),
                    version=version_input.text(),
                )
                self._mark_dirty("mods")
                modified["value"] = True
            except Exception as error:
                self._show_error(self._t("Модификации", "Mods"), str(error))

        def add_file() -> None:
            rel = self._ask_text_value(
                self._t("Новый файл", "New file"),
                self._t("Путь внутри модификации.", "Path inside the modification."),
                "lists/list-general.txt",
            )
            if not rel:
                return
            try:
                self.context.mods.write_file(mod_id, rel, "")
                reload_files()
                self._mark_dirty("mods", "components", "files")
                modified["value"] = True
            except Exception as error:
                self._show_error(self._t("Файл модификации", "Mod file"), str(error))

        def save_file() -> None:
            rel = current_path["path"]
            if not rel:
                return
            try:
                self.context.mods.write_file(mod_id, rel, editor.toPlainText())
                reload_files()
                self._mark_dirty("mods", "components", "files")
                modified["value"] = True
            except Exception as error:
                self._show_error(self._t("Файл модификации", "Mod file"), str(error))

        def delete_file() -> None:
            rel = current_path["path"]
            if not rel:
                return
            try:
                self.context.mods.delete_file(mod_id, rel)
                current_path["path"] = ""
                editor.clear()
                reload_files()
                self._mark_dirty("mods", "components", "files")
                modified["value"] = True
            except Exception as error:
                self._show_error(self._t("Файл модификации", "Mod file"), str(error))

        save_meta_btn.clicked.connect(save_metadata)
        add_file_btn.clicked.connect(add_file)
        save_file_btn.clicked.connect(save_file)
        delete_file_btn.clicked.connect(delete_file)
        close_btn.clicked.connect(dialog.accept)
        dialog.prepare_and_center()
        dialog.exec()
        if modified["value"]:
            self._request_page_refresh("mods")

    def _import_mod_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select mod folder")
        if not path:
            return
        try:
            self._submit_backend_task("import_mod_from_path", {"path": path}, action_id="__mods_import__")
        except Exception as error:
            self._show_error(self._t("Модификации", "Mods"), f"{self._t('Не удалось импортировать папку', 'Failed to import folder')}:\n{error}")

    def _import_mod_archive(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select mod archive", filter="ZIP archive (*.zip)")
        if not path:
            return
        try:
            self._submit_backend_task("import_mod_from_path", {"path": path}, action_id="__mods_import__")
        except Exception as error:
            self._show_error(self._t("Модификации", "Mods"), f"{self._t('Не удалось импортировать архив', 'Failed to import archive')}:\n{error}")

    def _rebuild_runtime(self) -> None:
        self._submit_backend_task("rebuild_merge_runtime", action_id="__merge_rebuild__")

    def _check_updates_popup(self) -> None:
        self._start_update_check(manual=True)

    def _check_updates_on_start(self) -> None:
        if self._launch_hidden:
            return
        if not self.context.settings.get().check_updates_on_start:
            return
        self._start_update_check(manual=False)

    def _start_update_check(self, manual: bool) -> None:
        if self._update_check_in_progress:
            return
        self._update_check_in_progress = True
        if manual:
            self._show_update_check_dialog()
        thread = threading.Thread(target=self._run_update_check_worker, args=(manual,), daemon=True)
        thread.start()

    def _run_update_check_worker(self, manual: bool) -> None:
        try:
            release = self.context.updates.fetch_latest_application_release()
        except Exception as error:
            release = {
                "status": "error",
                "current_version": __version__,
                "latest_version": __version__,
                "error": str(error),
            }
        self._ui_signals.update_check_done.emit(release, manual)

    def _on_update_check_done(self, release: object, manual: bool) -> None:
        self._update_check_in_progress = False
        self._close_update_check_dialog()
        if not isinstance(release, dict):
            if manual:
                self._show_error(self._t("Обновления", "Updates"), self._t("Не удалось проверить обновления.", "Failed to check for updates."))
            return

        status = str(release.get("status", "error"))
        latest_version = str(release.get("latest_version", ""))
        prompt_key = latest_version
        if bool(release.get("is_hotfix")):
            prompt_key = f"{latest_version}:{release.get('release_updated_at', '')}"
        if status == "up-to-date":
            if self.context.settings.get().apply_update_on_next_launch:
                self.context.settings.update(apply_update_on_next_launch=False)
        if status == "available" and not manual and self.context.settings.get().apply_update_on_next_launch:
            self._last_prompted_update_version = prompt_key
            self._start_update_apply(None, release)
            return
        if status == "available":
            if manual or self._last_prompted_update_version != prompt_key:
                self._last_prompted_update_version = prompt_key
                self._show_update_prompt(release)
            return
        if manual:
            if status == "up-to-date":
                self._show_info(
                    self._t("Обновления", "Updates"),
                    self._t(
                        f"У вас уже установлена последняя версия: {release.get('current_version', '')}.",
                        f"You already have the latest version: {release.get('current_version', '')}.",
                    ),
                )
            else:
                message = str(release.get("error", self._t("Не удалось проверить обновления.", "Failed to check for updates.")))
                self._add_notification("error", self._t("Обновления", "Updates"), message, source="updates")
                self._show_error(
                    self._t("Обновления", "Updates"),
                    message,
                )

    def _show_update_check_dialog(self) -> None:
        if self._update_check_dialog is not None:
            try:
                self._update_check_dialog.prepare_and_center()
                self._update_check_dialog.show()
                self._update_check_dialog.raise_()
                self._update_check_dialog.activateWindow()
            except Exception:
                pass
            return
        dialog = AppDialog(self, self.context, self._t("Обновления", "Updates"))
        label = QLabel(self._t("Проверка обновлений...", "Checking for updates..."))
        label.setWordWrap(True)
        dialog.body_layout.addWidget(label)
        dialog.prepare_and_center()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._update_check_dialog = dialog
        self._update_check_label = label

    def _close_update_check_dialog(self) -> None:
        dialog = self._update_check_dialog
        self._update_check_dialog = None
        self._update_check_label = None
        if dialog is None:
            return
        try:
            dialog.close()
            dialog.deleteLater()
        except Exception:
            pass

    def _show_component_update_dialog(self, component_name: str) -> None:
        text = self._t(
            f"Проверка обновлений {component_name}...",
            f"Checking {component_name} updates...",
        )
        if self._component_update_dialog is not None and self._component_update_label is not None:
            try:
                self._component_update_label.setText(text)
                self._component_update_dialog.prepare_and_center()
                self._component_update_dialog.show()
                self._component_update_dialog.raise_()
                self._component_update_dialog.activateWindow()
            except Exception:
                pass
            return
        dialog = AppDialog(self, self.context, self._t("Обновления", "Updates"))
        label = QLabel(text)
        label.setWordWrap(True)
        dialog.body_layout.addWidget(label)
        dialog.prepare_and_center()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._component_update_dialog = dialog
        self._component_update_label = label

    def _close_component_update_dialog(self) -> None:
        dialog = self._component_update_dialog
        self._component_update_dialog = None
        self._component_update_label = None
        if dialog is None:
            return
        try:
            dialog.close()
            dialog.deleteLater()
        except Exception:
            pass

    def _show_update_prompt(self, release: dict[str, str]) -> None:
        is_hotfix = bool(release.get("is_hotfix"))
        dialog = AppDialog(self, self.context, self._t("Доступен hotfix", "Hotfix available") if is_hotfix else self._t("Доступно обновление", "Update available"))
        if is_hotfix:
            message_text_ru = (
                "Доступна обновленная сборка текущей версии Zapret-Zen.\n\n"
                f"Версия: {release.get('current_version', '')}\n"
                "Рекомендуется установить hotfix, даже если номер версии не изменился."
            )
            message_text_en = (
                "An updated build of the current Zapret-Zen version is available.\n\n"
                f"Version: {release.get('current_version', '')}\n"
                "Installing this hotfix is recommended even though the version number did not change."
            )
        else:
            message_text_ru = f"Вышла новая версия Zapret-Zen.\n\nТекущая версия: {release.get('current_version', '')}\nНовая версия: {release.get('latest_version', '')}"
            message_text_en = f"A new Zapret-Zen version is available.\n\nCurrent version: {release.get('current_version', '')}\nNew version: {release.get('latest_version', '')}"
        message = QLabel(
            self._t(message_text_ru, message_text_en)
        )
        message.setWordWrap(True)
        dialog.body_layout.addWidget(message)

        releases = release.get("releases", [])
        release_list: list[dict[str, object]] = list(releases) if isinstance(releases, list) else []
        if not release_list:
            release_list = [
                {
                    "version": str(release.get("latest_version", "")).strip(),
                    "body": str(release.get("body", "")).strip(),
                    "html_url": str(release.get("html_url", "")).strip(),
                    "is_latest": True,
                }
            ]
        show_version_list = len(release_list) > 1
        dialog.setMinimumWidth(760 if show_version_list else 620)

        body_shell = QWidget()
        body_shell.setObjectName("UpdatePromptBody")
        body_layout = QHBoxLayout(body_shell)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)
        body_shell.setStyleSheet("#UpdatePromptBody { background: transparent; border: none; }")

        versions_list = QListWidget()
        versions_list.setMaximumWidth(170)
        versions_list.setMinimumHeight(160)
        versions_list.setSpacing(6)
        if show_version_list:
            body_layout.addWidget(versions_list, 0)

        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setMinimumHeight(160)
        notes.setMaximumHeight(260)
        notes.setMinimumWidth(520 if not show_version_list else 0)
        notes.setMaximumWidth(560 if not show_version_list else 16777215)
        notes.setProperty("class", "muted")
        body_layout.addWidget(notes, 1)

        for item in release_list:
            version = str(item.get("version", "")).strip()
            title = version
            if bool(item.get("is_latest")):
                title = f"{version} · {self._t('последняя', 'latest')}"
            if bool(item.get("is_hotfix")):
                title = f"{version} · hotfix"
            row_item = QListWidgetItem(title)
            row_item.setData(Qt.ItemDataRole.UserRole, dict(item))
            row_item.setSizeHint(QSize(140, 38))
            if show_version_list:
                versions_list.addItem(row_item)

        def _render_release(payload: object) -> None:
            if not isinstance(payload, dict):
                notes.clear()
                return
            version = str(payload.get("version", "")).strip()
            body = str(payload.get("body", "")).strip()
            html_url = str(payload.get("html_url", "")).strip()
            parts = [f"v{version}"] if version else []
            if html_url:
                parts.append(html_url)
            if body:
                parts.append("")
                parts.append(body)
            notes.setPlainText("\n".join(parts).strip())

        def _select_release(item: QListWidgetItem | None) -> None:
            _render_release(item.data(Qt.ItemDataRole.UserRole) if item is not None else {})

        if show_version_list:
            versions_list.currentItemChanged.connect(_select_release)
            if versions_list.count() > 0:
                versions_list.setCurrentRow(0)
        elif release_list:
            _render_release(release_list[0])
        dialog.body_layout.addWidget(body_shell)

        next_launch_checkbox = QCheckBox(self._t("Обновить при следующем запуске", "Update on next launch"))
        next_launch_checkbox.setChecked(bool(self.context.settings.get().apply_update_on_next_launch))
        dialog.body_layout.addWidget(next_launch_checkbox)

        row = QHBoxLayout()
        row.addStretch(1)
        close_btn = QPushButton(self._t("Закрыть", "Close"))
        link_btn = QPushButton(self._t("Открыть ссылку", "Open link"))
        update_btn = QPushButton(self._t("Обновить сейчас", "Update now"))
        update_btn.setProperty("class", "primary")
        self._attach_button_animations(close_btn)
        self._attach_button_animations(link_btn)
        self._attach_button_animations(update_btn)
        def _sync_update_button() -> None:
            update_btn.setText(self._t("Применить", "Apply") if next_launch_checkbox.isChecked() else self._t("Обновить сейчас", "Update now"))
        _sync_update_button()
        next_launch_checkbox.toggled.connect(lambda _checked=False: _sync_update_button())
        close_btn.clicked.connect(dialog.reject)
        link_btn.clicked.connect(lambda: self._open_update_link(str(release.get("html_url", ""))))
        update_btn.clicked.connect(
            lambda: self._start_update_apply(dialog, release, schedule_only=next_launch_checkbox.isChecked())
        )
        row.addWidget(close_btn)
        row.addWidget(link_btn)
        row.addWidget(update_btn)
        dialog.body_layout.addLayout(row)
        dialog.prepare_and_center()
        dialog.exec()

    def _open_update_link(self, url: str) -> None:
        if not url:
            return
        try:
            if sys.platform.startswith("win"):
                import os

                os.startfile(url)  # type: ignore[attr-defined]
            else:
                webbrowser.open(url)
        except Exception:
            webbrowser.open(url)

    def _start_update_apply(self, parent_dialog: AppDialog | None, release: dict[str, str], *, schedule_only: bool = False) -> None:
        if parent_dialog is not None:
            parent_dialog.accept()
        if schedule_only:
            self.context.settings.update(apply_update_on_next_launch=True)
            return
        if self.context.settings.get().apply_update_on_next_launch:
            self.context.settings.update(apply_update_on_next_launch=False)
        if self._update_prepare_dialog is not None:
            return
        dialog = AppDialog(self, self.context, self._t("Подготовка обновления", "Preparing update"))
        label = QLabel(self._t("Скачиваем и подготавливаем новую версию. Приложение перезапустится автоматически.", "Downloading and preparing the new version. The app will restart automatically."))
        label.setWordWrap(True)
        dialog.body_layout.addWidget(label)
        bar = QProgressBar()
        bar.setRange(0, 0)
        dialog.body_layout.addWidget(bar)
        dialog.prepare_and_center()
        dialog.show()
        self._update_prepare_dialog = dialog
        thread = threading.Thread(target=self._run_update_prepare_worker, args=(release,), daemon=True)
        thread.start()

    def _run_update_prepare_worker(self, release: dict[str, str]) -> None:
        try:
            prepared = self.context.updates.prepare_update(release)
            self._ui_signals.update_prepare_done.emit({"ok": True, "prepared": prepared})
        except Exception as error:
            self._ui_signals.update_prepare_done.emit({"ok": False, "error": str(error)})

    def _on_update_prepare_done(self, payload: object) -> None:
        if self._update_prepare_dialog is not None:
            self._update_prepare_dialog.accept()
            self._update_prepare_dialog = None
        if not isinstance(payload, dict) or not payload.get("ok"):
            message = str((payload or {}).get("error", self._t("Не удалось подготовить обновление.", "Failed to prepare the update."))) if isinstance(payload, dict) else self._t("Не удалось подготовить обновление.", "Failed to prepare the update.")
            self._add_notification("error", self._t("Обновления", "Updates"), message, source="updates")
            self._show_error(
                self._t("Обновления", "Updates"),
                message,
            )
            return
        prepared = payload.get("prepared")
        if not isinstance(prepared, dict):
            self._show_error(self._t("Обновления", "Updates"), self._t("Некорректный пакет обновления.", "Invalid update package."))
            return
        try:
            self.context.updates.launch_update(prepared)
        except Exception as error:
            self._add_notification("error", self._t("Обновления", "Updates"), str(error), source="updates")
            self._show_error(self._t("Обновления", "Updates"), str(error))
            return
        self._add_notification(
            "success",
            self._t("Обновления", "Updates"),
            self._t("Обновление подготовлено, приложение перезапускается.", "Update is prepared, restarting the app."),
            source="updates",
        )
        self._quit_for_update()

    def _run_diagnostics_popup(self) -> None:
        results = self.context.diagnostics.run_all()
        text = "\n".join(
            f"{item.name}: {item.status}"
            + (f" ({item.message})" if getattr(item, "message", "") else "")
            for item in results
        )
        self._show_info(self._t("Диагностика", "Diagnostics"), text or self._t("Нет данных диагностики.", "No diagnostics data."))

    def _load_selected_file(self, *_args: object) -> None:
        full_path = self._selected_file_path()
        if not full_path:
            return
        item = self.files_list.currentItem()
        label_text = item.text().split("\n")[0] if item else full_path
        self.file_path_label.setText(label_text)
        if self.rename_file_btn is not None:
            self.rename_file_btn.setEnabled(Path(full_path) != self.context.files.local_hosts_path())
        self._request_file_content(full_path)

    def _save_current_file(self) -> None:
        full_path = self._selected_file_path()
        if not full_path:
            self._show_info(self._t("Файлы", "Files"), self._t("Выберите файл перед сохранением.", "Select a file before saving."))
            return
        self._submit_backend_task(
            "write_file_text",
            {"path": full_path, "content": self.file_editor.toPlainText()},
            action_id="__file_saved__",
        )

    def _toggle_file_search(self, expanded: bool | None = None) -> None:
        panel = self._file_search_panel
        field = self._file_search_input
        if panel is None or field is None:
            return
        if self._file_search_shell is not None:
            self._file_search_shell.raise_()
        target_expanded = (not self._file_search_expanded) if expanded is None else bool(expanded)
        self._file_search_expanded = target_expanded
        panel.setProperty("expanded", target_expanded)
        panel.style().unpolish(panel)
        panel.style().polish(panel)
        if self._file_search_anim is not None:
            self._file_search_anim.stop()
            self._file_search_anim = None
        if target_expanded:
            field.setVisible(True)
            target_width = self._current_file_search_expanded_width()
        else:
            self._clear_file_search(reset_text=True)
            target_width = 44
        group = QParallelAnimationGroup(self)
        for prop_name in (b"minimumWidth", b"maximumWidth"):
            anim = QPropertyAnimation(panel, prop_name, self)
            anim.setDuration(180 if target_expanded else 120)
            current = panel.minimumWidth() if prop_name == b"minimumWidth" else panel.maximumWidth()
            anim.setStartValue(current)
            anim.setEndValue(target_width)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic if target_expanded else QEasingCurve.Type.InCubic)
            group.addAnimation(anim)

        def _after() -> None:
            if not target_expanded:
                field.setVisible(False)
                if self._file_search_prev_btn is not None:
                    self._file_search_prev_btn.setVisible(False)
                if self._file_search_next_btn is not None:
                    self._file_search_next_btn.setVisible(False)
                app = QCoreApplication.instance()
                if app is not None:
                    try:
                        app.removeEventFilter(self)
                    except Exception:
                        pass
            else:
                app = QCoreApplication.instance()
                if app is not None:
                    try:
                        app.installEventFilter(self)
                    except Exception:
                        pass
                field.setFocus(Qt.FocusReason.MouseFocusReason)
                field.selectAll()
                self._update_file_search_controls()
                if field.text().strip():
                    self._refresh_file_search_matches()

        group.finished.connect(_after)
        group.start()
        self._file_search_anim = group

    def _register_file_search_variant(
        self,
        name: str,
        *,
        shell: QWidget,
        panel: QFrame,
        toggle: QToolButton,
        field: QLineEdit,
        prev_btn: QToolButton,
        next_btn: QToolButton,
    ) -> None:
        self._file_search_variants[name] = {
            "shell": shell,
            "panel": panel,
            "toggle": toggle,
            "field": field,
            "prev": prev_btn,
            "next": next_btn,
        }

    def _use_file_search_variant(self, name: str) -> None:
        variant = self._file_search_variants.get(name)
        if variant is None:
            return
        self._file_search_shell = variant["shell"]
        self._file_search_panel = variant["panel"]  # type: ignore[assignment]
        self._file_search_toggle = variant["toggle"]  # type: ignore[assignment]
        self._file_search_input = variant["field"]  # type: ignore[assignment]
        self._file_search_prev_btn = variant["prev"]  # type: ignore[assignment]
        self._file_search_next_btn = variant["next"]  # type: ignore[assignment]
        if self._file_search_panel is not None:
            expanded = bool(self._file_search_expanded)
            self._file_search_panel.setProperty("expanded", expanded)
            expanded_width = self._current_file_search_expanded_width()
            self._file_search_panel.setMinimumWidth(expanded_width if expanded else 44)
            self._file_search_panel.setMaximumWidth(expanded_width if expanded else 44)
        if self._file_search_input is not None:
            self._file_search_input.setVisible(bool(self._file_search_expanded))
        if self._file_search_shell is not None:
            self._file_search_shell.raise_()
        self._apply_file_search_style()
        self._update_file_search_controls()

    def _build_file_search_variant(self, parent: QWidget, *, placeholder: str) -> tuple[QWidget, QFrame, QToolButton, QLineEdit, QToolButton, QToolButton]:
        search_shell = QWidget(parent)
        search_shell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        search_shell.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        search_shell.setAutoFillBackground(False)
        search_shell.setStyleSheet("background: transparent;")
        search_layout = QHBoxLayout(search_shell)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(8)
        search_layout.addStretch(1)

        search_panel = QFrame(search_shell)
        search_panel.setObjectName("FileSearchPanel")
        search_panel.setProperty("expanded", False)
        search_panel.setProperty("searchState", "idle")
        search_panel.setMaximumWidth(44)
        search_panel.setMinimumWidth(44)
        search_panel.setMinimumHeight(38)
        search_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        search_panel_layout = QHBoxLayout(search_panel)
        search_panel_layout.setContentsMargins(8, 4, 4, 4)
        search_panel_layout.setSpacing(4)

        search_input = QLineEdit()
        search_input.setPlaceholderText(placeholder)
        search_input.setFixedWidth(156)
        search_input.setVisible(False)
        search_input.installEventFilter(self)
        search_input.textChanged.connect(self._on_file_search_text_changed)
        search_panel_layout.addWidget(search_input)

        search_prev_btn = QToolButton()
        search_prev_btn.setProperty("class", "action")
        search_prev_btn.setArrowType(Qt.ArrowType.UpArrow)
        search_prev_btn.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
        search_prev_btn.setVisible(False)
        search_prev_btn.clicked.connect(lambda: self._jump_file_search_match(-1))
        search_panel_layout.addWidget(search_prev_btn)

        search_next_btn = QToolButton()
        search_next_btn.setProperty("class", "action")
        search_next_btn.setArrowType(Qt.ArrowType.DownArrow)
        search_next_btn.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
        search_next_btn.setVisible(False)
        search_next_btn.clicked.connect(lambda: self._jump_file_search_match(1))
        search_panel_layout.addWidget(search_next_btn)

        search_toggle = QToolButton()
        search_toggle.setProperty("class", "action")
        search_toggle.setIcon(self._icon("search.svg"))
        search_toggle.setIconSize(QSize(16, 16))
        search_toggle.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
        search_toggle.clicked.connect(lambda _=False: self._toggle_file_search())
        search_panel_layout.addWidget(search_toggle)

        search_layout.addWidget(search_panel, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        self._attach_button_animations(search_prev_btn)
        self._attach_button_animations(search_next_btn)
        self._attach_button_animations(search_toggle)
        return search_shell, search_panel, search_toggle, search_input, search_prev_btn, search_next_btn

    def _clear_file_search(self, *, reset_text: bool) -> None:
        if reset_text and self._file_search_input is not None:
            self._file_search_input.blockSignals(True)
            self._file_search_input.clear()
            self._file_search_input.blockSignals(False)
        self._file_search_matches = []
        self._file_search_index = -1
        self._file_tag_search_matches = []
        self._file_tag_search_index = -1
        self.file_editor.setExtraSelections([])
        self._apply_tag_search_highlights()
        if self._file_search_panel is not None:
            self._file_search_panel.setProperty("searchState", "idle")
            self._file_search_panel.style().unpolish(self._file_search_panel)
            self._file_search_panel.style().polish(self._file_search_panel)
        self._update_file_search_controls()

    def _on_file_search_text_changed(self, _text: str) -> None:
        if self._file_search_mode == "tags":
            self._render_file_tags(self._current_file_values_cache)
            return
        self._refresh_file_search_matches()

    def _on_file_editor_text_changed(self) -> None:
        if self._file_search_expanded and self._file_search_input is not None and self._file_search_input.text().strip():
            self._refresh_file_search_matches()

    def _refresh_file_search_matches(self) -> None:
        if self._file_search_input is None:
            return
        query = self._file_search_input.text().strip()
        if not query:
            self._clear_file_search(reset_text=False)
            return
        if self._file_search_mode == "tags":
            self._refresh_tag_search_matches(query)
            return
        self._file_search_matches = []
        document = self.file_editor.document()
        cursor = QTextCursor(document)
        while True:
            cursor = document.find(query, cursor, QTextDocument.FindFlag(0))
            if cursor.isNull():
                break
            self._file_search_matches.append((cursor.selectionStart(), cursor.selectionEnd()))
        if not self._file_search_matches:
            self.file_editor.setExtraSelections([])
            self._file_search_index = -1
            if self._file_search_panel is not None:
                self._file_search_panel.setProperty("searchState", "empty")
                self._file_search_panel.style().unpolish(self._file_search_panel)
                self._file_search_panel.style().polish(self._file_search_panel)
            self._update_file_search_controls()
            return
        if self._file_search_panel is not None:
            self._file_search_panel.setProperty("searchState", "ok")
            self._file_search_panel.style().unpolish(self._file_search_panel)
            self._file_search_panel.style().polish(self._file_search_panel)
        self._file_search_index = 0
        self._apply_file_search_highlights()
        self._focus_file_search_match(self._file_search_index)
        self._update_file_search_controls()

    def _refresh_tag_search_matches(self, query: str) -> None:
        self._file_tag_search_matches = []
        query_lower = query.lower()
        if self._file_tag_flow is not None:
            for idx in range(self._file_tag_flow.count()):
                item = self._file_tag_flow.itemAt(idx)
                widget = item.widget() if item is not None else None
                if isinstance(widget, QFrame):
                    value = str(widget.property("tagValue") or "")
                    if query_lower in value.lower():
                        self._file_tag_search_matches.append(widget)
        if not self._file_tag_search_matches:
            self._file_tag_search_index = -1
            if self._file_search_panel is not None:
                self._file_search_panel.setProperty("searchState", "empty")
                self._file_search_panel.style().unpolish(self._file_search_panel)
                self._file_search_panel.style().polish(self._file_search_panel)
            self._apply_tag_search_highlights()
            self._update_file_search_controls()
            return
        if self._file_search_panel is not None:
            self._file_search_panel.setProperty("searchState", "ok")
            self._file_search_panel.style().unpolish(self._file_search_panel)
            self._file_search_panel.style().polish(self._file_search_panel)
        self._file_tag_search_index = 0
        self._apply_tag_search_highlights()
        self._focus_tag_search_match(self._file_tag_search_index)
        self._update_file_search_controls()

    def _apply_file_search_highlights(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []
        for index, (start, end) in enumerate(self._file_search_matches):
            cursor = self.file_editor.textCursor()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            fmt = QTextCharFormat()
            if index == self._file_search_index:
                fmt.setBackground(QColor(88, 101, 242, 145))
                fmt.setForeground(QColor("#ffffff"))
            else:
                fmt.setBackground(QColor(126, 164, 255, 72))
            selection.format = fmt
            selections.append(selection)
        self.file_editor.setExtraSelections(selections)

    def _apply_tag_search_highlights(self) -> None:
        if self._file_tag_flow is None:
            return
        active_widget = None
        if 0 <= self._file_tag_search_index < len(self._file_tag_search_matches):
            active_widget = self._file_tag_search_matches[self._file_tag_search_index]
        for idx in range(self._file_tag_flow.count()):
            item = self._file_tag_flow.itemAt(idx)
            widget = item.widget() if item is not None else None
            if not isinstance(widget, QFrame):
                continue
            if widget is active_widget:
                state = "active"
            elif widget in self._file_tag_search_matches:
                state = "match"
            else:
                state = "idle"
            widget.setProperty("searchState", state)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

    def _focus_file_search_match(self, index: int) -> None:
        if index < 0 or index >= len(self._file_search_matches):
            return
        start, end = self._file_search_matches[index]
        cursor = self.file_editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.file_editor.setTextCursor(cursor)
        self.file_editor.ensureCursorVisible()

    def _focus_tag_search_match(self, index: int) -> None:
        if index < 0 or index >= len(self._file_tag_search_matches):
            return
        widget = self._file_tag_search_matches[index]
        if self._file_tag_scroll is not None:
            self._file_tag_scroll.ensureWidgetVisible(widget, 12, 12)

    def _jump_file_search_match(self, step: int) -> None:
        if self._file_search_mode == "tags":
            if not self._file_tag_search_matches:
                return
            self._file_tag_search_index = (self._file_tag_search_index + step) % len(self._file_tag_search_matches)
            self._apply_tag_search_highlights()
            self._focus_tag_search_match(self._file_tag_search_index)
            self._update_file_search_controls()
            return
        if not self._file_search_matches:
            return
        self._file_search_index = (self._file_search_index + step) % len(self._file_search_matches)
        self._apply_file_search_highlights()
        self._focus_file_search_match(self._file_search_index)
        self._update_file_search_controls()

    def _update_file_search_controls(self) -> None:
        count = len(self._file_tag_search_matches) if self._file_search_mode == "tags" else len(self._file_search_matches)
        multi = count > 1 and self._file_search_expanded
        if self._file_search_prev_btn is not None:
            self._file_search_prev_btn.setVisible(multi)
            self._file_search_prev_btn.setEnabled(count > 1)
        if self._file_search_next_btn is not None:
            self._file_search_next_btn.setVisible(multi)
            self._file_search_next_btn.setEnabled(count > 1)
        if self._file_search_input is not None:
            self._file_search_input.setFixedWidth(156 if not multi else 170)
        if self._file_search_panel is not None and self._file_search_expanded:
            width = self._current_file_search_expanded_width()
            self._file_search_panel.setMinimumWidth(width)
            self._file_search_panel.setMaximumWidth(width)

    def _current_file_search_expanded_width(self) -> int:
        count = len(self._file_tag_search_matches) if self._file_search_mode == "tags" else len(self._file_search_matches)
        return 278 if count > 1 else 214

    def _rename_current_file(self) -> None:
        full_path = self._selected_file_path()
        if not full_path:
            self._show_info(self._t("Файлы", "Files"), self._t("Выберите файл перед переименованием.", "Select a file before renaming."))
            return
        path = Path(full_path)
        new_name, ok = QInputDialog.getText(self, "Rename file", "New file name:", text=path.name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == path.name:
            return
        target = path.with_name(new_name)
        if target.exists():
            self._show_warning(self._t("Файлы", "Files"), self._t("Файл с таким именем уже существует.", "A file with this name already exists."))
            return
        try:
            path.rename(target)
            self.context.logging.log("info", "File renamed", source=str(path), target=str(target))
            self._set_files_mode_loading(True)
            self._request_page_refresh("files")
            self.refresh_logs()
        except Exception as error:
            self._show_error(self._t("Файлы", "Files"), f"{self._t('Не удалось переименовать файл', 'Failed to rename file')}:\n{error}")

    def schedule_refresh_all(self) -> None:
        self._refresh_dirty_sections.update({"dashboard", "services", "components", "mods", "files", "logs", "tray"})
        self._schedule_dirty_refresh()

    def _mark_dirty(self, *sections: str) -> None:
        self._refresh_dirty_sections.update(sections)
        self._schedule_dirty_refresh()

    def _schedule_dirty_refresh(self) -> None:
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        delay = 0 if not (self._page_transition_running or self._files_mode_transition_running) else 140
        QTimer.singleShot(delay, self._flush_dirty_refresh)

    def _flush_dirty_refresh(self) -> None:
        if self._page_transition_running or self._files_mode_transition_running:
            self._refresh_scheduled = False
            self._schedule_dirty_refresh()
            return
        self._refresh_scheduled = False
        dirty = set(self._refresh_dirty_sections)
        self._refresh_dirty_sections.clear()

        if "dashboard" in dirty:
            try:
                self.refresh_dashboard()
            except Exception as error:
                self.context.logging.log("error", "refresh_dashboard_failed", error=str(error))
        if "services" in dirty:
            try:
                self.refresh_services()
            except Exception as error:
                self.context.logging.log("error", "refresh_services_failed", error=str(error))
        if "tray" in dirty:
            try:
                self._rebuild_tray_menu()
            except Exception:
                pass
        if "components" in dirty:
            try:
                self.refresh_components()
            except Exception as error:
                self.context.logging.log("error", "refresh_components_failed", error=str(error))
        if "mods" in dirty:
            try:
                self.refresh_mods()
            except Exception:
                pass
        if "files" in dirty:
            try:
                self._request_page_refresh("files")
            except Exception:
                pass
        if "logs" in dirty:
            try:
                self._request_page_refresh("logs")
            except Exception:
                pass

        if self._initial_refresh_pending:
            self._initial_refresh_pending = False
            self._hide_loading_overlay()

    def refresh_all(self) -> None:
        self.schedule_refresh_all()

    def _request_page_refresh(self, section: str) -> None:
        if section == "files":
            self._files_refresh_token += 1
            token = self._files_refresh_token
            mode_index = self._file_mode_stack.currentIndex() if self._file_mode_stack is not None else 0
            collection_id = self._current_file_collection
            file_filter = self._current_file_list_filter
            cached = self._page_payload_cache.get(section)
            if isinstance(cached, dict):
                cached_mode = int(cached.get("mode_index", -1) or -1)
                cached_collection = str(cached.get("collection_id", "") or "")
                cached_filter = str(cached.get("file_filter", "all") or "all")
                if cached_mode == mode_index and cached_collection == collection_id and cached_filter == file_filter:
                    self.refresh_files(cached)
            if self.context.backend is not None:
                try:
                    self._submit_backend_task(
                        "load_files_payload",
                        {"_token": token, "mode_index": mode_index, "collection_id": collection_id, "file_filter": file_filter},
                        action_id="__files_payload__",
                    )
                    return
                except Exception:
                    pass
            thread = threading.Thread(
                target=self._collect_files_payload_worker,
                args=(token, mode_index, collection_id, file_filter),
                daemon=True,
            )
            thread.start()
            return
        if section == "components" and self.context.backend is not None:
            try:
                self._submit_backend_task("load_components_payload", action_id="__components_payload__")
                return
            except Exception:
                pass
        cached = self._page_payload_cache.get(section)
        if cached is not None:
            if section == "components":
                self.refresh_components(cached)
            elif section == "mods":
                self.refresh_mods(cached)
            elif section == "files":
                self.refresh_files(cached)
            elif section == "logs":
                self.refresh_logs(cached)
        if section in self._page_refresh_in_progress:
            return
        self._page_refresh_in_progress.add(section)
        thread = threading.Thread(target=self._collect_page_payload_worker, args=(section,), daemon=True)
        thread.start()

    def _collect_files_payload_worker(self, token: int, mode_index: int, collection_id: str, file_filter: str = "all") -> None:
        try:
            payload = {
                "_token": token,
                "mode_index": mode_index,
                "collection_id": collection_id,
                "file_filter": file_filter,
                "records": self._file_records_for_filter_sync(file_filter) if mode_index == 2 else None,
                "collection_values": self.context.files.read_collection(collection_id) if mode_index == 1 else None,
            }
            self._ui_signals.page_payload_ready.emit("files", payload)
        except Exception:
            self._ui_signals.page_payload_ready.emit("files", {"_token": token, "mode_index": mode_index, "collection_id": collection_id, "file_filter": file_filter, "records": None, "collection_values": None})

    def _file_records_for_filter_sync(self, file_filter: str) -> list[FileRecord]:
        if file_filter == "generals":
            return self._general_file_records_sync()
        if file_filter == "hosts":
            path = self.context.files.ensure_local_hosts_file()
            try:
                relative = str(path.relative_to(self.context.paths.install_root))
            except ValueError:
                relative = str(path)
            return [FileRecord(path=str(path), relative_path=relative, size=path.stat().st_size)]
        return self.context.files.list_files()

    def _general_file_records_sync(self) -> list[FileRecord]:
        records: list[FileRecord] = []
        seen: set[str] = set()
        for option in self.context.processes.list_zapret_generals():
            path = Path(str(option.get("path", "") or ""))
            if not path.exists() or not path.is_file():
                continue
            resolved = str(path.resolve()).lower()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                relative = str(path.relative_to(self.context.paths.install_root))
            except ValueError:
                relative = str(path)
            bundle = str(option.get("bundle", "") or "").strip()
            label = f"{bundle}/{path.name}" if bundle else path.name
            records.append(FileRecord(path=str(path), relative_path=label if label else relative, size=path.stat().st_size))
        return sorted(records, key=lambda item: item.relative_path.lower())

    def _collect_page_payload_worker(self, section: str) -> None:
        try:
            payload: object
            if section == "components":
                payload = {
                    "components": self.context.processes.list_components(),
                    "states": {item.component_id: item for item in self.context.processes.list_states()},
                    "general_options": list(self.context.processes.list_zapret_generals()),
                }
            elif section == "mods":
                payload = {
                    "index": self.context.mods.fetch_index(),
                    "installed": list(self.context.mods.list_installed()),
                }
            elif section == "files":
                mode_index = self._file_mode_stack.currentIndex() if self._file_mode_stack is not None else 0
                collection_id = self._current_file_collection
                payload = {
                    "records": self._file_records_for_filter_sync(self._current_file_list_filter) if mode_index == 2 else None,
                    "collection_values": self.context.files.read_collection(collection_id) if mode_index == 1 else None,
                    "collection_id": collection_id,
                    "mode_index": mode_index,
                    "file_filter": self._current_file_list_filter,
                }
            elif section == "logs":
                source_id = self._current_log_source
                payload = {
                    "source": source_id,
                    "lines": self.context.logging.read_source_lines(source_id),
                }
            else:
                payload = None
            self._ui_signals.page_payload_ready.emit(section, payload)
        except Exception:
            self._ui_signals.page_payload_ready.emit(section, None)

    def _collect_file_content_worker(self, token: int, full_path: str) -> None:
        try:
            payload = {
                "_token": token,
                "path": full_path,
                "content": self.context.files.read_text(full_path),
            }
            self._ui_signals.page_payload_ready.emit("file_content", payload)
        except Exception:
            self._ui_signals.page_payload_ready.emit("file_content", {"_token": token, "path": full_path, "content": ""})

    def _on_page_payload_ready(self, section: str, payload: object) -> None:
        if section == "file_content" and isinstance(payload, dict):
            if int(payload.get("_token", 0) or 0) != self._file_content_refresh_token:
                return
            if str(payload.get("path", "") or "") != self._pending_file_content_path:
                return
            self.file_editor.setPlainText(str(payload.get("content", "") or ""))
            self._refresh_file_search_matches()
            self._set_file_editor_loading(False)
            return
        self._page_refresh_in_progress.discard(section)
        if section == "files" and isinstance(payload, dict):
            if int(payload.get("_token", 0) or 0) != self._files_refresh_token:
                return
        if payload is not None:
            self._page_payload_cache[section] = payload
            if section == "components":
                self._update_runtime_snapshot_from_payload(payload)
                self._update_general_options_from_payload(payload)
                self._notify_component_errors_from_payload(payload)
            elif section == "mods":
                self._update_mods_cache_from_payload(payload)
        visible_page = self.pages.currentIndex() if hasattr(self, "pages") else 0
        if section == "components":
            self.context.logging.log("info", "components_payload_received", payload_type=type(payload).__name__)
            self.refresh_components(payload)
            QTimer.singleShot(0, self._sync_component_card_layout)
            self.context.logging.log("info", "components_render_done")
        elif section == "mods":
            self.refresh_mods(payload)
            QTimer.singleShot(0, self._sync_mod_card_layout)
        elif section == "files":
            self.refresh_files(payload)
        elif section == "logs":
            self.refresh_logs(payload)
        if self._loading_overlay_context == f"page:{section}":
            self._hide_loading_overlay()

    def refresh_dashboard(self) -> None:
        if self._page_transition_running and (
            self.pages.currentIndex() == 0 or getattr(self, "_page_transition_target", -1) == 0
        ):
            self._refresh_dirty_sections.add("dashboard")
            return
        settings = self.context.settings.get()
        if not self._startup_snapshot_ready:
            self._ensure_local_runtime_snapshot()
        if not self._startup_snapshot_ready:
            self.power_button.setEnabled(False)
            self.power_button.setProperty("state", "loading")
            self._update_power_icon()
            if isinstance(self.power_button, AnimatedPowerButton):
                self.power_button.set_loading_state(True, animate=not self._page_transition_running)
            if self.power_aura is not None:
                self.power_aura.set_idle_pulse_enabled(False)
                self.power_aura.set_status_glow_enabled(True)
            if self.power_caption_dots is not None:
                self.power_caption_dots.setText("")
                self.power_caption_dots.hide()
            self._power_caption_base_text = self._t("ЗАГРУЗКА", "LOADING")
            if self.power_caption_text is not None:
                self._set_power_status_pill(self._power_caption_base_text, "loading")
            self._set_badge("app", self._t("Загрузка", "Loading"), "status_warn.svg")
            self._set_badge("zapret", self._t("Загрузка", "Loading"), "status_warn.svg")
            self._set_badge("tg", self._t("Загрузка", "Loading"), "status_warn.svg")
            self._set_badge("mods", self._t("Загрузка", "Loading"), "status_mod.svg")
            return
        if self.general_combo.isVisible():
            self._refresh_general_combo(settings.selected_zapret_general)
        states = self._component_states()
        components = self._component_defs()
        active_ids = self._master_active_components()
        zapret_state = states.get("zapret", None)
        tg_state = states.get("tg-ws-proxy", None)
        running_ids = {cid for cid in active_ids if states.get(cid) and states[cid].status == "running"}
        any_running = len(running_ids) > 0
        fully_running = bool(active_ids) and set(active_ids) == running_ids

        self.power_button.setProperty("state", "on" if fully_running else "off")
        self._update_power_icon()
        self.power_button.setEnabled(not self._toggle_in_progress)
        if isinstance(self.power_button, AnimatedPowerButton):
            animate_power = not self._page_transition_running
            self.power_button.set_active_state(fully_running, animate=animate_power)
        if self.power_aura is not None:
            self.power_aura.set_idle_pulse_enabled(fully_running and not self._toggle_in_progress)
            self.power_aura.set_status_glow_enabled(fully_running or self._toggle_in_progress)
        if self.power_caption_dots is not None:
            self.power_caption_dots.setText("")
            self.power_caption_dots.hide()
        self._power_caption_base_text = ""
        if not active_ids:
            if self.power_caption_text is not None:
                self._set_power_status_pill(self._t("Нет компонентов", "No components"), "off")
                self._power_caption_base_text = self._t("НЕТ КОМПОНЕНТОВ", "NO COMPONENTS")
        else:
            target_caption = self._t("Включено", "On") if fully_running else (self._t("Частично", "Partial") if any_running else self._t("Выключено", "Off"))
            if self.power_caption_text is not None:
                self._set_power_status_pill(target_caption, "on" if fully_running else ("partial" if any_running else "off"))
                self._power_caption_base_text = target_caption

        enabled_mods = list(settings.enabled_mod_ids or [])

        self._set_badge("app", self._t("Работает", "Running") if fully_running else (self._t("Частично", "Partial") if any_running else self._t("Ожидание", "Idle")), "status_ok.svg" if fully_running else ("status_warn.svg" if any_running else "status_off.svg"))
        vpn_state = states.get("peshk0v-vpn")
        vpn_running = bool(vpn_state and str(getattr(vpn_state, "status", "") or "") == "running")
        vpn_enabled = "peshk0v-vpn" in {str(item) for item in list(settings.enabled_component_ids or [])}
        show_vpn_badge = vpn_enabled or vpn_running
        if show_vpn_badge:
            zapret_text, zapret_icon = self._component_badge_state(components.get("peshk0v-vpn"), vpn_state, any_running)
            self._set_badge_title("zapret", self._t("VPN", "VPN"))
        else:
            zapret_text, zapret_icon = self._component_badge_state(components.get("zapret"), zapret_state, any_running)
            self._set_badge_title("zapret", "Zapret")
        tg_text, tg_icon = self._component_badge_state(components.get("tg-ws-proxy"), tg_state, any_running)
        self._set_badge("zapret", zapret_text, zapret_icon)
        self._set_badge("tg", tg_text, tg_icon)
        self._set_badge("mods", f"{len(enabled_mods)} {self._t('Активно', 'Active')}", "status_mod.svg")

    def _set_power_status_pill(self, text: str, state: str) -> None:
        if self.power_caption_text is None:
            return
        text_color, accent, alpha = self._power_status_palette(state)
        border = QColor(accent)
        border.setAlpha(96 if state != "off" else 56)
        fill = QColor(accent)
        fill.setAlpha(alpha)
        self.power_caption_text.setText(text)
        self.power_caption_text.setStyleSheet(
            "QLabel#PowerStatusPill {"
            f"color: {text_color};"
            f"background: {fill.name(QColor.NameFormat.HexArgb)};"
            f"border: 1px solid {border.name(QColor.NameFormat.HexArgb)};"
            "border-radius: 15px;"
            "font-size: 12px;"
            "font-weight: 700;"
            "padding: 0 14px;"
            "}"
        )

    def _power_status_palette(self, state: str) -> tuple[str, str, int]:
        theme = self.context.settings.get().theme
        light = is_light_theme(theme)
        colors = {
            "on": ("#132447" if light else "#dce5ff", "#1f4fbf" if light else "#6e8fff", 26),
            "partial": ("#3d2b08" if light else "#ffe1a0", "#c77908" if light else "#f0a020", 24),
            "loading": ("#132447" if light else "#dce5ff", "#1f4fbf" if light else "#4fbfe8", 22),
            "off": ("#334155" if light else "#b4bfcd", "#526071" if light else "#8793a4", 14),
        }
        return colors.get(state, colors["off"])

    def _inactive_control_style_values(self) -> tuple[str, QColor, QColor]:
        text_color, accent, alpha = self._power_status_palette("off")
        border = QColor(accent)
        border.setAlpha(56)
        fill = QColor(accent)
        fill.setAlpha(alpha)
        return text_color, border, fill

    def _sync_power_reconfigure_button_style(self) -> None:
        button = self.power_reconfigure_btn
        if button is None:
            return
        text, border, fill = self._inactive_control_style_values()
        hover = QColor(fill)
        hover.setAlpha(min(255, fill.alpha() + 14))
        button.setIcon(
            self._build_tinted_icon(
                self._icons_dir / "rerun.svg",
                QColor(text),
                fill_ratio=0.82,
                offset_x=-1.0,
                offset_y=-0.3,
            )
        )
        button.setIconSize(QSize(15, 15))
        button.setStyleSheet(
            "QToolButton#PowerReconfigureButton {"
            f"background: {fill.name(QColor.NameFormat.HexArgb)};"
            f"border: 1px solid {border.name(QColor.NameFormat.HexArgb)};"
            f"color: {text};"
            "border-radius: 15px;"
            "padding: 0px;"
            "margin: 0px;"
            "}"
            "QToolButton#PowerReconfigureButton:hover {"
            f"background: {hover.name(QColor.NameFormat.HexArgb)};"
            "}"
        )

    def _sync_power_vpn_button_style(self) -> None:
        button = self.power_vpn_btn
        if button is None:
            return
        settings = self.context.settings.get()
        selected = "peshk0v-vpn" in {str(item) for item in list(settings.enabled_component_ids or [])}
        running = False
        try:
            states = self._component_states()
            vpn_state = states.get("peshk0v-vpn")
            running = bool(vpn_state and str(getattr(vpn_state, "status", "") or "") == "running")
            selected = selected or running
        except Exception:
            pass
        if selected:
            text = "#d9ffe9" if not is_light_theme(settings.theme) else "#06391d"
            border = QColor("#22c55e")
            border.setAlpha(120)
            fill = QColor("#22c55e")
            fill.setAlpha((64 if running else 42) if not is_light_theme(settings.theme) else (54 if running else 34))
        else:
            text, border, fill = self._inactive_control_style_values()
        hover = QColor(fill)
        hover.setAlpha(min(255, fill.alpha() + 18))
        button.setEnabled(not self._vpn_mode_switch_in_progress)
        button.setIcon(
            self._build_tinted_icon(
                self._icons_dir / self._vpn_icon_name(),
                QColor(text),
                fill_ratio=0.72,
                offset_x=-0.2,
                offset_y=0.6,
            )
        )
        button.setIconSize(QSize(16, 16))
        button.setStyleSheet(
            "QToolButton#PowerVpnButton {"
            f"background: {fill.name(QColor.NameFormat.HexArgb)};"
            f"border: 1px solid {border.name(QColor.NameFormat.HexArgb)};"
            f"color: {text};"
            "border-radius: 15px;"
            "padding: 0px;"
            "margin: 0px;"
            "}"
            "QToolButton#PowerVpnButton:hover {"
            f"background: {hover.name(QColor.NameFormat.HexArgb)};"
            "}"
            "QToolButton#PowerVpnButton:disabled {"
            "opacity: 0.72;"
            "}"
        )

    def _handle_power_vpn_button(self) -> None:
        if self._vpn_mode_switch_in_progress:
            return
        vpn_state = {}
        try:
            vpn_state = self.context.vpn.state() if getattr(self.context, "vpn", None) is not None else {}
        except Exception:
            vpn_state = {}
        configured = str(vpn_state.get("subscription_state", "") or "") == "valid"
        if not configured:
            self._open_peshk0v_vpn_component()
            return
        settings = self.context.settings.get()
        enabled = {str(item) for item in list(settings.enabled_component_ids or [])}
        target_enabled = "peshk0v-vpn" not in enabled
        zapret_enabled_before = "zapret" in enabled
        if target_enabled:
            enabled.add("peshk0v-vpn")
            enabled.discard("zapret")
        else:
            enabled.discard("peshk0v-vpn")
            if zapret_enabled_before:
                enabled.add("zapret")
        self.context.settings.update(enabled_component_ids=sorted(enabled))
        self._set_power_vpn_switch_pending(True)
        if self._master_runtime_has_running_components():
            self._begin_vpn_mode_power_transition()
        self._mark_dirty("dashboard", "components", "tray")
        self._submit_backend_task(
            "toggle_peshk0v_vpn_mode",
            {"enabled": target_enabled, "zapret_enabled_before": zapret_enabled_before},
            action_id="peshk0v-vpn",
        )

    def _set_power_vpn_switch_pending(self, pending: bool) -> None:
        self._vpn_mode_switch_in_progress = bool(pending)
        if self.power_vpn_btn is not None:
            self.power_vpn_btn.setEnabled(not pending)
        self._sync_power_vpn_button_style()

    def _master_runtime_has_running_components(self) -> bool:
        try:
            return any(str(state.status or "") == "running" for state in self._component_states().values())
        except Exception:
            return False

    def _begin_vpn_mode_power_transition(self) -> None:
        if self._toggle_in_progress:
            return
        self._vpn_switch_power_transition = True
        self._loading_action = "connect"
        self._toggle_in_progress = True
        self.power_button.setEnabled(False)
        self._loading_frame = 0
        self._loading_timer.start()
        self._advance_loading_caption()

    def _finish_vpn_mode_power_transition(self) -> None:
        if not self._vpn_switch_power_transition:
            return
        self._vpn_switch_power_transition = False
        self._loading_timer.stop()
        self._toggle_in_progress = False
        self.power_button.setEnabled(bool(self._startup_snapshot_ready))
        self._update_power_icon()

    def _open_peshk0v_vpn_component(self) -> None:
        self._components_scroll_target_component_id = ""
        try:
            self._switch_page(2)
        except Exception:
            self.pages.setCurrentIndex(2)
        self._mark_dirty("components")
        QTimer.singleShot(0, self.refresh_components)
        if self._components_scroll is not None:
            QTimer.singleShot(0, lambda: self._components_scroll.verticalScrollBar().setValue(0))
            QTimer.singleShot(120, lambda: self._components_scroll.verticalScrollBar().setValue(0))

    def _scroll_to_peshk0v_vpn_component(self) -> None:
        self._ensure_components_scroll_target_visible()

    def _ensure_components_scroll_target_visible(self) -> None:
        target_id = self._components_scroll_target_component_id
        if not target_id or self._components_scroll is None:
            return
        target = self._components_card_by_id.get(target_id)
        if target is not None and target.isVisible():
            try:
                self._components_scroll.ensureWidgetVisible(target, 18, 18)
                self._components_scroll_target_component_id = ""
                return
            except Exception:
                pass
        if self._components_scroll is not None:
            self._components_scroll.verticalScrollBar().setValue(self._components_scroll.verticalScrollBar().maximum())
            if target is not None:
                self._components_scroll_target_component_id = ""

    def _sync_onboarding_back_button_style(self) -> None:
        button = self._onboarding_back_btn
        if button is None:
            return
        text, border, fill = self._inactive_control_style_values()
        hover = QColor(fill)
        hover.setAlpha(min(255, fill.alpha() + 14))
        button.setIcon(self._build_tinted_icon(self._icons_dir / "arrow_left.svg", QColor(text)))
        button.setStyleSheet(
            "QToolButton#OnboardingBackButton {"
            f"background: {fill.name(QColor.NameFormat.HexArgb)};"
            f"border: 1px solid {border.name(QColor.NameFormat.HexArgb)};"
            f"color: {text};"
            "border-radius: 16px;"
            "padding: 0px;"
            "margin: 0px;"
            "}"
            "QToolButton#OnboardingBackButton:hover {"
            f"background: {hover.name(QColor.NameFormat.HexArgb)};"
            "}"
        )

    def _sync_onboarding_back_button_visibility(self, *, force: bool = False) -> None:
        button = self._onboarding_back_btn
        if button is None:
            return
        show = force or (
            self._onboarding_stage == "services"
            and (self._onboarding_quick_restart or self._onboarding_manual_restart)
        )
        if show:
            self._reset_widget_opacity(button)
            button.setVisible(True)
            button.raise_()
        else:
            button.setVisible(False)

    def _ensure_merge_runtime_ready(self) -> None:
        if self._merge_ensure_in_progress:
            return
        self._merge_ensure_in_progress = True

        def _worker() -> None:
            try:
                self.context.merge.rebuild()
            except Exception:
                return
            finally:
                self._merge_ensure_in_progress = False
            self._ui_signals.component_action_done.emit("__merge__")

        threading.Thread(target=_worker, daemon=True).start()

    def _component_badge_state(self, component: object, state: object, any_running: bool) -> tuple[str, str]:
        status = str(getattr(state, "status", "unknown") or "unknown").lower()
        last_error = str(getattr(state, "last_error", "") or "").strip()
        enabled = bool(getattr(component, "enabled", False))
        if status == "running":
            return self._t("Работает", "Running"), "status_ok.svg"
        if last_error or (enabled and any_running):
            return self._t("Ошибка", "Error") if last_error else self._t("Не Запущен", "Not Running"), "status_warn.svg"
        if status == "stopped":
            return self._t("Остановлен", "Stopped"), "status_off.svg"
        return self._t("Неизвестно", "Unknown"), "status_off.svg"

    def _refresh_general_combo(self, selected_id: str) -> None:
        options = self._general_options_for_current_service_tests(self._sorted_general_options())
        self._updating_general_combo = True
        try:
            self.general_combo.clear()
            for option in options:
                label = self._format_general_option_label(option)
                self.general_combo.addItem(label, option["id"])
            if self.general_combo.count() == 0:
                return
            target_id = selected_id
            if not target_id:
                target_id = self.general_combo.itemData(0)
            for i in range(self.general_combo.count()):
                if self.general_combo.itemData(i) == target_id:
                    self.general_combo.setCurrentIndex(i)
                    break
        finally:
            self._updating_general_combo = False

    def _on_general_selected(self, _index: int) -> None:
        if self._updating_general_combo:
            return
        selected = self.general_combo.currentData()
        if not selected:
            return
        current = self.context.settings.get().selected_zapret_general
        if selected == current:
            return
        self.context.settings.get().selected_zapret_general = selected
        states = self._component_states()
        zapret_running = states.get("zapret") and states["zapret"].status == "running"
        if zapret_running:
            self._loading_action = "connect"
            self._toggle_in_progress = True
            self.power_button.setEnabled(False)
            self._loading_frame = 0
            self._loading_timer.start()
            self._advance_loading_caption()
            self._submit_backend_task("select_general", {"selected": selected}, action_id="__general__")
        else:
            self._submit_backend_task("select_general", {"selected": selected}, action_id="__general__")
            self._mark_dirty("dashboard", "components", "tray")

    def _on_general_selected_from_components(self, selected: str, combo: QComboBox, status_label: QLabel) -> None:
        if not selected:
            return
        current = self.context.settings.get().selected_zapret_general
        if selected == current:
            return
        if self._general_loading_combo is not None:
            return
        self._general_loading_combo = combo
        self._general_loading_label = status_label
        combo.setEnabled(False)
        status_label.show()
        self._component_loading_frame = 0
        if not self._component_loading_timer.isActive():
            self._component_loading_timer.start()
        self._advance_component_loading()
        self._submit_backend_task("select_general", {"selected": selected}, action_id="__general__")

    def _apply_general_selection_worker(self, selected: str) -> None:
        self.context.settings.get().selected_zapret_general = selected
        self.context.settings.save()
        states = self._component_states()
        zapret_running = states.get("zapret") and states["zapret"].status == "running"
        if zapret_running:
            self.context.processes.stop_component("zapret")
            self.context.processes.start_component("zapret")
        self._ui_signals.component_action_done.emit("__general__")

    def _sync_general_favorite_button(self, general_id: str, button: QToolButton) -> None:
        favorite = self._is_general_favorite(general_id)
        button.setIcon(self._icon("star_filled.svg" if favorite else "star_outline.svg"))
        button.setIconSize(QSize(16, 16))
        button.setToolTip(
            self._t("Убрать из избранного", "Remove from favorites")
            if favorite
            else self._t("Добавить в избранное", "Add to favorites")
        )

    def _toggle_general_favorite_from_button(self, general_id: str, button: QToolButton) -> None:
        if not general_id:
            return
        favorite = not self._is_general_favorite(general_id)
        self._sync_general_favorite_button(general_id, button)
        current = self.context.settings.get()
        favorites = [item for item in self._favorite_general_ids() if item]
        if favorite and general_id not in favorites:
            favorites.append(general_id)
        if not favorite:
            favorites = [item for item in favorites if item != general_id]
        current.favorite_zapret_generals = favorites
        self._refresh_general_combo(current.selected_zapret_general)
        self._mark_dirty("components", "tray")
        self._submit_backend_task("set_favorite_generals", {"favorites": favorites}, action_id="__favorite__")

    def _master_active_components(self) -> list[str]:
        return [c.id for c in self._component_defs().values() if c.enabled]

    def _maybe_run_first_general_autotest(self) -> None:
        settings = self.context.settings.get()
        if self._skip_autosettings:
            self.context.settings.update(general_autotest_done=True)
            return
        if settings.general_autotest_done:
            return
        options = self._sorted_general_options()
        if not options:
            return
        self._set_onboarding_visible(True)

    def _set_onboarding_visible(self, visible: bool) -> None:
        self._onboarding_active = visible
        self._onboarding_transition_busy = False
        self._onboarding_transition_token += 1
        self._clear_onboarding_intro_transition_overlay()
        if not visible:
            self._stop_onboarding_glow_orbit()
            self._onboarding_quick_restart = False
        if visible:
            theme = self.context.settings.get().theme
            if self._onboarding_quick_restart:
                self._apply_onboarding_quick_chrome(theme, True)
                if self._onboarding_widget is not None:
                    self._onboarding_widget.setVisible(True)
                if self._pages_shell is not None:
                    self._pages_shell.setVisible(False)
                if self._sidebar_widget is not None:
                    self._sidebar_widget.setVisible(False)
                if self._notifications_btn is not None:
                    self._notifications_btn.setVisible(False)
            else:
                self._reset_onboarding_intro_state()
                self._prepare_onboarding_services_stage()
                self._sync_onboarding_background_stage(animated=False)
            if not self._onboarding_prewarming:
                QTimer.singleShot(0, self._schedule_onboarding_services_prewarm)
        if self._onboarding_widget is not None:
            self._onboarding_widget.setVisible(visible)
        if self._pages_shell is not None:
            self._pages_shell.setVisible(not visible)
            if not visible:
                self._pages_shell.show()
        if self._page_transition_overlay is not None:
            self._page_transition_overlay.hide()
            self._page_transition_overlay.clear_transition()
        self._page_transition_running = False
        self._page_transition_started_at = 0.0
        self._page_transition_target = self.pages.currentIndex() if hasattr(self, "pages") else -1
        if self._content_surface_layout is not None:
            if visible:
                self._content_surface_layout.setContentsMargins(0, 0, 0, 0)
                self._content_surface_layout.setSpacing(0)
            else:
                self._content_surface_layout.setContentsMargins(12, 12, 12, 0)
                self._content_surface_layout.setSpacing(8)
        if self._sidebar_widget is not None:
            self._sidebar_widget.setVisible(not visible)
        if self._notifications_btn is not None:
            self._notifications_btn.setVisible(not visible)
        if not visible and self._onboarding_service_action_btn is not None:
            self._onboarding_service_action_btn.hide()
        if not visible and self._onboarding_back_btn is not None:
            self._onboarding_back_btn.hide()
        if not visible:
            self._apply_onboarding_chrome(self.context.settings.get().theme, False)
        elif not self._onboarding_quick_restart:
            self._apply_onboarding_style()
        self._relayout_onboarding_content()
        if visible:
            QTimer.singleShot(0, self._relayout_onboarding_content)
        else:
            QTimer.singleShot(160, self._cache_quick_onboarding_entry_snapshot)

    def _apply_onboarding_quick_chrome(self, theme: str, onboarding_active: bool) -> None:
        if self._content_surface is None:
            return
        chrome = _chrome_surface_color(theme).name()
        root_frame = self.findChild(OnboardingFrame, "RootFrame")
        if root_frame is not None:
            root_frame.set_onboarding_background(QColor(chrome), onboarding_active)
        if self._onboarding_services_fade is not None:
            self._onboarding_services_fade.set_onboarding_background_frame(root_frame if onboarding_active else None)
        title_bar = self.findChild(QFrame, "TitleBar")
        if title_bar is not None:
            title_bar.setAutoFillBackground(False)
            title_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            title_bar.setStyleSheet(
                "QFrame#TitleBar {"
                "background: transparent;"
                "border: none;"
                "border-top-left-radius: 16px;"
                "border-top-right-radius: 16px;"
                "}"
                "QFrame#TitleBar QLabel, QFrame#TitleBar QToolButton {"
                "background: transparent;"
                "}"
            )
        self._content_surface.setStyleSheet(
            "QFrame#ContentSurface {"
            "background: transparent;"
            "border: none;"
            "border-top-left-radius: 18px;"
            "border-top-right-radius: 0px;"
            "border-bottom-left-radius: 16px;"
            "border-bottom-right-radius: 16px;"
            "}"
        )
        if isinstance(self._onboarding_widget, OnboardingPageWidget):
            self._onboarding_widget.set_background_color(QColor(0, 0, 0, 0) if onboarding_active else QColor(chrome))
            if self._onboarding_widget.property("onboardingPageStyleReady") is not True:
                self._onboarding_widget.setStyleSheet("QWidget#OnboardingPage { border: none; }")
                self._onboarding_widget.setProperty("onboardingPageStyleReady", True)
        elif self._onboarding_widget is not None:
            self._onboarding_widget.setStyleSheet(f"QWidget#OnboardingPage {{ background: {chrome}; border: none; }}")

    def _sync_onboarding_background_stage(self, *, animated: bool = True) -> None:
        frame = self.findChild(OnboardingFrame, "RootFrame")
        if frame is None:
            return
        stage = self._onboarding_stage
        if stage == "intro":
            target = (0.12, 0.55)
        elif stage in {"services_transition", "services"}:
            target = (0.50, 1.18)
        elif stage == "running":
            target = (0.84, 0.18)
        elif stage in {"success", "failed"}:
            target = (0.50, -0.08)
        else:
            target = (0.50, 1.18)
        frame.set_glow_position(target[0], target[1], animated=animated)
        if self._onboarding_services_fade is not None:
            self._onboarding_services_fade.update()

    def _start_onboarding_glow_orbit(self) -> None:
        self._onboarding_glow_orbit_index = 0
        self._onboarding_glow_orbit_phase = -0.88
        self._advance_onboarding_glow_orbit()
        if not self._onboarding_glow_orbit_timer.isActive():
            self._onboarding_glow_orbit_timer.start()

    def _stop_onboarding_glow_orbit(self) -> None:
        if self._onboarding_glow_orbit_timer.isActive():
            self._onboarding_glow_orbit_timer.stop()

    def _advance_onboarding_glow_orbit(self) -> None:
        frame = self.findChild(OnboardingFrame, "RootFrame")
        if frame is None or not self._onboarding_active or self._onboarding_stage != "running":
            return
        self._onboarding_glow_orbit_phase = (self._onboarding_glow_orbit_phase + 0.018) % (math.pi * 2.0)
        phase = self._onboarding_glow_orbit_phase
        x = 0.5 + math.cos(phase) * 0.43
        y = 0.5 + math.sin(phase) * 0.72
        frame.set_glow_position(x, y, animated=False)

    def _restore_sidebar_after_onboarding(self) -> None:
        self._nav_highlight_initialized = False
        if self._sidebar_widget is not None:
            if self._sidebar_widget.layout() is not None:
                self._sidebar_widget.layout().activate()
            self._sidebar_widget.updateGeometry()
            self._sidebar_widget.update()
        sidebar = self.findChild(SidebarPanel, "Sidebar")
        if sidebar is not None:
            sidebar.clear_highlight()
        self._sync_nav_highlight(animated=False)

    def _clear_onboarding_intro_transition_overlay(self) -> None:
        overlay = self._onboarding_intro_transition_overlay
        if overlay is None:
            return
        try:
            effect = overlay.graphicsEffect()
            if isinstance(effect, QGraphicsOpacityEffect):
                effect.setOpacity(1.0)
            overlay.hide()
            overlay.deleteLater()
        finally:
            self._onboarding_intro_transition_overlay = None

    def _handle_onboarding_primary_action(self) -> None:
        if self._onboarding_transition_busy:
            return
        if self._onboarding_stage == "intro":
            self._show_onboarding_services_stage()
            return
        if self._onboarding_stage == "services_transition":
            return
        if self._onboarding_stage == "services":
            if len(self._selected_service_ids()) < self._onboarding_services_minimum:
                self._update_service_selection_summary()
                return
            self._start_onboarding_flow()
            return
        if self._onboarding_stage in {"success", "failed"}:
            self._finish_onboarding()
            return

    def _handle_onboarding_secondary_action(self) -> None:
        if self._onboarding_stage == "services":
            self._skip_onboarding()
            return
        self._skip_onboarding()

    def _show_onboarding_services_stage(self) -> None:
        if self._onboarding_transition_busy:
            return
        self._onboarding_stage = "services"
        self._sync_onboarding_background_stage(animated=True)
        self._run_onboarding_transition(
            outgoing=self._onboarding_intro_panel,
            incoming=self._onboarding_services_stage_panel,
            out_duration=230,
            in_duration=190,
            overlap=100,
            prepare_in=self._finish_show_onboarding_services_stage,
        )

    def _finish_show_onboarding_services_stage(self, token: int | None = None) -> None:
        if token is not None and token != self._onboarding_transition_token:
            return
        if self._onboarding_title_label is not None:
            self._onboarding_title_label.setText(self._t("Выберите сервисы", "Choose services"))
        if self._onboarding_desc_label is not None:
            self._onboarding_desc_label.setText(
                self._t(
                    "Выберите от трёх приложений, сайтов и сервисов, которыми вы планируете пользоваться. Приложение само настроит Zapret так, чтобы выбранные сервисы работали.",
                    "Choose at least three apps, sites, and services you plan to use. The app will configure Zapret so the selected services work.",
                )
            )
        if self._onboarding_services_panel is not None:
            self._onboarding_services_panel.show()
        if self._onboarding_services_search is not None:
            self._onboarding_services_search.clear()
            self._onboarding_services_search.setFocus()
        if self._onboarding_primary_btn is not None:
            self._onboarding_primary_btn.setText(self._t("Продолжить", "Continue"))
        if self._onboarding_actions_widget is not None:
            self._onboarding_actions_widget.hide()
        if self._onboarding_service_action_btn is not None:
            self._onboarding_service_action_btn.set_theme(self.context.settings.get().theme)
            self._onboarding_service_action_btn.set_force_light(True)
            self._onboarding_service_action_btn.set_selection_state(
                len(self._selected_service_ids()),
                self._onboarding_services_minimum,
                text=self._t("Продолжить", "Continue"),
            )
            self._onboarding_service_action_btn.show()
            self._position_onboarding_service_action()
            self._onboarding_service_action_btn.raise_()
        if self._onboarding_secondary_btn is not None:
            self._onboarding_secondary_btn.hide()
            self._onboarding_secondary_btn.setText(self._t("Пропустить", "Skip"))
        self._update_service_selection_summary()
        if self._onboarding_services_scroll is not None:
            QTimer.singleShot(0, lambda: self._onboarding_services_scroll.verticalScrollBar().setValue(0))
        self._sync_onboarding_back_button_visibility()

    def _sync_onboarding_service_action_button(self) -> None:
        button = self._onboarding_service_action_btn
        if button is None or self._onboarding_stage != "services" or self._onboarding_transition_busy:
            return
        self._reset_widget_opacity(button)
        button.setEnabled(True)
        button.setVisible(True)
        button.set_theme(self.context.settings.get().theme)
        button.set_selection_state(
            len(self._selected_service_ids()),
            self._onboarding_services_minimum,
            text=self._t("Продолжить", "Continue"),
        )
        self._position_onboarding_service_action()
        button.raise_()

    def _reset_onboarding_intro_state(self) -> None:
        self._onboarding_running = False
        self._onboarding_stage = "intro"
        self._sync_onboarding_background_stage(animated=False)
        self._reset_widget_opacity(self._onboarding_intro_panel)
        self._reset_widget_opacity(self._onboarding_services_stage_panel)
        self._reset_widget_opacity(self._onboarding_running_stage_panel)
        self._reset_widget_opacity(self._onboarding_result_stage_panel)
        self._reset_widget_opacity(self._onboarding_actions_widget)
        if self._onboarding_stage_layout is not None and self._onboarding_intro_panel is not None:
            self._onboarding_stage_layout.setCurrentWidget(self._onboarding_intro_panel)
        if self._onboarding_intro_panel is not None:
            self._onboarding_intro_panel.show()
        if self._onboarding_services_stage_panel is not None:
            self._onboarding_services_stage_panel.hide()
        if self._onboarding_running_stage_panel is not None:
            self._onboarding_running_stage_panel.hide()
        if self._onboarding_result_stage_panel is not None:
            self._onboarding_result_stage_panel.hide()
        legacy_seen = self._legacy_onboarding_seen() and not self._onboarding_seen() and not self._onboarding_manual_restart
        if self._onboarding_intro_title_label is not None:
            self._onboarding_intro_title_label.setText(self._t("Приложение обновилось", "The app has been updated") if legacy_seen else self._t("Добро пожаловать", "Welcome"))
        if self._onboarding_intro_desc_label is not None:
            self._onboarding_intro_desc_label.setText(
                self._t(
                    "В новой версии Zapret-Zen появилась настройка обхода по сервисам. Выберите приложения, сайты и игры, которыми пользуетесь, а приложение само подготовит подходящие правила.",
                    "This Zapret-Zen update adds per-service bypass setup. Choose the apps, sites, and games you use, and the app will prepare the right rules automatically.",
                )
                if legacy_seen
                else self._t(
                    "Zapret-Zen - это ваш главный помощник в обходе сервисов. Хотите приступить к первичной настройке?",
                    "Zapret-Zen is your ultimate assistant for bypassing restrictions. Ready to run the initial setup?",
                )
            )
        if self._onboarding_result_card is not None:
            self._onboarding_result_card.hide()
        if self._onboarding_progress_label is not None:
            self._onboarding_progress_label.hide()
            self._onboarding_progress_label.setText("")
        if self._onboarding_progress_counter_label is not None:
            self._onboarding_progress_counter_label.hide()
            self._onboarding_progress_counter_label.setText("")
        if self._onboarding_progress_bar is not None:
            self._onboarding_progress_bar.hide()
            self._onboarding_progress_bar.setValue(0)
        if self._onboarding_services_panel is not None:
            self._onboarding_services_panel.hide()
        if self._onboarding_result_actions_widget is not None:
            self._onboarding_result_actions_widget.hide()
        if self._onboarding_result_primary_btn is not None:
            self._onboarding_result_primary_btn.setText(self._t("Далее", "Next"))
        if self._onboarding_primary_btn is not None:
            self._onboarding_primary_btn.setEnabled(True)
            self._onboarding_primary_btn.setVisible(True)
            self._onboarding_primary_btn.setText(self._t("Далее", "Next"))
        if self._onboarding_secondary_btn is not None:
            self._onboarding_secondary_btn.hide()
            self._onboarding_secondary_btn.setText(self._t("Пропустить", "Skip"))
        if self._onboarding_actions_widget is not None:
            self._onboarding_actions_widget.show()
        if self._onboarding_service_action_btn is not None:
            self._reset_widget_opacity(self._onboarding_service_action_btn)
            self._onboarding_service_action_btn.hide()
        self._update_service_selection_summary()
        self._relayout_onboarding_content()

    def _animate_widget_visibility(self, widget: QWidget, visible: bool, *, duration: int = 220) -> QPropertyAnimation:
        effect = getattr(widget, "_opacity_effect", None)
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
            widget._opacity_effect = effect  # type: ignore[attr-defined]
        animation = getattr(widget, "_opacity_animation", None)
        if isinstance(animation, QPropertyAnimation):
            animation.stop()
        if visible:
            widget.show()
            effect.setOpacity(0.0)
            start_value = 0.0
            end_value = 1.0
        else:
            start_value = float(effect.opacity())
            end_value = 0.0
        animation = QPropertyAnimation(effect, b"opacity", widget)
        animation.setDuration(duration)
        animation.setStartValue(start_value)
        animation.setEndValue(end_value)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        if not visible:
            animation.finished.connect(widget.hide)
        animation.start()
        widget._opacity_animation = animation  # type: ignore[attr-defined]
        return animation

    def _reset_widget_opacity(self, widget: QWidget | None) -> None:
        if widget is None:
            return
        animation = getattr(widget, "_opacity_animation", None)
        if isinstance(animation, QPropertyAnimation):
            animation.stop()
        effect = getattr(widget, "_opacity_effect", None)
        if isinstance(effect, QGraphicsOpacityEffect):
            effect.setOpacity(1.0)
        widget.show()

    def _run_onboarding_transition(
        self,
        *,
        outgoing: QWidget | None,
        incoming: QWidget | None,
        out_duration: int,
        in_duration: int,
        overlap: int,
        prepare_in,
        on_finished=None,
    ) -> None:
        if outgoing is None or incoming is None:
            return
        self._onboarding_transition_token += 1
        token = self._onboarding_transition_token
        self._onboarding_transition_busy = True
        self._ensure_widget_opacity_ready(outgoing)
        self._ensure_widget_opacity_ready(incoming)
        outgoing_effect = getattr(outgoing, "_opacity_effect", None)
        incoming_effect = getattr(incoming, "_opacity_effect", None)
        if not isinstance(outgoing_effect, QGraphicsOpacityEffect) or not isinstance(incoming_effect, QGraphicsOpacityEffect):
            self._onboarding_transition_busy = False
            return

        for panel in (
            self._onboarding_intro_panel,
            self._onboarding_services_stage_panel,
            self._onboarding_running_stage_panel,
            self._onboarding_result_stage_panel,
        ):
            animation = getattr(panel, "_opacity_animation", None)
            if isinstance(animation, QPropertyAnimation):
                animation.stop()

        outgoing.show()
        outgoing.raise_()
        outgoing_effect.setOpacity(1.0)
        incoming.hide()
        incoming_effect.setOpacity(0.0)

        out_animation = QPropertyAnimation(outgoing_effect, b"opacity", outgoing)
        out_animation.setDuration(out_duration)
        out_animation.setStartValue(1.0)
        out_animation.setEndValue(0.0)
        out_animation.setEasingCurve(QEasingCurve.Type.InCubic)
        outgoing._opacity_animation = out_animation  # type: ignore[attr-defined]
        out_animation.start()

        begin_delay = max(0, out_duration - max(0, overlap))
        end_delay = max(out_duration, begin_delay + in_duration)

        def _begin_in() -> None:
            if token != self._onboarding_transition_token:
                return
            prepare_in(token)
            if self._onboarding_stage_layout is not None:
                self._onboarding_stage_layout.setCurrentWidget(incoming)
            incoming.show()
            incoming.raise_()
            incoming_effect.setOpacity(0.0)
            in_animation = QPropertyAnimation(incoming_effect, b"opacity", incoming)
            in_animation.setDuration(in_duration)
            in_animation.setStartValue(0.0)
            in_animation.setEndValue(1.0)
            in_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            incoming._opacity_animation = in_animation  # type: ignore[attr-defined]
            in_animation.start()

        QTimer.singleShot(begin_delay, _begin_in)
        QTimer.singleShot(
            end_delay,
            lambda: self._finish_onboarding_transition(
                token,
                outgoing=outgoing,
                incoming=incoming,
                on_finished=on_finished,
            ),
        )

    def _finish_onboarding_transition(
        self,
        token: int,
        *,
        outgoing: QWidget,
        incoming: QWidget,
        on_finished=None,
    ) -> None:
        if token != self._onboarding_transition_token:
            return
        outgoing.hide()
        outgoing_effect = getattr(outgoing, "_opacity_effect", None)
        incoming_effect = getattr(incoming, "_opacity_effect", None)
        if isinstance(outgoing_effect, QGraphicsOpacityEffect):
            outgoing_effect.setOpacity(1.0)
        if isinstance(incoming_effect, QGraphicsOpacityEffect):
            incoming_effect.setOpacity(1.0)
        incoming.show()
        incoming.raise_()
        self._onboarding_transition_busy = False
        if self._onboarding_stage == "services":
            QTimer.singleShot(0, self._sync_onboarding_service_action_button)
        if on_finished is not None:
            on_finished(token)

    def _skip_onboarding(self) -> None:
        self._mark_onboarding_seen()
        self.context.settings.update(general_autotest_done=True)
        self._submit_backend_task("set_general_autotest_done", {"done": True}, action_id="__autotest_declined__")
        self._set_onboarding_visible(False)
        self.refresh_all()
        QTimer.singleShot(0, self._restore_sidebar_after_onboarding)
        QTimer.singleShot(80, self._restore_sidebar_after_onboarding)

    def _start_onboarding_flow(self) -> None:
        if self._onboarding_running or self._onboarding_transition_busy:
            return
        if self._onboarding_back_btn is not None and self._onboarding_back_btn.isVisible():
            self._animate_widget_visibility(self._onboarding_back_btn, False, duration=140)
        self._onboarding_stage = "running"
        self._sync_onboarding_background_stage(animated=True)
        self._start_onboarding_glow_orbit()
        self._onboarding_running = True
        self._finish_start_onboarding_flow()
        self._run_onboarding_transition(
            outgoing=self._onboarding_services_stage_panel,
            incoming=self._onboarding_running_stage_panel,
            out_duration=230,
            in_duration=200,
            overlap=55,
            prepare_in=lambda _token: None,
            on_finished=lambda token: (
                token == self._onboarding_transition_token
                and self._run_general_tests_popup(auto_apply=True, embedded=True)
            ),
        )

    def _finish_start_onboarding_flow(self, token: int | None = None) -> None:
        if token is not None and token != self._onboarding_transition_token:
            return
        if token is not None and self._onboarding_back_btn is not None:
            self._onboarding_back_btn.hide()
        if self._onboarding_running_title_label is not None:
            self._onboarding_running_title_label.setText(self._t("Подбор конфигурации", "Selecting configuration"))
        if self._onboarding_running_desc_label is not None:
            self._onboarding_running_desc_label.setText(
                self._t(
                    "Сейчас приложение проверит доступные конфигурации и автоматически выберет первую полностью рабочую.",
                    "The app will now check available configurations and automatically choose the first fully working one.",
                )
            )
        if self._onboarding_progress_label is not None:
            self._onboarding_progress_label.setText(self._t("Подготовка...", "Preparing..."))
            self._onboarding_progress_label.show()
        if self._onboarding_progress_counter_label is not None:
            self._onboarding_progress_counter_label.setText("")
            self._onboarding_progress_counter_label.show()
        if self._onboarding_progress_bar is not None:
            self._onboarding_progress_bar.setMaximum(100)
            self._onboarding_progress_bar.setValue(0)
            self._onboarding_progress_bar.show()
        if self._onboarding_actions_widget is not None:
            self._onboarding_actions_widget.hide()
        if self._onboarding_service_action_btn is not None:
            self._onboarding_service_action_btn.hide()

    def _show_onboarding_completion_stage(
        self,
        *,
        success: bool,
        chosen_id: str,
        best_failed_targets: list[object],
    ) -> None:
        self._onboarding_stage = "success" if success else "failed"
        self._sync_onboarding_background_stage(animated=True)

        def _begin_completion_stage(token: int) -> None:
            if token != self._onboarding_transition_token:
                return
            if self._onboarding_result_title_label is not None:
                self._onboarding_result_title_label.setText(
                    self._t("Настройка завершена", "Setup complete")
                    if success
                    else self._t("Настройка не завершена", "Setup was not completed")
                )
            if self._onboarding_result_desc_label is not None:
                if success:
                    failed_service_ids = self._service_ids_from_failed_targets(best_failed_targets)
                    if failed_service_ids:
                        failed_names = ", ".join(self._service_title_by_id(service_id) for service_id in failed_service_ids)
                        text = self._t(
                            f"Приложение выбрало лучшую доступную конфигурацию, но не смогло настроить подключение к: {failed_names}. Эти сервисы могут не работать.",
                            f"The app selected the best available configuration, but could not configure access to: {failed_names}. These services may not work.",
                        )
                    else:
                        text = self._t(
                            "Подходящая конфигурация уже выбрана и применена. Можно перейти в главное меню.",
                            "A suitable configuration has been selected and applied. You can continue to the main interface.",
                        )
                else:
                    text = self._t(
                        "Не удалось автоматически подобрать полностью рабочую конфигурацию. Вы можете продолжить без этого шага.",
                        "Could not automatically find a fully working configuration. You can continue without this step.",
                    )
                self._onboarding_result_desc_label.setText(text)
            if success and chosen_id and self._onboarding_result_card is not None:
                chosen_label = self._format_general_option_label(
                    next((item for item in self._sorted_general_options() if item["id"] == chosen_id), {"id": chosen_id, "bundle": "", "name": chosen_id})
                )
                if self._onboarding_found_label is not None:
                    self._onboarding_found_label.setText(self._format_onboarding_general_line(f"General: {chosen_label}"))
                self._reset_widget_opacity(self._onboarding_result_card)
                self._onboarding_result_card.show()
            elif self._onboarding_result_card is not None:
                self._onboarding_result_card.hide()
            if self._onboarding_actions_widget is not None:
                self._onboarding_actions_widget.hide()
            if self._onboarding_result_actions_widget is not None:
                self._reset_widget_opacity(self._onboarding_result_actions_widget)
                self._onboarding_result_actions_widget.show()
            if self._onboarding_result_primary_btn is not None:
                self._onboarding_result_primary_btn.setEnabled(True)
                self._onboarding_result_primary_btn.setVisible(True)
                self._onboarding_result_primary_btn.setText(self._t("Далее", "Next") if success else self._t("Продолжить", "Continue"))
            if self._onboarding_secondary_btn is not None:
                self._onboarding_secondary_btn.hide()
        self._run_onboarding_transition(
            outgoing=self._onboarding_running_stage_panel,
            incoming=self._onboarding_result_stage_panel,
            out_duration=190,
            in_duration=210,
            overlap=82,
            prepare_in=_begin_completion_stage,
        )

    def _finish_onboarding(self) -> None:
        self._mark_onboarding_seen()
        self._onboarding_quick_restart = False
        self._fade_out_onboarding_to_app()

    def _fade_out_onboarding_to_app(self) -> None:
        if self._onboarding_widget is None:
            self._set_onboarding_visible(False)
            self.refresh_all()
            QTimer.singleShot(0, self._restore_sidebar_after_onboarding)
            return
        self._stop_onboarding_glow_orbit()
        pixmap = self._onboarding_widget.grab()
        top_left = self._onboarding_widget.mapTo(self, QPoint(0, 0))
        overlay = QLabel(self)
        overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        overlay.setPixmap(pixmap)
        overlay.setGeometry(QRect(top_left, self._onboarding_widget.size()))
        overlay.show()
        overlay.raise_()
        self._startup_snapshot_ready = True
        self._set_onboarding_visible(False)
        self._sync_power_aura_geometry()
        self.refresh_all()
        QTimer.singleShot(0, self._restore_sidebar_after_onboarding)
        QTimer.singleShot(0, self._sync_power_aura_geometry)
        QTimer.singleShot(80, self._sync_power_aura_geometry)
        effect = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(effect)
        effect.setOpacity(1.0)
        anim = QPropertyAnimation(effect, b"opacity", overlay)
        anim.setDuration(280)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def _finish() -> None:
            overlay.hide()
            overlay.deleteLater()
            QTimer.singleShot(80, self._restore_sidebar_after_onboarding)

        anim.finished.connect(_finish)
        overlay._finish_fade_animation = anim  # type: ignore[attr-defined]
        anim.start()

    def _selected_service_ids(self) -> list[str]:
        selected = list(self.context.settings.get().selected_service_ids or [])
        valid = {preset.id for preset in SERVICE_PRESETS}
        return [item for item in selected if item in valid]

    def _normalize_service_ids(self, service_ids: list[str] | tuple[str, ...] | set[str]) -> list[str]:
        raw = {str(item).strip() for item in service_ids if str(item).strip()}
        ordered: list[str] = []
        for preset in SERVICE_PRESETS:
            if preset.id in raw:
                ordered.append(preset.id)
        return ordered

    def _apply_fortnite_service_preferences_locally(self) -> None:
        changes: dict[str, str] = {
            "zapret_ipset_mode": "any",
            "zapret_game_filter_mode": "tcpudp",
        }
        options = self._general_options_for_current_service_tests(self._sorted_general_options())
        for wanted in FORTNITE_GENERAL_PRIORITY:
            match = next((option for option in options if str(option.get("name", "")).strip().lower() == wanted.lower()), None)
            if match is not None and str(match.get("id", "")).strip():
                changes["selected_zapret_general"] = str(match["id"])
                break
        self.context.settings.update(**changes)

    def _on_service_card_toggled(self, service_id: str, selected: bool) -> None:
        current = set(self._selected_service_ids())
        selected = service_id not in current
        if selected:
            current.add(service_id)
        else:
            current.discard(service_id)
        self._set_selected_service_ids(list(current))

    def _refresh_service_cards_subset(self, service_ids: set[str], *, theme: str | None = None) -> None:
        if not service_ids:
            return
        active_theme = theme or self.context.settings.get().theme
        selected = set(self._selected_service_ids())
        preset_map = {preset.id: preset for preset in SERVICE_PRESETS}
        for service_id in service_ids:
            preset = preset_map.get(service_id)
            if preset is None:
                continue
            is_selected = service_id in selected
            for card in self._service_cards_by_id.get(service_id, []):
                try:
                    card.blockSignals(True)
                    card.set_icon_pixmap(self._service_icon_pixmap(preset, 34, selected=is_selected))
                    card.set_check_pixmap(self._service_check_pixmap(10))
                    card.set_selected(is_selected)
                finally:
                    try:
                        card.blockSignals(False)
                    except Exception:
                        pass

    def _schedule_selected_services_backend_sync(self, normalized: list[str], revision: int) -> None:
        self._pending_selected_service_ids = list(normalized)
        self._pending_selected_services_revision = int(revision)
        self._optimistic_selected_service_ids = list(normalized)
        self._services_sync_timer.start(140)

    def _restore_optimistic_service_selection_if_needed(self) -> None:
        if self._optimistic_selected_service_ids is None:
            return
        if self._services_selection_acked_revision >= self._services_selection_revision:
            self._optimistic_selected_service_ids = None
            return
        normalized = self._normalize_service_ids(self._optimistic_selected_service_ids)
        if normalized != self._selected_service_ids():
            self.context.settings.update(selected_service_ids=normalized)
            if "fortnite" in normalized:
                self._apply_fortnite_service_preferences_locally()

    def _flush_selected_services_backend_sync(self) -> None:
        pending = list(self._pending_selected_service_ids or [])
        revision = int(self._pending_selected_services_revision)
        self._pending_selected_service_ids = None
        self._pending_selected_services_revision = 0
        if self.context.backend is None:
            return
        try:
            self._submit_backend_task(
                "set_selected_services",
                {"service_ids": pending, "client_revision": revision},
                action_id="__services_selection__",
            )
        except Exception as error:
            self._show_error(self._t("Сервисы", "Services"), str(error))

    def _set_selected_service_ids(self, service_ids: list[str] | tuple[str, ...] | set[str]) -> None:
        current = self._selected_service_ids()
        normalized = self._normalize_service_ids(service_ids)
        if normalized != current:
            self._services_selection_revision += 1
            revision = self._services_selection_revision
            self.context.settings.update(selected_service_ids=normalized)
            if "fortnite" in normalized:
                self._apply_fortnite_service_preferences_locally()
            changed = set(current).symmetric_difference(normalized)
            self._refresh_service_cards_subset(changed)
            self._update_service_selection_summary()
            self._schedule_selected_services_backend_sync(normalized, revision)
            return
        self._update_service_selection_summary()

    def _update_service_selection_summary(self) -> None:
        count = len(self._selected_service_ids())
        total = len(SERVICE_PRESETS)
        if self._services_count_label is not None:
            self._services_count_label.setText(
                self._t(
                    f"Выбрано: {count} из {total}",
                    f"Selected: {count} of {total}",
                )
            )
        if self._onboarding_services_count_label is not None:
            self._onboarding_services_count_label.hide()
            if count >= self._onboarding_services_minimum:
                text = self._t(
                    f"Выбрано {count} сервисов. Можно продолжать.",
                    f"{count} services selected. You can continue.",
                )
            else:
                remaining = self._onboarding_services_minimum - count
                text = self._t(
                    f"Нужно выбрать ещё {remaining}, минимум {self._onboarding_services_minimum}.",
                    f"Choose {remaining} more, at least {self._onboarding_services_minimum} total.",
                )
            self._onboarding_services_count_label.setText(text)
            good = count >= self._onboarding_services_minimum
            color = "#4f73d9" if (good and is_light_theme(self.context.settings.get().theme)) else "#b86b4b" if is_light_theme(self.context.settings.get().theme) else "#7ea5ff" if good else "#d18a5e"
            self._onboarding_services_count_label.setStyleSheet(
                f"color: {color}; background: transparent;"
            )
        if self._onboarding_primary_btn is not None and self._onboarding_stage == "services":
            self._onboarding_primary_btn.setEnabled(False)
            self._onboarding_primary_btn.setVisible(False)
        if self._onboarding_service_action_btn is not None:
            self._onboarding_service_action_btn.set_selection_state(
                count,
                self._onboarding_services_minimum,
                text=self._t("Продолжить", "Continue"),
            )
            self._position_onboarding_service_action()

    def refresh_services(self) -> None:
        theme = self.context.settings.get().theme
        selected = set(self._selected_service_ids())
        for preset in SERVICE_PRESETS:
            is_selected = preset.id in selected
            for card in self._service_cards_by_id.get(preset.id, []):
                title, description = self._service_card_texts(preset, scope=str(card.property("serviceScope") or "onboarding"))
                try:
                    card.blockSignals(True)
                    card.set_theme(theme)
                    card.set_texts(title, description)
                    card.set_icon_pixmap(self._service_icon_pixmap(preset, 34, selected=is_selected))
                    card.set_check_pixmap(self._service_check_pixmap(10))
                    card.set_selected(is_selected)
                finally:
                    try:
                        card.blockSignals(False)
                    except Exception:
                        pass
        self._update_service_selection_summary()
        if self._services_grid is not None:
            self._services_grid.updateGeometry()
        if self._onboarding_services_grid is not None:
            self._onboarding_services_grid.updateGeometry()

    def _restart_zapret_worker(self) -> None:
        self.context.settings.save()
        self.context.processes.stop_component("zapret")
        self.context.processes.start_component("zapret")
        self._ui_signals.toggle_done.emit()

    def _on_component_action_done(self, action_id: str) -> None:
        if action_id == "peshk0v-vpn":
            self._set_power_vpn_switch_pending(False)
            self._finish_vpn_mode_power_transition()

        if action_id == "__settings__":
            self._hide_loading_overlay()
            self._mark_dirty("dashboard", "components", "files", "tray")
            return

        if action_id == "__favorite__":
            return

        if action_id == "__autotest_declined__":
            return

        if action_id == "__merge__":
            self._mark_dirty("dashboard")
            return

        if action_id == "__merge_rebuild__":
            self._mark_dirty("dashboard", "mods", "files", "logs", "tray")
            return

        if action_id == "__files_collection__":
            self._mark_dirty("dashboard", "files", "logs", "tray")
            return

        if action_id == "__file_saved__":
            self._request_page_refresh("logs")
            return

        if action_id == "__general__":
            if self._general_loading_combo is not None:
                try:
                    self._general_loading_combo.setEnabled(True)
                except RuntimeError:
                    pass
            if self._general_loading_label is not None:
                try:
                    self._general_loading_label.hide()
                    self._general_loading_label.setText("")
                except RuntimeError:
                    pass
            self._general_loading_combo = None
            self._general_loading_label = None
            if not self._component_loading_buttons:
                self._component_loading_timer.stop()
            self._mark_dirty("dashboard", "components", "tray")
            return

        self._stop_component_loading(action_id)
        self._mark_dirty("dashboard", "components", "tray")

    def _run_general_tests_popup(self, auto_apply: bool = False, embedded: bool = False) -> None:
        if self._general_test_running:
            return
        options = self._general_options_for_current_service_tests(self._sorted_general_options())
        if not options:
            if embedded:
                self._onboarding_running = False
                if self._onboarding_progress_label is not None:
                    self._onboarding_progress_label.hide()
                if self._onboarding_progress_counter_label is not None:
                    self._onboarding_progress_counter_label.hide()
                if self._onboarding_progress_bar is not None:
                    self._onboarding_progress_bar.hide()
                if self._onboarding_actions_widget is not None:
                    self._onboarding_actions_widget.show()
            self._show_info(self._t("Подобрать конфигурацию", "Find best configuration"), self._t("Список конфигураций пока пуст.", "The configuration list is empty."))
            return

        self._general_test_running = True
        self._general_test_cancelled = False
        self._general_test_show_results = True
        self._general_test_auto_apply = auto_apply
        self._general_test_embedded = embedded
        self._general_test_original_general = str(self.context.settings.get().selected_zapret_general or "")
        self._general_test_started_at = time.time()
        self._general_test_current_index = 0
        self._general_test_total = len(options)
        self._general_test_last_progress_at = self._general_test_started_at
        self._general_test_options = options
        self._general_test_results = []
        self._general_test_next_option_index = 0
        targets = self.context.processes._load_standard_test_targets()
        self._general_test_target_budget_seconds = sum(3 if str(item.get("type", "url")) == "url" else 2 for item in targets)
        self._general_test_remaining_budget_seconds = max(1, self._general_test_total * self._general_test_target_budget_seconds)
        self._general_test_found_working_id = ""
        if embedded:
            self._general_test_dialog = None
            self._general_test_status_label = self._onboarding_progress_label
            self._general_test_eta_label = None
            self._general_test_counter_label = self._onboarding_progress_counter_label
            self._general_test_progress_bar = self._onboarding_progress_bar
            self._prepare_general_test_runtime_before_run()
            return

        dialog = AppDialog(self, self.context, self._t("Подобрать конфигурацию", "Find best configuration"))
        title = QLabel(
            self._t(
                "Сейчас приложение по очереди проверит все доступные конфигурации и посмотрит, какие из них действительно дают подключение ко всем тестовым серверам. Этот процесс может занять много времени.",
                "The app will now test each available configuration and show which ones can actually reach all test servers. This process may take a while.",
            )
        )
        title.setWordWrap(True)
        dialog.body_layout.addWidget(title)
        status = QLabel(self._t("Подготовка...", "Preparing..."))
        status.setProperty("class", "muted")
        dialog.body_layout.addWidget(status)
        eta = QLabel(self._t("Расчёт времени...", "Estimating time..."))
        eta.setProperty("class", "muted")
        dialog.body_layout.addWidget(eta)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        dialog.body_layout.addWidget(bar)
        counter = QLabel("")
        counter.setProperty("class", "muted")
        counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dialog.body_layout.addWidget(counter)
        dialog.prepare_and_center()
        dialog.show()
        self._general_test_dialog = dialog
        self._general_test_status_label = status
        self._general_test_eta_label = eta
        self._general_test_counter_label = counter
        self._general_test_progress_bar = bar
        dialog.rejected.connect(self._cancel_general_tests)
        self._update_general_test_eta()
        self._general_test_eta_timer.start()
        self._prepare_general_test_runtime_before_run()

    def _run_general_tests_worker(self) -> None:
        results = self.context.processes.run_general_diagnostics(
            progress_callback=lambda current, total, name: self._ui_signals.general_test_progress.emit(
                {
                    "target_current": current,
                    "target_total": total,
                    "target_name": name,
                    "config_index": max(1, self._general_test_next_option_index + 1),
                    "config_total": max(1, self._general_test_total),
                }
            ),
            stop_callback=lambda: self._general_test_cancelled,
        )
        self._ui_signals.general_test_done.emit(results)

    def _cancel_general_tests(self) -> None:
        if not self._general_test_running and not self._general_test_task_id:
            return
        self._general_test_cancelled = True
        self._general_test_show_results = False
        self._general_test_running = False
        self._general_test_eta_timer.stop()
        if self.context.backend is not None and self._general_test_task_id:
            self.context.backend.cancel(self._general_test_task_id)
        original = str(self._general_test_original_general or "").strip()
        if original:
            self.context.settings.update(selected_zapret_general=original)
        if self._general_test_dialog is not None:
            self._general_test_dialog = None
        self._general_test_status_label = None
        self._general_test_eta_label = None
        self._general_test_counter_label = None
        self._general_test_progress_bar = None
        self._general_test_auto_apply = False
        self._clear_windows_taskbar_progress()
        if self._general_test_embedded:
            self._general_test_embedded = False
            self._onboarding_running = False
            if self._onboarding_progress_label is not None:
                self._onboarding_progress_label.setText(self._t("Подбор конфигурации остановлен.", "Configuration selection stopped."))
            if self._onboarding_primary_btn is not None:
                self._onboarding_primary_btn.setEnabled(True)
                self._onboarding_primary_btn.setVisible(True)
                self._onboarding_primary_btn.setText(self._t("Далее", "Next"))
            if self._onboarding_actions_widget is not None:
                self._onboarding_actions_widget.show()
        self._mark_dirty("dashboard", "components", "tray")
        self._restore_general_test_runtime_after_run()

    def _start_next_general_test(self) -> None:
        if self._general_test_cancelled:
            return
        if self._general_test_next_option_index >= len(self._general_test_options):
            self._on_general_test_done(list(self._general_test_results))
            return
        option = self._general_test_options[self._general_test_next_option_index]
        config_index = self._general_test_next_option_index + 1
        if self._general_test_progress_bar is not None:
            self._general_test_progress_bar.setMaximum(100)
            self._general_test_progress_bar.setValue(0)
        if self._general_test_counter_label is not None:
            self._general_test_counter_label.setText(self._format_general_test_counter(config_index, self._general_test_total))
            self._general_test_counter_label.show()
        self._set_windows_taskbar_progress(0)
        self._general_test_task_id = self._submit_backend_task(
            "run_general_diagnostic_single",
            {
                "general_id": option["id"],
                "ipset_mode": option.get("ipset_mode", "loaded"),
                "game_mode": option.get("game_mode", "tcpudp"),
            },
            action_id="__general_test__",
        )

    def _format_general_test_counter(self, current: int, total: int) -> str:
        return self._t(
            f"{max(1, int(current))} конфигурация из {max(1, int(total))}",
            f"Configuration {max(1, int(current))} of {max(1, int(total))}",
        )

    def _on_general_test_progress(self, payload: object) -> None:
        if isinstance(payload, dict):
            current = int(payload.get("target_current", payload.get("current", 0)) or 0)
            total = int(payload.get("target_total", payload.get("total", 0)) or 0)
            name = str(payload.get("target_name", payload.get("name", "")) or "")
            config_index = int(payload.get("config_index", self._general_test_next_option_index + 1) or 1)
            config_total = int(payload.get("config_total", self._general_test_total) or self._general_test_total or 1)
        else:
            current = 0
            total = 0
            name = ""
            config_index = self._general_test_next_option_index + 1
            config_total = self._general_test_total or 1
        self._general_test_current_index = current
        self._general_test_last_progress_at = time.time()
        progress_value = 0
        if total > 0:
            progress_value = int(round((max(0, min(total, current)) / max(1, total)) * 100))
        if self._general_test_progress_bar is not None:
            self._general_test_progress_bar.setMaximum(100)
            self._general_test_progress_bar.setValue(max(0, min(100, progress_value)))
        if self._general_test_counter_label is not None:
            self._general_test_counter_label.setText(self._format_general_test_counter(config_index, config_total))
            self._general_test_counter_label.show()
        self._set_windows_taskbar_progress(progress_value)
        if self._general_test_status_label is not None:
            self._general_test_status_label.setText(
                self._t(
                    f"Проверяется: {name}" if name else "Проверяется текущая конфигурация...",
                    f"Checking: {name}" if name else "Checking current configuration...",
                )
            )
        self._update_general_test_eta()

    def _update_general_test_eta(self) -> None:
        if self._general_test_eta_label is None or self._general_test_total <= 0:
            return
        if self._general_test_started_at <= 0:
            self._general_test_eta_label.setText(self._t("Расчёт времени...", "Estimating time..."))
            return
        if self._general_test_running and self._general_test_remaining_budget_seconds > 0:
            self._general_test_remaining_budget_seconds = max(0, self._general_test_remaining_budget_seconds - 1)
        shown_seconds = max(1, int(round(self._general_test_remaining_budget_seconds * 0.75))) if self._general_test_running else 0
        self._general_test_eta_label.setText(
            self._t(
                f"Осталось примерно: {shown_seconds} сек.",
                f"About {shown_seconds}s remaining.",
            )
        )

    def _on_general_test_done(self, results: object) -> None:
        if self._general_test_cancelled:
            self._general_test_task_id = None
            self._general_test_auto_apply = False
            self._general_test_embedded = False
            self._clear_windows_taskbar_progress()
            return
        if isinstance(results, dict) and results.get("id"):
            self._general_test_task_id = None
            self._general_test_results.append(results)
            self._general_test_next_option_index += 1
            if self._general_test_progress_bar is not None:
                self._general_test_progress_bar.setMaximum(100)
                self._general_test_progress_bar.setValue(100)
            self._set_windows_taskbar_progress(100)
            passed = int(results.get("passed_targets", 0) or 0)
            total_targets = int(results.get("total_targets", 0) or 0)
            self._general_test_remaining_budget_seconds = max(
                0,
                self._general_test_remaining_budget_seconds - max(1, self._general_test_target_budget_seconds),
            )
            if str(results.get("status", "")) == "ok" and not self._general_test_found_working_id:
                self._general_test_found_working_id = str(results.get("id", ""))
                if self._general_test_embedded:
                    results = list(self._general_test_results)
                else:
                    dialog = AppDialog(self, self.context, self._t("Конфигурация найдена", "Working configuration found"))
                    label = QLabel(
                        self._t(
                            "Найдена полностью рабочая конфигурация. Остановиться и использовать её или продолжить проверку остальных?",
                            "A fully working configuration has been found. Stop and use it, or continue checking the rest?",
                        )
                    )
                    label.setWordWrap(True)
                    dialog.body_layout.addWidget(label)
                    row = QHBoxLayout()
                    row.addStretch(1)
                    stop_btn = QPushButton(self._t("Использовать найденный", "Use found config"))
                    cont_btn = QPushButton(self._t("Проверить остальные", "Check the rest"))
                    stop_btn.setProperty("class", "primary")
                    stop_btn.clicked.connect(dialog.accept)
                    cont_btn.clicked.connect(dialog.reject)
                    row.addWidget(cont_btn)
                    row.addWidget(stop_btn)
                    dialog.body_layout.addLayout(row)
                    dialog.prepare_and_center()
                    use_found = dialog.exec() == QDialog.DialogCode.Accepted
                    if use_found:
                        chosen_id = self._general_test_found_working_id
                        if chosen_id:
                            chosen_raw = next((raw for raw in self._general_test_results if str(raw.get("id", "")) == chosen_id), {})
                            current_settings = self.context.settings.get()
                            self.context.settings.update(
                                selected_zapret_general=chosen_id,
                                zapret_ipset_mode=str(chosen_raw.get("ipset_mode", current_settings.zapret_ipset_mode) or current_settings.zapret_ipset_mode),
                                zapret_game_filter_mode=str(chosen_raw.get("game_mode", current_settings.zapret_game_filter_mode) or current_settings.zapret_game_filter_mode),
                                general_autotest_done=True,
                            )
                            self._set_general_favorite(chosen_id, True)
                        results = list(self._general_test_results)
                    else:
                        self._start_next_general_test()
                        return
            elif self._general_test_next_option_index < len(self._general_test_options):
                self._start_next_general_test()
                return
            else:
                results = list(self._general_test_results)

        self._general_test_running = False
        self._general_test_task_id = None
        self._general_test_eta_timer.stop()
        if self._general_test_dialog is not None:
            self._general_test_dialog.accept()
        self._general_test_dialog = None
        self._general_test_status_label = None
        self._general_test_eta_label = None
        self._general_test_counter_label = None
        self._general_test_progress_bar = None
        self._clear_windows_taskbar_progress()
        if self.isMinimized() or not self.isActiveWindow():
            self._request_windows_attention()

        checked = results if isinstance(results, list) else []
        working: list[str] = []
        failed: list[str] = []
        best_label = ""
        best_score = -1
        best_total = 0
        best_id = ""
        best_working_id = ""
        best_failed_targets: list[object] = []
        for raw in checked:
            if not isinstance(raw, dict):
                continue
            label = self._format_general_option_label(
                {
                    "id": str(raw.get("id", "")),
                    "bundle": str(raw.get("bundle", "")),
                    "name": str(raw.get("name", "")),
                }
            )
            passed = int(str(raw.get("passed_targets", 0)) or 0)
            total = int(str(raw.get("total_targets", 0)) or 0)
            if passed > best_score:
                best_score = passed
                best_total = total
                best_label = label
                best_id = str(raw.get("id", ""))
                best_failed_targets = list(raw.get("failed_targets", []) or [])
            if raw.get("status") == "ok":
                working.append(label)
                if not best_working_id:
                    best_working_id = str(raw.get("id", ""))
            else:
                error_text = str(raw.get("error", "")).strip() or self._t("не удалось запустить", "failed to start")
                failed.append(f"{label} - {error_text}")

        chosen_id = best_working_id or best_id
        auto_applied = False
        if self._general_test_auto_apply and chosen_id:
            chosen_raw = next((raw for raw in checked if isinstance(raw, dict) and str(raw.get("id", "")) == chosen_id), {})
            current_settings = self.context.settings.get()
            self.context.settings.update(
                selected_zapret_general=chosen_id,
                zapret_ipset_mode=str(chosen_raw.get("ipset_mode", current_settings.zapret_ipset_mode) or current_settings.zapret_ipset_mode),
                zapret_game_filter_mode=str(chosen_raw.get("game_mode", current_settings.zapret_game_filter_mode) or current_settings.zapret_game_filter_mode),
                general_autotest_done=True,
            )
            self._set_general_favorite(chosen_id, True)
            self.refresh_all()
            auto_applied = True
        self._general_test_auto_apply = False
        self._restore_general_test_runtime_after_run()

        if self._general_test_embedded:
            self._general_test_embedded = False
            self._onboarding_running = False
            self._stop_onboarding_glow_orbit()
            if self._onboarding_services_panel is not None:
                self._onboarding_services_panel.hide()
            self._show_onboarding_completion_stage(
                success=bool(chosen_id and self._onboarding_result_card is not None),
                chosen_id=chosen_id,
                best_failed_targets=best_failed_targets,
            )
            self.context.settings.update(general_autotest_done=True)
            self._mark_onboarding_seen()
            self._submit_backend_task("set_general_autotest_done", {"done": True}, action_id="__autotest_declined__")
            return

        if not self._general_test_show_results:
            self._mark_dirty("dashboard", "components", "tray")
            return

        dialog = AppDialog(self, self.context, self._t("Результаты проверки", "Test results"))
        title = QLabel(self._t("Проверка завершена.", "Testing is complete."))
        title.setProperty("class", "title")
        dialog.body_layout.addWidget(title)
        summary = QTextEdit()
        summary.setReadOnly(True)
        summary.setMinimumHeight(260)
        summary.setPlainText(
            f"{self._t('Работают:', 'Working:')}\n"
            + ("\n".join(working) if working else self._t("Нет полностью работающих конфигураций.", "No fully working configurations."))
            + "\n\n"
            + (
                f"{self._t('Лучший результат:', 'Best result:')}\n{best_label} ({best_score}/{best_total})\n\n"
                if not working and best_label
                else ""
            )
            + (
                f"{self._t('Применено автоматически:', 'Applied automatically:')}\n"
                f"{self._format_general_option_label(next((item for item in self._sorted_general_options() if item['id'] == chosen_id), {'id': chosen_id, 'bundle': '', 'name': chosen_id}))}\n\n"
                if auto_applied and chosen_id
                else ""
            )
            + f"{self._t('Не работают или дают ошибку:', 'Not working or failed:')}\n"
            + ("\n".join(failed) if failed else self._t("Ошибок не обнаружено.", "No failed configurations."))
        )
        dialog.body_layout.addWidget(summary)
        row = QHBoxLayout()
        row.addStretch(1)
        ok_btn = QPushButton(self._t("Ок", "OK"))
        ok_btn.setProperty("class", "primary")
        self._attach_button_animations(ok_btn)
        ok_btn.clicked.connect(dialog.accept)
        row.addWidget(ok_btn)
        dialog.body_layout.addLayout(row)
        dialog.prepare_and_center()
        dialog.exec()

    def _set_badge(self, key: str, text: str, icon_name: str) -> None:
        badge = self._status_badges.get(key)
        if not badge:
            return
        badge.value_label.setText(text)
        badge.icon_label.setPixmap(self._icon(icon_name).pixmap(18, 18))

    def _set_badge_title(self, key: str, title: str) -> None:
        badge = self._status_badges.get(key)
        if not badge:
            return
        badge.title = title
        badge.title_label.setText(title)

    def _show_info(self, title: str, text: str) -> None:
        dialog = AppDialog(self, self.context, title)
        label = QLabel(text)
        label.setWordWrap(True)
        dialog.body_layout.addWidget(label)
        row = QHBoxLayout()
        row.addStretch(1)
        ok_btn = QPushButton(self._t("Ок", "OK"))
        ok_btn.setProperty("class", "primary")
        self._attach_button_animations(ok_btn)
        ok_btn.clicked.connect(dialog.accept)
        row.addWidget(ok_btn)
        dialog.body_layout.addLayout(row)
        dialog.prepare_and_center()
        dialog.exec()

    def _show_warning(self, title: str, text: str) -> None:
        self._show_info(title, text)

    def _show_error(self, title: str, text: str) -> None:
        self._show_info(title, self._friendly_ui_error_text(text))

    def _friendly_ui_error_text(self, text: str) -> str:
        message = str(text or "").strip()
        lowered = message.lower()
        if "expecting value" in lowered and "line 1 column 1" in lowered:
            return self._t(
                "Получен пустой или повреждённый JSON-ответ. Локальные данные защищены, повторите действие. Если ошибка повторится, откройте логи - там будет указан источник операции.",
                "An empty or corrupted JSON response was received. Local data is protected; try again. If it repeats, open logs - the operation source will be listed there.",
            )
        return message

    def _ask_text_value(self, title: str, text: str, placeholder: str = "") -> str:
        dialog = AppDialog(self, self.context, title)
        label = QLabel(text)
        label.setWordWrap(True)
        dialog.body_layout.addWidget(label)
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        dialog.body_layout.addWidget(field)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel_btn = QPushButton(self._t("Отмена", "Cancel"))
        ok_btn = QPushButton(self._t("Загрузить", "Load"))
        ok_btn.setProperty("class", "primary")
        self._attach_button_animations(cancel_btn)
        self._attach_button_animations(ok_btn)
        cancel_btn.clicked.connect(dialog.reject)
        ok_btn.clicked.connect(dialog.accept)
        row.addWidget(cancel_btn)
        row.addWidget(ok_btn)
        dialog.body_layout.addLayout(row)
        dialog.prepare_and_center()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ""
        return field.text().strip()

    def _ask_yes_no(self, title: str, text: str) -> bool:
        dialog = AppDialog(self, self.context, title)
        label = QLabel(text)
        label.setWordWrap(True)
        dialog.body_layout.addWidget(label)
        row = QHBoxLayout()
        row.addStretch(1)
        no_btn = QPushButton(self._t("Нет", "No"))
        yes_btn = QPushButton(self._t("Да", "Yes"))
        yes_btn.setProperty("class", "primary")
        self._attach_button_animations(no_btn)
        self._attach_button_animations(yes_btn)
        no_btn.clicked.connect(dialog.reject)
        yes_btn.clicked.connect(dialog.accept)
        row.addWidget(no_btn)
        row.addWidget(yes_btn)
        dialog.body_layout.addLayout(row)
        dialog.prepare_and_center()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def refresh_components(self, payload: object | None = None) -> None:
        components: list[ComponentDefinition] = []
        states: dict[str, ComponentState] = {}
        general_options_from_payload: list[dict[str, str]] | None = None
        explicit_payload = False
        if isinstance(payload, dict):
            explicit_payload = "components" in payload or "states" in payload
            raw_general_options = payload.get("general_options")
            if isinstance(raw_general_options, list):
                general_options_from_payload = [
                    item for item in raw_general_options if isinstance(item, dict) and item.get("id")
                ]
                if general_options_from_payload:
                    self._general_options_cache = general_options_from_payload
            raw_components = payload.get("components", [])
            raw_states = payload.get("states", {})
            if isinstance(raw_components, list):
                for item in raw_components:
                    if isinstance(item, ComponentDefinition):
                        components.append(item)
                    elif isinstance(item, dict):
                        try:
                            components.append(ComponentDefinition(**item))
                        except Exception:
                            continue
            if isinstance(raw_states, dict):
                for key, item in raw_states.items():
                    if isinstance(item, ComponentState):
                        states[str(key)] = item
                    elif isinstance(item, dict):
                        try:
                            states[str(key)] = ComponentState(**item)
                        except Exception:
                            continue
            elif isinstance(raw_states, list):
                for item in raw_states:
                    if isinstance(item, ComponentState):
                        states[item.component_id] = item
                    elif isinstance(item, dict) and item.get("component_id"):
                        try:
                            parsed = ComponentState(**item)
                            states[parsed.component_id] = parsed
                        except Exception:
                            continue
        if not components and not explicit_payload:
            components = list(self._component_defs().values())
        if not states and not explicit_payload:
            states = self._component_states()
        order = {"zapret": 0, "peshk0v-vpn": 1, "tg-ws-proxy": 2}
        components = sorted(components, key=lambda item: order.get(item.id, 99))
        self.components_list.clear()
        self._components_card_by_id = {}
        if not self._startup_snapshot_ready and not components:
            if self._components_cards_layout is None:
                return
            while self._components_cards_layout.count():
                layout_item = self._components_cards_layout.takeAt(0)
                widget = layout_item.widget()
                if widget is not None:
                    widget.deleteLater()
            loading, loading_layout = self._card()
            loading_title = QLabel(self._t("Компоненты загружаются", "Components are loading"))
            loading_title.setProperty("class", "title")
            loading_text = QLabel(
                self._t(
                    "Подождите немного: сначала приложение получает реальный snapshot состояния компонентов.",
                    "Please wait a moment while the app gets the real component snapshot.",
                )
            )
            loading_text.setProperty("class", "muted")
            loading_text.setWordWrap(True)
            loading_layout.addWidget(loading_title)
            loading_layout.addWidget(loading_text)
            self._components_cards_layout.addWidget(loading, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            return
        for component in components:
            state = states.get(component.id)
            status_text = state.status if state else "stopped"
            subtitle = f"{self._t('Версия', 'Version')}: {component.version} | {self._t('Включен', 'Enabled')}: {self._t('да', 'yes') if component.enabled else self._t('нет', 'no')} | {self._t('Автозапуск', 'Autostart')}: {self._t('да', 'yes') if component.autostart else self._t('нет', 'no')} | {self._t('Статус', 'Status')}: {status_text}"
            source = f"{self._t('Источник', 'Source')}: {component.source}"
            display_name = {"zapret": "Zapret", "tg-ws-proxy": "Tg-Ws-Proxy"}.get(component.id, component.name)
            item = QListWidgetItem(f"{display_name}\n{subtitle}\n{source}")
            item.setData(Qt.ItemDataRole.UserRole, component.id)
            item.setSizeHint(QSize(200, 70))
            self.components_list.addItem(item)
        if self._components_cards_layout is None:
            return

        while self._components_cards_layout.count():
            layout_item = self._components_cards_layout.takeAt(0)
            widget = layout_item.widget()
            if widget is not None:
                widget.deleteLater()

        if not components:
            empty, empty_layout = self._card()
            empty_title = QLabel(self._t("Компоненты пока недоступны", "Components are currently unavailable"))
            empty_title.setProperty("class", "title")
            empty_text = QLabel(
                self._t(
                    "Данные ещё подгружаются. Попробуйте открыть вкладку ещё раз через секунду.",
                    "Data is still loading. Try opening this tab again in a second.",
                )
            )
            empty_text.setProperty("class", "muted")
            empty_text.setWordWrap(True)
            empty_layout.addWidget(empty_title)
            empty_layout.addWidget(empty_text)
            self._components_cards_layout.addWidget(empty, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            return

        descriptions = {
            "zapret": self._t(
                "Классический способ обхода блокировок через DPI.",
                "A classic DPI-based bypass method for blocked services.",
            ),
            "tg-ws-proxy": self._t(
                "Локальный Telegram Proxy. Позволяет подключаться к Telegram в обход блокировок, маскируясь под обычный https-трафик.",
                "Local Telegram Proxy. Lets Telegram connect through restrictions by blending in with regular HTTPS traffic.",
            ),
            "peshk0v-vpn": self._t(
                "Авторская VPN-подписка без ограничений по трафику и количеству устройств. Доступна на смартфонах, ПК, ноутбуках и других устройствах. Первые 10 дней подписки бесплатно.",
                "Author VPN subscription with no traffic or device limits. Available on phones, PCs, laptops, and other devices. The first 10 days are free.",
            ),
        }
        icons = {"zapret": "component_zapret.svg", "tg-ws-proxy": "component_tg.svg", "peshk0v-vpn": self._vpn_icon_name()}
        vpn_state = {}
        if isinstance(payload, dict) and isinstance(payload.get("peshk0v_vpn"), dict):
            vpn_state = dict(payload.get("peshk0v_vpn") or {})
        elif getattr(self.context, "vpn", None) is not None:
            try:
                vpn_state = self.context.vpn.state()
            except Exception:
                vpn_state = {}
        component_cards: list[QFrame] = []

        for index, component in enumerate(components):
            state = states.get(component.id)
            status_text, _status_icon = self._component_badge_state(component, state, any_running=False)
            display_name = {"zapret": "Zapret", "tg-ws-proxy": "Tg-Ws-Proxy", "peshk0v-vpn": "peshk0v vpn"}.get(component.id, component.name)
            card, card_layout = self._card()
            card.setMinimumWidth(360)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            self._components_card_by_id[component.id] = card
            icon = QLabel()
            if component.id == "tg-ws-proxy":
                icon_size = 38
            elif component.id == "peshk0v-vpn":
                icon_size = 29
            else:
                icon_size = 36
            icon_slot = QSize(icon_size, icon_size + (4 if component.id == "peshk0v-vpn" else 0))
            icon.setFixedSize(icon_slot)
            raw_icon_pixmap = self._icon(icons.get(component.id, "components.svg")).pixmap(icon_size, icon_size)
            icon.setPixmap(
                self._compose_icon_slot_pixmap(
                    raw_icon_pixmap,
                    icon_slot,
                    1.0,
                    0.0,
                    3.0 if component.id == "peshk0v-vpn" else 0.0,
                )
            )
            icon_row = QHBoxLayout()
            icon_row.setContentsMargins(0, 6, 0, 0)
            icon_row.setSpacing(8)
            icon_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            icon_row.addStretch(1)
            if component.id in {"tg-ws-proxy"} or (component.id == "peshk0v-vpn" and str(vpn_state.get("subscription_state", "") or "") == "valid"):
                settings_icon_btn = QToolButton()
                settings_icon_btn.setProperty("class", "action")
                settings_icon_btn.setIcon(self._icon("settings.svg"))
                settings_icon_btn.setIconSize(QSize(16, 16))
                settings_icon_btn.setFixedSize(30, 30)
                settings_icon_btn.setToolTip(
                    self._t("Настройки TG WS Proxy", "TG WS Proxy settings")
                    if component.id == "tg-ws-proxy"
                    else self._t("Настройки peshk0v vpn", "peshk0v vpn settings")
                )
                settings_icon_btn.setEnabled(True)
                settings_icon_btn.clicked.connect(lambda _=False, cid=component.id: self._open_component_settings(cid))
                self._attach_button_animations(settings_icon_btn)
                icon_row.addWidget(settings_icon_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            if component.id in {"zapret", "tg-ws-proxy", "peshk0v-vpn"}:
                source_icon_btn = QToolButton()
                source_icon_btn.setProperty("class", "action")
                source_icon_btn.setIcon(self._icon("external.svg"))
                source_icon_btn.setIconSize(QSize(16, 16))
                source_icon_btn.setFixedSize(30, 30)
                source_icon_btn.setToolTip(self._t("Источник", "Source"))
                source_icon_btn.clicked.connect(lambda _=False, url=component.source: self._open_update_link(url))
                self._attach_button_animations(source_icon_btn)
                icon_row.addWidget(source_icon_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
                if component.id != "peshk0v-vpn":
                    update_icon_btn = QToolButton()
                    update_icon_btn.setProperty("class", "action")
                    update_icon_btn.setIcon(self._icon("refresh.svg"))
                    update_icon_btn.setIconSize(QSize(16, 16))
                    update_icon_btn.setFixedSize(30, 30)
                    update_icon_btn.setToolTip(
                        self._t("Обновить Zapret", "Update Zapret")
                        if component.id == "zapret"
                        else self._t("Обновить TG WS Proxy", "Update TG WS Proxy")
                    )
                    update_icon_btn.clicked.connect(
                        self._update_zapret_runtime if component.id == "zapret" else self._update_tg_ws_proxy_runtime
                    )
                    self._attach_button_animations(update_icon_btn)
                    icon_row.addWidget(update_icon_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            card_layout.addLayout(icon_row)

            title = QLabel(display_name)
            title.setProperty("class", "title")
            title.setWordWrap(True)
            card_layout.addWidget(title)

            description_text = descriptions.get(component.id, component.description)
            if component.id == "peshk0v-vpn" and str(vpn_state.get("subscription_state", "") or "") == "valid":
                description_text = self._t(
                    "Авторский VPN без ограничений по трафику и количеству устройств.",
                    "Author VPN with no traffic or device limits.",
                )
            desc = QLabel(description_text)
            desc.setProperty("class", "muted")
            desc.setWordWrap(True)
            card_layout.addWidget(desc)

            details = QLabel(
                f"{self._t('Автор', 'Author')}: {'peshk0v' if component.id == 'peshk0v-vpn' else 'Flowseal'}\n"
                f"{self._t('Статус', 'Status')}: {status_text}\n"
                f"{self._t('Версия', 'Version')}: {component.version}"
            )
            details.setProperty("class", "muted")
            details.setWordWrap(True)
            card_layout.addWidget(details)

            enabled_text = self._t("включен", "enabled") if component.enabled else self._t("выключен", "disabled")
            participation = QLabel(f"{self._t('Участие в ON/OFF', 'ON/OFF participation')}: {enabled_text}")
            participation.setWordWrap(True)
            card_layout.addWidget(participation)
            if component.id == "zapret":
                if not self._sorted_general_options() and general_options_from_payload:
                    self._general_options_cache = general_options_from_payload
                config_label = QLabel(self._t("Конфигурация Zapret", "Zapret Configuration"))
                config_label.setProperty("class", "muted")
                card_layout.addWidget(config_label)
                config_combo = ClickSelectComboBox()
                config_status = QLabel("")
                config_status.setProperty("class", "muted")
                config_status.hide()
                options = self._sorted_general_options()
                selected = self.context.settings.get().selected_zapret_general
                for option in options:
                    config_combo.addItem(self._format_general_option_label(option), option["id"])
                if config_combo.count() == 0:
                    config_combo.addItem(self._t("Конфигурации загружаются", "Configurations are loading"), "")
                    config_combo.setEnabled(False)
                    try:
                        self._submit_backend_task("load_components_payload")
                    except Exception:
                        pass
                if config_combo.count() > 0:
                    picked_index = 0
                    for i in range(config_combo.count()):
                        if config_combo.itemData(i) == selected:
                            picked_index = i
                            break
                    config_combo.setCurrentIndex(picked_index)
                config_row = QHBoxLayout()
                config_row.setContentsMargins(0, 0, 0, 0)
                config_row.setSpacing(8)
                config_combo.currentIndexChanged.connect(
                    lambda _=0, combo=config_combo, status_label=config_status: self._on_general_selected_from_components(
                        str(combo.currentData() or ""),
                        combo,
                        status_label,
                    )
                )
                favorite_btn = QToolButton()
                favorite_btn.setProperty("class", "action")
                current_general = str(config_combo.currentData() or "")
                self._sync_general_favorite_button(current_general, favorite_btn)
                favorite_btn.clicked.connect(
                    lambda _=False, combo=config_combo, btn=favorite_btn: self._toggle_general_favorite_from_button(
                        str(combo.currentData() or ""),
                        btn,
                    )
                )
                config_combo.currentIndexChanged.connect(
                    lambda _=0, combo=config_combo, btn=favorite_btn: self._sync_general_favorite_button(
                        str(combo.currentData() or ""),
                        btn,
                    )
                )
                config_row.addWidget(config_combo, 1)
                config_row.addWidget(favorite_btn, 0)
                card_layout.addLayout(config_row)
                card_layout.addWidget(config_status)

            if component.id == "tg-ws-proxy":
                telegram_link = QLabel()
                telegram_link.setProperty("class", "muted")
                link_color = "#2563eb" if is_light_theme(self.context.settings.get().theme) else "#60a5fa"
                telegram_link.setText(
                    f'<a style="color:{link_color};" href="tg-download://telegram-desktop">{self._t("Скачать Telegram Desktop", "Download Telegram Desktop")}</a>'
                )
                telegram_link.setTextFormat(Qt.TextFormat.RichText)
                telegram_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
                telegram_link.setOpenExternalLinks(False)
                telegram_link.linkActivated.connect(self._open_external_url)
                card_layout.addWidget(telegram_link)
                connect_btn = QPushButton(self._t("Подключить к Telegram", "Connect to Telegram"))
                connect_btn.clicked.connect(self._prompt_tg_proxy_connect)
                self._attach_button_animations(connect_btn)
                card_layout.addWidget(connect_btn)
            if component.id == "peshk0v-vpn":
                self._add_peshk0v_vpn_controls(card_layout, vpn_state, state)
            if state is not None and getattr(state, "last_error", ""):
                error_label = QLabel(str(getattr(state, "last_error", "")))
                error_label.setProperty("class", "muted")
                error_label.setWordWrap(True)
                card_layout.addWidget(error_label)

            vpn_configured = str(vpn_state.get("subscription_state", "") or "") == "valid"
            if component.id != "peshk0v-vpn" or vpn_configured:
                toggle_btn = QPushButton(
                    self._t("Выключить компонент", "Disable component")
                    if component.enabled
                    else self._t("Включить компонент", "Enable component")
                )
                toggle_btn.setProperty("class", "danger" if component.enabled else "primary")
                toggle_btn.clicked.connect(lambda _=False, cid=component.id, btn=toggle_btn: self._toggle_component_card(cid, btn))
                self._attach_button_animations(toggle_btn)
                card_layout.addWidget(toggle_btn)
            component_cards.append(card)
            self._components_cards_layout.addWidget(
                card,
                index // 2,
                index % 2,
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
            )
        self._sync_component_card_layout(component_cards)
        if self._components_scroll_target_component_id:
            QTimer.singleShot(0, self._ensure_components_scroll_target_visible)
            QTimer.singleShot(120, self._ensure_components_scroll_target_visible)

    def _add_peshk0v_vpn_controls(
        self,
        layout: QVBoxLayout,
        vpn_state: dict[str, object],
        component_state: ComponentState | None = None,
    ) -> None:
        subscription_state = str(vpn_state.get("subscription_state", "empty") or "empty")
        running = bool(component_state is not None and str(getattr(component_state, "status", "") or "") == "running")
        configured = subscription_state == "valid" or running
        invalid = subscription_state == "invalid"

        if not configured:
            access_btn = QPushButton(self._t("10 дней бесплатно", "10 days free"))
            access_btn.setProperty("class", "primary")
            access_btn.clicked.connect(lambda: self._open_external_url("https://vpn.peshk0v.ru"))
            self._attach_button_animations(access_btn)
            layout.addWidget(access_btn)

        if not configured:
            sub_row = QHBoxLayout()
            sub_row.setContentsMargins(0, 0, 0, 0)
            sub_row.setSpacing(8)
            sub_input = QLineEdit()
            sub_input.setPlaceholderText("https://vpn.peshk0v.ru/sub/...")
            sub_input.setText(str(vpn_state.get("subscription_url", "") or ""))
            import_btn = QPushButton(self._t("Импорт", "Import"))
            import_btn.clicked.connect(lambda _=False, field=sub_input: self._import_peshk0v_vpn_subscription(field.text()))
            self._attach_button_animations(import_btn)
            sub_row.addWidget(sub_input, 1)
            sub_row.addWidget(import_btn, 0)
            layout.addLayout(sub_row)

        if invalid:
            invalid_label = QLabel(str(vpn_state.get("last_error", "") or self._t("Ссылка подписки невалидна.", "Subscription link is invalid.")))
            invalid_label.setProperty("class", "muted")
            invalid_label.setWordWrap(True)
            layout.addWidget(invalid_label)

        if not configured:
            return

        servers = [item for item in list(vpn_state.get("servers", []) or []) if isinstance(item, dict)]
        server_combo = ClickSelectComboBox()
        selected_server = str(vpn_state.get("selected_server_id", "") or "")
        if servers:
            for server in servers:
                server_combo.addItem(self._display_peshk0v_vpn_server_name(server), str(server.get("id", "") or ""))
            for index in range(server_combo.count()):
                if str(server_combo.itemData(index) or "") == selected_server:
                    server_combo.setCurrentIndex(index)
                    break
            server_combo.currentIndexChanged.connect(
                lambda _=0, combo=server_combo: self._update_peshk0v_vpn_settings({"selected_server_id": str(combo.currentData() or "")})
            )
        else:
            server_combo.addItem(self._t("Добавьте подписку, чтобы увидеть локации", "Add a subscription to see locations"), "")
            server_combo.setEnabled(False)
        layout.addWidget(server_combo)

    def _display_peshk0v_vpn_server_name(self, server: dict[str, object]) -> str:
        raw_name = str(server.get("name", "") or server.get("host", "") or "peshk0v vpn")
        cleaned = raw_name
        for token in ("🇫🇮", "🇳🇱", "🇩🇪", "🇷🇺", "🇺🇸", "🇨🇦", "🇸🇪", "🇳🇴", "🇩🇰", "🇵🇱", "🇫🇷", "🇨🇿", "🇸🇰"):
            cleaned = cleaned.replace(token, "")
        cleaned = cleaned.replace(" - ", " ").replace(" #", " ").replace("#", "")
        cleaned = cleaned.replace("обход ", "обход ")
        cleaned = " ".join(cleaned.split()).strip(" -–—")
        return cleaned or str(server.get("host", "") or "peshk0v vpn")

    def _format_bytes(self, value: int) -> str:
        size = float(max(0, int(value)))
        for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
            if size < 1024.0 or unit == "ТБ":
                return f"{size:.1f} {unit}" if unit != "Б" else f"{int(size)} {unit}"
            size /= 1024.0
        return f"{int(value)} Б"

    def _select_combo_value(self, combo: QComboBox, value: str) -> None:
        for index in range(combo.count()):
            if str(combo.itemData(index) or "") == value:
                combo.setCurrentIndex(index)
                return

    def _import_peshk0v_vpn_subscription(self, url: str) -> None:
        self._submit_backend_task("import_peshk0v_vpn_subscription", {"url": url})

    def _update_peshk0v_vpn_settings(self, payload: dict[str, object]) -> None:
        self._submit_backend_task("update_peshk0v_vpn_settings", payload)

    def _prompt_tg_proxy_connect(self) -> None:
        try:
            self.context.processes.prompt_telegram_proxy_link()
            self._notify_telegram_proxy_status_from_payload({"telegram_proxy": self.context.processes.consume_telegram_proxy_launch_info() or {}})
        except Exception as error:
            self._show_error(
                self._t("TG Proxy", "TG Proxy"),
                f"{self._t('Не удалось открыть запрос на подключение в Telegram.', 'Failed to open Telegram connection prompt.')}\n{error}",
            )

    def _update_zapret_runtime(self) -> None:
        try:
            self._show_component_update_dialog("Zapret")
            self._submit_backend_task("update_zapret_runtime")
        except Exception as error:
            self._close_component_update_dialog()
            self._show_error("Zapret", str(error))

    def _update_tg_ws_proxy_runtime(self) -> None:
        try:
            self._show_component_update_dialog("TG WS Proxy")
            self._submit_backend_task("update_tg_ws_proxy_runtime")
        except Exception as error:
            self._close_component_update_dialog()
            self._show_error("TG WS Proxy", str(error))

    def _telegram_download_url(self) -> str:
        machine = platform.machine().lower()
        want_arm = "arm" in machine or "aarch64" in machine
        fallback = (
            "https://github.com/telegramdesktop/tdesktop/releases/latest/download/tsetup-arm64.exe"
            if want_arm
            else "https://github.com/telegramdesktop/tdesktop/releases/latest/download/tsetup-x64.exe"
        )
        try:
            payload = self.context.updates.github.github_json(
                "https://api.github.com/repos/telegramdesktop/tdesktop/releases/latest",
                timeout=10,
                purpose="telegram-release-metadata",
            )
            if not isinstance(payload, dict):
                return fallback
            assets = payload.get("assets") or []
            preferred_markers = ("arm64", "arm") if want_arm else ("x64",)
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                name = str(asset.get("name") or "").lower()
                url = str(asset.get("browser_download_url") or "").strip()
                if not url or not name.endswith(".exe"):
                    continue
                if "tsetup" not in name:
                    continue
                if any(marker in name for marker in preferred_markers):
                    return url
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                name = str(asset.get("name") or "").lower()
                url = str(asset.get("browser_download_url") or "").strip()
                if url and name.startswith("tsetup.") and name.endswith(".exe"):
                    return url
        except Exception:
            return fallback
        return fallback

    def _open_external_url(self, url: str) -> None:
        if not url:
            return
        if url.startswith("tg-download://"):
            url = self._telegram_download_url()
        try:
            if sys.platform.startswith("win"):
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                webbrowser.open(url)
        except Exception:
            webbrowser.open(url)

    def _sync_component_card_layout(self, cards: list[QFrame] | None = None) -> None:
        if self._components_cards_layout is None or self._components_scroll is None:
            return
        resolved_cards = cards or [self._components_cards_layout.itemAt(i).widget() for i in range(self._components_cards_layout.count())]
        widgets = [widget for widget in resolved_cards if isinstance(widget, QFrame)]
        if not widgets:
            return
        viewport = self._components_scroll.viewport()
        if viewport.height() <= 0:
            QTimer.singleShot(0, self._sync_component_card_layout)
            return
        row_groups: dict[int, list[QFrame]] = {}
        for index, widget in enumerate(widgets):
            widget.setMinimumHeight(0)
            widget.setMaximumHeight(16777215)
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            row_groups.setdefault(index // 2, []).append(widget)
        for row_widgets in row_groups.values():
            target_height = 0
            for widget in row_widgets:
                try:
                    if widget.layout() is not None:
                        widget.layout().activate()
                except Exception:
                    pass
                widget.adjustSize()
                target_height = max(
                    target_height,
                    widget.minimumSizeHint().height(),
                    widget.sizeHint().height(),
                )
            for widget in row_widgets:
                widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                widget.setFixedHeight(target_height)
        self._components_cards_root.updateGeometry()

    def _sync_mod_card_layout(self) -> None:
        if self.mods_scroll is None or self.mods_canvas is None or self.mods_cards_layout is None:
            return
        try:
            self.mods_cards_layout.activate()
        except Exception:
            pass
        self.mods_canvas.updateGeometry()
        self.mods_scroll.viewport().update()

    def _refresh_mods_legacy(self) -> None:
        index = self.context.mods.fetch_index()
        installed = {item.id: item for item in self.context.mods.list_installed()}
        combined: list[tuple[str, str, str, str, str, str]] = []
        seen: set[str] = set()
        for item in index:
            seen.add(item.id)
            state = "not installed"
            if item.id in installed:
                state = "enabled" if installed[item.id].enabled else "installed"
            combined.append(
                (
                    item.id,
                    item.name,
                    item.description,
                    f"{self._t('Автор', 'Author')}: {item.author} | {self._t('Версия', 'Version')}: {item.version} | {self._t('Статус', 'Status')}: {state}",
                    f"{self._t('Категория', 'Category')}: {item.category}",
                    state,
                )
            )

        for mod_id, item in installed.items():
            if mod_id in seen:
                continue
            state = "enabled" if item.enabled else "installed"
            source_type = "zapret bundle" if item.source_type == "zapret_bundle" else item.source_type
            combined.append(
                (
                    mod_id,
                    mod_id,
                    self._t("Локальная модификация без пользовательского описания.", "Local modification without user description."),
                    f"{self._t('Локальный импорт', 'Local import')} | {self._t('Версия', 'Version')}: {item.version} | {self._t('Статус', 'Status')}: {state}",
                    f"{self._t('Тип', 'Type')}: {source_type}",
                    state,
                )
            )

        selected = self._selected_mod_id()
        self.mods_list.clear()
        for mod_id, name, description, subtitle, tags, _state in combined:
            row_item = QListWidgetItem(f"{name}\n{description}\n{subtitle}\n{tags}")
            row_item.setData(Qt.ItemDataRole.UserRole, mod_id)
            row_item.setSizeHint(QSize(200, 88))
            self.mods_list.addItem(row_item)
        if selected:
            for i in range(self.mods_list.count()):
                it = self.mods_list.item(i)
                if it.data(Qt.ItemDataRole.UserRole) == selected:
                    self.mods_list.setCurrentItem(it)
                    break

    def _toggle_mod_by_id(self, mod_id: str) -> None:
        try:
            installed = dict(self._mods_installed_cache)
            target = installed.get(mod_id)
            if target is not None:
                target.enabled = not bool(target.enabled)
                self.refresh_mods({"index": list(self._mods_index_cache), "installed": installed})
        except Exception:
            pass
        self._submit_backend_task("toggle_mod", {"mod_id": mod_id}, action_id=f"mod:{mod_id}")

    def _mod_circle_action_style(self, role: str, *, active: bool) -> str:
        theme = self.context.settings.get().theme
        if role == "power" and active:
            border = "#2f8f5d"
            fg = "#a8efc1" if not is_light_theme(theme) else "#1f6b45"
            fill = "rgba(44, 163, 93, 0.14)"
            hover = "rgba(44, 163, 93, 0.22)"
        elif role == "delete":
            border = "#fb5e5e"
            fg = "#ffd9dd" if not is_light_theme(theme) else "#bc4357"
            fill = "rgba(239, 68, 68, 0.08)"
            hover = "rgba(239, 68, 68, 0.16)"
        else:
            if is_light_theme(theme):
                border = "#bfd2f0"
                fg = "#37507e"
                fill = "rgba(191, 210, 240, 0.18)"
                hover = "rgba(148, 170, 205, 0.28)"
            else:
                border = "#35517f"
                fg = "#dbe5fb"
                fill = "rgba(53, 81, 127, 0.16)"
                hover = "rgba(83, 108, 148, 0.26)"
        return (
            "QToolButton {"
            f"border: 1px solid {border};"
            f"color: {fg};"
            f"background: {fill};"
            "border-radius: 18px;"
            "padding: 0px;"
            "}"
            "QToolButton:disabled { opacity: 0.45; }"
        )

    def _choose_directory_dialog(self, title: str, start_dir: str) -> str:
        dialog = QFileDialog(self, title, start_dir)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ""
        files = dialog.selectedFiles()
        return files[0] if files else ""

    def _choose_save_file_dialog(self, title: str, start_path: str, file_filter: str) -> str:
        _bring_widget_to_front(self)
        start = Path(start_path)
        self.context.logging.log("info", "Mod export save dialog opened", start_path=str(start_path))
        selected_path = ""
        native_failed = False
        try:
            dialog = QFileDialog(self, title, str(start.parent))
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            dialog.setFileMode(QFileDialog.FileMode.AnyFile)
            dialog.setNameFilter(file_filter)
            dialog.setDefaultSuffix("zip")
            dialog.selectFile(start.name)
            dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
            result = dialog.exec()
            if result == QDialog.DialogCode.Accepted:
                files = dialog.selectedFiles()
                selected_path = files[0] if files else ""
            else:
                self.context.logging.log("info", "Mod export native dialog cancelled")
                return ""
        except Exception as error:
            native_failed = True
            self.context.logging.log("warning", "Native save dialog failed", error=str(error))
        if selected_path:
            return selected_path
        if not native_failed:
            return ""
        dialog = QFileDialog(self, title, str(start.parent))
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter(file_filter)
        dialog.setDefaultSuffix("zip")
        dialog.selectFile(start.name)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.raise_()
        dialog.activateWindow()
        self.context.logging.log("info", "Mod export fallback dialog opened")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.context.logging.log("info", "Mod export fallback dialog cancelled")
            return ""
        files = dialog.selectedFiles()
        return files[0] if files else ""

    def _export_mod_by_id(self, mod_id: str) -> None:
        self.context.logging.log("info", "Mod export requested", mod_id=mod_id)
        try:
            mod_entry = next(item for item in self.context.mods.list_installed() if item.id == mod_id)
        except Exception:
            mod_entry = None
        suggested_name = f"{mod_id}-{getattr(mod_entry, 'version', '') or __version__}.zip"
        desktop_dir = Path.home() / "Desktop"
        default_dir = desktop_dir if desktop_dir.exists() else self.context.paths.install_root
        target_path = self._choose_save_file_dialog(
            self._t("Сохранить ZIP модификации", "Save modification ZIP"),
            str(default_dir / suggested_name),
            self._t("ZIP архив (*.zip)", "ZIP archive (*.zip)"),
        )
        if not target_path:
            self.context.logging.log("info", "Mod export cancelled", mod_id=mod_id)
            return
        try:
            if not str(target_path).lower().endswith(".zip"):
                target_path = f"{target_path}.zip"
            self.context.logging.log("info", "Mod export target selected", mod_id=mod_id, target_path=str(target_path))
            archive_path = self.context.mods.export_mod(mod_id, target_path)
            self.context.logging.log("info", "Mod export finished", mod_id=mod_id, archive_path=str(archive_path))
            self._show_info(
                self._t("Модификации", "Mods"),
                self._t(
                    f"Модификация сохранена:\n{archive_path}",
                    f"Modification exported:\n{archive_path}",
                ),
            )
        except Exception as error:
            self.context.logging.log("error", "Mod export failed", mod_id=mod_id, error=str(error))
            self._show_error(self._t("Модификации", "Mods"), str(error))

    def _request_mod_export(self, mod_id: str) -> None:
        self.context.logging.log("info", "Mod export click dispatched", mod_id=mod_id)
        self._export_mod_by_id(mod_id)

    def _remove_mod_with_confirmation(self, mod_id: str) -> None:
        if not self._ask_yes_no(
            self._t("Удалить модификацию", "Delete modification"),
            self._t(
                "Точно удалить эту модификацию? Это действие нельзя отменить.",
                "Delete this modification? This action cannot be undone.",
            ),
        ):
            return
        try:
            self._submit_backend_task("remove_mod", {"mod_id": mod_id}, action_id=f"mod-remove:{mod_id}")
        except Exception as error:
            self._show_error(self._t("Модификации", "Mods"), str(error))

    def refresh_mods(self, payload: object | None = None) -> None:
        def _field(obj: object, name: str, default: object = "") -> object:
            if isinstance(obj, dict):
                return obj.get(name, default)
            return getattr(obj, name, default)

        index: list[object] = []
        installed: dict[str, object] = {}
        if isinstance(payload, dict):
            raw_index = payload.get("index", [])
            raw_installed = payload.get("installed", {})
            if isinstance(raw_index, list):
                index = list(raw_index)
            if isinstance(raw_installed, dict):
                installed = {str(key): value for key, value in raw_installed.items()}
            elif isinstance(raw_installed, list):
                for item in raw_installed:
                    item_id = str(_field(item, "id", "") or "")
                    if item_id:
                        installed[item_id] = item
        if not index:
            index = list(self._mods_index_cache)
        if not installed:
            installed = dict(self._mods_installed_cache)
        combined: list[dict[str, str | bool | int]] = []
        index_map = {str(_field(item, "id", "") or ""): item for item in index if str(_field(item, "id", "") or "")}
        installed_items = list(installed.values())
        seen: set[str] = set()
        for order, installed_item in enumerate(installed_items):
            mod_id = str(_field(installed_item, "id", "") or "")
            if not mod_id:
                continue
            if mod_id == "unified-by-peshk0v":
                continue
            seen.add(mod_id)
            indexed = index_map.get(mod_id)
            enabled = bool(_field(installed_item, "enabled", False))
            state = "enabled" if enabled else "installed"
            combined.append(
                {
                    "id": mod_id,
                    "name": str(_field(indexed or installed_item, "name", mod_id) or mod_id),
                    "description": str(_field(indexed or installed_item, "description", "") or self._t("Локальная модификация без описания.", "Local mod without description.")),
                    "subtitle": f"{self._t('Автор', 'Author')}: {str(_field(indexed or installed_item, 'author', self._t('неизвестен', 'unknown')) or self._t('неизвестен', 'unknown'))} | {self._t('Версия', 'Version')}: {str(_field(installed_item, 'version', _field(indexed or installed_item, 'version', '')))}",
                    "state": state,
                    "enabled": enabled,
                    "changelog": str(_field(indexed or installed_item, "changelog", "") or ""),
                    "emoji": self._resolve_mod_emoji(mod_id, str(_field(installed_item, "emoji", "") or "")),
                    "installed": True,
                    "order": order,
                }
            )

        for item in index:
            item_id = str(_field(item, "id", "") or "")
            if not item_id or item_id in seen:
                continue
            if item_id == "unified-by-peshk0v":
                continue
            combined.append(
                {
                    "id": item_id,
                    "name": str(_field(item, "name", item_id)),
                    "description": str(_field(item, "description", "") or self._t("Описание не указано.", "No description.")),
                    "subtitle": f"{self._t('Автор', 'Author')}: {str(_field(item, 'author', self._t('неизвестен', 'unknown')) or self._t('неизвестен', 'unknown'))} | {self._t('Версия', 'Version')}: {str(_field(item, 'version', ''))}",
                    "state": "not installed",
                    "enabled": False,
                    "changelog": str(_field(item, "changelog", "") or ""),
                    "emoji": self._resolve_mod_emoji(item_id, ""),
                    "installed": False,
                    "order": 9999,
                }
            )

        if not hasattr(self, "mods_cards_layout"):
            return
        scroll_bar = self.mods_scroll.verticalScrollBar() if getattr(self, "mods_scroll", None) is not None else None
        previous_scroll_value = int(scroll_bar.value()) if scroll_bar is not None else 0

        def restore_scroll_position() -> None:
            if scroll_bar is None:
                return
            scroll_bar.setValue(min(previous_scroll_value, scroll_bar.maximum()))

        enabled_count = sum(1 for mod in combined if bool(mod["enabled"]))
        if hasattr(self, "mods_summary_chip"):
            self.mods_summary_chip.setText(
                self._t(
                    f"Всего пакетов: {len(combined)}",
                    f"Total packs: {len(combined)}",
                )
            )
        if hasattr(self, "mods_enabled_chip"):
            self.mods_enabled_chip.setText(
                self._t(
                    f"Активно сейчас: {enabled_count}",
                    f"Active now: {enabled_count}",
                )
            )

        while self.mods_cards_layout.count():
            child = self.mods_cards_layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

        if not combined:
            empty, empty_layout = self._card()
            empty.setProperty("class", "modCard")
            empty_layout.setContentsMargins(14, 14, 14, 14)
            title = QLabel(self._t("Пока пусто", "Nothing here yet"))
            title.setProperty("class", "title")
            text = QLabel(
                self._t(
                    "Добавьте архив, конфиг или папку с файлами, чтобы здесь появились модификации.",
                    "Add an archive, config, or folder with files and your modifications will appear here.",
                )
            )
            text.setProperty("class", "muted")
            text.setWordWrap(True)
            empty_layout.addWidget(title)
            empty_layout.addWidget(text)
            self.mods_cards_layout.addWidget(empty)
            self.mods_cards_layout.addStretch(1)
            QTimer.singleShot(0, restore_scroll_position)
            return

        for mod in combined:
            mod_id = str(mod["id"])
            enabled = bool(mod["enabled"])
            state = str(mod["state"])
            if mod_id == "unified-by-peshk0v":
                mod["description"] = self._t(
                    "Позволяет обойти блокировки самых популярных сервисов, включая игровые сервисы, социальные сети и другие платформы.",
                    "Helps bypass restrictions for the most popular services, including gaming platforms, social networks, and other services.",
                )

            card = ModCardFrame(mod_id, bool(mod.get("installed")) and mod_id != "unified-by-peshk0v")
            card.setProperty("class", "modCard")
            card.clicked.connect(self._open_mod_editor)
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(16, 16, 16, 16)
            card_layout.setSpacing(16)

            left_col = QVBoxLayout()
            left_col.setContentsMargins(0, 0, 0, 0)
            left_col.setSpacing(10)
            left_col.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

            icon_wrap = QFrame()
            icon_wrap.setProperty("class", "")
            icon_wrap.setFixedSize(60, 60)
            palette_bg, palette_border, palette_fg = self._mod_badge_palette(str(mod["emoji"]))
            palette_bg, palette_border, palette_fg = self._theme_adjusted_badge_palette(palette_bg, palette_border, palette_fg)
            icon_wrap.setStyleSheet(
                f"QFrame {{ background: {palette_bg}; border: 1px solid {palette_border}; border-radius: 16px; }}"
            )
            icon_row = QVBoxLayout(icon_wrap)
            icon_row.setContentsMargins(2, 2, 2, 2)
            icon_row.setSpacing(0)
            emoji_btn = EmojiBadgeButton(str(mod["emoji"]))
            emoji_btn.setToolTip(self._t("Выбрать эмодзи", "Choose emoji"))
            emoji_btn.setFixedSize(48, 48)
            emoji_btn.setStyleSheet("border: none; background: transparent;")
            emoji_btn.setEmojiColor(palette_fg)
            badge_dx, badge_dy = self._mod_badge_offset(str(mod["emoji"]))
            emoji_btn.setEmojiOffset(badge_dx, badge_dy)
            if mod_id == "unified-by-peshk0v":
                emoji_btn.setEnabled(False)
            else:
                emoji_btn.clicked.connect(lambda _=False, mid=mod_id, btn=emoji_btn: self._open_mod_emoji_menu(mid, btn))
            icon_row.addWidget(emoji_btn, 1, Qt.AlignmentFlag.AlignCenter)
            left_col.addWidget(icon_wrap, 0, Qt.AlignmentFlag.AlignHCenter)

            body = QVBoxLayout()
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(10)

            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(10)

            text_col = QVBoxLayout()
            text_col.setContentsMargins(0, 0, 0, 0)
            text_col.setSpacing(5)
            title = QLabel(str(mod["name"]))
            title.setProperty("class", "title")
            text_col.addWidget(title)

            state_map = {
                "enabled": self._t("Включена", "Enabled"),
                "installed": self._t("Выключена", "Disabled"),
                "not installed": self._t("Еще не подключена", "Not added yet"),
            }
            badge = QLabel(state_map.get(state, state))
            badge.setProperty("class", "modState")
            badge.setProperty("state", state)
            badge.setObjectName("ModStateBadge")
            text_col.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)
            head.addLayout(text_col, 1)

            actions = QHBoxLayout()
            actions.setContentsMargins(0, 0, 0, 0)
            actions.setSpacing(8)

            move_controls = QVBoxLayout()
            move_controls.setContentsMargins(0, 6, 0, 0)
            move_controls.setSpacing(2)
            move_up = QToolButton()
            move_up.setProperty("class", "action")
            move_up.setArrowType(Qt.ArrowType.UpArrow)
            move_up.setToolTip(self._t("Поднять выше", "Move up"))
            move_up.clicked.connect(lambda _=False, mid=mod_id: self._move_mod(mid, -1))
            move_down = QToolButton()
            move_down.setProperty("class", "action")
            move_down.setArrowType(Qt.ArrowType.DownArrow)
            move_down.setToolTip(self._t("Опустить ниже", "Move down"))
            move_down.clicked.connect(lambda _=False, mid=mod_id: self._move_mod(mid, 1))
            installed_total = sum(1 for item in combined if bool(item.get("installed")))
            if bool(mod.get("installed")) and installed_total > 1:
                if int(mod.get("order", 9999)) > 0:
                    move_controls.addWidget(move_up, 0, Qt.AlignmentFlag.AlignHCenter)
                if int(mod.get("order", 9999)) < installed_total - 1:
                    move_controls.addWidget(move_down, 0, Qt.AlignmentFlag.AlignHCenter)
            if move_controls.count() > 0:
                left_col.addLayout(move_controls)
            else:
                left_col.addSpacing(30)

            card_layout.addLayout(left_col, 0)

            toggle_btn = QToolButton()
            toggle_btn.setToolTip(self._t("Выключить модификацию", "Disable modification") if enabled else self._t("Включить модификацию", "Enable modification"))
            toggle_btn.setIcon(self._icon("power.svg"))
            toggle_btn.setIconSize(QSize(16, 16))
            toggle_btn.setFixedSize(36, 36)
            toggle_btn.setProperty("hoverRadius", 18)
            toggle_btn.setStyleSheet(self._mod_circle_action_style("power", active=enabled))
            toggle_btn.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
            toggle_btn.clicked.connect(lambda _=False, mid=mod_id: self._toggle_mod_by_id(mid))
            self._attach_button_animations(toggle_btn)
            actions.addWidget(toggle_btn)

            share_btn = QToolButton()
            share_btn.setToolTip(self._t("Поделиться модификацией", "Export modification"))
            share_btn.setIcon(self._icon("share.svg"))
            share_btn.setIconSize(QSize(16, 16))
            share_btn.setFixedSize(36, 36)
            share_btn.setProperty("hoverRadius", 18)
            share_btn.setStyleSheet(self._mod_circle_action_style("share", active=enabled))
            share_btn.setEnabled(bool(mod.get("installed")) and mod_id != "unified-by-peshk0v")
            share_btn.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
            share_btn.clicked.connect(lambda _=False, mid=mod_id: self._request_mod_export(mid))
            self._attach_button_animations(share_btn)
            if mod_id != "unified-by-peshk0v":
                actions.addWidget(share_btn)

            remove_btn = QToolButton()
            remove_btn.setToolTip(self._t("Удалить модификацию", "Delete modification"))
            remove_btn.setIcon(self._icon("trash.svg"))
            remove_btn.setIconSize(QSize(16, 16))
            remove_btn.setFixedSize(36, 36)
            remove_btn.setProperty("hoverRadius", 18)
            remove_btn.setStyleSheet(self._mod_circle_action_style("delete", active=False))
            remove_btn.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
            remove_btn.clicked.connect(lambda _=False, mid=mod_id: self._remove_mod_with_confirmation(mid))
            self._attach_button_animations(remove_btn)
            if mod_id != "unified-by-peshk0v":
                actions.addWidget(remove_btn)
            head.addLayout(actions)
            body.addLayout(head)

            desc = ExpandableDescriptionLabel(str(mod["description"]))
            body.addWidget(desc)

            meta_row = QHBoxLayout()
            meta_row.setContentsMargins(0, 0, 0, 0)
            meta_row.setSpacing(8)
            for meta_text in str(mod["subtitle"]).split(" | "):
                meta = QLabel(meta_text)
                meta.setProperty("class", "modMeta")
                meta.setObjectName("ModMetaChip")
                meta_row.addWidget(meta)
            meta_row.addStretch(1)
            body.addLayout(meta_row)
            card_layout.addLayout(body, 1)
            self.mods_cards_layout.addWidget(card)

        self.mods_cards_layout.addStretch(1)
        QTimer.singleShot(0, restore_scroll_position)

    def refresh_files(self, payload: object | None = None) -> None:
        mode_index = self._file_mode_stack.currentIndex() if self._file_mode_stack is not None else 0
        if isinstance(payload, dict):
            payload_mode = int(payload.get("mode_index", -1) or -1)
            if payload_mode not in {-1, mode_index}:
                return
            payload_collection = str(payload.get("collection_id", "") or "")
            payload_filter = str(payload.get("file_filter", "all") or "all")
            if mode_index == 2 and payload_filter != self._current_file_list_filter:
                return
            if mode_index == 1 and payload_collection != self._current_file_collection:
                return
            if mode_index == 1 and payload_collection == self._current_file_collection and payload.get("collection_values") is not None:
                incoming_values = list(payload.get("collection_values", []))
                self._apply_file_collection_meta()
                if incoming_values != self._current_file_values_cache or self._file_tag_flow is None or self._file_tag_flow.count() != len(incoming_values):
                    self._refresh_file_collection_view_with_values(incoming_values, finish_loading=True)
                else:
                    self._set_files_mode_loading(False)
            elif mode_index == 1:
                self._apply_file_collection_meta()
                self._set_files_mode_loading(False)
            records = payload.get("records", []) if payload.get("records") is not None else []
        else:
            if mode_index == 1:
                self._refresh_file_collection_view()
            records = []
        if mode_index == 0:
            QTimer.singleShot(0, self._prepare_files_page_geometry)
            return
        if mode_index != 2:
            if self._file_search_shell is not None:
                self._file_search_shell.raise_()
            return
        self._set_files_mode_loading(False)
        selected = self._selected_file_path()
        preferred = self._preferred_file_path
        self.files_list.clear()
        for record in records:
            if isinstance(record, dict):
                relative_path = str(record.get("relative_path", "") or record.get("path", ""))
                size = int(record.get("size", 0) or 0)
                path = str(record.get("path", "") or "")
            else:
                relative_path = str(getattr(record, "relative_path", ""))
                size = int(getattr(record, "size", 0) or 0)
                path = str(getattr(record, "path", "") or "")
            row_item = QListWidgetItem(f"{relative_path}\n{self._t('Размер', 'Size')}: {size} {self._t('байт', 'bytes')}")
            row_item.setData(Qt.ItemDataRole.UserRole, path)
            row_item.setSizeHint(QSize(200, 54))
            self.files_list.addItem(row_item)
        if not records:
            self.file_path_label.setText(
                self._t("General-файлы не найдены", "No General files found")
                if self._current_file_list_filter == "generals"
                else ("Hosts" if self._current_file_list_filter == "hosts" else self._t("Файлы не найдены", "No files found"))
            )
            self.file_editor.clear()
            self._set_file_editor_loading(False)
            return
        if preferred:
            for i in range(self.files_list.count()):
                it = self.files_list.item(i)
                if it.data(Qt.ItemDataRole.UserRole) == preferred:
                    self.files_list.setCurrentItem(it)
                    self._preferred_file_path = ""
                    break
            else:
                self._preferred_file_path = ""
                if self.files_list.count() > 0:
                    self.files_list.setCurrentRow(0)
        elif selected:
            for i in range(self.files_list.count()):
                it = self.files_list.item(i)
                if it.data(Qt.ItemDataRole.UserRole) == selected:
                    self.files_list.setCurrentItem(it)
                    break
            else:
                if self.files_list.count() > 0:
                    self.files_list.setCurrentRow(0)
        elif self.files_list.count() > 0:
            self.files_list.setCurrentRow(0)
        if self._file_search_shell is not None:
            self._file_search_shell.raise_()

    def _advance_files_loading_frame(self) -> None:
        self._files_loading_frame = (self._files_loading_frame + 1) % 4
        dots = "." * self._files_loading_frame
        if self._files_tags_loading_label is not None:
            self._files_tags_loading_label.setText(f"{self._t('Загрузка', 'Loading')}{dots}")
        if self._files_list_loading_label is not None:
            self._files_list_loading_label.setText(f"{self._t('Загрузка файлов', 'Loading files')}{dots}")
        if self._files_editor_loading_label is not None:
            self._files_editor_loading_label.setText(f"{self._t('Загрузка файла', 'Loading file')}{dots}")

    def _set_files_mode_loading(self, loading: bool, *, mode_index_override: int | None = None) -> None:
        mode_index = mode_index_override if mode_index_override is not None else (
            self._file_mode_stack.currentIndex() if self._file_mode_stack is not None else 0
        )
        self._files_loading_mode_index = mode_index
        if self._files_tags_stack is not None:
            self._files_tags_stack.setCurrentIndex(0 if (loading and mode_index == 1) else 1)
        if self._files_list_stack is not None:
            self._files_list_stack.setCurrentIndex(0 if (loading and mode_index == 2) else 1)
        if self._files_editor_stack is not None and mode_index == 2:
            self._files_editor_stack.setCurrentIndex(0 if loading else 1)
        active = (
            (self._files_tags_stack is not None and self._files_tags_stack.currentIndex() == 0)
            or (self._files_list_stack is not None and self._files_list_stack.currentIndex() == 0)
            or (self._files_editor_stack is not None and self._files_editor_stack.currentIndex() == 0)
        )
        if active and not self._files_loading_timer.isActive():
            self._files_loading_timer.start()
            self._advance_files_loading_frame()
        elif not active and self._files_loading_timer.isActive():
            self._files_loading_timer.stop()

    def _set_file_editor_loading(self, loading: bool) -> None:
        if self._files_editor_stack is not None:
            self._files_editor_stack.setCurrentIndex(0 if loading else 1)
        active = (
            (self._files_tags_stack is not None and self._files_tags_stack.currentIndex() == 0)
            or (self._files_list_stack is not None and self._files_list_stack.currentIndex() == 0)
            or (self._files_editor_stack is not None and self._files_editor_stack.currentIndex() == 0)
        )
        if active and not self._files_loading_timer.isActive():
            self._files_loading_timer.start()
            self._advance_files_loading_frame()
        elif not active and self._files_loading_timer.isActive():
            self._files_loading_timer.stop()

    def _request_file_content(self, full_path: str) -> None:
        self._file_content_refresh_token += 1
        self._pending_file_content_path = full_path
        self._set_file_editor_loading(True)
        thread = threading.Thread(
            target=self._collect_file_content_worker,
            args=(self._file_content_refresh_token, full_path),
            daemon=True,
        )
        thread.start()

    def _rebuild_logs_source_combo(self) -> None:
        if self._logs_source_combo is None:
            return
        options = [
            ("app", self._t("Приложение", "App")),
            ("zapret", "Zapret"),
            ("tg-ws-proxy", "TG WS Proxy"),
            ("peshk0v-vpn", self._t("VPN", "VPN")),
            ("all", self._t("Все логи", "All logs")),
        ]
        current = self._current_log_source
        self._logs_source_combo.blockSignals(True)
        self._logs_source_combo.clear()
        for source_id, title in options:
            self._logs_source_combo.addItem(title, source_id)
        index = max(0, self._logs_source_combo.findData(current))
        self._logs_source_combo.setCurrentIndex(index)
        self._logs_source_combo.blockSignals(False)

    def _on_logs_source_changed(self, *_args: object) -> None:
        if self._logs_source_combo is None:
            return
        self._current_log_source = str(self._logs_source_combo.currentData() or "app")
        self._logs_force_scroll_bottom = True
        self.refresh_logs()

    def _set_logs_live_enabled(self, enabled: bool) -> None:
        if enabled:
            if not self._logs_live_timer.isActive():
                self._logs_live_timer.start()
        elif self._logs_live_timer.isActive():
            self._logs_live_timer.stop()

    def _refresh_logs_live(self) -> None:
        if not self._logs_live_timer.isActive():
            return
        if self._logs_view_update_locked():
            return
        self._request_page_refresh("logs")

    def refresh_logs(self, payload: object | None = None) -> None:
        if payload is None:
            self._logs_force_scroll_bottom = True
            if self._logs_stack is not None:
                self._logs_stack.setCurrentIndex(0)
            self._request_page_refresh("logs")
            return
        if isinstance(payload, dict):
            if str(payload.get("source", "") or "") != self._current_log_source:
                return
            lines = list(payload.get("lines", []))
        elif isinstance(payload, list):
            lines = payload
        else:
            lines = []
        if self._logs_view_update_locked():
            self._pending_logs_payload = {
                "source": self._current_log_source,
                "lines": list(lines),
            }
            if self._logs_stack is not None:
                self._logs_stack.setCurrentIndex(1)
            return
        self._pending_logs_payload = None
        scrollbar = self.logs_text.verticalScrollBar()
        old_maximum = scrollbar.maximum()
        old_value = scrollbar.value()
        distance_from_bottom = max(0, old_maximum - old_value)
        at_bottom = bool(self._logs_force_scroll_bottom) or distance_from_bottom <= 4
        self._logs_force_scroll_bottom = False
        if self._logs_stack is not None:
            self._logs_stack.setCurrentIndex(1)
        self.logs_text.setPlainText("\n".join(lines) if lines else self._t("Логи пока пустые.", "No logs yet."))

        def _restore_scroll_position() -> None:
            if at_bottom:
                scrollbar.setValue(scrollbar.maximum())
            else:
                target = old_value
                scrollbar.setValue(min(target, scrollbar.maximum()))

        QTimer.singleShot(0, _restore_scroll_position)
        if at_bottom:
            QTimer.singleShot(40, _restore_scroll_position)

    def _logs_view_update_locked(self) -> bool:
        if not hasattr(self, "logs_text") or self.logs_text is None:
            return False
        try:
            return self.logs_text.textCursor().hasSelection()
        except Exception:
            return False

    def _on_logs_selection_changed(self) -> None:
        if self._logs_view_update_locked():
            return
        pending = self._pending_logs_payload
        if isinstance(pending, dict):
            self.refresh_logs(pending)



