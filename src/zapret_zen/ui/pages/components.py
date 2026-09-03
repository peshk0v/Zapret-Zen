from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QAbstractItemView,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from zapret_zen.platform import IS_LINUX
from zapret_zen.ui.pages.base import BasePage, PageHost


class ComponentsPage(BasePage):
    """Components page — manage zapret, tg-ws-proxy and other components."""

    def __init__(self, host: PageHost, parent: QWidget | None = None) -> None:
        super().__init__(host, parent)
        self.setProperty("class", "pageRoot")

        self._title_label: QLabel | None = None
        self._scroll: QScrollArea | None = None
        self._cards_root: QWidget | None = None
        self._cards_layout: QHBoxLayout | None = None
        self._card_by_id: dict[str, QFrame] = {}
        self._scroll_target_component_id = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 0, 1, 0)
        root.setSpacing(6)

        label = QLabel(self._t("Компоненты", "Components"))
        label.setProperty("class", "title")
        self._title_label = label
        root.addWidget(label)

        if IS_LINUX:
            header = QWidget()
            header_layout = QHBoxLayout(header)
            header_layout.setContentsMargins(0, 0, 0, 0)
            header_layout.setSpacing(8)
            self._linux_sudoers_status = QLabel("")
            self._linux_sudoers_status.setProperty("class", "muted")
            sudoers_btn = QPushButton(self._t("Настроить автоматические права (root)", "Configure automatic root privileges"))
            sudoers_btn.setProperty("class", "inlineButton")
            self._attach_button_animations(sudoers_btn)
            sudoers_btn.clicked.connect(self._on_configure_sudoers)
            self._linux_sudoers_button = sudoers_btn
            self._linux_sudoers_row = header
            header_layout.addWidget(self._linux_sudoers_status)
            header_layout.addStretch(1)
            header_layout.addWidget(sudoers_btn)
            root.addWidget(header)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("ComponentsScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._cards_root = QWidget()
        self._cards_root.setObjectName("ComponentsCanvas")
        self._cards_root.setProperty("class", "pageCanvas")
        self._cards_layout = QHBoxLayout(self._cards_root)
        self._cards_layout.setContentsMargins(1, 0, 1, 12)
        self._cards_layout.setSpacing(12)
        self._cards_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._cards_root)
        self._register_scroll_fade(self._scroll)
        self._register_scroll_arrow(self._scroll)
        self._register_smooth_scroll(self._scroll)
        root.addWidget(self._scroll, 1)

    def _on_configure_sudoers(self) -> None:
        self._host._submit_backend_task("configure_zapret_sudoers", action_id="linux_sudoers_config")

    def set_sudoers_configured(self, configured: bool) -> None:
        if not hasattr(self, "_linux_sudoers_status"):
            return
        if configured:
            self._linux_sudoers_status.setText(self._t("Права root настроены", "Root privileges configured"))
            self._linux_sudoers_button.setText(self._t("Автоматические права настроены", "Automatic privileges configured"))
        else:
            self._linux_sudoers_status.setText("")
