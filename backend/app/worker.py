import asyncio
import importlib.metadata
import json
import logging
import os
import platform
import re
import signal
from datetime import datetime, timezone
from typing import Any, Dict

import asyncpg
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


async def command_listener_loop(
    redis_client: aioredis.Redis,
    scan_trigger: asyncio.Event,
    snmp_trigger: asyncio.Event,
    unifi_trigger: asyncio.Event,
    stop_event: asyncio.Event,
) -> None:
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("netdash:cmd")
    try:
        while not stop_event.is_set():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("data"):
                raw = msg["data"]
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                if raw in ("scan_now", "reload_all"):
                    scan_trigger.set()
                if raw in ("snmp_now", "reload_all", "rematch_mibs"):
                    if raw == "rematch_mibs":
                        await redis_client.set("snmp:force_rematch", "1", ex=60)
                    snmp_trigger.set()
                if raw in ("unifi_now", "reload_all"):
                    unifi_trigger.set()
            await asyncio.sleep(0.2)
    finally:
        await pubsub.close()


async def main() -> None:
    stop_event = asyncio.Event()
    scan_trigger = asyncio.Event()
    snmp_trigger = asyncio.Event()
    unifi_trigger = asyncio.Event()

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
        },
    }

    await asyncio.gather(
        heartbeat_loop(redis_client, worker_state, stop_event),
        unifi_loop(unifi_collector, pool, redis_client, worker_state, stop_event, unifi_trigger),
        snmp_loop(snmp_poller, pool, redis_client, worker_state, stop_event, snmp_trigger),
        scanner_loop(subnet_scanner, pool, redis_client, worker_state, stop_event, scan_trigger),
        command_listener_loop(redis_client, scan_trigger, snmp_trigger, unifi_trigger, stop_event),
    )

    await redis_client.aclose()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
