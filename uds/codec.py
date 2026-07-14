"""
UDS Protocol Codec
==================
Implements the encoding and decoding of UDS (ISO 14229-1) PDUs.

Supported services:
    0x22  ReadDataByIdentifier  (RDBI)
    0x2E  WriteDataByIdentifier (WDBI)
    0x27  SecurityAccess        (SA)
    0x10  DiagnosticSessionControl (DSC)
    0x11  ECUReset
    0x3E  TesterPresent
    0x14  ClearDiagnosticInformation
    0x19  ReadDTCInformation

All public encode_* methods return bytes.
All public decode_* methods return a dict or raise UDSError.
"""
from __future__ import annotations

import struct
from enum import IntEnum
from typing import Any, Dict, Optional, Tuple


# ------------------------------------------------------------------ #
#  Service IDs                                                         #
# ------------------------------------------------------------------ #

class ServiceID(IntEnum):
    DIAGNOSTIC_SESSION_CONTROL     = 0x10
    ECU_RESET                      = 0x11
    CLEAR_DTC                      = 0x14
    READ_DTC                       = 0x19
    READ_DATA_BY_ID                = 0x22
    WRITE_DATA_BY_ID               = 0x2E
    SECURITY_ACCESS                = 0x27
    TESTER_PRESENT                 = 0x3E
    NEGATIVE_RESPONSE              = 0x7F
    # positive response = service_id | 0x40
    RESPONSE_OFFSET                = 0x40


class SessionType(IntEnum):
    DEFAULT      = 0x01
    PROGRAMMING  = 0x02
    EXTENDED     = 0x03


class ResetType(IntEnum):
    HARD_RESET          = 0x01
    KEY_OFF_ON_RESET    = 0x02
    SOFT_RESET          = 0x03


class NRC(IntEnum):
    """Negative Response Codes (ISO 14229-1 Table A-1)"""
    GENERAL_REJECT                          = 0x10
    SERVICE_NOT_SUPPORTED                   = 0x11
    SUB_FUNCTION_NOT_SUPPORTED              = 0x12
    INCORRECT_MESSAGE_LENGTH                = 0x13
    RESPONSE_TOO_LONG                       = 0x14
    BUSY_REPEAT_REQUEST                     = 0x21
    CONDITIONS_NOT_CORRECT                  = 0x22
    REQUEST_SEQUENCE_ERROR                  = 0x24
    REQUEST_OUT_OF_RANGE                    = 0x31
    SECURITY_ACCESS_DENIED                  = 0x33
    INVALID_KEY                             = 0x35
    EXCEEDED_NUMBER_OF_ATTEMPTS             = 0x36
    REQUIRED_TIME_DELAY_NOT_EXPIRED         = 0x37
    UPLOAD_DOWNLOAD_NOT_ACCEPTED            = 0x70
    TRANSFER_DATA_SUSPENDED                 = 0x71
    GENERAL_PROGRAMMING_FAILURE             = 0x72
    WRONG_BLOCK_SEQUENCE_COUNTER            = 0x73
    RESPONSE_PENDING                        = 0x78
    SUB_FUNCTION_NOT_SUPPORTED_IN_SESSION   = 0x7E
    SERVICE_NOT_SUPPORTED_IN_SESSION        = 0x7F

    @classmethod
    def description(cls, code: int) -> str:
        try:
            return cls(code).name.replace("_", " ").title()
        except ValueError:
            return f"Unknown NRC 0x{code:02X}"


# ------------------------------------------------------------------ #
#  Exceptions                                                          #
# ------------------------------------------------------------------ #

class UDSError(Exception):
    """Base UDS error."""

class UDSNegativeResponse(UDSError):
    def __init__(self, service_id: int, nrc: int):
        self.service_id = service_id
        self.nrc = nrc
        super().__init__(
            f"NRC 0x{nrc:02X} ({NRC.description(nrc)}) "
            f"for service 0x{service_id:02X}"
        )

class UDSDecodeError(UDSError):
    """Malformed PDU."""


# ------------------------------------------------------------------ #
#  Data encoding helpers                                               #
# ------------------------------------------------------------------ #

class DataCodec:
    """
    Encode/decode parameter values to/from UDS payload bytes.
    Uses little-endian for all multi-byte integers to match typical
    embedded firmware conventions (override if your target differs).
    """

    @staticmethod
    def encode(value: Any, param_type: str) -> bytes:
        t = param_type.lower()
        if t == "bool":
            return bytes([1 if value else 0])
        if t == "uint8":
            return struct.pack("<B", int(value))
        if t == "uint16":
            return struct.pack("<H", int(value))
        if t == "uint32":
            return struct.pack("<I", int(value))
        if t == "int8":
            return struct.pack("<b", int(value))
        if t == "int16":
            return struct.pack("<h", int(value))
        if t == "int32":
            return struct.pack("<i", int(value))
        if t == "float":
            return struct.pack("<f", float(value))
        if t == "enum":
            return struct.pack("<H", int(value))
        raise UDSError(f"Unsupported type: {param_type}")

    @staticmethod
    def decode(data: bytes, param_type: str) -> Any:
        t = param_type.lower()
        if t == "bool":
            return bool(data[0])
        if t == "uint8":
            return struct.unpack("<B", data[:1])[0]
        if t == "uint16":
            return struct.unpack("<H", data[:2])[0]
        if t == "uint32":
            return struct.unpack("<I", data[:4])[0]
        if t == "int8":
            return struct.unpack("<b", data[:1])[0]
        if t == "int16":
            return struct.unpack("<h", data[:2])[0]
        if t == "int32":
            return struct.unpack("<i", data[:4])[0]
        if t == "float":
            return struct.unpack("<f", data[:4])[0]
        if t == "enum":
            return struct.unpack("<H", data[:2])[0]
        raise UDSError(f"Unsupported type: {param_type}")

    @staticmethod
    def byte_size(param_type: str) -> int:
        t = param_type.lower()
        sizes = {
            "bool": 1, "uint8": 1, "int8": 1,
            "uint16": 2, "int16": 2, "enum": 2,
            "uint32": 4, "int32": 4, "float": 4,
        }
        return sizes.get(t, 0)


# ------------------------------------------------------------------ #
#  UDS PDU builder / parser                                            #
# ------------------------------------------------------------------ #

class UDSCodec:
    """Stateless encoder/decoder for UDS PDUs."""

    # ── Encode requests ─────────────────────────────────────────────

    @staticmethod
    def encode_diagnostic_session_control(session: SessionType = SessionType.DEFAULT) -> bytes:
        return bytes([ServiceID.DIAGNOSTIC_SESSION_CONTROL, int(session)])

    @staticmethod
    def encode_ecu_reset(reset_type: ResetType = ResetType.HARD_RESET) -> bytes:
        return bytes([ServiceID.ECU_RESET, int(reset_type)])

    @staticmethod
    def encode_tester_present(suppress_response: bool = True) -> bytes:
        sub = 0x80 if suppress_response else 0x00
        return bytes([ServiceID.TESTER_PRESENT, sub])

    @staticmethod
    def encode_security_access_request_seed(level: int = 0x01) -> bytes:
        return bytes([ServiceID.SECURITY_ACCESS, level])

    @staticmethod
    def encode_security_access_send_key(level: int, key: bytes) -> bytes:
        return bytes([ServiceID.SECURITY_ACCESS, level + 1]) + key

    @staticmethod
    def encode_read_data_by_id(did: int) -> bytes:
        return bytes([ServiceID.READ_DATA_BY_ID]) + struct.pack(">H", did)

    @staticmethod
    def encode_write_data_by_id(did: int, data: bytes) -> bytes:
        return bytes([ServiceID.WRITE_DATA_BY_ID]) + struct.pack(">H", did) + data

    @staticmethod
    def encode_clear_dtc(group: int = 0xFFFFFF) -> bytes:
        b = struct.pack(">I", group)
        return bytes([ServiceID.CLEAR_DTC]) + b[1:]  # 3 bytes

    @staticmethod
    def encode_read_dtc_by_status_mask(mask: int = 0xFF) -> bytes:
        return bytes([ServiceID.READ_DTC, 0x02, mask])

    # ── Decode responses ────────────────────────────────────────────

    @staticmethod
    def decode_response(raw: bytes) -> Dict[str, Any]:
        """
        Top-level dispatcher. Returns a dict with at least:
            service_id, positive, ...service-specific fields
        Raises UDSNegativeResponse or UDSDecodeError on problems.
        """
        if len(raw) < 1:
            raise UDSDecodeError("Empty response")

        sid = raw[0]

        if sid == ServiceID.NEGATIVE_RESPONSE:
            if len(raw) < 3:
                raise UDSDecodeError("Truncated NRC")
            raise UDSNegativeResponse(service_id=raw[1], nrc=raw[2])

        # Positive response: SID should equal request_SID | 0x40
        actual_service = sid - ServiceID.RESPONSE_OFFSET

        if actual_service == ServiceID.READ_DATA_BY_ID:
            return UDSCodec._decode_rdbi_response(raw)
        if actual_service == ServiceID.WRITE_DATA_BY_ID:
            return UDSCodec._decode_wdbi_response(raw)
        if actual_service == ServiceID.DIAGNOSTIC_SESSION_CONTROL:
            return {"service_id": actual_service, "positive": True, "session": raw[1] if len(raw) > 1 else None}
        if actual_service == ServiceID.SECURITY_ACCESS:
            return UDSCodec._decode_security_access_response(raw)
        if actual_service == ServiceID.TESTER_PRESENT:
            return {"service_id": actual_service, "positive": True}
        if actual_service == ServiceID.ECU_RESET:
            return {"service_id": actual_service, "positive": True}

        return {"service_id": actual_service, "positive": True, "raw": raw}

    @staticmethod
    def _decode_rdbi_response(raw: bytes) -> Dict[str, Any]:
        if len(raw) < 3:
            raise UDSDecodeError("RDBI response too short")
        did = struct.unpack(">H", raw[1:3])[0]
        data = raw[3:]
        return {
            "service_id": ServiceID.READ_DATA_BY_ID,
            "positive": True,
            "did": did,
            "data": data,
        }

    @staticmethod
    def _decode_wdbi_response(raw: bytes) -> Dict[str, Any]:
        if len(raw) < 3:
            raise UDSDecodeError("WDBI response too short")
        did = struct.unpack(">H", raw[1:3])[0]
        return {
            "service_id": ServiceID.WRITE_DATA_BY_ID,
            "positive": True,
            "did": did,
        }

    @staticmethod
    def _decode_security_access_response(raw: bytes) -> Dict[str, Any]:
        level = raw[1] if len(raw) > 1 else None
        seed = raw[2:] if len(raw) > 2 else b""
        return {
            "service_id": ServiceID.SECURITY_ACCESS,
            "positive": True,
            "level": level,
            "seed": seed,
        }


# ------------------------------------------------------------------ #
#  DTC definitions                                                     #
# ------------------------------------------------------------------ #

class DTCStatusMask:
    """ISO 14229-1 DTC status bits."""
    TEST_FAILED                  = 0x01
    TEST_FAILED_THIS_CYCLE       = 0x02
    PENDING                      = 0x04
    CONFIRMED                    = 0x08
    TEST_NOT_COMPLETED_SINCE_CLEAR = 0x10
    TEST_FAILED_SINCE_CLEAR      = 0x20
    TEST_NOT_COMPLETED_THIS_CYCLE = 0x40
    WARNING_INDICATOR_REQUESTED  = 0x80
    ALL                          = 0xFF

    @staticmethod
    def describe(mask: int) -> list[str]:
        names = [
            (0x01, "TestFailed"),
            (0x02, "FailedThisCycle"),
            (0x04, "Pending"),
            (0x08, "Confirmed"),
            (0x10, "NotCompletedSinceClear"),
            (0x20, "FailedSinceClear"),
            (0x40, "NotCompletedThisCycle"),
            (0x80, "WarningIndicator"),
        ]
        return [name for bit, name in names if mask & bit]


class DTCSeverity:
    MAINTENANCE_ONLY    = 0x20
    CHECK_AT_NEXT_HALT  = 0x40
    CHECK_IMMEDIATELY   = 0x80

    @staticmethod
    def describe(sev: int) -> str:
        if sev & 0x80: return "Check Immediately"
        if sev & 0x40: return "Check at Next Halt"
        if sev & 0x20: return "Maintenance Only"
        return f"0x{sev:02X}"


import struct as _struct

class DTCRecord:
    """A single decoded DTC entry."""
    def __init__(self, dtc_id: int, status: int, severity: int = 0):
        self.dtc_id   = dtc_id    # 3-byte DTC number
        self.status   = status    # status availability mask
        self.severity = severity

    @property
    def dtc_str(self) -> str:
        """Format as Pxxxx / Cxxxx / Bxxxx / Uxxxx."""
        prefix = {0: "P", 1: "C", 2: "B", 3: "U"}
        group = (self.dtc_id >> 22) & 0x03
        code  = self.dtc_id & 0x3FFF
        return f"{prefix.get(group,'?')}{code:04X}"

    @property
    def status_bits(self) -> list[str]:
        return DTCStatusMask.describe(self.status)

    def __repr__(self) -> str:
        return f"<DTC {self.dtc_str} status=0x{self.status:02X}>"


# ------------------------------------------------------------------ #
#  Extended UDS codec methods                                          #
# ------------------------------------------------------------------ #

class UDSCodecExtended:
    """
    Extension to UDSCodec for diagnostic services:
      0x10  DiagnosticSessionControl
      0x11  ECUReset
      0x14  ClearDiagnosticInformation
      0x19  ReadDTCInformation
      0x22  ReadDataByIdentifier (ECU info DIDs)
    """

    # ── Session ──────────────────────────────────────────────────

    @staticmethod
    def encode_session(session: int) -> bytes:
        return bytes([ServiceID.DIAGNOSTIC_SESSION_CONTROL, session])

    @staticmethod
    def decode_session_response(raw: bytes) -> dict:
        if len(raw) < 2 or raw[0] != (ServiceID.DIAGNOSTIC_SESSION_CONTROL | 0x40):
            raise UDSDecodeError("Not a session response")
        return {"session": raw[1]}

    # ── ECU Reset ────────────────────────────────────────────────

    @staticmethod
    def encode_ecu_reset(reset_type: int = 0x01) -> bytes:
        return bytes([ServiceID.ECU_RESET, reset_type])

    # ── Clear DTC (0x14) ─────────────────────────────────────────

    @staticmethod
    def encode_clear_dtc(group: int = 0xFFFFFF) -> bytes:
        b = _struct.pack(">I", group)
        return bytes([ServiceID.CLEAR_DTC]) + b[1:]   # 3-byte group

    # ── Read DTC (0x19) ──────────────────────────────────────────

    @staticmethod
    def encode_read_dtc_by_status(mask: int = DTCStatusMask.ALL) -> bytes:
        """Sub-function 0x02: reportDTCByStatusMask."""
        return bytes([ServiceID.READ_DTC, 0x02, mask])

    @staticmethod
    def encode_read_dtc_number(mask: int = DTCStatusMask.ALL) -> bytes:
        """Sub-function 0x01: reportNumberOfDTCByStatusMask."""
        return bytes([ServiceID.READ_DTC, 0x01, mask])

    @staticmethod
    def encode_read_supported_dtc() -> bytes:
        """Sub-function 0x0A: reportSupportedDTC."""
        return bytes([ServiceID.READ_DTC, 0x0A])

    @staticmethod
    def decode_dtc_response(raw: bytes) -> list[DTCRecord]:
        """
        Parse 0x59 (0x19 positive response).
        Returns list of DTCRecord.
        """
        if len(raw) < 3:
            return []
        if raw[0] != (ServiceID.READ_DTC | 0x40):
            raise UDSDecodeError(f"Not a DTC response: 0x{raw[0]:02X}")
        # sub_func = raw[1]
        # dtc_status_availability_mask = raw[2]
        records = []
        i = 3
        while i + 3 <= len(raw):
            dtc_id = (raw[i] << 16) | (raw[i+1] << 8) | raw[i+2]
            status = raw[i+3] if i+3 < len(raw) else 0
            records.append(DTCRecord(dtc_id, status))
            i += 4
        return records

    @staticmethod
    def decode_dtc_count(raw: bytes) -> int:
        """Parse 0x01 sub-function response — returns DTC count."""
        if len(raw) < 6 or raw[0] != (ServiceID.READ_DTC | 0x40):
            return 0
        return (raw[4] << 8) | raw[5]

    # ── ECU Info DIDs (0x22) ─────────────────────────────────────

    # Standard ECU info DID numbers (ISO 14229-1 Annex C)
    ECU_INFO_DIDS = {
        0xF186: "Active Diagnostic Session",
        0xF187: "Vehicle Manufacturer Spare Part Number",
        0xF188: "Vehicle Manufacturer ECU Software Number",
        0xF189: "Vehicle Manufacturer ECU Software Version",
        0xF18A: "System Supplier Identifier",
        0xF18B: "ECU Manufacturing Date",
        0xF18C: "ECU Serial Number",
        0xF18D: "Supported Functional Units",
        0xF18E: "Vehicle Manufacturer Kit Assembly Part Number",
        0xF190: "VIN",
        0xF191: "Vehicle Manufacturer ECU Hardware Number",
        0xF192: "System Supplier ECU Hardware Number",
        0xF193: "System Supplier ECU Hardware Version",
        0xF194: "System Supplier ECU Software Number",
        0xF195: "System Supplier ECU Software Version",
        0xF197: "System Name or Engine Type",
        0xF198: "Repair Shop Code or Tester Serial Number",
        0xF199: "Programming Date",
        0xF19E: "ODX File",
        0xF1A0: "Status of Programming Attempts",
    }

    @staticmethod
    def encode_read_ecu_info(did: int) -> bytes:
        return bytes([ServiceID.READ_DATA_BY_ID]) + _struct.pack(">H", did)
