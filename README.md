# MCTool – Motor Controller Configuration Tool

A modular, layered PySide6 desktop application for configuring, diagnosing,
and managing FOC motor controllers over UDS (ISO 14229-1).

---

## Architecture

```
mctool/
├── main.py                      ← Entry point, Qt app setup, theme
│
├── core/
│   └── parameter_model.py       ← ParameterDefinition, ParameterValue, ParameterStore (QObject)
│                                   Pure data layer – no transport dependency
│
├── uds/
│   ├── codec.py                 ← Stateless UDS PDU encoder/decoder
│   │                               ServiceID, NRC, DataCodec, UDSCodec
│   └── client.py                ← UDSClient (main thread facade)
│                                   _UDSWorker (QThread) – non-blocking RDBI/WDBI
│
├── transport/
│   └── transport.py             ← AbstractTransport base class
│                                   SerialTransport  – length-framed UART
│                                   CANTransport     – ISO 15765-2 ISO-TP
│                                   MockTransport    – offline simulation
│
├── gui/
│   ├── connection_dialog.py     ← Transport selector + connect UI
│   ├── parameter_model_qt.py    ← QAbstractTableModel + QStyledItemDelegate
│   ├── parameter_panel.py       ← Category sidebar + parameter table
│   └── main_window.py           ← MainWindow, menus, status bar, tab host
│
├── resources/
│   └── style_dark.qss           ← Catppuccin-inspired dark theme
│
└── parameters.json              ← FOC parameter definitions (DID, type, range, …)
```

### Layer separation

```
GUI  ──→  ParameterStore.request_write(did, value)
                  ↓  (Qt signal)
         UDSClient._on_write_requested(did, value)
                  ↓  (QueuedConnection → worker thread)
          _UDSWorker.write_one(did, value)
                  ↓
          UDSCodec.encode_write_data_by_id()
                  ↓
          AbstractTransport.send_and_wait()
                  ↓
          UDSCodec.decode_response()
                  ↓  (signal back to main thread)
          ParameterStore.update_from_device(did, value)
                  ↓  (Qt signal)
          GUI updates automatically
```

---

## Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows

# Install dependencies
pip install -r requirements.txt

# Run (simulation mode – no hardware required)
python main.py
```

---

## Running with hardware

### Serial (UART)
1. Connect USB-UART adapter to motor controller
2. Launch MCTool → Device → Connect → Serial
3. Select COM port / /dev/ttyUSB0, set baudrate (default 115200)

**Firmware framing (adjust in `transport/transport.py` if needed):**
```
[0xAA][0x55][LEN_HI][LEN_LO][UDS_PAYLOAD...][CRC16_HI][CRC16_LO]
```

### CAN (PEAK USB-CAN)
```bash
# Windows
pip install "python-can[pcan]"

# Linux – load kernel module first
sudo modprobe peak_usb
pip install python-can
```
Launch MCTool → Device → Connect → CAN (PEAK/python-can)
- Interface: `pcan`
- Channel: `PCAN_USBBUS1`
- Bitrate: 500 kbit/s
- TX ID: `0x7E0`  RX ID: `0x7E8`

### Simulation (no hardware)
Device → Connect → Simulation (Mock)
All parameters are read with plausible default values.

---

## Parameter JSON format

```json
{
  "did":         "0x1001",
  "name":        "Motor.PolePairs",
  "description": "Number of motor pole pairs",
  "category":    "Motor",
  "type":        "uint8",          // uint8 uint16 uint32 int8 int16 int32 float bool enum
  "unit":        "-",
  "min":         1,
  "max":         64,
  "step":        1,
  "readOnly":    false,
  "visible":     true
}
```

Enum parameters also have:
```json
"values": { "0": "None", "1": "Incremental", "4": "Hall" }
```

---

## Data encoding (firmware contract)

All multi-byte integers and floats use **little-endian** byte order
(matches STM32 native). The DID in the UDS PDU header uses
**big-endian** per ISO 14229-1.

To change byte order, edit `DataCodec.encode()` / `.decode()` in `uds/codec.py`.

---

## Planned extensions

| Feature            | Where to add                          |
|--------------------|---------------------------------------|
| Real-time plotter  | `gui/plotter_panel.py` + pyqtgraph    |
| Firmware update    | `uds/firmware_update.py` (0x34/0x36/0x37) |
| DTC reader         | `gui/dtc_panel.py` + codec 0x19       |
| Security Access    | `uds/security.py` (seed/key 0x27)    |
| Config save/load   | `core/config_manager.py`             |
| Version management | `uds/version.py`                     |
| Session management | Already in `UDSClient.set_session()` |
| Logging to CSV     | `core/logger.py`                     |
