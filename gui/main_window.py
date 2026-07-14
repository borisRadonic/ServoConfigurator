"""
Main Window — UI lock during firmware upload
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QTabWidget,
)

from core.parameter_model import ParameterStore
from gui.connection_dialog import ConnectionDialog
from gui.parameter_panel import ParameterPanel
from gui.firmware_panel import FirmwarePanel
from transport.transport import AbstractTransport
from uds.client import UDSClient

log = logging.getLogger(__name__)

FW_TAB_INDEX = 1   # index of Firmware tab in QTabWidget


class _LogHandler(logging.Handler):
    def __init__(self, console: QPlainTextEdit):
        super().__init__()
        self._c = console
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        colors = {
            "DEBUG": "#585B70", "INFO": "#CDD6F4",
            "WARNING": "#FAB387", "ERROR": "#F38BA8",
        }
        color = colors.get(record.levelname, "#CDD6F4")
        self._c.appendHtml(
            f'<span style="color:{color};font-family:monospace">{msg}</span>')


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ServoConfigurator")
        self.resize(1340, 860)

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

        # Parameters tab (index 0)
        self._param_panel = ParameterPanel(self._store)
        self._param_panel.refresh_requested.connect(self._read_all)
        self._tabs.addTab(self._param_panel, "⚙  Parameters")

        # Firmware tab (index 1 = FW_TAB_INDEX)
        self._fw_panel = FirmwarePanel()
        self._fw_panel.upload_started.connect(self._on_upload_started)
        self._fw_panel.upload_finished.connect(self._on_upload_finished)
        self._tabs.addTab(self._fw_panel, "⬆  Firmware")

        # Console tab (index 2)
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

        self._act_ecu_reset = QAction("ECU Reset (Hard)", self)
        self._act_ecu_reset.triggered.connect(self._ecu_reset)
        dm.addAction(self._act_ecu_reset)

        hm = mb.addMenu("Help")
        a = QAction("About", self)
        a.triggered.connect(self._about)
        hm.addAction(a)

    def _build_statusbar(self):
        sb = self.statusBar()
        self._lbl_conn = QLabel("  ● Disconnected")
        self._lbl_conn.setStyleSheet("color:#F38BA8; font-weight:bold;")
        sb.addPermanentWidget(self._lbl_conn)
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

    # ── Upload lock/unlock ────────────────────────────────────────

    def _on_upload_started(self):
        """Lock entire UI during firmware upload — only Firmware tab active."""
        log.warning("Firmware upload started — UI locked")

        # Switch to firmware tab and prevent switching away
        self._tabs.setCurrentIndex(FW_TAB_INDEX)

        # Disable all other tabs
        for i in range(self._tabs.count()):
            if i != FW_TAB_INDEX:
                self._tabs.setTabEnabled(i, False)

        # Disable all menu actions
        for act in [self._act_connect, self._act_disconnect,
                    self._act_read_all, self._act_ecu_reset]:
            act.setEnabled(False)

        # Stop keepalive — don't send TP while uploading
        self._tp_timer.stop()
        self._lbl_tp.setText("")
        self._lbl_lock.setText("  🔒 UPLOAD IN PROGRESS")

    def _on_upload_finished(self):
        """Unlock UI after upload completes or is cancelled."""
        log.info("Upload finished — UI unlocked")

        # Re-enable all tabs
        for i in range(self._tabs.count()):
            self._tabs.setTabEnabled(i, True)

        # Restore menu actions based on connection state
        connected = self._transport is not None
        self._act_connect.setEnabled(not connected)
        self._act_disconnect.setEnabled(connected)
        self._act_read_all.setEnabled(connected)
        self._act_ecu_reset.setEnabled(connected)

        # Resume keepalive
        if connected:
            self._tp_timer.start()

        self._lbl_lock.setText("")

    # ── Actions ──────────────────────────────────────────────────

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

        from uds.firmware_update import FirmwareUpdater
        self._updater = FirmwareUpdater(transport, parent=self)
        self._fw_panel.set_updater(self._updater)

        self._lbl_conn.setText(f"  ● {transport.name}")
        self._lbl_conn.setStyleSheet("color:#A6E3A1; font-weight:bold;")
        self._act_connect.setEnabled(False)
        self._act_disconnect.setEnabled(True)
        self._act_read_all.setEnabled(True)
        self._param_panel.set_connected(True)
        self._fw_panel.set_connected(True)
        self._tp_timer.start()

        log.info("Connected via %s — reading all parameters…", transport.name)
        self._read_all()

    def _disconnect(self):
        self._tp_timer.stop()
        if self._client:
            self._client.shutdown()
            self._client = None
        if self._transport:
            self._transport.disconnect()
            self._transport = None
        self._updater = None
        self._fw_panel.set_updater(None)

        self._lbl_conn.setText("  ● Disconnected")
        self._lbl_conn.setStyleSheet("color:#F38BA8; font-weight:bold;")
        self._lbl_tp.setText("")
        self._act_connect.setEnabled(True)
        self._act_disconnect.setEnabled(False)
        self._act_read_all.setEnabled(False)
        self._param_panel.set_connected(False)
        self._fw_panel.set_connected(False)
        log.info("Disconnected")

    def _read_all(self):
        if self._client:
            self._client.read_all_parameters()

    def _keepalive(self):
        if self._client:
            self._client.send_tester_present()
            self._tp_pulse = not self._tp_pulse
            self._lbl_tp.setText("  ◉ TP" if self._tp_pulse else "  ○ TP")

    def _ecu_reset(self):
        if not self._transport:
            QMessageBox.warning(self, "Not Connected", "Connect first.")
            return
        if QMessageBox.question(
                self, "ECU Reset", "Send hard reset to ECU?",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            from uds.codec import UDSCodec, ResetType
            try:
                self._transport.send(UDSCodec.encode_ecu_reset(ResetType.HARD_RESET))
                log.info("ECU hard reset sent")
            except Exception as e:
                log.error("ECU reset: %s", e)

    def _on_error(self, msg: str):
        self.statusBar().showMessage(f"⚠ {msg}", 5000)

    def _about(self):
        QMessageBox.about(self, "ServoConfigurator",
            "<h3>ServoConfigurator</h3>"
            "<p>UDS motor controller configuration tool.</p>"
            "<b>Transports:</b> Serial · CAN (PEAK) · TCP/IP · Mock")

    def closeEvent(self, event):
        if self._fw_panel.is_uploading:
            r = QMessageBox.question(
                self, "Upload in Progress",
                "Firmware upload is active. Cancel upload and quit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No)
            if r != QMessageBox.Yes:
                event.ignore()
                return
            if self._updater:
                self._updater.cancel()
        self._disconnect()
        event.accept()
