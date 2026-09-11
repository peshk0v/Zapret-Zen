from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QListWidget,
    QAbstractItemView,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from zapret_zen.ui.pages.base import BasePage, PageHost


class ComponentsPage(BasePage):
    """Components page — manage zapret, tg-ws-proxy and other components."""

    def __init__(self, host: PageHost, parent: QWidget | None = None) -> None:
        super().__init__(host, parent)
        self.setProperty("class", "pageRoot")

        self._scroll: QScrollArea | None = None
        self._cards_root: QWidget | None = None
        self._cards_layout: QVBoxLayout | None = None
        self._card_by_id: dict[str, QFrame] = {}
        self._scroll_target_component_id = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 0, 1, 0)
        root.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("ComponentsScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._cards_root = QWidget()
        self._cards_root.setObjectName("ComponentsCanvas")
        self._cards_root.setProperty("class", "pageCanvas")
        self._cards_layout = QVBoxLayout(self._cards_root)
        self._cards_layout.setContentsMargins(12, 8, 12, 12)
        self._cards_layout.setSpacing(12)
        self._cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._cards_root)
        self._register_scroll_fade(self._scroll)
        self._register_smooth_scroll(self._scroll)
        root.addWidget(self._scroll, 1)
