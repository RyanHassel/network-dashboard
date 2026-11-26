import asyncio
import logging
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.db_schema import ensure_device_ids

logger = logging.getLogger("netdash.icmp")

_LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)%\s*packet\s*loss", re.IGNORECASE)
_RTT_SUMMARY_RE = re.compile(
    r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*[\d.]+/([\d.]+)/[\d.]+/[\d.]+\s*ms",
    re.IGNORECASE,
)
_PER_PKT_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms", re.IGNORECASE)


def parse_ping_output(stdout: str, returncode: int) -> Tuple[bool, Optional[float], float]:
    """Parses Linux iputils-ping or BSD ping stdout into (is_reachable, avg_rtt_ms, loss_pct)."""
    text = stdout or ""
    loss_match = _LOSS_RE.search(text)
    if loss_match:
        try:
            loss_pct = max(0.0, min(100.0, float(loss_match.group(1))))
        except ValueError:
            loss_pct = 100.0 if returncode != 0 else 0.0
    else:
        loss_pct = 100.0 if returncode != 0 else 0.0

    avg_rtt: Optional[float] = None
    summary_match = _RTT_SUMMARY_RE.search(text)
    if summary_match:
        try:
            avg_rtt = round(float(summary_match.group(1)), 2)
        except ValueError:
            avg_rtt = None

    if avg_rtt is None:
        times: List[float] = []
        for m in _PER_PKT_TIME_RE.finditer(text):
            try:
                times.append(float(m.group(1)))
            except ValueError:
                continue
        if times:
            avg_rtt = round(sum(times) / len(times), 2)

    is_reachable = (loss_pct < 100.0) and (returncode == 0 or avg_rtt is not None)
    if not is_reachable:
        avg_rtt = None
        loss_pct = 100.0
    return is_reachable, avg_rtt, round(loss_pct, 1)


async def resolve_reverse_dns(ip: str, timeout: float = 1.5) -> Optional[str]:
    """Resolves a reverse-DNS PTR hostname for ip, returning None if unresolved or equal to ip."""
    clean_ip = str(ip or "").strip()
    if not clean_ip:
        return None
    loop = asyncio.get_running_loop()
    try:
        res = await asyncio.wait_for(
            loop.run_in_executor(None, socket.gethostbyaddr, clean_ip),
            timeout=timeout,
        )
        host = str(res[0] or "").strip().rstrip(".")
        if host and host != clean_ip:
            return host
    except Exception:
        pass
    return None


class ICMPPoller:
    """Asynchronous ICMP echo latency, availability, and reverse-DNS collector."""

    def __init__(self, concurrency: int = 20) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._last_polled_mono: Dict[str, float] = {}

    async def ping_host(self, ip: str) -> Dict[str, Any]:
        """Executes a 3-packet ping probe and reverse-DNS PTR lookup concurrently."""
        async def _do_ping() -> Tuple[bool, Optional[float], float]:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ping",
                    "-n",
                    "-c",
                    "3",
                    "-W",
                    "1",
                    "-i",
                    "0.2",
                    ip,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=6.0)
                return parse_ping_output(
                    out.decode("utf-8", errors="ignore"),
                    proc.returncode if proc.returncode is not None else 1,
                )
            except Exception as exc:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                logger.debug("ICMP ping failed for %s: %s", ip, exc)
                return False, None, 100.0

        async with self._sem:
            ping_res, dns_host = await asyncio.gather(
                _do_ping(),
                resolve_reverse_dns(ip),
            )
        is_reachable, rtt_ms, loss_pct = ping_res
        return {
            "device_ip": ip,
            "is_reachable": is_reachable,
            "rtt_ms": rtt_ms,
            "packet_loss_pct": loss_pct,
            "resolved_hostname": dns_host,
        }

    async def _store_result(self, conn: asyncpg.Connection, res: Dict[str, Any]) -> None:
        ip = str(res["device_ip"])
        is_reachable = bool(res["is_reachable"])
        rtt_ms = res["rtt_ms"]
        loss_pct = float(res["packet_loss_pct"])
        dns_host = res.get("resolved_hostname")

        await conn.execute(
            """
            UPDATE icmp_targets_config
            SET is_reachable = $2,
                last_rtt_ms = $3,
                packet_loss_pct = $4,
                resolved_hostname = COALESCE($5, resolved_hostname),
                status_since = CASE
                    WHEN status_since IS NULL OR is_reachable IS DISTINCT FROM $2 THEN NOW()
                    ELSE status_since
                END,
                last_polled = NOW()
            WHERE device_ip = $1
            """,
            ip,
            is_reachable,
            rtt_ms,
            loss_pct,
            dns_host,
        )
        await conn.execute(
            """
            INSERT INTO icmp_metrics_raw (
                time, device_ip, is_reachable, rtt_ms, packet_loss_pct
            ) VALUES (NOW(), $1, $2, $3, $4)
            """,
            ip,
            is_reachable,
            rtt_ms,
            loss_pct,
        )

    async def poll_single_ip(
        self, ip: str, pool: asyncpg.Pool
    ) -> Optional[Dict[str, Any]]:
        res = await self.ping_host(ip)
        async with pool.acquire() as conn:
            await self._store_result(conn, res)
            await ensure_device_ids(conn, [ip], touch_last_seen=bool(res["is_reachable"]))
        self._last_polled_mono[ip] = time.monotonic()
        return res

    async def poll_all(
        self, pool: asyncpg.Pool, force: bool = False
    ) -> List[Dict[str, Any]]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT device_ip, poll_interval
                FROM icmp_targets_config
                WHERE enabled = TRUE
                ORDER BY id ASC
                """
            )

        if not rows:
            return []

        now_mono = time.monotonic()
        due_ips: List[str] = []
        for r in rows:
            ip = str(r["device_ip"] or "").strip()
            if not ip:
                continue
            interval = max(10, int(r["poll_interval"] or 30))
            last_m = self._last_polled_mono.get(ip)
            if force or last_m is None or (now_mono - last_m) >= (interval - 1.0):
                due_ips.append(ip)

        if not due_ips:
            return []

        results = await asyncio.gather(*(self.ping_host(ip) for ip in due_ips))
        async with pool.acquire() as conn:
            for res in results:
                await self._store_result(conn, res)
                self._last_polled_mono[str(res["device_ip"])] = time.monotonic()
            online_ips = [str(r["device_ip"]) for r in results if r["is_reachable"]]
            await ensure_device_ids(conn, due_ips, touch_last_seen=False)
            if online_ips:
                await ensure_device_ids(conn, online_ips, touch_last_seen=True)
        return list(results)
