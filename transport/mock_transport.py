"""
Mock Transport — profile-aware simulation
==========================================
Returns realistic values from app_config.yaml [simulation] section.
Falls back to hardcoded defaults for parameters not in config.
"""
from __future__ import annotations

import logging
import struct
import time

from transport.transport import AbstractTransport, TransportError
from uds.codec import ServiceID

log = logging.getLogger(__name__)


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
        time.sleep(0.005)  # simulate realistic latency

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
