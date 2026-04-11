"""
System tray icon and menu.

Double-click or "Show" opens the debug window.
The icon colour indicates connection state:
  grey  = not connected to MSFS
  green = connected, NOTAMs up to date
  amber = connected, fetching
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from src.scheduler import Scheduler
from src.ui.main_window import MainWindow

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


ICON_GREY  = _make_icon("#666666")
ICON_GREEN = _make_icon("#44cc44")
ICON_AMBER = _make_icon("#ffaa00")
ICON_RED   = _make_icon("#ff4444")


class TrayIcon(QSystemTrayIcon):
    def __init__(self, scheduler: Scheduler, parent=None) -> None:
        super().__init__(ICON_GREY, parent)
        self.scheduler   = scheduler
        self.main_window = MainWindow(scheduler)

        self._setup_menu()
        self._connect_signals()
        self.setToolTip("NOTAM Injector — not connected")

    def _setup_menu(self) -> None:
        menu = QMenu()

        self._status_action = menu.addAction("Not connected")
        self._status_action.setEnabled(False)
        menu.addSeparator()

        show_action = menu.addAction("Show debug window")
        show_action.triggered.connect(self._show_window)

        menu.addSeparator()

        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(QApplication.quit)

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

    def _on_sim_status(self, msg: str) -> None:
        self._status_action.setText(msg)
        if "connected" in msg.lower() and "dis" not in msg.lower():
            self.setIcon(ICON_GREEN)
            self.setToolTip(f"NOTAM Injector — {msg}")
        elif "mock" in msg.lower():
            self.setIcon(ICON_AMBER)
            self.setToolTip(f"NOTAM Injector — Mock mode")
        else:
            self.setIcon(ICON_GREY)
            self.setToolTip(f"NOTAM Injector — {msg}")

    def _on_notams_updated(self, notams: list) -> None:
        active = [n for n in notams if n.is_active]
        self.setToolTip(f"NOTAM Injector — {len(active)} active NOTAM(s)")
        self.setIcon(ICON_GREEN)

    def _on_position(self, lat: float, lon: float, alt: float) -> None:
        self.setIcon(ICON_AMBER)   # brief amber flash while fetching
        # Reset to green after 3 s (fetch should be done by then)
        QTimer.singleShot(3000, lambda: self.setIcon(ICON_GREEN))
