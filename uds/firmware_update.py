"""
Firmware Update
===============
Implements UDS-based firmware download sequence:

    1. 0x10 0x02  DiagnosticSessionControl ? Programming Session
    2. 0x27 0x01  SecurityAccess ? RequestSeed
    3. 0x27 0x02  SecurityAccess ? SendKey
    4. 0x31 0x01  RoutineControl ? Start  (erase flash)
    5. 0x34       RequestDownload          (announce transfer)
    6. 0x36 �     TransferData             (blocks)
    7. 0x37       RequestTransferExit      (finalize)
    8. 0x31 0x01  RoutineControl ? Start  (verify checksum/CRC)
    9. 0x11 0x01  ECUReset ? HardReset

Intel HEX parsing is done internally � no external library required.
Supports HEX record types 00 (Data), 01 (EOF), 02 (Extended Segment),
03 (Start Segment), 04 (Extended Linear Address), 05 (Start Linear).

Usage example:
    updater = FirmwareUpdater(transport)
    updater.progress.connect(on_progress)   # (percent, message)
    updater.finished.connect(on_done)       # (success, message)
    updater.load_hex("firmware.hex")
    updater.start()   # non-blocking, runs in QThread
"""
from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal, Slot

from transport.transport import AbstractTransport, TransportError
from uds.codec import (
    NRC, ResetType, ServiceID, SessionType,
    UDSCodec, UDSDecodeError, UDSNegativeResponse,
)

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Intel HEX parser                                                    #
# ------------------------------------------------------------------ #

@dataclass
class HexSegment:
    """A contiguous block of data with its base address."""
    address: int
    data: bytearray = field(default_factory=bytearray)

    def end_address(self) -> int:
        return self.address + len(self.data)


class IntelHexError(Exception):
    pass


class IntelHexParser:
    """
    Parses Intel HEX files into a list of contiguous HexSegments.
    Supports record types 00�05.
    """

    def parse(self, path: str | Path) -> List[HexSegment]:
        segments: List[HexSegment] = []
        current: Optional[HexSegment] = None
        upper_address = 0  # extended linear address (type 04)

        with open(path, "r", encoding="ascii") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                if line[0] != ":":
                    raise IntelHexError(f"Line {lineno}: missing ':' start code")

                try:
                    raw = bytes.fromhex(line[1:])
                except ValueError as e:
                    raise IntelHexError(f"Line {lineno}: invalid hex: {e}")

                if len(raw) < 5:
                    raise IntelHexError(f"Line {lineno}: record too short")

                byte_count = raw[0]
                address    = (raw[1] << 8) | raw[2]
                rec_type   = raw[3]
                data       = raw[4 : 4 + byte_count]
                checksum   = raw[4 + byte_count]

                # Verify checksum
                calc = (-(sum(raw[:-1]) & 0xFF)) & 0xFF
                if calc != checksum:
                    raise IntelHexError(
                        f"Line {lineno}: checksum error (got 0x{checksum:02X}, "
                        f"expected 0x{calc:02X})"
                    )

                if rec_type == 0x00:  # Data
                    abs_address = upper_address + address
                    if current is None or abs_address != current.end_address():
                        current = HexSegment(address=abs_address)
                        segments.append(current)
                    current.data.extend(data)

                elif rec_type == 0x01:  # End Of File
                    break

                elif rec_type == 0x02:  # Extended Segment Address
                    upper_address = ((data[0] << 8) | data[1]) << 4
                    current = None

                elif rec_type == 0x04:  # Extended Linear Address
                    upper_address = ((data[0] << 8) | data[1]) << 16
                    current = None

                elif rec_type in (0x03, 0x05):  # Start addresses � ignore
                    pass

                else:
                    log.warning("Line %d: unknown record type 0x%02X, skipping", lineno, rec_type)

        return segments

    def flat_binary(self, segments: List[HexSegment]) -> Tuple[int, bytes]:
        """
        Returns (base_address, flat_bytes) covering the full address range.
        Gaps between segments are filled with 0xFF (erased flash pattern).
        """
        if not segments:
            raise IntelHexError("No segments found")
        base = min(s.address for s in segments)
        end  = max(s.end_address() for s in segments)
        buf = bytearray(b"\xFF" * (end - base))
        for seg in segments:
            offset = seg.address - base
            buf[offset : offset + len(seg.data)] = seg.data
        return base, bytes(buf)

    def total_bytes(self, segments: List[HexSegment]) -> int:
        return sum(len(s.data) for s in segments)


# ------------------------------------------------------------------ #
#  Security Access key derivation                                      #
# ------------------------------------------------------------------ #

def default_key_from_seed(seed: bytes, level: int = 0x01) -> bytes:
    """
    Security Access seed -> key derivation.

    BL library (l3/security_access.hpp):
        key = HMAC-SHA256(per_level_secret, seed)[0:4]

    Levels: 0x01=User, 0x02=Manufacturer, 0x03=Developer
    Firmware update needs Manufacturer (0x02).

    !! Replace secrets with real provisioned values from your HSM !!
    """
    import hmac
    import hashlib
    # Per-level HMAC secrets — REPLACE with real OTP provisioned values
    secrets = {
        0x01: bytes(32),   # User         — placeholder (32 zero bytes)
        0x02: bytes(32),   # Manufacturer — placeholder (32 zero bytes)
        0x03: bytes(32),   # Developer    — placeholder (32 zero bytes)
    }
    secret = secrets.get(level, bytes(32))
    digest = hmac.new(secret, seed, hashlib.sha256).digest()
    return digest[:4]  # truncated to 4 bytes per BL library


# ------------------------------------------------------------------ #
#  Routine IDs                                                         #
# ------------------------------------------------------------------ #

class RoutineID:
    ERASE_FLASH              = 0xFF00  # EraseMemory  (l4/uds_service_table.hpp)
    CHECK_MEMORY             = 0xFF04  # CheckMemory  (l4/uds_service_table.hpp)
    ERASE_OPTION_APPLICATION = 0x11    # VinBT-355/500: erase Application
    ERASE_OPTION_CBL         = 0xAA    # VinBT-355/500: erase CustomerBootloader


# ------------------------------------------------------------------ #
#  Firmware Updater Worker                                             #
# ------------------------------------------------------------------ #

class _UpdateWorker(QObject):
    """Runs in a QThread. Executes the full update sequence."""

    progress  = Signal(int, str)   # percent (0-100), status message
    finished  = Signal(bool, str)  # success, message

    # Block size for TransferData payload (adjust to match ECU buffer)
    BLOCK_SIZE = 256

    def __init__(self, transport: AbstractTransport,
                 segments: List[HexSegment],
                 base_address: int,
                 flat_data: bytes,
                 key_fn: Callable[[bytes, int], bytes]):
        super().__init__()
        self._transport  = transport
        self._segments   = segments
        self._base_addr  = base_address
        self._flat_data  = flat_data
        self._key_fn     = key_fn
        self._codec      = UDSCodec()
        self._cancelled  = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            self._execute()
        except UDSNegativeResponse as e:
            msg = f"UDS error: NRC 0x{e.nrc:02X} � {NRC.description(e.nrc)}"
            log.error(msg)
            self.finished.emit(False, msg)
        except TransportError as e:
            msg = f"Transport error: {e}"
            log.error(msg)
            self.finished.emit(False, msg)
        except Exception as e:
            msg = f"Unexpected error: {e}"
            log.exception(msg)
            self.finished.emit(False, msg)

    def _execute(self) -> None:
        total_bytes = len(self._flat_data)

        # -- Step 1: Programming session -----------------------------
        self._report(0, "Switching to programming session�")
        self._send(self._codec.encode_diagnostic_session_control(
            SessionType.PROGRAMMING))

        # -- Step 2-3: Security Access --------------------------------
        self._report(2, "Security Access: requesting seed�")
        resp = self._send(self._codec.encode_security_access_request_seed(0x01))
        seed = resp.get("seed", b"")
        if not seed:
            raise TransportError("ECU returned empty seed")

        key = self._key_fn(seed, 0x01)
        self._report(4, "Security Access: sending key�")
        self._send(self._codec.encode_security_access_send_key(0x01, key))

        # -- Step 4: Erase flash (RoutineControl 0xFF00) --------------
        self._report(6, f"Erasing flash at 0x{self._base_addr:08X}�")
        # Option byte: 0x11=Application, 0xAA=CBL (VinBT-355/500)
        erase_data = struct.pack(">II", self._base_addr, total_bytes) + bytes([RoutineID.ERASE_OPTION_APPLICATION])
        self._send_routine(RoutineID.ERASE_FLASH, erase_data, timeout=15.0)

        # -- Step 5: RequestDownload ----------------------------------
        self._report(10, f"Requesting download ({total_bytes} bytes)�")
        rd_req = self._encode_request_download(
            address=self._base_addr,
            length=total_bytes,
            compression=0x00,
            encrypting=0x00,
        )
        rd_resp = self._raw_send(rd_req, timeout=2.0)
        max_block = self._decode_request_download_response(rd_resp)
        block_size = min(self.BLOCK_SIZE, max_block - 2)  # -2 for SID + block counter
        log.info("Download accepted. Negotiated block size: %d bytes", block_size)

        # -- Step 6: TransferData -------------------------------------
        self._report(12, "Transferring firmware�")
        offset = 0
        block_seq = 1
        while offset < total_bytes:
            if self._cancelled:
                self.finished.emit(False, "Cancelled by user")
                return

            chunk = self._flat_data[offset : offset + block_size]
            td_req = bytes([ServiceID.TRANSFER_DATA, block_seq & 0xFF]) + chunk
            self._raw_send(td_req, timeout=5.0)

            offset    += len(chunk)
            block_seq  = (block_seq % 0xFF) + 1

            percent = 12 + int(offset / total_bytes * 78)  # 12% ? 90%
            self._report(percent,
                         f"Transferring� {offset}/{total_bytes} bytes "
                         f"({offset*100//total_bytes}%)")

        # -- Step 7: RequestTransferExit ------------------------------
        self._report(91, "Finalizing transfer�")
        self._raw_send(bytes([ServiceID.REQUEST_TRANSFER_EXIT]), timeout=5.0)

        # -- Step 8: Verify integrity (RoutineControl 0xFF01) ---------
        self._report(93, "Verifying integrity�")
        verify_data = struct.pack(">II", self._base_addr, total_bytes)
        self._send_routine(RoutineID.CHECK_MEMORY, verify_data, timeout=10.0)

        # -- Step 9: ECU Reset ----------------------------------------
        self._report(98, "Resetting ECU�")
        try:
            self._raw_send(self._codec.encode_ecu_reset(ResetType.HARD_RESET), timeout=2.0)
        except TransportError:
            pass  # ECU may reset before sending response � that's OK

        self._report(100, "Firmware update complete ?")
        self.finished.emit(True, "Firmware updated successfully")

    # -- Helpers -----------------------------------------------------

    def _report(self, percent: int, msg: str) -> None:
        log.info("[FW %3d%%] %s", percent, msg)
        self.progress.emit(percent, msg)

    def _send(self, req: bytes, timeout: float = 2.0) -> dict:
        """Send request, decode response, return decoded dict."""
        raw = self._raw_send(req, timeout)
        return self._codec.decode_response(raw)

    def _raw_send(self, req: bytes, timeout: float = 2.0) -> bytes:
        return self._transport.send_and_wait(req, timeout=timeout)

    def _send_routine(self, routine_id: int, routine_data: bytes = b"",
                      timeout: float = 5.0) -> dict:
        req = struct.pack(">BHH", ServiceID.ROUTINE_CONTROL, 0x0101, routine_id)
        # sub-function 0x01 = startRoutine; routine_id = 2 bytes; then option record
        req = bytes([ServiceID.ROUTINE_CONTROL, 0x01]) + \
              struct.pack(">H", routine_id) + routine_data
        raw = self._raw_send(req, timeout)
        return self._codec.decode_response(raw)

    @staticmethod
    def _encode_request_download(address: int, length: int,
                                  compression: int = 0,
                                  encrypting: int = 0) -> bytes:
        """
        ISO 14229-1 �14.3 RequestDownload (0x34)
        dataFormatIdentifier:  [comp(4) | encr(4)]
        addressAndLengthFormatIdentifier: [memLen(4) | memAddr(4)]
        Both address and length encoded as 4 bytes (big-endian).
        """
        dfi  = ((compression & 0x0F) << 4) | (encrypting & 0x0F)
        alfi = 0x44  # 4 bytes address, 4 bytes length
        payload = (
            bytes([ServiceID.REQUEST_DOWNLOAD, dfi, alfi])
            + struct.pack(">I", address)
            + struct.pack(">I", length)
        )
        return payload

    @staticmethod
    def _decode_request_download_response(raw: bytes) -> int:
        """Returns maxNumberOfBlockLength from 0x74 response."""
        if len(raw) < 2:
            raise UDSDecodeError("RequestDownload response too short")
        if raw[0] != (ServiceID.REQUEST_DOWNLOAD | 0x40):
            raise UDSDecodeError(f"Unexpected SID in RD response: 0x{raw[0]:02X}")
        length_format = raw[1]
        n_bytes = (length_format >> 4) & 0x0F
        if len(raw) < 2 + n_bytes:
            raise UDSDecodeError("RequestDownload response truncated")
        max_block = int.from_bytes(raw[2 : 2 + n_bytes], "big")
        return max_block if max_block > 0 else 0x100


# ------------------------------------------------------------------ #
#  Add missing ServiceIDs to codec                                     #
# ------------------------------------------------------------------ #

# Extend ServiceID with transfer services not in original codec
ServiceID.ROUTINE_CONTROL        = 0x31
ServiceID.REQUEST_DOWNLOAD       = 0x34
ServiceID.TRANSFER_DATA          = 0x36
ServiceID.REQUEST_TRANSFER_EXIT  = 0x37


# ------------------------------------------------------------------ #
#  Public FirmwareUpdater facade                                       #
# ------------------------------------------------------------------ #

class FirmwareUpdater(QObject):
    """
    Main-thread facade for firmware update operations.

    Signals:
        progress(percent: int, message: str)
        finished(success: bool, message: str)

    Example:
        updater = FirmwareUpdater(transport)
        updater.progress.connect(lambda p, m: print(f"{p}% {m}"))
        updater.finished.connect(lambda ok, m: print("Done:", m))
        updater.load_hex("build/firmware.hex")
        updater.start()
    """

    progress = Signal(int, str)
    finished = Signal(bool, str)

    def __init__(self, transport: AbstractTransport,
                 key_fn: Optional[Callable[[bytes, int], bytes]] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._transport  = transport
        self._key_fn     = key_fn or default_key_from_seed
        self._segments:  List[HexSegment] = []
        self._base_addr: int = 0
        self._flat_data: bytes = b""
        self._thread:    Optional[QThread] = None
        self._worker:    Optional[_UpdateWorker] = None

    # -- Public API --------------------------------------------------

    def load_hex(self, path: str | Path) -> None:
        """
        Parse Intel HEX file. Call before start().
        Raises IntelHexError on parse failure.
        """
        parser = IntelHexParser()
        self._segments = parser.parse(path)
        self._base_addr, self._flat_data = parser.flat_binary(self._segments)

        total = parser.total_bytes(self._segments)
        log.info(
            "HEX loaded: %d segment(s), base=0x%08X, "
            "flat size=%d bytes, data bytes=%d",
            len(self._segments), self._base_addr,
            len(self._flat_data), total,
        )

    @property
    def is_loaded(self) -> bool:
        return len(self._flat_data) > 0

    @property
    def firmware_size(self) -> int:
        return len(self._flat_data)

    @property
    def base_address(self) -> int:
        return self._base_addr

    @property
    def segments(self) -> List[HexSegment]:
        return list(self._segments)

    def start(self) -> None:
        """Start firmware update in background thread."""
        if not self.is_loaded:
            self.finished.emit(False, "No firmware loaded. Call load_hex() first.")
            return
        if self._thread and self._thread.isRunning():
            self.finished.emit(False, "Update already in progress.")
            return

        self._thread = QThread(self)
        self._worker = _UpdateWorker(
            transport=self._transport,
            segments=self._segments,
            base_address=self._base_addr,
            flat_data=self._flat_data,
            key_fn=self._key_fn,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.finished.connect(self._on_worker_finished)
        self._thread.start()

    def cancel(self) -> None:
        if self._worker:
            self._worker.cancel()

    # -- Private -----------------------------------------------------

    def _on_worker_finished(self, success: bool, message: str) -> None:
        self.finished.emit(success, message)
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)