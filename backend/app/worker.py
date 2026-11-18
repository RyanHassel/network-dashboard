import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis

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


async def unifi_loop(
    collector: UniFiCollector,
    pool: asyncpg.Pool,
    redis_client: aioredis.Redis,
    stop_event: asyncio.Event,
) -> None:
    logger.info("Starting UDM Pro telemetry loop (interval=%ds)", UNIFI_POLL_INTERVAL)
    while not stop_event.is_set():
        try:
            summary = await collector.poll_and_store(pool)
            if summary:
                await publish_event(redis_client, "unifi_update", summary)
        except Exception as exc:
            logger.error("Error in UDM Pro loop: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=UNIFI_POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def snmp_loop(
    poller: SNMPPoller,
    pool: asyncpg.Pool,
    redis_client: aioredis.Redis,
    stop_event: asyncio.Event,
) -> None:
    logger.info(
        "Starting SNMP poller loop (targets=%d, interval=%ds)",
        len(poller.targets),
        SNMP_POLL_INTERVAL,
    )
    while not stop_event.is_set():
        try:
            results = await poller.poll_all(pool, redis_client)
            if results:
                await publish_event(redis_client, "snmp_update", results)
        except Exception as exc:
            logger.error("Error in SNMP loop: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SNMP_POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def scanner_loop(
    scanner: SubnetScanner,
    pool: asyncpg.Pool,
    redis_client: aioredis.Redis,
    stop_event: asyncio.Event,
    scan_trigger: asyncio.Event,
) -> None:
    logger.info(
        "Starting Nmap subnet scanner loop (subnets=%s, interval=%dm)",
        scanner.subnets,
        SUBNET_SCAN_MINUTES,
    )
    while not stop_event.is_set():
        scan_trigger.clear()
        try:
            await publish_event(redis_client, "scanner_status", {"scanning": True})
            summaries = await scanner.scan_all_subnets(pool)
            await publish_event(
                redis_client,
                "scanner_update",
                {"scanning": False, "summaries": summaries},
            )
        except Exception as exc:
            logger.error("Error in subnet scanner loop: %s", exc)

        # Wait either for the scheduled interval, manual scan trigger, or shutdown
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
                if raw == "scan_now":
                    logger.info("Received manual subnet scan trigger via Redis.")
                    scan_trigger.set()
            await asyncio.sleep(0.2)
    finally:
        await pubsub.close()


async def main() -> None:
    stop_event = asyncio.Event()
    scan_trigger = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    # Retry DB connection on startup
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

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    unifi_collector = UniFiCollector()
    snmp_poller = SNMPPoller()
    subnet_scanner = SubnetScanner()

    await asyncio.gather(
        unifi_loop(unifi_collector, pool, redis_client, stop_event),
        snmp_loop(snmp_poller, pool, redis_client, stop_event),
        scanner_loop(subnet_scanner, pool, redis_client, stop_event, scan_trigger),
        command_listener_loop(redis_client, scan_trigger, stop_event),
    )

    await redis_client.aclose()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
