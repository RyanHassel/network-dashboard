import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import asyncpg
import redis.asyncio as aioredis

from app.mib_catalog import match_device_vendor_profile

logger = logging.getLogger("netdash.snmp")

# Standard MIB-II / IF-MIB OIDs
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_PHYS_ADDR = "1.3.6.1.2.1.2.2.1.6"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
OID_IF_IN_DISCARDS = "1.3.6.1.2.1.2.2.1.13"
OID_IF_IN_ERRORS = "1.3.6.1.2.1.2.2.1.14"
OID_IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"
OID_IF_OUT_DISCARDS = "1.3.6.1.2.1.2.2.1.19"
OID_IF_OUT_ERRORS = "1.3.6.1.2.1.2.2.1.20"
OID_IP_ADDR_TABLE = "1.3.6.1.2.1.4.20.1"
OID_BRIDGE_MAC = "1.3.6.1.2.1.17.1.1.0"

OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"

# Universal Enrichment MIBs
OID_HR_PROC_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
OID_HR_STORAGE = "1.3.6.1.2.1.25.2.3.1"
OID_HR_SYSTEM = "1.3.6.1.2.1.25.1"
OID_ENT_PHYSICAL = "1.3.6.1.2.1.47.1.1.1.1"
OID_LLDP_LOC_CHASSIS = "1.0.8802.1.1.2.1.3.2.0"
OID_LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"

# UCD-SNMP-MIB (Linux, Proxmox, Ubiquiti, Synology, TrueNAS, QNAP)
OID_UCD_MEMORY = "1.3.6.1.4.1.2021.4"
OID_UCD_DISK = "1.3.6.1.4.1.2021.9.1"
OID_UCD_LOAD = "1.3.6.1.4.1.2021.10.1.3"
OID_UCD_CPU = "1.3.6.1.4.1.2021.11"
OID_LM_SENSORS = "1.3.6.1.4.1.2021.13.16"


def _to_float(val: Optional[str], default: float = 0.0) -> float:
    if not val:
        return default
    clean = val.strip().split()[0].replace('"', "")
    try:
        return float(clean)
    except ValueError:
        return default


def _to_int(val: Optional[str], default: int = 0) -> int:
    if not val:
        return default
    clean = val.strip().split()[0].replace('"', "")
    try:
        return int(float(clean))
    except ValueError:
        return default


def _compute_safe_rate_bps(
    delta_octets: int,
    dt: float,
    speed_bps: int,
    prev_octets: int,
    max_fallback_bps: float = 40_000_000_000.0,
    min_dt: float = 1.0,
) -> float:
    """Computes validated interface bps from an octet counter delta, rejecting spike artifacts."""
    if delta_octets <= 0 or dt < min_dt:
        return 0.0
    rate_bps = (delta_octets * 8.0) / dt
    # Guard against cumulative lifetime octets jumping from a 0 baseline
    if prev_octets == 0 and rate_bps > 1_000_000_000.0:
        logger.debug("Ignoring implausible zero-baseline SNMP rate jump: %.2f bps", rate_bps)
        return 0.0
    max_bps = max(speed_bps * 1.25, 100_000_000.0) if speed_bps > 0 else max_fallback_bps
    if rate_bps > max_bps:
        logger.debug("Ignoring implausible SNMP rate %.2f bps > ceiling %.2f bps", rate_bps, max_bps)
        return 0.0
    return round(rate_bps, 2)


def _parse_timeticks(raw: Optional[str]) -> int:
    """Parses Net-SNMP numeric ticks, Timeticks: (N), D:H:MM:SS.cc, or H:MM:SS.cc into centiseconds."""
    if not raw:
        return 0
    s = raw.strip().strip('"')
    if not s or "No Such" in s:
        return 0
    # Plain numeric integer/float (e.g. from -Ot)
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return int(float(s))
    # Explicit "Timeticks: (123456) 0:20:34.56"
    m_ticks = re.search(r"\((\d+)\)", s)
    if m_ticks:
        return int(m_ticks.group(1))
    # D:H:MM:SS(.cc) or H:MM:SS(.cc)
    m_hms = re.match(r"^(?:(\d+):)?(\d+):(\d+):(\d+(?:\.\d+)?)(?:\s|$)", s)
    if m_hms:
        days = int(m_hms.group(1) or 0)
        hours = int(m_hms.group(2))
        mins = int(m_hms.group(3))
        secs = float(m_hms.group(4))
        total_secs = days * 86400 + hours * 3600 + mins * 60 + secs
        return int(round(total_secs * 100))
    return 0


def _normalize_mac(raw: Optional[str]) -> Optional[str]:
    """Normalizes SNMP MAC strings into lowercase colon-delimited aa:bb:cc:dd:ee:ff."""
    if not raw:
        return None
    s = raw.strip().strip('"')
    for prefix in ("Hex-STRING:", "STRING:"):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix) :].strip()
    if not s or "No Such" in s:
        return None

    tokens = [t for t in re.split(r"[:\-\s.]+", s) if t]
    if len(tokens) == 6 and all(re.fullmatch(r"[0-9a-fA-F]{1,2}", t) for t in tokens):
        octets = [t.zfill(2).lower() for t in tokens]
    else:
        hex_only = re.sub(r"[^0-9a-fA-F]", "", s)
        if len(hex_only) != 12:
            return None
        octets = [hex_only[i : i + 2].lower() for i in range(0, 12, 2)]

    mac = ":".join(octets)
    if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return None
    # Reject multicast/group MACs (LSB of first octet == 1)
    if int(octets[0], 16) & 1:
        return None
    return mac


def _parse_sys_descr_metadata(sys_descr: str, vendor_key: str) -> Dict[str, str]:
    """Extracts hardware model, firmware version, serial, and OS/kernel fallback info from sysDescr."""
    descr = (sys_descr or "").strip()
    out = {"hardware_model": "", "firmware_version": "", "serial_number": "", "os_kernel": ""}
    if not descr:
        return out

    if vendor_key == "ubiquiti" or any(
        kw in descr.lower() for kw in ("ubiquiti", "unifi", "ubnt", "edgeos", "udm", "usw", "uap")
    ):
        # Match UniFi model names: UDM-Pro, UDM-SE, UCG-Ultra, UXG-Pro, USW-Pro-24-PoE, UAP-AC-Pro, U6-Pro, U7-Pro, etc.
        m_unifi = re.search(
            r"\b((?:UDM|UCG|UXG|USG|USW|USL|UAP|U6|U7|UK|UNVR|UCK)[A-Za-z0-9-]*)\b(?:\s+v?(\d+\.\d+(?:\.\d+)+))?",
            descr,
        )
        if m_unifi:
            out["hardware_model"] = m_unifi.group(1)
            if m_unifi.group(2):
                out["firmware_version"] = m_unifi.group(2)
        else:
            m_edge = re.search(r"\bEdge(?:Router|Switch|OS)[^,\n]*?\b(v?\d+\.\d+\.\d+[^\s,]*)", descr, re.I)
            if m_edge:
                out["firmware_version"] = m_edge.group(1)
            m_model = re.search(r"\b(Edge(?:Router|Switch)[A-Za-z0-9-]*)\b", descr, re.I)
            if m_model:
                out["hardware_model"] = m_model.group(1)

    elif vendor_key == "mikrotik":
        m_mt = re.search(r"\bRouterOS\s+([A-Za-z0-9+._/-]+)", descr, re.I)
        if m_mt:
            out["hardware_model"] = m_mt.group(1)

    elif vendor_key == "cisco":
        m_cs = re.search(r"\bVersion\s+([0-9A-Za-z()._-]+)", descr, re.I)
        if m_cs:
            out["firmware_version"] = m_cs.group(1).rstrip(",")

    elif vendor_key in ("apc", "cyberpower", "eaton", "ups") or any(
        kw in descr.lower() for kw in ("apc ", "smart-ups", "back-ups", "ap96")
    ):
        m_mn = re.search(r"\bMN:([^\s)]+)", descr)
        m_pf = re.search(r"\bPF:([^\s)]+)", descr)
        m_sn = re.search(r"\bSN:([^\s)]+)", descr)
        if m_mn:
            out["hardware_model"] = m_mn.group(1).strip()
        else:
            m_ups = re.search(r"\b((?:Smart|Back)-UPS(?:\s+[A-Za-z0-9-]+){0,3})", descr, re.I)
            if m_ups:
                out["hardware_model"] = m_ups.group(1).strip()
        if m_pf:
            out["firmware_version"] = m_pf.group(1).strip()
        if m_sn:
            out["serial_number"] = m_sn.group(1).strip()

    # Extract Linux kernel + architecture from sysDescr regardless of vendor
    m_linux = re.search(
        r"\bLinux\s+(?:[A-Za-z][A-Za-z0-9._-]*\s+)?(\d+\.\d+\.\d+[^\s,]*)(?:\s+(al\d+|aarch64|arm64|armv7l|x86_64|amd64|mips(?:64)?))?",
        descr,
    )
    if m_linux:
        kernel = m_linux.group(1)
        arch = m_linux.group(2)
        out["os_kernel"] = f"Linux {kernel} ({arch})" if arch else f"Linux {kernel}"

    return out


def _extract_ip_and_mac(
    target: str,
    ip_addr_table: Dict[str, str],
    if_phys: Dict[str, str],
    if_names: Dict[str, str],
    if_oper: Dict[str, str],
    lldp_loc_chassis: str = "",
    bridge_mac: str = "",
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """Resolves primary device MAC, target subnet CIDR, and non-loopback IPv4 addresses."""
    ips_seen: Set[str] = set()
    for k in ip_addr_table.keys():
        if "." in k and k.split(".", 1)[0] in ("1", "2", "3"):
            ips_seen.add(k.split(".", 1)[1])

    ip_addresses: List[Dict[str, Any]] = []
    target_subnet_cidr = ""
    target_if_idx = 0

    for ip_str in ips_seen:
        try:
            ip_obj = ipaddress.IPv4Address(ip_str)
        except ValueError:
            continue
        if ip_obj.is_loopback:
            continue

        mask = (ip_addr_table.get(f"3.{ip_str}") or "").strip()
        if_idx = _to_int(ip_addr_table.get(f"2.{ip_str}"), 0)
        if_name = (if_names.get(str(if_idx)) or "").strip()
        mac_for_if = _normalize_mac(if_phys.get(str(if_idx))) or ""

        cidr = ""
        if mask:
            try:
                net_obj = ipaddress.IPv4Network(f"{ip_str}/{mask}", strict=False)
                cidr = str(net_obj)
            except ValueError:
                cidr = ""

        if ip_str == target:
            target_subnet_cidr = cidr
            target_if_idx = if_idx

        ip_addresses.append(
            {
                "ip": ip_str,
                "netmask": mask,
                "cidr": cidr,
                "if_index": if_idx,
                "if_name": if_name,
                "mac_address": mac_for_if,
            }
        )

    ip_addresses.sort(key=lambda item: int(ipaddress.IPv4Address(item["ip"])))

    if not target_subnet_cidr:
        try:
            target_ip_obj = ipaddress.IPv4Address(target)
            for item in ip_addresses:
                if item["cidr"] and target_ip_obj in ipaddress.IPv4Network(item["cidr"], strict=False):
                    target_subnet_cidr = item["cidr"]
                    if not target_if_idx:
                        target_if_idx = int(item["if_index"] or 0)
                    break
        except ValueError:
            pass

    def _is_loopback_name(idx_str: str) -> bool:
        nm = (if_names.get(idx_str) or "").strip().lower()
        return nm in ("lo", "loopback", "null0")

    primary_mac: Optional[str] = None
    if target_if_idx > 0 and not _is_loopback_name(str(target_if_idx)):
        primary_mac = _normalize_mac(if_phys.get(str(target_if_idx)))

    sorted_if_keys = sorted(if_phys.keys(), key=lambda x: int(x) if x.isdigit() else 999999)
    if not primary_mac:
        for k in sorted_if_keys:
            if _is_loopback_name(k):
                continue
            oper_raw = (if_oper.get(k) or "").strip().lower()
            is_up = _to_int(oper_raw, 2) == 1 or oper_raw.startswith("up")
            if is_up:
                candidate = _normalize_mac(if_phys.get(k))
                if candidate:
                    primary_mac = candidate
                    break

    if not primary_mac:
        for k in sorted_if_keys:
            if _is_loopback_name(k):
                continue
            candidate = _normalize_mac(if_phys.get(k))
            if candidate:
                primary_mac = candidate
                break

    if not primary_mac:
        primary_mac = _normalize_mac(lldp_loc_chassis) or _normalize_mac(bridge_mac)

    return (primary_mac or "", target_subnet_cidr, ip_addresses)


def _parse_lm_sensors(lm_data: Dict[str, str]) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    """Parses temperature, fan RPM, and voltage sensors from LM-SENSORS-MIB (1.3.6.1.4.1.2021.13.16)."""
    sensors: List[Dict[str, Any]] = []
    temp_celsius: Optional[float] = None
    if not lm_data:
        return (temp_celsius, sensors)

    # Support both .13.16 (keys 2.1.2.<idx>) and direct .2.1 (keys 2.<idx>) formats
    def _collect_table(sub_prefix: str, short_mode: bool = False) -> Dict[str, Tuple[str, str]]:
        name_pfx = "2." if short_mode else f"{sub_prefix}.2."
        val_pfx = "3." if short_mode else f"{sub_prefix}.3."
        indices = {
            k[len(name_pfx) :]
            for k in lm_data.keys()
            if k.startswith(name_pfx) and k[len(name_pfx) :]
        }
        rows: Dict[str, Tuple[str, str]] = {}
        for idx in sorted(indices, key=lambda x: int(x) if x.isdigit() else 999999):
            rows[idx] = (lm_data.get(f"{name_pfx}{idx}", ""), lm_data.get(f"{val_pfx}{idx}", ""))
        return rows

    temp_rows = _collect_table("2.1") or (
        _collect_table("2.1", short_mode=True) if any(k.startswith("2.") and not k.startswith("2.1.") for k in lm_data) else {}
    )
    for t_idx, (t_name, raw_val) in temp_rows.items():
        raw_mdeg = _to_float(raw_val, 0.0)
        deg_c = round(raw_mdeg / 1000.0, 1) if raw_mdeg > 200 else round(raw_mdeg, 1)
        if 0 < deg_c < 150:
            if temp_celsius is None:
                temp_celsius = deg_c
            sensors.append(
                {
                    "name": t_name or f"Temp #{t_idx}",
                    "category": "Temperature",
                    "value": f"{deg_c} °C",
                    "status": "warning" if deg_c >= 80 else "ok",
                }
            )

    for f_idx, (f_name, raw_val) in _collect_table("3.1").items():
        rpm = _to_int(raw_val, -1)
        if rpm >= 0:
            sensors.append(
                {
                    "name": f_name or f"Fan #{f_idx}",
                    "category": "Fan",
                    "value": f"{rpm} RPM",
                    "status": "warning" if rpm == 0 else "ok",
                }
            )

    for v_idx, (v_name, raw_val) in _collect_table("4.1").items():
        mv = _to_float(raw_val, -1.0)
        if mv >= 0:
            volts = round(mv / 1000.0, 2)
            sensors.append(
                {
                    "name": v_name or f"Voltage #{v_idx}",
                    "category": "Voltage",
                    "value": f"{volts:.2f} V",
                    "status": "ok",
                }
            )

    return (temp_celsius, sensors)


_APC_OUTPUT_STATUS_MAP: Dict[int, str] = {
    1: "Unknown",
    2: "On Line",
    3: "On Battery",
    4: "On Smart Boost",
    5: "Timed Sleeping",
    6: "Software Bypass",
    7: "Off",
    8: "Rebooting",
    9: "Switched Bypass",
    10: "Hardware Failure Bypass",
    11: "Sleeping Until Power Return",
    12: "On Smart Trim",
    13: "Eco Mode",
    14: "Hot Standby",
    15: "On Battery Test",
}

_RFC_OUTPUT_SOURCE_MAP: Dict[int, str] = {
    1: "Other",
    2: "None (Off)",
    3: "On Line",
    4: "Bypass",
    5: "On Battery",
    6: "On Smart Boost",
    7: "On Smart Trim",
}

_APC_BATT_STATUS_MAP: Dict[int, str] = {
    1: "Unknown",
    2: "Normal",
    3: "Low",
    4: "Fault",
}

_RFC_BATT_STATUS_MAP: Dict[int, str] = {
    1: "Unknown",
    2: "Normal",
    3: "Low",
    4: "Depleted",
}

_APC_TRANSFER_REASON_MAP: Dict[int, str] = {
    1: "No Transfer",
    2: "High Line Voltage",
    3: "Brownout",
    4: "Blackout",
    5: "Small Momentary Sag",
    6: "Deep Momentary Sag",
    7: "Small Momentary Spike",
    8: "Large Momentary Spike",
    9: "Self-Test",
    10: "Rate of Voltage Change",
}

_APC_TEST_RESULT_MAP: Dict[int, str] = {
    1: "Passed",
    2: "Failed",
    3: "Invalid",
    4: "In Progress",
}

_APC_CALIBRATION_RESULT_MAP: Dict[int, str] = {
    1: "Passed",
    2: "Invalid",
    3: "In Progress",
}


def _parse_ups_telemetry(
    apc_ident: Dict[str, str],
    apc_batt: Dict[str, str],
    apc_in: Dict[str, str],
    apc_out: Dict[str, str],
    apc_test: Dict[str, str],
    std_ups: Dict[str, str],
) -> Dict[str, Any]:
    """Parses APC PowerNet-UPS-MIB (.318.1.1.1.{1,2,3,4,7}) and RFC 1628 UPS-MIB (.33.1) telemetry."""

    def _first_valid(candidates: List[Tuple[Optional[str], float]], min_val: float = 0.0) -> Optional[float]:
        for raw, scale in candidates:
            if raw is None or str(raw).strip() == "":
                continue
            val = _to_float(raw, -99999.0)
            if val >= min_val:
                return round(val * scale, 1)
        return None

    def _first_str(*vals: Optional[str]) -> str:
        for v in vals:
            s = str(v or "").strip()
            if s and "No Such" not in s:
                return s
        return ""

    hardware_model = _first_str(apc_ident.get("1.1.0"), std_ups.get("1.2.0"))
    firmware_version = _first_str(
        apc_ident.get("2.1.0"), std_ups.get("1.3.0"), std_ups.get("1.4.0")
    )
    serial_number = _first_str(apc_ident.get("2.3.0"))
    sku = _first_str(apc_ident.get("2.5.0"))
    manufacture_date = _first_str(apc_ident.get("2.2.0"))

    # Operating status
    out_status_code = _to_int(apc_out.get("1.1.0"), 0)
    output_status = ""
    on_battery = False
    if out_status_code > 0:
        output_status = _APC_OUTPUT_STATUS_MAP.get(out_status_code, f"Status #{out_status_code}")
        on_battery = out_status_code in (3, 15)
    elif std_ups.get("4.1.0"):
        rfc_code = _to_int(std_ups.get("4.1.0"), 0)
        output_status = _RFC_OUTPUT_SOURCE_MAP.get(rfc_code, f"Source #{rfc_code}")
        on_battery = rfc_code == 5

    # Battery metrics
    batt_status_code = _to_int(apc_batt.get("1.1.0"), 0)
    battery_status = ""
    if batt_status_code > 0:
        battery_status = _APC_BATT_STATUS_MAP.get(batt_status_code, f"Status #{batt_status_code}")
    elif std_ups.get("2.1.0"):
        rfc_batt = _to_int(std_ups.get("2.1.0"), 0)
        battery_status = _RFC_BATT_STATUS_MAP.get(rfc_batt, f"Status #{rfc_batt}")

    replace_code = _to_int(apc_batt.get("2.4.0"), 0)
    battery_replace_needed = replace_code == 2

    battery_charge_pct = _first_valid(
        [(apc_batt.get("3.1.0"), 0.1), (apc_batt.get("2.1.0"), 1.0), (std_ups.get("2.4.0"), 1.0)]
    )
    battery_temp_c = _first_valid(
        [(apc_batt.get("3.2.0"), 0.1), (apc_batt.get("2.2.0"), 1.0), (std_ups.get("2.7.0"), 1.0)],
        min_val=0.1,
    )

    runtime_remaining_sec: Optional[int] = None
    if apc_batt.get("2.3.0") is not None:
        ticks = _parse_timeticks(apc_batt.get("2.3.0"))
        if ticks > 0 or str(apc_batt.get("2.3.0")).strip().isdigit():
            runtime_remaining_sec = ticks // 100
    if runtime_remaining_sec is None and std_ups.get("2.3.0") is not None:
        mins = _to_int(std_ups.get("2.3.0"), -1)
        if mins >= 0:
            runtime_remaining_sec = mins * 60

    time_on_battery_sec = 0
    if apc_batt.get("1.2.0") is not None:
        time_on_battery_sec = _parse_timeticks(apc_batt.get("1.2.0")) // 100
    elif std_ups.get("2.2.0") is not None:
        time_on_battery_sec = max(0, _to_int(std_ups.get("2.2.0"), 0))

    battery_voltage_v = _first_valid(
        [(apc_batt.get("3.4.0"), 0.1), (apc_batt.get("2.8.0"), 1.0), (std_ups.get("2.5.0"), 0.1)]
    )
    battery_nominal_voltage_v = _first_valid([(apc_batt.get("2.7.0"), 1.0)], min_val=1.0)
    last_replace_date = _first_str(apc_batt.get("1.3.0"))
    external_battery_packs = (
        _to_int(apc_batt.get("2.5.0"), 0) if apc_batt.get("2.5.0") is not None else None
    )
    bad_battery_packs = (
        _to_int(apc_batt.get("2.6.0"), 0) if apc_batt.get("2.6.0") is not None else None
    )

    # Utility input
    input_voltage_v = _first_valid(
        [(apc_in.get("3.1.0"), 0.1), (apc_in.get("2.1.0"), 1.0), (std_ups.get("3.3.1.3.1"), 1.0)]
    )
    input_max_voltage_v = _first_valid([(apc_in.get("3.2.0"), 0.1), (apc_in.get("2.2.0"), 1.0)])
    input_min_voltage_v = _first_valid([(apc_in.get("3.3.0"), 0.1), (apc_in.get("2.3.0"), 1.0)])
    input_frequency_hz = _first_valid(
        [(apc_in.get("3.4.0"), 0.1), (apc_in.get("2.4.0"), 1.0), (std_ups.get("3.3.1.2.1"), 0.1)]
    )
    transfer_code = _to_int(apc_in.get("2.5.0"), 0)
    last_transfer_reason = (
        _APC_TRANSFER_REASON_MAP.get(transfer_code, f"Cause #{transfer_code}")
        if transfer_code > 0
        else ""
    )

    # Output power & load
    output_voltage_v = _first_valid(
        [(apc_out.get("3.1.0"), 0.1), (apc_out.get("2.1.0"), 1.0), (std_ups.get("4.4.1.2.1"), 1.0)]
    )
    output_frequency_hz = _first_valid(
        [(apc_out.get("3.2.0"), 0.1), (apc_out.get("2.2.0"), 1.0), (std_ups.get("4.2.0"), 0.1)]
    )
    output_load_pct = _first_valid(
        [(apc_out.get("3.3.0"), 0.1), (apc_out.get("2.3.0"), 1.0), (std_ups.get("4.4.1.5.1"), 1.0)]
    )
    output_current_a = _first_valid(
        [(apc_out.get("3.4.0"), 0.1), (apc_out.get("2.4.0"), 1.0), (std_ups.get("4.4.1.3.1"), 0.1)]
    )
    output_watts = _first_valid([(apc_out.get("2.8.0"), 1.0), (std_ups.get("4.4.1.4.1"), 1.0)])
    output_va = _first_valid([(apc_out.get("2.9.0"), 1.0)])
    raw_energy = _first_valid([(apc_out.get("2.10.0"), 1.0)], min_val=0.1)
    energy_kwh = raw_energy if raw_energy is not None and raw_energy > 0 else None

    # Diagnostics & alarms
    test_code = _to_int(apc_test.get("2.3.0"), 0)
    self_test_result = _APC_TEST_RESULT_MAP.get(test_code, "") if test_code > 0 else ""
    self_test_date = _first_str(apc_test.get("2.4.0"))
    cal_code = _to_int(apc_test.get("2.6.0"), 0)
    calibration_result = _APC_CALIBRATION_RESULT_MAP.get(cal_code, "") if cal_code > 0 else ""
    calibration_date = _first_str(apc_test.get("2.7.0"))
    alarms_present = _to_int(std_ups.get("6.1.0"), 0) if std_ups.get("6.1.0") is not None else 0

    return {
        "hardware_model": hardware_model,
        "firmware_version": firmware_version,
        "serial_number": serial_number,
        "sku": sku,
        "manufacture_date": manufacture_date,
        "output_status": output_status,
        "output_status_code": out_status_code,
        "on_battery": on_battery,
        "battery_status": battery_status,
        "battery_replace_needed": battery_replace_needed,
        "battery_charge_pct": battery_charge_pct,
        "battery_temp_c": battery_temp_c,
        "runtime_remaining_sec": runtime_remaining_sec,
        "time_on_battery_sec": time_on_battery_sec,
        "battery_voltage_v": battery_voltage_v,
        "battery_nominal_voltage_v": battery_nominal_voltage_v,
        "last_replace_date": last_replace_date,
        "external_battery_packs": external_battery_packs,
        "bad_battery_packs": bad_battery_packs,
        "input_voltage_v": input_voltage_v,
        "input_max_voltage_v": input_max_voltage_v,
        "input_min_voltage_v": input_min_voltage_v,
        "input_frequency_hz": input_frequency_hz,
        "last_transfer_reason": last_transfer_reason,
        "output_voltage_v": output_voltage_v,
        "output_frequency_hz": output_frequency_hz,
        "output_load_pct": output_load_pct,
        "output_current_a": output_current_a,
        "output_watts": output_watts,
        "output_va": output_va,
        "energy_kwh": energy_kwh,
        "self_test_result": self_test_result,
        "self_test_date": self_test_date,
        "calibration_result": calibration_result,
        "calibration_date": calibration_date,
        "alarms_present": alarms_present,
    }


def _parse_printer_supplies(prt_mib: Dict[str, str]) -> List[Dict[str, Any]]:
    """Extracts toner/drum/ink supply levels from Printer-MIB::prtMarkerSuppliesTable (11.1.1.*)."""
    supplies: List[Dict[str, Any]] = []
    if not prt_mib:
        return supplies
    indices = {
        k[len("11.1.1.6.") :]
        for k in prt_mib.keys()
        if k.startswith("11.1.1.6.") and k[len("11.1.1.6.") :]
    }
    for s_idx in sorted(indices):
        descr = (prt_mib.get(f"11.1.1.6.{s_idx}") or "").strip()
        if not descr:
            continue
        max_cap = _to_int(prt_mib.get(f"11.1.1.8.{s_idx}"), 0)
        level = _to_int(prt_mib.get(f"11.1.1.9.{s_idx}"), -1)
        pct: Optional[float] = None
        if max_cap > 0 and level >= 0:
            pct = round(min(100.0, max(0.0, (level / max_cap) * 100.0)), 1)
        supplies.append(
            {
                "name": descr,
                "level": level,
                "max_capacity": max_cap,
                "pct": pct,
            }
        )
    return supplies[:16]


class SNMPPoller:
    """Asynchronous SNMPv2c/v3 GETBULK collector with Vendor MIB fingerprinting & enrichment."""

    def __init__(self) -> None:
        self.default_version = os.getenv("SNMP_VERSION", "v2c").strip().lower()
        self.default_community = os.getenv("SNMP_COMMUNITY", "public").strip() or "public"
        self.default_port = int(os.getenv("SNMP_PORT", "161") or 161)
        # Isolated in-memory state for 2s live streaming (never shares Redis snmp:prev:* with 60s polls)
        self._live_prev_counters: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._live_device_rates: Dict[str, Tuple[float, float, float]] = {}
        self._live_ignore_indices: Dict[str, Set[int]] = {}
        self._live_if_speeds: Dict[Tuple[str, int], int] = {}
        self._live_dev_max_speed: Dict[str, float] = {}

    @staticmethod
    def _build_auth_args(profile: Dict[str, Any]) -> List[str]:
        version = str(profile.get("version") or "v2c").strip().lower()
        community = str(profile.get("community") or "public").strip()
        v3_user = str(profile.get("v3_user") or "").strip()
        v3_auth_key = str(profile.get("v3_auth_key") or "").strip()
        v3_priv_key = str(profile.get("v3_priv_key") or "").strip()
        v3_auth_proto = str(profile.get("v3_auth_proto") or "SHA").strip().upper()
        v3_priv_proto = str(profile.get("v3_priv_proto") or "AES").strip().upper()

        if version == "v1":
            return ["-v1", "-c", community]
        if version == "v3" and v3_user:
            args = ["-v3", "-u", v3_user]
            if v3_auth_key and v3_priv_key:
                args.extend(
                    [
                        "-l",
                        "authPriv",
                        "-a",
                        v3_auth_proto,
                        "-A",
                        v3_auth_key,
                        "-x",
                        v3_priv_proto,
                        "-X",
                        v3_priv_key,
                    ]
                )
            elif v3_auth_key:
                args.extend(["-l", "authNoPriv", "-a", v3_auth_proto, "-A", v3_auth_key])
            else:
                args.extend(["-l", "noAuthNoPriv"])
            return args
        return ["-v2c", "-c", community]

    async def _snmp_walk_oid(
        self,
        target: str,
        port: int,
        auth_args: List[str],
        base_oid: str,
        timeout: float = 6.0,
        snmp_timeout: int = 2,
        retries: int = 1,
        sem: Optional[asyncio.Semaphore] = None,
        stats: Optional[Dict[str, int]] = None,
    ) -> Dict[str, str]:
        clean_base = base_oid.strip().lstrip(".")
        walker = "snmpwalk" if auth_args and auth_args[0] == "-v1" else "snmpbulkwalk"
        cmd = [
            walker,
            "-On",
            "-Oq",
            "-Ot",
            "-Oe",
            "-O0",
            "-OU",
            "-t",
            str(snmp_timeout),
            "-r",
            str(retries),
            *auth_args,
            f"{target}:{port}",
            clean_base,
        ]

        async def _exec() -> Dict[str, str]:
            if stats is not None:
                stats["walks"] = stats.get("walks", 0) + 1
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                if proc.returncode != 0 and not stdout:
                    return {}
                result: Dict[str, str] = {}
                prefix = f".{clean_base}."
                for line in stdout.decode("utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or "No Such" in line or "End of MIB" in line:
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) != 2:
                        continue
                    oid_str, val_str = parts[0], parts[1].strip().strip('"')
                    if oid_str.startswith(prefix):
                        idx = oid_str[len(prefix) :]
                        result[idx] = val_str
                    elif oid_str == f".{clean_base}":
                        result["0"] = val_str
                if stats is not None and result:
                    stats["oids"] = stats.get("oids", 0) + len(result)
                return result
            except Exception as exc:
                logger.debug("SNMP walk failed on %s (%s): %s", target, clean_base, exc)
                return {}

        if sem is not None:
            async with sem:
                return await _exec()
        return await _exec()

    async def _collect_vendor_enrichment(
        self,
        target: str,
        port: int,
        auth_args: List[str],
        vendor_key: str,
        matched_mibs: List[str],
        sys_descr: str = "",
        sys_name: str = "",
        redis_client: Optional[aioredis.Redis] = None,
        sem: Optional[asyncio.Semaphore] = None,
        poll_stats: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Walks universal & vendor-matched MIB trees to extract hardware/OS/sensor/LLDP details."""
        (
            ent_phys,
            hr_storage,
            hr_sys,
            ucd_mem,
            ucd_load,
            ucd_cpu,
            ucd_disk,
            lm_sensors,
            lldp_rem,
            lldp_loc_chassis,
            bridge_mac,
        ) = await asyncio.gather(
            self._snmp_walk_oid(target, port, auth_args, OID_ENT_PHYSICAL, timeout=5.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_HR_STORAGE, timeout=5.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_HR_SYSTEM, timeout=4.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_UCD_MEMORY, timeout=4.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_UCD_LOAD, timeout=4.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_UCD_CPU, timeout=4.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_UCD_DISK, timeout=4.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_LM_SENSORS, timeout=4.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_LLDP_REM, timeout=5.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_LLDP_LOC_CHASSIS, timeout=3.0, sem=sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_BRIDGE_MAC, timeout=3.0, sem=sem, stats=poll_stats),
        )

        active_mibs = list(matched_mibs)
        responding_mibs: List[str] = []

        def _mark_mib(mib_name: str) -> None:
            if mib_name not in responding_mibs:
                responding_mibs.append(mib_name)
            if mib_name not in active_mibs:
                active_mibs.append(mib_name)

        hardware_model = ""
        serial_number = ""
        firmware_version = ""
        software_version = ""
        manufacturer = ""
        sensors: List[Dict[str, Any]] = []
        storage_volumes: List[Dict[str, Any]] = []
        lldp_neighbors: List[Dict[str, Any]] = []
        wireless_radios: List[Dict[str, Any]] = []
        vendor_scalars: Dict[str, Any] = {}
        ups_telemetry: Optional[Dict[str, Any]] = None
        printer_supplies: List[Dict[str, Any]] = []
        temp_celsius: Optional[float] = None
        cpu_pct: Optional[float] = None
        mem_pct: Optional[float] = None
        hr_uptime_ticks = _parse_timeticks(hr_sys.get("1.0"))
        ubnt_uptime_ticks = 0
        lldp_chassis_mac = _normalize_mac(lldp_loc_chassis.get("0", "")) or ""
        bridge_mac_norm = _normalize_mac(bridge_mac.get("0", "")) or ""

        if bridge_mac:
            _mark_mib("BRIDGE-MIB")

        # 1. Parse ENTITY-MIB (entPhysicalTable)
        if ent_phys:
            _mark_mib("ENTITY-MIB")
            for k, val in ent_phys.items():
                if not val or val in ("", "0"):
                    continue
                if k.startswith("13.") and not hardware_model:
                    hardware_model = val
                elif k.startswith("11.") and not serial_number:
                    serial_number = val
                elif k.startswith("9.") and not firmware_version:
                    firmware_version = val
                elif k.startswith("10.") and not software_version:
                    software_version = val
                elif k.startswith("12.") and not manufacturer:
                    manufacturer = val

        # 2. Parse UCD-SNMP-MIB (Load Averages, Live Raw CPU Delta, RAM, Disks)
        if ucd_load or ucd_cpu or ucd_mem or ucd_disk:
            _mark_mib("UCD-SNMP-MIB")

        cpu_details: Dict[str, Any] = {}
        if ucd_load:
            cpu_details["load_1m"] = _to_float(ucd_load.get("1"))
            cpu_details["load_5m"] = _to_float(ucd_load.get("2"))
            cpu_details["load_15m"] = _to_float(ucd_load.get("3"))

        if ucd_cpu:
            raw_keys = ("50.0", "51.0", "52.0", "53.0", "54.0", "56.0", "61.0")
            curr_raw = {
                rk: _to_int(ucd_cpu.get(rk), -1) for rk in raw_keys if rk in ucd_cpu
            }
            curr_raw = {rk: rv for rk, rv in curr_raw.items() if rv >= 0}
            if "53.0" in curr_raw and len(curr_raw) >= 2 and redis_client is not None:
                cpu_redis_key = f"snmp:prev_cpu:{target}"
                try:
                    prev_raw_str = await redis_client.get(cpu_redis_key)
                    if prev_raw_str:
                        prev_raw = json.loads(prev_raw_str)
                        deltas: Dict[str, int] = {}
                        valid_delta = True
                        for rk, cur_v in curr_raw.items():
                            prev_v = int(prev_raw.get(rk, -1))
                            if prev_v < 0 or cur_v < prev_v:
                                valid_delta = False
                                break
                            deltas[rk] = cur_v - prev_v
                        d_total = sum(deltas.values()) if valid_delta else 0
                        if d_total > 0:
                            d_idle = deltas.get("53.0", 0)
                            d_user = deltas.get("50.0", 0) + deltas.get("51.0", 0)
                            d_sys = deltas.get("52.0", 0) + deltas.get("56.0", 0) + deltas.get("61.0", 0)
                            cpu_details["user_pct"] = round((d_user / d_total) * 100.0, 1)
                            cpu_details["system_pct"] = round((d_sys / d_total) * 100.0, 1)
                            cpu_details["idle_pct"] = round((d_idle / d_total) * 100.0, 1)
                            cpu_pct = round(max(0.0, min(100.0, ((d_total - d_idle) / d_total) * 100.0)), 1)
                    await redis_client.set(cpu_redis_key, json.dumps(curr_raw), ex=600)
                except Exception:
                    pass

            if cpu_pct is None:
                user_p = _to_float(ucd_cpu.get("9.0"))
                sys_p = _to_float(ucd_cpu.get("10.0"))
                idle_p = _to_float(ucd_cpu.get("11.0"))
                if idle_p > 0 or user_p > 0 or sys_p > 0:
                    cpu_details["user_pct"] = user_p
                    cpu_details["system_pct"] = sys_p
                    cpu_details["idle_pct"] = idle_p
                    cpu_pct = round(max(0.0, 100.0 - idle_p), 1)

        memory_details: Dict[str, Any] = {}
        if ucd_mem:
            tot_kb = _to_int(ucd_mem.get("5.0"))
            avail_kb = _to_int(ucd_mem.get("6.0"))
            buf_kb = _to_int(ucd_mem.get("14.0"))
            cache_kb = _to_int(ucd_mem.get("15.0"))
            swap_tot_kb = _to_int(ucd_mem.get("3.0"))
            swap_avail_kb = _to_int(ucd_mem.get("4.0"))
            if tot_kb > 0:
                used_kb = max(0, tot_kb - avail_kb - buf_kb - cache_kb)
                mem_pct = round((used_kb / tot_kb) * 100.0, 1)
                memory_details = {
                    "total_bytes": tot_kb * 1024,
                    "used_bytes": used_kb * 1024,
                    "avail_bytes": avail_kb * 1024,
                    "cached_bytes": (buf_kb + cache_kb) * 1024,
                    "swap_total_bytes": swap_tot_kb * 1024,
                    "swap_used_bytes": max(0, swap_tot_kb - swap_avail_kb) * 1024,
                    "usage_pct": mem_pct,
                }

        # 3. Parse HOST-RESOURCES-MIB (hrStorageTable & hrSystem)
        if hr_sys or hr_storage:
            _mark_mib("HOST-RESOURCES-MIB")
        if hr_sys:
            procs = _to_int(hr_sys.get("6.0"))
            users = _to_int(hr_sys.get("5.0"))
            if procs > 0:
                vendor_scalars["Running Processes"] = procs
            if users > 0:
                vendor_scalars["Logged-in Users"] = users

        if hr_storage:
            storage_indices = {
                k.split(".", 1)[1] for k in hr_storage.keys() if k.startswith("3.") and "." in k
            }
            for s_idx in sorted(storage_indices, key=lambda x: int(x) if x.isdigit() else 999):
                descr = hr_storage.get(f"3.{s_idx}", "")
                unit_bytes = _to_int(hr_storage.get(f"4.{s_idx}"), 0)
                size_units = _to_int(hr_storage.get(f"5.{s_idx}"), 0)
                used_units = _to_int(hr_storage.get(f"6.{s_idx}"), 0)
                if not descr or unit_bytes <= 0 or size_units <= 0:
                    continue
                total_b = unit_bytes * size_units
                used_b = unit_bytes * used_units
                pct = round((used_b / total_b) * 100.0, 1) if total_b > 0 else 0.0
                if "physical memory" in descr.lower() or descr == "RAM":
                    if mem_pct is None:
                        mem_pct = pct
                        memory_details = {
                            "total_bytes": total_b,
                            "used_bytes": used_b,
                            "avail_bytes": max(0, total_b - used_b),
                            "usage_pct": pct,
                        }
                elif (
                    descr.startswith("/")
                    or ":\\" in descr
                    or "volume" in descr.lower()
                    or "ram" in descr.lower()
                ) and not any(
                    skip in descr for skip in ("/dev", "/run", "/sys", "/proc", "/snap")
                ):
                    storage_volumes.append(
                        {
                            "mount": descr,
                            "total_bytes": total_b,
                            "used_bytes": used_b,
                            "usage_pct": pct,
                        }
                    )

        # Fallback to UCD-SNMP-MIB::dskTable when hrStorageTable yields no filesystems
        if not storage_volumes and ucd_disk:
            d_indices = {
                k.split(".", 1)[1] for k in ucd_disk.keys() if k.startswith("2.") and "." in k
            }
            for d_idx in sorted(d_indices, key=lambda x: int(x) if x.isdigit() else 999):
                mount = (ucd_disk.get(f"2.{d_idx}") or "").strip()
                if not mount or any(
                    mount.startswith(skip) for skip in ("/dev", "/run", "/sys", "/proc", "/snap")
                ):
                    continue
                tot_kb = _to_int(ucd_disk.get(f"6.{d_idx}"), 0)
                used_kb = _to_int(ucd_disk.get(f"8.{d_idx}"), 0)
                if tot_kb <= 0:
                    continue
                total_b = tot_kb * 1024
                used_b = used_kb * 1024
                pct = _to_float(ucd_disk.get(f"9.{d_idx}"), -1.0)
                if pct < 0:
                    pct = round((used_b / total_b) * 100.0, 1) if total_b > 0 else 0.0
                storage_volumes.append(
                    {
                        "mount": mount,
                        "total_bytes": total_b,
                        "used_bytes": used_b,
                        "usage_pct": round(pct, 1),
                    }
                )

        # 4. Parse LM-SENSORS-MIB temperatures, fans, and voltages
        if lm_sensors:
            _mark_mib("LM-SENSORS-MIB")
            lm_temp_c, lm_list = _parse_lm_sensors(lm_sensors)
            if lm_temp_c is not None and temp_celsius is None:
                temp_celsius = lm_temp_c
            sensors.extend(lm_list)

        # 5. Parse LLDP-MIB Neighbors
        if lldp_rem or lldp_loc_chassis:
            _mark_mib("LLDP-MIB")
        if lldp_rem:
            rem_keys = {
                k.split(".", 1)[1]
                for k in lldp_rem.keys()
                if "." in k and k.split(".", 1)[0] in ("5", "7", "8", "9", "10")
            }
            for rk in sorted(rem_keys):
                chassis_id = lldp_rem.get(f"5.{rk}", "")
                sys_n = (
                    lldp_rem.get(f"9.{rk}", "")
                    or _normalize_mac(chassis_id)
                    or chassis_id
                )
                port_id = lldp_rem.get(f"7.{rk}", "")
                port_desc = lldp_rem.get(f"8.{rk}", "")
                sys_desc = lldp_rem.get(f"10.{rk}", "")
                parts = rk.split(".")
                local_port = parts[1] if len(parts) >= 2 else rk
                if sys_n or port_id:
                    lldp_neighbors.append(
                        {
                            "local_port": local_port,
                            "neighbor_name": sys_n or "LLDP Neighbor",
                            "neighbor_port": port_id or port_desc or "—",
                            "neighbor_descr": (port_desc or sys_desc or "")[:100],
                        }
                    )

        # 6. Vendor-Specific Enterprise MIB Walks
        is_ups_hint = vendor_key in ("apc", "cyberpower", "eaton", "ups") or "ups" in f"{sys_descr} {sys_name}".lower()

        if vendor_key == "ubiquiti":
            ubnt_sys, ubnt_vap, frog_mem = await asyncio.gather(
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.41112.1.6.3", timeout=3.0, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.41112.1.6.1.2.1", timeout=3.0, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.10002.1.1.1.1", timeout=3.0, sem=sem, stats=poll_stats),
            )
            if ubnt_sys or ubnt_vap:
                _mark_mib("UBNT-UniFi-MIB")
            if ubnt_sys:
                hardware_model = ubnt_sys.get("3.0") or hardware_model
                firmware_version = ubnt_sys.get("6.0") or firmware_version
                if ubnt_sys.get("5.0"):
                    ubnt_uptime_ticks = _to_int(ubnt_sys.get("5.0"), 0) * 100
            if frog_mem:
                _mark_mib("FROGFOOT-RESOURCES-MIB")
                if mem_pct is None:
                    tot_k = _to_int(frog_mem.get("1.0"))
                    free_k = _to_int(frog_mem.get("2.0"))
                    if tot_k > 0:
                        used_k = max(0, tot_k - free_k)
                        mem_pct = round((used_k / tot_k) * 100.0, 1)
                        memory_details = {
                            "total_bytes": tot_k * 1024,
                            "used_bytes": used_k * 1024,
                            "avail_bytes": free_k * 1024,
                            "usage_pct": mem_pct,
                        }
            if ubnt_vap:
                vap_ids = {
                    k.split(".", 1)[1] for k in ubnt_vap.keys() if k.startswith("6.") and "." in k
                }
                for vid in sorted(vap_ids):
                    essid = ubnt_vap.get(f"6.{vid}", "")
                    chan = ubnt_vap.get(f"4.{vid}", "")
                    radio = ubnt_vap.get(f"9.{vid}", "")
                    sta = _to_int(ubnt_vap.get(f"8.{vid}"), 0)
                    txp = ubnt_vap.get(f"21.{vid}", "")
                    if essid:
                        wireless_radios.append(
                            {
                                "ssid": essid,
                                "radio": radio or "WLAN",
                                "channel": chan,
                                "stations": sta,
                                "tx_power": f"{txp} dBm" if txp else "Auto",
                            }
                        )

        elif vendor_key == "mikrotik":
            mt_hl, mt_lic, mt_sys = await asyncio.gather(
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.14988.1.1.3", timeout=3.0, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.14988.1.1.4", timeout=3.0, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.14988.1.1.7", timeout=3.0, sem=sem, stats=poll_stats),
            )
            if mt_hl or mt_lic or mt_sys:
                _mark_mib("MIKROTIK-MIB")
            if mt_lic:
                firmware_version = mt_lic.get("4.0") or firmware_version
            if mt_sys:
                serial_number = mt_sys.get("3.0") or serial_number
            if mt_hl:
                volt = _to_float(mt_hl.get("8.0"))
                b_temp = _to_float(mt_hl.get("10.0"))
                c_temp = _to_float(mt_hl.get("11.0"))
                pwr = _to_float(mt_hl.get("12.0"))
                if volt > 0:
                    sensors.append(
                        {"name": "Input Voltage", "category": "Voltage", "value": f"{volt / 10.0:.1f} V", "status": "ok"}
                    )
                if b_temp > 0:
                    deg = b_temp / 10.0 if b_temp > 120 else b_temp
                    temp_celsius = round(deg, 1)
                    sensors.append(
                        {"name": "Board Temperature", "category": "Temperature", "value": f"{deg:.1f} °C", "status": "ok"}
                    )
                if c_temp > 0:
                    deg = c_temp / 10.0 if c_temp > 120 else c_temp
                    temp_celsius = round(deg, 1)
                    sensors.append(
                        {"name": "CPU Temperature", "category": "Temperature", "value": f"{deg:.1f} °C", "status": "ok"}
                    )
                if pwr > 0:
                    sensors.append(
                        {"name": "Power Draw", "category": "Power", "value": f"{pwr / 10.0:.1f} W", "status": "ok"}
                    )

        elif vendor_key == "synology":
            syno_sys, syno_disk, syno_raid = await asyncio.gather(
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.6574.1", timeout=3.0, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.6574.2.1.1", timeout=3.0, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.6574.3.1.1", timeout=3.0, sem=sem, stats=poll_stats),
            )
            if syno_sys:
                _mark_mib("SYNOLOGY-SYSTEM-MIB")
                t_val = _to_float(syno_sys.get("2.0"))
                if t_val > 0:
                    temp_celsius = t_val
                    sensors.append(
                        {"name": "NAS System Temp", "category": "Temperature", "value": f"{t_val:.0f} °C", "status": "ok"}
                    )
                hardware_model = syno_sys.get("5.1.0") or hardware_model
                serial_number = syno_sys.get("5.2.0") or serial_number
                firmware_version = syno_sys.get("5.3.0") or firmware_version
            if syno_disk:
                _mark_mib("SYNOLOGY-DISK-MIB")
                d_ids = {k.split(".", 1)[1] for k in syno_disk.keys() if k.startswith("2.") and "." in k}
                for did in sorted(d_ids):
                    d_name = syno_disk.get(f"2.{did}", f"Disk {did}")
                    d_model = syno_disk.get(f"3.{did}", "")
                    d_temp = _to_float(syno_disk.get(f"6.{did}"))
                    sensors.append(
                        {
                            "name": f"{d_name} ({d_model})".strip(),
                            "category": "Disk",
                            "value": f"{d_temp:.0f} °C" if d_temp > 0 else "Healthy",
                            "status": "ok",
                        }
                    )
            if syno_raid:
                _mark_mib("SYNOLOGY-RAID-MIB")
                r_ids = {k.split(".", 1)[1] for k in syno_raid.keys() if k.startswith("2.") and "." in k}
                for rid in sorted(r_ids):
                    r_name = syno_raid.get(f"2.{rid}", f"RAID {rid}")
                    r_stat = _to_int(syno_raid.get(f"3.{rid}"), 1)
                    sensors.append(
                        {
                            "name": r_name,
                            "category": "RAID",
                            "value": "Normal" if r_stat == 1 else f"State #{r_stat}",
                            "status": "ok" if r_stat == 1 else "warning",
                        }
                    )

        elif vendor_key == "cisco":
            c_cpu, c_env = await asyncio.gather(
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.9.9.109.1.1.1.1", timeout=3.0, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.9.9.13.1.3.1", timeout=3.0, sem=sem, stats=poll_stats),
            )
            if c_cpu:
                _mark_mib("CISCO-PROCESS-MIB")
                for k, v in c_cpu.items():
                    if k.startswith("7.") or k.startswith("8."):
                        cpu_pct = _to_float(v)
                        break
            if c_env:
                _mark_mib("CISCO-ENVMON-MIB")
                e_ids = {k.split(".", 1)[1] for k in c_env.keys() if k.startswith("2.") and "." in k}
                for eid in sorted(e_ids):
                    s_name = c_env.get(f"2.{eid}", f"Temp Sensor {eid}")
                    s_val = _to_float(c_env.get(f"3.{eid}"))
                    if s_val > 0:
                        if temp_celsius is None:
                            temp_celsius = s_val
                        sensors.append(
                            {"name": s_name, "category": "Temperature", "value": f"{s_val:.0f} °C", "status": "ok"}
                        )

        elif vendor_key == "fortinet":
            fg_sys = await self._snmp_walk_oid(
                target, port, auth_args, "1.3.6.1.4.1.12356.101.4.1", timeout=3.0, sem=sem, stats=poll_stats
            )
            if fg_sys:
                _mark_mib("FORTINET-FORTIGATE-MIB")
                firmware_version = fg_sys.get("1.0") or firmware_version
                cpu_pct = _to_float(fg_sys.get("3.0")) or cpu_pct
                mem_pct = _to_float(fg_sys.get("4.0")) or mem_pct
                ses = _to_int(fg_sys.get("8.0"))
                if ses > 0:
                    vendor_scalars["Active Firewall Sessions"] = ses

        elif is_ups_hint:
            apc_ident, apc_batt, apc_in, apc_out, apc_test, std_ups = await asyncio.gather(
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.318.1.1.1.1", timeout=6.0, snmp_timeout=3, retries=2, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.318.1.1.1.2", timeout=6.0, snmp_timeout=3, retries=2, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.318.1.1.1.3", timeout=6.0, snmp_timeout=3, retries=2, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.318.1.1.1.4", timeout=6.0, snmp_timeout=3, retries=2, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.318.1.1.1.7", timeout=6.0, snmp_timeout=3, retries=2, sem=sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.2.1.33.1", timeout=6.0, snmp_timeout=3, retries=2, sem=sem, stats=poll_stats),
            )
            if apc_ident or apc_batt or apc_in or apc_out or apc_test:
                _mark_mib("PowerNet-UPS-MIB")
            if std_ups:
                _mark_mib("UPS-MIB")

            if apc_ident or apc_batt or apc_in or apc_out or apc_test or std_ups:
                ups_telemetry = _parse_ups_telemetry(
                    apc_ident, apc_batt, apc_in, apc_out, apc_test, std_ups
                )
                hardware_model = ups_telemetry["hardware_model"] or hardware_model
                firmware_version = ups_telemetry["firmware_version"] or firmware_version
                serial_number = ups_telemetry["serial_number"] or serial_number
                if ups_telemetry["battery_temp_c"] is not None:
                    temp_celsius = ups_telemetry["battery_temp_c"]

                seen_sensors = {s.get("name") for s in sensors}
                def _add_ups_sensor(name: str, category: str, value: str, status: str = "ok") -> None:
                    if name not in seen_sensors:
                        seen_sensors.add(name)
                        sensors.append({"name": name, "category": category, "value": value, "status": status})

                if ups_telemetry["battery_charge_pct"] is not None:
                    b_val = ups_telemetry["battery_charge_pct"]
                    _add_ups_sensor("Battery Charge", "UPS", f"{b_val:g}%", "warning" if b_val < 50 else "ok")
                if ups_telemetry["battery_temp_c"] is not None:
                    bt = ups_telemetry["battery_temp_c"]
                    _add_ups_sensor("Battery Temp", "Temperature", f"{bt:g} °C", "warning" if bt >= 45 else "ok")
                if ups_telemetry["input_voltage_v"] is not None:
                    iv = ups_telemetry["input_voltage_v"]
                    _add_ups_sensor("Input Line Voltage", "Voltage", f"{iv:g} VAC", "warning" if iv < 95 else "ok")
                if ups_telemetry["output_voltage_v"] is not None:
                    ov = ups_telemetry["output_voltage_v"]
                    _add_ups_sensor("Output Voltage", "Voltage", f"{ov:g} VAC", "ok")
                if ups_telemetry["output_load_pct"] is not None:
                    ol = ups_telemetry["output_load_pct"]
                    _add_ups_sensor("UPS Output Load", "Load", f"{ol:g}%", "warning" if ol >= 80 else "ok")
                if ups_telemetry["output_watts"] is not None:
                    _add_ups_sensor("Output Power", "Power", f"{ups_telemetry['output_watts']:g} W", "ok")
                if ups_telemetry["battery_voltage_v"] is not None:
                    _add_ups_sensor("Battery Bus Voltage", "Voltage", f"{ups_telemetry['battery_voltage_v']:g} VDC", "ok")

                if ups_telemetry["output_status"]:
                    vendor_scalars["UPS Status"] = ups_telemetry["output_status"]
                if ups_telemetry["runtime_remaining_sec"] is not None:
                    rt_m = ups_telemetry["runtime_remaining_sec"] // 60
                    vendor_scalars["Estimated Runtime"] = f"{rt_m} mins"
                if ups_telemetry["time_on_battery_sec"] > 0:
                    vendor_scalars["Time on Battery"] = f"{ups_telemetry['time_on_battery_sec']}s"
                if ups_telemetry["last_transfer_reason"]:
                    vendor_scalars["Last Transfer Cause"] = ups_telemetry["last_transfer_reason"]
                if ups_telemetry["self_test_result"]:
                    vendor_scalars["Self-Test Result"] = ups_telemetry["self_test_result"]

        elif vendor_key == "printer":
            prt_mib = await self._snmp_walk_oid(
                target, port, auth_args, "1.3.6.1.2.1.43", timeout=4.0, sem=sem, stats=poll_stats
            )
            if prt_mib:
                _mark_mib("Printer-MIB")
                pages = _to_int(prt_mib.get("10.2.1.4.1.1"))
                if pages > 0:
                    vendor_scalars["Total Lifetime Pages Printed"] = pages
                serial_number = prt_mib.get("5.1.1.17.1") or serial_number
                printer_supplies = _parse_printer_supplies(prt_mib)

        # Apply sysDescr fallback extraction (UDM-Pro, switches, APs, APC NMC, Linux kernels, etc.)
        descr_meta = _parse_sys_descr_metadata(sys_descr, vendor_key)
        if not hardware_model and descr_meta["hardware_model"]:
            hardware_model = descr_meta["hardware_model"]
        if not firmware_version and not software_version and descr_meta["firmware_version"]:
            firmware_version = descr_meta["firmware_version"]
        if not serial_number and descr_meta["serial_number"]:
            serial_number = descr_meta["serial_number"]
        if descr_meta["os_kernel"]:
            vendor_scalars["OS / Kernel"] = descr_meta["os_kernel"]

        ext_payload: Dict[str, Any] = {
            "cpu_details": cpu_details,
            "memory_details": memory_details,
            "storage_volumes": storage_volumes[:24],
            "sensors": sensors[:32],
            "lldp_neighbors": lldp_neighbors[:32],
            "wireless_radios": wireless_radios[:16],
            "vendor_scalars": vendor_scalars,
        }
        if ups_telemetry is not None:
            ext_payload["ups_telemetry"] = ups_telemetry
        if printer_supplies:
            ext_payload["printer_supplies"] = printer_supplies

        return {
            "active_mibs": active_mibs,
            "responding_mibs": responding_mibs,
            "hardware_model": hardware_model,
            "serial_number": serial_number,
            "firmware_version": firmware_version or software_version,
            "manufacturer": manufacturer,
            "temp_celsius": temp_celsius,
            "cpu_pct": cpu_pct,
            "mem_pct": mem_pct,
            "hr_uptime_ticks": hr_uptime_ticks,
            "ubnt_uptime_ticks": ubnt_uptime_ticks,
            "lldp_chassis_mac": lldp_chassis_mac,
            "bridge_mac": bridge_mac_norm,
            "extended_metrics": ext_payload,
        }

    async def _poll_single_device(
        self,
        target_cfg: Dict[str, Any],
        pool: asyncpg.Pool,
        redis_client: aioredis.Redis,
    ) -> Optional[Dict[str, Any]]:
        target = str(target_cfg["device_ip"]).strip()
        label = str(target_cfg.get("label") or "").strip()
        port = int(target_cfg.get("port") or self.default_port)
        snmp_version = str(target_cfg.get("version") or self.default_version).strip().lower()
        auth_args = self._build_auth_args(target_cfg)
        poll_stats: Dict[str, int] = {"walks": 0, "oids": 0}

        start_ts = time.perf_counter()
        sys_scalars = await self._snmp_walk_oid(
            target, port, auth_args, "1.3.6.1.2.1.1", stats=poll_stats
        )
        if not sys_scalars:
            fail_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO snmp_devices (device_ip, sys_name, is_reachable, last_polled)
                    VALUES ($1, $2, FALSE, NOW())
                    ON CONFLICT (device_ip) DO UPDATE SET
                        sys_name = COALESCE(NULLIF($2, ''), snmp_devices.sys_name),
                        is_reachable = FALSE,
                        last_polled = NOW()
                    """,
                    target,
                    label or target,
                )
                try:
                    await conn.execute(
                        """
                        INSERT INTO snmp_poll_metrics (
                            time, device_ip, success, poll_duration_ms, oid_walks,
                            oids_collected, interfaces_ingested, responding_mib_count,
                            snmp_version, error_message
                        ) VALUES (NOW(), $1, FALSE, $2, $3, 0, 0, 0, $4, $5)
                        """,
                        target,
                        fail_ms,
                        poll_stats["walks"],
                        snmp_version,
                        "No SNMP response (timeout or auth mismatch)",
                    )
                except Exception:
                    pass
            return None

        sys_descr = sys_scalars.get("1.0", "")
        sys_obj_id = sys_scalars.get("2.0", "")
        snmpd_uptime_ticks = _parse_timeticks(sys_scalars.get("3.0"))
        sys_contact = sys_scalars.get("4.0", "")
        sys_name = label or sys_scalars.get("5.0", target) or target
        sys_location = sys_scalars.get("6.0", "")

        # Match device against Vendor MIB Catalog
        vendor_profile = match_device_vendor_profile(sys_obj_id, sys_descr, sys_name)
        vendor_name = vendor_profile["vendor_name"]
        device_category = vendor_profile["category"]
        base_mibs = vendor_profile["mibs"]

        # Bound per-device subprocess concurrency to avoid SNMP agent throttling/drops
        dev_sem = asyncio.Semaphore(6)

        # Walk interface tables, IP address table, HR processor load, and vendor enrichment MIBs
        (
            if_names,
            if_descrs,
            if_aliases,
            if_speeds,
            if_speed32,
            if_phys,
            if_oper,
            hc_in,
            hc_out,
            in_errs,
            out_errs,
            in_disc,
            out_disc,
            ip_addr_table,
            cpu_loads,
            enrichment,
        ) = await asyncio.gather(
            self._snmp_walk_oid(target, port, auth_args, OID_IF_NAME, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_DESCR, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_ALIAS, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_HIGH_SPEED, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_SPEED, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_PHYS_ADDR, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_OPER_STATUS, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_HC_IN_OCTETS, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_HC_OUT_OCTETS, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_IN_ERRORS, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_OUT_ERRORS, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_IN_DISCARDS, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_OUT_DISCARDS, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_IP_ADDR_TABLE, sem=dev_sem, stats=poll_stats),
            self._snmp_walk_oid(target, port, auth_args, OID_HR_PROC_LOAD, sem=dev_sem, stats=poll_stats),
            self._collect_vendor_enrichment(
                target,
                port,
                auth_args,
                vendor_profile["key"],
                base_mibs,
                sys_descr=sys_descr,
                sys_name=sys_name,
                redis_client=redis_client,
                sem=dev_sem,
                poll_stats=poll_stats,
            ),
        )

        # Fallback to 32-bit IF-MIB counters when 64-bit HC counters are unavailable
        used_counter_bits = 64
        if not hc_in and not hc_out:
            hc_in, hc_out = await asyncio.gather(
                self._snmp_walk_oid(target, port, auth_args, OID_IF_IN_OCTETS, sem=dev_sem, stats=poll_stats),
                self._snmp_walk_oid(target, port, auth_args, OID_IF_OUT_OCTETS, sem=dev_sem, stats=poll_stats),
            )
            if hc_in or hc_out:
                used_counter_bits = 32

        hr_uptime_ticks = int(enrichment.get("hr_uptime_ticks") or 0)
        ubnt_uptime_ticks = int(enrichment.get("ubnt_uptime_ticks") or 0)
        if hr_uptime_ticks > 0:
            sys_uptime = hr_uptime_ticks
        elif ubnt_uptime_ticks > 0:
            sys_uptime = ubnt_uptime_ticks
        else:
            sys_uptime = snmpd_uptime_ticks

        if hr_uptime_ticks > 0 and snmpd_uptime_ticks > 0 and abs(hr_uptime_ticks - snmpd_uptime_ticks) > 60000:
            sec = snmpd_uptime_ticks // 100
            d, h, m = sec // 86400, (sec % 86400) // 3600, (sec % 3600) // 60
            if d > 0:
                agent_up_str = f"{d}d {h}h"
            elif h > 0:
                agent_up_str = f"{h}h {m}m"
            else:
                agent_up_str = f"{m}m"
            enrichment["extended_metrics"]["vendor_scalars"]["SNMP Agent Uptime"] = agent_up_str

        mac_address, subnet_cidr, ip_addresses = _extract_ip_and_mac(
            target,
            ip_addr_table,
            if_phys,
            if_names or if_descrs,
            if_oper,
            enrichment.get("lldp_chassis_mac", ""),
            enrichment.get("bridge_mac", ""),
        )

        responding_mibs: List[str] = ["SNMPv2-MIB"]
        if if_names or if_descrs or hc_in or if_phys:
            responding_mibs.append("IF-MIB")
        if ip_addr_table:
            responding_mibs.append("IP-MIB")
        if cpu_loads and "HOST-RESOURCES-MIB" not in responding_mibs:
            responding_mibs.append("HOST-RESOURCES-MIB")
        for rmib in enrichment.get("responding_mibs", []):
            if rmib not in responding_mibs:
                responding_mibs.append(rmib)

        active_mibs = list(enrichment["active_mibs"])
        for rmib in responding_mibs:
            if rmib not in active_mibs:
                active_mibs.append(rmib)

        enrichment["extended_metrics"]["responding_mibs"] = responding_mibs
        enrichment["extended_metrics"]["ip_addresses"] = ip_addresses[:32]

        cpu_vals = [_to_float(v, -1.0) for v in cpu_loads.values()]
        cpu_vals = [v for v in cpu_vals if v >= 0.0]
        if cpu_vals:
            avg_cpu = round(sum(cpu_vals) / len(cpu_vals), 2)
            enrichment["extended_metrics"]["cpu_details"]["core_count"] = len(cpu_vals)
        elif enrichment["cpu_pct"] is not None:
            avg_cpu = float(enrichment["cpu_pct"])
        else:
            avg_cpu = 0.0

        mem_usage = float(enrichment["mem_pct"] or 0.0)
        temp_celsius = enrichment["temp_celsius"]

        now_epoch = time.time()
        all_indices = sorted(
            {int(k) for k in set(if_names.keys()) | set(if_descrs.keys()) if k.isdigit()}
        )

        if_speed_map: Dict[int, int] = {}
        for idx in all_indices:
            k = str(idx)
            high_speed_mbps = _to_int(if_speeds.get(k), 0)
            if_speed_map[idx] = (
                high_speed_mbps * 1_000_000 if high_speed_mbps > 0 else _to_int(if_speed32.get(k), 0)
            )
        max_device_speed_bps = max(if_speed_map.values(), default=0)
        fallback_cap_bps = min(
            max(max_device_speed_bps * 1.25, 10_000_000_000.0),
            100_000_000_000.0,
        )
        self._live_dev_max_speed[target] = fallback_cap_bps

        rows_to_insert: List[Tuple[Any, ...]] = []
        loopback_indices: Set[int] = set()
        for idx in all_indices:
            k = str(idx)
            name = if_names.get(k) or if_descrs.get(k) or f"if{idx}"
            if name.strip().lower() in ("lo", "loopback", "null0"):
                loopback_indices.add(idx)
            descr = if_descrs.get(k) or name
            alias = if_aliases.get(k, "")
            speed_bps = if_speed_map.get(idx, 0)
            self._live_if_speeds[(target, idx)] = speed_bps

            oper_raw = (if_oper.get(k) or "1").strip()
            oper_status = 1 if (_to_int(oper_raw, 2) == 1 or oper_raw.lower().startswith("up")) else 2

            has_in = k in hc_in
            has_out = k in hc_out
            in_oct = _to_int(hc_in.get(k), 0) if has_in else 0
            out_oct = _to_int(hc_out.get(k), 0) if has_out else 0
            in_err = _to_int(in_errs.get(k), 0)
            out_err = _to_int(out_errs.get(k), 0)
            in_dsc = _to_int(in_disc.get(k), 0)
            out_dsc = _to_int(out_disc.get(k), 0)

            redis_key = f"snmp:prev:{target}:{idx}"
            prev_raw = await redis_client.get(redis_key)
            prev_in: Optional[int] = None
            prev_out: Optional[int] = None
            prev_in_ts: Optional[float] = None
            prev_out_ts: Optional[float] = None
            prev_bits: Optional[int] = None
            prev_uptime: int = 0
            if prev_raw:
                try:
                    prev = json.loads(prev_raw)
                    if prev.get("in_oct") is not None:
                        prev_in = int(prev["in_oct"])
                    if prev.get("out_oct") is not None:
                        prev_out = int(prev["out_oct"])
                    raw_in_ts = prev.get("in_ts") if prev.get("in_ts") is not None else prev.get("ts")
                    raw_out_ts = prev.get("out_ts") if prev.get("out_ts") is not None else prev.get("ts")
                    if raw_in_ts is not None:
                        prev_in_ts = float(raw_in_ts)
                    if raw_out_ts is not None:
                        prev_out_ts = float(raw_out_ts)
                    if prev.get("counter_bits") is not None:
                        prev_bits = int(prev["counter_bits"])
                    prev_uptime = int(prev.get("sys_uptime") or 0)
                except Exception:
                    prev_in = None
                    prev_out = None
                    prev_in_ts = None
                    prev_out_ts = None
                    prev_bits = None
                    prev_uptime = 0

            width_changed = prev_bits is not None and prev_bits != used_counter_bits
            rebooted = sys_uptime > 0 and prev_uptime > 0 and sys_uptime < prev_uptime
            reset_baseline = width_changed or rebooted

            in_bps = 0.0
            out_bps = 0.0
            if not reset_baseline:
                if has_in and prev_in is not None and prev_in_ts is not None:
                    in_bps = _compute_safe_rate_bps(
                        in_oct - prev_in,
                        now_epoch - prev_in_ts,
                        speed_bps,
                        prev_in,
                        max_fallback_bps=fallback_cap_bps,
                        min_dt=1.0,
                    )
                if has_out and prev_out is not None and prev_out_ts is not None:
                    out_bps = _compute_safe_rate_bps(
                        out_oct - prev_out,
                        now_epoch - prev_out_ts,
                        speed_bps,
                        prev_out,
                        max_fallback_bps=fallback_cap_bps,
                        min_dt=1.0,
                    )

            next_state: Dict[str, Any] = {
                "counter_bits": used_counter_bits,
                "sys_uptime": sys_uptime,
            }
            if has_in:
                next_state["in_ts"] = now_epoch
                next_state["in_oct"] = in_oct
            elif not reset_baseline and prev_in is not None and prev_in_ts is not None:
                next_state["in_ts"] = prev_in_ts
                next_state["in_oct"] = prev_in

            if has_out:
                next_state["out_ts"] = now_epoch
                next_state["out_oct"] = out_oct
            elif not reset_baseline and prev_out is not None and prev_out_ts is not None:
                next_state["out_ts"] = prev_out_ts
                next_state["out_oct"] = prev_out

            if "in_oct" in next_state or "out_oct" in next_state:
                await redis_client.set(redis_key, json.dumps(next_state), ex=600)

            db_in_oct = in_oct if has_in else (prev_in if prev_in is not None else 0)
            db_out_oct = out_oct if has_out else (prev_out if prev_out is not None else 0)

            rows_to_insert.append(
                (
                    target,
                    sys_name,
                    idx,
                    name,
                    descr,
                    alias,
                    speed_bps,
                    oper_status,
                    db_in_oct,
                    db_out_oct,
                    in_bps,
                    out_bps,
                    in_err,
                    out_err,
                    in_dsc,
                    out_dsc,
                    avg_cpu,
                    mem_usage,
                    sys_uptime,
                )
            )

        self._live_ignore_indices[target] = loopback_indices
        poll_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO snmp_devices (
                    device_ip, sys_name, sys_descr, sys_object_id, sys_location, sys_contact,
                    sys_uptime_ticks, cpu_usage_pct, mem_usage_pct, temp_celsius,
                    interface_count, vendor_name, device_category, matched_mibs,
                    hardware_model, serial_number, firmware_version, mac_address, subnet_cidr,
                    extended_metrics, is_reachable, last_poll_duration_ms, first_polled, last_polled
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10,
                    $11, $12, $13, $14::jsonb,
                    $15, $16, $17, $18, $19,
                    $20::jsonb, TRUE, $21, NOW(), NOW()
                )
                ON CONFLICT (device_ip) DO UPDATE SET
                    sys_name = EXCLUDED.sys_name,
                    sys_descr = EXCLUDED.sys_descr,
                    sys_object_id = EXCLUDED.sys_object_id,
                    sys_location = EXCLUDED.sys_location,
                    sys_contact = EXCLUDED.sys_contact,
                    sys_uptime_ticks = EXCLUDED.sys_uptime_ticks,
                    cpu_usage_pct = EXCLUDED.cpu_usage_pct,
                    mem_usage_pct = EXCLUDED.mem_usage_pct,
                    temp_celsius = COALESCE(EXCLUDED.temp_celsius, snmp_devices.temp_celsius),
                    interface_count = EXCLUDED.interface_count,
                    vendor_name = EXCLUDED.vendor_name,
                    device_category = EXCLUDED.device_category,
                    matched_mibs = EXCLUDED.matched_mibs,
                    hardware_model = COALESCE(NULLIF(EXCLUDED.hardware_model, ''), snmp_devices.hardware_model),
                    serial_number = COALESCE(NULLIF(EXCLUDED.serial_number, ''), snmp_devices.serial_number),
                    firmware_version = COALESCE(NULLIF(EXCLUDED.firmware_version, ''), snmp_devices.firmware_version),
                    mac_address = COALESCE(NULLIF(EXCLUDED.mac_address, ''), snmp_devices.mac_address),
                    subnet_cidr = COALESCE(NULLIF(EXCLUDED.subnet_cidr, ''), snmp_devices.subnet_cidr),
                    extended_metrics = EXCLUDED.extended_metrics,
                    is_reachable = TRUE,
                    last_poll_duration_ms = EXCLUDED.last_poll_duration_ms,
                    first_polled = COALESCE(snmp_devices.first_polled, NOW()),
                    last_polled = NOW()
                """,
                target,
                sys_name,
                sys_descr,
                sys_obj_id,
                sys_location,
                sys_contact,
                sys_uptime,
                avg_cpu,
                mem_usage,
                temp_celsius,
                len(rows_to_insert),
                vendor_name,
                device_category,
                json.dumps(active_mibs),
                enrichment["hardware_model"],
                enrichment["serial_number"],
                enrichment["firmware_version"],
                mac_address,
                subnet_cidr,
                json.dumps(enrichment["extended_metrics"]),
                poll_ms,
            )

            if rows_to_insert:
                await conn.executemany(
                    """
                    INSERT INTO snmp_metrics_raw (
                        time, device_ip, hostname, if_index, if_name, if_descr, if_alias,
                        if_speed_bps, if_oper_status, in_octets, out_octets, in_bps, out_bps,
                        in_errors, out_errors, in_discards, out_discards,
                        cpu_usage, mem_usage, sys_uptime
                    ) VALUES (
                        NOW(), $1, $2, $3, $4, $5, $6,
                        $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, $16,
                        $17, $18, $19
                    )
                    """,
                    rows_to_insert,
                )

            try:
                await conn.execute(
                    """
                    INSERT INTO snmp_poll_metrics (
                        time, device_ip, success, poll_duration_ms, oid_walks,
                        oids_collected, interfaces_ingested, responding_mib_count,
                        snmp_version, error_message
                    ) VALUES (NOW(), $1, TRUE, $2, $3, $4, $5, $6, $7, '')
                    """,
                    target,
                    poll_ms,
                    poll_stats["walks"],
                    poll_stats["oids"],
                    len(rows_to_insert),
                    len(responding_mibs),
                    snmp_version,
                )
            except Exception as exc:
                logger.debug("snmp_poll_metrics insert skipped: %s", exc)

            ups_t = enrichment["extended_metrics"].get("ups_telemetry")
            if isinstance(ups_t, dict) and any(
                ups_t.get(k) is not None
                for k in (
                    "battery_charge_pct",
                    "output_load_pct",
                    "input_voltage_v",
                    "runtime_remaining_sec",
                    "output_watts",
                )
            ):
                try:
                    await conn.execute(
                        """
                        INSERT INTO ups_metrics_raw (
                            time, device_ip, battery_charge_pct, output_load_pct,
                            runtime_remaining_sec, input_voltage_v, output_voltage_v,
                            output_watts, battery_temp_c, output_status
                        ) VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        target,
                        ups_t.get("battery_charge_pct"),
                        ups_t.get("output_load_pct"),
                        ups_t.get("runtime_remaining_sec"),
                        ups_t.get("input_voltage_v"),
                        ups_t.get("output_voltage_v"),
                        ups_t.get("output_watts"),
                        ups_t.get("battery_temp_c"),
                        ups_t.get("output_status") or "",
                    )
                except Exception as exc:
                    logger.debug("ups_metrics_raw insert skipped: %s", exc)

        return {
            "device_ip": target,
            "sys_name": sys_name,
            "vendor_name": vendor_name,
            "device_category": device_category,
            "interfaces": len(rows_to_insert),
            "cpu_usage_pct": avg_cpu,
            "mem_usage_pct": mem_usage,
            "poll_ms": poll_ms,
        }

    async def poll_single_ip(
        self,
        device_ip: str,
        pool: asyncpg.Pool,
        redis_client: aioredis.Redis,
    ) -> Optional[Dict[str, Any]]:
        """Immediately polls one specific IP using its configured profile or matching profiles."""
        target_ip = device_ip.strip()
        if not target_ip:
            return None

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT t.device_ip, t.label,
                       COALESCE(p.version, 'v2c') AS version,
                       COALESCE(p.port, 161) AS port,
                       COALESCE(p.community, 'public') AS community,
                       COALESCE(p.v3_user, '') AS v3_user,
                       COALESCE(p.v3_auth_proto, 'SHA') AS v3_auth_proto,
                       COALESCE(p.v3_auth_key, '') AS v3_auth_key,
                       COALESCE(p.v3_priv_proto, 'AES') AS v3_priv_proto,
                       COALESCE(p.v3_priv_key, '') AS v3_priv_key
                FROM snmp_targets_config t
                LEFT JOIN snmp_profiles p ON p.id = t.profile_id
                WHERE t.device_ip = $1
                """,
                target_ip,
            )
            if row:
                return await self._poll_single_device(dict(row), pool, redis_client)

            profiles = [
                dict(p)
                for p in await conn.fetch(
                    """
                    SELECT id, name, version, port, community, v3_user,
                           v3_auth_proto, v3_auth_key, v3_priv_proto, v3_priv_key
                    FROM snmp_profiles
                    ORDER BY id ASC
                    """
                )
            ]

        for prof in profiles:
            port = int(prof.get("port") or 161)
            auth_args = self._build_auth_args(prof)
            scalars = await self._snmp_walk_oid(
                target_ip, port, auth_args, "1.3.6.1.2.1.1", timeout=2.5
            )
            if scalars:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO snmp_targets_config (device_ip, label, profile_id, enabled)
                        VALUES ($1, '', $2, TRUE)
                        ON CONFLICT (device_ip) DO UPDATE SET
                            profile_id = EXCLUDED.profile_id,
                            enabled = TRUE
                        """,
                        target_ip,
                        prof["id"],
                    )
                target_cfg = {**prof, "device_ip": target_ip, "label": ""}
                return await self._poll_single_device(target_cfg, pool, redis_client)
        return None

    async def auto_discover_and_rematch_devices(
        self,
        pool: asyncpg.Pool,
        redis_client: aioredis.Redis,
        force: bool = False,
    ) -> int:
        """
        Automatically updates previously discovered devices in `discovered_hosts` and `snmp_devices`:
        1. Backfills vendor MIB profile matching on any existing `snmp_devices` rows.
        2. Probes online `discovered_hosts` against all configured `snmp_profiles` and automatically
           enrolls responsive devices with their matched SNMP profile & Vendor MIBs.
        """
        matched_count = 0
        async with pool.acquire() as conn:
            # 1. Backfill vendor_name / matched_mibs on any previously polled snmp_devices
            existing_snmp = await conn.fetch(
                """
                SELECT device_ip, sys_name, sys_descr, sys_object_id, vendor_name, matched_mibs
                FROM snmp_devices
                """
            )
            for row in existing_snmp:
                vp = match_device_vendor_profile(
                    row["sys_object_id"] or "",
                    row["sys_descr"] or "",
                    row["sys_name"] or "",
                )
                await conn.execute(
                    """
                    UPDATE snmp_devices
                    SET vendor_name = $1,
                        device_category = $2,
                        matched_mibs = CASE
                            WHEN matched_mibs IS NULL OR matched_mibs = '[]'::jsonb
                            THEN $3::jsonb
                            ELSE matched_mibs
                        END
                    WHERE device_ip = $4
                    """,
                    vp["vendor_name"],
                    vp["category"],
                    json.dumps(vp["mibs"]),
                    row["device_ip"],
                )

            # 2. Fetch all configured SNMP profiles and previously discovered hosts
            profiles = [
                dict(p)
                for p in await conn.fetch(
                    """
                    SELECT id, name, version, port, community, v3_user,
                           v3_auth_proto, v3_auth_key, v3_priv_proto, v3_priv_key
                    FROM snmp_profiles
                    ORDER BY id ASC
                    """
                )
            ]

            discovered = [
                dict(d)
                for d in await conn.fetch(
                    """
                    SELECT host(d.ip_address) AS ip_address, d.hostname, d.vendor,
                           t.id AS target_id, t.profile_id, s.is_reachable
                    FROM discovered_hosts d
                    LEFT JOIN snmp_targets_config t ON t.device_ip = host(d.ip_address)
                    LEFT JOIN snmp_devices s ON s.device_ip = host(d.ip_address)
                    WHERE d.is_online = TRUE
                    ORDER BY d.ip_address ASC
                    LIMIT 60
                    """
                )
            ]

        if not profiles or not discovered:
            return matched_count

        async def _probe_host(host_row: Dict[str, Any]) -> None:
            nonlocal matched_count
            ip_str = str(host_row["ip_address"])
            # Skip if already reachable via SNMP and not forced
            if host_row.get("target_id") and host_row.get("is_reachable") and not force:
                return

            cooldown_key = f"snmp:autoprobe:{ip_str}"
            if not force and await redis_client.get(cooldown_key):
                return

            for prof in profiles:
                port = int(prof.get("port") or 161)
                auth_args = self._build_auth_args(prof)
                scalars = await self._snmp_walk_oid(
                    ip_str, port, auth_args, "1.3.6.1.2.1.1", timeout=2.5
                )
                if scalars:
                    label = host_row.get("hostname") or ""
                    if label == ip_str:
                        label = ""
                    async with pool.acquire() as conn:
                        await conn.execute(
                            """
                            INSERT INTO snmp_targets_config (device_ip, label, profile_id, enabled)
                            VALUES ($1, $2, $3, TRUE)
                            ON CONFLICT (device_ip) DO UPDATE SET
                                profile_id = EXCLUDED.profile_id,
                                enabled = TRUE
                            """,
                            ip_str,
                            label,
                            prof["id"],
                        )
                    matched_count += 1
                    await redis_client.delete(cooldown_key)
                    return

            # Mark probed for 15 minutes so unreachable/non-SNMP hosts aren't re-probed every minute
            await redis_client.set(cooldown_key, "1", ex=900)

        # Probe in small concurrent batches of 10
        for i in range(0, len(discovered), 10):
            batch = discovered[i : i + 10]
            await asyncio.gather(*[_probe_host(h) for h in batch], return_exceptions=True)

        return matched_count

    async def poll_all(
        self, pool: asyncpg.Pool, redis_client: aioredis.Redis, force_rematch: bool = False
    ) -> List[Dict[str, Any]]:
        try:
            await self.auto_discover_and_rematch_devices(pool, redis_client, force=force_rematch)
        except Exception as exc:
            logger.debug("Auto-discover MIB rematch error: %s", exc)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.device_ip, t.label,
                       COALESCE(p.version, 'v2c') AS version,
                       COALESCE(p.port, 161) AS port,
                       COALESCE(p.community, 'public') AS community,
                       COALESCE(p.v3_user, '') AS v3_user,
                       COALESCE(p.v3_auth_proto, 'SHA') AS v3_auth_proto,
                       COALESCE(p.v3_auth_key, '') AS v3_auth_key,
                       COALESCE(p.v3_priv_proto, 'AES') AS v3_priv_proto,
                       COALESCE(p.v3_priv_key, '') AS v3_priv_key
                FROM snmp_targets_config t
                LEFT JOIN snmp_profiles p ON p.id = t.profile_id
                WHERE t.enabled = TRUE
                ORDER BY t.id ASC
                LIMIT 50
                """
            )

        targets = [dict(r) for r in rows]
        if not targets:
            return []

        tasks = [self._poll_single_device(t, pool, redis_client) for t in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid: List[Dict[str, Any]] = []
        for res in results:
            if isinstance(res, dict):
                valid.append(res)
            elif isinstance(res, Exception):
                logger.warning("SNMP poll exception: %s", res)
        return valid

    async def _snmp_walk_oid_fast(
        self, target: str, port: int, auth_args: List[str], base_oid: str, timeout: float = 1.8
    ) -> Dict[str, str]:
        """Short-timeout SNMP bulkwalk for sub-2s live traffic sampling."""
        return await self._snmp_walk_oid(
            target,
            port,
            auth_args,
            base_oid,
            timeout=timeout,
            snmp_timeout=1,
            retries=0,
        )

    async def poll_live_traffic(
        self,
        targets: List[Dict[str, Any]],
        db_ignore_indices: Optional[Dict[str, Set[int]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Walks only 64-bit interface octets concurrently to compute real-time aggregate bps."""
        if not targets:
            self._live_prev_counters.clear()
            self._live_device_rates.clear()
            return None

        sem = asyncio.Semaphore(16)
        active_ips = {str(t["device_ip"]).strip() for t in targets if t.get("device_ip")}

        async def _sample_target(target_cfg: Dict[str, Any]) -> None:
            ip = str(target_cfg.get("device_ip") or "").strip()
            if not ip:
                return
            port = int(target_cfg.get("port") or self.default_port)
            auth_args = self._build_auth_args(target_cfg)

            used_bits = 64
            async with sem:
                hc_in, hc_out = await asyncio.gather(
                    self._snmp_walk_oid_fast(ip, port, auth_args, OID_IF_HC_IN_OCTETS),
                    self._snmp_walk_oid_fast(ip, port, auth_args, OID_IF_HC_OUT_OCTETS),
                )
                if not hc_in and not hc_out:
                    hc_in, hc_out = await asyncio.gather(
                        self._snmp_walk_oid_fast(ip, port, auth_args, OID_IF_IN_OCTETS),
                        self._snmp_walk_oid_fast(ip, port, auth_args, OID_IF_OUT_OCTETS),
                    )
                    if hc_in or hc_out:
                        used_bits = 32

            if not hc_in and not hc_out:
                return

            now_epoch = time.time()
            ignore = self._live_ignore_indices.get(ip, set())
            if db_ignore_indices:
                ignore = ignore | db_ignore_indices.get(ip, set())

            all_indices = sorted(
                {int(k) for k in (set(hc_in.keys()) | set(hc_out.keys())) if k.isdigit()}
            )
            fallback_cap = self._live_dev_max_speed.get(ip, 40_000_000_000.0)
            dev_rx = 0.0
            dev_tx = 0.0
            has_delta = False

            for idx in all_indices:
                if idx in ignore:
                    continue
                k = str(idx)
                has_in = k in hc_in
                has_out = k in hc_out
                if not has_in and not has_out:
                    continue
                in_oct = _to_int(hc_in.get(k), 0) if has_in else 0
                out_oct = _to_int(hc_out.get(k), 0) if has_out else 0
                speed_bps = self._live_if_speeds.get((ip, idx), 0)

                prev = self._live_prev_counters.get((ip, idx))
                width_changed = prev is not None and prev.get("bits") != used_bits
                if prev is not None and not width_changed:
                    if has_in and prev.get("in_oct") is not None and prev.get("in_ts") is not None:
                        prev_in_val = int(prev["in_oct"])
                        dt_in = now_epoch - float(prev["in_ts"])
                        if dt_in >= 0.4:
                            rate_in = _compute_safe_rate_bps(
                                in_oct - prev_in_val,
                                dt_in,
                                speed_bps,
                                prev_in_val,
                                max_fallback_bps=fallback_cap,
                                min_dt=0.4,
                            )
                            dev_rx += rate_in
                            has_delta = True
                    if has_out and prev.get("out_oct") is not None and prev.get("out_ts") is not None:
                        prev_out_val = int(prev["out_oct"])
                        dt_out = now_epoch - float(prev["out_ts"])
                        if dt_out >= 0.4:
                            rate_out = _compute_safe_rate_bps(
                                out_oct - prev_out_val,
                                dt_out,
                                speed_bps,
                                prev_out_val,
                                max_fallback_bps=fallback_cap,
                                min_dt=0.4,
                            )
                            dev_tx += rate_out
                            has_delta = True

                next_live: Dict[str, Any] = (
                    {} if (prev is None or width_changed) else dict(prev)
                )
                next_live["bits"] = used_bits
                if has_in:
                    next_live["in_ts"] = now_epoch
                    next_live["in_oct"] = in_oct
                if has_out:
                    next_live["out_ts"] = now_epoch
                    next_live["out_oct"] = out_oct
                self._live_prev_counters[(ip, idx)] = next_live

            if has_delta:
                self._live_device_rates[ip] = (now_epoch, dev_rx, dev_tx)

        await asyncio.gather(*[_sample_target(t) for t in targets], return_exceptions=True)

        # Prune removed targets from in-memory caches
        stale_keys = [k for k in self._live_prev_counters if k[0] not in active_ips]
        for k in stale_keys:
            del self._live_prev_counters[k]
        stale_ips = [ip for ip in self._live_device_rates if ip not in active_ips]
        for ip in stale_ips:
            del self._live_device_rates[ip]
        stale_spd_keys = [k for k in self._live_if_speeds if k[0] not in active_ips]
        for k in stale_spd_keys:
            del self._live_if_speeds[k]
        stale_dev_spd = [ip for ip in self._live_dev_max_speed if ip not in active_ips]
        for ip in stale_dev_spd:
            del self._live_dev_max_speed[ip]

        now_agg = time.time()
        total_rx = 0.0
        total_tx = 0.0
        active_count = 0
        for ip, (ts, rx_val, tx_val) in list(self._live_device_rates.items()):
            if now_agg - ts <= 15.0:
                total_rx += rx_val
                total_tx += tx_val
                active_count += 1

        if active_count == 0:
            return None

        return {
            "time": datetime.now(timezone.utc).isoformat(),
            "rx_bps": round(total_rx, 1),
            "tx_bps": round(total_tx, 1),
            "latency_ms": 0.0,
            "source": "snmp",
            "device_count": active_count,
        }
