"""
Firmware Upload Panel
=====================
- Pamti zadnji direktorij i fajl (QSettings)
- Za vrijeme uploada blokira cijeli UI osim Cancel
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QSettings
from PySide6.QtWidgets import (
    QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget, QSizePolicy, QFrame,
)

log = logging.getLogger(__name__)

SETTINGS_LAST_DIR  = "firmware/last_dir"
SETTINGS_LAST_FILE = "firmware/last_file"


class FirmwarePanel(QWidget):
    """Tab for firmware upload via UDS 0x34/0x36/0x37."""

    # Emitted when upload starts/finishes so main window can lock/unlock UI
    upload_started  = Signal()
    upload_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._updater = None
        self._hex_path: Optional[str] = None
        self._uploading = False
        self._settings = QSettings("BucherHydraulics", "ServoConfigurator")
        self._build_ui()
        self._restore_last_file()

    # ── Build ────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(16)
        root.setContentsMargins(28, 22, 28, 22)

        # ── File selection ────────────────────────────────────────
        file_group = QGroupBox("Firmware File")
        fl = QVBoxLayout(file_group)
        fl.setSpacing(10)

        file_row = QHBoxLayout()
        self._file_label = QLabel("No file selected")
        self._file_label.setStyleSheet("color:#6C7086; font-style:italic;")
        self._file_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._file_label.setWordWrap(True)

        self._browse_btn = QPushButton("📂  Browse…")
        self._browse_btn.setFixedWidth(110)
        self._browse_btn.clicked.connect(self._browse)

        file_row.addWidget(self._file_label, 1)
        file_row.addWidget(self._browse_btn)
        fl.addLayout(file_row)

        # File info line
        self._info_label = QLabel("")
        self._info_label.setStyleSheet("color:#89B4FA; font-size:12px;")
        fl.addWidget(self._info_label)
        root.addWidget(file_group)

        # ── Upload control ────────────────────────────────────────
        ctrl_group = QGroupBox("Upload")
        cl = QVBoxLayout(ctrl_group)
        cl.setSpacing(10)

        btn_row = QHBoxLayout()
        self._upload_btn = QPushButton("⬆  Start Upload")
        self._upload_btn.setObjectName("primaryBtn")
        self._upload_btn.setEnabled(False)
        self._upload_btn.setFixedHeight(38)
        self._upload_btn.clicked.connect(self._start_upload)

        self._cancel_btn = QPushButton("✕  Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setFixedHeight(38)
        self._cancel_btn.setFixedWidth(110)
        self._cancel_btn.clicked.connect(self._cancel)

        btn_row.addWidget(self._upload_btn, 1)
        btn_row.addWidget(self._cancel_btn)
        cl.addLayout(btn_row)

        self._progress = QProgressBar()
        self._progress.setMinimumHeight(24)
        self._progress.setTextVisible(True)
        self._progress.setFormat("  %p%  —  %v / %m bytes")
        self._progress.hide()
        cl.addWidget(self._progress)

        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setMinimumHeight(26)
        self._status_label.setStyleSheet("font-size:13px; font-weight:bold;")
        cl.addWidget(self._status_label)
        root.addWidget(ctrl_group)

        # ── Log ──────────────────────────────────────────────────
        log_group = QGroupBox("Upload Log")
        ll = QVBoxLayout(log_group)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(1000)
        self._log.setStyleSheet(
            "background:#11111B; color:#CDD6F4;"
            "font-family:'Consolas','Courier New',monospace; font-size:11px;"
        )
        self._log.setMinimumHeight(220)
        ll.addWidget(self._log)
        root.addWidget(log_group, 1)

    # ── Public API ───────────────────────────────────────────────

    def set_connected(self, connected: bool):
        if not self._uploading:
            self._browse_btn.setEnabled(connected)
            self._upload_btn.setEnabled(connected and self._hex_path is not None)

    def set_updater(self, updater):
        if self._updater:
            try:
                self._updater.progress.disconnect(self._on_progress)
                self._updater.finished.disconnect(self._on_finished)
            except Exception:
                pass
        self._updater = updater
        if updater:
            updater.progress.connect(self._on_progress)
            updater.finished.connect(self._on_finished)

    @property
    def is_uploading(self) -> bool:
        return self._uploading

    # ── Private ──────────────────────────────────────────────────

    def _restore_last_file(self):
        """Restore last used HEX file path from settings."""
        last = self._settings.value(SETTINGS_LAST_FILE, "")
        if last and Path(last).exists():
            self._load_hex_file(last)

    def _browse(self):
        last_dir = self._settings.value(SETTINGS_LAST_DIR, "")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Firmware HEX File",
            last_dir,                          # ← starts in last used directory
            "Intel HEX Files (*.hex);;All Files (*)"
        )
        if not path:
            return
        self._load_hex_file(path)

    def _load_hex_file(self, path: str):
        """Parse HEX file and update UI. Saves path to settings."""
        try:
            from uds.firmware_update import IntelHexParser
            parser = IntelHexParser()
            segs = parser.parse(path)
            base, flat = parser.flat_binary(segs)
            data_bytes = parser.total_bytes(segs)

            self._hex_path = path

            # Save last used dir and file
            self._settings.setValue(SETTINGS_LAST_DIR,  str(Path(path).parent))
            self._settings.setValue(SETTINGS_LAST_FILE, path)

            # Update UI
            name = Path(path).name
            self._file_label.setText(f"<b>{name}</b>  <span style='color:#6C7086'>{path}</span>")
            self._file_label.setStyleSheet("color:#CDD6F4;")
            info = (
                f"Segments: {len(segs)}    "
                f"Base: 0x{base:08X}    "
                f"Data: {data_bytes:,} B    "
                f"Flat size: {len(flat):,} B"
            )
            self._info_label.setText(info)
            self._info_label.setStyleSheet("color:#89B4FA; font-size:12px;")

            self._log.clear()
            self._status_label.setText("")
            self._progress.hide()
            self._append_log(f"Loaded: {name}")
            self._append_log(info)
            self._upload_btn.setEnabled(True)

        except Exception as e:
            self._info_label.setText(f"Parse error: {e}")
            self._info_label.setStyleSheet("color:#F38BA8; font-size:12px;")
            self._hex_path = None
            self._upload_btn.setEnabled(False)
            log.error("HEX parse error: %s", e)

    def _start_upload(self):
        if not self._hex_path or not self._updater:
            return
        try:
            self._updater.load_hex(self._hex_path)
        except Exception as e:
            self._set_status(f"✗ Load failed: {e}", error=True)
            return

        # Lock UI
        self._uploading = True
        self._browse_btn.setEnabled(False)
        self._upload_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._log.clear()
        self._progress.show()
        self._progress.setMaximum(len(open(self._hex_path, 'rb').read()))  # approx
        self._progress.setValue(0)
        self._set_status("Uploading…")
        self._append_log(f"Starting: {Path(self._hex_path).name}")

        self.upload_started.emit()   # → main window locks tabs
        self._updater.start()

    def _cancel(self):
        if self._updater:
            self._updater.cancel()
        self._cancel_btn.setEnabled(False)
        self._set_status("Cancelling…")

    def _on_progress(self, percent: int, message: str):
        self._progress.setValue(percent)
        self._progress.setMaximum(100)
        self._progress.setFormat(f"  {percent}%  —  {message[:50]}")
        self._set_status(message)
        self._append_log(f"[{percent:3d}%] {message}")

    def _on_finished(self, success: bool, message: str):
        self._uploading = False
        self._cancel_btn.setEnabled(False)
        self._upload_btn.setEnabled(self._hex_path is not None)
        self._browse_btn.setEnabled(True)

        if success:
            self._progress.setValue(100)
            self._progress.setFormat("  100%  — Complete")
            self._set_status("✓ Upload complete", error=False)
        else:
            self._set_status(f"✗ {message}", error=True)

        self._append_log(f"{'✓' if success else '✗'} {message}")
        self.upload_finished.emit()   # → main window unlocks tabs

    def _set_status(self, text: str, error: bool = False):
        color = "#F38BA8" if error else "#A6E3A1"
        self._status_label.setStyleSheet(
            f"font-size:13px; font-weight:bold; color:{color};")
        self._status_label.setText(text)

    def _append_log(self, text: str):
        self._log.appendPlainText(text)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())
