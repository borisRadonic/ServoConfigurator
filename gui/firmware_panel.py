"""
Firmware Upload Panel
=====================
Odabir HEX fajla, prikaz info, progress bar, log, start/cancel.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget, QSizePolicy,
)

log = logging.getLogger(__name__)


class FirmwarePanel(QWidget):
    """Tab for firmware upload via UDS 0x34/0x36/0x37."""

    upload_requested = Signal(str)   # hex file path

    def __init__(self, parent=None):
        super().__init__(parent)
        self._updater = None
        self._hex_path: Optional[str] = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(16)
        root.setContentsMargins(24, 20, 24, 20)

        # ── File selection ────────────────────────────────────────
        file_group = QGroupBox("Firmware File")
        fl = QVBoxLayout(file_group)

        file_row = QHBoxLayout()
        self._file_label = QLabel("No file selected")
        self._file_label.setStyleSheet("color:#6C7086; font-style:italic;")
        self._file_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(100)
        browse_btn.clicked.connect(self._browse)

        file_row.addWidget(self._file_label, 1)
        file_row.addWidget(browse_btn)
        fl.addLayout(file_row)

        # File info
        self._info_label = QLabel("")
        self._info_label.setStyleSheet("color:#89B4FA; font-size:12px;")
        fl.addWidget(self._info_label)
        root.addWidget(file_group)

        # ── Upload control ────────────────────────────────────────
        ctrl_group = QGroupBox("Upload")
        cl = QVBoxLayout(ctrl_group)

        btn_row = QHBoxLayout()
        self._upload_btn = QPushButton("⬆  Start Upload")
        self._upload_btn.setObjectName("primaryBtn")
        self._upload_btn.setEnabled(False)
        self._upload_btn.setFixedHeight(36)
        self._upload_btn.clicked.connect(self._start_upload)

        self._cancel_btn = QPushButton("✕  Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setFixedHeight(36)
        self._cancel_btn.setFixedWidth(100)
        self._cancel_btn.clicked.connect(self._cancel)

        btn_row.addWidget(self._upload_btn, 1)
        btn_row.addWidget(self._cancel_btn)
        cl.addLayout(btn_row)

        self._progress = QProgressBar()
        self._progress.setMinimumHeight(22)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%  %v / %m bytes")
        self._progress.hide()
        cl.addWidget(self._progress)

        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setStyleSheet("font-size:13px; font-weight:bold;")
        cl.addWidget(self._status_label)
        root.addWidget(ctrl_group)

        # ── Log ──────────────────────────────────────────────────
        log_group = QGroupBox("Upload Log")
        ll = QVBoxLayout(log_group)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        self._log.setStyleSheet(
            "background:#11111B; color:#CDD6F4; "
            "font-family:'Consolas','Courier New',monospace; font-size:11px;"
        )
        self._log.setMinimumHeight(200)
        ll.addWidget(self._log)
        root.addWidget(log_group, 1)

    # ── Public API ───────────────────────────────────────────────

    def set_connected(self, connected: bool):
        self._update_upload_btn()

    def set_updater(self, updater):
        """Inject FirmwareUpdater instance from main window."""
        if self._updater:
            self._updater.progress.disconnect()
            self._updater.finished.disconnect()
        self._updater = updater
        if updater:
            updater.progress.connect(self._on_progress)
            updater.finished.connect(self._on_finished)

    # ── Private ──────────────────────────────────────────────────

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Firmware HEX File", "",
            "Intel HEX Files (*.hex);;All Files (*)"
        )
        if not path:
            return
        self._hex_path = path
        name = Path(path).name
        self._file_label.setText(name)
        self._file_label.setStyleSheet("color:#CDD6F4;")
        self._log.clear()
        self._status_label.setText("")
        self._progress.hide()

        # Parse and show info
        try:
            from uds.firmware_update import IntelHexParser
            parser = IntelHexParser()
            segs = parser.parse(path)
            base, flat = parser.flat_binary(segs)
            data_bytes = parser.total_bytes(segs)
            info = (f"Segments: {len(segs)}    "
                    f"Base: 0x{base:08X}    "
                    f"Data: {data_bytes:,} bytes    "
                    f"Flat size: {len(flat):,} bytes")
            self._info_label.setText(info)
            self._append_log(f"Loaded: {name}")
            self._append_log(info)
        except Exception as e:
            self._info_label.setText(f"Parse error: {e}")
            self._info_label.setStyleSheet("color:#F38BA8; font-size:12px;")
            self._hex_path = None

        self._update_upload_btn()

    def _update_upload_btn(self):
        has_file = self._hex_path is not None
        self._upload_btn.setEnabled(has_file)

    def _start_upload(self):
        if not self._hex_path or not self._updater:
            return
        try:
            self._updater.load_hex(self._hex_path)
        except Exception as e:
            self._set_status(f"✗ Load failed: {e}", error=True)
            return

        self._log.clear()
        self._progress.show()
        self._progress.setValue(0)
        self._upload_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._set_status("Uploading…", error=False)
        self._append_log(f"Starting upload: {Path(self._hex_path).name}")
        self._updater.start()

    def _cancel(self):
        if self._updater:
            self._updater.cancel()
        self._cancel_btn.setEnabled(False)
        self._set_status("Cancelling…", error=False)

    def _on_progress(self, percent: int, message: str):
        self._progress.setValue(percent)
        self._append_log(f"[{percent:3d}%] {message}")
        self._set_status(message)

    def _on_finished(self, success: bool, message: str):
        self._progress.setValue(100 if success else self._progress.value())
        self._upload_btn.setEnabled(self._hex_path is not None)
        self._cancel_btn.setEnabled(False)
        if success:
            self._set_status("✓ Upload complete", error=False)
            self._progress.hide()
        else:
            self._set_status(f"✗ {message}", error=True)
        self._append_log(message)

    def _set_status(self, text: str, error: bool = False):
        color = "#F38BA8" if error else "#A6E3A1"
        self._status_label.setStyleSheet(
            f"font-size:13px; font-weight:bold; color:{color};")
        self._status_label.setText(text)

    def _append_log(self, text: str):
        self._log.appendPlainText(text)
        self._log.verticalScrollBar().setValue(
            self._log.verticalScrollBar().maximum())
