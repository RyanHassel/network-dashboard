import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import redis.asyncio as aioredis

logger = logging.getLogger("netdash.snmp")

# Standard numeric OIDs for SNMPv2-MIB, IF-MIB (64-bit ifXTable), and HOST-RESOURCES-MIB
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_LOCATION = "1.3.6.1.2.1.1.6.0"

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

OID_HR_PROC_LOAD = "1.3.6.1.2.1.25.3.3.1.2"


class SNMPPoller:
    """Asynchronous SNMPv2c/v3 GETBULK collector with 64-bit counter delta rate calculation."""

    def __init__(self) -> None:
        raw_targets = os.getenv("SNMP_TARGETS", "").strip()
        self.targets = [t.strip() for t in raw_targets.split(",") if t.strip()][:50]
        self.version = os.getenv("SNMP_VERSION", "v2c").strip().lower()
        self.community = os.getenv("SNMP_COMMUNITY", "public").strip() or "public"
        self.port = int(os.getenv("SNMP_PORT", "161") or 161)
        self.v3_user = os.getenv("SNMP_V3_USER", "").strip()
        self.v3_auth_key = os.getenv("SNMP_V3_AUTH_KEY", "").strip()
        self.v3_priv_key = os.getenv("SNMP_V3_PRIV_KEY", "").strip()
        self.v3_auth_proto = os.getenv("SNMP_V3_AUTH_PROTOCOL", "SHA").strip().upper()
        self.v3_priv_proto = os.getenv("SNMP_V3_PRIV_PROTOCOL", "AES").strip().upper()

    def _build_auth_args(self) -> List[str]:
        if self.version == "v3" and self.v3_user:
            args = ["-v3", "-u", self.v3_user]
            if self.v3_auth_key and self.v3_priv_key:
                args.extend(
                    [
                        "-l",
                        "authPriv",
                        "-a",
                        self.v3_auth_proto,
                        "-A",
                        self.v3_auth_key,
                        "-x",
                        self.v3_priv_proto,
                        "-X",
                        self.v3_priv_key,
                    ]
                )
            elif self.v3_auth_key:
                args.extend(["-l", "authNoPriv", "-a", self.v3_auth_proto, "-A", self.v3_auth_key])
            else:
                args.extend(["-l", "noAuthNoPriv"])
            return args
        return ["-v2c", "-c", self.community]

    async def _snmp_walk_oid(self, target: str, base_oid: str) -> Dict[str, str]:
        cmd = [
            "snmpbulkwalk",
            "-On",
            "-Oq",
            "-t",
            "2",
            "-r",
            "1",
            *self._build_auth_args(),
            f"{target}:{self.port}",
            base_oid,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
            if proc.returncode != 0 and not stdout:
                return {}
            result: Dict[str, str] = {}
            prefix = f".{base_oid}."
            for line in stdout.decode("utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or "No Such" in line:
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

    async def _poll_single_device(
        self,
        target: str,
        pool: asyncpg.Pool,
        redis_client: aioredis.Redis,
    ) -> Optional[Dict[str, Any]]:
        start_ts = time.perf_counter()

        # Fetch system scalars first
        sys_scalars = await self._snmp_walk_oid(target, "1.3.6.1.2.1.1")
        if not sys_scalars:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO snmp_devices (device_ip, is_reachable, last_polled)
                    VALUES ($1, FALSE, NOW())
                    ON CONFLICT (device_ip) DO UPDATE SET
                        is_reachable = FALSE,
                        last_polled = NOW()
                    """,
                    target,
                )
            return None

        sys_descr = sys_scalars.get("1.0", "")
        sys_obj_id = sys_scalars.get("2.0", "")
        raw_uptime = sys_scalars.get("3.0", "0").split(":")[0].strip()
        try:
            # Handle timeticks or days/hours string
            sys_uptime = int(raw_uptime) if raw_uptime.isdigit() else 0
        except ValueError:
            sys_uptime = 0
        sys_name = sys_scalars.get("5.0", target) or target
        sys_location = sys_scalars.get("6.0", "")

        # Walk interface tables & CPU load concurrently
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
        ) = await asyncio.gather(
            self._snmp_walk_oid(target, OID_IF_NAME),
            self._snmp_walk_oid(target, OID_IF_DESCR),
            self._snmp_walk_oid(target, OID_IF_ALIAS),
            self._snmp_walk_oid(target, OID_IF_HIGH_SPEED),
            self._snmp_walk_oid(target, OID_IF_OPER_STATUS),
            self._snmp_walk_oid(target, OID_IF_HC_IN_OCTETS),
            self._snmp_walk_oid(target, OID_IF_HC_OUT_OCTETS),
            self._snmp_walk_oid(target, OID_IF_IN_ERRORS),
            self._snmp_walk_oid(target, OID_IF_OUT_ERRORS),
            self._snmp_walk_oid(target, OID_IF_IN_DISCARDS),
            self._snmp_walk_oid(target, OID_IF_OUT_DISCARDS),
            self._snmp_walk_oid(target, OID_HR_PROC_LOAD),
        )

        cpu_vals = [float(v) for v in cpu_loads.values() if v.replace(".", "", 1).isdigit()]
        avg_cpu = round(sum(cpu_vals) / len(cpu_vals), 2) if cpu_vals else 0.0
        mem_usage = 0.0

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

            # Compute delta bps against previous counter stored in Redis
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
                    device_ip, sys_name, sys_descr, sys_object_id, sys_location,
                    sys_uptime_ticks, cpu_usage_pct, mem_usage_pct, interface_count,
                    is_reachable, last_poll_duration_ms, last_polled
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    TRUE, $10, NOW()
                )
                ON CONFLICT (device_ip) DO UPDATE SET
                    sys_name = EXCLUDED.sys_name,
                    sys_descr = EXCLUDED.sys_descr,
                    sys_object_id = EXCLUDED.sys_object_id,
                    sys_location = EXCLUDED.sys_location,
                    sys_uptime_ticks = EXCLUDED.sys_uptime_ticks,
                    cpu_usage_pct = EXCLUDED.cpu_usage_pct,
                    mem_usage_pct = EXCLUDED.mem_usage_pct,
                    interface_count = EXCLUDED.interface_count,
                    is_reachable = TRUE,
                    last_poll_duration_ms = EXCLUDED.last_poll_duration_ms,
                    last_polled = NOW()
                """,
                target,
                sys_name,
                sys_descr,
                sys_obj_id,
                sys_location,
                sys_uptime,
                avg_cpu,
                mem_usage,
                len(rows_to_insert),
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
            "interfaces": len(rows_to_insert),
            "cpu_usage_pct": avg_cpu,
            "poll_ms": poll_ms,
        }

    async def poll_all(
        self, pool: asyncpg.Pool, redis_client: aioredis.Redis
    ) -> List[Dict[str, Any]]:
        if not self.targets:
            return []
        tasks = [self._poll_single_device(t, pool, redis_client) for t in self.targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid: List[Dict[str, Any]] = []
        for res in results:
            if isinstance(res, dict):
                valid.append(res)
            elif isinstance(res, Exception):
                logger.warning("SNMP poll exception: %s", res)
        return valid
