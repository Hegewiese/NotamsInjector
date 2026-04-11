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

from src.airports.lookup import _haversine_nm
from src.notam.models import Notam, NotamCondition, NotamSubject

COLUMNS = [
    "ID",
    "Airport",
    "Subject",
    "Condition",
    "Valid From",
    "Valid To",
    "Distance (nm)",
    "Description",
]

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
        self._notams: list[Notam] = []
        self._last_lat: Optional[float] = None
        self._last_lon: Optional[float] = None
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

    def _fmt_distance(self, distance: Optional[float]) -> str:
        return "—" if distance is None else f"{distance:.1f}"

    def _calculate_distance(self, notam: Notam) -> Optional[float]:
        if self._last_lat is None or self._last_lon is None:
            return None
        if notam.lat is None or notam.lon is None:
            return None
        return _haversine_nm(self._last_lat, self._last_lon, notam.lat, notam.lon)

    def _sort_notams(self) -> None:
        if self._last_lat is None or self._last_lon is None:
            self._notams.sort(key=lambda n: n.id)
            return

        def sort_key(notam: Notam) -> float:
            if notam.lat is None or notam.lon is None:
                return float("inf")
            return _haversine_nm(self._last_lat, self._last_lon, notam.lat, notam.lon)

        self._notams.sort(key=sort_key)

    def _render_table(self) -> None:
        self._table.setRowCount(0)
        for row, notam in enumerate(self._notams):
            self._table.insertRow(row)
            color = CONDITION_COLORS.get(notam.condition, "#888888")
            distance = self._calculate_distance(notam)

            cells = [
                notam.id,
                notam.icao,
                notam.subject.name,
                notam.condition.name,
                _fmt_dt(notam.valid_from),
                _fmt_dt(notam.valid_to),
                self._fmt_distance(distance),
                notam.description[:80],
            ]

            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(color))
                self._table.setItem(row, col, item)

    def update_notams(self, notams: list[Notam]) -> None:
        self._notams = [n for n in notams if n.is_active]
        self._count_label.setText(
            f"{len(self._notams)} active NOTAM(s)  ({len(notams)} total fetched)"
        )
        self._sort_notams()
        self._render_table()

    def update_position(self, lat: float, lon: float, alt: float) -> None:
        self._last_lat = lat
        self._last_lon = lon
        if self._notams:
            self._sort_notams()
            self._render_table()
