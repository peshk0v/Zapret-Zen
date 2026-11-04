from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QListView,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from zapret_zen.ui.pages.base import BasePage, PageHost
from zapret_zen.ui.theme import is_light_theme


class _FloatingComboOverlay(QObject):
    def __init__(self, combo: QComboBox, host: QWidget) -> None:
        super().__init__(host)
        self._combo = combo
        self._host = host
        host.installEventFilter(self)
        self.snap()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._host and event.type() == QEvent.Type.Resize:
            self.snap()
        return False

    def snap(self) -> None:
        self._combo.adjustSize()
        width = self._host.width()
        x = max(0, width - self._combo.width() - 12)
        self._combo.move(x, 10)
        self._combo.raise_()


class LogsPage(BasePage):
    """Logs page — displays application and component logs."""

    def __init__(self, host: PageHost, parent: QWidget | None = None) -> None:
        super().__init__(host, parent)
        self.setProperty("class", "pageRoot")
        self._current_log_source = "all"
        self._pending_logs_payload: dict[str, object] | None = None
        self._logs_force_scroll_top = True
        self._title_label: QLabel | None = None
        self._float_overlay: _FloatingComboOverlay | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 18, 16, 18)
        root.setSpacing(10)

        self._logs_text = QTextEdit()
        self._logs_text.setReadOnly(True)
        self._logs_text.selectionChanged.connect(self._on_selection_changed)
        self._register_scroll_fade(self._logs_text)
        self._register_smooth_scroll(self._logs_text)

        self._logs_stack = QStackedWidget()
        logs_loading = QLabel(self._t("Загрузка логов...", "Loading logs..."))
        logs_loading.setProperty("class", "muted")
        logs_loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_label = logs_loading
        self._logs_stack.addWidget(logs_loading)
        self._logs_stack.addWidget(self._logs_text)
        root.addWidget(self._logs_stack)

        self._source_combo = QComboBox()
        self._source_combo.setObjectName("LogsSourceCombo")
        self._source_combo.setView(QListView())
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        self._apply_combo_plate_style()
        self._rebuild_source_combo()
        self._source_combo.setParent(self._logs_stack)
        self._source_combo.show()
        self._float_overlay = _FloatingComboOverlay(self._source_combo, self._logs_stack)

    @property
    def logs_text(self) -> QTextEdit:
        return self._logs_text

    @property
    def source_combo(self) -> QComboBox:
        return self._source_combo

    @property
    def current_log_source(self) -> str:
        return self._current_log_source

    def _rebuild_source_combo(self) -> None:
        sources = [
            ("all", self._t("Все", "All")),
            ("app", "App"),
            ("zapret", "Zapret"),
            ("tg", "TG Proxy"),
            ("warp", "WARP"),
        ]
        self._source_combo.blockSignals(True)
        self._source_combo.clear()
        for key, label in sources:
            self._source_combo.addItem(label, key)
        self._source_combo.blockSignals(False)

    def _on_source_changed(self, _index: int) -> None:
        data = self._source_combo.currentData()
        if data:
            self._current_log_source = str(data)

    def _apply_combo_plate_style(self) -> None:
        theme = ""
        try:
            theme = str(self.context.settings.get().theme)
        except Exception:
            theme = ""
        dark = not is_light_theme(theme)
        if theme == "oled":
            background = "#101215"
        elif theme == "dark":
            background = "#15171a"
        elif dark:
            background = "#0d1320"
        else:
            background = "#f4f7fc"
        border = "rgba(90, 122, 186, 0.95)" if dark else "rgba(131, 159, 212, 0.95)"
        self._source_combo.setStyleSheet(
            "QComboBox {"
            f"background: {background};"
            f"border: 1px solid {border};"
            "border-radius: 10px;"
            "padding: 4px 10px;"
            "}"
        )

    def _on_selection_changed(self) -> None:
        cursor = self._logs_text.textCursor()
        if not cursor.hasSelection() and self._pending_logs_payload is not None:
            payload = self._pending_logs_payload
            self._pending_logs_payload = None
            self._apply_payload(payload)

    def _apply_payload(self, payload: dict[str, object]) -> None:
        lines = payload.get("lines", []) if isinstance(payload, dict) else []
        if isinstance(lines, list):
            text = "\n".join(str(line) for line in lines)
        else:
            text = str(lines) if lines else ""
        self._logs_text.setPlainText(text)
        if self._logs_force_scroll_top:
            sb = self._logs_text.verticalScrollBar()
            if sb is not None:
                sb.setValue(0)

    def refresh(self, payload: object | None = None) -> None:
        if payload is None:
            return
        if isinstance(payload, dict):
            source = str(payload.get("source", self._current_log_source))
            if source:
                self._current_log_source = source
        cursor = self._logs_text.textCursor()
        if cursor.hasSelection():
            self._pending_logs_payload = payload
            return
        self._apply_payload(payload)

    def set_live_enabled(self, enabled: bool) -> None:
        pass

    def view_update_locked(self) -> bool:
        cursor = self._logs_text.textCursor()
        return cursor.hasSelection()
