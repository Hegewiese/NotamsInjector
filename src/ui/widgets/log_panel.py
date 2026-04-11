"""
Log panel widget — displays live log output inside the debug window.
Loguru is wired to write into this widget via a custom sink.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QLabel,
)
from loguru import logger


LEVEL_COLORS: dict[str, str] = {
    "TRACE":   "#888888",
    "DEBUG":   "#aaaaaa",
    "INFO":    "#ffffff",
    "SUCCESS": "#44ff44",
    "WARNING": "#ffaa00",
    "ERROR":   "#ff4444",
    "CRITICAL":"#ff0000",
}


class _LogSignalBridge(QObject):
    """Receives log records from the loguru sink (any thread) → Qt signal."""
    new_record = Signal(str, str)   # level, message


_bridge = _LogSignalBridge()


def _qt_sink(record):
    level = record["level"].name
    line  = record["message"]
    _bridge.new_record.emit(level, line)


# Install the loguru → Qt bridge once at import time
logger.add(_qt_sink, format="{message}", level="DEBUG")


class LogPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()
        _bridge.new_record.connect(self._append)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Min level:"))

        self._level_combo = QComboBox()
        self._level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._level_combo.setCurrentText("INFO")
        toolbar.addWidget(self._level_combo)

        toolbar.addStretch()

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        toolbar.addWidget(clear_btn)
        layout.addLayout(toolbar)

        # Text area
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(2000)
        font = QFont("Courier New", 9)
        self._text.setFont(font)
        self._text.setStyleSheet("background:#1e1e1e; color:#ffffff;")
        layout.addWidget(self._text)

    def _append(self, level: str, message: str) -> None:
        min_level = self._level_combo.currentText()
        levels = list(LEVEL_COLORS.keys())
        if levels.index(level) < levels.index(min_level):
            return

        color = LEVEL_COLORS.get(level, "#ffffff")
        html = (
            f'<span style="color:{color}">'
            f'[{level:8s}] {message.replace("<","&lt;").replace(">","&gt;")}'
            f'</span>'
        )
        self._text.appendHtml(html)
        # Auto-scroll to bottom
        self._text.moveCursor(QTextCursor.MoveOperation.End)

    def _clear(self) -> None:
        self._text.clear()
