"""
MSFS Actions panel — shows what has been (or will be) applied in the sim.
"""

from __future__ import annotations

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

from src.notam.models import MsfsAction

COLUMNS = ["NOTAM", "Airport", "Action", "Applied", "Applied At", "Lit", "Note"]


class ActionsPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._count_label = QLabel("No actions yet")
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

    def update_actions(self, actions: list[MsfsAction]) -> None:
        self._table.setRowCount(0)
        applied = sum(1 for a in actions if a.applied)
        self._count_label.setText(
            f"{applied} applied / {len(actions)} total action(s)"
        )

        for row, action in enumerate(actions):
            self._table.insertRow(row)

            if action.error:
                color = "#ff6b6b"
                status = "Error"
            elif action.applied:
                color = "#44cc44"
                status = "Applied"
            else:
                color = "#ffaa44"
                status = "Pending"

            at_str  = action.applied_at.strftime("%H:%Mz") if action.applied_at else "-"
            note    = action.error or ""
            lit_val = action.params.get("lit")
            lit_str = "Yes" if lit_val is True else ("No" if lit_val is False else "-")

            cells = [
                action.notam_id,
                action.icao,
                action.action_type,
                status,
                at_str,
                lit_str,
                note,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(color))
                self._table.setItem(row, col, item)
