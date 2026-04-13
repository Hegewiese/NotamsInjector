"""
Config editor window shown from tray menu.

Uses the same visual language as the NOTAM alert overlay.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import sys

from src.config import Settings, settings

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config.yaml"
_FIXED_WIDTH = 560
_HIGHLIGHT_DEPENDENT_KEYS = (
    "highlight_beacon_base_ft",
    "highlight_beacon_step_ft",
    "highlight_beacon_count",
)


@dataclass(frozen=True)
class _FieldSpec:
    key: str
    label: str
    group: str
    control: str
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    decimals: int = 0
    options: tuple[str, ...] = ()
    restart_required: bool = False
    tooltip: str = ""


_GROUP_ORDER = (
    "Notams Fetching",
    "MSFS Actions",
    "Notam Alert Window",
    "Logging",
)

_FIELD_SPECS: tuple[_FieldSpec, ...] = (
    _FieldSpec("position_poll_interval_s", "Position Poll Interval (s)", "Notams Fetching", "combo_int",
        options=("5", "10", "20", "30", "45", "60"),
        tooltip="How often (in seconds) the app reads your aircraft position from MSFS via SimConnect.\nLower values give more responsive NOTAM updates but use slightly more CPU."),
    _FieldSpec("min_move_nm", "Minimum Move (nm)", "Notams Fetching", "slider_float",
        minimum=2.0, maximum=10.0, step=1.0, decimals=1,
        tooltip="The minimum distance (nautical miles) you must travel before a new NOTAM fetch is triggered by movement.\nPrevents unnecessary re-fetches during minor position jitter on the ground."),
    _FieldSpec("notam_radius_nm", "Fetch Radius (nm)", "Notams Fetching", "slider_float",
        minimum=25.0, maximum=75.0, step=1.0, decimals=1,
        tooltip="The radius around your aircraft (nautical miles) used to find nearby airports whose NOTAMs should be fetched.\nLarger values show more NOTAMs but increase network load."),
    _FieldSpec("max_notam_age_h", "Max NOTAM Age (h)", "Notams Fetching", "slider_float",
        minimum=1.0, maximum=168.0, step=1.0, decimals=0,
        tooltip="NOTAMs older than this value (in hours) are ignored even if still technically active.\nHelps filter out very old NOTAMs that are unlikely to be relevant to your flight."),
    _FieldSpec("notam_movement_refetch_cooldown_s", "Movement Refetch Cooldown (s)", "Notams Fetching", "slider_float",
        minimum=0.0, maximum=7200.0, step=30.0, decimals=0,
        tooltip="Minimum time (in seconds) that must pass between two movement-triggered fetches.\nPrevents fetch storms if you are moving continuously; set to 0 to disable the cooldown."),

    _FieldSpec("auto_apply_notams", "Auto Apply NOTAM Actions", "MSFS Actions", "bool",
        tooltip="When enabled, NOTAM actions (placing obstacle objects, beacon markers, etc.) are applied automatically each fetch cycle without requiring manual confirmation."),
    _FieldSpec("obstacle_placement_radius_nm", "Obstacle Placement Radius (nm)", "MSFS Actions", "slider_float",
        minimum=5.0, maximum=30.0, step=1.0, decimals=1,
        tooltip="Maximum distance from the affected airport (nautical miles) within which obstacle SimObjects are placed in MSFS.\nNOTAMs for obstacles outside this radius are noted but not injected into the sim."),
    _FieldSpec("highlight_obstacle_objects", "Highlight Obstacle Objects", "MSFS Actions", "bool",
        tooltip="When enabled, obstacle objects placed in MSFS are visually highlighted with a set of vertical beacon markers to make them easy to spot from the cockpit."),
    _FieldSpec("highlight_beacon_base_ft", "Beacon Base Altitude (ft MSL)", "MSFS Actions", "slider_float",
        minimum=0.0, maximum=10000.0, step=100.0, decimals=0,
        tooltip="The altitude (feet MSL) at which the lowest beacon marker of an obstacle highlight stack is placed.\nSet this at or just above ground level for the area to keep beacons visible."),
    _FieldSpec("highlight_beacon_step_ft", "Beacon Vertical Step (ft)", "MSFS Actions", "slider_float",
        minimum=50.0, maximum=5000.0, step=50.0, decimals=0,
        tooltip="Vertical distance (feet) between consecutive beacon markers in an obstacle highlight stack.\nSmaller steps produce a denser column; larger steps spread them further apart."),
    _FieldSpec("highlight_beacon_count", "Beacon Count", "MSFS Actions", "slider_float",
        minimum=1.0, maximum=20.0, step=1.0, decimals=0,
        tooltip="Number of beacon marker objects stacked vertically above each highlighted obstacle.\nMore beacons make the obstacle more visible but also add more SimObjects to the sim."),
    _FieldSpec("msfs_status_dialog_enabled", "Show MSFS Status Dialog", "MSFS Actions", "bool",
        tooltip="When enabled, a startup status dialog is shown until MSFS is running and a valid in-flight position is received.\nDisable this if you never want to see the startup dialog."),

    _FieldSpec("notam_alert_enabled", "Enable In-Sim NOTAM Popups", "Notam Alert Window", "bool",
        tooltip="When enabled, approaching NOTAMs trigger a text notification inside MSFS via SimConnect.\nUseful for heads-up alerts without switching to the overlay window."),
    _FieldSpec("notam_alert_radius_nm", "Alert Radius (nm)", "Notam Alert Window", "slider_float",
        minimum=10.0, maximum=75.0, step=1.0, decimals=1,
        tooltip="Distance (nautical miles) from a NOTAM location at which an in-sim alert is triggered.\nSmaller values only alert when you are very close; larger values give earlier warnings."),
    _FieldSpec("alert_window_opacity", "Alert Window Opacity", "Notam Alert Window", "slider_float",
        minimum=0.1, maximum=1.0, step=0.01, decimals=2,
        tooltip="Transparency of the NOTAM Alerts overlay window (1.0 = fully opaque, 0.1 = nearly invisible).\nLower opacity lets you see the sim through the window during flight."),

    _FieldSpec("log_level", "Log Level", "Logging", "combo_str",
        options=("DEBUG", "INFO", "WARNING", "ERROR"), restart_required=True,
        tooltip="Verbosity of log output. DEBUG logs everything; INFO logs normal operations; WARNING/ERROR log only problems.\nChanging this requires a restart to take effect."),
    _FieldSpec("log_file", "Log File", "Logging", "line",
        restart_required=True,
        tooltip="Path to the log file where application messages are written.\nLeave empty to disable file logging. Requires a restart to take effect."),
)


class SettingsWindow(QWidget):
    def __init__(self, on_apply: Optional[Callable[[dict[str, Any]], None]] = None, parent=None) -> None:
        super().__init__(parent)
        self._drag_pos: QPoint | None = None
        self._on_apply = on_apply
        self._field_widgets: dict[str, QWidget] = {}
        self._slider_labels: dict[str, QLabel] = {}

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )

        self._title_bar = QWidget()
        self._title_bar.setStyleSheet("background: transparent; border: none;")
        title_layout = QHBoxLayout(self._title_bar)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(4)

        title_lbl = QLabel("Settings")
        title_lbl.setStyleSheet(
            "color:#ffffff; font-size:13px; font-weight:700;"
            " background: transparent; border: none;"
        )
        title_layout.addWidget(title_lbl)
        title_layout.addStretch()

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

        self._form_widget = QWidget()
        self._form_root_layout = QVBoxLayout(self._form_widget)
        self._form_root_layout.setContentsMargins(8, 0, 8, 0)
        self._form_root_layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._form_widget)

        reload_btn = QPushButton("Reload")
        reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reload_btn.clicked.connect(self._load_values)

        apply_btn = QPushButton("Apply")
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.clicked.connect(self._apply_values)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(reload_btn)
        buttons.addWidget(apply_btn)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#b0d8b0; font-size:11px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(self._title_bar)
        layout.addWidget(scroll)
        layout.addLayout(buttons)
        layout.addWidget(self._status)

        self.setStyleSheet(
            "SettingsWindow {"
            "  background-color: #3c3c3c;"
            "  border: 2px solid #888;"
            "  border-radius: 10px;"
            "}"
            "QLabel { color:#f0f0f0; }"
            "QLineEdit {"
            "  background:#2f2f2f; color:#f0f0f0;"
            "  border:1px solid #666; border-radius:4px; padding:4px 6px;"
            "}"
            "QLineEdit:focus { border:1px solid #8aa8ff; }"
            "QSpinBox, QDoubleSpinBox, QComboBox {"
            "  background:#2f2f2f; color:#f0f0f0;"
            "  border:1px solid #666; border-radius:4px; padding:2px 6px;"
            "}"
            "QSlider::groove:horizontal { background:#2f2f2f; height:6px; border-radius:3px; }"
            "QSlider::handle:horizontal { background:#8aa8ff; width:14px; margin:-4px 0; border-radius:7px; }"
            "QCheckBox { color:#f0f0f0; }"
            "QPushButton {"
            "  background-color: #4a4a4a; color: #f0f0f0;"
            "  border: 1px solid #777; border-radius: 6px;"
            "  padding: 4px 14px; font-size: 12px;"
            "}"
            "QPushButton:hover { background-color: #606060; }"
            "QPushButton:pressed { background-color: #303030; }"
            "QGroupBox { border:1px solid #5a5a5a; border-radius:8px; margin-top:10px; padding:8px 6px 6px 6px; }"
            "QGroupBox::title { color:#cfe0ff; subcontrol-origin: margin; left:8px; padding:0 4px; }"
            "QScrollArea { background: transparent; }"
            "QToolTip {"
            "  background-color: #2a2a2a; color: #e8e8e8;"
            "  border: 1px solid #666; border-radius: 4px;"
            "  padding: 6px 8px; font-size: 11px;"
            "}"
        )

        self.setFixedWidth(_FIXED_WIDTH)
        self.resize(_FIXED_WIDTH, 680)
        self._build_fields()
        self._load_values()
        self.hide()

    def _build_fields(self) -> None:
        group_layouts: dict[str, QFormLayout] = {}
        for group_name in _GROUP_ORDER:
            box = QGroupBox(group_name)
            form = QFormLayout(box)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
            form.setContentsMargins(8, 12, 8, 6)
            form.setHorizontalSpacing(16)
            form.setVerticalSpacing(8)
            self._form_root_layout.addWidget(box)
            group_layouts[group_name] = form

        for spec in _FIELD_SPECS:
            widget = self._make_widget(spec)
            label_text = spec.label + ("  (restart)" if spec.restart_required else "")
            label = QLabel(label_text)
            if spec.tooltip:
                label.setToolTip(spec.tooltip)
                label.setCursor(Qt.CursorShape.WhatsThisCursor)
            self._field_widgets[spec.key] = widget
            group_layouts[spec.group].addRow(label, widget)

        obstacle_toggle = self._field_widgets.get("highlight_obstacle_objects")
        if isinstance(obstacle_toggle, QCheckBox):
            obstacle_toggle.toggled.connect(self._update_highlight_dependents_enabled)

        self._form_root_layout.addStretch()

    def _update_highlight_dependents_enabled(self, enabled: bool) -> None:
        for key in _HIGHLIGHT_DEPENDENT_KEYS:
            widget = self._field_widgets.get(key)
            if widget is not None:
                widget.setEnabled(enabled)

    def _make_widget(self, spec: _FieldSpec) -> QWidget:
        if spec.control == "bool":
            return QCheckBox()

        if spec.control == "line":
            return QLineEdit()

        if spec.control == "int":
            spin = QSpinBox()
            spin.setRange(int(spec.minimum or 0), int(spec.maximum or 1000000))
            spin.setSingleStep(int(spec.step or 1))
            return spin

        if spec.control == "double":
            spin = QDoubleSpinBox()
            spin.setDecimals(spec.decimals)
            spin.setRange(float(spec.minimum or 0.0), float(spec.maximum or 1000000.0))
            spin.setSingleStep(float(spec.step or 0.1))
            return spin

        if spec.control == "combo_str":
            combo = QComboBox()
            combo.addItems(list(spec.options))
            return combo

        if spec.control == "combo_int":
            combo = QComboBox()
            combo.addItems(list(spec.options))
            return combo

        if spec.control == "slider_float":
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setMinimum(0)
            max_steps = int(round((float(spec.maximum) - float(spec.minimum)) / float(spec.step)))
            slider.setMaximum(max_steps)

            value_label = QLabel("0")
            value_label.setMinimumWidth(56)
            self._slider_labels[spec.key] = value_label

            slider.valueChanged.connect(
                lambda _v, s=spec, sl=slider, lbl=value_label: self._update_slider_label(s, sl, lbl)
            )

            row_layout.addWidget(slider, 1)
            row_layout.addWidget(value_label)
            return row_widget

        return QLineEdit()

    def _slider_components(self, key: str) -> tuple[QSlider, QLabel]:
        row_widget = self._field_widgets[key]
        slider = row_widget.findChild(QSlider)
        label = self._slider_labels[key]
        if slider is None:
            raise RuntimeError(f"Slider widget missing for {key}")
        return slider, label

    @staticmethod
    def _slider_value_to_float(spec: _FieldSpec, slider_value: int) -> float:
        return float(spec.minimum) + slider_value * float(spec.step)

    @staticmethod
    def _float_to_slider_value(spec: _FieldSpec, value: float) -> int:
        return int(round((value - float(spec.minimum)) / float(spec.step)))

    def _update_slider_label(self, spec: _FieldSpec, slider: QSlider, label: QLabel) -> None:
        value = self._slider_value_to_float(spec, slider.value())
        label.setText(f"{value:.{spec.decimals}f}")

    def _load_values(self) -> None:
        values = settings.model_dump()
        for spec in _FIELD_SPECS:
            key = spec.key
            widget = self._field_widgets[key]
            value = values.get(key)
            if spec.control == "bool" and isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif spec.control == "line" and isinstance(widget, QLineEdit):
                widget.setText(str(value))
            elif spec.control == "int" and isinstance(widget, QSpinBox):
                widget.setValue(int(value))
            elif spec.control == "double" and isinstance(widget, QDoubleSpinBox):
                widget.setValue(float(value))
            elif spec.control == "combo_str" and isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
            elif spec.control == "combo_int" and isinstance(widget, QComboBox):
                widget.setCurrentText(str(int(value)))
            elif spec.control == "slider_float":
                slider, lbl = self._slider_components(key)
                slider.setValue(self._float_to_slider_value(spec, float(value)))
                self._update_slider_label(spec, slider, lbl)

        obstacle_toggle = self._field_widgets.get("highlight_obstacle_objects")
        if isinstance(obstacle_toggle, QCheckBox):
            self._update_highlight_dependents_enabled(obstacle_toggle.isChecked())

        self._status.setStyleSheet("color:#b0d8b0; font-size:11px;")
        self._status.setText("Loaded current settings")

    def _apply_values(self) -> None:
        previous = settings.model_dump()
        parsed: dict[str, Any] = {}
        for spec in _FIELD_SPECS:
            key = spec.key
            widget = self._field_widgets[key]

            if spec.control == "bool" and isinstance(widget, QCheckBox):
                parsed[key] = widget.isChecked()
            elif spec.control == "line" and isinstance(widget, QLineEdit):
                parsed[key] = widget.text().strip()
            elif spec.control == "int" and isinstance(widget, QSpinBox):
                parsed[key] = int(widget.value())
            elif spec.control == "double" and isinstance(widget, QDoubleSpinBox):
                parsed[key] = float(widget.value())
            elif spec.control == "combo_str" and isinstance(widget, QComboBox):
                parsed[key] = widget.currentText().strip()
            elif spec.control == "combo_int" and isinstance(widget, QComboBox):
                parsed[key] = int(widget.currentText().strip())
            elif spec.control == "slider_float":
                slider, _lbl = self._slider_components(key)
                parsed[key] = self._slider_value_to_float(spec, slider.value())

        try:
            validated = Settings.model_validate(parsed)
        except Exception as exc:
            self._status.setStyleSheet("color:#ffaaaa; font-size:11px;")
            self._status.setText(f"Invalid settings: {exc}")
            return

        dumped = validated.model_dump()
        yaml_text = self._to_yaml_text(dumped)
        _CONFIG_PATH.write_text(yaml_text, encoding="utf-8")

        for key, value in dumped.items():
            setattr(settings, key, value)

        if self._on_apply:
            self._on_apply(dumped)

        changed = [k for k, v in dumped.items() if previous.get(k) != v]
        restart_changed = [spec for spec in _FIELD_SPECS if spec.restart_required and spec.key in changed]

        self._status.setStyleSheet("color:#b0d8b0; font-size:11px;")
        if restart_changed:
            restart_labels = [spec.label for spec in restart_changed]
            reply = QMessageBox.question(
                self,
                "Restart Required",
                f"The following settings require a restart to take effect:\n\n" + ", ".join(restart_labels) + "\n\nRestart the application now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._restart_application()
            else:
                self._status.setText("Applied. Restart required for: " + ", ".join(restart_labels))
        elif changed:
            self._status.setText("Applied and saved to config.yaml")
        else:
            self._status.setText("No changes to apply")

    def _restart_application(self) -> None:
        """Restart the application via QProcess (mirrors TrayIcon._restart_application)."""
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication
        try:
            self.hide()
            # Stop scheduler and clean up SimObjects before exit
            app = QApplication.instance()
            tray = getattr(app, '_tray_icon', None)
            if tray and hasattr(tray, '_clean_shutdown'):
                tray._clean_shutdown()
            if getattr(sys, 'frozen', False):
                cmd = [sys.executable, *sys.argv[1:]]
            else:
                cmd = [sys.executable, *sys.argv]
            started = QProcess.startDetached(cmd[0], cmd[1:], str(Path.cwd()))
            if not started:
                raise RuntimeError('detached process launch failed')
            QApplication.quit()
        except Exception as exc:
            self._status.setStyleSheet("color:#ffaaaa; font-size:11px;")
            self._status.setText(f"Restart failed: {exc}")

    @staticmethod
    def _to_yaml_text(values: dict[str, Any]) -> str:
        lines = [
            "# NOTAM Injector configuration",
            "# Generated by Settings window",
            "",
        ]
        for key in Settings.model_fields:
            value = values[key]
            lines.append(f"{key}: {SettingsWindow._yaml_scalar(value)}")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _yaml_scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value).replace('"', '\\"')
        return f'"{text}"'

    def show_near_top_right(self, anchor: QWidget | None = None) -> None:
        """Show the window. If *anchor* is a visible widget, place left of it, top-aligned."""
        if not self.isVisible():
            if anchor is not None and anchor.isVisible():
                margin = 8
                anchor_geo = anchor.frameGeometry()
                x = anchor_geo.left() - self.width() - margin
                y = anchor_geo.top()
                # Clamp to screen left edge
                screen = QGuiApplication.primaryScreen()
                if screen is not None:
                    x = max(screen.availableGeometry().left(), x)
                self.move(x, y)
            else:
                self._reposition_top_right()
        self.show()
        self.raise_()
        self.activateWindow()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._title_bar.geometry().contains(event.position().toPoint())
        ):
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        else:
            self._drag_pos = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def _reposition_top_right(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        margin = 20
        x = area.x() + area.width() - self.width() - margin
        y = area.y() + margin
        self.move(x, y)
