"""
MSFS Actions panel — shows what has been (or will be) applied in the sim.
"""

from __future__ import annotations

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
from src.notam.models import MsfsAction

COLUMNS = [
    "NOTAM",
    "Airport",
    "Action",
    "Applied",
    "Applied At",
    "Distance (nm)",
    "Lit",
    "Note",
]


class ActionsPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._actions: list[MsfsAction] = []
        self._last_lat: Optional[float] = None
        self._last_lon: Optional[float] = None
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

        self._actions = list(actions)
        self._sort_actions()

        for row, action in enumerate(self._actions):
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

            at_str = action.applied_at.strftime("%H:%Mz") if action.applied_at else "-"
            distance = self._calculate_distance(action)
            distance_str = "—" if distance is None else f"{distance:.1f}"

            note_parts = []
            if action.error:
                note_parts.append(action.error)
            placement_note = action.params.get("placement_note")
            if placement_note:
                note_parts.append(str(placement_note))

            match action.action_type:
                case "disable_ils":
                    component = action.params.get("component", "full")
                    if component != "full":
                        note_parts.append(f"Component: {component.upper()}")
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])
                case "enable_ils":
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])
                case "disable_navaid":
                    navaid_type = action.params.get("navaid_type", "")
                    if navaid_type:
                        note_parts.append(navaid_type)
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])
                case "close_runway":
                    rwy = action.params.get("runway_designator", "")
                    if rwy:
                        note_parts.append(f"RWY {rwy}")
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])
                case "atis_unserviceable":
                    freq = action.params.get("frequency_mhz")
                    if freq is not None:
                        note_parts.append(f"{freq:.3f} MHz")
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])
                case "close_taxiway":
                    twy = action.params.get("taxiway_designator", "")
                    if twy:
                        note_parts.append(f"TWY {twy}")
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])
                case "close_stand":
                    stand = action.params.get("stand_designator", "")
                    if stand:
                        note_parts.append(f"Stand {stand}")
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])
                case "runway_limited":
                    rwy = action.params.get("runway_designator", "")
                    if rwy:
                        note_parts.append(f"RWY {rwy}")
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])
                case "fuel_unavailable":
                    fuel = action.params.get("fuel_type", "")
                    if fuel:
                        note_parts.append(fuel)
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])
                case "set_tfr":
                    lower = action.params.get("lower_ft", 0)
                    upper = action.params.get("upper_ft", 0)
                    radius = action.params.get("radius_nm")
                    tfr_info = f"{lower}–{upper} ft"
                    if radius:
                        tfr_info += f", r={radius} nm"
                    note_parts.append(tfr_info)
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:100])
                case "place_obstacle":
                    desc = action.params.get("description", "")
                    if desc:
                        note_parts.append(desc[:120])

            note = " — ".join(note_parts)
            lit_val = action.params.get("lit")
            lit_str = "Yes" if lit_val is True else ("No" if lit_val is False else "-")

            cells = [
                action.notam_id,
                action.icao,
                action.action_type,
                status,
                at_str,
                distance_str,
                lit_str,
                note,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(color))
                self._table.setItem(row, col, item)

    def _calculate_distance(self, action: MsfsAction) -> Optional[float]:
        if self._last_lat is None or self._last_lon is None:
            return None
        lat = action.params.get("lat")
        lon = action.params.get("lon")
        if lat is None or lon is None:
            return None
        return _haversine_nm(self._last_lat, self._last_lon, lat, lon)

    def _sort_actions(self) -> None:
        def sort_key(action: MsfsAction) -> float:
            if action.params.get("lat") is None or action.params.get("lon") is None:
                return float("inf")
            if self._last_lat is None or self._last_lon is None:
                return float("inf")
            return self._calculate_distance(action) or float("inf")

        self._actions.sort(key=sort_key)

    def update_position(self, lat: float, lon: float, alt: float) -> None:
        self._last_lat = lat
        self._last_lon = lon
        if self._actions:
            self._sort_actions()
            self.update_actions(self._actions)
