"""
NOTAM table widget — shows the current list of active NOTAMs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.notam.models import Notam, NotamCondition, NotamSubject

COLUMNS = ["ID", "Airport", "Subject", "Condition", "Valid From", "Valid To", "Description"]

CONDITION_COLORS: dict[NotamCondition, str] = {
    NotamCondition.UNSERVICEABLE: "#ff6b6b",
    NotamCondition.CLOSED:        "#ff6b6b",
    NotamCondition.NEW:           "#ffaa44",
    NotamCondition.CHANGED:       "#ffdd44",
    NotamCondition.SERVICEABLE:   "#44cc44",
    NotamCondition.OPEN:          "#44cc44",
    NotamCondition.RESTRICTED:    "#ff8844",
    NotamCondition.LIMITED:       "#ff8844",
    NotamCondition.LIMITED_PROC:  "#ff8844",
    NotamCondition.LIMITED_WX:    "#ffaa44",
    NotamCondition.FREQ_CHANGED:  "#ffdd44",
    NotamCondition.UNKNOWN:       "#888888",
}


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "PERM"
    return dt.strftime("%Y-%m-%d %H:%Mz")


class NotamTable(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._count_label = QLabel("No NOTAMs loaded")
        layout.addWidget(self._count_label)

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.setStyleSheet(
            "QTableWidget { background:#1e1e1e; color:#ffffff; gridline-color:#333; }"
            "QHeaderView::section { background:#2d2d2d; color:#cccccc; }"
            "QTableWidget::item:alternate { background:#252525; }"
        )
        layout.addWidget(self._table)

    def update_notams(self, notams: list[Notam]) -> None:
        self._table.setRowCount(0)

        active = [n for n in notams if n.is_active]
        self._count_label.setText(
            f"{len(active)} active NOTAM(s)  ({len(notams)} total fetched)"
        )

        for row, notam in enumerate(active):
            self._table.insertRow(row)
            color = CONDITION_COLORS.get(notam.condition, "#888888")

            cells = [
                notam.id,
                notam.icao,
                notam.subject.name,
                notam.condition.name,
                _fmt_dt(notam.valid_from),
                _fmt_dt(notam.valid_to),
                notam.description[:80],
            ]

            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(color))
                self._table.setItem(row, col, item)
