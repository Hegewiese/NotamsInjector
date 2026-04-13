"""
Startup status dialog for MSFS readiness.

Shown as a standalone, closeable window while waiting for a valid aircraft
position from MSFS. It can be dismissed by the user and is hidden automatically
once coordinates are received.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


_CRANE_LIBRARY_URL = "https://de.flightsim.to/addon/29221/construction-crane-library"


class MsfsStartupDialog(QWidget):
    dismissed = Signal()
    do_not_show_again_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._has_centered_once = False
        self._sim_status = QLabel("")
        self._position_status = QLabel("")
        self._package_status = QLabel("")
        self._hint = QLabel(
            "Waiting for simulator startup and in-flight position data."
        )
        self._download_btn = QPushButton("Download Construction Assets")
        self._do_not_show_again = QCheckBox("Do not show again")
        self._build_ui()

    def _build_ui(self) -> None:
        self.setWindowTitle("MSFS Status")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        title = QLabel("MSFS status")
        title.setStyleSheet("color:#f3f6fb; font-size:13px; font-weight:700;")

        self._sim_status.setWordWrap(True)
        self._sim_status.setStyleSheet("color:#d6deea; font-size:11px;")

        self._position_status.setWordWrap(True)
        self._position_status.setStyleSheet("color:#d6deea; font-size:11px;")

        self._package_status.setWordWrap(True)
        self._package_status.setStyleSheet("color:#d6deea; font-size:11px;")

        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color:#9db0c7; font-size:10px;")
        self._hint.setTextFormat(Qt.TextFormat.RichText)
        self._hint.setOpenExternalLinks(True)

        self._do_not_show_again.setStyleSheet("color:#d6deea; font-size:11px;")
        self._do_not_show_again.toggled.connect(self.do_not_show_again_changed.emit)

        self._download_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._download_btn.clicked.connect(self._open_crane_library_page)
        self._download_btn.hide()

        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.addStretch()
        button_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(self._sim_status)
        layout.addWidget(self._position_status)
        layout.addWidget(self._package_status)
        layout.addWidget(self._hint)
        layout.addWidget(self._download_btn)
        layout.addWidget(self._do_not_show_again)
        layout.addLayout(button_row)

        self.setStyleSheet(
            "MsfsStartupDialog {"
            "  background-color: #2f3238;"
            "  border: 2px solid #6c7684;"
            "  border-radius: 10px;"
            "}"
            "QPushButton {"
            "  background-color: #4a4a4a; color: #f0f0f0;"
            "  border: 1px solid #777; border-radius: 6px;"
            "  padding: 4px 12px; font-size: 12px;"
            "}"
            "QPushButton:hover { background-color: #606060; }"
            "QPushButton:pressed { background-color: #303030; }"
        )

        self.setFixedWidth(430)
        self.adjustSize()

    def set_state(
        self,
        simulator_running: bool,
        position_ready: bool,
        construction_assets_available: bool,
    ) -> None:
        sim_icon = "\u2713" if simulator_running else "\u2717"
        pos_icon = "\u2713" if position_ready else "\u2717"
        pkg_icon = "\u2713" if construction_assets_available else "\u2717"
        sim_color = "#66dd88" if simulator_running else "#ff7d7d"
        pos_color = "#66dd88" if position_ready else "#ff7d7d"
        pkg_color = "#66dd88" if construction_assets_available else "#ffb870"

        self._sim_status.setText(
            f"<span style='color:{sim_color}; font-weight:700'>{sim_icon}</span> "
            f"Simulator running"
        )
        self._position_status.setText(
            f"<span style='color:{pos_color}; font-weight:700'>{pos_icon}</span> "
            f"Flight loaded and valid position received"
        )
        self._package_status.setText(
            f"<span style='color:{pkg_color}; font-weight:700'>{pkg_icon}</span> "
            f"Construction Assets package installed (for crane obstacles)"
        )

        if construction_assets_available:
            self._hint.setText("Waiting for simulator startup and in-flight position data.")
            self._download_btn.hide()
        else:
            self._hint.setText(
                "Construction crane library is missing. Please download and install "
                f"<a href=\"{_CRANE_LIBRARY_URL}\" style=\"color:#9ecbff; text-decoration: underline;\">"
                "Construction Assets</a> in your Community folder."
            )
            self._download_btn.show()

    def show_state(
        self,
        simulator_running: bool,
        position_ready: bool,
        construction_assets_available: bool,
    ) -> None:
        self.set_state(simulator_running, position_ready, construction_assets_available)
        if not self.isVisible() and not self._has_centered_once:
            self._show_centered()
            self._has_centered_once = True
        self.show()
        self.raise_()

    def set_do_not_show_again(self, value: bool) -> None:
        self._do_not_show_again.blockSignals(True)
        self._do_not_show_again.setChecked(value)
        self._do_not_show_again.blockSignals(False)

    def _show_centered(self) -> None:
        self.adjustSize()
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = area.x() + (area.width() - self.width()) // 2
        y = area.y() + (area.height() - self.height()) // 2
        self.move(x, y)

    @staticmethod
    def _open_crane_library_page() -> None:
        QDesktopServices.openUrl(QUrl(_CRANE_LIBRARY_URL))

    def closeEvent(self, event) -> None:  # type: ignore[override]
        super().closeEvent(event)
        self.dismissed.emit()
