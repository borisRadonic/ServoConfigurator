"""
Main Window — with Device Scanner + Change Device Address
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QDialog, QInputDialog, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QTabWidget,
    QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

from core.parameter_model import ParameterStore
from gui.connection_dialog import ConnectionDialog
from gui.parameter_panel import ParameterPanel
from gui.firmware_panel import FirmwarePanel
from gui.dtc_panel import DTCPanel
from gui.session_panel import SessionPanel
from gui.ecu_info_panel import ECUInfoPanel
from gui.config_panel import ConfigPanel
from transport.transport import AbstractTransport, CANTransport
from uds.client import UDSClient
from core.app_profile import profile

log = logging.getLogger(__name__)

# Tab indices are computed dynamically in MainWindow._build_ui()
# because tabs are conditionally shown based on app_config.yaml profile.
# Use self._tab_index("firmware") etc. instead of hardcoded constants.

# NvField::DeviceAddress DID — from nvstore_field_map (offset 0x000A)
# Write this DID with WDBI to change device address, then ECU Reset
DID_DEVICE_ADDRESS = 0x000A


class _LogHandler(logging.Handler):
    def __init__(self, console: QPlainTextEdit):
        super().__init__()
        self._c = console
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        colors = {"DEBUG": "#585B70", "INFO": "#CDD6F4",
                  "WARNING": "#FAB387", "ERROR": "#F38BA8"}
        color = colors.get(record.levelname, "#CDD6F4")
        self._c.appendHtml(
            f'<span style="color:{color};font-family:monospace">{msg}</span>')


class _DiagTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._sub = QTabWidget()
        self._sub.setDocumentMode(True)
        from core.app_profile import profile as _p
        _df = _p.features.diagnostics

        if _df.dtc:
            self.dtc_panel = DTCPanel()
            self._sub.addTab(self.dtc_panel, "🔴  DTC")
        else:
            self.dtc_panel = None

        if _df.session:
            self.session_panel = SessionPanel()
            self._sub.addTab(self.session_panel, "🔧  Session / Raw UDS")
        else:
            self.session_panel = None

        if _df.ecu_info:
            self.ecu_panel = ECUInfoPanel()
            self._sub.addTab(self.ecu_panel, "ℹ  ECU Info")
        else:
            self.ecu_panel = None
        layout.addWidget(self._sub)

    def set_transport(self, transport):
        if self.dtc_panel:     self.dtc_panel.set_transport(transport)
        if self.session_panel: self.session_panel.set_transport(transport)
        if self.ecu_panel:     self.ecu_panel.set_transport(transport)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(profile.app.title)
        self.resize(1380, 880)

        self._store = ParameterStore(self)
        self._transport: Optional[AbstractTransport] = None
        self._client:    Optional[UDSClient] = None
        self._updater = None
        self._device_address: Optional[int] = None
        self._tab_labels: dict[str, str] = {}  # key → tab text, set in _build_ui

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

        _params_feat = profile.features.parameters
        if _params_feat.enabled and _params_feat.show_tab:
            self._param_panel = ParameterPanel(self._store)
            self._param_panel.refresh_requested.connect(self._read_all)
            self._tabs.addTab(self._param_panel, "⚙  Parameters")
            self._tab_labels["params"] = "⚙  Parameters"
        else:
            self._param_panel = None

        if profile.features.diagnostics.enabled:
            self._diag_tab = _DiagTab()
            self._tabs.addTab(self._diag_tab, "🔍  Diagnostics")
            self._tab_labels["diag"] = "🔍  Diagnostics"
        else:
            self._diag_tab = None

        # Configuration Management tab
        if profile.features.config_management.enabled:
            self._config_panel = ConfigPanel(self._store)
            self._config_panel.write_parameter.connect(self._on_config_write)
            self._tabs.addTab(self._config_panel, "⚙  Configuration")
            self._tab_labels["config"] = "⚙  Configuration"
        else:
            self._config_panel = None

        if profile.features.firmware.enabled:
            self._fw_panel = FirmwarePanel()
            self._fw_panel.upload_started.connect(self._on_upload_started)
            self._fw_panel.upload_finished.connect(self._on_upload_finished)
            self._tabs.addTab(self._fw_panel, "⬆  Firmware")
            self._tab_labels["firmware"] = "⬆  Firmware"
        else:
            self._fw_panel = None

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(3000)
        self._console.setFont(QFont("Consolas, Courier New", 10))
        self._console.setStyleSheet(
            "background:#11111B; color:#CDD6F4; border:none;")
        self._tabs.addTab(self._console, "📋  Console")
        self._tab_labels["console"] = "📋  Console"

    def _build_menus(self):
        mb = self.menuBar()

        # File
        fm = mb.addMenu("File")
        a = QAction("Open Parameter JSON…", self)
        a.setShortcut(QKeySequence.Open)
        a.triggered.connect(self._open_json)
        fm.addAction(a)
        fm.addSeparator()
        fm.addAction(QAction("Quit", self, shortcut=QKeySequence.Quit,
                              triggered=self.close))

        # Device
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

        # Scanner — only useful on CAN
        self._act_scan = QAction("🔍  Scan CAN Bus for Devices…", self)
        self._act_scan.setShortcut("Ctrl+Shift+S")
        self._act_scan.triggered.connect(self._show_scanner)
        self._act_scan.setVisible(profile.features.device_scanner.enabled)
        dm.addAction(self._act_scan)

        dm.addSeparator()

        self._act_read_all = QAction("Read All Parameters", self)
        self._act_read_all.setShortcut("F5")
        self._act_read_all.setEnabled(False)
        self._act_read_all.setVisible(
            profile.features.parameters.enabled and
            profile.features.parameters.show_menu)
        self._act_read_all.triggered.connect(self._read_all)
        dm.addAction(self._act_read_all)

        self._act_read_dtc = QAction("Read DTCs", self)
        self._act_read_dtc.setShortcut("F6")
        self._act_read_dtc.setEnabled(False)
        self._act_read_dtc.setVisible(profile.features.diagnostics.dtc)
        self._act_read_dtc.triggered.connect(self._quick_read_dtc)
        dm.addAction(self._act_read_dtc)

        self._act_read_ecu = QAction("Read ECU Info", self)
        self._act_read_ecu.setShortcut("F7")
        self._act_read_ecu.setEnabled(False)
        self._act_read_ecu.setVisible(profile.features.diagnostics.ecu_info)
        self._act_read_ecu.triggered.connect(self._quick_read_ecu)
        dm.addAction(self._act_read_ecu)

        dm.addSeparator()

        self._act_change_addr = QAction("Change Device Address…", self)
        self._act_change_addr.setEnabled(False)
        self._act_change_addr.triggered.connect(self._change_device_address)
        self._act_change_addr.setVisible(
            profile.features.change_device_address.enabled)
        dm.addAction(self._act_change_addr)

        self._act_ecu_reset = QAction("ECU Reset (Hard)", self)
        self._act_ecu_reset.setEnabled(False)
        self._act_ecu_reset.triggered.connect(self._ecu_reset)
        dm.addAction(self._act_ecu_reset)

        # Help
        hm = mb.addMenu("Help")
        hm.addAction(QAction("About", self, triggered=self._about))

    def _tab_index(self, key: str) -> int:
        """Return current tab index for a given key, or -1 if tab is disabled."""
        label = self._tab_labels.get(key)
        if label is None:
            return -1
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == label:
                return i
        return -1

    def _build_statusbar(self):
        sb = self.statusBar()

        self._lbl_conn = QLabel("  ● Disconnected")
        self._lbl_conn.setStyleSheet("color:#F38BA8; font-weight:bold;")
        sb.addPermanentWidget(self._lbl_conn)

        # Device address indicator — shows which device is connected
        self._lbl_device = QLabel("")
        self._lbl_device.setStyleSheet(
            "color:#89B4FA; font-family:monospace; font-size:11px; "
            "background:#1E1E2E; border:1px solid #313244; "
            "border-radius:4px; padding:1px 6px;")
        self._lbl_device.hide()
        sb.addPermanentWidget(self._lbl_device)

        self._lbl_sess = QLabel("")
        self._lbl_sess.setStyleSheet("color:#6C7086; font-size:11px;")
        sb.addPermanentWidget(self._lbl_sess)

        self._lbl_lock = QLabel("")
        self._lbl_lock.setStyleSheet(
            "color:#FAB387; font-weight:bold; font-size:12px;")
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
        fw_idx = self._tab_index("firmware")
        if fw_idx >= 0:
            self._tabs.setCurrentIndex(fw_idx)
        for i in range(self._tabs.count()):
            if i != fw_idx:
                self._tabs.setTabEnabled(i, False)
        for act in [self._act_connect, self._act_disconnect, self._act_scan,
                    self._act_read_all, self._act_read_dtc, self._act_read_ecu,
                    self._act_change_addr, self._act_ecu_reset]:
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
        self._act_scan.setEnabled(True)
        self._act_read_all.setEnabled(connected)
        self._act_read_dtc.setEnabled(connected)
        self._act_read_ecu.setEnabled(connected)
        self._act_change_addr.setEnabled(connected)
        self._act_ecu_reset.setEnabled(connected)
        if connected:
            self._tp_timer.start()
        self._lbl_lock.setText("")

    # ── Actions ───────────────────────────────────────────────────

    def _load_default_json(self):
        for p in [Path(__file__).parent.parent / "parameters.json",
                  Path.cwd() / "parameters.json"]:
            if p.exists():
                self._store.load_from_json(p)
                if self._param_panel: self._param_panel.refresh_categories()
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
            if self._param_panel: self._param_panel.refresh_categories()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _show_connect(self):
        dlg = ConnectionDialog(self)
        dlg.connected.connect(self._on_connected)
        dlg.exec()

    def _show_scanner(self):
        """Open Device Scanner. Works standalone — no active connection needed."""
        # Need a CAN transport — either active or create a temporary one
        if self._transport and isinstance(self._transport, CANTransport):
            # Use existing connection
            self._run_scanner(self._transport, take_ownership=False)
        else:
            # No CAN connection — ask user to configure CAN first
            QMessageBox.information(
                self, "Device Scanner",
                "Device Scanner requires a CAN connection.\n\n"
                "Connect via CAN transport first, then use\n"
                "Device → Scan CAN Bus for Devices.")

    def _run_scanner(self, transport: CANTransport, take_ownership: bool = False):
        from gui.device_scanner import DeviceScannerDialog
        dlg = DeviceScannerDialog(transport, parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_address is not None:
            addr = dlg.selected_address
            if self._transport and isinstance(self._transport, CANTransport):
                if self._device_address == addr:
                    # Already connected to this device
                    log.info("Already connected to 0x%02X", addr)
                    return
                # Reconnect to different device on same bus
                self._disconnect()
            # Reconnect with new device_address
            log.info("Connecting to device 0x%02X from scanner", addr)
            new_transport = CANTransport(device_address=addr,
                                         tester_address=transport._tester_addr)
            # Reuse same CAN bus — we need to reconnect with new IDs
            # Simplest: ask user to reconnect (avoids rebinding the bus)
            QMessageBox.information(
                self, "Device Selected",
                f"Device address <b>0x{addr:02X}</b> selected.<br><br>"
                f"Use <b>Device → Connect</b> and enter <b>0x{addr:02X}</b> "
                f"as the Device Address to connect.")

    def _on_connected(self, transport: AbstractTransport):
        self._transport = transport
        self._device_address = getattr(transport, '_device_addr', None)
        if self._config_panel:
            self._config_panel.set_device_address(self._device_address)

        if self._client:
            self._client.shutdown()

        self._client = UDSClient(transport, self._store, self)
        if self._param_panel:
            self._client.read_progress.connect(self._param_panel.on_read_progress)
        if self._param_panel:
            self._client.all_read_done.connect(self._param_panel.on_all_read_done)
        if self._param_panel:
            self._client.parameter_written.connect(self._param_panel.on_parameter_written)
        self._client.error_occurred.connect(self._on_error)
        self._client.parameter_written.connect(self._on_parameter_written_history)

        if self._diag_tab: self._diag_tab.set_transport(transport)
        if self._diag_tab and self._diag_tab.session_panel:
            self._diag_tab.session_panel.session_changed.connect(self._on_session_changed)

        from uds.firmware_update import FirmwareUpdater
        self._updater = FirmwareUpdater(transport, parent=self)
        if self._fw_panel: self._fw_panel.set_updater(self._updater)

        # Status bar — show transport + device address
        is_can = isinstance(transport, CANTransport)
        self._lbl_conn.setText(f"  ● {transport.name}")
        self._lbl_conn.setStyleSheet("color:#A6E3A1; font-weight:bold;")

        if is_can and self._device_address is not None:
            tx = getattr(transport, '_tx_id', 0)
            rx = getattr(transport, '_rx_id', 0)
            self._lbl_device.setText(
                f"  Addr: 0x{self._device_address:02X}  "
                f"TX: 0x{tx:08X}  RX: 0x{rx:08X}  ")
            self._lbl_device.show()
        else:
            self._lbl_device.hide()

        self._lbl_sess.setText("  Default Session")
        self._act_connect.setEnabled(False)
        self._act_disconnect.setEnabled(True)
        self._act_read_all.setEnabled(True)
        self._act_read_dtc.setEnabled(True)
        self._act_read_ecu.setEnabled(True)
        self._act_change_addr.setEnabled(is_can)  # only on CAN
        self._act_ecu_reset.setEnabled(True)
        if self._param_panel: self._param_panel.set_connected(True)
        if self._fw_panel: self._fw_panel.set_connected(True)
        self._tp_timer.start()

        log.info("Connected via %s%s — reading all parameters…",
                 transport.name,
                 f" (device 0x{self._device_address:02X})" if self._device_address else "")
        self._read_all()

    def _disconnect(self):
        self._tp_timer.stop()
        if self._client: self._client.shutdown(); self._client = None
        if self._transport: self._transport.disconnect(); self._transport = None
        self._updater = None
        self._device_address = None
        if self._config_panel:
            self._config_panel.set_device_address(None)
        if self._diag_tab: self._diag_tab.set_transport(None)
        if self._fw_panel: self._fw_panel.set_updater(None)

        self._lbl_conn.setText("  ● Disconnected")
        self._lbl_conn.setStyleSheet("color:#F38BA8; font-weight:bold;")
        self._lbl_device.hide()
        self._lbl_sess.setText("")
        self._lbl_tp.setText("")
        self._act_connect.setEnabled(True)
        self._act_disconnect.setEnabled(False)
        self._act_read_all.setEnabled(False)
        self._act_read_dtc.setEnabled(False)
        self._act_read_ecu.setEnabled(False)
        self._act_change_addr.setEnabled(False)
        self._act_ecu_reset.setEnabled(False)
        if self._param_panel: self._param_panel.set_connected(False)
        if self._fw_panel: self._fw_panel.set_connected(False)
        log.info("Disconnected")

    def _read_all(self):
        if self._client:
            self._client.read_all_parameters()

    def _quick_read_dtc(self):
        if not self._diag_tab or not self._diag_tab.dtc_panel: return
        diag_idx = self._tab_index("diag")
        if diag_idx >= 0: self._tabs.setCurrentIndex(diag_idx)
        self._diag_tab._sub.setCurrentIndex(0)
        self._diag_tab.dtc_panel._read_dtcs()

    def _quick_read_ecu(self):
        if not self._diag_tab or not self._diag_tab.ecu_panel: return
        diag_idx = self._tab_index("diag")
        if diag_idx >= 0: self._tabs.setCurrentIndex(diag_idx)
        for i in range(self._diag_tab._sub.count()):
            if "ECU" in self._diag_tab._sub.tabText(i):
                self._diag_tab._sub.setCurrentIndex(i)
                break
        self._diag_tab.ecu_panel._read_all()

    def _change_device_address(self):
        """
        Write new device_address via WDBI on DID 0x000A (NvField::DeviceAddress),
        then prompt user to do ECU Reset so the change takes effect.
        Standard UDS — no custom protocol.
        """
        if not self._client or not isinstance(self._transport, CANTransport):
            return

        current = self._device_address or 0xA0
        val, ok = QInputDialog.getText(
            self, "Change Device Address",
            f"Current address: <b>0x{current:02X}</b><br><br>"
            "Enter new device address (hex, e.g. <b>0xA1</b>):<br>"
            "<small>Range: 0x01–0xFE  (0x00 and 0xFF are reserved)</small>",
            text=f"0x{current:02X}")

        if not ok or not val.strip():
            return

        try:
            new_addr = int(val.strip(), 0)
        except ValueError:
            QMessageBox.warning(self, "Invalid Input",
                                f"'{val}' is not a valid hex address.")
            return

        if not 0x01 <= new_addr <= 0xFE:
            QMessageBox.warning(self, "Invalid Address",
                                "Address must be in range 0x01–0xFE.")
            return

        if new_addr == current:
            QMessageBox.information(self, "No Change",
                                    "New address is the same as current.")
            return

        # Confirm
        r = QMessageBox.question(
            self, "Change Device Address",
            f"Change device address from <b>0x{current:02X}</b> "
            f"to <b>0x{new_addr:02X}</b>?\n\n"
            "This will be written to EEPROM (NvField::DeviceAddress).\n"
            "An ECU Reset is required for the change to take effect.\n\n"
            "After reset, reconnect using the new address.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)

        if r != QMessageBox.Yes:
            return

        # Write via standard WDBI — DID 0x000A, 1 byte value
        log.info("Writing device address 0x%02X → 0x%02X via WDBI DID 0x%04X",
                 current, new_addr, DID_DEVICE_ADDRESS)
        self._client.write_parameter(DID_DEVICE_ADDRESS, new_addr)

        # Prompt for reset
        QMessageBox.information(
            self, "Address Written",
            f"Device address 0x{new_addr:02X} written to EEPROM.\n\n"
            "Use Device → ECU Reset (Hard) to apply the change,\n"
            "then reconnect using the new address 0x{:02X}.".format(new_addr))

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

    def _on_session_changed(self, sess: int):
        if not self._diag_tab or not self._diag_tab.session_panel: return
        from gui.session_panel import SESSION_NAMES
        name = SESSION_NAMES.get(sess, f"0x{sess:02X}")
        colors = {0x01: "#6C7086", 0x02: "#F38BA8", 0x03: "#FAB387"}
        self._lbl_sess.setText(f"  {name}")
        self._lbl_sess.setStyleSheet(
            f"color:{colors.get(sess,'#6C7086')}; font-size:11px;")

    def _keepalive(self):
        if self._client:
            self._client.send_tester_present()
            self._tp_pulse = not self._tp_pulse
            self._lbl_tp.setText("  ◉ TP" if self._tp_pulse else "  ○ TP")

    def _on_config_write(self, did: int, value) -> None:
        """Write from ConfigPanel (batch/undo) — goes direct to client."""
        if self._client:
            self._client.write_parameter(did, value)

    def _on_parameter_written_history(self, did: int) -> None:
        """Record successful write in history."""
        if not self._config_panel:
            return
        pv   = self._store.get_value(did)
        defn = self._store.get_definition(did)
        if pv and defn:
            self._config_panel.on_parameter_written(did, None, pv.value)

    def _on_error(self, msg: str):
        self.statusBar().showMessage(f"⚠ {msg}", 5000)

    def _about(self):
        QMessageBox.about(self, "Device Configurator",
            "<h3>Device Configurator</h3>"
            "<p>UDS motor controller configuration and diagnostics tool.</p>"
            "<b>Transports:</b> Serial · CAN (PEAK) · TCP/IP · Mock<br>"
            "<b>Features:</b> Parameters · Diagnostics · "
            "ECU Info · Firmware · Device Scanner")

    def closeEvent(self, event):
        if (self._fw_panel and self._fw_panel.is_uploading):
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
