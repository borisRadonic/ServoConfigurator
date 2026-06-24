"""
Main Window
===========
Top-level application window. Hosts:
    - Menu bar with File / Device / View / Help
    - Tab widget: Parameters | Console/Log | (future: Plotter, Firmware)
    - Status bar with connection state + keepalive indicator
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QStatusBar,
    QTabWidget,
    QWidget,
)

from core.parameter_model import ParameterStore
from gui.connection_dialog import ConnectionDialog
from gui.parameter_panel import ParameterPanel
from transport.transport import AbstractTransport
from uds.client import UDSClient

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Log Handler → GUI console                                           #
# ------------------------------------------------------------------ #

class _QtLogHandler(logging.Handler):
    def __init__(self, console: QPlainTextEdit):
        super().__init__()
        self._console = console
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        colors = {
            "DEBUG":    "#6C7086",
            "INFO":     "#CDD6F4",
            "WARNING":  "#FAB387",
            "ERROR":    "#F38BA8",
            "CRITICAL": "#FF0000",
        }
        color = colors.get(record.levelname, "#CDD6F4")
        html = f'<span style="color:{color}; font-family:monospace">{msg}</span>'
        self._console.appendHtml(html)


# ------------------------------------------------------------------ #
#  Main Window                                                         #
# ------------------------------------------------------------------ #

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MCTool – Motor Controller Configuration")
        self.resize(1280, 820)

        self._store = ParameterStore(self)
        self._transport: Optional[AbstractTransport] = None
        self._client: Optional[UDSClient] = None

        self._build_ui()
        self._build_menus()
        self._build_status_bar()
        self._setup_logging()
        self._setup_keepalive()

        self._load_default_parameters()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self):
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self.setCentralWidget(self._tabs)

        # Parameters tab
        self._param_panel = ParameterPanel(self._store)
        self._param_panel.refresh_requested.connect(self._read_all_parameters)
        self._tabs.addTab(self._param_panel, "⚙  Parameters")

        # Console tab
        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(2000)
        font = QFont("Consolas, Courier New, monospace")
        font.setPointSize(11)
        self._console.setFont(font)
        self._console.setStyleSheet(
            "background-color: #11111B; color: #CDD6F4; border: none;"
        )
        self._tabs.addTab(self._console, "📋  Console")

    def _build_menus(self):
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("File")
        open_json_act = QAction("Open Parameter JSON…", self)
        open_json_act.setShortcut(QKeySequence.Open)
        open_json_act.triggered.connect(self._open_json)
        file_menu.addAction(open_json_act)
        file_menu.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.setShortcut(QKeySequence.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # Device
        dev_menu = mb.addMenu("Device")
        self._connect_act = QAction("Connect…", self)
        self._connect_act.setShortcut("Ctrl+Shift+C")
        self._connect_act.triggered.connect(self._show_connect_dialog)
        dev_menu.addAction(self._connect_act)

        self._disconnect_act = QAction("Disconnect", self)
        self._disconnect_act.setEnabled(False)
        self._disconnect_act.triggered.connect(self._disconnect)
        dev_menu.addAction(self._disconnect_act)

        dev_menu.addSeparator()

        self._read_all_act = QAction("Read All Parameters", self)
        self._read_all_act.setShortcut("F5")
        self._read_all_act.setEnabled(False)
        self._read_all_act.triggered.connect(self._read_all_parameters)
        dev_menu.addAction(self._read_all_act)

        dev_menu.addSeparator()

        ecu_reset_act = QAction("ECU Reset (Hard)", self)
        ecu_reset_act.triggered.connect(self._ecu_reset)
        dev_menu.addAction(ecu_reset_act)

        # Help
        help_menu = mb.addMenu("Help")
        about_act = QAction("About MCTool", self)
        about_act.triggered.connect(self._about)
        help_menu.addAction(about_act)

    def _build_status_bar(self):
        sb = self.statusBar()

        self._conn_indicator = QLabel("  ● Disconnected")
        self._conn_indicator.setStyleSheet("color: #F38BA8; font-weight: bold;")
        sb.addPermanentWidget(self._conn_indicator)

        self._transport_label = QLabel("")
        sb.addPermanentWidget(self._transport_label)

        self._keepalive_label = QLabel("")
        self._keepalive_label.setStyleSheet("color: #6C7086; font-size: 11px;")
        sb.addPermanentWidget(self._keepalive_label)

    def _setup_logging(self):
        handler = _QtLogHandler(self._console)
        handler.setLevel(logging.DEBUG)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)

    def _setup_keepalive(self):
        """Send TesterPresent every 2 seconds to keep ECU in session."""
        self._keepalive_timer = QTimer(self)
        self._keepalive_timer.setInterval(2000)
        self._keepalive_timer.timeout.connect(self._send_keepalive)
        self._keepalive_pulse = False

    # ── Actions ──────────────────────────────────────────────────────

    def _load_default_parameters(self):
        """Load bundled parameters.json if it exists next to the executable."""
        for candidate in [
            Path(__file__).parent.parent / "parameters.json",
            Path.cwd() / "parameters.json",
        ]:
            if candidate.exists():
                self._store.load_from_json(candidate)
                self._param_panel.refresh_categories()
                log.info("Loaded parameters from %s", candidate)
                self.statusBar().showMessage(f"Loaded {len(self._store.all_dids())} parameters", 4000)
                return

    def _open_json(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Parameter JSON", "", "JSON Files (*.json);;All Files (*)"
        )
        if path:
            try:
                self._store.load_from_json(path)
                self._param_panel.refresh_categories()
                log.info("Loaded %d parameters from %s", len(self._store.all_dids()), path)
                self.statusBar().showMessage(f"Loaded {len(self._store.all_dids())} parameters", 4000)
            except Exception as e:
                QMessageBox.critical(self, "Load Error", f"Failed to load JSON:\n{e}")

    def _show_connect_dialog(self):
        dlg = ConnectionDialog(self)
        dlg.connected.connect(self._on_connected)
        dlg.exec()

    def _on_connected(self, transport: AbstractTransport):
        self._transport = transport
        if self._client:
            self._client.shutdown()

        self._client = UDSClient(transport, self._store, self)
        self._client.read_progress.connect(self._param_panel.on_read_progress)
        self._client.all_read_done.connect(self._param_panel.on_all_read_done)
        self._client.all_read_done.connect(self._on_all_read_done)
        self._client.parameter_written.connect(self._param_panel.on_parameter_written)
        self._client.error_occurred.connect(self._on_error)

        # Update UI state
        self._conn_indicator.setText(f"  ● Connected ({transport.name})")
        self._conn_indicator.setStyleSheet("color: #A6E3A1; font-weight: bold;")
        self._transport_label.setText(f"  {transport.name}")
        self._connect_act.setEnabled(False)
        self._disconnect_act.setEnabled(True)
        self._read_all_act.setEnabled(True)

        self._keepalive_timer.start()

        # Auto-read all parameters
        self._read_all_parameters()

    def _disconnect(self):
        self._keepalive_timer.stop()
        if self._client:
            self._client.shutdown()
            self._client = None
        if self._transport:
            self._transport.disconnect()
            self._transport = None

        self._conn_indicator.setText("  ● Disconnected")
        self._conn_indicator.setStyleSheet("color: #F38BA8; font-weight: bold;")
        self._transport_label.setText("")
        self._keepalive_label.setText("")
        self._connect_act.setEnabled(True)
        self._disconnect_act.setEnabled(False)
        self._read_all_act.setEnabled(False)
        log.info("Disconnected")

    def _read_all_parameters(self):
        if self._client:
            self._client.read_all_parameters()
            log.info("Reading all %d parameters…", len(self._store.all_dids()))

    def _on_all_read_done(self):
        loaded = sum(1 for pv in self._store.values.values() if pv.is_loaded)
        errors = sum(1 for pv in self._store.values.values() if pv.error)
        log.info("Read complete: %d loaded, %d errors", loaded, errors)

    def _send_keepalive(self):
        if self._client:
            self._client.send_tester_present()
            self._keepalive_pulse = not self._keepalive_pulse
            self._keepalive_label.setText("  ◉ TP" if self._keepalive_pulse else "  ○ TP")

    def _ecu_reset(self):
        if not self._client or not self._transport:
            QMessageBox.warning(self, "Not Connected", "Connect to a device first.")
            return
        reply = QMessageBox.question(
            self, "ECU Reset",
            "Send a hard reset to the ECU?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            from uds.codec import UDSCodec, ResetType
            try:
                req = UDSCodec.encode_ecu_reset(ResetType.HARD_RESET)
                self._transport.send(req)
                log.info("ECU hard reset sent")
            except Exception as e:
                log.error("ECU reset failed: %s", e)

    def _on_error(self, msg: str):
        self.statusBar().showMessage(f"⚠ {msg}", 5000)
        log.error(msg)

    def _about(self):
        QMessageBox.about(
            self,
            "About MCTool",
            "<h3>MCTool – Motor Controller Configuration Tool</h3>"
            "<p>UDS-based parameter configuration, diagnostics, and firmware "
            "management for FOC motor controllers.</p>"
            "<p><b>Architecture:</b><br>"
            "GUI (PySide6) → UDS Client → Transport (Serial / CAN / Mock)<br>"
            "Parameter Model ← ParameterStore ← UDS RDBI/WDBI</p>"
            "<p>Transport: Serial (pyserial), CAN (python-can / PEAK USB-CAN)<br>"
            "Protocol: ISO 14229-1 UDS, ISO 15765-2 ISO-TP</p>"
        )

    def closeEvent(self, event):
        self._disconnect()
        event.accept()
