"""
Transport Layer
===============
Defines the abstract Transport interface and concrete implementations:
    - SerialTransport  (pyserial, framed with length prefix)
    - CANTransport     (PEAK USB-CAN via python-can, ISO 15765-2 TP)

The framing for Serial uses a simple length-prefixed protocol:
    [0xAA] [0x55] [LEN_HI] [LEN_LO] [PAYLOAD...] [CRC16]

This is a clean, extensible design – swap the transport without
touching any UDS or application code.
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  CRC-16/CCITT helper                                                 #
# ------------------------------------------------------------------ #

def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


# ------------------------------------------------------------------ #
#  Abstract base                                                       #
# ------------------------------------------------------------------ #

class TransportError(Exception):
    """Any transport-level error."""


class AbstractTransport(ABC):
    """
    Contract for a UDS transport channel.

    Implementations must be thread-safe: send() may be called from a
    worker thread, and the response_callback will be called from the
    receiver thread.
    """

    def __init__(self):
        self._response_callback: Optional[Callable[[bytes], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None

    def set_response_callback(self, cb: Callable[[bytes], None]) -> None:
        self._response_callback = cb

    def set_error_callback(self, cb: Callable[[str], None]) -> None:
        self._error_callback = cb

    @abstractmethod
    def connect(self, **kwargs) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def send(self, payload: bytes) -> None: ...

    def set_scan_callback(self, cb) -> None:
        """Register callback(device_addr: int, data: bytes) for scanner.
        Set to None to stop scanning. Thread-safe via GIL.
        """
        self._scan_callback = cb

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes: ...

    @property
    @abstractmethod
    def name(self) -> str: ...


# ------------------------------------------------------------------ #
#  Serial Transport                                                    #
# ------------------------------------------------------------------ #

SERIAL_SOF = b"\xAA\x55"

class SerialTransport(AbstractTransport):
    """
    UDS over RS-232/USB-UART with length-prefixed framing.

    Frame structure (all big-endian):
        0xAA 0x55  – start-of-frame
        uint16     – payload length (bytes)
        payload    – UDS PDU
        uint16     – CRC-16/CCITT of payload

    Adjust framing to match your firmware if needed.
    """

    def __init__(self):
        super().__init__()
        self._serial = None
        self._rx_thread: Optional[threading.Thread] = None
        self._running = False
        self._response_event = threading.Event()
        self._last_response: Optional[bytes] = None
        self._lock = threading.Lock()
        self._scan_callback = None

    @property
    def name(self) -> str:
        return "Serial"

    def connect(self, port: str, baudrate: int = 115200, timeout: float = 0.1, **kwargs) -> None:
        try:
            import serial
        except ImportError:
            raise TransportError("pyserial not installed. Run: pip install pyserial")

        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
            )
            self._running = True
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()
            log.info("Serial connected: %s @ %d baud", port, baudrate)
        except Exception as e:
            raise TransportError(f"Failed to open {port}: {e}") from e

    def disconnect(self) -> None:
        self._running = False
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        log.info("Serial disconnected")

    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def _frame(self, payload: bytes) -> bytes:
        crc = crc16_ccitt(payload)
        return SERIAL_SOF + struct.pack(">H", len(payload)) + payload + struct.pack(">H", crc)

    def send(self, payload: bytes) -> None:
        if not self.is_connected():
            raise TransportError("Not connected")
        frame = self._frame(payload)
        with self._lock:
            self._serial.write(frame)

    def set_scan_callback(self, cb) -> None:
        """Register callback(device_addr: int, data: bytes) for scanner.
        Set to None to stop scanning. Thread-safe via GIL.
        """
        self._scan_callback = cb

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes:
        self._response_event.clear()
        self._last_response = None
        self.send(payload)
        if not self._response_event.wait(timeout):
            raise TransportError(f"Timeout waiting for response ({timeout}s)")
        if self._last_response is None:
            raise TransportError("No response data")
        return self._last_response

    def _rx_loop(self) -> None:
        buf = bytearray()
        while self._running:
            try:
                chunk = self._serial.read(256)
                if chunk:
                    buf.extend(chunk)
                    self._process_buffer(buf)
            except Exception as e:
                if self._running:
                    log.error("Serial RX error: %s", e)
                    if self._error_callback:
                        self._error_callback(str(e))

    def _process_buffer(self, buf: bytearray) -> None:
        while True:
            # Find SOF
            idx = buf.find(SERIAL_SOF)
            if idx == -1:
                buf.clear()
                return
            if idx > 0:
                del buf[:idx]

            # Need at least SOF(2) + len(2) + CRC(2) = 6 bytes min
            if len(buf) < 6:
                return

            payload_len = struct.unpack(">H", buf[2:4])[0]
            total = 2 + 2 + payload_len + 2

            if len(buf) < total:
                return

            payload = bytes(buf[4 : 4 + payload_len])
            crc_recv = struct.unpack(">H", buf[4 + payload_len : total])[0]
            crc_calc = crc16_ccitt(payload)

            del buf[:total]

            if crc_recv != crc_calc:
                log.warning("CRC mismatch: recv=0x%04X calc=0x%04X", crc_recv, crc_calc)
                continue

            self._last_response = payload
            self._response_event.set()
            if self._response_callback:
                self._response_callback(payload)


# ------------------------------------------------------------------ #
#  CAN Transport (ISO 15765-2 / ISO-TP)                               #
# ------------------------------------------------------------------ #

class CANTransport(AbstractTransport):
    """
    UDS over CAN using ISO 15765-2 transport protocol (ISO-TP).
    Requires: pip install python-can

    For PEAK USB-CAN adapters, also install:
        pip install python-can[pcan]  (Windows)
        or the PEAK Linux driver with peak_usb kernel module.

    ISO-TP frame types:
        Single Frame (SF):    1 + data (up to 7 bytes)
        First Frame (FF):     2 header + data start
        Consecutive Frame:    1 seq + data
        Flow Control (FC):    3 bytes

    This implementation handles single-frame and multi-frame for
    UDS payloads up to ~4095 bytes.
    """

    # CAN IDs — 29-bit extended, ISO 15765-2 / SAE J1939
    # Physical: 0x18DA<TA><SA>  (VinBT-263)
    # device_address=0xA0 (DEVICE_ADDRESS_VAL, VinBT-259/260)
    # tester_address=0xF1 (standard OBD tester SA)
    TESTER_ADDRESS = 0xF1

    def __init__(self, device_address: int = 0xA0,
                 tester_address: int = TESTER_ADDRESS):
        super().__init__()
        self._device_addr = device_address
        self._tester_addr = tester_address
        self._tx_id   = 0x18DA0000 | ((device_address & 0xFF) << 8) | (tester_address & 0xFF)
        self._rx_id   = 0x18DA0000 | ((tester_address & 0xFF) << 8) | (device_address & 0xFF)
        self._func_id = 0x18DB3300 | (tester_address & 0xFF)
        log.info("CAN IDs: TX=0x%08X RX=0x%08X FUNC=0x%08X",
                 self._tx_id, self._rx_id, self._func_id)
        self._bus = None
        self._running = False
        self._rx_thread: Optional[threading.Thread] = None
        self._response_event = threading.Event()
        self._last_response: Optional[bytes] = None
        self._rx_buffer: bytearray = bytearray()
        self._rx_expected_len: int = 0
        self._rx_seq: int = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "CAN"

    def connect(self, interface: str = "pcan", channel: str = "PCAN_USBBUS1",
                bitrate: int = 250000, **kwargs) -> None:
        try:
            import can
        except ImportError:
            raise TransportError("python-can not installed. Run: pip install python-can")

        try:
            self._bus = can.interface.Bus(
                interface=interface,
                channel=channel,
                bitrate=bitrate,
            )
            self._running = True
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()
            log.info("CAN connected: %s %s @ %d bps  TX=0x%08X RX=0x%08X",
                     interface, channel, bitrate, self._tx_id, self._rx_id)
        except Exception as e:
            raise TransportError(f"Failed to open CAN {interface}/{channel}: {e}") from e

    def disconnect(self) -> None:
        self._running = False
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        if self._bus:
            self._bus.shutdown()
        log.info("CAN disconnected")

    def is_connected(self) -> bool:
        return self._bus is not None and self._running

    def _send_flow_control(self) -> None:
        """Send ISO-TP Flow Control (ContinueToSend)."""
        import can
        fc_data = bytearray(8)
        fc_data[0] = 0x30  # FC, ContinueToSend
        fc_data[1] = 0x00  # block size = 0 (unlimited)
        fc_data[2] = 0x00  # STmin = 0ms
        msg = can.Message(arbitration_id=self._tx_id, data=fc_data, is_extended_id=True)
        self._bus.send(msg)

    def send_functional(self, payload: bytes) -> None:
        """Broadcast via functional address 0x18DB33<SA> (VinBT-264)."""
        if not self.is_connected():
            raise TransportError("CAN not connected")
        import can
        if len(payload) <= 7:
            data = bytearray(8)
            data[0] = len(payload)
            data[1:1+len(payload)] = payload
            msg = can.Message(arbitration_id=self._func_id, data=data, is_extended_id=True)
            with self._lock:
                self._bus.send(msg)

    def send(self, payload: bytes) -> None:
        if not self.is_connected():
            raise TransportError("CAN not connected")
        import can

        with self._lock:
            if len(payload) <= 7:
                # Single Frame
                data = bytearray(8)
                data[0] = len(payload)  # SF, length in nibble 0
                data[1 : 1 + len(payload)] = payload
                msg = can.Message(arbitration_id=self._tx_id, data=data, is_extended_id=True)
                self._bus.send(msg)
            else:
                # Multi-Frame: First Frame
                data = bytearray(8)
                total = len(payload)
                data[0] = 0x10 | ((total >> 8) & 0x0F)
                data[1] = total & 0xFF
                data[2:8] = payload[0:6]
                msg = can.Message(arbitration_id=self._tx_id, data=data, is_extended_id=True)
                self._bus.send(msg)

                # Wait for Flow Control
                time.sleep(0.02)

                # Consecutive Frames
                seq = 1
                offset = 6
                while offset < total:
                    chunk = payload[offset : offset + 7]
                    data = bytearray(8)
                    data[0] = 0x20 | (seq & 0x0F)
                    data[1 : 1 + len(chunk)] = chunk
                    msg = can.Message(arbitration_id=self._tx_id, data=data, is_extended_id=True)
                    self._bus.send(msg)
                    offset += 7
                    seq = (seq + 1) & 0x0F
                    time.sleep(0.001)

    def set_scan_callback(self, cb) -> None:
        """Register callback(device_addr: int, data: bytes) for scanner.
        Set to None to stop scanning. Thread-safe via GIL.
        """
        self._scan_callback = cb

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes:
        self._response_event.clear()
        self._last_response = None
        self._rx_buffer = bytearray()
        self._rx_expected_len = 0
        self.send(payload)
        if not self._response_event.wait(timeout):
            raise TransportError(f"CAN timeout waiting for response ({timeout}s)")
        if self._last_response is None:
            raise TransportError("No CAN response data")
        return self._last_response

    def _rx_loop(self) -> None:
        while self._running:
            try:
                msg = self._bus.recv(timeout=0.1)
                if not msg:
                    continue
                # Normal UDS response for active connection
                if msg.arbitration_id == self._rx_id:
                    self._process_can_frame(bytes(msg.data))
                # Scanner callback: receive any 0x18DAF1xx frame
                # (physical replies to our tester addr 0xF1)
                elif self._scan_callback is not None:
                    if (msg.arbitration_id & 0xFFFFFF00) == 0x18DAF100:
                        device_addr = msg.arbitration_id & 0xFF
                        self._scan_callback(device_addr, bytes(msg.data))
            except Exception as e:
                if self._running:
                    log.error("CAN RX error: %s", e)
                    if self._error_callback:
                        self._error_callback(str(e))

    def _process_can_frame(self, data: bytes) -> None:
        pci = data[0]
        frame_type = (pci >> 4) & 0x0F

        if frame_type == 0x0:  # Single Frame
            length = pci & 0x0F
            payload = bytes(data[1 : 1 + length])
            self._last_response = payload
            self._response_event.set()
            if self._response_callback:
                self._response_callback(payload)

        elif frame_type == 0x1:  # First Frame
            self._rx_expected_len = ((pci & 0x0F) << 8) | data[1]
            self._rx_buffer = bytearray(data[2:8])
            self._rx_seq = 1
            self._send_flow_control()

        elif frame_type == 0x2:  # Consecutive Frame
            seq = pci & 0x0F
            if seq == self._rx_seq:
                self._rx_buffer.extend(data[1:8])
                self._rx_seq = (self._rx_seq + 1) & 0x0F
                if len(self._rx_buffer) >= self._rx_expected_len:
                    payload = bytes(self._rx_buffer[: self._rx_expected_len])
                    self._last_response = payload
                    self._response_event.set()
                    if self._response_callback:
                        self._response_callback(payload)
            else:
                log.warning("CAN CF sequence error: expected %d got %d", self._rx_seq, seq)


# ------------------------------------------------------------------ #
#  Mock Transport (for offline / UI development)                      #

# ------------------------------------------------------------------ #
#  Mock Transport — full simulation (profile-aware)                   #
# ------------------------------------------------------------------ #

class MockTransport(AbstractTransport):
    """
    Simulation transport. Values come from AppProfile.simulation,
    which is loaded from app_config.yaml.

    For ECU Info DIDs (0xF1xx): returns strings from simulation.ecu_info.
    For parameter DIDs (0x1xxx): returns hardcoded plausible defaults.
    For DTC queries: returns simulation.dtc_list.
    """

    def __init__(self):
        super().__init__()
        self._connected = False

    @property
    def name(self) -> str:
        return "Mock (Simulation)"

    def connect(self, **kw) -> None:
        self._connected = True
        log.info("Mock transport connected (simulation mode)")

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def send(self, payload: bytes) -> None:
        pass  # fire-and-forget not used in mock

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes:
        if not self._connected:
            raise TransportError("Mock not connected")

        # Import here to avoid circular import at module level
        from core.app_profile import profile

        sid = payload[0]
        time.sleep(0.001)  # minimal latency simulation

        # ── ReadDataByIdentifier (0x22) ─────────────────────────
        if sid == ServiceID.READ_DATA_BY_ID and len(payload) >= 3:
            did = struct.unpack(">H", payload[1:3])[0]
            data = self._mock_rdbi(did, profile)
            if data is not None:
                return bytes([sid | 0x40]) + payload[1:3] + data
            # DID not found
            return bytes([0x7F, sid, 0x31])  # requestOutOfRange

        # ── WriteDataByIdentifier (0x2E) ────────────────────────
        if sid == ServiceID.WRITE_DATA_BY_ID and len(payload) >= 3:
            # In read_only mode: reject writes
            from core.app_profile import profile as p
            if p.params_read_only:
                return bytes([0x7F, sid, 0x22])  # conditionsNotCorrect
            time.sleep(0.010)
            return bytes([sid | 0x40]) + payload[1:3]

        # ── TesterPresent (0x3E) ────────────────────────────────
        if sid == ServiceID.TESTER_PRESENT:
            sub = payload[1] if len(payload) > 1 else 0x00
            if sub & 0x80:
                return b""  # suppress
            return bytes([sid | 0x40, sub & 0x7F])

        # ── DiagnosticSessionControl (0x10) ─────────────────────
        if sid == ServiceID.DIAGNOSTIC_SESSION_CONTROL:
            return bytes([sid | 0x40, payload[1]])

        # ── ECUReset (0x11) ─────────────────────────────────────
        if sid == ServiceID.ECU_RESET:
            time.sleep(0.050)
            return bytes([sid | 0x40, payload[1]])

        # ── ReadDTCInformation (0x19) ────────────────────────────
        if sid == ServiceID.READ_DTC and len(payload) >= 2:
            return self._mock_read_dtc(payload, profile)

        # ── ClearDiagnosticInformation (0x14) ───────────────────
        if sid == ServiceID.CLEAR_DTC:
            time.sleep(0.100)
            return bytes([sid | 0x40])

        # ── SecurityAccess (0x27) ────────────────────────────────
        if sid == ServiceID.SECURITY_ACCESS and len(payload) >= 2:
            sub = payload[1]
            if sub % 2 == 1:  # seed request
                return bytes([sid | 0x40, sub, 0x12, 0x34, 0x56, 0x78])
            else:              # key response
                return bytes([sid | 0x40, sub])

        # ── RoutineControl (0x31) ────────────────────────────────
        if sid == 0x31 and len(payload) >= 4:
            routine_id = (payload[2] << 8) | payload[3]
            if routine_id == 0xFF00:    # EraseMemory
                time.sleep(0.500)
            elif routine_id == 0xFF04:  # CheckMemory
                time.sleep(0.200)
            else:
                time.sleep(0.050)
            return bytes([0x71]) + payload[1:4]

        # ── RequestDownload (0x34) ───────────────────────────────
        if sid == 0x34:
            return bytes([0x74, 0x20, 0x01, 0x02])  # lengthFormat=0x20→2bytes, maxBlock=0x0102=258

        # ── TransferData (0x36) ──────────────────────────────────
        if sid == 0x36:
            return bytes([0x76, payload[1] if len(payload) > 1 else 0x01])

        # ── RequestTransferExit (0x37) ───────────────────────────
        if sid == 0x37:
            return bytes([0x77])

        # Unknown service
        return bytes([0x7F, sid, 0x11])  # serviceNotSupported

    # ── Private helpers ──────────────────────────────────────────

    def _mock_rdbi(self, did: int, profile) -> bytes | None:
        """Return mock bytes for a DID, or None if not known."""

        # ECU Info DIDs from simulation config
        if 0xF100 <= did <= 0xF1FF:
            text = profile.simulation.ecu_info.get(did)
            if text:
                return text.encode("ascii")
            return None

        # Parameter DIDs — plausible FOC values
        _params = {
            0x1001: struct.pack("<B",  4),
            0x1002: struct.pack("<f",  0.185),
            0x1003: struct.pack("<f",  0.000210),
            0x1004: struct.pack("<f",  0.000280),
            0x1005: struct.pack("<f",  320.0),
            0x1006: struct.pack("<f",  6000.0),
            0x1007: struct.pack("<f",  8.5),
            0x1101: struct.pack("<H",  4),       # Hall encoder
            0x1102: struct.pack("<I",  4096),
            0x1103: struct.pack("<H",  0),
            0x1104: struct.pack("<f",  0.0),
            0x1201: struct.pack("<f",  1.0),
            0x1202: struct.pack("<f",  0.0003),
            0x1301: struct.pack("<f",  30.0),
            0x1302: struct.pack("<f",  48.0),
            0x1303: struct.pack("<f",  10.0),
            0x1401: struct.pack("<f",  80.0),
            0x1402: struct.pack("<f",  100.0),
            0x1403: struct.pack("<f",  120.0),
            0x1404: struct.pack("<f",  0.2),
            0x1501: struct.pack("<I",  100),
            0x1502: struct.pack("<H",  3),
            0x1503: struct.pack("<H",  10),
            0x1601: struct.pack("<f",  6000.0),
            0x1602: struct.pack("<f",  60000.0),
            0x1701: struct.pack("<f",  2.5),
            0x1702: struct.pack("<f",  500.0),
            0x1703: struct.pack("<f",  24.0),
            0x1704: struct.pack("<f",  24.0),
            0x1705: struct.pack("<f",  2.5),
            0x1706: struct.pack("<f",  500.0),
            0x1707: struct.pack("<f",  24.0),
            0x1708: struct.pack("<f",  24.0),
            0x1801: struct.pack("<B",  1),   # bool True
            0x1802: struct.pack("<B",  0),
            0x1803: struct.pack("<f",  500.0),
            0x1804: struct.pack("<f",  8.5),
            0x1805: struct.pack("<f", -8.5),
            0x1901: struct.pack("<B",  1),
            0x1902: struct.pack("<B",  1),
            0x1903: struct.pack("<B",  0),
            0x1904: struct.pack("<f",  200.0),
            0x1905: struct.pack("<f",  0.05),
            0x1906: struct.pack("<f",  2.0),
            0x1907: struct.pack("<f",  15.0),
            0x1908: struct.pack("<f",  25.0),
            0x1909: struct.pack("<f",  10000.0),
            0x190A: struct.pack("<f",  10000.0),
            0x1A01: struct.pack("<B",  1),
            0x1A02: struct.pack("<B",  0),
            0x1A03: struct.pack("<B",  0),
            0x1A04: struct.pack("<f",  50.0),
            0x1A05: struct.pack("<f",  30.0),
            0x1A06: struct.pack("<f",  0.0),
            0x1A07: struct.pack("<f",  0.001),
            0x1A08: struct.pack("<f",  500.0),
            0x1A09: struct.pack("<f",  3000.0),
            0x1A0A: struct.pack("<f",  3000.0),
            0x1A0B: struct.pack("<f",  30000.0),
            0x1B01: struct.pack("<B",  1),
            0x1B02: struct.pack("<B",  0),
            0x1B03: struct.pack("<B",  0),
            0x1B04: struct.pack("<f",  0.0003),
            0x1B05: struct.pack("<f",  0.0),
            0x1B06: struct.pack("<f",  0.5),
            0x1B07: struct.pack("<f",  0.001),
        }
        if did in _params:
            return _params[did]

        # Device address DID
        if did == 0x000A:
            return struct.pack("<B", 0xA0)

        return None

    def _mock_read_dtc(self, payload: bytes, profile) -> bytes:
        """Build 0x59 response from simulation.dtc_list."""
        sub_func = payload[1] if len(payload) > 1 else 0x02
        status_mask = payload[2] if len(payload) > 2 else 0xFF

        result = bytearray([ServiceID.READ_DTC | 0x40, sub_func, 0xFF])

        for item in profile.simulation.dtc_list:
            status = item.get("status", 0x08)
            if status & status_mask:
                dtc_str = item.get("dtc", "P0000")
                dtc_id  = _parse_dtc_str(dtc_str)
                result += bytes([
                    (dtc_id >> 16) & 0xFF,
                    (dtc_id >>  8) & 0xFF,
                     dtc_id        & 0xFF,
                    status,
                ])

        return bytes(result)


def _parse_dtc_str(s: str) -> int:
    """Parse Pxxxx / Cxxxx / Bxxxx / Uxxxx to 3-byte int."""
    prefix_map = {"P": 0, "C": 1, "B": 2, "U": 3}
    s = s.strip().upper()
    if not s:
        return 0
    group = prefix_map.get(s[0], 0)
    try:
        code = int(s[1:], 16)
    except ValueError:
        code = 0
    return (group << 22) | (code & 0x3FFF)

class MockTransport(AbstractTransport):
    """
    Simulated transport that returns plausible values without hardware.
    Used for UI development and testing.
    """

    def __init__(self):
        super().__init__()
        self._connected = False
        self._param_store = None

    def set_param_store(self, store) -> None:
        """Optional: inject store so mock can return real defaults."""
        self._param_store = store

    @property
    def name(self) -> str:
        return "Mock"

    def connect(self, **kwargs) -> None:
        self._connected = True
        log.info("Mock transport connected")

    def disconnect(self) -> None:
        self._connected = False
        log.info("Mock transport disconnected")

    def is_connected(self) -> bool:
        return self._connected

    def send(self, payload: bytes) -> None:
        pass  # fire-and-forget not used in mock

    def set_scan_callback(self, cb) -> None:
        """Register callback(device_addr: int, data: bytes) for scanner.
        Set to None to stop scanning. Thread-safe via GIL.
        """
        self._scan_callback = cb

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes:
        import struct
        from uds.codec import ServiceID

        if not self._connected:
            raise TransportError("Mock not connected")

        sid = payload[0]

        if sid == ServiceID.READ_DATA_BY_ID and len(payload) >= 3:
            did = struct.unpack(">H", payload[1:3])[0]
            mock_data = self._make_mock_data(did)
            response = bytes([ServiceID.READ_DATA_BY_ID | 0x40]) + payload[1:3] + mock_data
            time.sleep(0.005)  # simulate latency
            return response

        if sid == ServiceID.WRITE_DATA_BY_ID and len(payload) >= 3:
            response = bytes([ServiceID.WRITE_DATA_BY_ID | 0x40]) + payload[1:3]
            time.sleep(0.003)
            return response

        if sid == ServiceID.TESTER_PRESENT:
            return bytes([ServiceID.TESTER_PRESENT | 0x40, 0x00])

        if sid == ServiceID.DIAGNOSTIC_SESSION_CONTROL:
            return bytes([ServiceID.DIAGNOSTIC_SESSION_CONTROL | 0x40, payload[1]])

        return bytes([0x7F, sid, 0x11])  # service not supported

    def _make_mock_data(self, did: int) -> bytes:
        """Return plausible mock bytes for a given DID."""
        import struct
        # DID-specific mock values
        mock_map = {
            0x1001: struct.pack("<B", 4),        # PolePairs = 4
            0x1002: struct.pack("<f", 0.185),    # PhaseResistance
            0x1003: struct.pack("<f", 0.000210), # Ld
            0x1004: struct.pack("<f", 0.000280), # Lq
            0x1005: struct.pack("<f", 320.0),    # Kv
            0x1006: struct.pack("<f", 6000.0),   # MaxSpeed
            0x1007: struct.pack("<f", 8.5),      # MaxTorque
            0x1101: struct.pack("<H", 4),         # Encoder.Type = Hall
            0x1102: struct.pack("<I", 4096),      # Resolution
            0x1103: struct.pack("<H", 0),         # Direction = Normal
            0x1104: struct.pack("<f", 0.0),       # Offset
            0x1201: struct.pack("<f", 1.0),       # GearRatio
            0x1202: struct.pack("<f", 0.0003),    # TotalInertia
            0x1301: struct.pack("<f", 30.0),      # MaxPhaseCurrent
            0x1302: struct.pack("<f", 48.0),      # MaxDcBusVoltage
            0x1303: struct.pack("<f", 10.0),      # MinDcBusVoltage
        }
        if did in mock_map:
            return mock_map[did]
        # Fallback: 4 zero bytes (float 0.0)
        return struct.pack("<f", 0.0)# ------------------------------------------------------------------ #
#  Mock Transport — simulation-capable                                #
# ------------------------------------------------------------------ #

class MockTransport(AbstractTransport):
    """
    Simulation transport. Values come from AppProfile.simulation,
    which is loaded from app_config.yaml.

    For ECU Info DIDs (0xF1xx): returns strings from simulation.ecu_info.
    For parameter DIDs (0x1xxx): returns hardcoded plausible defaults.
    For DTC queries: returns simulation.dtc_list.
    """

    def __init__(self):
        super().__init__()
        self._connected = False

    @property
    def name(self) -> str:
        return "Mock (Simulation)"

    def connect(self, **kw) -> None:
        self._connected = True
        log.info("Mock transport connected (simulation mode)")

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def send(self, payload: bytes) -> None:
        pass  # fire-and-forget not used in mock

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes:
        if not self._connected:
            raise TransportError("Mock not connected")

        # Import here to avoid circular import at module level
        from core.app_profile import profile

        sid = payload[0]
        time.sleep(0.001)  # minimal latency simulation

        # ── ReadDataByIdentifier (0x22) ─────────────────────────
        if sid == ServiceID.READ_DATA_BY_ID and len(payload) >= 3:
            did = struct.unpack(">H", payload[1:3])[0]
            data = self._mock_rdbi(did, profile)
            if data is not None:
                return bytes([sid | 0x40]) + payload[1:3] + data
            # DID not found
            return bytes([0x7F, sid, 0x31])  # requestOutOfRange

        # ── WriteDataByIdentifier (0x2E) ────────────────────────
        if sid == ServiceID.WRITE_DATA_BY_ID and len(payload) >= 3:
            # In read_only mode: reject writes
            from core.app_profile import profile as p
            if p.params_read_only:
                return bytes([0x7F, sid, 0x22])  # conditionsNotCorrect
            time.sleep(0.010)
            return bytes([sid | 0x40]) + payload[1:3]

        # ── TesterPresent (0x3E) ────────────────────────────────
        if sid == ServiceID.TESTER_PRESENT:
            sub = payload[1] if len(payload) > 1 else 0x00
            if sub & 0x80:
                return b""  # suppress
            return bytes([sid | 0x40, sub & 0x7F])

        # ── DiagnosticSessionControl (0x10) ─────────────────────
        if sid == ServiceID.DIAGNOSTIC_SESSION_CONTROL:
            return bytes([sid | 0x40, payload[1]])

        # ── ECUReset (0x11) ─────────────────────────────────────
        if sid == ServiceID.ECU_RESET:
            time.sleep(0.050)
            return bytes([sid | 0x40, payload[1]])

        # ── ReadDTCInformation (0x19) ────────────────────────────
        if sid == ServiceID.READ_DTC and len(payload) >= 2:
            return self._mock_read_dtc(payload, profile)

        # ── ClearDiagnosticInformation (0x14) ───────────────────
        if sid == ServiceID.CLEAR_DTC:
            time.sleep(0.100)
            return bytes([sid | 0x40])

        # ── SecurityAccess (0x27) ────────────────────────────────
        if sid == ServiceID.SECURITY_ACCESS and len(payload) >= 2:
            sub = payload[1]
            if sub % 2 == 1:  # seed request
                return bytes([sid | 0x40, sub, 0x12, 0x34, 0x56, 0x78])
            else:              # key response
                return bytes([sid | 0x40, sub])

        # ── RoutineControl (0x31) ────────────────────────────────
        if sid == 0x31 and len(payload) >= 4:
            routine_id = (payload[2] << 8) | payload[3]
            if routine_id == 0xFF00:    # EraseMemory
                time.sleep(0.500)
            elif routine_id == 0xFF04:  # CheckMemory
                time.sleep(0.200)
            else:
                time.sleep(0.050)
            return bytes([0x71]) + payload[1:4]

        # ── RequestDownload (0x34) ───────────────────────────────
        if sid == 0x34:
            return bytes([0x74, 0x20, 0x01, 0x02])  # lengthFormat=0x20→2bytes, maxBlock=0x0102=258

        # ── TransferData (0x36) ──────────────────────────────────
        if sid == 0x36:
            return bytes([0x76, payload[1] if len(payload) > 1 else 0x01])

        # ── RequestTransferExit (0x37) ───────────────────────────
        if sid == 0x37:
            return bytes([0x77])

        # Unknown service
        return bytes([0x7F, sid, 0x11])  # serviceNotSupported

    # ── Private helpers ──────────────────────────────────────────

    def _mock_rdbi(self, did: int, profile) -> bytes | None:
        """Return mock bytes for a DID, or None if not known."""

        # ECU Info DIDs from simulation config
        if 0xF100 <= did <= 0xF1FF:
            text = profile.simulation.ecu_info.get(did)
            if text:
                return text.encode("ascii")
            return None

        # Parameter DIDs — plausible FOC values
        _params = {
            0x1001: struct.pack("<B",  4),
            0x1002: struct.pack("<f",  0.185),
            0x1003: struct.pack("<f",  0.000210),
            0x1004: struct.pack("<f",  0.000280),
            0x1005: struct.pack("<f",  320.0),
            0x1006: struct.pack("<f",  6000.0),
            0x1007: struct.pack("<f",  8.5),
            0x1101: struct.pack("<H",  4),       # Hall encoder
            0x1102: struct.pack("<I",  4096),
            0x1103: struct.pack("<H",  0),
            0x1104: struct.pack("<f",  0.0),
            0x1201: struct.pack("<f",  1.0),
            0x1202: struct.pack("<f",  0.0003),
            0x1301: struct.pack("<f",  30.0),
            0x1302: struct.pack("<f",  48.0),
            0x1303: struct.pack("<f",  10.0),
            0x1401: struct.pack("<f",  80.0),
            0x1402: struct.pack("<f",  100.0),
            0x1403: struct.pack("<f",  120.0),
            0x1404: struct.pack("<f",  0.2),
            0x1501: struct.pack("<I",  100),
            0x1502: struct.pack("<H",  3),
            0x1503: struct.pack("<H",  10),
            0x1601: struct.pack("<f",  6000.0),
            0x1602: struct.pack("<f",  60000.0),
            0x1701: struct.pack("<f",  2.5),
            0x1702: struct.pack("<f",  500.0),
            0x1703: struct.pack("<f",  24.0),
            0x1704: struct.pack("<f",  24.0),
            0x1705: struct.pack("<f",  2.5),
            0x1706: struct.pack("<f",  500.0),
            0x1707: struct.pack("<f",  24.0),
            0x1708: struct.pack("<f",  24.0),
            0x1801: struct.pack("<B",  1),   # bool True
            0x1802: struct.pack("<B",  0),
            0x1803: struct.pack("<f",  500.0),
            0x1804: struct.pack("<f",  8.5),
            0x1805: struct.pack("<f", -8.5),
            0x1901: struct.pack("<B",  1),
            0x1902: struct.pack("<B",  1),
            0x1903: struct.pack("<B",  0),
            0x1904: struct.pack("<f",  200.0),
            0x1905: struct.pack("<f",  0.05),
            0x1906: struct.pack("<f",  2.0),
            0x1907: struct.pack("<f",  15.0),
            0x1908: struct.pack("<f",  25.0),
            0x1909: struct.pack("<f",  10000.0),
            0x190A: struct.pack("<f",  10000.0),
            0x1A01: struct.pack("<B",  1),
            0x1A02: struct.pack("<B",  0),
            0x1A03: struct.pack("<B",  0),
            0x1A04: struct.pack("<f",  50.0),
            0x1A05: struct.pack("<f",  30.0),
            0x1A06: struct.pack("<f",  0.0),
            0x1A07: struct.pack("<f",  0.001),
            0x1A08: struct.pack("<f",  500.0),
            0x1A09: struct.pack("<f",  3000.0),
            0x1A0A: struct.pack("<f",  3000.0),
            0x1A0B: struct.pack("<f",  30000.0),
            0x1B01: struct.pack("<B",  1),
            0x1B02: struct.pack("<B",  0),
            0x1B03: struct.pack("<B",  0),
            0x1B04: struct.pack("<f",  0.0003),
            0x1B05: struct.pack("<f",  0.0),
            0x1B06: struct.pack("<f",  0.5),
            0x1B07: struct.pack("<f",  0.001),
        }
        if did in _params:
            return _params[did]

        # Device address DID
        if did == 0x000A:
            return struct.pack("<B", 0xA0)

        return None

    def _mock_read_dtc(self, payload: bytes, profile) -> bytes:
        """Build 0x59 response from simulation.dtc_list."""
        sub_func = payload[1] if len(payload) > 1 else 0x02
        status_mask = payload[2] if len(payload) > 2 else 0xFF

        result = bytearray([ServiceID.READ_DTC | 0x40, sub_func, 0xFF])

        for item in profile.simulation.dtc_list:
            status = item.get("status", 0x08)
            if status & status_mask:
                dtc_str = item.get("dtc", "P0000")
                dtc_id  = _parse_dtc_str(dtc_str)
                result += bytes([
                    (dtc_id >> 16) & 0xFF,
                    (dtc_id >>  8) & 0xFF,
                     dtc_id        & 0xFF,
                    status,
                ])

        return bytes(result)


def _parse_dtc_str(s: str) -> int:
    """Parse Pxxxx / Cxxxx / Bxxxx / Uxxxx to 3-byte int."""
    prefix_map = {"P": 0, "C": 1, "B": 2, "U": 3}
    s = s.strip().upper()
    if not s:
        return 0
    group = prefix_map.get(s[0], 0)
    try:
        code = int(s[1:], 16)
    except ValueError:
        code = 0
    return (group << 22) | (code & 0x3FFF)

class TransportError(Exception):
    """Any transport-level error."""


class AbstractTransport(ABC):
    """
    Contract for a UDS transport channel.

    Implementations must be thread-safe: send() may be called from a
    worker thread, and the response_callback will be called from the
    receiver thread.
    """

    def __init__(self):
        self._response_callback: Optional[Callable[[bytes], None]] = None
        self._error_callback: Optional[Callable[[str], None]] = None

    def set_response_callback(self, cb: Callable[[bytes], None]) -> None:
        self._response_callback = cb

    def set_error_callback(self, cb: Callable[[str], None]) -> None:
        self._error_callback = cb

    @abstractmethod
    def connect(self, **kwargs) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def send(self, payload: bytes) -> None: ...

    def set_scan_callback(self, cb) -> None:
        """Register callback(device_addr: int, data: bytes) for scanner.
        Set to None to stop scanning. Thread-safe via GIL.
        """
        self._scan_callback = cb

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes: ...

    @property
    @abstractmethod
    def name(self) -> str: ...


# ------------------------------------------------------------------ #
#  Serial Transport                                                    #
# ------------------------------------------------------------------ #

SERIAL_SOF = b"\xAA\x55"

class SerialTransport(AbstractTransport):
    """
    UDS over RS-232/USB-UART with length-prefixed framing.

    Frame structure (all big-endian):
        0xAA 0x55  – start-of-frame
        uint16     – payload length (bytes)
        payload    – UDS PDU
        uint16     – CRC-16/CCITT of payload

    Adjust framing to match your firmware if needed.
    """

    def __init__(self):
        super().__init__()
        self._serial = None
        self._rx_thread: Optional[threading.Thread] = None
        self._running = False
        self._response_event = threading.Event()
        self._last_response: Optional[bytes] = None
        self._lock = threading.Lock()
        self._scan_callback = None

    @property
    def name(self) -> str:
        return "Serial"

    def connect(self, port: str, baudrate: int = 115200, timeout: float = 0.1, **kwargs) -> None:
        try:
            import serial
        except ImportError:
            raise TransportError("pyserial not installed. Run: pip install pyserial")

        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
            )
            self._running = True
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()
            log.info("Serial connected: %s @ %d baud", port, baudrate)
        except Exception as e:
            raise TransportError(f"Failed to open {port}: {e}") from e

    def disconnect(self) -> None:
        self._running = False
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        log.info("Serial disconnected")

    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def _frame(self, payload: bytes) -> bytes:
        crc = crc16_ccitt(payload)
        return SERIAL_SOF + struct.pack(">H", len(payload)) + payload + struct.pack(">H", crc)

    def send(self, payload: bytes) -> None:
        if not self.is_connected():
            raise TransportError("Not connected")
        frame = self._frame(payload)
        with self._lock:
            self._serial.write(frame)

    def set_scan_callback(self, cb) -> None:
        """Register callback(device_addr: int, data: bytes) for scanner.
        Set to None to stop scanning. Thread-safe via GIL.
        """
        self._scan_callback = cb

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes:
        self._response_event.clear()
        self._last_response = None
        self.send(payload)
        if not self._response_event.wait(timeout):
            raise TransportError(f"Timeout waiting for response ({timeout}s)")
        if self._last_response is None:
            raise TransportError("No response data")
        return self._last_response

    def _rx_loop(self) -> None:
        buf = bytearray()
        while self._running:
            try:
                chunk = self._serial.read(256)
                if chunk:
                    buf.extend(chunk)
                    self._process_buffer(buf)
            except Exception as e:
                if self._running:
                    log.error("Serial RX error: %s", e)
                    if self._error_callback:
                        self._error_callback(str(e))

    def _process_buffer(self, buf: bytearray) -> None:
        while True:
            # Find SOF
            idx = buf.find(SERIAL_SOF)
            if idx == -1:
                buf.clear()
                return
            if idx > 0:
                del buf[:idx]

            # Need at least SOF(2) + len(2) + CRC(2) = 6 bytes min
            if len(buf) < 6:
                return

            payload_len = struct.unpack(">H", buf[2:4])[0]
            total = 2 + 2 + payload_len + 2

            if len(buf) < total:
                return

            payload = bytes(buf[4 : 4 + payload_len])
            crc_recv = struct.unpack(">H", buf[4 + payload_len : total])[0]
            crc_calc = crc16_ccitt(payload)

            del buf[:total]

            if crc_recv != crc_calc:
                log.warning("CRC mismatch: recv=0x%04X calc=0x%04X", crc_recv, crc_calc)
                continue

            self._last_response = payload
            self._response_event.set()
            if self._response_callback:
                self._response_callback(payload)


# ------------------------------------------------------------------ #
#  CAN Transport (ISO 15765-2 / ISO-TP)                               #
# ------------------------------------------------------------------ #

class CANTransport(AbstractTransport):
    """
    UDS over CAN using ISO 15765-2 transport protocol (ISO-TP).
    Requires: pip install python-can

    For PEAK USB-CAN adapters, also install:
        pip install python-can[pcan]  (Windows)
        or the PEAK Linux driver with peak_usb kernel module.

    ISO-TP frame types:
        Single Frame (SF):    1 + data (up to 7 bytes)
        First Frame (FF):     2 header + data start
        Consecutive Frame:    1 seq + data
        Flow Control (FC):    3 bytes

    This implementation handles single-frame and multi-frame for
    UDS payloads up to ~4095 bytes.
    """

    # CAN IDs — 29-bit extended, ISO 15765-2 / SAE J1939
    # Physical: 0x18DA<TA><SA>  (VinBT-263)
    # device_address=0xA0 (DEVICE_ADDRESS_VAL, VinBT-259/260)
    # tester_address=0xF1 (standard OBD tester SA)
    TESTER_ADDRESS = 0xF1

    def __init__(self, device_address: int = 0xA0,
                 tester_address: int = TESTER_ADDRESS):
        super().__init__()
        self._device_addr = device_address
        self._tester_addr = tester_address
        self._tx_id   = 0x18DA0000 | ((device_address & 0xFF) << 8) | (tester_address & 0xFF)
        self._rx_id   = 0x18DA0000 | ((tester_address & 0xFF) << 8) | (device_address & 0xFF)
        self._func_id = 0x18DB3300 | (tester_address & 0xFF)
        log.info("CAN IDs: TX=0x%08X RX=0x%08X FUNC=0x%08X",
                 self._tx_id, self._rx_id, self._func_id)
        self._bus = None
        self._running = False
        self._rx_thread: Optional[threading.Thread] = None
        self._response_event = threading.Event()
        self._last_response: Optional[bytes] = None
        self._rx_buffer: bytearray = bytearray()
        self._rx_expected_len: int = 0
        self._rx_seq: int = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "CAN"

    def connect(self, interface: str = "pcan", channel: str = "PCAN_USBBUS1",
                bitrate: int = 250000, **kwargs) -> None:
        try:
            import can
        except ImportError:
            raise TransportError("python-can not installed. Run: pip install python-can")

        try:
            self._bus = can.interface.Bus(
                interface=interface,
                channel=channel,
                bitrate=bitrate,
            )
            self._running = True
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()
            log.info("CAN connected: %s %s @ %d bps  TX=0x%08X RX=0x%08X",
                     interface, channel, bitrate, self._tx_id, self._rx_id)
        except Exception as e:
            raise TransportError(f"Failed to open CAN {interface}/{channel}: {e}") from e

    def disconnect(self) -> None:
        self._running = False
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        if self._bus:
            self._bus.shutdown()
        log.info("CAN disconnected")

    def is_connected(self) -> bool:
        return self._bus is not None and self._running

    def _send_flow_control(self) -> None:
        """Send ISO-TP Flow Control (ContinueToSend)."""
        import can
        fc_data = bytearray(8)
        fc_data[0] = 0x30  # FC, ContinueToSend
        fc_data[1] = 0x00  # block size = 0 (unlimited)
        fc_data[2] = 0x00  # STmin = 0ms
        msg = can.Message(arbitration_id=self._tx_id, data=fc_data, is_extended_id=True)
        self._bus.send(msg)

    def send_functional(self, payload: bytes) -> None:
        """Broadcast via functional address 0x18DB33<SA> (VinBT-264)."""
        if not self.is_connected():
            raise TransportError("CAN not connected")
        import can
        if len(payload) <= 7:
            data = bytearray(8)
            data[0] = len(payload)
            data[1:1+len(payload)] = payload
            msg = can.Message(arbitration_id=self._func_id, data=data, is_extended_id=True)
            with self._lock:
                self._bus.send(msg)

    def send(self, payload: bytes) -> None:
        if not self.is_connected():
            raise TransportError("CAN not connected")
        import can

        with self._lock:
            if len(payload) <= 7:
                # Single Frame
                data = bytearray(8)
                data[0] = len(payload)  # SF, length in nibble 0
                data[1 : 1 + len(payload)] = payload
                msg = can.Message(arbitration_id=self._tx_id, data=data, is_extended_id=True)
                self._bus.send(msg)
            else:
                # Multi-Frame: First Frame
                data = bytearray(8)
                total = len(payload)
                data[0] = 0x10 | ((total >> 8) & 0x0F)
                data[1] = total & 0xFF
                data[2:8] = payload[0:6]
                msg = can.Message(arbitration_id=self._tx_id, data=data, is_extended_id=True)
                self._bus.send(msg)

                # Wait for Flow Control
                time.sleep(0.02)

                # Consecutive Frames
                seq = 1
                offset = 6
                while offset < total:
                    chunk = payload[offset : offset + 7]
                    data = bytearray(8)
                    data[0] = 0x20 | (seq & 0x0F)
                    data[1 : 1 + len(chunk)] = chunk
                    msg = can.Message(arbitration_id=self._tx_id, data=data, is_extended_id=True)
                    self._bus.send(msg)
                    offset += 7
                    seq = (seq + 1) & 0x0F
                    time.sleep(0.001)

    def set_scan_callback(self, cb) -> None:
        """Register callback(device_addr: int, data: bytes) for scanner.
        Set to None to stop scanning. Thread-safe via GIL.
        """
        self._scan_callback = cb

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes:
        self._response_event.clear()
        self._last_response = None
        self._rx_buffer = bytearray()
        self._rx_expected_len = 0
        self.send(payload)
        if not self._response_event.wait(timeout):
            raise TransportError(f"CAN timeout waiting for response ({timeout}s)")
        if self._last_response is None:
            raise TransportError("No CAN response data")
        return self._last_response

    def _rx_loop(self) -> None:
        while self._running:
            try:
                msg = self._bus.recv(timeout=0.1)
                if not msg:
                    continue
                # Normal UDS response for active connection
                if msg.arbitration_id == self._rx_id:
                    self._process_can_frame(bytes(msg.data))
                # Scanner callback: receive any 0x18DAF1xx frame
                # (physical replies to our tester addr 0xF1)
                elif self._scan_callback is not None:
                    if (msg.arbitration_id & 0xFFFFFF00) == 0x18DAF100:
                        device_addr = msg.arbitration_id & 0xFF
                        self._scan_callback(device_addr, bytes(msg.data))
            except Exception as e:
                if self._running:
                    log.error("CAN RX error: %s", e)
                    if self._error_callback:
                        self._error_callback(str(e))

    def _process_can_frame(self, data: bytes) -> None:
        pci = data[0]
        frame_type = (pci >> 4) & 0x0F

        if frame_type == 0x0:  # Single Frame
            length = pci & 0x0F
            payload = bytes(data[1 : 1 + length])
            self._last_response = payload
            self._response_event.set()
            if self._response_callback:
                self._response_callback(payload)

        elif frame_type == 0x1:  # First Frame
            self._rx_expected_len = ((pci & 0x0F) << 8) | data[1]
            self._rx_buffer = bytearray(data[2:8])
            self._rx_seq = 1
            self._send_flow_control()

        elif frame_type == 0x2:  # Consecutive Frame
            seq = pci & 0x0F
            if seq == self._rx_seq:
                self._rx_buffer.extend(data[1:8])
                self._rx_seq = (self._rx_seq + 1) & 0x0F
                if len(self._rx_buffer) >= self._rx_expected_len:
                    payload = bytes(self._rx_buffer[: self._rx_expected_len])
                    self._last_response = payload
                    self._response_event.set()
                    if self._response_callback:
                        self._response_callback(payload)
            else:
                log.warning("CAN CF sequence error: expected %d got %d", self._rx_seq, seq)


# ------------------------------------------------------------------ #
#  Mock Transport (for offline / UI development)                      #
# ------------------------------------------------------------------ #

class MockTransport(AbstractTransport):
    """
    Simulated transport that returns plausible values without hardware.
    Used for UI development and testing.
    """

    def __init__(self):
        super().__init__()
        self._connected = False
        self._param_store = None

    def set_param_store(self, store) -> None:
        """Optional: inject store so mock can return real defaults."""
        self._param_store = store

    @property
    def name(self) -> str:
        return "Mock"

    def connect(self, **kwargs) -> None:
        self._connected = True
        log.info("Mock transport connected")

    def disconnect(self) -> None:
        self._connected = False
        log.info("Mock transport disconnected")

    def is_connected(self) -> bool:
        return self._connected

    def send(self, payload: bytes) -> None:
        pass  # fire-and-forget not used in mock

    def set_scan_callback(self, cb) -> None:
        """Register callback(device_addr: int, data: bytes) for scanner.
        Set to None to stop scanning. Thread-safe via GIL.
        """
        self._scan_callback = cb

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes:
        import struct
        from uds.codec import ServiceID

        if not self._connected:
            raise TransportError("Mock not connected")

        sid = payload[0]

        if sid == ServiceID.READ_DATA_BY_ID and len(payload) >= 3:
            did = struct.unpack(">H", payload[1:3])[0]
            mock_data = self._make_mock_data(did)
            response = bytes([ServiceID.READ_DATA_BY_ID | 0x40]) + payload[1:3] + mock_data
            time.sleep(0.005)  # simulate latency
            return response

        if sid == ServiceID.WRITE_DATA_BY_ID and len(payload) >= 3:
            response = bytes([ServiceID.WRITE_DATA_BY_ID | 0x40]) + payload[1:3]
            time.sleep(0.003)
            return response

        if sid == ServiceID.TESTER_PRESENT:
            return bytes([ServiceID.TESTER_PRESENT | 0x40, 0x00])

        if sid == ServiceID.DIAGNOSTIC_SESSION_CONTROL:
            return bytes([ServiceID.DIAGNOSTIC_SESSION_CONTROL | 0x40, payload[1]])

        return bytes([0x7F, sid, 0x11])  # service not supported

    def _make_mock_data(self, did: int) -> bytes:
        """Return plausible mock bytes for a given DID."""
        import struct
        # DID-specific mock values
        mock_map = {
            0x1001: struct.pack("<B", 4),        # PolePairs = 4
            0x1002: struct.pack("<f", 0.185),    # PhaseResistance
            0x1003: struct.pack("<f", 0.000210), # Ld
            0x1004: struct.pack("<f", 0.000280), # Lq
            0x1005: struct.pack("<f", 320.0),    # Kv
            0x1006: struct.pack("<f", 6000.0),   # MaxSpeed
            0x1007: struct.pack("<f", 8.5),      # MaxTorque
            0x1101: struct.pack("<H", 4),         # Encoder.Type = Hall
            0x1102: struct.pack("<I", 4096),      # Resolution
            0x1103: struct.pack("<H", 0),         # Direction = Normal
            0x1104: struct.pack("<f", 0.0),       # Offset
            0x1201: struct.pack("<f", 1.0),       # GearRatio
            0x1202: struct.pack("<f", 0.0003),    # TotalInertia
            0x1301: struct.pack("<f", 30.0),      # MaxPhaseCurrent
            0x1302: struct.pack("<f", 48.0),      # MaxDcBusVoltage
            0x1303: struct.pack("<f", 10.0),      # MinDcBusVoltage
        }
        if did in mock_map:
            return mock_map[did]
        # Fallback: 4 zero bytes (float 0.0)
        return struct.pack("<f", 0.0)
