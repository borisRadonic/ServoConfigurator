"""
Application Profile
===================
Loads app_config.yaml and exposes typed feature flags.

Priority:
  1. --config CLI argument
  2. SERVOCONFIG_PROFILE environment variable
  3. app_config.yaml next to main.py
  4. Built-in defaults (all features enabled)

Usage:
    from core.app_profile import profile

    if profile.features.diagnostics.enabled:
        ...
    if profile.transports.mock:
        ...
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Typed config dataclasses                                            #
# ------------------------------------------------------------------ #

@dataclass
class TransportConfig:
    serial: bool = True
    can:    bool = True
    tcp:    bool = True
    mock:   bool = True


@dataclass
class CANConfig:
    default_mode:         str  = "classic"   # "classic" | "fd"
    default_bitrate:      int  = 250000
    default_data_bitrate: int  = 2000000
    allow_fd:             bool = True


@dataclass
class ParameterFeature:
    enabled:   bool = True
    read_only: bool = False


@dataclass
class DiagnosticsFeature:
    enabled:   bool = True
    dtc:       bool = True
    session:   bool = True
    ecu_info:  bool = True
    raw_uds:   bool = True


@dataclass
class FirmwareFeature:
    enabled: bool = True


@dataclass
class DeviceScannerFeature:
    enabled: bool = True


@dataclass
class ChangeAddressFeature:
    enabled: bool = True


@dataclass
class ConfigManagementFeature:
    enabled: bool = True


@dataclass
class FeaturesConfig:
    parameters:           ParameterFeature      = field(default_factory=ParameterFeature)
    diagnostics:          DiagnosticsFeature     = field(default_factory=DiagnosticsFeature)
    firmware:             FirmwareFeature        = field(default_factory=FirmwareFeature)
    device_scanner:       DeviceScannerFeature   = field(default_factory=DeviceScannerFeature)
    change_device_address: ChangeAddressFeature  = field(default_factory=ChangeAddressFeature)
    config_management:     ConfigManagementFeature = field(default_factory=ConfigManagementFeature)


@dataclass
class SimulationConfig:
    enabled:  bool = True
    ecu_info: Dict[int, str]  = field(default_factory=dict)
    dtc_list: list            = field(default_factory=list)


@dataclass
class AppConfig:
    name:         str = "Device Configurator"
    organization: str = ""
    title:        str = "Device Configurator"


@dataclass
class UIConfig:
    theme:             str  = "dark"
    show_did_column:   bool = True
    show_unit_column:  bool = True
    show_desc_column:  bool = True
    parameter_json:    str  = "parameters.json"


@dataclass
class AppProfile:
    app:        AppConfig        = field(default_factory=AppConfig)
    transports: TransportConfig  = field(default_factory=TransportConfig)
    can:        CANConfig        = field(default_factory=CANConfig)
    features:   FeaturesConfig   = field(default_factory=FeaturesConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    ui:         UIConfig         = field(default_factory=UIConfig)
    source:     str              = "defaults"

    # Convenience
    @property
    def mock_enabled(self) -> bool:
        return self.transports.mock and self.simulation.enabled

    @property
    def params_read_only(self) -> bool:
        return (not self.features.parameters.enabled
                or self.features.parameters.read_only)


# ------------------------------------------------------------------ #
#  Loader                                                              #
# ------------------------------------------------------------------ #

def _get(d: dict, *keys, default=None):
    """Safe nested dict access."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, None)
        if d is None:
            return default
    return d


def _parse_dtc_list(raw: list) -> list:
    """Parse DTC list from YAML into list of dicts."""
    result = []
    for item in raw:
        if isinstance(item, dict):
            result.append({
                "dtc":    item.get("dtc", "P0000"),
                "status": item.get("status", 0x08),
            })
    return result


def _parse_ecu_info(raw: dict) -> Dict[int, str]:
    """Parse ECU info dict — keys may be int or hex strings."""
    result = {}
    for k, v in raw.items():
        try:
            did = int(str(k), 0)
            result[did] = str(v)
        except (ValueError, TypeError):
            pass
    return result


def load_profile(path: Optional[Path] = None) -> AppProfile:
    """
    Load AppProfile from YAML file.
    Falls back to built-in defaults if file not found or invalid.
    """
    # Resolve path
    if path is None:
        env = os.environ.get("SERVOCONFIG_PROFILE")
        if env:
            path = Path(env)
        else:
            # Look next to main.py
            for candidate in [
                Path(__file__).parent.parent / "app_config.yaml",
                Path.cwd() / "app_config.yaml",
            ]:
                if candidate.exists():
                    path = candidate
                    break

    if path is None or not path.exists():
        log.info("No app_config.yaml found — using built-in defaults (all features enabled)")
        return AppProfile(source="defaults")

    try:
        import yaml
    except ImportError:
        log.warning("PyYAML not installed — using defaults. Run: pip install pyyaml")
        return AppProfile(source="defaults (pyyaml missing)")

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        log.error("Failed to load %s: %s — using defaults", path, e)
        return AppProfile(source=f"defaults (load error: {e})")

    # Parse into typed structure
    p = AppProfile(source=str(path))

    # app
    app_raw = raw.get("app", {})
    p.app.name         = _get(app_raw, "name",         default=p.app.name)
    p.app.organization = _get(app_raw, "organization",  default="")
    p.app.title        = _get(app_raw, "title",         default=p.app.title)

    # transports
    t = raw.get("transports", {})
    p.transports.serial = bool(_get(t, "serial", default=True))
    p.transports.can    = bool(_get(t, "can",    default=True))
    p.transports.tcp    = bool(_get(t, "tcp",    default=True))
    p.transports.mock   = bool(_get(t, "mock",   default=True))

    # can
    can_raw = raw.get("can", {})
    p.can.default_mode         = str(_get(can_raw, "default_mode",         default="classic"))
    p.can.default_bitrate      = int(_get(can_raw, "default_bitrate",      default=250000))
    p.can.default_data_bitrate = int(_get(can_raw, "default_data_bitrate", default=2000000))
    p.can.allow_fd             = bool(_get(can_raw, "allow_fd",            default=True))

    # features.parameters
    fp = raw.get("features", {}).get("parameters", {})
    p.features.parameters.enabled   = bool(_get(fp, "enabled",   default=True))
    p.features.parameters.read_only = bool(_get(fp, "read_only", default=False))

    # features.diagnostics
    fd = raw.get("features", {}).get("diagnostics", {})
    p.features.diagnostics.enabled  = bool(_get(fd, "enabled",  default=True))
    p.features.diagnostics.dtc      = bool(_get(fd, "dtc",      default=True))
    p.features.diagnostics.session  = bool(_get(fd, "session",  default=True))
    p.features.diagnostics.ecu_info = bool(_get(fd, "ecu_info", default=True))
    p.features.diagnostics.raw_uds  = bool(_get(fd, "raw_uds",  default=True))

    # features.firmware
    ff = raw.get("features", {}).get("firmware", {})
    p.features.firmware.enabled = bool(_get(ff, "enabled", default=True))

    # features.device_scanner
    fs = raw.get("features", {}).get("device_scanner", {})
    p.features.device_scanner.enabled = bool(_get(fs, "enabled", default=True))

    # features.change_device_address
    fc = raw.get("features", {}).get("change_device_address", {})
    p.features.change_device_address.enabled = bool(_get(fc, "enabled", default=True))

    # features.config_management
    fcm = raw.get("features", {}).get("config_management", {})
    p.features.config_management.enabled = bool(_get(fcm, "enabled", default=True))

    # simulation
    sim = raw.get("simulation", {})
    p.simulation.enabled  = bool(_get(sim, "enabled", default=True))
    p.simulation.ecu_info = _parse_ecu_info(_get(sim, "ecu_info", default={}))
    p.simulation.dtc_list = _parse_dtc_list(_get(sim, "dtc_list", default=[]))

    # ui
    ui = raw.get("ui", {})
    p.ui.theme             = str(_get(ui, "theme",             default="dark"))
    p.ui.show_did_column   = bool(_get(ui, "show_did_column",  default=True))
    p.ui.show_unit_column  = bool(_get(ui, "show_unit_column", default=True))
    p.ui.show_desc_column  = bool(_get(ui, "show_desc_column", default=True))
    p.ui.parameter_json    = str(_get(ui, "parameter_json",    default="parameters.json"))

    # Ensure consistency: mock transport must be on for simulation
    if not p.transports.mock:
        p.simulation.enabled = False

    log.info("Profile loaded from %s", path)
    log.info("  transports: serial=%s can=%s tcp=%s mock=%s",
             p.transports.serial, p.transports.can,
             p.transports.tcp,    p.transports.mock)
    log.info("  features: params=%s diag=%s fw=%s scanner=%s",
             p.features.parameters.enabled,
             p.features.diagnostics.enabled,
             p.features.firmware.enabled,
             p.features.device_scanner.enabled)

    return p


# ------------------------------------------------------------------ #
#  Global singleton — loaded once at startup                           #
# ------------------------------------------------------------------ #

profile: AppProfile = AppProfile(source="not-loaded")


def init_profile(path: Optional[Path] = None) -> AppProfile:
    """Call once from main.py. Sets the global `profile` singleton."""
    global profile
    profile = load_profile(path)
    return profile
