"""
Main Window
===========
Fixes:
 - Parametre prikazuje odmah iz JSON (defaulti '–')
 - Čitanje sa uređaja tek nakon connecta (automatski)
 - Read All button omogućen tek kad je connected
"""
from __future__ import annotations
import logging, sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QTabWidget,
)

from core.parameter_model import ParameterStore
from gui.connection_dialog import ConnectionDialog
from gui.parameter_panel import ParameterPanel
from transport.transport import AbstractTransport
from uds.client import UDSClient

log = logging.getLogger(__name__)


class _QtLogHandler(logging.Handler):
    def __init__(self, console: QPlainTextEdit):
        super().__init__()
        self._c = console
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        colors = {"DEBUG":"#6C7086","INFO":"#CDD6F4",
                  "WARNING":"#FAB387","ERROR":"#F38BA8","CRITICAL":"#FF0000"}
        color = colors.get(record.levelname, "#CDD6F4")
        self._c.appendHtml(f'<span style="color:{color};font-family:monospace">{msg}</span>')


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ServoConfigurator — Motor Controller Configuration")
        self.resize(1300, 840)

        self._store = ParameterStore(self)
        self._transport: Optional[AbstractTransport] = None
        self._client: Optional[UDSClient] = None

        self._build_ui()
        self._build_menus()
        self._build_status_bar()
        self._setup_logging()
        self._setup_keepalive()

        # Load JSON immediately — shows parameter names/ranges without device
        self._load_default_parameters()

    # ── UI ──────────────────────────────────────────────────────

    def _build_ui(self):
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self.setCentralWidget(self._tabs)

        self._param_panel = ParameterPanel(self._store)
        self._param_panel.refresh_requested.connect(self._read_all_parameters)
        self._tabs.addTab(self._param_panel, "⚙  Parameters")

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(2000)
        font = QFont("Consolas, Courier New, monospace")
        font.setPointSize(10)
        self._console.setFont(font)
        self._console.setStyleSheet("background:#11111B; color:#CDD6F4; border:none;")
        self._tabs.addTab(self._console, "📋  Console")

    def _build_menus(self):
        mb = self.menuBar()

        fm = mb.addMenu("File")
        act = QAction("Open Parameter JSON…", self)
        act.setShortcut(QKeySequence.Open)
        act.triggered.connect(self._open_json)
        fm.addAction(act)
        fm.addSeparator()
        qa = QAction("Quit", self)
        qa.setShortcut(QKeySequence.Quit)
        qa.triggered.connect(self.close)
        fm.addAction(qa)

        dm = mb.addMenu("Device")
        self._connect_act = QAction("Connect…", self)
        self._connect_act.setShortcut("Ctrl+Shift+C")
        self._connect_act.triggered.connect(self._show_connect)
        dm.addAction(self._connect_act)

        self._disconnect_act = QAction("Disconnect", self)
        self._disconnect_act.setEnabled(False)
        self._disconnect_act.triggered.connect(self._disconnect)
        dm.addAction(self._disconnect_act)
        dm.addSeparator()

        self._read_all_act = QAction("Read All Parameters", self)
        self._read_all_act.setShortcut("F5")
        self._read_all_act.setEnabled(False)
        self._read_all_act.triggered.connect(self._read_all_parameters)
        dm.addAction(self._read_all_act)
        dm.addSeparator()

        ecu = QAction("ECU Reset (Hard)", self)
        ecu.triggered.connect(self._ecu_reset)
        dm.addAction(ecu)

        hm = mb.addMenu("Help")
        ab = QAction("About", self)
        ab.triggered.connect(self._about)
        hm.addAction(ab)

    def _build_status_bar(self):
        sb = self.statusBar()
        self._conn_label = QLabel("  ● Disconnected")
        self._conn_label.setStyleSheet("color:#F38BA8; font-weight:bold;")
        sb.addPermanentWidget(self._conn_label)
        self._tp_label = QLabel("")
        self._tp_label.setStyleSheet("color:#6C7086; font-size:11px;")
        sb.addPermanentWidget(self._tp_label)

    def _setup_logging(self):
        h = _QtLogHandler(self._console)
        h.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(logging.DEBUG)

    def _setup_keepalive(self):
        self._keepalive_timer = QTimer(self)
        self._keepalive_timer.setInterval(2000)
        self._keepalive_timer.timeout.connect(self._send_keepalive)
        self._tp_pulse = False

    # ── Actions ─────────────────────────────────────────────────

    def _load_default_parameters(self):
        """Load JSON at startup — parameters visible before any connection."""
        for candidate in [
            Path(__file__).parent.parent / "parameters.json",
            Path.cwd() / "parameters.json",
        ]:
            if candidate.exists():
                self._store.load_from_json(candidate)
                self._param_panel.refresh_categories()
                n = len(self._store.all_dids())
                log.info("Loaded %d parameters from %s", n, candidate)
                self.statusBar().showMessage(
                    f"Loaded {n} parameters — connect to device to read values", 6000)
                return

    def _open_json(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Parameter JSON", "", "JSON Files (*.json);;All Files (*)")
        if not path: return
        try:
            self._store.load_from_json(path)
            self._param_panel.refresh_categories()
            log.info("Loaded %d parameters", len(self._store.all_dids()))
        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Failed to load JSON:\n{e}")

    def _show_connect(self):
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

        self._conn_label.setText(f"  ● {transport.name}")
        self._conn_label.setStyleSheet("color:#A6E3A1; font-weight:bold;")
        self._connect_act.setEnabled(False)
        self._disconnect_act.setEnabled(True)
        self._read_all_act.setEnabled(True)
        self._param_panel.set_connected(True)

        self._keepalive_timer.start()

        # Automatski čitaj sve parametre odmah nakon connecta
        log.info("Connected via %s — reading all parameters…", transport.name)
        self._read_all_parameters()

    def _disconnect(self):
        self._keepalive_timer.stop()
        if self._client: self._client.shutdown(); self._client = None
        if self._transport: self._transport.disconnect(); self._transport = None
        self._conn_label.setText("  ● Disconnected")
        self._conn_label.setStyleSheet("color:#F38BA8; font-weight:bold;")
        self._tp_label.setText("")
        self._connect_act.setEnabled(True)
        self._disconnect_act.setEnabled(False)
        self._read_all_act.setEnabled(False)
        self._param_panel.set_connected(False)
        log.info("Disconnected")

    def _read_all_parameters(self):
        if self._client:
            self._client.read_all_parameters()

    def _on_all_read_done(self):
        loaded = sum(1 for pv in self._store.values.values() if pv.is_loaded)
        errors = sum(1 for pv in self._store.values.values() if pv.error)
        log.info("Read complete: %d loaded, %d errors", loaded, errors)

    def _send_keepalive(self):
        if self._client:
            self._client.send_tester_present()
            self._tp_pulse = not self._tp_pulse
            self._tp_label.setText("  ◉ TP" if self._tp_pulse else "  ○ TP")

    def _ecu_reset(self):
        if not self._transport:
            QMessageBox.warning(self, "Not Connected", "Connect to a device first.")
            return
        if QMessageBox.question(self, "ECU Reset", "Send hard reset to ECU?",
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            from uds.codec import UDSCodec, ResetType
            try:
                self._transport.send(UDSCodec.encode_ecu_reset(ResetType.HARD_RESET))
                log.info("ECU hard reset sent")
            except Exception as e:
                log.error("ECU reset failed: %s", e)

    def _on_error(self, msg: str):
        self.statusBar().showMessage(f"⚠ {msg}", 5000)
        log.error(msg)

    def _about(self):
        QMessageBox.about(self, "About ServoConfigurator",
            "<h3>ServoConfigurator</h3>"
            "<p>UDS-based motor controller configuration tool.</p>"
            "<p><b>Transports:</b> Serial · CAN (PEAK) · TCP/IP · Mock</p>"
            "<p><b>TCP/IP:</b> Lokalni UDS server za testiranje protokola<br>"
            "bez hardwarea — koristi isti framing kao Serial transport.</p>")

    def closeEvent(self, event):
        self._disconnect()
        event.accept()
