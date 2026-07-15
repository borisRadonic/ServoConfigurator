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
import io

# Fix Windows terminal encoding (cp1252 cannot handle Unicode)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
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
    parser = argparse.ArgumentParser(description="ServoConfigurator Motor Controller Configuration")
    parser.add_argument("--json",    metavar="FILE", help="Parameter JSON file")
    parser.add_argument("--config",  metavar="FILE", help="App profile YAML (default: app_config.yaml)")
    args = parser.parse_args()

    # Load application profile FIRST — before any Qt or GUI code
    from core.app_profile import init_profile
    from pathlib import Path as _Path
    cfg_path = _Path(args.config) if getattr(args, "config", None) else None
    init_profile(cfg_path)

    _configure_logging()

    # Enable HiDPI scaling (Qt6 default, explicit for clarity)
    #QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Device Configurator")
    app.setOrganizationName("UDS")

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
