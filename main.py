"""
NOTAM Injector — entry point.

Run with:
    python main.py

Or, after packaging:
    NotamInjector.exe
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is importable when running directly (not installed)
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src.config import settings
from src.scheduler import Scheduler
from src.ui.tray import TrayIcon
from src.ui.widgets.log_panel import _qt_sink


def _configure_logging() -> None:
    # Don't remove logger.remove() because it would remove the Qt sink added by log_panel
    # Instead, just add the terminal and file handlers alongside the Qt sink
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="<green>{time:HH:mm:ss}</green> | <level>{level:8}</level> | {message}",
    )
    logger.add(
        settings.log_file,
        level="DEBUG",
        rotation="10 MB",
        retention=3,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:8} | {name}:{line} | {message}",
    )


def main() -> None:
    _configure_logging()
    logger.info("NOTAM Injector starting…")

    # Qt must not quit when the last window closes (we live in the tray)
    QApplication.setQuitOnLastWindowClosed(False)
    app = QApplication(sys.argv)
    app.setApplicationName("NOTAM Injector")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("notam-injector")

    scheduler = Scheduler()
    tray      = TrayIcon(scheduler)

    if not tray.isSystemTrayAvailable():
        logger.error("System tray not available on this system.")
        sys.exit(1)

    tray.show()
    tray.show_msfs_startup_dialog()
    QTimer.singleShot(350, tray.show_startup_notice)
    scheduler.start()

    logger.info("NOTAM Injector running in system tray.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
