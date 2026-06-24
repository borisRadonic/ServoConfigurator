#!/usr/bin/env python3
"""
MCTool – Motor Controller Configuration Tool
============================================
Entry point. Sets up Qt application, applies theme, and launches the main window.

Usage:
    python main.py [--json path/to/parameters.json]

Run with simulation (no hardware required):
    python main.py
    → Connect → Simulation (Mock)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ── Make sure package root is on sys.path ────────────────────────────
ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication


def _load_stylesheet(app: QApplication) -> None:
    qss_path = ROOT / "resources" / "style_dark.qss"
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Suppress noisy third-party loggers
    logging.getLogger("PySide6").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(description="MCTool Motor Controller Configuration")
    parser.add_argument("--json", metavar="FILE", help="Parameter JSON file to load at startup")
    args = parser.parse_args()

    _configure_logging()

    # Enable HiDPI scaling (Qt6 default, explicit for clarity)
    #QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("MCTool")
    app.setOrganizationName("Bucher Automation")

    # Font
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    _load_stylesheet(app)

    from gui.main_window import MainWindow
    window = MainWindow()

    # Override JSON path if provided on command line
    if args.json:
        p = Path(args.json)
        if p.exists():
            window._store.load_from_json(p)
            window._param_panel.refresh_categories()
            logging.getLogger(__name__).info("Loaded parameters from %s", p)
        else:
            logging.getLogger(__name__).warning("JSON file not found: %s", p)

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
