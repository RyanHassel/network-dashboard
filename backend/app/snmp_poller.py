import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import redis.asyncio as aioredis

from app.mib_catalog import match_device_vendor_profile

logger = logging.getLogger("netdash.snmp")

# Standard MIB-II / IF-MIB OIDs
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_IN_DISCARDS = "1.3.6.1.2.1.2.2.1.13"
OID_IF_IN_ERRORS = "1.3.6.1.2.1.2.2.1.14"
OID_IF_OUT_DISCARDS = "1.3.6.1.2.1.2.2.1.19"
OID_IF_OUT_ERRORS = "1.3.6.1.2.1.2.2.1.20"

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
OID_LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"

# UCD-SNMP-MIB (Linux, Proxmox, Ubiquiti, Synology, TrueNAS, QNAP)
OID_UCD_MEMORY = "1.3.6.1.4.1.2021.4"
OID_UCD_LOAD = "1.3.6.1.4.1.2021.10.1.3"
OID_UCD_CPU = "1.3.6.1.4.1.2021.11"
OID_LM_TEMP = "1.3.6.1.4.1.2021.13.16.2.1"


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


class SNMPPoller:
    """Asynchronous SNMPv2c/v3 GETBULK collector with Vendor MIB fingerprinting & enrichment."""

    def __init__(self) -> None:
        self.default_version = os.getenv("SNMP_VERSION", "v2c").strip().lower()
        self.default_community = os.getenv("SNMP_COMMUNITY", "public").strip() or "public"
        self.default_port = int(os.getenv("SNMP_PORT", "161") or 161)

    @staticmethod
    def _build_auth_args(profile: Dict[str, Any]) -> List[str]:
        version = str(profile.get("version") or "v2c").strip().lower()
        community = str(profile.get("community") or "public").strip()
        v3_user = str(profile.get("v3_user") or "").strip()
        v3_auth_key = str(profile.get("v3_auth_key") or "").strip()
        v3_priv_key = str(profile.get("v3_priv_key") or "").strip()
        v3_auth_proto = str(profile.get("v3_auth_proto") or "SHA").strip().upper()
        v3_priv_proto = str(profile.get("v3_priv_proto") or "AES").strip().upper()

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
        self, target: str, port: int, auth_args: List[str], base_oid: str, timeout: float = 5.0
    ) -> Dict[str, str]:
        cmd = [
            "snmpbulkwalk",
            "-On",
            "-Oq",
            "-t",
            "2",
            "-r",
            "1",
            *auth_args,
            f"{target}:{port}",
            base_oid,
        ]
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
            prefix = f".{base_oid}."
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
                elif oid_str == f".{base_oid}":
                    result["0"] = val_str
            return result
        except Exception as exc:
            logger.debug("SNMP walk failed on %s (%s): %s", target, base_oid, exc)
            return {}

    async def _collect_vendor_enrichment(
        self,
        target: str,
        port: int,
        auth_args: List[str],
        vendor_key: str,
        matched_mibs: List[str],
    ) -> Dict[str, Any]:
        """Walks universal & vendor-matched MIB trees to extract maximum hardware/OS/sensor details."""
        (
            ent_phys,
            hr_storage,
            hr_sys,
            ucd_mem,
            ucd_load,
            ucd_cpu,
            lm_temp,
            lldp_rem,
        ) = await asyncio.gather(
            self._snmp_walk_oid(target, port, auth_args, OID_ENT_PHYSICAL, timeout=4.0),
            self._snmp_walk_oid(target, port, auth_args, OID_HR_STORAGE, timeout=4.0),
            self._snmp_walk_oid(target, port, auth_args, OID_HR_SYSTEM, timeout=3.0),
            self._snmp_walk_oid(target, port, auth_args, OID_UCD_MEMORY, timeout=3.0),
            self._snmp_walk_oid(target, port, auth_args, OID_UCD_LOAD, timeout=3.0),
            self._snmp_walk_oid(target, port, auth_args, OID_UCD_CPU, timeout=3.0),
            self._snmp_walk_oid(target, port, auth_args, OID_LM_TEMP, timeout=3.0),
            self._snmp_walk_oid(target, port, auth_args, OID_LLDP_REM, timeout=4.0),
        )

        active_mibs = list(matched_mibs)
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
        temp_celsius: Optional[float] = None
        cpu_pct: Optional[float] = None
        mem_pct: Optional[float] = None

        # 1. Parse ENTITY-MIB (entPhysicalTable)
        if ent_phys:
            if "ENTITY-MIB" not in active_mibs:
                active_mibs.append("ENTITY-MIB")
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

        # 2. Parse UCD-SNMP-MIB (CPU, Load Averages, RAM)
        cpu_details: Dict[str, Any] = {}
        if ucd_load:
            if "UCD-SNMP-MIB" not in active_mibs:
                active_mibs.append("UCD-SNMP-MIB")
            cpu_details["load_1m"] = _to_float(ucd_load.get("1"))
            cpu_details["load_5m"] = _to_float(ucd_load.get("2"))
            cpu_details["load_15m"] = _to_float(ucd_load.get("3"))

        if ucd_cpu:
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
        if hr_sys:
            procs = _to_int(hr_sys.get("6.0"))
            users = _to_int(hr_sys.get("5.0"))
            if procs > 0:
                vendor_scalars["Running Processes"] = procs
            if users > 0:
                vendor_scalars["Logged-in Users"] = users

        if hr_storage:
            if "HOST-RESOURCES-MIB" not in active_mibs:
                active_mibs.append("HOST-RESOURCES-MIB")
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

        # 4. Parse LM-SENSORS-MIB temperatures
        if lm_temp:
            if "LM-SENSORS-MIB" not in active_mibs:
                active_mibs.append("LM-SENSORS-MIB")
            t_indices = {
                k.split(".", 1)[1] for k in lm_temp.keys() if k.startswith("2.") and "." in k
            }
            for t_idx in sorted(t_indices):
                t_name = lm_temp.get(f"2.{t_idx}", f"Sensor #{t_idx}")
                raw_mdeg = _to_float(lm_temp.get(f"3.{t_idx}"), 0.0)
                deg_c = round(raw_mdeg / 1000.0, 1) if raw_mdeg > 200 else round(raw_mdeg, 1)
                if 0 < deg_c < 150:
                    if temp_celsius is None:
                        temp_celsius = deg_c
                    sensors.append(
                        {
                            "name": t_name,
                            "category": "Temperature",
                            "value": f"{deg_c} °C",
                            "status": "warning" if deg_c >= 80 else "ok",
                        }
                    )

        # 5. Parse LLDP-MIB Neighbors
        if lldp_rem:
            if "LLDP-MIB" not in active_mibs:
                active_mibs.append("LLDP-MIB")
            rem_keys = {
                ".".join(k.split(".")[1:])
                for k in lldp_rem.keys()
                if k.startswith("9.") and "." in k
            }
            for rk in sorted(rem_keys):
                sys_n = lldp_rem.get(f"9.{rk}", "")
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
        if vendor_key == "ubiquiti":
            ubnt_sys, ubnt_vap, frog_mem = await asyncio.gather(
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.41112.1.6.3", timeout=3.0),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.41112.1.6.1.2.1", timeout=3.0),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.10002.1.1.1.1", timeout=3.0),
            )
            if ubnt_sys:
                hardware_model = ubnt_sys.get("3.0") or hardware_model
                firmware_version = ubnt_sys.get("6.0") or firmware_version
            if frog_mem and mem_pct is None:
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
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.14988.1.1.3", timeout=3.0),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.14988.1.1.4", timeout=3.0),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.14988.1.1.7", timeout=3.0),
            )
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
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.6574.1", timeout=3.0),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.6574.2.1.1", timeout=3.0),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.6574.3.1.1", timeout=3.0),
            )
            if syno_sys:
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
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.9.9.109.1.1.1.1", timeout=3.0),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.9.9.13.1.3.1", timeout=3.0),
            )
            if c_cpu:
                for k, v in c_cpu.items():
                    if k.startswith("7.") or k.startswith("8."):
                        cpu_pct = _to_float(v)
                        break
            if c_env:
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
            fg_sys = await self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.12356.101.4.1", timeout=3.0)
            if fg_sys:
                firmware_version = fg_sys.get("1.0") or firmware_version
                cpu_pct = _to_float(fg_sys.get("3.0")) or cpu_pct
                mem_pct = _to_float(fg_sys.get("4.0")) or mem_pct
                ses = _to_int(fg_sys.get("8.0"))
                if ses > 0:
                    vendor_scalars["Active Firewall Sessions"] = ses

        elif vendor_key in ("apc", "cyberpower", "eaton"):
            apc_ups, std_ups = await asyncio.gather(
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.4.1.318.1.1.1", timeout=3.0),
                self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.2.1.33.1", timeout=3.0),
            )
            if apc_ups:
                hardware_model = apc_ups.get("1.1.1.0") or hardware_model
                b_cap = _to_float(apc_ups.get("2.2.1.0"))
                b_temp = _to_float(apc_ups.get("2.2.2.0"))
                in_v = _to_float(apc_ups.get("3.2.1.0"))
                out_load = _to_float(apc_ups.get("4.2.3.0"))
                if b_cap > 0:
                    sensors.append({"name": "Battery Charge", "category": "UPS", "value": f"{b_cap:.0f}%", "status": "ok"})
                if b_temp > 0:
                    temp_celsius = b_temp
                    sensors.append({"name": "Battery Temp", "category": "Temperature", "value": f"{b_temp:.0f} °C", "status": "ok"})
                if in_v > 0:
                    sensors.append({"name": "Input Line Voltage", "category": "Voltage", "value": f"{in_v:.0f} VAC", "status": "ok"})
                if out_load >= 0:
                    sensors.append({"name": "UPS Output Load", "category": "Load", "value": f"{out_load:.0f}%", "status": "ok"})
            if std_ups:
                b_chg = _to_float(std_ups.get("2.4.0"))
                b_min = _to_int(std_ups.get("2.3.0"))
                if b_chg > 0:
                    sensors.append({"name": "Battery Charge", "category": "UPS", "value": f"{b_chg:.0f}%", "status": "ok"})
                if b_min > 0:
                    vendor_scalars["Estimated Runtime"] = f"{b_min} mins"

        elif vendor_key == "printer":
            prt_mib = await self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.2.1.43", timeout=3.0)
            if prt_mib:
                pages = _to_int(prt_mib.get("10.2.1.4.1.1"))
                if pages > 0:
                    vendor_scalars["Total Lifetime Pages Printed"] = pages
                serial_number = prt_mib.get("5.1.1.17.1") or serial_number

        return {
            "active_mibs": active_mibs,
            "hardware_model": hardware_model,
            "serial_number": serial_number,
            "firmware_version": firmware_version or software_version,
            "manufacturer": manufacturer,
            "temp_celsius": temp_celsius,
            "cpu_pct": cpu_pct,
            "mem_pct": mem_pct,
            "extended_metrics": {
                "cpu_details": cpu_details,
                "memory_details": memory_details,
                "storage_volumes": storage_volumes[:24],
                "sensors": sensors[:32],
                "lldp_neighbors": lldp_neighbors[:32],
                "wireless_radios": wireless_radios[:16],
                "vendor_scalars": vendor_scalars,
            },
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
        auth_args = self._build_auth_args(target_cfg)

        start_ts = time.perf_counter()
        sys_scalars = await self._snmp_walk_oid(target, port, auth_args, "1.3.6.1.2.1.1")
        if not sys_scalars:
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
            return None

        sys_descr = sys_scalars.get("1.0", "")
        sys_obj_id = sys_scalars.get("2.0", "")
        raw_uptime = sys_scalars.get("3.0", "0").split(":")[0].strip()
        sys_uptime = int(raw_uptime) if raw_uptime.isdigit() else 0
        sys_contact = sys_scalars.get("4.0", "")
        sys_name = label or sys_scalars.get("5.0", target) or target
        sys_location = sys_scalars.get("6.0", "")

        # Match device against Vendor MIB Catalog
        vendor_profile = match_device_vendor_profile(sys_obj_id, sys_descr, sys_name)
        vendor_name = vendor_profile["vendor_name"]
        device_category = vendor_profile["category"]
        base_mibs = vendor_profile["mibs"]

        # Walk interface tables, HR processor load, and vendor enrichment MIBs concurrently
        (
            if_names,
            if_descrs,
            if_aliases,
            if_speeds,
            if_oper,
            hc_in,
            hc_out,
            in_errs,
            out_errs,
            in_disc,
            out_disc,
            cpu_loads,
            enrichment,
        ) = await asyncio.gather(
            self._snmp_walk_oid(target, port, auth_args, OID_IF_NAME),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_DESCR),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_ALIAS),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_HIGH_SPEED),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_OPER_STATUS),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_HC_IN_OCTETS),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_HC_OUT_OCTETS),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_IN_ERRORS),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_OUT_ERRORS),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_IN_DISCARDS),
            self._snmp_walk_oid(target, port, auth_args, OID_IF_OUT_DISCARDS),
            self._snmp_walk_oid(target, port, auth_args, OID_HR_PROC_LOAD),
            self._collect_vendor_enrichment(
                target, port, auth_args, vendor_profile["key"], base_mibs
            ),
        )

        cpu_vals = [float(v) for v in cpu_loads.values() if v.replace(".", "", 1).isdigit()]
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

        rows_to_insert: List[Tuple[Any, ...]] = []
        for idx in all_indices:
            k = str(idx)
            name = if_names.get(k) or if_descrs.get(k) or f"if{idx}"
            descr = if_descrs.get(k) or name
            alias = if_aliases.get(k, "")
            high_speed_mbps = int(if_speeds.get(k, "0") or 0) if if_speeds.get(k, "0").isdigit() else 0
            speed_bps = high_speed_mbps * 1_000_000

            oper_raw = if_oper.get(k, "1")
            oper_status = 1 if "1" in oper_raw or "up" in oper_raw.lower() else 2

            in_oct = int(hc_in.get(k, "0") or 0) if hc_in.get(k, "0").isdigit() else 0
            out_oct = int(hc_out.get(k, "0") or 0) if hc_out.get(k, "0").isdigit() else 0
            in_err = int(in_errs.get(k, "0") or 0) if in_errs.get(k, "0").isdigit() else 0
            out_err = int(out_errs.get(k, "0") or 0) if out_errs.get(k, "0").isdigit() else 0
            in_dsc = int(in_disc.get(k, "0") or 0) if in_disc.get(k, "0").isdigit() else 0
            out_dsc = int(out_disc.get(k, "0") or 0) if out_disc.get(k, "0").isdigit() else 0

            redis_key = f"snmp:prev:{target}:{idx}"
            prev_raw = await redis_client.get(redis_key)
            in_bps = 0.0
            out_bps = 0.0
            if prev_raw:
                try:
                    prev = json.loads(prev_raw)
                    dt = now_epoch - float(prev.get("ts", now_epoch))
                    if dt > 1.0:
                        d_in = in_oct - int(prev.get("in_oct", in_oct))
                        d_out = out_oct - int(prev.get("out_oct", out_oct))
                        if d_in >= 0:
                            in_bps = round((d_in * 8.0) / dt, 2)
                        if d_out >= 0:
                            out_bps = round((d_out * 8.0) / dt, 2)
                except Exception:
                    pass

            await redis_client.set(
                redis_key,
                json.dumps({"ts": now_epoch, "in_oct": in_oct, "out_oct": out_oct}),
                ex=600,
            )

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
                    in_oct,
                    out_oct,
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

        poll_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO snmp_devices (
                    device_ip, sys_name, sys_descr, sys_object_id, sys_location, sys_contact,
                    sys_uptime_ticks, cpu_usage_pct, mem_usage_pct, temp_celsius,
                    interface_count, vendor_name, device_category, matched_mibs,
                    hardware_model, serial_number, firmware_version, extended_metrics,
                    is_reachable, last_poll_duration_ms, last_polled
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10,
                    $11, $12, $13, $14::jsonb,
                    $15, $16, $17, $18::jsonb,
                    TRUE, $19, NOW()
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
                    extended_metrics = EXCLUDED.extended_metrics,
                    is_reachable = TRUE,
                    last_poll_duration_ms = EXCLUDED.last_poll_duration_ms,
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
                json.dumps(enrichment["active_mibs"]),
                enrichment["hardware_model"],
                enrichment["serial_number"],
                enrichment["firmware_version"],
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
