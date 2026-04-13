"""
Main debug window — shown/hidden from the system tray.
Closes to tray instead of quitting the app.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QProgressBar,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.config import settings
from src.scheduler import Scheduler
from src.ui.widgets.actions_panel import ActionsPanel
from src.ui.widgets.alert_overlay import AlertOverlay
from src.ui.widgets.log_panel import LogPanel
from src.ui.widgets.notam_table import NotamTable


class MainWindow(QMainWindow):
    def __init__(self, scheduler: Scheduler, parent=None) -> None:
        super().__init__(parent)
        self.scheduler = scheduler
        self._fetch_hide_timer = QTimer(self)
        self._fetch_hide_timer.setSingleShot(True)
        self._fetch_hide_timer.timeout.connect(self._hide_fetch_bar)
        self.alert_overlay = AlertOverlay(self.scheduler.airport_lookup)
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

        self._simconnect_label = QLabel("SimConnect: checking...")
        self._simconnect_label.setStyleSheet("color:#aaa; font-size:11px;")
        layout.addWidget(self._simconnect_label)

        # Slim fetch-progress bar — visible only while NOTAMs are being fetched
        self._fetch_bar = QProgressBar()
        self._fetch_bar.setTextVisible(True)
        self._fetch_bar.setFormat("Fetching NOTAMs… %v / %m airports")
        self._fetch_bar.setMaximumHeight(14)
        self._fetch_bar.setStyleSheet(
            "QProgressBar { background:#2a2a2a; border:1px solid #444; border-radius:3px; color:#ccc; font-size:10px; }"
            "QProgressBar::chunk { background:#2266cc; border-radius:2px; }"
        )
        self._fetch_bar.setVisible(False)
        layout.addWidget(self._fetch_bar)

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
        self.scheduler.position_updated.connect(self.notam_table.update_position)
        self.scheduler.position_updated.connect(self.actions_panel.update_position)
        self.scheduler.position_sampled.connect(self.alert_overlay.update_reference_position)
        self.scheduler.sim_status.connect(self._on_sim_status)
        self.scheduler.fetch_progress.connect(self._on_fetch_progress)
        self.scheduler.poll_progress.connect(self.alert_overlay.update_poll_progress)
        self.scheduler.alert_overlay_batch.connect(self.alert_overlay.replace_alerts)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_position(self, lat: float, lon: float, alt: float) -> None:
        self._position_label.setText(
            f"Position:  {lat:+.4f}°  {lon:+.4f}°  {alt:,.0f} ft"
        )

    def _on_sim_status(self, msg: str) -> None:
        self._status_label.setText(f"Sim: {msg}")
        self._statusbar.showMessage(msg, 5000)
        text = msg.lower()
        if text.startswith("simconnect connected"):
            self._simconnect_label.setText("SimConnect: available")
        elif "mock mode" in text or "unavailable" in text:
            self._simconnect_label.setText("SimConnect: unavailable (mock mode)")
        elif text.startswith("simconnect disconnected") or text.startswith("disconnected"):
            self._simconnect_label.setText("SimConnect: disconnected")
            self.alert_overlay.clear_and_hide()  # no active flight — clear and hide
        else:
            self._simconnect_label.setText(f"SimConnect: {msg}")

    def _on_fetch_progress(self, done: int, total: int) -> None:
        if total == 0:
            self._fetch_bar.setVisible(False)
            return
        if self._fetch_hide_timer.isActive():
            self._fetch_hide_timer.stop()
        self._fetch_bar.setMaximum(total)
        self._fetch_bar.setValue(done)
        self._fetch_bar.setVisible(True)
        if done >= total:
            # Keep full bar briefly so completion is visible to the user.
            self._fetch_hide_timer.start(2000)

    def _hide_fetch_bar(self) -> None:
        self._fetch_bar.setVisible(False)


    # ── Close to tray ─────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()
