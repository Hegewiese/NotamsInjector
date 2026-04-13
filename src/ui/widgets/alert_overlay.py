"""
Floating on-screen NOTAM alert panel.

Always-on-top compact overlay used as a fallback when in-sim text is not visible.
Alerts are grouped by airport (ICAO) and sorted nearest-first within each group.
Clicking a row expands/collapses the full message.
"""

from __future__ import annotations

import html
import math
from typing import Callable

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPainter, QPen, QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.airports.lookup import AirportLookup, _haversine_nm
from src.config import settings

_ICAO_HEADER_STYLE = (
    "color:#aad4ff; font-size:11px; font-weight:700; letter-spacing:1px;"
    " background: transparent; border: none; padding: 0;"
)
_AIRPORT_BOX_STYLE = "background: #2a2a3a; border: none; border-radius: 3px;"
_HEADER_STYLE = (
    "color:#fff3a6; font-size:13px; font-weight:800;"
    " background: #3f444d; border: 1px solid #59616d;"
    " padding: 3px 6px; border-radius: 4px;"
)
_BODY_STYLE = (
    "color:#f6f8fb; font-size:13px; font-weight:500;"
    " background: #444b50; border: none;"
    " padding: 0;"
)
_SEP_STYLE = "background-color: #555; border: none; min-height:1px; max-height:1px;"
_TYPE_HEADER_STYLE = (
    "color:#d8f0ff; font-size:11px; font-weight:600;"
    " background: #3a3a4a; border: none; padding: 2px 6px; border-radius: 3px;"
)
_AIRPORT_DETAILS_STYLE = (
    "color:#d2d9e3; font-size:10px;"
    " background: transparent; border: none; padding: 0; margin: 0;"
)
_FIXED_WIDTH = 400
_GROUP_BY_TYPE_THRESHOLD = 7
_MAX_HEIGHT_RATIO = 0.65
_MIN_OVERLAY_HEIGHT = 180
_MIN_ROWS_VIEWPORT_HEIGHT = 92
_HEIGHT_SAFETY_PX = 12
_AIRPORT_HEADER_MIN_HEIGHT = 28
_TYPE_HEADER_MIN_HEIGHT = 28
_ALERT_HEADER_MIN_HEIGHT = 30
_AIRPORT_DETAILS_DURATION_MS = 10_000
_INITIAL_VISIBLE_HEIGHT_RATIO = 0.25
_BOTTOM_RESIZE_HIT_PX = 10

_AIRPORT_TYPE_LABELS = {
    "balloonport": "Baloon Port",
    "closed": "CLOSED",
    "heliport": "Heliport",
    "large_airport": "Large Airport",
    "medium_airport": "Medium Airport",
    "seaplane_base": "Seaplane Base",
    "small_airport": "Small Airport",
}


class _CircularProgressDial(QWidget):
    """Movement dial: 0% at cycle start, 100% at minimum move."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._progress = 0
        self.setFixedSize(20, 20)
        self.setStyleSheet("background: transparent; border: none;")

    def set_progress(self, percent: int) -> None:
        self._progress = max(0, min(100, percent))
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        center_x, center_y = rect.width() // 2, rect.height() // 2
        radius = 8

        bg_pen = QPen(QColor(80, 80, 100), 1.5)
        painter.setPen(bg_pen)
        painter.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)

        if self._progress > 0:
            progress_pen = QPen(QColor(138, 168, 255), 1.5)
            painter.setPen(progress_pen)
            start_angle = 90 * 16
            span_angle = -int((self._progress / 100.0) * 360 * 16)
            painter.drawArc(
                center_x - radius,
                center_y - radius,
                radius * 2,
                radius * 2,
                start_angle,
                span_angle,
            )

        painter.end()


class _AlertRow(QWidget):
    """One collapsible row: header one-liner + expandable body."""

    def __init__(self, title: str, message: str, expanded: bool, on_toggle: Callable[[bool], None]) -> None:
        super().__init__()
        self._expanded = expanded
        self._on_toggle = on_toggle

        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._header = QLabel(f"▶  {title}")
        self._header.setStyleSheet(_HEADER_STYLE)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setWordWrap(True)
        self._header.setMinimumHeight(_ALERT_HEADER_MIN_HEIGHT)
        self._header.setMaximumHeight(_ALERT_HEADER_MIN_HEIGHT)
        self._header.mousePressEvent = lambda _: self._toggle()  # type: ignore[method-assign]

        self._body = QLabel(message)
        self._body.setWordWrap(True)
        self._body.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._body.setTextFormat(Qt.TextFormat.PlainText)
        self._body.setStyleSheet(_BODY_STYLE)
        # Use QLabel margin (included in size hint) to avoid line clipping on wrapped text.
        self._body.setMargin(4)
        self._apply_expanded_state()

        layout.addWidget(self._header)
        layout.addWidget(self._body)

    def _apply_expanded_state(self) -> None:
        if self._expanded:
            self._header.setText(self._header.text().replace("▶", "▼", 1) if "▶" in self._header.text() else self._header.text())
            self._body.setMaximumHeight(16777215)
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self._body.show()
        else:
            self._header.setText(self._header.text().replace("▼", "▶", 1) if "▼" in self._header.text() else self._header.text())
            self._body.setMaximumHeight(0)
            self.setMinimumHeight(_ALERT_HEADER_MIN_HEIGHT)
            self.setMaximumHeight(_ALERT_HEADER_MIN_HEIGHT)
            self._body.hide()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._apply_expanded_state()
        self.updateGeometry()

    def _toggle(self) -> None:
        self._on_toggle(not self._expanded)


class AlertOverlay(QWidget):
    def __init__(self, airport_lookup: AirportLookup | None = None, parent=None) -> None:
        super().__init__(parent)
        # (dist_nm, icao, airport_name, notam_type, title, message)
        self._alerts: list[tuple[float, str, str, str, str, str]] = []
        self._expanded_type_key: tuple[str, ...] | None = None
        self._expanded_alert_key: tuple[str, ...] | None = None
        self._alert_row_widgets: dict[tuple[str, ...], _AlertRow] = {}
        self._alert_parent_type: dict[tuple[str, ...], tuple[str, ...] | None] = {}
        self._type_group_widgets: dict[tuple[str, ...], tuple[QLabel, QWidget, str, int, float, QWidget]] = {}
        self._airport_details_widgets: dict[str, list[QLabel]] = {}
        self._airport_header_labels: dict[str, tuple[QLabel, str, float]] = {}
        self._airport_lookup = airport_lookup
        self._aircraft_lat: float | None = None
        self._aircraft_lon: float | None = None
        self._aircraft_heading_deg: float | None = None
        self._on_open_settings: Callable[[], None] | None = None
        self._on_open_debug: Callable[[], None] | None = None
        self._details_icao: str | None = None
        self._details_timer = QTimer(self)
        self._details_timer.setSingleShot(True)
        self._details_timer.timeout.connect(self._clear_airport_details)
        self._drag_pos: QPoint | None = None
        self._pinned_top_left: QPoint | None = None
        self._allow_programmatic_move = False
        self._preferred_visible_height: int | None = None
        self._is_resizing = False
        self._resize_start_global_y = 0
        self._resize_start_height = 0

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # ── title bar ─────────────────────────────────────────────────────────
        title_bar = QWidget()
        self._title_bar = title_bar
        title_bar.setStyleSheet("background: transparent; border: none;")
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(4)

        self._poll_dial = _CircularProgressDial()
        title_layout.addWidget(self._poll_dial)

        title_lbl = QLabel("NOTAM Alerts")
        title_lbl.setStyleSheet(
            "color:#ffffff; font-size:13px; font-weight:700;"
            " background: transparent; border: none;"
        )
        title_layout.addWidget(title_lbl)
        title_layout.addStretch()

        debug_btn = QPushButton("Debug")
        debug_btn.setFixedSize(54, 22)
        debug_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        debug_btn.setStyleSheet(
            "QPushButton { background: #2a4a6a; color:#f0f0f0; border: none;"
            " border-radius: 4px; font-size:10px; font-weight:700; padding: 0 6px; }"
            "QPushButton:hover { background: #3b678f; }"
            "QPushButton:pressed { background: #1e3953; }"
        )
        debug_btn.clicked.connect(self._open_debug)
        title_layout.addWidget(debug_btn)

        settings_btn = QPushButton("Settings")
        settings_btn.setFixedSize(64, 22)
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(
            "QPushButton { background: #365a3a; color:#f0f0f0; border: none;"
            " border-radius: 4px; font-size:10px; font-weight:700; padding: 0 6px; }"
            "QPushButton:hover { background: #4a7a50; }"
            "QPushButton:pressed { background: #28442b; }"
        )
        settings_btn.clicked.connect(self._open_settings)
        title_layout.addWidget(settings_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton { background: #5a2a2a; color:#f0f0f0; border: none;"
            " border-radius: 4px; font-size:11px; font-weight:700; }"
            "QPushButton:hover { background: #8a3a3a; }"
            "QPushButton:pressed { background: #3a1a1a; }"
        )
        close_btn.clicked.connect(self.hide)
        title_layout.addWidget(close_btn)

        # ── rows area ──────────────────────────────────────────────────────────
        self._rows_widget = QWidget()
        self._rows_widget.setStyleSheet("background: transparent; border: none;")
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(1)
        self._rows_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._rows_scroll = QScrollArea()
        self._rows_scroll.setWidgetResizable(False)
        self._rows_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._rows_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._rows_scroll.setMinimumHeight(_MIN_ROWS_VIEWPORT_HEIGHT)
        self._rows_scroll.setWidget(self._rows_widget)
        self._rows_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        # ── main layout ────────────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(title_bar)
        layout.addWidget(self._rows_scroll)

        self.setStyleSheet(
            "AlertOverlay {"
            "  background-color: #3c3c3c;"
            "  border: 2px solid #888;"
            "  border-radius: 10px;"
            "}"
        )
        self.setFixedWidth(_FIXED_WIDTH)
        self.setWindowOpacity(max(0.1, min(1.0, float(settings.alert_window_opacity))))
        self.hide()

    # ── public API ─────────────────────────────────────────────────────────────

    def clear_alerts(self) -> None:
        self._alerts.clear()
        self._expanded_type_key = None
        self._expanded_alert_key = None
        self._details_icao = None
        self._details_timer.stop()
        self._clear_rows()

    def clear_and_hide(self) -> None:
        self.clear_alerts()
        self.hide()

    def replace_alerts(self, alerts: list[tuple[str, str, float, str, str, str, bool]], pop_up: bool = False) -> None:
        """Atomically replace all overlay alerts to avoid clear/add flicker."""
        if not alerts:
            self.clear_and_hide()
            return

        self._alerts = [
            (dist_nm, icao.upper(), airport_name, notam_type, title, message)
            for (title, message, dist_nm, icao, airport_name, notam_type, _is_new) in alerts
        ]
        self._alerts.sort(key=lambda a: a[0])

        was_hidden = not self.isVisible()
        self._rebuild_rows()
        self._resize_height()
        QTimer.singleShot(0, self._resize_height)
        QTimer.singleShot(30, self._resize_height)

        if was_hidden and pop_up:
            self._reposition_top_right()
            self.show()
            self.raise_()

    def show_alert(
        self,
        title: str,
        message: str,
        dist_nm: float = 0.0,
        icao: str = "",
        airport_name: str = "",
        notam_type: str = "",
        pop_up: bool = False,
    ) -> None:
        """Add/re-sort alert grouped by ICAO; rows inside each group sorted by type then distance."""
        title = title[:80]
        message = message[:400]
        self._alerts.append((dist_nm, icao.upper(), airport_name, notam_type, title, message))
        self._alerts.sort(key=lambda a: a[0])
        is_hidden = not self.isVisible()
        self._rebuild_rows()
        self._resize_height()
        # Qt sometimes finalizes wrapped-label geometry one event-loop tick later;
        # run a deferred second pass so first render is fully sized without user interaction.
        QTimer.singleShot(0, self._resize_height)
        QTimer.singleShot(30, self._resize_height)
        if is_hidden and pop_up:
            self._reposition_top_right()
            self.show()
            self.raise_()

    def update_reference_position(self, lat: float, lon: float, heading_deg: float) -> None:
        """Refresh airport distance and direction indicators on each sampled position."""
        self._aircraft_lat = lat
        self._aircraft_lon = lon
        self._aircraft_heading_deg = heading_deg % 360.0
        if not self._airport_header_labels:
            return
        for icao, (label, airport_name, fallback_nm) in self._airport_header_labels.items():
            label.setText(self._format_airport_header(icao, airport_name, fallback_nm))

    def update_poll_progress(self, percent: int) -> None:
        """Update movement dial progress (0-100)."""
        self._poll_dial.set_progress(percent)

    def set_action_handlers(
        self,
        *,
        on_open_settings: Callable[[], None] | None = None,
        on_open_debug: Callable[[], None] | None = None,
    ) -> None:
        self._on_open_settings = on_open_settings
        self._on_open_debug = on_open_debug

    # ── private ────────────────────────────────────────────────────────────────

    def showEvent(self, event: QShowEvent) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._preferred_visible_height is None:
            screen = QGuiApplication.screenAt(self.pos()) or QGuiApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                self._preferred_visible_height = max(_MIN_OVERLAY_HEIGHT, int(area.height() * _INITIAL_VISIBLE_HEIGHT_RATIO))
                self._resize_height()

    def _is_in_bottom_resize_zone(self, local_pos: QPoint) -> bool:
        return local_pos.y() >= self.height() - _BOTTOM_RESIZE_HIT_PX

    def _update_resize_cursor(self, local_pos: QPoint) -> None:
        if self._is_resizing:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            return
        if self._is_in_bottom_resize_zone(local_pos):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif self._drag_pos is None:
            self.unsetCursor()

    def _clear_rows(self) -> None:
        self._alert_row_widgets.clear()
        self._alert_parent_type.clear()
        self._type_group_widgets.clear()
        self._airport_details_widgets.clear()
        self._airport_header_labels.clear()
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.deleteLater()

    def _format_airport_header(self, icao: str, airport_name: str, fallback_nm: float) -> str:
        dist_nm = self._distance_to_airport_nm(icao, fallback_nm)
        name_part = f"  {airport_name}" if airport_name else ""
        arrow = self._direction_arrow(icao)
        arrow_part = f"  {arrow}" if arrow else ""
        return f"{icao}{name_part}  —  {dist_nm:.1f} nm{arrow_part}"

    def _distance_to_airport_nm(self, icao: str, fallback_nm: float = 0.0) -> float:
        if self._airport_lookup is None:
            return fallback_nm
        if self._aircraft_lat is None or self._aircraft_lon is None:
            return fallback_nm
        ap = self._airport_lookup.find(icao)
        if ap is None:
            return fallback_nm
        return _haversine_nm(self._aircraft_lat, self._aircraft_lon, ap.lat, ap.lon)

    def _direction_arrow(self, icao: str) -> str:
        if self._airport_lookup is None:
            return ""
        if self._aircraft_lat is None or self._aircraft_lon is None or self._aircraft_heading_deg is None:
            return ""
        ap = self._airport_lookup.find(icao)
        if ap is None:
            return ""

        lat1 = math.radians(self._aircraft_lat)
        lat2 = math.radians(ap.lat)
        dlon = math.radians(ap.lon - self._aircraft_lon)
        y = math.sin(dlon) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
        rel = (bearing - self._aircraft_heading_deg + 360.0) % 360.0
        arrows = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
        idx = int((rel + 22.5) // 45) % 8
        return arrows[idx]

    def _rebuild_rows(self) -> None:
        self.setUpdatesEnabled(False)
        try:
            self._clear_rows()

            groups: dict[str, list[tuple[float, str, str, str, str, str]]] = {}
            for alert in self._alerts:
                icao = alert[1]
                groups.setdefault(icao, []).append(alert)

            ordered_groups: list[tuple[str, list[tuple[float, str, str, str, str, str]]]] = sorted(
                groups.items(),
                key=lambda item: min(self._distance_to_airport_nm(a[1], a[0]) for a in item[1]),
            )

            first_group = True
            for icao, group in ordered_groups:
                group.sort(key=lambda a: (a[3], self._distance_to_airport_nm(a[1], a[0])))

                if not first_group:
                    sep = QLabel()
                    sep.setFixedHeight(1)
                    sep.setStyleSheet(_SEP_STYLE)
                    self._rows_layout.addWidget(sep)
                first_group = False

                min_dist = min(self._distance_to_airport_nm(a[1], a[0]) for a in group)
                airport_name = group[0][2]   # same for all entries in this group
                airport_block = QWidget()
                airport_block.setStyleSheet(_AIRPORT_BOX_STYLE)
                airport_layout = QVBoxLayout(airport_block)
                airport_layout.setContentsMargins(6, 2, 6, 2)
                airport_layout.setSpacing(0)
                airport_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

                airport_lbl = QLabel(self._format_airport_header(icao, airport_name, min_dist))
                airport_lbl.setStyleSheet(_ICAO_HEADER_STYLE)
                airport_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                airport_lbl.setMinimumHeight(_AIRPORT_HEADER_MIN_HEIGHT)
                airport_lbl.setMaximumHeight(_AIRPORT_HEADER_MIN_HEIGHT)
                airport_lbl.mousePressEvent = (  # type: ignore[method-assign]
                    lambda _e, airport_icao=icao: self._show_airport_details(airport_icao)
                )
                airport_layout.addWidget(airport_lbl)
                self._airport_header_labels[icao] = (airport_lbl, airport_name, min_dist)

                details = self._build_airport_details_lines(icao)
                if details:
                    widgets: list[QLabel] = []
                    show_details = self._details_icao == icao
                    for line in details:
                        details_lbl = QLabel(line)
                        details_lbl.setWordWrap(True)
                        details_lbl.setStyleSheet(_AIRPORT_DETAILS_STYLE)
                        if line.startswith("Home: ") or line.startswith("Wiki: "):
                            label, url = line.split(": ", 1)
                            safe_label = html.escape(label)
                            safe_url = html.escape(url)
                            details_lbl.setTextFormat(Qt.TextFormat.RichText)
                            details_lbl.setText(
                                f"{safe_label}: <a href=\"{safe_url}\" style=\"color:#9ecbff; text-decoration: underline;\">{safe_url}</a>"
                            )
                            details_lbl.setOpenExternalLinks(True)
                        details_lbl.setVisible(show_details)
                        airport_layout.addWidget(details_lbl)
                        widgets.append(details_lbl)
                    self._airport_details_widgets[icao] = widgets

                self._rows_layout.addWidget(airport_block)

                if len(group) > _GROUP_BY_TYPE_THRESHOLD:
                    by_type: dict[str, list[tuple[float, str, str, str, str, str]]] = {}
                    for alert in group:
                        key = self._normalize_notam_type(alert[3])
                        by_type.setdefault(key, []).append(alert)

                    ordered_types = sorted(
                        by_type.items(),
                        key=lambda item: (-len(item[1]), min(a[0] for a in item[1]), item[0]),
                    )

                    for type_name, type_alerts in ordered_types:
                        type_alerts.sort(key=lambda a: self._distance_to_airport_nm(a[1], a[0]))
                        group_key = ("type", icao, type_name)
                        expanded = self._expanded_type_key == group_key
                        nearest = min(self._distance_to_airport_nm(a[1], a[0]) for a in type_alerts)

                        type_block = QWidget()
                        type_block.setMinimumHeight(_TYPE_HEADER_MIN_HEIGHT)
                        type_block.setMaximumHeight(_TYPE_HEADER_MIN_HEIGHT if not expanded else 16777215)
                        type_layout = QVBoxLayout(type_block)
                        type_layout.setContentsMargins(8, 0, 0, 0)
                        type_layout.setSpacing(0)

                        header = QLabel()
                        header.setStyleSheet(_TYPE_HEADER_STYLE)
                        header.setCursor(Qt.CursorShape.PointingHandCursor)
                        header.setMinimumHeight(_TYPE_HEADER_MIN_HEIGHT)
                        header.setMaximumHeight(_TYPE_HEADER_MIN_HEIGHT)
                        self._set_type_header_text(
                            header,
                            type_name=type_name,
                            count=len(type_alerts),
                            nearest_nm=nearest,
                            expanded=expanded,
                        )

                        body = QWidget()
                        body_layout = QVBoxLayout(body)
                        body_layout.setContentsMargins(4, 0, 0, 0)
                        body_layout.setSpacing(0)
                        body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
                        body.setMaximumHeight(0 if not expanded else 16777215)
                        body.setVisible(expanded)
                        self._type_group_widgets[group_key] = (header, body, type_name, len(type_alerts), nearest, type_block)

                        header.mousePressEvent = (  # type: ignore[method-assign]
                            lambda _e, key=group_key: self._toggle_type_group(key)
                        )

                        type_layout.addWidget(header)
                        type_layout.addWidget(body)

                        for _dist, _icao, _name, _type, title, message in type_alerts:
                            alert_key = ("alert", _icao, title, message)
                            row = _AlertRow(
                                title,
                                message,
                                expanded=self._expanded_alert_key == alert_key,
                                on_toggle=lambda expand, key=alert_key: self._toggle_alert_row(key, expand),
                            )
                            self._alert_row_widgets[alert_key] = row
                            self._alert_parent_type[alert_key] = group_key
                            body_layout.addWidget(row)

                        self._rows_layout.addWidget(type_block)
                else:
                    for _dist, _icao, _name, _type, title, message in group:
                        alert_key = ("alert", _icao, title, message)
                        row = _AlertRow(
                            title,
                            message,
                            expanded=self._expanded_alert_key == alert_key,
                            on_toggle=lambda expand, key=alert_key: self._toggle_alert_row(key, expand),
                        )
                        self._alert_row_widgets[alert_key] = row
                        self._alert_parent_type[alert_key] = None
                        self._rows_layout.addWidget(row)
        finally:
            self.setUpdatesEnabled(True)

    @staticmethod
    def _normalize_notam_type(raw_type: str) -> str:
        text = (raw_type or "").strip()
        if not text:
            return "Other"
        return text.replace("_", " ").replace("-", " ").title()

    @staticmethod
    def _set_type_header_text(
        header: QLabel,
        *,
        type_name: str,
        count: int,
        nearest_nm: float,
        expanded: bool,
    ) -> None:
        marker = "-" if expanded else "+"
        header.setText(f"{marker}  {type_name} ({count})")

    @staticmethod
    def _display_airport_type(raw_type: str) -> str:
        key = (raw_type or "").strip().lower()
        if key in _AIRPORT_TYPE_LABELS:
            return _AIRPORT_TYPE_LABELS[key]
        normalized = key.replace("_", " ").replace("-", " ").title()
        return normalized or "Unknown"

    @staticmethod
    def _format_elevation(elevation_ft: int | None) -> str:
        if elevation_ft is None:
            return "Elevation n/a"
        return f"{elevation_ft:,} ft"

    def _build_airport_details_lines(self, icao: str) -> list[str]:
        try:
            if self._airport_lookup is None:
                return []

            ap = self._airport_lookup.find(icao)
            if ap is None:
                return []

            line1_parts = [
                self._display_airport_type(ap.type),
                (ap.municipality or "").strip(),
                (ap.country or "").strip(),
                (ap.region or "").strip(),
                (ap.continent or "").strip(),
                self._format_elevation(ap.elevation_ft),
            ]
            line1 = " • ".join(part for part in line1_parts if part)

            home_link = (ap.home_link or "").strip()
            wiki_link = (ap.wikipedia_link or "").strip()
            line2 = f"Home: {home_link}" if home_link else (f"Wiki: {wiki_link}" if wiki_link else "")

            if not line2:
                return [line1]
            return [line1, line2]
        except Exception:
            return []

    def _show_airport_details(self, icao: str) -> None:
        self._details_icao = icao
        self._details_timer.start(_AIRPORT_DETAILS_DURATION_MS)
        self._refresh_airport_details_visibility()
        self._rows_scroll.verticalScrollBar().setValue(0)
        self._resize_height()

    def _open_settings(self) -> None:
        if self._on_open_settings is not None:
            self._on_open_settings()

    def _open_debug(self) -> None:
        if self._on_open_debug is not None:
            self._on_open_debug()

    def _clear_airport_details(self) -> None:
        if self._details_icao is None:
            return
        self._details_icao = None
        self._refresh_airport_details_visibility()
        self._resize_height()

    def _refresh_airport_details_visibility(self) -> None:
        for icao, labels in self._airport_details_widgets.items():
            visible = self._details_icao == icao
            for lbl in labels:
                lbl.setVisible(visible)

    def _collapse_expanded_alert(self) -> None:
        if self._expanded_alert_key is None:
            return

        row = self._alert_row_widgets.get(self._expanded_alert_key)
        if row is not None:
            row.set_expanded(False)
        self._expanded_alert_key = None

    def _collapse_type_group(self, group_key: tuple[str, ...]) -> None:
        group = self._type_group_widgets.get(group_key)
        if group is None:
            return

        header, body, type_name, count, nearest_nm, type_block = group
        body.setMaximumHeight(0)
        body.setVisible(False)
        type_block.setMinimumHeight(_TYPE_HEADER_MIN_HEIGHT)
        type_block.setMaximumHeight(_TYPE_HEADER_MIN_HEIGHT)
        self._set_type_header_text(
            header,
            type_name=type_name,
            count=count,
            nearest_nm=nearest_nm,
            expanded=False,
        )

        if self._expanded_alert_key is not None and self._alert_parent_type.get(self._expanded_alert_key) == group_key:
            self._collapse_expanded_alert()

    def _expand_type_group(self, group_key: tuple[str, ...]) -> None:
        group = self._type_group_widgets.get(group_key)
        if group is None:
            return

        header, body, type_name, count, nearest_nm, type_block = group
        body.setMaximumHeight(16777215)
        body.setVisible(True)
        type_block.setMinimumHeight(0)
        type_block.setMaximumHeight(16777215)
        self._set_type_header_text(
            header,
            type_name=type_name,
            count=count,
            nearest_nm=nearest_nm,
            expanded=True,
        )

    def _toggle_type_group(self, group_key: tuple[str, ...]) -> None:
        if self._expanded_type_key == group_key:
            self._collapse_type_group(group_key)
            self._expanded_type_key = None
            self._resize_height()
            self._enforce_pinned_position()
            return

        self._collapse_expanded_alert()
        if self._expanded_type_key is not None:
            self._collapse_type_group(self._expanded_type_key)
        self._expand_type_group(group_key)
        self._expanded_type_key = group_key
        self._resize_height()
        self._enforce_pinned_position()

    def _toggle_alert_row(self, alert_key: tuple[str, ...], expand: bool) -> None:
        if not expand and self._expanded_alert_key == alert_key:
            self._collapse_expanded_alert()
            self._resize_height()
            self._enforce_pinned_position()
            return

        if expand:
            parent_type = self._alert_parent_type.get(alert_key)
            if parent_type is not None and self._expanded_type_key != parent_type:
                if self._expanded_type_key is not None:
                    self._collapse_type_group(self._expanded_type_key)
                self._expand_type_group(parent_type)
                self._expanded_type_key = parent_type

            self._collapse_expanded_alert()
            row = self._alert_row_widgets.get(alert_key)
            if row is None:
                return
            row.set_expanded(True)
            self._expanded_alert_key = alert_key
        self._resize_height()
        self._enforce_pinned_position()

    def _resize_height(self) -> None:
        self.setFixedWidth(_FIXED_WIDTH)
        anchor = self._pinned_top_left if self._pinned_top_left is not None else self.pos()
        current_x = anchor.x()
        current_y = anchor.y()
        locked_visible_height = None
        if self.isVisible() and self.height() > 0:
            if self._preferred_visible_height is not None:
                locked_visible_height = self._preferred_visible_height
            else:
                locked_visible_height = self.height()
        margins = self.layout().contentsMargins()
        content_width = _FIXED_WIDTH - margins.left() - margins.right()
        self._rows_widget.setFixedWidth(max(120, content_width))
        self._rows_layout.activate()
        self._rows_widget.adjustSize()
        self._rows_widget.updateGeometry()
        self.layout().invalidate()
        self.layout().activate()

        # Compute desired height from actual content so the overlay stays compact
        # for few rows and only scrolls when it exceeds the max screen ratio.
        screen = QGuiApplication.screenAt(anchor)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            max_height = 1000
        else:
            area = screen.availableGeometry()
            ratio_limit = int(area.height() * _MAX_HEIGHT_RATIO)
            # Cap by free space below current top so the window never needs to shift upward.
            space_below = max(120, area.bottom() - current_y + 1)
            max_height = min(ratio_limit, space_below)

        rows_height = max(self._rows_layout.sizeHint().height(), self._rows_widget.sizeHint().height())
        rows_height += _HEIGHT_SAFETY_PX
        self._rows_widget.resize(self._rows_widget.width(), rows_height)
        chrome_height = (
            margins.top()
            + margins.bottom()
            + self._title_bar.sizeHint().height()
            + self.layout().spacing()
        )

        if locked_visible_height is not None:
            # While visible, keep frame height fixed so expanding/collapsing
            # sections cannot shift the title bar position.
            final_height = min(max_height, max(_MIN_OVERLAY_HEIGHT, locked_visible_height))
            max_rows_height = max(_MIN_ROWS_VIEWPORT_HEIGHT, final_height - chrome_height - _HEIGHT_SAFETY_PX)
            needs_scroll = rows_height > max_rows_height
            self._rows_scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded if needs_scroll else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self._rows_scroll.setFixedHeight(max_rows_height)
        else:
            max_rows_height = max(_MIN_ROWS_VIEWPORT_HEIGHT, max_height - chrome_height)
            needs_scroll = rows_height > max_rows_height
            self._rows_scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded if needs_scroll else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            rows_viewport_height = max_rows_height if needs_scroll else max(_MIN_ROWS_VIEWPORT_HEIGHT, rows_height)
            self._rows_scroll.setFixedHeight(rows_viewport_height)

            final_height = chrome_height + self._rows_scroll.height() + _HEIGHT_SAFETY_PX
            final_height = min(max_height, final_height)
            final_height = max(_MIN_OVERLAY_HEIGHT, final_height)
            final_height = min(final_height, max_height)

        # Keep the overlay pinned to one top-left anchor and only resize.
        # Avoid setGeometry here because some window managers may nudge the
        # top-left while applying geometry constraints.
        self._allow_programmatic_move = True
        self.move(current_x, current_y)
        self.resize(_FIXED_WIDTH, final_height)
        self._allow_programmatic_move = False
        self._pinned_top_left = QPoint(current_x, current_y)
        if self.isVisible():
            self._preferred_visible_height = final_height

    def _enforce_pinned_position(self) -> None:
        if self._pinned_top_left is None:
            self._pinned_top_left = self.pos()

        def _snap() -> None:
            if self._pinned_top_left is None:
                return
            if self.pos() == self._pinned_top_left:
                return
            self._allow_programmatic_move = True
            self.move(self._pinned_top_left)
            self._allow_programmatic_move = False

        _snap()
        QTimer.singleShot(0, _snap)
        QTimer.singleShot(30, _snap)

    def _relayout(self) -> None:
        self._resize_height()

    # ── drag to move ───────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._is_in_bottom_resize_zone(event.position().toPoint()):
            self._is_resizing = True
            self._resize_start_global_y = event.globalPosition().toPoint().y()
            self._resize_start_height = self.height()
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            event.accept()
            return

        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._title_bar.geometry().contains(event.position().toPoint())
        ):
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        else:
            self._drag_pos = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._is_resizing and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint().y() - self._resize_start_global_y
            self._preferred_visible_height = max(_MIN_OVERLAY_HEIGHT, self._resize_start_height + delta)
            self._resize_height()
            event.accept()
            return

        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        else:
            self._update_resize_cursor(event.position().toPoint())
        super().mouseMoveEvent(event)

    def moveEvent(self, event) -> None:  # type: ignore[override]
        super().moveEvent(event)
        if self._allow_programmatic_move:
            return
        if self._drag_pos is not None:
            return
        if self._pinned_top_left is None:
            self._pinned_top_left = self.pos()
            return
        if self.pos() != self._pinned_top_left:
            self._allow_programmatic_move = True
            self.move(self._pinned_top_left)
            self._allow_programmatic_move = False

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._is_resizing:
            self._is_resizing = False
            self._preferred_visible_height = self.height()
            self.unsetCursor()
            event.accept()
            return

        if self._drag_pos is not None:
            self._pinned_top_left = self.pos()
        self._drag_pos = None
        self._update_resize_cursor(event.position().toPoint())
        super().mouseReleaseEvent(event)

    def _reposition_top_right(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        margin = 20
        x = area.x() + area.width() - self.width() - margin
        y = area.y() + margin
        self._allow_programmatic_move = True
        self.move(x, y)
        self._allow_programmatic_move = False
        self._pinned_top_left = QPoint(x, y)
