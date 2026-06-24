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

    @abstractmethod
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

    # CAN IDs – adjust to match your device
    DEFAULT_TX_ID = 0x7E0  # tester → ECU (physical)
    DEFAULT_RX_ID = 0x7E8  # ECU → tester

    def __init__(self, tx_id: int = DEFAULT_TX_ID, rx_id: int = DEFAULT_RX_ID):
        super().__init__()
        self._tx_id = tx_id
        self._rx_id = rx_id
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
                bitrate: int = 500000, **kwargs) -> None:
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
            log.info("CAN connected: %s %s @ %d bps", interface, channel, bitrate)
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
        fc_data[2] = 0x0A  # separation time = 10ms
        msg = can.Message(arbitration_id=self._tx_id, data=fc_data, is_extended_id=False)
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
                msg = can.Message(arbitration_id=self._tx_id, data=data, is_extended_id=False)
                self._bus.send(msg)
            else:
                # Multi-Frame: First Frame
                data = bytearray(8)
                total = len(payload)
                data[0] = 0x10 | ((total >> 8) & 0x0F)
                data[1] = total & 0xFF
                data[2:8] = payload[0:6]
                msg = can.Message(arbitration_id=self._tx_id, data=data, is_extended_id=False)
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
                    msg = can.Message(arbitration_id=self._tx_id, data=data, is_extended_id=False)
                    self._bus.send(msg)
                    offset += 7
                    seq = (seq + 1) & 0x0F
                    time.sleep(0.001)

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
                if msg and msg.arbitration_id == self._rx_id:
                    self._process_can_frame(bytes(msg.data))
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
