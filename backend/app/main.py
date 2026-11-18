import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("netdash.api")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://netdash:changeme_strong_db_password@timescaledb:5432/netdash",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
REPO_MOUNT_DIR = Path(os.getenv("NETDASH_REPO_DIR", "/opt/netdash-repo"))

db_pool: Optional[asyncpg.Pool] = None
redis_client: Optional[aioredis.Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, redis_client
    for attempt in range(1, 15):
        try:
            db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=12)
            break
        except Exception as exc:
            logger.warning("Waiting for TimescaleDB (%d/15): %s", attempt, exc)
            await asyncio.sleep(2)
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield
    if redis_client:
        await redis_client.aclose()
    if db_pool:
        await db_pool.close()


app = FastAPI(
    title="NetDash Observability API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_pool() -> asyncpg.Pool:
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database connection pool not initialized.")
    return db_pool


async def _inspect_git_update(do_fetch: bool = False) -> Dict[str, Any]:
    """Inspects /opt/netdash-repo for available commits on origin/main without auto-installing."""
    if not (REPO_MOUNT_DIR / ".git").exists():
        return {
            "update_available": False,
            "current_sha": "local",
            "remote_sha": "local",
            "commits_behind": 0,
            "commits": [],
            "status": "idle",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    # Ensure git trusts the mounted repository directory
    proc_safe = await asyncio.create_subprocess_exec(
        "git",
        "config",
        "--global",
        "--replace-all",
        "safe.directory",
        str(REPO_MOUNT_DIR),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc_safe.wait()

    if do_fetch:
        proc_fetch = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(REPO_MOUNT_DIR),
            "fetch",
            "origin",
            "main",
            "--quiet",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc_fetch.wait(), timeout=15.0)
        except Exception:
            pass

    async def _run_git(*args: str) -> str:
        p = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(REPO_MOUNT_DIR),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        return out.decode("utf-8", errors="ignore").strip()

    local_sha = await _run_git("rev-parse", "--short=7", "HEAD") or "unknown"
    remote_sha = await _run_git("rev-parse", "--short=7", "origin/main") or local_sha
    behind_str = await _run_git("rev-list", "HEAD..origin/main", "--count")
    commits_behind = int(behind_str) if behind_str.isdigit() else 0

    commits: List[Dict[str, str]] = []
    if commits_behind > 0:
        log_out = await _run_git(
            "log",
            "HEAD..origin/main",
            "-n",
            "10",
            "--pretty=format:%h|%s|%an|%ar",
        )
        for line in log_out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append(
                    {
                        "sha": parts[0],
                        "subject": parts[1],
                        "author": parts[2],
                        "date": parts[3],
                    }
                )

    trigger_file = REPO_MOUNT_DIR / ".trigger-update"
    status_file = REPO_MOUNT_DIR / ".update-status.json"
    is_updating = trigger_file.exists()

    payload = {
        "update_available": commits_behind > 0,
        "current_sha": local_sha,
        "remote_sha": remote_sha,
        "commits_behind": commits_behind,
        "commits": commits,
        "status": "updating" if is_updating else "idle",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        status_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass

    return payload


@app.get("/api/health")
async def health_check() -> Dict[str, Any]:
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/system/update")
async def get_update_status() -> Dict[str, Any]:
    return await _inspect_git_update(do_fetch=False)


@app.post("/api/system/update/check")
async def check_for_updates() -> Dict[str, Any]:
    return await _inspect_git_update(do_fetch=True)


@app.post("/api/system/update/install")
async def install_update() -> Dict[str, Any]:
    if not REPO_MOUNT_DIR.exists():
        raise HTTPException(status_code=400, detail="Repository mount /opt/netdash-repo not available.")
    trigger_file = REPO_MOUNT_DIR / ".trigger-update"
    trigger_file.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    return {
        "status": "updating",
        "message": "Update installation triggered on host. Containers will rebuild and restart shortly.",
    }


@app.get("/api/overview")
async def get_pulse_overview() -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        gw_row = await conn.fetchrow(
            """
            SELECT time, site_name, gateway_mac, gateway_name, gateway_model, gateway_version,
                   wan_ip, wan_isp, wan_status, wan_rx_bps, wan_tx_bps, wan_latency_ms,
                   wan_packet_loss_pct, wan_uptime_sec, cpu_usage_pct, mem_usage_pct,
                   temp_celsius, total_poe_watts, active_wan_clients, active_lan_clients,
                   active_wlan_clients, total_devices, online_devices
            FROM unifi_gateway_metrics
            ORDER BY time DESC
            LIMIT 1
            """
        )

        wan_history = await conn.fetch(
            """
            SELECT time_bucket('1 minute', time) AS bucket,
                   AVG(wan_rx_bps) AS rx_bps,
                   AVG(wan_tx_bps) AS tx_bps,
                   AVG(wan_latency_ms) AS latency_ms
            FROM unifi_gateway_metrics
            WHERE time > NOW() - INTERVAL '1 hour'
            GROUP BY bucket
            ORDER BY bucket ASC
            """
        )

        unifi_devs = await conn.fetch(
            """
            SELECT mac, ip_address, name, model, device_type, firmware_version,
                   status, uptime_sec, cpu_usage_pct, mem_usage_pct, temp_celsius,
                   poe_total_watts, poe_max_watts, num_sta, rx_bps, tx_bps, port_table, last_seen
            FROM unifi_devices
            ORDER BY device_type ASC, name ASC
            """
        )

        snmp_devs = await conn.fetch(
            """
            SELECT device_ip, sys_name, sys_descr, sys_location, sys_uptime_ticks,
                   cpu_usage_pct, mem_usage_pct, interface_count, is_reachable,
                   last_poll_duration_ms, last_polled
            FROM snmp_devices
            ORDER BY device_ip ASC
            """
        )

        host_counts = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE is_online = TRUE) AS online_hosts,
                   COUNT(*) AS total_discovered
            FROM discovered_hosts
            """
        )

        log_counts = await conn.fetchrow(
            """
            SELECT COUNT(*) AS total_24h,
                   COUNT(*) FILTER (WHERE severity_num <= 3) AS errors_24h,
                   COUNT(*) FILTER (WHERE severity_num = 4) AS warnings_24h
            FROM device_logs
            WHERE time > NOW() - INTERVAL '24 hours'
            """
        )

    devices_list = []
    for d in unifi_devs:
        item = dict(d)
        if isinstance(item.get("port_table"), str):
            try:
                item["port_table"] = json.loads(item["port_table"])
            except Exception:
                item["port_table"] = []
        item["last_seen"] = item["last_seen"].isoformat() if item.get("last_seen") else None
        devices_list.append(item)

    snmp_list = []
    for s in snmp_devs:
        item = dict(s)
        item["last_polled"] = item["last_polled"].isoformat() if item.get("last_polled") else None
        snmp_list.append(item)

    gw_dict = dict(gw_row) if gw_row else None
    if gw_dict and gw_dict.get("time"):
        gw_dict["time"] = gw_dict["time"].isoformat()

    return {
        "gateway": gw_dict,
        "wan_series": [
            {
                "time": r["bucket"].isoformat(),
                "rx_bps": round(float(r["rx_bps"] or 0.0), 1),
                "tx_bps": round(float(r["tx_bps"] or 0.0), 1),
                "latency_ms": round(float(r["latency_ms"] or 0.0), 2),
            }
            for r in wan_history
        ],
        "unifi_devices": devices_list,
        "snmp_devices": snmp_list,
        "scanner_summary": dict(host_counts) if host_counts else {"online_hosts": 0, "total_discovered": 0},
        "syslog_summary": dict(log_counts) if log_counts else {"total_24h": 0, "errors_24h": 0, "warnings_24h": 0},
    }


@app.get("/api/snmp/interfaces")
async def get_snmp_interfaces(device_ip: Optional[str] = None) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        if device_ip:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (device_ip, if_index)
                    device_ip, hostname, if_index, if_name, if_descr, if_alias,
                    if_speed_bps, if_oper_status, in_bps, out_bps,
                    in_errors, out_errors, cpu_usage, sys_uptime, time
                FROM snmp_metrics_raw
                WHERE device_ip = $1 AND time > NOW() - INTERVAL '24 hours'
                ORDER BY device_ip, if_index, time DESC
                """,
                device_ip,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (device_ip, if_index)
                    device_ip, hostname, if_index, if_name, if_descr, if_alias,
                    if_speed_bps, if_oper_status, in_bps, out_bps,
                    in_errors, out_errors, cpu_usage, sys_uptime, time
                FROM snmp_metrics_raw
                WHERE time > NOW() - INTERVAL '24 hours'
                ORDER BY device_ip, if_index, time DESC
                """
            )
    items = []
    for r in rows:
        d = dict(r)
        d["time"] = d["time"].isoformat() if d.get("time") else None
        items.append(d)
    return {"interfaces": items}


@app.get("/api/snmp/trends")
async def get_snmp_trends(
    device_ip: str = Query(...),
    if_index: int = Query(...),
    range_key: str = Query("24h", alias="range"),
) -> Dict[str, Any]:
    """
    Routes time-range queries <= 24h to `snmp_metrics_raw` and queries > 24h
    (`7d`, `30d`, `1y`, `all`) to `snmp_metrics_hourly` or `snmp_metrics_daily`.
    """
    pool = _get_pool()
    rk = range_key.lower()

    interval_map = {
        "1h": "1 hour",
        "6h": "6 hours",
        "24h": "24 hours",
        "7d": "7 days",
        "30d": "30 days",
        "1y": "365 days",
    }

    async with pool.acquire() as conn:
        if rk in ("1h", "6h", "24h"):
            sql_interval = interval_map[rk]
            rows = await conn.fetch(
                f"""
                SELECT time AS bucket,
                       in_bps AS avg_in_bps,
                       in_bps AS max_in_bps,
                       in_bps AS p95_in_bps,
                       out_bps AS avg_out_bps,
                       out_bps AS max_out_bps,
                       out_bps AS p95_out_bps,
                       cpu_usage AS avg_cpu_usage
                FROM snmp_metrics_raw
                WHERE device_ip = $1
                  AND if_index = $2
                  AND time > NOW() - INTERVAL '{sql_interval}'
                ORDER BY time ASC
                """,
                device_ip,
                if_index,
            )
            source_table = "snmp_metrics_raw"
        elif rk in ("7d", "30d"):
            sql_interval = interval_map[rk]
            rows = await conn.fetch(
                f"""
                SELECT bucket,
                       avg_in_bps,
                       max_in_bps,
                       p95_in_bps,
                       avg_out_bps,
                       max_out_bps,
                       p95_out_bps,
                       avg_cpu_usage
                FROM snmp_metrics_hourly
                WHERE device_ip = $1
                  AND if_index = $2
                  AND bucket > NOW() - INTERVAL '{sql_interval}'
                ORDER BY bucket ASC
                """,
                device_ip,
                if_index,
            )
            source_table = "snmp_metrics_hourly"
        else:
            # 1y or all -> query snmp_metrics_daily (retained indefinitely)
            where_time = "AND bucket > NOW() - INTERVAL '365 days'" if rk == "1y" else ""
            rows = await conn.fetch(
                f"""
                SELECT bucket,
                       avg_in_bps,
                       max_in_bps,
                       p95_in_bps,
                       avg_out_bps,
                       max_out_bps,
                       p95_out_bps,
                       avg_cpu_usage
                FROM snmp_metrics_daily
                WHERE device_ip = $1
                  AND if_index = $2
                  {where_time}
                ORDER BY bucket ASC
                """,
                device_ip,
                if_index,
            )
            source_table = "snmp_metrics_daily"

    series = [
        {
            "time": r["bucket"].isoformat(),
            "avg_in_bps": round(float(r["avg_in_bps"] or 0.0), 1),
            "max_in_bps": round(float(r["max_in_bps"] or 0.0), 1),
            "p95_in_bps": round(float(r["p95_in_bps"] or 0.0), 1),
            "avg_out_bps": round(float(r["avg_out_bps"] or 0.0), 1),
            "max_out_bps": round(float(r["max_out_bps"] or 0.0), 1),
            "p95_out_bps": round(float(r["p95_out_bps"] or 0.0), 1),
            "avg_cpu_usage": round(float(r["avg_cpu_usage"] or 0.0), 1),
        }
        for r in rows
    ]

    return {
        "device_ip": device_ip,
        "if_index": if_index,
        "range": rk,
        "source_table": source_table,
        "series": series,
    }


@app.get("/api/scanner/hosts")
async def get_discovered_hosts(subnet: Optional[str] = None) -> Dict[str, Any]:
    pool = _get_pool()
    allowed_raw = os.getenv("ALLOWED_SUBNETS", "")
    configured_subnets = [s.strip() for s in allowed_raw.split(",") if s.strip()]

    async with pool.acquire() as conn:
        if subnet:
            rows = await conn.fetch(
                """
                SELECT host(ip_address) AS ip_address, subnet_cidr, mac_address,
                       hostname, vendor, vlan_id, is_online, latency_ms,
                       discovery_source, switch_mac, switch_port, first_seen, last_seen
                FROM discovered_hosts
                WHERE subnet_cidr = $1
                ORDER BY ip_address ASC
                """,
                subnet,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT host(ip_address) AS ip_address, subnet_cidr, mac_address,
                       hostname, vendor, vlan_id, is_online, latency_ms,
                       discovery_source, switch_mac, switch_port, first_seen, last_seen
                FROM discovered_hosts
                ORDER BY ip_address ASC
                """
            )

        recent_runs = await conn.fetch(
            """
            SELECT DISTINCT ON (subnet_cidr)
                subnet_cidr, scan_time, total_ips, hosts_up, new_hosts, scan_duration_sec
            FROM subnet_scan_runs
            ORDER BY subnet_cidr, scan_time DESC
            """
        )

    hosts = []
    for r in rows:
        d = dict(r)
        d["first_seen"] = d["first_seen"].isoformat() if d.get("first_seen") else None
        d["last_seen"] = d["last_seen"].isoformat() if d.get("last_seen") else None
        hosts.append(d)

    runs = []
    for r in recent_runs:
        d = dict(r)
        d["scan_time"] = d["scan_time"].isoformat() if d.get("scan_time") else None
        runs.append(d)

    return {
        "configured_subnets": configured_subnets,
        "scan_runs": runs,
        "hosts": hosts,
    }


@app.post("/api/scanner/trigger")
async def trigger_subnet_scan() -> Dict[str, str]:
    if redis_client:
        await redis_client.publish("netdash:cmd", "scan_now")
    return {"status": "triggered", "message": "Immediate Nmap subnet sweep queued."}


@app.get("/api/logs")
async def get_device_logs(
    severity: Optional[str] = None,
    source_ip: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(150, ge=10, le=500),
) -> Dict[str, Any]:
    pool = _get_pool()
    clauses = ["time > NOW() - INTERVAL '30 days'"]
    params: List[Any] = []
    idx = 1

    if severity and severity != "all":
        clauses.append(f"severity = ${idx}")
        params.append(severity.lower())
        idx += 1

    if source_ip and source_ip != "all":
        clauses.append(f"source_ip = ${idx}")
        params.append(source_ip)
        idx += 1

    if search and search.strip():
        clauses.append(f"(message ILIKE ${idx} OR hostname ILIKE ${idx} OR app_name ILIKE ${idx})")
        params.append(f"%{search.strip()}%")
        idx += 1

    where_sql = " AND ".join(clauses)
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT time, source_ip, hostname, facility, severity, severity_num,
                   app_name, proc_id, message
            FROM device_logs
            WHERE {where_sql}
            ORDER BY time DESC
            LIMIT ${idx}
            """,
            *params,
        )

        sources = await conn.fetch(
            """
            SELECT DISTINCT source_ip, MAX(hostname) AS hostname
            FROM device_logs
            WHERE time > NOW() - INTERVAL '7 days'
            GROUP BY source_ip
            ORDER BY source_ip ASC
            """
        )

    logs = []
    for r in rows:
        d = dict(r)
        d["time"] = d["time"].isoformat() if d.get("time") else None
        logs.append(d)

    return {
        "logs": logs,
        "sources": [dict(s) for s in sources],
    }


@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket) -> None:
    await ws.accept()
    if not redis_client:
        await ws.close()
        return

    pubsub = redis_client.pubsub()
    await pubsub.subscribe("netdash:live")
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("data"):
                raw = msg["data"]
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                await ws.send_text(raw)
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket closed: %s", exc)
    finally:
        await pubsub.close()
