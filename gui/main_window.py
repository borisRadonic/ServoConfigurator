"""
Main Window — with Diagnostics tab (DTC + Session + ECU Info)
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QTabWidget, QWidget,
    QVBoxLayout, QHBoxLayout, QSplitter,
)
from PySide6.QtCore import Qt

from core.parameter_model import ParameterStore
from gui.connection_dialog import ConnectionDialog
from gui.parameter_panel import ParameterPanel
from gui.firmware_panel import FirmwarePanel
from gui.dtc_panel import DTCPanel
from gui.session_panel import SessionPanel
from gui.ecu_info_panel import ECUInfoPanel
from transport.transport import AbstractTransport
from uds.client import UDSClient

log = logging.getLogger(__name__)

TAB_PARAMS    = 0
TAB_DIAG      = 1
TAB_FIRMWARE  = 2
TAB_CONSOLE   = 3


class _LogHandler(logging.Handler):
    def __init__(self, console: QPlainTextEdit):
        super().__init__()
        self._c = console
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"))

    def emit(self, record):
        msg  = self.format(record)
        colors = {"DEBUG":"#585B70","INFO":"#CDD6F4",
                  "WARNING":"#FAB387","ERROR":"#F38BA8"}
        color = colors.get(record.levelname, "#CDD6F4")
        self._c.appendHtml(
            f'<span style="color:{color};font-family:monospace">{msg}</span>')


class _DiagTab(QWidget):
    """Container for DTC + Session + ECU Info in one tab with sub-tabs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._sub_tabs = QTabWidget()
        self._sub_tabs.setDocumentMode(True)
        self._sub_tabs.setTabPosition(QTabWidget.North)

        self.dtc_panel     = DTCPanel()
        self.session_panel = SessionPanel()
        self.ecu_panel     = ECUInfoPanel()

        self._sub_tabs.addTab(self.dtc_panel,     "🔴  DTC")
        self._sub_tabs.addTab(self.session_panel, "🔧  Session / Raw UDS")
        self._sub_tabs.addTab(self.ecu_panel,     "ℹ  ECU Info")

        layout.addWidget(self._sub_tabs)

    def set_transport(self, transport):
        self.dtc_panel.set_transport(transport)
        self.session_panel.set_transport(transport)
        self.ecu_panel.set_transport(transport)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ServoConfigurator")
        self.resize(1380, 880)

        self._store = ParameterStore(self)
        self._transport: Optional[AbstractTransport] = None
        self._client:    Optional[UDSClient] = None
        self._updater = None

        self._build_ui()
        self._build_menus()
        self._build_statusbar()
        self._setup_logging()
        self._setup_keepalive()
        self._load_default_json()

    # ── Build ────────────────────────────────────────────────────

    def _build_ui(self):
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self.setCentralWidget(self._tabs)

        # Parameters (index 0)
        self._param_panel = ParameterPanel(self._store)
        self._param_panel.refresh_requested.connect(self._read_all)
        self._tabs.addTab(self._param_panel, "⚙  Parameters")

        # Diagnostics (index 1)
        self._diag_tab = _DiagTab()
        self._tabs.addTab(self._diag_tab, "🔍  Diagnostics")

        # Firmware (index 2)
        self._fw_panel = FirmwarePanel()
        self._fw_panel.upload_started.connect(self._on_upload_started)
        self._fw_panel.upload_finished.connect(self._on_upload_finished)
        self._tabs.addTab(self._fw_panel, "⬆  Firmware")

        # Console (index 3)
        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(3000)
        self._console.setFont(QFont("Consolas, Courier New", 10))
        self._console.setStyleSheet(
            "background:#11111B; color:#CDD6F4; border:none;")
        self._tabs.addTab(self._console, "📋  Console")

    def _build_menus(self):
        mb = self.menuBar()

        fm = mb.addMenu("File")
        a = QAction("Open Parameter JSON…", self)
        a.setShortcut(QKeySequence.Open)
        a.triggered.connect(self._open_json)
        fm.addAction(a)
        fm.addSeparator()
        q = QAction("Quit", self)
        q.setShortcut(QKeySequence.Quit)
        q.triggered.connect(self.close)
        fm.addAction(q)

        dm = mb.addMenu("Device")
        self._act_connect = QAction("Connect…", self)
        self._act_connect.setShortcut("Ctrl+Shift+C")
        self._act_connect.triggered.connect(self._show_connect)
        dm.addAction(self._act_connect)

        self._act_disconnect = QAction("Disconnect", self)
        self._act_disconnect.setEnabled(False)
        self._act_disconnect.triggered.connect(self._disconnect)
        dm.addAction(self._act_disconnect)
        dm.addSeparator()

        self._act_read_all = QAction("Read All Parameters", self)
        self._act_read_all.setShortcut("F5")
        self._act_read_all.setEnabled(False)
        self._act_read_all.triggered.connect(self._read_all)
        dm.addAction(self._act_read_all)
        dm.addSeparator()

        self._act_read_dtc = QAction("Read DTCs", self)
        self._act_read_dtc.setShortcut("F6")
        self._act_read_dtc.setEnabled(False)
        self._act_read_dtc.triggered.connect(self._quick_read_dtc)
        dm.addAction(self._act_read_dtc)

        self._act_read_ecu = QAction("Read ECU Info", self)
        self._act_read_ecu.setShortcut("F7")
        self._act_read_ecu.setEnabled(False)
        self._act_read_ecu.triggered.connect(self._quick_read_ecu)
        dm.addAction(self._act_read_ecu)

        hm = mb.addMenu("Help")
        a = QAction("About", self)
        a.triggered.connect(self._about)
        hm.addAction(a)

    def _build_statusbar(self):
        sb = self.statusBar()
        self._lbl_conn = QLabel("  ● Disconnected")
        self._lbl_conn.setStyleSheet("color:#F38BA8; font-weight:bold;")
        sb.addPermanentWidget(self._lbl_conn)
        self._lbl_sess = QLabel("")
        self._lbl_sess.setStyleSheet("color:#6C7086; font-size:11px;")
        sb.addPermanentWidget(self._lbl_sess)
        self._lbl_lock = QLabel("")
        self._lbl_lock.setStyleSheet("color:#FAB387; font-weight:bold; font-size:12px;")
        sb.addPermanentWidget(self._lbl_lock)
        self._lbl_tp = QLabel("")
        self._lbl_tp.setStyleSheet("color:#585B70; font-size:11px;")
        sb.addPermanentWidget(self._lbl_tp)

    def _setup_logging(self):
        h = _LogHandler(self._console)
        h.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(logging.DEBUG)

    def _setup_keepalive(self):
        self._tp_timer = QTimer(self)
        self._tp_timer.setInterval(2000)
        self._tp_timer.timeout.connect(self._keepalive)
        self._tp_pulse = False

    # ── Upload lock ───────────────────────────────────────────────

    def _on_upload_started(self):
        log.warning("Firmware upload started — UI locked")
        self._tabs.setCurrentIndex(TAB_FIRMWARE)
        for i in range(self._tabs.count()):
            if i != TAB_FIRMWARE:
                self._tabs.setTabEnabled(i, False)
        for act in [self._act_connect, self._act_disconnect,
                    self._act_read_all, self._act_read_dtc, self._act_read_ecu]:
            act.setEnabled(False)
        self._tp_timer.stop()
        self._lbl_tp.setText("")
        self._lbl_lock.setText("  🔒 UPLOAD IN PROGRESS")

    def _on_upload_finished(self):
        log.info("Upload finished — UI unlocked")
        for i in range(self._tabs.count()):
            self._tabs.setTabEnabled(i, True)
        connected = self._transport is not None
        self._act_connect.setEnabled(not connected)
        self._act_disconnect.setEnabled(connected)
        self._act_read_all.setEnabled(connected)
        self._act_read_dtc.setEnabled(connected)
        self._act_read_ecu.setEnabled(connected)
        if connected:
            self._tp_timer.start()
        self._lbl_lock.setText("")

    # ── Actions ───────────────────────────────────────────────────

    def _load_default_json(self):
        for p in [Path(__file__).parent.parent / "parameters.json",
                  Path.cwd() / "parameters.json"]:
            if p.exists():
                self._store.load_from_json(p)
                self._param_panel.refresh_categories()
                n = len(self._store.all_dids())
                log.info("Loaded %d parameters from %s", n, p.name)
                self.statusBar().showMessage(
                    f"{n} parameters loaded — connect to read device values", 5000)
                return

    def _open_json(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Parameter JSON", "",
            "JSON Files (*.json);;All Files (*)")
        if not path: return
        try:
            self._store.load_from_json(path)
            self._param_panel.refresh_categories()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

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
        self._client.parameter_written.connect(self._param_panel.on_parameter_written)
        self._client.error_occurred.connect(self._on_error)

        # Wire diagnostic panels
        self._diag_tab.set_transport(transport)
        self._diag_tab.session_panel.session_changed.connect(self._on_session_changed)

        # Wire firmware updater
        from uds.firmware_update import FirmwareUpdater
        self._updater = FirmwareUpdater(transport, parent=self)
        self._fw_panel.set_updater(self._updater)

        # UI state
        self._lbl_conn.setText(f"  ● {transport.name}")
        self._lbl_conn.setStyleSheet("color:#A6E3A1; font-weight:bold;")
        self._lbl_sess.setText("  Default Session")
        self._act_connect.setEnabled(False)
        self._act_disconnect.setEnabled(True)
        self._act_read_all.setEnabled(True)
        self._act_read_dtc.setEnabled(True)
        self._act_read_ecu.setEnabled(True)
        self._param_panel.set_connected(True)
        self._fw_panel.set_connected(True)
        self._tp_timer.start()

        log.info("Connected via %s — reading all parameters…", transport.name)
        self._read_all()

    def _disconnect(self):
        self._tp_timer.stop()
        if self._client: self._client.shutdown(); self._client = None
        if self._transport: self._transport.disconnect(); self._transport = None
        self._updater = None
        self._diag_tab.set_transport(None)
        self._fw_panel.set_updater(None)

        self._lbl_conn.setText("  ● Disconnected")
        self._lbl_conn.setStyleSheet("color:#F38BA8; font-weight:bold;")
        self._lbl_sess.setText("")
        self._lbl_tp.setText("")
        self._act_connect.setEnabled(True)
        self._act_disconnect.setEnabled(False)
        self._act_read_all.setEnabled(False)
        self._act_read_dtc.setEnabled(False)
        self._act_read_ecu.setEnabled(False)
        self._param_panel.set_connected(False)
        self._fw_panel.set_connected(False)
        log.info("Disconnected")

    def _read_all(self):
        if self._client:
            self._client.read_all_parameters()

    def _quick_read_dtc(self):
        """F6: switch to Diagnostics/DTC and trigger read."""
        self._tabs.setCurrentIndex(TAB_DIAG)
        self._diag_tab._sub_tabs.setCurrentIndex(0)
        self._diag_tab.dtc_panel._read_dtcs()

    def _quick_read_ecu(self):
        """F7: switch to Diagnostics/ECU Info and trigger read."""
        self._tabs.setCurrentIndex(TAB_DIAG)
        self._diag_tab._sub_tabs.setCurrentIndex(2)
        self._diag_tab.ecu_panel._read_all()

    def _on_session_changed(self, sess: int):
        from gui.session_panel import SESSION_NAMES
        name = SESSION_NAMES.get(sess, f"Session 0x{sess:02X}")
        colors = {0x01: "#6C7086", 0x02: "#F38BA8", 0x03: "#FAB387"}
        color = colors.get(sess, "#6C7086")
        self._lbl_sess.setText(f"  {name}")
        self._lbl_sess.setStyleSheet(f"color:{color}; font-size:11px;")

    def _keepalive(self):
        if self._client:
            self._client.send_tester_present()
            self._tp_pulse = not self._tp_pulse
            self._lbl_tp.setText("  ◉ TP" if self._tp_pulse else "  ○ TP")

    def _on_error(self, msg: str):
        self.statusBar().showMessage(f"⚠ {msg}", 5000)

    def _about(self):
        QMessageBox.about(self, "ServoConfigurator",
            "<h3>ServoConfigurator</h3>"
            "<p>UDS motor controller configuration and diagnostics tool.</p>"
            "<b>Transports:</b> Serial · CAN (PEAK) · TCP/IP · Mock<br>"
            "<b>Features:</b> Parameters · DTC · Session Control · "
            "Raw UDS · ECU Info · Firmware Upload")

    def closeEvent(self, event):
        if self._fw_panel.is_uploading:
            r = QMessageBox.question(
                self, "Upload in Progress",
                "Firmware upload is active. Cancel and quit?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                event.ignore()
                return
            if self._updater: self._updater.cancel()
        self._disconnect()
        event.accept()
