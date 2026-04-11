"""
Main debug window — shown/hidden from the system tray.
Closes to tray instead of quitting the app.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.scheduler import Scheduler
from src.ui.widgets.actions_panel import ActionsPanel
from src.ui.widgets.log_panel import LogPanel
from src.ui.widgets.notam_table import NotamTable


class MainWindow(QMainWindow):
    def __init__(self, scheduler: Scheduler, parent=None) -> None:
        super().__init__(parent)
        self.scheduler = scheduler
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        self.setWindowTitle("NOTAM Injector — Debug")
        self.resize(1100, 650)
        self.setStyleSheet("QMainWindow { background:#1a1a1a; } QTabBar::tab { min-width:100px; }")

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)

        # ── Header ────────────────────────────────────────────────────────────
        self._status_label = QLabel("Connecting to MSFS…")
        self._status_label.setStyleSheet("color:#aaa; font-size:11px;")
        layout.addWidget(self._status_label)

        self._position_label = QLabel("Position: —")
        self._position_label.setStyleSheet("color:#ddd; font-size:11px;")
        layout.addWidget(self._position_label)

        # ── Tabs ──────────────────────────────────────────────────────────────
        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane { border:1px solid #333; }"
            "QTabBar::tab { background:#2d2d2d; color:#aaa; padding:6px 14px; }"
            "QTabBar::tab:selected { background:#1e1e1e; color:#fff; }"
        )
        layout.addWidget(tabs)

        self.notam_table  = NotamTable()
        self.actions_panel = ActionsPanel()
        self.log_panel    = LogPanel()

        tabs.addTab(self.notam_table,   "NOTAMs")
        tabs.addTab(self.actions_panel, "MSFS Actions")
        tabs.addTab(self.log_panel,     "Log")

        # ── Status bar ────────────────────────────────────────────────────────
        self._statusbar = QStatusBar()
        self._statusbar.setStyleSheet("color:#888; font-size:10px;")
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage("Ready")

    def _connect_signals(self) -> None:
        self.scheduler.notams_updated.connect(self.notam_table.update_notams)
        self.scheduler.actions_updated.connect(self.actions_panel.update_actions)
        self.scheduler.position_updated.connect(self._on_position)
        self.scheduler.sim_status.connect(self._on_sim_status)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_position(self, lat: float, lon: float, alt: float) -> None:
        self._position_label.setText(
            f"Position:  {lat:+.4f}°  {lon:+.4f}°  {alt:,.0f} ft"
        )

    def _on_sim_status(self, msg: str) -> None:
        self._status_label.setText(f"Sim: {msg}")
        self._statusbar.showMessage(msg, 5000)

    # ── Close to tray ─────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()
