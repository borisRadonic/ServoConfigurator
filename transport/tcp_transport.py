"""
TCP Transport
=============
UDS over TCP/IP — omogućava komunikaciju s lokalnim UDS serverom
koji radi na istom PC-u (npr. BL library test aplikacija).

Protokol (isti framing kao Serial):
    [0xAA][0x55][LEN16 big-endian][UDS PAYLOAD][CRC16-CCITT big-endian]

Primjer pokretanja BL test servera:
    Tvoja aplikacija sluša na TCP portu (default 13400)
    MCTool se spaja kao klijent i šalje UDS zahtjeve

Port 13400 je DoIP (Diagnostics over IP) standard port — možeš koristiti
bilo koji port koji tvoja test aplikacija sluša.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
from typing import Optional

from transport.transport import AbstractTransport, TransportError, crc16_ccitt

log = logging.getLogger(__name__)


class TCPTransport(AbstractTransport):
    """
    UDS over TCP/IP socket.
    Koristi isti length+CRC framing kao SerialTransport.

    Upotreba:
        transport = TCPTransport()
        transport.connect(host="127.0.0.1", port=13400)
    """

    def __init__(self):
        super().__init__()
        self._sock: Optional[socket.socket] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._running = False
        self._event = threading.Event()
        self._last_response: Optional[bytes] = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "TCP/IP"

    def connect(self, host: str = "127.0.0.1", port: int = 13400, **kw) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((host, port))
            self._sock.settimeout(0.1)
            self._running = True
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()
            log.info("TCP connected: %s:%d", host, port)
        except Exception as e:
            raise TransportError(f"TCP connect failed {host}:{port} — {e}") from e

    def disconnect(self) -> None:
        self._running = False
        if self._rx_thread:
            self._rx_thread.join(timeout=1.0)
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        log.info("TCP disconnected")

    def is_connected(self) -> bool:
        return self._sock is not None and self._running

    def _frame(self, payload: bytes) -> bytes:
        crc = crc16_ccitt(payload)
        return b"\xAA\x55" + struct.pack(">H", len(payload)) + payload + struct.pack(">H", crc)

    def send(self, payload: bytes) -> None:
        if not self.is_connected():
            raise TransportError("TCP not connected")
        with self._lock:
            self._sock.sendall(self._frame(payload))

    def send_and_wait(self, payload: bytes, timeout: float = 1.0) -> bytes:
        self._event.clear()
        self._last_response = None
        self.send(payload)
        if not self._event.wait(timeout):
            raise TransportError(f"TCP timeout {timeout}s")
        return self._last_response

    def _rx_loop(self) -> None:
        buf = bytearray()
        while self._running:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    log.warning("TCP server closed connection")
                    self._running = False
                    if self._error_callback:
                        self._error_callback("Server closed connection")
                    break
                buf.extend(chunk)
                self._drain(buf)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    log.error("TCP RX error: %s", e)
                    if self._error_callback:
                        self._error_callback(str(e))
                break

    def _drain(self, buf: bytearray) -> None:
        SOF = b"\xAA\x55"
        while True:
            i = buf.find(SOF)
            if i < 0:
                buf.clear()
                return
            if i:
                del buf[:i]
            if len(buf) < 6:
                return
            n = struct.unpack(">H", buf[2:4])[0]
            total = 4 + n + 2
            if len(buf) < total:
                return
            payload = bytes(buf[4:4 + n])
            crc_r = struct.unpack(">H", buf[4 + n:total])[0]
            del buf[:total]
            if crc_r != crc16_ccitt(payload):
                log.warning("TCP CRC mismatch")
                continue
            self._last_response = payload
            self._event.set()
            if self._response_callback:
                self._response_callback(payload)
