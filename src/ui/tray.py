"""
System tray icon and menu.

Double-click or "Show" opens the debug window.
The icon colour indicates connection state:
  grey  = not connected to MSFS
  green = connected, NOTAMs up to date
  amber = connected, fetching
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from src.config import Settings, settings
from src.scheduler import Scheduler
from src.ui.main_window import MainWindow
from src.ui.widgets.msfs_startup_dialog import MsfsStartupDialog
from src.ui.widgets.settings_window import SettingsWindow

_ICON_SIZE = 22
_ASSETS    = Path(__file__).resolve().parent.parent.parent / "assets"
_CRANE_PACKAGE_FOLDER = "chrispiaviation_construction_assets"
_CRANE_PACKAGE_TITLE = "Construction Assets"
_CRANE_PACKAGE_CREATOR = "ChrisPiAviation"


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
        self._msfs_startup_dialog = MsfsStartupDialog()
        self._startup_dialog_dismissed = False
        self._startup_position_received = False
        self._simulator_running = False
        self._construction_assets_available: bool = False
        self._next_construction_assets_check_ts = 0.0
        self._last_sim_status_msg = ""
        self._startup_state_timer = QTimer(self)
        self._startup_state_timer.setInterval(2000)
        self._startup_state_timer.timeout.connect(self._refresh_startup_state)
        self._startup_state_timer.start()
        self._startup_notice: QWidget | None = None
        self._msfs_startup_dialog.dismissed.connect(self._on_startup_dialog_dismissed)
        self._msfs_startup_dialog.do_not_show_again_changed.connect(
            self._on_do_not_show_again_changed
        )
        self._msfs_startup_dialog.set_do_not_show_again(
            not bool(settings.msfs_status_dialog_enabled)
        )
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
        notice.setStyleSheet(
            "QWidget {"
            "  background: #1c2028;"
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

        self._toggle_msfs_status_action = settings_menu.addAction("Show MSFS Status Window")
        self._toggle_msfs_status_action.triggered.connect(self._toggle_msfs_status_window)
        settings_menu.aboutToShow.connect(self._update_msfs_status_action_text)

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

    def show_msfs_startup_dialog(self) -> None:
        """Show startup guidance immediately while waiting for live position data."""
        if not settings.msfs_status_dialog_enabled:
            return
        if self._startup_position_received or self._startup_dialog_dismissed:
            return
        self._simulator_running = self._detect_simulator_process()
        self._construction_assets_available = self._is_construction_assets_package_available()
        self._msfs_startup_dialog.show_state(
            simulator_running=self._simulator_running,
            position_ready=self._startup_position_received,
            construction_assets_available=self._construction_assets_available,
        )

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

        if "msfs_status_dialog_enabled" in applied:
            enabled = bool(applied["msfs_status_dialog_enabled"])
            self._msfs_startup_dialog.set_do_not_show_again(not enabled)
            if not enabled:
                self._msfs_startup_dialog.hide()
            else:
                self._refresh_startup_state()

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
        self._startup_state_timer.stop()
        self.hide()
        self._msfs_startup_dialog.hide()
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

    def _toggle_msfs_status_window(self) -> None:
        if self._msfs_startup_dialog.isVisible():
            self._msfs_startup_dialog.hide()
        else:
            self._startup_dialog_dismissed = False
            self._simulator_running = self._detect_simulator_process()
            self._construction_assets_available = self._is_construction_assets_package_available(force=True)
            self._msfs_startup_dialog.show_state(
                simulator_running=self._simulator_running,
                position_ready=self._startup_position_received,
                construction_assets_available=self._construction_assets_available,
            )
        self._update_msfs_status_action_text()

    def _update_msfs_status_action_text(self) -> None:
        if self._msfs_startup_dialog.isVisible():
            self._toggle_msfs_status_action.setText("Hide MSFS Status Window")
        else:
            self._toggle_msfs_status_action.setText("Show MSFS Status Window")

    def _on_sim_status(self, msg: str) -> None:
        self._last_sim_status_msg = msg
        self._simulator_running = self._detect_simulator_process()
        self._status_action.setText(msg)
        text = msg.lower()
        if "connected" in text and "dis" not in text and self._simulator_running:
            self.setIcon(self.icon_green)
            self.setToolTip(f"NOTAM Injector — {msg}")
        elif "mock" in text:
            self.setIcon(self.icon_amber)
            self.setToolTip(f"NOTAM Injector — Mock mode")
            if not self._startup_position_received:
                self._msfs_startup_dialog.hide()
        else:
            self.setIcon(self.icon_grey)
            if "connected" in text and not self._simulator_running:
                self._status_action.setText("SimConnect connected, but simulator process not detected")
                self.setToolTip("NOTAM Injector — Simulator process not detected")
            else:
                self.setToolTip(f"NOTAM Injector — {msg}")

        self._refresh_startup_state()

    def _on_notams_updated(self, notams: list) -> None:
        active = [n for n in notams if n.is_active]
        self.setToolTip(f"NOTAM Injector — {len(active)} active NOTAM(s)")
        self.setIcon(self.icon_green)

    def _on_position(self, lat: float, lon: float, alt: float) -> None:
        self._simulator_running = self._detect_simulator_process()
        self._startup_position_received = self._is_valid_flight_position(lat, lon)
        if self._startup_position_received:
            self._msfs_startup_dialog.hide()
        else:
            self._refresh_startup_state()
        self.setIcon(self.icon_amber)   # brief amber flash while fetching
        # Reset to green after 3 s (fetch should be done by then)
        QTimer.singleShot(3000, lambda: self.setIcon(self.icon_green))

    def _on_startup_dialog_dismissed(self) -> None:
        self._startup_dialog_dismissed = True

    def _is_valid_flight_position(self, lat: float, lon: float) -> bool:
        # Treat known placeholder/default coordinates as pre-flight state.
        if abs(lat) < 0.01 and abs(lon) < 0.01:
            return False
        if abs(lat) < 0.01 and abs(lon - 90.0) < 0.5:
            return False
        return True

    def _detect_simulator_process(self) -> bool:
        if sys.platform != "win32":
            return self.scheduler.connector.is_connected

        process_names = (
            "FlightSimulator.exe",
            "FlightSimulator2024.exe",
        )
        for process_name in process_names:
            try:
                res = subprocess.run(
                    [
                        "tasklist",
                        "/FI",
                        f"IMAGENAME eq {process_name}",
                        "/FO",
                        "CSV",
                        "/NH",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except Exception:
                continue

            out = (res.stdout or "").lower()
            if process_name.lower() in out:
                return True

        return False

    def _refresh_startup_state(self) -> None:
        self._simulator_running = self._detect_simulator_process()
        self._construction_assets_available = self._is_construction_assets_package_available()
        if self._startup_position_received:
            self._msfs_startup_dialog.hide()
            return
        if not settings.msfs_status_dialog_enabled:
            self._msfs_startup_dialog.hide()
            return
        if self._startup_dialog_dismissed:
            return
        self._msfs_startup_dialog.show_state(
            simulator_running=self._simulator_running,
            position_ready=self._startup_position_received,
            construction_assets_available=self._construction_assets_available,
        )

    def _is_construction_assets_package_available(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now < self._next_construction_assets_check_ts:
            return self._construction_assets_available

        self._next_construction_assets_check_ts = now + 60.0
        for packages_root in self._candidate_msfs_package_roots():
            community_dir = packages_root / "Community"
            package_dir = community_dir / _CRANE_PACKAGE_FOLDER
            if not package_dir.exists() or not package_dir.is_dir():
                continue

            manifest_path = package_dir / "manifest.json"
            if not manifest_path.exists():
                return True

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="ignore"))
                title = str(manifest.get("title", "")).strip()
                creator = str(manifest.get("creator", "")).strip()
                if title.lower() == _CRANE_PACKAGE_TITLE.lower() and creator.lower() == _CRANE_PACKAGE_CREATOR.lower():
                    return True
                # Folder is present even if metadata differs; keep as installed.
                return True
            except Exception:
                return True

        return False

    def _candidate_msfs_package_roots(self) -> list[Path]:
        roots: list[Path] = []

        user_cfg = self._find_msfs_usercfg_opt()
        if user_cfg is not None:
            installed_path = self._read_installed_packages_path(user_cfg)
            if installed_path is not None:
                roots.append(installed_path)

        local_appdata = Path(os.environ.get("LOCALAPPDATA", ""))
        appdata = Path(os.environ.get("APPDATA", ""))
        userprofile = Path(os.environ.get("USERPROFILE", ""))

        roots.extend(
            [
                local_appdata / "Packages" / "Microsoft.Limitless_8wekyb3d8bbwe" / "LocalCache" / "Packages",
                local_appdata / "Packages" / "Microsoft.FlightSimulator_8wekyb3d8bbwe" / "LocalCache" / "Packages",
                appdata / "Microsoft Flight Simulator" / "Packages",
                userprofile / "AppData" / "Roaming" / "Microsoft Flight Simulator" / "Packages",
                Path("C:/XboxGames/Microsoft Flight Simulator/Content"),
                Path("D:/XboxGames/Microsoft Flight Simulator/Content"),
            ]
        )

        dedup: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            if root.exists() and root.is_dir():
                dedup.append(root)
        return dedup

    @staticmethod
    def _find_msfs_usercfg_opt() -> Path | None:
        local_appdata = Path(os.environ.get("LOCALAPPDATA", ""))
        appdata = Path(os.environ.get("APPDATA", ""))
        userprofile = Path(os.environ.get("USERPROFILE", ""))

        candidates = [
            local_appdata / "Packages" / "Microsoft.Limitless_8wekyb3d8bbwe" / "LocalCache" / "UserCfg.opt",
            local_appdata / "Packages" / "Microsoft.FlightSimulator_8wekyb3d8bbwe" / "LocalCache" / "UserCfg.opt",
            appdata / "Microsoft Flight Simulator" / "UserCfg.opt",
            userprofile / "AppData" / "Roaming" / "Microsoft Flight Simulator" / "UserCfg.opt",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _read_installed_packages_path(user_cfg_path: Path) -> Path | None:
        try:
            text = user_cfg_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

        match = re.search(r'InstalledPackagesPath\s+"([^"]+)"', text, flags=re.IGNORECASE)
        if match is None:
            return None
        path_text = match.group(1).strip()
        if not path_text:
            return None
        expanded = os.path.expandvars(path_text)
        return Path(expanded)

    def _on_do_not_show_again_changed(self, checked: bool) -> None:
        enabled = not checked
        self._set_msfs_status_dialog_enabled(enabled)
        if not enabled:
            self._msfs_startup_dialog.hide()
        else:
            self._startup_dialog_dismissed = False
            self._refresh_startup_state()

    def _set_msfs_status_dialog_enabled(self, enabled: bool) -> None:
        if bool(settings.msfs_status_dialog_enabled) == enabled:
            return
        setattr(settings, "msfs_status_dialog_enabled", enabled)
        values = settings.model_dump()
        self._persist_settings_yaml(values)

    @staticmethod
    def _persist_settings_yaml(values: dict[str, object]) -> None:
        config_path = Path(__file__).resolve().parents[3] / "config.yaml"
        lines = [
            "# NOTAM Injector configuration",
            "# Generated by Settings window",
            "",
        ]
        for key in Settings.model_fields:
            value = values[key]
            lines.append(f"{key}: {TrayIcon._yaml_scalar(value)}")
        lines.append("")
        config_path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _yaml_scalar(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value).replace('"', '\\"')
        return f'"{text}"'
