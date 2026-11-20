import asyncio
import importlib.metadata
import json
import logging
import os
import platform
import re
import signal
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple

import asyncpg
import httpx
import redis.asyncio as aioredis

from app.db_schema import ensure_dynamic_config_tables
from app.scanner import SubnetScanner
from app.snmp_poller import SNMPPoller
from app.unifi import UniFiCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("netdash.worker")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://netdash:changeme_strong_db_password@timescaledb:5432/netdash",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

UNIFI_POLL_INTERVAL = max(10, int(os.getenv("UNIFI_POLL_INTERVAL", "30") or 30))
SNMP_POLL_INTERVAL = max(15, int(os.getenv("SNMP_POLL_INTERVAL", "60") or 60))
SUBNET_SCAN_MINUTES = max(1, int(os.getenv("SUBNET_SCAN_MINUTES", "15") or 15))
LIVE_TRAFFIC_INTERVAL = max(1.0, min(10.0, float(os.getenv("LIVE_TRAFFIC_INTERVAL", "2") or 2)))


def _pkg_version(name: str, default: str = "unknown") -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return default


async def _detect_nmap_version() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmap",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        first_line = out.decode("utf-8", errors="ignore").splitlines()[0]
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", first_line)
        return match.group(1) if match else first_line.strip()
    except Exception:
        return "unknown"


async def publish_event(redis_client: aioredis.Redis, event_type: str, data: object) -> None:
    payload = json.dumps(
        {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
    )
    try:
        await redis_client.publish("netdash:live", payload)
    except Exception as exc:
        logger.debug("Redis publish failed: %s", exc)


async def heartbeat_loop(
    redis_client: aioredis.Redis,
    worker_state: Dict[str, Any],
    stop_event: asyncio.Event,
) -> None:
    """Publishes worker status, versions, and loop health to Redis every 10s."""
    while not stop_event.is_set():
        try:
            payload = {
                **worker_state,
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            }
            await redis_client.set("netdash:worker:heartbeat", json.dumps(payload), ex=45)
        except Exception as exc:
            logger.debug("Worker heartbeat write failed: %s", exc)

        done, pending = await asyncio.wait(
            [asyncio.create_task(stop_event.wait())],
            timeout=10,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


async def unifi_loop(
    collector: UniFiCollector,
    pool: asyncpg.Pool,
    redis_client: aioredis.Redis,
    worker_state: Dict[str, Any],
    stop_event: asyncio.Event,
    unifi_trigger: asyncio.Event,
) -> None:
    logger.info("Starting optional UniFi telemetry loop (interval=%ds)", UNIFI_POLL_INTERVAL)
    loop_info = worker_state["loops"]["unifi_collector"]
    while not stop_event.is_set():
        unifi_trigger.clear()
        try:
            summary = await collector.poll_and_store(pool)
            loop_info["status"] = "running"
            loop_info["last_run"] = datetime.now(timezone.utc).isoformat()
            loop_info["detail"] = "Controller polled" if summary else "No UniFi controller configured"
            if summary:
                await publish_event(redis_client, "unifi_update", summary)
        except Exception as exc:
            logger.error("Error in UniFi loop: %s", exc)
            loop_info["status"] = "error"
            loop_info["last_run"] = datetime.now(timezone.utc).isoformat()
            loop_info["detail"] = str(exc)[:160]

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(stop_event.wait()),
                asyncio.create_task(unifi_trigger.wait()),
            ],
            timeout=UNIFI_POLL_INTERVAL,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


async def snmp_loop(
    poller: SNMPPoller,
    pool: asyncpg.Pool,
    redis_client: aioredis.Redis,
    worker_state: Dict[str, Any],
    stop_event: asyncio.Event,
    snmp_trigger: asyncio.Event,
) -> None:
    logger.info("Starting dynamic SNMP poller loop (interval=%ds)", SNMP_POLL_INTERVAL)
    loop_info = worker_state["loops"]["snmp_poller"]
    first_run = True
    while not stop_event.is_set():
        snmp_trigger.clear()
        try:
            force_flag = first_run or bool(await redis_client.getdel("snmp:force_rematch"))
            first_run = False
            results = await poller.poll_all(pool, redis_client, force_rematch=force_flag)
            loop_info["status"] = "running"
            loop_info["last_run"] = datetime.now(timezone.utc).isoformat()
            loop_info["detail"] = f"{len(results)} device(s) polled" if results else "Idle (no active targets)"
            if results:
                await publish_event(redis_client, "snmp_update", results)
        except Exception as exc:
            logger.error("Error in SNMP loop: %s", exc)
            loop_info["status"] = "error"
            loop_info["last_run"] = datetime.now(timezone.utc).isoformat()
            loop_info["detail"] = str(exc)[:160]

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(stop_event.wait()),
                asyncio.create_task(snmp_trigger.wait()),
            ],
            timeout=SNMP_POLL_INTERVAL,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


async def scanner_loop(
    scanner: SubnetScanner,
    pool: asyncpg.Pool,
    redis_client: aioredis.Redis,
    worker_state: Dict[str, Any],
    stop_event: asyncio.Event,
    scan_trigger: asyncio.Event,
) -> None:
    logger.info("Starting dynamic Nmap subnet scanner loop (interval=%dm)", SUBNET_SCAN_MINUTES)
    loop_info = worker_state["loops"]["subnet_scanner"]
    while not stop_event.is_set():
        scan_trigger.clear()
        try:
            loop_info["status"] = "scanning"
            await publish_event(redis_client, "scanner_status", {"scanning": True})
            summaries = await scanner.scan_all_subnets(pool)
            loop_info["status"] = "running"
            loop_info["last_run"] = datetime.now(timezone.utc).isoformat()
            loop_info["detail"] = f"{len(summaries)} subnet(s) scanned" if summaries else "Idle (no subnets configured)"
            await publish_event(
                redis_client,
                "scanner_update",
                {"scanning": False, "summaries": summaries},
            )
        except Exception as exc:
            logger.error("Error in subnet scanner loop: %s", exc)
            loop_info["status"] = "error"
            loop_info["last_run"] = datetime.now(timezone.utc).isoformat()
            loop_info["detail"] = str(exc)[:160]

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(stop_event.wait()),
                asyncio.create_task(scan_trigger.wait()),
            ],
            timeout=SUBNET_SCAN_MINUTES * 60,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


async def live_traffic_loop(
    unifi_collector: UniFiCollector,
    snmp_poller: SNMPPoller,
    pool: asyncpg.Pool,
    redis_client: aioredis.Redis,
    worker_state: Dict[str, Any],
    stop_event: asyncio.Event,
    live_reload_trigger: asyncio.Event,
) -> None:
    """High-frequency traffic streamer publishing 2s live RX/TX samples to Redis + WebSocket."""
    logger.info("Starting live traffic streamer loop (interval=%.1fs)", LIVE_TRAFFIC_INTERVAL)
    loop_info = worker_state["loops"]["live_traffic_streamer"]

    unifi_controllers: List[Dict[str, Any]] = []
    snmp_targets: List[Dict[str, Any]] = []
    db_ignore_indices: Dict[str, Set[int]] = {}
    client_map: Dict[int, httpx.AsyncClient] = {}
    config_signature: str = ""
    last_cfg_refresh = 0.0

    async def _refresh_targets() -> None:
        nonlocal unifi_controllers, snmp_targets, db_ignore_indices, config_signature
        async with pool.acquire() as conn:
            u_rows = await conn.fetch(
                """
                SELECT id, name, host, api_key, site_name, verify_ssl
                FROM unifi_controllers_config
                WHERE enabled = TRUE AND host != '' AND api_key != ''
                ORDER BY id ASC
                """
            )
            s_rows = await conn.fetch(
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
            lo_rows = await conn.fetch(
                """
                SELECT DISTINCT device_ip, if_index
                FROM snmp_metrics_raw
                WHERE time > NOW() - INTERVAL '24 hours'
                  AND COALESCE(LOWER(if_name), '') IN ('lo', 'loopback', 'null0')
                """
            )

        new_unifi = [dict(r) for r in u_rows]
        new_sig = json.dumps(
            [(c["id"], c["host"], c["api_key"], c["site_name"], c["verify_ssl"]) for c in new_unifi],
            sort_keys=True,
        )
        if new_sig != config_signature:
            for c in list(client_map.values()):
                try:
                    await c.aclose()
                except Exception:
                    pass
            client_map.clear()
            config_signature = new_sig

        unifi_controllers = new_unifi
        snmp_targets = [dict(r) for r in s_rows]
        ignores: Dict[str, Set[int]] = {}
        for r in lo_rows:
            ip = str(r["device_ip"])
            ignores.setdefault(ip, set()).add(int(r["if_index"]))
        db_ignore_indices = ignores

    try:
        while not stop_event.is_set():
            tick_start = time.monotonic()
            force_cfg = live_reload_trigger.is_set()
            if force_cfg:
                live_reload_trigger.clear()

            if force_cfg or (tick_start - last_cfg_refresh >= 30.0):
                try:
                    await _refresh_targets()
                    last_cfg_refresh = tick_start
                except Exception as exc:
                    logger.debug("Live traffic config refresh failed: %s", exc)

            try:
                sample = None
                if unifi_controllers:
                    sample = await unifi_collector.poll_live_wan(unifi_controllers, client_map)
                if sample is None and snmp_targets:
                    sample = await snmp_poller.poll_live_traffic(snmp_targets, db_ignore_indices)

                if sample is not None:
                    encoded = json.dumps(sample)
                    await redis_client.lpush("netdash:live:traffic", encoded)
                    await redis_client.ltrim("netdash:live:traffic", 0, 179)
                    await redis_client.expire("netdash:live:traffic", 600)
                    await publish_event(redis_client, "traffic_tick", sample)

                    loop_info["status"] = "running"
                    loop_info["last_run"] = sample["time"]
                    if sample.get("source") == "unifi":
                        loop_info["detail"] = f"Streaming UniFi WAN every {LIVE_TRAFFIC_INTERVAL:g}s"
                    else:
                        loop_info["detail"] = (
                            f"Streaming {sample.get('device_count', 0)} SNMP host(s) every {LIVE_TRAFFIC_INTERVAL:g}s"
                        )
                else:
                    loop_info["status"] = "running"
                    loop_info["detail"] = (
                        "Priming live counters..."
                        if (unifi_controllers or snmp_targets)
                        else "Idle (no active traffic sources)"
                    )
            except Exception as exc:
                logger.error("Error in live traffic loop: %s", exc)
                loop_info["status"] = "error"
                loop_info["last_run"] = datetime.now(timezone.utc).isoformat()
                loop_info["detail"] = str(exc)[:160]

            elapsed = time.monotonic() - tick_start
            remaining = max(0.2, LIVE_TRAFFIC_INTERVAL - elapsed)
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(stop_event.wait()),
                    asyncio.create_task(live_reload_trigger.wait()),
                ],
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    finally:
        for c in list(client_map.values()):
            try:
                await c.aclose()
            except Exception:
                pass
        client_map.clear()


async def command_listener_loop(
    redis_client: aioredis.Redis,
    snmp_poller: SNMPPoller,
    pool: asyncpg.Pool,
    scan_trigger: asyncio.Event,
    snmp_trigger: asyncio.Event,
    unifi_trigger: asyncio.Event,
    live_reload_trigger: asyncio.Event,
    stop_event: asyncio.Event,
) -> None:
    async def _run_targeted_poll(target_ip: str) -> None:
        ok = False
        try:
            res = await snmp_poller.poll_single_ip(target_ip, pool, redis_client)
            if res:
                ok = True
                await publish_event(redis_client, "snmp_update", [res])
        except Exception as exc:
            logger.warning("Targeted SNMP poll failed for %s: %s", target_ip, exc)
        try:
            await redis_client.set(
                f"snmp:poll_done:{target_ip}",
                json.dumps({"ok": ok}),
                ex=20,
            )
        except Exception:
            pass

    pubsub = redis_client.pubsub()
    await pubsub.subscribe("netdash:cmd")
    try:
        while not stop_event.is_set():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("data"):
                raw = msg["data"]
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                if raw.startswith("poll_device:"):
                    target_ip = raw.split(":", 1)[1].strip()
                    if target_ip:
                        asyncio.create_task(_run_targeted_poll(target_ip))
                if raw in ("scan_now", "reload_all"):
                    scan_trigger.set()
                if raw in ("snmp_now", "reload_all", "rematch_mibs"):
                    if raw == "rematch_mibs":
                        await redis_client.set("snmp:force_rematch", "1", ex=60)
                    snmp_trigger.set()
                if raw in ("unifi_now", "reload_all"):
                    unifi_trigger.set()
                if raw in ("snmp_now", "unifi_now", "reload_all"):
                    live_reload_trigger.set()
            await asyncio.sleep(0.2)
    finally:
        await pubsub.close()


async def main() -> None:
    stop_event = asyncio.Event()
    scan_trigger = asyncio.Event()
    snmp_trigger = asyncio.Event()
    unifi_trigger = asyncio.Event()
    live_reload_trigger = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    pool = None
    for attempt in range(1, 20):
        try:
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
            break
        except Exception as exc:
            logger.warning("Waiting for TimescaleDB (%d/20): %s", attempt, exc)
            await asyncio.sleep(3)

    if pool is None:
        raise RuntimeError("Could not connect to TimescaleDB.")

    await ensure_dynamic_config_tables(pool)

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    unifi_collector = UniFiCollector()
    snmp_poller = SNMPPoller()
    subnet_scanner = SubnetScanner()

    worker_state: Dict[str, Any] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "pysnmp_version": _pkg_version("pysnmp"),
        "asyncpg_version": _pkg_version("asyncpg"),
        "redis_py_version": _pkg_version("redis"),
        "nmap_version": await _detect_nmap_version(),
        "loops": {
            "snmp_poller": {
                "status": "starting",
                "interval_sec": SNMP_POLL_INTERVAL,
                "last_run": None,
                "detail": "Initializing",
            },
            "subnet_scanner": {
                "status": "starting",
                "interval_sec": SUBNET_SCAN_MINUTES * 60,
                "last_run": None,
                "detail": "Initializing",
            },
            "unifi_collector": {
                "status": "starting",
                "interval_sec": UNIFI_POLL_INTERVAL,
                "last_run": None,
                "detail": "Initializing",
            },
            "live_traffic_streamer": {
                "status": "starting",
                "interval_sec": LIVE_TRAFFIC_INTERVAL,
                "last_run": None,
                "detail": "Initializing",
            },
        },
    }

    await asyncio.gather(
        heartbeat_loop(redis_client, worker_state, stop_event),
        unifi_loop(unifi_collector, pool, redis_client, worker_state, stop_event, unifi_trigger),
        snmp_loop(snmp_poller, pool, redis_client, worker_state, stop_event, snmp_trigger),
        scanner_loop(subnet_scanner, pool, redis_client, worker_state, stop_event, scan_trigger),
        live_traffic_loop(
            unifi_collector,
            snmp_poller,
            pool,
            redis_client,
            worker_state,
            stop_event,
            live_reload_trigger,
        ),
        command_listener_loop(
            redis_client,
            snmp_poller,
            pool,
            scan_trigger,
            snmp_trigger,
            unifi_trigger,
            live_reload_trigger,
            stop_event,
        ),
    )

    await redis_client.aclose()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
