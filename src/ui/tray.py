"""
System tray icon and menu.

Double-click or "Show" opens the debug window.
The icon colour indicates connection state:
  grey  = not connected to MSFS
  green = connected, NOTAMs up to date
  amber = connected, fetching
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from src.scheduler import Scheduler
from src.ui.main_window import MainWindow
from src.ui.widgets.settings_window import SettingsWindow

_ICON_SIZE = 22
_ASSETS    = Path(__file__).resolve().parent.parent.parent / "assets"


def _make_icon(color: str) -> QIcon:
    """Generate a simple filled-circle tray icon in the given hex colour."""
    icon_path = _ASSETS / "icon.png"
    if icon_path.exists():
        return QIcon(str(icon_path))

    # Fallback: draw a coloured circle
    pix = QPixmap(_ICON_SIZE, _ICON_SIZE)
    pix.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(QColor("#ffffff"))
    painter.drawEllipse(2, 2, _ICON_SIZE - 4, _ICON_SIZE - 4)
    painter.end()
    return QIcon(pix)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, scheduler: Scheduler, parent=None) -> None:
        # Create icons after QApplication is instantiated
        self.icon_grey  = _make_icon("#666666")
        self.icon_green = _make_icon("#44cc44")
        self.icon_amber = _make_icon("#ffaa00")
        self.icon_red   = _make_icon("#ff4444")

        super().__init__(self.icon_grey, parent)
        self.scheduler = scheduler
        self.main_window = MainWindow(scheduler)
        self.settings_window = SettingsWindow(on_apply=self._apply_runtime_settings)
        self._startup_notice: QWidget | None = None
        self.main_window.alert_overlay.set_action_handlers(
            on_open_settings=self._show_settings,
            on_open_debug=self._show_window,
        )

        self._setup_menu()
        self._connect_signals()
        self.setToolTip("NOTAM Injector — not connected")

        # Keep app in tray by default; debug window opens on explicit user action.

    def show_startup_notice(self) -> None:
        if self._startup_notice is not None and self._startup_notice.isVisible():
            return

        notice = QWidget(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        notice.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        notice.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        notice.setStyleSheet(
            "QWidget {"
            "  background: rgba(28, 32, 40, 228);"
            "  border: 1px solid rgba(255, 255, 255, 32);"
            "  border-radius: 10px;"
            "}"
            "QLabel#title { color: #f3f6fb; font-size: 13px; font-weight: 700; }"
            "QLabel#body  { color: #c8d2e0; font-size: 11px; }"
        )

        layout = QVBoxLayout(notice)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        title = QLabel("NOTAM Injector is running")
        title.setObjectName("title")
        body = QLabel("The app is available in your system tray.")
        body.setObjectName("body")
        layout.addWidget(title)
        layout.addWidget(body)

        notice.adjustSize()
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = area.x() + (area.width() - notice.width()) // 2
            y = area.y() + (area.height() - notice.height()) // 2
            notice.move(x, y)

        self._startup_notice = notice
        notice.show()
        notice.raise_()
        QTimer.singleShot(3200, self._hide_startup_notice)

    def _hide_startup_notice(self) -> None:
        if self._startup_notice is None:
            return
        self._startup_notice.close()
        self._startup_notice.deleteLater()
        self._startup_notice = None

    def _setup_menu(self) -> None:
        menu = QMenu()

        self._status_action = menu.addAction("Not connected")
        self._status_action.setEnabled(False)
        menu.addSeparator()

        windows_menu = menu.addMenu("NOTAMS")

        self._toggle_overlay_action = windows_menu.addAction("Show NOTAM alerts")
        self._toggle_overlay_action.triggered.connect(self._toggle_overlay)

        settings_menu = menu.addMenu("Settings")

        settings_action = settings_menu.addAction("Open settings")
        settings_action.triggered.connect(self._show_settings)

        show_action = settings_menu.addAction("Show Debug Window")
        show_action.triggered.connect(self._show_window)

        menu.addSeparator()

        restart_action = menu.addAction("Restart")
        restart_action.triggered.connect(self._restart_application)

        exit_action = menu.addAction("Exit")
        exit_action.triggered.connect(self._exit_application)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _connect_signals(self) -> None:
        self.scheduler.sim_status.connect(self._on_sim_status)
        self.scheduler.notams_updated.connect(self._on_notams_updated)
        self.scheduler.position_updated.connect(self._on_position)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self) -> None:
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def _show_settings(self) -> None:
        overlay = self.main_window.alert_overlay
        anchor = overlay if overlay.isVisible() else None
        self.settings_window.show_near_top_right(anchor=anchor)

    def _apply_runtime_settings(self, applied: dict[str, object]) -> None:
        # Apply settings that are safe to update in the running process.
        connector = self.scheduler.connector
        connector.poll_interval_s = int(applied.get("position_poll_interval_s", connector.poll_interval_s))
        connector.min_move_nm = float(applied.get("min_move_nm", connector.min_move_nm))

        opacity = float(applied.get("alert_window_opacity", 1.0))
        opacity = max(0.1, min(1.0, opacity))
        self.main_window.alert_overlay.setWindowOpacity(opacity)

    def _restart_application(self) -> None:
        self._clean_shutdown()
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, *sys.argv[1:]]
        else:
            cmd = [sys.executable, *sys.argv]

        try:
            # Detached launch avoids terminal/session coupling and gives a truly fresh app instance.
            if getattr(sys, "frozen", False):
                started = QProcess.startDetached(cmd[0], cmd[1:], str(Path.cwd()))
            else:
                # Keep development mode launch behavior identical to manual `python main.py`.
                started = QProcess.startDetached(cmd[0], cmd[1:], str(Path.cwd()))
            if not started:
                raise RuntimeError("detached process launch failed")
            QApplication.quit()
        except Exception as exc:
            self._status_action.setText(f"Restart failed: {exc}")
            # If restart failed, keep app alive by reconnecting state where possible.

    def _exit_application(self) -> None:
        self._clean_shutdown()
        QApplication.quit()

    def _clean_shutdown(self) -> None:
        self.scheduler.stop()
        self.hide()
        self.settings_window.hide()
        self.main_window.hide()

    def _toggle_overlay(self) -> None:
        overlay = self.main_window.alert_overlay
        if overlay.isVisible():
            overlay.hide()
            self._toggle_overlay_action.setText("Show NOTAM alerts")
        else:
            overlay.show()
            overlay.raise_()
            self._toggle_overlay_action.setText("Hide NOTAM alerts")

    def _on_sim_status(self, msg: str) -> None:
        self._status_action.setText(msg)
        if "connected" in msg.lower() and "dis" not in msg.lower():
            self.setIcon(self.icon_green)
            self.setToolTip(f"NOTAM Injector — {msg}")
        elif "mock" in msg.lower():
            self.setIcon(self.icon_amber)
            self.setToolTip(f"NOTAM Injector — Mock mode")
        else:
            self.setIcon(self.icon_grey)
            self.setToolTip(f"NOTAM Injector — {msg}")

    def _on_notams_updated(self, notams: list) -> None:
        active = [n for n in notams if n.is_active]
        self.setToolTip(f"NOTAM Injector — {len(active)} active NOTAM(s)")
        self.setIcon(self.icon_green)

    def _on_position(self, lat: float, lon: float, alt: float) -> None:
        self.setIcon(self.icon_amber)   # brief amber flash while fetching
        # Reset to green after 3 s (fetch should be done by then)
        QTimer.singleShot(3000, lambda: self.setIcon(self.icon_green))
