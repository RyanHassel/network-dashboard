import asyncio
import importlib.metadata
import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.db_schema import ensure_dynamic_config_tables
from app.mib_catalog import (
    MIBS_DIR,
    VENDOR_CATALOG,
    list_backend_mibs,
    match_device_vendor_profile,
    parse_mib_text,
)

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
_RAW_STATE_DIR = Path(os.getenv("NETDASH_STATE_DIR", "/var/lib/netdash-state"))
STATE_DIR = _RAW_STATE_DIR if (_RAW_STATE_DIR.exists() or os.access(_RAW_STATE_DIR.parent, os.W_OK)) else (REPO_MOUNT_DIR / ".netdash-state")
PROGRESS_FILE = STATE_DIR / "update-progress.json"
LOCK_FILE = STATE_DIR / "updater.lock"
API_START_TIME = datetime.now(timezone.utc)

db_pool: Optional[asyncpg.Pool] = None
redis_client: Optional[aioredis.Redis] = None


def _default_idle_progress() -> Dict[str, Any]:
    return {
        "status": "idle",
        "update_id": "",
        "overall_pct": 0,
        "phase": "idle",
        "message": "No active update",
        "from_sha": "",
        "target_sha": "",
        "from_version": "",
        "target_version": "",
        "started_at": None,
        "updated_at": None,
        "components": [],
    }


def _write_update_progress(payload: Dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(STATE_DIR, 0o755)
        tmp = PROGRESS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, PROGRESS_FILE)
    except Exception as exc:
        logger.debug("Unable to write update progress: %s", exc)


def _read_update_progress() -> Dict[str, Any]:
    try:
        if PROGRESS_FILE.exists():
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict) and data.get("status"):
                return data
    except Exception:
        pass
    return _default_idle_progress()


def _extract_commit_bullets(subject: str, body: str = "") -> List[str]:
    """Extracts clean per-commit bullet points from body or multi-clause subject."""
    bullets: List[str] = []
    for bline in (body or "").splitlines():
        bclean = bline.strip()
        if bclean.startswith(("- ", "* ", "• ")):
            btext = bclean[2:].strip()
            if btext and btext not in bullets:
                bullets.append(btext)
    if bullets:
        return bullets

    raw_clauses = [
        c.strip().rstrip(".")
        for c in (subject or "").replace(" and ", ", ").split(",")
        if c.strip()
    ]
    for clause in raw_clauses:
        formatted = clause[0].upper() + clause[1:] if clause else ""
        if formatted and formatted not in bullets:
            bullets.append(formatted)
    if not bullets and subject.strip():
        bullets.append(subject.strip())
    return bullets


async def _compute_git_version_map(ref: str = "origin/main") -> Dict[str, str]:
    """Maps commit short SHAs to [Year.Month.Day.UpdateCount] version strings."""
    if not (REPO_MOUNT_DIR / ".git").exists():
        return {}

    async def _git_lines(target_ref: str) -> List[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(REPO_MOUNT_DIR),
                "log",
                "--first-parent",
                "--reverse",
                "--pretty=format:%h|%cs",
                target_ref,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return out.decode("utf-8", errors="ignore").splitlines()
        except Exception:
            return []

    def _populate(lines: List[str], out_map: Dict[str, str]) -> None:
        day_counts: Dict[str, int] = {}
        for line in lines:
            if "|" not in line:
                continue
            sha_s, ds = line.split("|", 1)
            sha_s = sha_s.strip()
            ds = ds.strip()
            if not sha_s or len(ds) != 10:
                continue
            day_counts[ds] = day_counts.get(ds, 0) + 1
            parts = ds.split("-")
            if len(parts) == 3:
                out_map[sha_s] = f"{parts[0]}.{parts[1]}.{parts[2]}.{day_counts[ds]}"

    ver_map: Dict[str, str] = {}
    head_lines = await _git_lines("HEAD")
    _populate(head_lines, ver_map)
    if ref and ref != "HEAD":
        ref_lines = await _git_lines(ref)
        if ref_lines:
            _populate(ref_lines, ver_map)
    return ver_map


async def _reconcile_update_progress() -> Dict[str, Any]:
    data = _read_update_progress()
    if data.get("status") != "updating":
        return data

    # Never reconcile while host updater lock is actively held
    if LOCK_FILE.exists():
        try:
            lock_age = time.time() - LOCK_FILE.stat().st_mtime
            if lock_age < 900:
                return data
        except Exception:
            pass

    now = datetime.now(timezone.utc)
    updated_at_str = data.get("updated_at") or data.get("started_at")
    age_sec = 0.0
    if isinstance(updated_at_str, str) and updated_at_str:
        try:
            dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            age_sec = max(0.0, (now - dt).total_seconds())
        except Exception:
            age_sec = 0.0

    # Clear stale locks older than 15 minutes
    if age_sec > 900:
        idle = _default_idle_progress()
        _write_update_progress(idle)
        return idle

    trigger_exists = (REPO_MOUNT_DIR / ".trigger-update").exists()
    target_sha = str(data.get("target_sha") or "").strip()
    started_at_str = data.get("started_at")
    started_dt: Optional[datetime] = None
    if isinstance(started_at_str, str) and started_at_str:
        try:
            started_dt = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
        except Exception:
            started_dt = None

    # Only mark complete if trigger file is gone AND this API process actually started AFTER the update began
    if (
        not trigger_exists
        and target_sha
        and started_dt is not None
        and API_START_TIME > started_dt
        and (REPO_MOUNT_DIR / ".git").exists()
    ):
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(REPO_MOUNT_DIR),
                "rev-parse",
                "--short=7",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            head_sha = out.decode("utf-8", errors="ignore").strip()
            if head_sha and head_sha == target_sha:
                target_ver = str(data.get("target_version") or target_sha)
                completed = {
                    **data,
                    "status": "completed",
                    "overall_pct": 100,
                    "phase": "completed",
                    "message": f"Update to {target_ver} installed successfully!",
                    "updated_at": now.isoformat(),
                    "components": [
                        {
                            "id": c.get("id", ""),
                            "name": c.get("name", ""),
                            "status": "completed",
                            "pct": 100,
                            "detail": f"Updated to {target_ver}",
                        }
                        for c in (data.get("components") or [])
                    ],
                }
                _write_update_progress(completed)
                return completed
        except Exception:
            pass

    return data


def _pkg_version(name: str, default: str = "unknown") -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return default


class SNMPProfilePayload(BaseModel):
    name: str
    version: str = "v2c"
    port: int = 161
    community: str = "public"
    v3_user: str = ""
    v3_auth_proto: str = "SHA"
    v3_auth_key: str = ""
    v3_priv_proto: str = "AES"
    v3_priv_key: str = ""
    poll_interval: int = 60


class SNMPTargetPayload(BaseModel):
    device_ip: str
    label: str = ""
    profile_id: Optional[int] = None
    enabled: bool = True


class SubnetConfigPayload(BaseModel):
    subnet_cidr: str
    label: str = ""
    vlan_id: Optional[int] = None
    enabled: bool = True


class UniFiControllerPayload(BaseModel):
    name: str = "UDM Pro"
    host: str
    api_key: str = ""
    site_name: str = "default"
    verify_ssl: bool = False
    poll_interval: int = 30
    enabled: bool = True


class OnboardingPayload(BaseModel):
    skipped: bool = False
    subnets: List[SubnetConfigPayload] = []
    snmp_profiles: List[SNMPProfilePayload] = []
    snmp_targets: List[SNMPTargetPayload] = []
    unifi_controllers: List[UniFiControllerPayload] = []


class CustomMIBUploadPayload(BaseModel):
    filename: str
    content: str


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
    if db_pool is not None:
        await ensure_dynamic_config_tables(db_pool)
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    await _reconcile_update_progress()
    yield
    if redis_client:
        await redis_client.aclose()
    if db_pool:
        await db_pool.close()


app = FastAPI(
    title="NetDash Observability API",
    version="1.1.0",
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


async def _notify_worker(cmd: str) -> None:
    if redis_client:
        try:
            await redis_client.publish("netdash:cmd", cmd)
        except Exception:
            pass


async def _read_live_traffic_series(limit: int = 120) -> List[Dict[str, Any]]:
    """Reads cached 2s live traffic samples from Redis in ascending chronological order."""
    if redis_client is None:
        return []
    try:
        raw_items = await asyncio.wait_for(
            redis_client.lrange("netdash:live:traffic", 0, max(0, limit - 1)),
            timeout=1.0,
        )
    except Exception:
        return []

    series: List[Dict[str, Any]] = []
    for raw in reversed(raw_items):
        try:
            item = json.loads(raw)
            if isinstance(item, dict) and item.get("time"):
                series.append(item)
        except Exception:
            continue
    return series


async def _inspect_git_update(do_fetch: bool = False) -> Dict[str, Any]:
    """Inspects /opt/netdash-repo for available commits on origin/main without auto-installing."""
    if not (REPO_MOUNT_DIR / ".git").exists():
        return {
            "update_available": False,
            "current_sha": "local",
            "remote_sha": "local",
            "current_version": "local",
            "remote_version": "local",
            "commits_behind": 0,
            "commits": [],
            "updates": [],
            "summary_bullets": [],
            "status": "idle",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

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

    ver_map = await _compute_git_version_map("origin/main")
    current_version = ver_map.get(local_sha, local_sha)
    remote_version = ver_map.get(remote_sha, remote_sha)

    commits: List[Dict[str, Any]] = []
    summary_bullets: List[str] = []
    if commits_behind > 0:
        log_out = await _run_git(
            "log",
            "HEAD..origin/main",
            "-n",
            "20",
            "--pretty=format:%h%x1f%s%x1f%an%x1f%ar%x1f%cs%x1f%b%x1e",
        )
        for rec in log_out.split("\x1e"):
            rec = rec.strip()
            if not rec:
                continue
            parts = rec.split("\x1f", 5)
            if len(parts) >= 5:
                sha_val = parts[0].strip()
                subj_val = parts[1].strip()
                auth_val = parts[2].strip()
                date_val = parts[3].strip()
                commit_date_val = parts[4].strip()
                body_val = parts[5].strip() if len(parts) > 5 else ""

                per_bullets = _extract_commit_bullets(subj_val, body_val)
                commits.append(
                    {
                        "version": ver_map.get(sha_val, sha_val),
                        "sha": sha_val,
                        "subject": subj_val,
                        "summary_bullets": per_bullets,
                        "author": auth_val,
                        "date": date_val,
                        "commit_date": commit_date_val,
                    }
                )
                for b in per_bullets:
                    if b and b not in summary_bullets:
                        summary_bullets.append(b)

    trigger_file = REPO_MOUNT_DIR / ".trigger-update"
    status_file = REPO_MOUNT_DIR / ".update-status.json"
    is_updating = trigger_file.exists() or LOCK_FILE.exists()

    payload = {
        "update_available": commits_behind > 0,
        "current_sha": local_sha,
        "remote_sha": remote_sha,
        "current_version": current_version,
        "remote_version": remote_version,
        "commits_behind": commits_behind,
        "commits": commits,
        "updates": commits,
        "summary_bullets": summary_bullets[:25],
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

    git_state = await _inspect_git_update(do_fetch=False)
    from_sha = str(git_state.get("current_sha") or "unknown")
    target_sha = str(git_state.get("remote_sha") or from_sha)
    from_version = str(git_state.get("current_version") or from_sha)
    target_version = str(git_state.get("remote_version") or target_sha)
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    initial_progress: Dict[str, Any] = {
        "status": "updating",
        "update_id": f"{int(now_dt.timestamp())}-{target_sha}",
        "overall_pct": 1,
        "phase": "queued",
        "message": f"Queued update ({from_version} -> {target_version})...",
        "from_sha": from_sha,
        "target_sha": target_sha,
        "from_version": from_version,
        "target_version": target_version,
        "started_at": now_iso,
        "updated_at": now_iso,
        "components": [
            {
                "id": "git",
                "name": "Source Code (GitHub)",
                "status": "running",
                "pct": 10,
                "detail": "Queued host updater...",
            },
            {
                "id": "frontend",
                "name": "Web Frontend (React + Nginx)",
                "status": "pending",
                "pct": 0,
                "detail": "Waiting for source update...",
            },
            {
                "id": "api",
                "name": "Backend API (FastAPI)",
                "status": "pending",
                "pct": 0,
                "detail": "Waiting for frontend...",
            },
            {
                "id": "worker",
                "name": "Telemetry Worker (SNMP / Nmap)",
                "status": "pending",
                "pct": 0,
                "detail": "Waiting for backend build...",
            },
            {
                "id": "services",
                "name": "Core Services & Health Check",
                "status": "pending",
                "pct": 0,
                "detail": "Waiting for services...",
            },
        ],
    }
    _write_update_progress(initial_progress)

    trigger_file = REPO_MOUNT_DIR / ".trigger-update"
    trigger_file.write_text(now_iso, encoding="utf-8")
    return {
        "status": "updating",
        "progress": initial_progress,
        "message": "Update installation triggered on host.",
    }


@app.get("/api/system/update/progress")
async def get_update_progress() -> Dict[str, Any]:
    return await _reconcile_update_progress()


@app.post("/api/system/update/dismiss")
async def dismiss_update_progress() -> Dict[str, Any]:
    idle = _default_idle_progress()
    _write_update_progress(idle)
    return idle


def _format_bytes_human(num: Optional[int]) -> str:
    val = float(num or 0)
    if val <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{int(val)} B"


def _read_os_pretty_name() -> str:
    try:
        os_release = Path("/etc/os-release")
        if os_release.exists():
            for line in os_release.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return f"{platform.system()} {platform.release()}"


def _read_memory_info() -> Dict[str, Any]:
    try:
        meminfo_path = Path("/proc/meminfo")
        if not meminfo_path.exists():
            return {"total_bytes": None, "used_bytes": None, "available_bytes": None, "usage_pct": None}
        values: Dict[str, int] = {}
        for line in meminfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if ":" in line:
                key, rest = line.split(":", 1)
                parts = rest.strip().split()
                if parts and parts[0].isdigit():
                    values[key.strip()] = int(parts[0]) * 1024
        total = values.get("MemTotal")
        avail = values.get("MemAvailable", values.get("MemFree"))
        if total and avail is not None:
            used = max(0, total - avail)
            pct = round((used / total) * 100.0, 1) if total > 0 else 0.0
            return {
                "total_bytes": total,
                "used_bytes": used,
                "available_bytes": avail,
                "usage_pct": pct,
            }
    except Exception:
        pass
    return {"total_bytes": None, "used_bytes": None, "available_bytes": None, "usage_pct": None}


def _read_disk_info() -> Dict[str, Any]:
    try:
        target = str(REPO_MOUNT_DIR) if REPO_MOUNT_DIR.exists() else "/"
        usage = shutil.disk_usage(target)
        pct = round((usage.used / usage.total) * 100.0, 1) if usage.total > 0 else 0.0
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "usage_pct": pct,
        }
    except Exception:
        return {"total_bytes": None, "used_bytes": None, "free_bytes": None, "usage_pct": None}


def _read_compose_image_tag(service_name: str, default_tag: str) -> str:
    try:
        compose_path = REPO_MOUNT_DIR / "docker-compose.yml"
        if not compose_path.exists():
            return default_tag
        in_service = False
        service_indent: Optional[int] = None
        for raw_line in compose_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if stripped == f"{service_name}:":
                in_service = True
                service_indent = indent
                continue
            if in_service:
                if service_indent is not None and indent <= service_indent:
                    break
                if stripped.startswith("image:"):
                    img = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                    if ":" in img:
                        return img.rsplit(":", 1)[1]
                    return img
    except Exception:
        pass
    return default_tag


def _read_frontend_pkg_versions() -> Dict[str, str]:
    result = {"react": "19.x", "vite": "6.x"}
    try:
        pkg_path = REPO_MOUNT_DIR / "frontend" / "package.json"
        if pkg_path.exists():
            data = json.loads(pkg_path.read_text(encoding="utf-8", errors="ignore"))
            deps = data.get("dependencies", {})
            dev_deps = data.get("devDependencies", {})
            if isinstance(deps.get("react"), str):
                result["react"] = deps["react"].lstrip("^~")
            if isinstance(dev_deps.get("vite"), str):
                result["vite"] = dev_deps["vite"].lstrip("^~")
    except Exception:
        pass
    return result


async def _run_git_info() -> Dict[str, Any]:
    if not (REPO_MOUNT_DIR / ".git").exists():
        return {
            "current_sha": "local",
            "current_version": "local",
            "branch": "local",
            "last_commit_subject": "Running without .git metadata",
            "last_commit_date": None,
            "remote_url": "",
        }

    async def _run_git(*args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(REPO_MOUNT_DIR),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return out.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""

    sha, branch, subj, dt_str, remote, ver_map = await asyncio.gather(
        _run_git("rev-parse", "--short=7", "HEAD"),
        _run_git("rev-parse", "--abbrev-ref", "HEAD"),
        _run_git("log", "-1", "--pretty=format:%s"),
        _run_git("log", "-1", "--pretty=format:%cI"),
        _run_git("remote", "get-url", "origin"),
        _compute_git_version_map("HEAD"),
    )
    sha_clean = sha or "unknown"
    return {
        "current_sha": sha_clean,
        "current_version": ver_map.get(sha_clean, sha_clean),
        "branch": branch or "main",
        "last_commit_subject": subj or "—",
        "last_commit_date": dt_str or None,
        "remote_url": remote or "",
    }


@app.get("/api/system/info")
async def get_system_info() -> Dict[str, Any]:
    """Returns server host information and live status/versions of all background services."""
    now_utc = datetime.now(timezone.utc)
    api_uptime_sec = max(0, int((now_utc - API_START_TIME).total_seconds()))
    mib_count = len(list_backend_mibs())

    try:
        load_avg: Optional[List[float]] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        load_avg = None

    web_port = int(os.getenv("WEB_PORT", "8080") or 8080)
    syslog_port = int(os.getenv("SYSLOG_PORT", "514") or 514)

    async def _probe_timescaledb() -> Dict[str, Any]:
        t0 = time.perf_counter()
        if db_pool is None:
            return {
                "id": "timescaledb",
                "name": "TimescaleDB / PostgreSQL",
                "container": "netdash-timescaledb",
                "status": "stopped",
                "version": "PostgreSQL 16 • TimescaleDB",
                "uptime_sec": None,
                "latency_ms": None,
                "detail": "Database connection pool not initialized",
                "db_size_bytes": None,
            }
        try:
            async with db_pool.acquire() as conn:
                row = await asyncio.wait_for(
                    conn.fetchrow(
                        """
                        SELECT
                            current_setting('server_version') AS pg_ver,
                            (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') AS ts_ver,
                            pg_database_size(current_database()) AS db_size,
                            EXTRACT(EPOCH FROM (NOW() - pg_postmaster_start_time()))::bigint AS uptime_sec,
                            (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) AS connections
                        """
                    ),
                    timeout=2.0,
                )
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            pg_ver = str(row["pg_ver"] or "16").split(" ")[0]
            ts_ver = str(row["ts_ver"]) if row["ts_ver"] else "N/A"
            db_size = int(row["db_size"] or 0)
            uptime_sec = int(row["uptime_sec"] or 0)
            conns = int(row["connections"] or 0)
            return {
                "id": "timescaledb",
                "name": "TimescaleDB / PostgreSQL",
                "container": "netdash-timescaledb",
                "status": "running",
                "version": f"PostgreSQL {pg_ver} • TimescaleDB {ts_ver}",
                "uptime_sec": uptime_sec,
                "latency_ms": latency_ms,
                "detail": f"{conns} active conn • DB size {_format_bytes_human(db_size)}",
                "db_size_bytes": db_size,
            }
        except Exception as exc:
            return {
                "id": "timescaledb",
                "name": "TimescaleDB / PostgreSQL",
                "container": "netdash-timescaledb",
                "status": "stopped",
                "version": "PostgreSQL 16 • TimescaleDB",
                "uptime_sec": None,
                "latency_ms": None,
                "detail": f"Probe failed: {str(exc)[:100]}",
                "db_size_bytes": None,
            }

    async def _probe_redis() -> Dict[str, Any]:
        t0 = time.perf_counter()
        if redis_client is None:
            return {
                "id": "redis",
                "name": "Redis Cache & Pub/Sub",
                "container": "netdash-redis",
                "status": "stopped",
                "version": "Redis 7.x",
                "uptime_sec": None,
                "latency_ms": None,
                "detail": "Redis client not initialized",
            }
        try:
            srv, mem, clients = await asyncio.wait_for(
                asyncio.gather(
                    redis_client.info("server"),
                    redis_client.info("memory"),
                    redis_client.info("clients"),
                ),
                timeout=2.0,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            redis_ver = srv.get("redis_version", "7.x")
            uptime_sec = int(srv.get("uptime_in_seconds", 0) or 0)
            mem_human = mem.get("used_memory_human", "?")
            conn_clients = clients.get("connected_clients", 0)
            return {
                "id": "redis",
                "name": "Redis Cache & Pub/Sub",
                "container": "netdash-redis",
                "status": "running",
                "version": f"Redis {redis_ver}",
                "uptime_sec": uptime_sec,
                "latency_ms": latency_ms,
                "detail": f"Mem {mem_human} • {conn_clients} connected client(s)",
            }
        except Exception as exc:
            return {
                "id": "redis",
                "name": "Redis Cache & Pub/Sub",
                "container": "netdash-redis",
                "status": "stopped",
                "version": "Redis 7.x",
                "uptime_sec": None,
                "latency_ms": None,
                "detail": f"Probe failed: {str(exc)[:100]}",
            }

    async def _probe_worker() -> Dict[str, Any]:
        default_py = platform.python_version()
        default_pysnmp = _pkg_version("pysnmp")
        if redis_client is None:
            return {
                "id": "worker",
                "name": "Background Telemetry Worker",
                "container": "netdash-worker",
                "status": "stopped",
                "version": f"Python {default_py} • PySNMP {default_pysnmp}",
                "uptime_sec": None,
                "latency_ms": None,
                "detail": "Redis unavailable to check worker heartbeat",
                "sub_services": [],
            }
        try:
            t0 = time.perf_counter()
            raw = await asyncio.wait_for(redis_client.get("netdash:worker:heartbeat"), timeout=1.5)
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            if not raw:
                return {
                    "id": "worker",
                    "name": "Background Telemetry Worker",
                    "container": "netdash-worker",
                    "status": "stopped",
                    "version": f"Python {default_py} • PySNMP {default_pysnmp}",
                    "uptime_sec": None,
                    "latency_ms": latency_ms,
                    "detail": "No active worker heartbeat in Redis",
                    "sub_services": [],
                }
            hb = json.loads(raw)
            py_v = hb.get("python_version") or default_py
            snmp_v = hb.get("pysnmp_version") or default_pysnmp
            nmap_v = hb.get("nmap_version") or "unknown"
            uptime_sec: Optional[int] = None
            if hb.get("started_at"):
                try:
                    started_dt = datetime.fromisoformat(hb["started_at"])
                    uptime_sec = max(0, int((now_utc - started_dt).total_seconds()))
                except Exception:
                    uptime_sec = None

            loops = hb.get("loops") or {}
            loop_labels = [
                ("live_traffic_streamer", "Live Traffic Streamer"),
                ("snmp_poller", "SNMP Poller Loop"),
                ("subnet_scanner", "Nmap Subnet Scanner Loop"),
                ("unifi_collector", "UniFi Collector Loop"),
            ]
            sub_services: List[Dict[str, Any]] = []
            for key, label in loop_labels:
                linfo = loops.get(key) or {}
                sub_services.append(
                    {
                        "key": key,
                        "name": label,
                        "status": linfo.get("status", "unknown"),
                        "interval_sec": linfo.get("interval_sec"),
                        "last_run": linfo.get("last_run"),
                        "detail": linfo.get("detail") or "",
                    }
                )

            return {
                "id": "worker",
                "name": "Background Telemetry Worker",
                "container": "netdash-worker",
                "status": "running",
                "version": f"Python {py_v} • PySNMP {snmp_v} • Nmap {nmap_v}",
                "uptime_sec": uptime_sec,
                "latency_ms": latency_ms,
                "detail": f"Heartbeat active • asyncpg {hb.get('asyncpg_version', _pkg_version('asyncpg'))}",
                "sub_services": sub_services,
            }
        except Exception as exc:
            return {
                "id": "worker",
                "name": "Background Telemetry Worker",
                "container": "netdash-worker",
                "status": "stopped",
                "version": f"Python {default_py} • PySNMP {default_pysnmp}",
                "uptime_sec": None,
                "latency_ms": None,
                "detail": f"Heartbeat check failed: {str(exc)[:100]}",
                "sub_services": [],
            }

    async def _probe_vector() -> Dict[str, Any]:
        raw_tag = _read_compose_image_tag("vector", "0.42.0-alpine")
        clean_tag = raw_tag.replace("-alpine", "")
        logs_24h = 0
        if db_pool is not None:
            try:
                async with db_pool.acquire() as conn:
                    cnt = await asyncio.wait_for(
                        conn.fetchval(
                            "SELECT COUNT(*) FROM device_logs WHERE time > NOW() - INTERVAL '24 hours'"
                        ),
                        timeout=1.5,
                    )
                    logs_24h = int(cnt or 0)
            except Exception:
                pass
        try:
            t0 = time.perf_counter()
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("vector", 514),
                timeout=1.5,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            return {
                "id": "vector",
                "name": "Vector Syslog Receiver",
                "container": "netdash-vector",
                "status": "running",
                "version": f"Vector {clean_tag}",
                "uptime_sec": None,
                "latency_ms": latency_ms,
                "detail": f"UDP/TCP :{syslog_port} listening • {logs_24h} logs ingested (24h)",
            }
        except Exception:
            return {
                "id": "vector",
                "name": "Vector Syslog Receiver",
                "container": "netdash-vector",
                "status": "stopped",
                "version": f"Vector {clean_tag}",
                "uptime_sec": None,
                "latency_ms": None,
                "detail": f"Port 514 unreachable • {logs_24h} logs stored (24h)",
            }

    async def _probe_frontend() -> Dict[str, Any]:
        fe_pkgs = _read_frontend_pkg_versions()
        react_ver = fe_pkgs["react"]
        vite_ver = fe_pkgs["vite"]
        try:
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.head("http://frontend:80/")
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            nginx_hdr = resp.headers.get("server", "nginx/1.27")
            svc_status = "running" if resp.status_code < 500 else "degraded"
            return {
                "id": "frontend",
                "name": "Nginx Web Frontend",
                "container": "netdash-frontend",
                "status": svc_status,
                "version": f"{nginx_hdr} • React {react_ver} • Vite {vite_ver}",
                "uptime_sec": None,
                "latency_ms": latency_ms,
                "detail": f"SPA & Reverse Proxy active on host port :{web_port}",
            }
        except Exception:
            return {
                "id": "frontend",
                "name": "Nginx Web Frontend",
                "container": "netdash-frontend",
                "status": "stopped",
                "version": f"nginx/1.27 • React {react_ver} • Vite {vite_ver}",
                "uptime_sec": None,
                "latency_ms": None,
                "detail": "Frontend HTTP check unreachable on internal network",
            }

    async def _probe_proxy() -> Optional[Dict[str, Any]]:
        try:
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=1.5) as client:
                resp = await client.head("http://proxy:80/_updating.html")
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            nginx_hdr = resp.headers.get("server", "nginx/1.27")
            return {
                "id": "proxy",
                "name": "Web Edge Proxy",
                "container": "netdash-proxy",
                "status": "running" if resp.status_code < 500 else "degraded",
                "version": nginx_hdr,
                "uptime_sec": None,
                "latency_ms": latency_ms,
                "detail": f"Always-on entrypoint & updating fallback on :{web_port}",
            }
        except Exception:
            return None

    git_info, ts_svc, redis_svc, worker_svc, vector_svc, frontend_svc, proxy_svc = (
        await asyncio.gather(
            _run_git_info(),
            _probe_timescaledb(),
            _probe_redis(),
            _probe_worker(),
            _probe_vector(),
            _probe_frontend(),
            _probe_proxy(),
        )
    )

    api_svc: Dict[str, Any] = {
        "id": "api",
        "name": "NetDash API Server",
        "container": "netdash-api",
        "status": "running",
        "version": (
            f"FastAPI {_pkg_version('fastapi')} • "
            f"Uvicorn {_pkg_version('uvicorn')} • "
            f"Python {platform.python_version()}"
        ),
        "uptime_sec": api_uptime_sec,
        "latency_ms": 0.1,
        "detail": f"API v{app.version} • {mib_count} backend MIB files loaded",
    }

    db_size_bytes = ts_svc.pop("db_size_bytes", None)
    services = [api_svc, worker_svc, ts_svc, redis_svc, vector_svc, frontend_svc]
    if proxy_svc is not None:
        services.append(proxy_svc)

    return {
        "server": {
            "hostname": socket.gethostname(),
            "os_name": _read_os_pretty_name(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "cpu_cores": os.cpu_count() or 1,
            "load_avg": load_avg,
            "memory": _read_memory_info(),
            "disk": _read_disk_info(),
            "db_size_bytes": db_size_bytes,
            "api_uptime_sec": api_uptime_sec,
            "server_time_utc": now_utc.isoformat(),
            "git": git_info,
            "config": {
                "web_port": web_port,
                "syslog_port": syslog_port,
                "snmp_poll_interval_sec": max(15, int(os.getenv("SNMP_POLL_INTERVAL", "60") or 60)),
                "subnet_scan_minutes": max(1, int(os.getenv("SUBNET_SCAN_MINUTES", "15") or 15)),
                "snmp_raw_retention_days": int(os.getenv("SNMP_RAW_RETENTION_DAYS", "180") or 180),
                "log_retention_days": int(os.getenv("LOG_RETENTION_DAYS", "30") or 30),
                "loaded_mib_files": mib_count,
            },
        },
        "services": services,
    }


# ==============================================================================
# Dynamic Configuration & First-Time Onboarding Endpoints
# ==============================================================================


@app.get("/api/config")
async def get_all_config() -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        ob_val = await conn.fetchval("SELECT value FROM app_settings WHERE key = 'onboarding'")
        onboarding = json.loads(ob_val) if isinstance(ob_val, str) else (ob_val or {"completed": False, "skipped": False})

        profiles = await conn.fetch(
            """
            SELECT id, name, version, port, community, v3_user, v3_auth_proto,
                   v3_auth_key, v3_priv_proto, v3_priv_key, poll_interval
            FROM snmp_profiles
            ORDER BY id ASC
            """
        )

        targets = await conn.fetch(
            """
            SELECT t.id, t.device_ip, t.label, t.profile_id, t.enabled,
                   p.name AS profile_name, p.version AS profile_version
            FROM snmp_targets_config t
            LEFT JOIN snmp_profiles p ON p.id = t.profile_id
            ORDER BY t.id ASC
            """
        )

        subnets = await conn.fetch(
            """
            SELECT id, subnet_cidr, label, vlan_id, enabled
            FROM scan_subnets_config
            ORDER BY id ASC
            """
        )

        unifi_ctrls = await conn.fetch(
            """
            SELECT id, name, host, api_key, site_name, verify_ssl, poll_interval, enabled
            FROM unifi_controllers_config
            ORDER BY id ASC
            """
        )

    return {
        "onboarding": onboarding,
        "snmp_profiles": [dict(r) for r in profiles],
        "snmp_targets": [dict(r) for r in targets],
        "subnets": [dict(r) for r in subnets],
        "unifi_controllers": [dict(r) for r in unifi_ctrls],
    }


@app.post("/api/config/onboarding")
async def complete_onboarding(payload: OnboardingPayload) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        default_profile_id = await conn.fetchval(
            "SELECT id FROM snmp_profiles ORDER BY id ASC LIMIT 1"
        )

        for p in payload.snmp_profiles:
            if not p.name.strip():
                continue
            default_profile_id = await conn.fetchval(
                """
                INSERT INTO snmp_profiles (
                    name, version, port, community, v3_user, v3_auth_proto,
                    v3_auth_key, v3_priv_proto, v3_priv_key, poll_interval
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (name) DO UPDATE SET
                    version = EXCLUDED.version,
                    port = EXCLUDED.port,
                    community = EXCLUDED.community,
                    v3_user = EXCLUDED.v3_user,
                    v3_auth_proto = EXCLUDED.v3_auth_proto,
                    v3_auth_key = EXCLUDED.v3_auth_key,
                    v3_priv_proto = EXCLUDED.v3_priv_proto,
                    v3_priv_key = EXCLUDED.v3_priv_key,
                    poll_interval = EXCLUDED.poll_interval
                RETURNING id
                """,
                p.name.strip(),
                p.version.strip().lower(),
                p.port,
                p.community.strip(),
                p.v3_user.strip(),
                p.v3_auth_proto.strip().upper(),
                p.v3_auth_key.strip(),
                p.v3_priv_proto.strip().upper(),
                p.v3_priv_key.strip(),
                p.poll_interval,
            )

        for t in payload.snmp_targets:
            ip_val = t.device_ip.strip()
            if not ip_val:
                continue
            await conn.execute(
                """
                INSERT INTO snmp_targets_config (device_ip, label, profile_id, enabled)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (device_ip) DO UPDATE SET
                    label = EXCLUDED.label,
                    profile_id = COALESCE(EXCLUDED.profile_id, snmp_targets_config.profile_id),
                    enabled = EXCLUDED.enabled
                """,
                ip_val,
                t.label.strip(),
                t.profile_id or default_profile_id,
                t.enabled,
            )

        for s in payload.subnets:
            raw_cidr = s.subnet_cidr.strip()
            if not raw_cidr:
                continue
            try:
                norm_cidr = str(ipaddress.ip_network(raw_cidr, strict=False))
            except ValueError:
                continue
            await conn.execute(
                """
                INSERT INTO scan_subnets_config (subnet_cidr, label, vlan_id, enabled)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (subnet_cidr) DO UPDATE SET
                    label = EXCLUDED.label,
                    vlan_id = EXCLUDED.vlan_id,
                    enabled = EXCLUDED.enabled
                """,
                norm_cidr,
                s.label.strip(),
                s.vlan_id,
                s.enabled,
            )

        for u in payload.unifi_controllers:
            if not u.host.strip():
                continue
            await conn.execute(
                """
                INSERT INTO unifi_controllers_config (
                    name, host, api_key, site_name, verify_ssl, poll_interval, enabled
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (host) DO UPDATE SET
                    name = EXCLUDED.name,
                    api_key = EXCLUDED.api_key,
                    site_name = EXCLUDED.site_name,
                    verify_ssl = EXCLUDED.verify_ssl,
                    poll_interval = EXCLUDED.poll_interval,
                    enabled = EXCLUDED.enabled
                """,
                u.name.strip() or "UDM Pro",
                u.host.strip(),
                u.api_key.strip(),
                u.site_name.strip() or "default",
                u.verify_ssl,
                u.poll_interval,
                u.enabled,
            )

        await conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES ('onboarding', $1::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """,
            json.dumps({"completed": True, "skipped": payload.skipped}),
        )

    await _notify_worker("reload_all")
    return await get_all_config()


@app.post("/api/config/snmp-profiles")
async def create_snmp_profile(payload: SNMPProfilePayload) -> Dict[str, Any]:
    pool = _get_pool()
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Profile name is required.")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO snmp_profiles (
                name, version, port, community, v3_user, v3_auth_proto,
                v3_auth_key, v3_priv_proto, v3_priv_key, poll_interval
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (name) DO UPDATE SET
                version = EXCLUDED.version,
                port = EXCLUDED.port,
                community = EXCLUDED.community,
                v3_user = EXCLUDED.v3_user,
                v3_auth_proto = EXCLUDED.v3_auth_proto,
                v3_auth_key = EXCLUDED.v3_auth_key,
                v3_priv_proto = EXCLUDED.v3_priv_proto,
                v3_priv_key = EXCLUDED.v3_priv_key,
                poll_interval = EXCLUDED.poll_interval
            """,
            name,
            payload.version.strip().lower(),
            payload.port,
            payload.community.strip(),
            payload.v3_user.strip(),
            payload.v3_auth_proto.strip().upper(),
            payload.v3_auth_key.strip(),
            payload.v3_priv_proto.strip().upper(),
            payload.v3_priv_key.strip(),
            payload.poll_interval,
        )
    await _notify_worker("snmp_now")
    return await get_all_config()


@app.put("/api/config/snmp-profiles/{profile_id}")
async def update_snmp_profile(profile_id: int, payload: SNMPProfilePayload) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE snmp_profiles
            SET name = $1, version = $2, port = $3, community = $4,
                v3_user = $5, v3_auth_proto = $6, v3_auth_key = $7,
                v3_priv_proto = $8, v3_priv_key = $9, poll_interval = $10
            WHERE id = $11
            """,
            payload.name.strip(),
            payload.version.strip().lower(),
            payload.port,
            payload.community.strip(),
            payload.v3_user.strip(),
            payload.v3_auth_proto.strip().upper(),
            payload.v3_auth_key.strip(),
            payload.v3_priv_proto.strip().upper(),
            payload.v3_priv_key.strip(),
            payload.poll_interval,
            profile_id,
        )
    await _notify_worker("snmp_now")
    return await get_all_config()


@app.delete("/api/config/snmp-profiles/{profile_id}")
async def delete_snmp_profile(profile_id: int) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM snmp_profiles WHERE id = $1", profile_id)
    await _notify_worker("snmp_now")
    return await get_all_config()


@app.post("/api/config/snmp-targets")
async def create_snmp_target(payload: SNMPTargetPayload) -> Dict[str, Any]:
    pool = _get_pool()
    ip_str = payload.device_ip.strip()
    if not ip_str:
        raise HTTPException(status_code=400, detail="Host IP or hostname is required.")
    label_str = payload.label.strip()
    async with pool.acquire() as conn:
        valid_profile_id = None
        if payload.profile_id:
            valid_profile_id = await conn.fetchval(
                "SELECT id FROM snmp_profiles WHERE id = $1", payload.profile_id
            )
        if not valid_profile_id:
            valid_profile_id = await conn.fetchval(
                "SELECT id FROM snmp_profiles ORDER BY id ASC LIMIT 1"
            )

        await conn.execute(
            """
            INSERT INTO snmp_targets_config (device_ip, label, profile_id, enabled)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (device_ip) DO UPDATE SET
                label = EXCLUDED.label,
                profile_id = EXCLUDED.profile_id,
                enabled = EXCLUDED.enabled
            """,
            ip_str,
            label_str,
            valid_profile_id,
            payload.enabled,
        )

        # Immediately seed snmp_devices so the host is visible right away before first 60s poll
        await conn.execute(
            """
            INSERT INTO snmp_devices (device_ip, sys_name, is_reachable, last_polled)
            VALUES ($1, $2, TRUE, NOW())
            ON CONFLICT (device_ip) DO UPDATE SET
                sys_name = COALESCE(NULLIF($2, ''), snmp_devices.sys_name)
            """,
            ip_str,
            label_str or ip_str,
        )
    await _notify_worker("snmp_now")
    return await get_all_config()


@app.put("/api/config/snmp-targets/{target_id}")
async def update_snmp_target(target_id: int, payload: SNMPTargetPayload) -> Dict[str, Any]:
    pool = _get_pool()
    ip_str = payload.device_ip.strip()
    if not ip_str:
        raise HTTPException(status_code=400, detail="Host IP or hostname is required.")
    async with pool.acquire() as conn:
        valid_profile_id = None
        if payload.profile_id:
            valid_profile_id = await conn.fetchval(
                "SELECT id FROM snmp_profiles WHERE id = $1", payload.profile_id
            )
        if not valid_profile_id:
            valid_profile_id = await conn.fetchval(
                "SELECT id FROM snmp_profiles ORDER BY id ASC LIMIT 1"
            )
        await conn.execute(
            """
            UPDATE snmp_targets_config
            SET device_ip = $1, label = $2, profile_id = $3, enabled = $4
            WHERE id = $5
            """,
            ip_str,
            payload.label.strip(),
            valid_profile_id,
            payload.enabled,
            target_id,
        )
    await _notify_worker("snmp_now")
    return await get_all_config()


@app.delete("/api/config/snmp-targets/{target_id}")
async def delete_snmp_target(target_id: int) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        dev_ip = await conn.fetchval(
            "DELETE FROM snmp_targets_config WHERE id = $1 RETURNING device_ip",
            target_id,
        )
        if dev_ip:
            await conn.execute("DELETE FROM snmp_devices WHERE device_ip = $1", dev_ip)
    return await get_all_config()


@app.post("/api/config/subnets")
async def create_subnet_config(payload: SubnetConfigPayload) -> Dict[str, Any]:
    raw_cidr = payload.subnet_cidr.strip()
    try:
        net = ipaddress.ip_network(raw_cidr, strict=False)
        if net.version != 4:
            raise ValueError("Only IPv4 CIDRs supported")
        norm_cidr = str(net)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid IPv4 CIDR: {raw_cidr}")

    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scan_subnets_config (subnet_cidr, label, vlan_id, enabled)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (subnet_cidr) DO UPDATE SET
                label = EXCLUDED.label,
                vlan_id = EXCLUDED.vlan_id,
                enabled = EXCLUDED.enabled
            """,
            norm_cidr,
            payload.label.strip(),
            payload.vlan_id,
            payload.enabled,
        )
    await _notify_worker("scan_now")
    return await get_all_config()


@app.delete("/api/config/subnets/{subnet_id}")
async def delete_subnet_config(subnet_id: int) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        cidr = await conn.fetchval(
            "DELETE FROM scan_subnets_config WHERE id = $1 RETURNING subnet_cidr",
            subnet_id,
        )
        if cidr:
            await conn.execute("DELETE FROM discovered_hosts WHERE subnet_cidr = $1", cidr)
    return await get_all_config()


@app.post("/api/config/unifi")
async def upsert_unifi_controller(payload: UniFiControllerPayload) -> Dict[str, Any]:
    host = payload.host.strip()
    if not host:
        raise HTTPException(status_code=400, detail="UniFi controller host/IP is required.")
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO unifi_controllers_config (
                name, host, api_key, site_name, verify_ssl, poll_interval, enabled
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (host) DO UPDATE SET
                name = EXCLUDED.name,
                api_key = CASE WHEN EXCLUDED.api_key != '' THEN EXCLUDED.api_key ELSE unifi_controllers_config.api_key END,
                site_name = EXCLUDED.site_name,
                verify_ssl = EXCLUDED.verify_ssl,
                poll_interval = EXCLUDED.poll_interval,
                enabled = EXCLUDED.enabled
            """,
            payload.name.strip() or "UDM Pro",
            host,
            payload.api_key.strip(),
            payload.site_name.strip() or "default",
            payload.verify_ssl,
            payload.poll_interval,
            payload.enabled,
        )
    await _notify_worker("unifi_now")
    return await get_all_config()


@app.delete("/api/config/unifi/{ctrl_id}")
async def delete_unifi_controller(ctrl_id: int) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM unifi_controllers_config WHERE id = $1", ctrl_id)
    return await get_all_config()


# ==============================================================================
# Observability Telemetry Query Endpoints
# ==============================================================================


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

        # Fallback: when UniFi gateway is not configured, aggregate SNMP traffic across all polled interfaces
        if not wan_history:
            wan_history = await conn.fetch(
                """
                SELECT bucket,
                       SUM(if_rx) AS rx_bps,
                       SUM(if_tx) AS tx_bps,
                       0.0 AS latency_ms
                FROM (
                    SELECT time_bucket('1 minute', time) AS bucket,
                           device_ip,
                           if_index,
                           AVG(in_bps) AS if_rx,
                           AVG(out_bps) AS if_tx
                    FROM snmp_metrics_raw
                    WHERE time > NOW() - INTERVAL '1 hour'
                      AND COALESCE(LOWER(if_name), '') NOT IN ('lo', 'loopback', 'null0')
                    GROUP BY bucket, device_ip, if_index
                ) sub
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
        "live_series": await _read_live_traffic_series(120),
        "unifi_devices": devices_list,
        "snmp_devices": snmp_list,
        "scanner_summary": dict(host_counts) if host_counts else {"online_hosts": 0, "total_discovered": 0},
        "syslog_summary": dict(log_counts) if log_counts else {"total_24h": 0, "errors_24h": 0, "warnings_24h": 0},
    }


@app.get("/api/overview/live")
async def get_overview_live_series(
    limit: int = Query(default=120, ge=10, le=180),
) -> Dict[str, Any]:
    return {"live_series": await _read_live_traffic_series(limit)}


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
    if_index: int = Query(0),
    range_key: str = Query("24h", alias="range"),
) -> Dict[str, Any]:
    """
    When if_index == 0, aggregates traffic across ALL interfaces on the device.
    When if_index > 0, returns traffic for that specific interface.
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
        if if_index <= 0:
            # All Ports / Interfaces Combined Traffic Overview for this device
            if rk in ("1h", "6h", "24h"):
                sql_interval = interval_map[rk]
                rows = await conn.fetch(
                    f"""
                    SELECT bucket,
                           SUM(if_avg_in) AS avg_in_bps,
                           SUM(if_max_in) AS max_in_bps,
                           SUM(if_max_in) AS p95_in_bps,
                           SUM(if_avg_out) AS avg_out_bps,
                           SUM(if_max_out) AS max_out_bps,
                           SUM(if_max_out) AS p95_out_bps,
                           AVG(if_cpu) AS avg_cpu_usage
                    FROM (
                        SELECT time_bucket('1 minute', time) AS bucket,
                               if_index,
                               AVG(in_bps) AS if_avg_in,
                               MAX(in_bps) AS if_max_in,
                               AVG(out_bps) AS if_avg_out,
                               MAX(out_bps) AS if_max_out,
                               AVG(cpu_usage) AS if_cpu
                        FROM snmp_metrics_raw
                        WHERE device_ip = $1
                          AND time > NOW() - INTERVAL '{sql_interval}'
                          AND COALESCE(LOWER(if_name), '') NOT IN ('lo', 'loopback', 'null0')
                        GROUP BY bucket, if_index
                    ) sub
                    GROUP BY bucket
                    ORDER BY bucket ASC
                    """,
                    device_ip,
                )
                source_table = "snmp_metrics_raw (All Interfaces Combined)"
            elif rk in ("7d", "30d"):
                sql_interval = interval_map[rk]
                rows = await conn.fetch(
                    f"""
                    SELECT bucket,
                           SUM(avg_in_bps) AS avg_in_bps,
                           SUM(max_in_bps) AS max_in_bps,
                           SUM(p95_in_bps) AS p95_in_bps,
                           SUM(avg_out_bps) AS avg_out_bps,
                           SUM(max_out_bps) AS max_out_bps,
                           SUM(p95_out_bps) AS p95_out_bps,
                           AVG(avg_cpu_usage) AS avg_cpu_usage
                    FROM snmp_metrics_hourly
                    WHERE device_ip = $1
                      AND bucket > NOW() - INTERVAL '{sql_interval}'
                      AND COALESCE(LOWER(if_name), '') NOT IN ('lo', 'loopback', 'null0')
                    GROUP BY bucket
                    ORDER BY bucket ASC
                    """,
                    device_ip,
                )
                source_table = "snmp_metrics_hourly (All Interfaces Combined)"
            else:
                where_time = "AND bucket > NOW() - INTERVAL '365 days'" if rk == "1y" else ""
                rows = await conn.fetch(
                    f"""
                    SELECT bucket,
                           SUM(avg_in_bps) AS avg_in_bps,
                           SUM(max_in_bps) AS max_in_bps,
                           SUM(p95_in_bps) AS p95_in_bps,
                           SUM(avg_out_bps) AS avg_out_bps,
                           SUM(max_out_bps) AS max_out_bps,
                           SUM(p95_out_bps) AS p95_out_bps,
                           AVG(avg_cpu_usage) AS avg_cpu_usage
                    FROM snmp_metrics_daily
                    WHERE device_ip = $1
                      {where_time}
                      AND COALESCE(LOWER(if_name), '') NOT IN ('lo', 'loopback', 'null0')
                    GROUP BY bucket
                    ORDER BY bucket ASC
                    """,
                    device_ip,
                )
                source_table = "snmp_metrics_daily (All Interfaces Combined)"
        else:
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
    async with pool.acquire() as conn:
        sub_rows = await conn.fetch(
            "SELECT subnet_cidr FROM scan_subnets_config WHERE enabled = TRUE ORDER BY id ASC"
        )
        configured_subnets = [str(r["subnet_cidr"]) for r in sub_rows]

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
    await _notify_worker("scan_now")
    return {"status": "triggered", "message": "Immediate Nmap subnet sweep queued."}


# ==============================================================================
# Unified Devices Inventory & Per-Device Detailed Telemetry Page Endpoints
# ==============================================================================


@app.get("/api/devices")
async def get_all_devices() -> Dict[str, Any]:
    """Combines all discovered Nmap/ARP hosts, SNMP targets/devices, and UniFi devices/clients."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        disc_rows = await conn.fetch(
            """
            SELECT host(ip_address) AS ip_address, subnet_cidr, mac_address,
                   hostname, vendor, vlan_id, is_online, discovery_source,
                   switch_mac, switch_port, first_seen, last_seen
            FROM discovered_hosts
            ORDER BY ip_address ASC
            """
        )

        snmp_cfg_rows = await conn.fetch(
            """
            SELECT t.id AS target_id, t.device_ip, t.label, t.profile_id, t.enabled,
                   p.name AS profile_name, p.version AS profile_version
            FROM snmp_targets_config t
            LEFT JOIN snmp_profiles p ON p.id = t.profile_id
            """
        )

        snmp_dev_rows = await conn.fetch(
            """
            SELECT device_ip, sys_name, sys_descr, sys_object_id, sys_location, sys_contact,
                   sys_uptime_ticks, cpu_usage_pct, mem_usage_pct, temp_celsius, interface_count,
                   vendor_name, device_category, matched_mibs, hardware_model, serial_number,
                   firmware_version, is_reachable, last_poll_duration_ms, last_polled
            FROM snmp_devices
            """
        )

        unifi_dev_rows = await conn.fetch(
            """
            SELECT mac, ip_address, name, model, device_type, firmware_version,
                   status, uptime_sec, cpu_usage_pct, mem_usage_pct, temp_celsius,
                   poe_total_watts, num_sta, last_seen
            FROM unifi_devices
            WHERE ip_address IS NOT NULL AND ip_address != ''
            """
        )

        unifi_cli_rows = await conn.fetch(
            """
            SELECT mac, ip_address, hostname, oui_vendor, is_wired, network_name,
                   vlan_id, sw_mac, sw_port, essid, signal_dbm, last_seen
            FROM unifi_clients
            WHERE ip_address IS NOT NULL AND ip_address != ''
            """
        )

        log_counts = await conn.fetch(
            """
            SELECT source_ip, COUNT(*) AS log_count_24h
            FROM device_logs
            WHERE time > NOW() - INTERVAL '24 hours'
            GROUP BY source_ip
            """
        )

    by_ip: Dict[str, Dict[str, Any]] = {}

    def _ensure_ip(ip_str: str) -> Dict[str, Any]:
        if ip_str not in by_ip:
            by_ip[ip_str] = {
                "ip_address": ip_str,
                "hostname": ip_str,
                "mac_address": None,
                "vendor": None,
                "device_category": None,
                "matched_mibs": [],
                "subnet_cidr": None,
                "vlan_id": None,
                "is_online": False,
                "snmp_enabled": False,
                "snmp_reachable": None,
                "snmp_profile_name": None,
                "snmp_profile_id": None,
                "snmp_target_id": None,
                "sys_descr": None,
                "sys_location": None,
                "cpu_usage_pct": None,
                "interface_count": 0,
                "uptime_sec": None,
                "sources": [],
                "log_count_24h": 0,
                "last_seen": None,
            }
        return by_ip[ip_str]

    for r in disc_rows:
        ip_str = str(r["ip_address"])
        item = _ensure_ip(ip_str)
        item["hostname"] = r["hostname"] or item["hostname"]
        item["mac_address"] = r["mac_address"] or item["mac_address"]
        item["vendor"] = r["vendor"] or item["vendor"]
        item["subnet_cidr"] = r["subnet_cidr"]
        item["vlan_id"] = r["vlan_id"]
        item["is_online"] = bool(r["is_online"])
        if "Nmap/ARP" not in item["sources"]:
            item["sources"].append("Nmap/ARP")
        if r["last_seen"]:
            item["last_seen"] = r["last_seen"].isoformat()

    for r in snmp_cfg_rows:
        ip_str = str(r["device_ip"])
        item = _ensure_ip(ip_str)
        if r["label"]:
            item["hostname"] = r["label"]
        item["snmp_enabled"] = bool(r["enabled"])
        item["snmp_profile_name"] = r["profile_name"]
        item["snmp_profile_id"] = r["profile_id"]
        item["snmp_target_id"] = r["target_id"]
        if "SNMP" not in item["sources"]:
            item["sources"].append("SNMP")

    for r_rec in snmp_dev_rows:
        r = dict(r_rec)
        ip_str = str(r["device_ip"])
        item = _ensure_ip(ip_str)
        if r.get("sys_name") and (item["hostname"] == ip_str or not item["hostname"]):
            item["hostname"] = r["sys_name"]
        item["snmp_reachable"] = bool(r.get("is_reachable"))
        if r.get("is_reachable"):
            item["is_online"] = True
        if r.get("vendor_name") and not item["vendor"]:
            item["vendor"] = (
                f"{r['vendor_name']} ({r['hardware_model']})"
                if r.get("hardware_model")
                else r["vendor_name"]
            )
        item["device_category"] = r.get("device_category") or item["device_category"]
        raw_mibs = r.get("matched_mibs")
        if isinstance(raw_mibs, str):
            try:
                raw_mibs = json.loads(raw_mibs)
            except Exception:
                raw_mibs = []
        if raw_mibs:
            item["matched_mibs"] = raw_mibs
        item["sys_descr"] = r.get("sys_descr")
        item["sys_location"] = r.get("sys_location")
        item["cpu_usage_pct"] = float(r.get("cpu_usage_pct") or 0.0)
        item["interface_count"] = int(r.get("interface_count") or 0)
        if r.get("sys_uptime_ticks"):
            item["uptime_sec"] = int(r["sys_uptime_ticks"]) // 100
        if "SNMP" not in item["sources"]:
            item["sources"].append("SNMP")
        if r.get("last_polled"):
            item["last_seen"] = r["last_polled"].isoformat()

    for r in unifi_dev_rows:
        ip_str = str(r["ip_address"])
        item = _ensure_ip(ip_str)
        if r["name"]:
            item["hostname"] = r["name"]
        item["mac_address"] = r["mac"] or item["mac_address"]
        item["vendor"] = item["vendor"] or f"Ubiquiti ({r['model']})"
        if r["status"] == "online":
            item["is_online"] = True
        item["cpu_usage_pct"] = float(r["cpu_usage_pct"] or 0.0)
        item["uptime_sec"] = int(r["uptime_sec"] or 0)
        if "UniFi" not in item["sources"]:
            item["sources"].append("UniFi")
        if r["last_seen"]:
            item["last_seen"] = r["last_seen"].isoformat()

    for r in unifi_cli_rows:
        ip_str = str(r["ip_address"])
        item = _ensure_ip(ip_str)
        if r["hostname"] and item["hostname"] == ip_str:
            item["hostname"] = r["hostname"]
        item["mac_address"] = r["mac"] or item["mac_address"]
        item["vendor"] = r["oui_vendor"] or item["vendor"]
        item["vlan_id"] = r["vlan_id"] or item["vlan_id"]
        if "UniFi" not in item["sources"]:
            item["sources"].append("UniFi")

    for r in log_counts:
        ip_str = str(r["source_ip"])
        item = _ensure_ip(ip_str)
        item["log_count_24h"] = int(r["log_count_24h"] or 0)
        if "Syslog" not in item["sources"]:
            item["sources"].append("Syslog")

    # Auto-match vendor MIB profile for any previously discovered device missing matched_mibs
    for item in by_ip.values():
        if not item.get("matched_mibs"):
            matched = match_device_vendor_profile(
                "",
                f"{item.get('vendor') or ''} {item.get('sys_descr') or ''}",
                item.get("hostname") or "",
            )
            item["matched_mibs"] = matched.get("mibs") or []
            item["device_category"] = item.get("device_category") or matched.get("category")
            matched_vendor = matched.get("vendor_name") or ""
            if not item.get("vendor") and matched_vendor not in (
                "",
                "Standard SNMP Device",
                "Generic / Standard RFC",
            ):
                item["vendor"] = matched_vendor

    def _ip_sort_key(d: Dict[str, Any]):
        try:
            return (0, int(ipaddress.ip_address(d["ip_address"])))
        except ValueError:
            return (1, d["ip_address"])

    devices_list = sorted(by_ip.values(), key=_ip_sort_key)
    return {"devices": devices_list}


@app.get("/api/devices/{device_ip}")
async def get_device_detail(device_ip: str) -> Dict[str, Any]:
    """Returns all polled and correlated information for a specific device IP."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        host_row = await conn.fetchrow(
            """
            SELECT host(ip_address) AS ip_address, subnet_cidr, mac_address,
                   hostname, vendor, vlan_id, is_online, latency_ms,
                   discovery_source, switch_mac, switch_port, first_seen, last_seen
            FROM discovered_hosts
            WHERE host(ip_address) = $1
            """,
            device_ip,
        )

        snmp_cfg_row = await conn.fetchrow(
            """
            SELECT t.id AS target_id, t.device_ip, t.label, t.profile_id, t.enabled,
                   p.name AS profile_name, p.version AS profile_version, p.port AS profile_port
            FROM snmp_targets_config t
            LEFT JOIN snmp_profiles p ON p.id = t.profile_id
            WHERE t.device_ip = $1
            """,
            device_ip,
        )

        snmp_sys_row = await conn.fetchrow(
            """
            SELECT device_ip, sys_name, sys_descr, sys_object_id, sys_location, sys_contact,
                   sys_uptime_ticks, cpu_usage_pct, mem_usage_pct, temp_celsius, interface_count,
                   vendor_name, device_category, matched_mibs, hardware_model, serial_number,
                   firmware_version, extended_metrics, is_reachable, last_poll_duration_ms, last_polled
            FROM snmp_devices
            WHERE device_ip = $1
            """,
            device_ip,
        )

        snmp_if_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (if_index)
                if_index, if_name, if_descr, if_alias, if_speed_bps, if_oper_status,
                in_octets, out_octets, in_bps, out_bps, in_errors, out_errors,
                in_discards, out_discards, cpu_usage, sys_uptime, time
            FROM snmp_metrics_raw
            WHERE device_ip = $1
            ORDER BY if_index ASC, time DESC
            """,
            device_ip,
        )

        unifi_dev_row = await conn.fetchrow(
            """
            SELECT mac, ip_address, name, model, device_type, firmware_version,
                   status, uptime_sec, cpu_usage_pct, mem_usage_pct, temp_celsius,
                   poe_total_watts, poe_max_watts, num_sta, rx_bps, tx_bps, port_table, last_seen
            FROM unifi_devices
            WHERE ip_address = $1
            """,
            device_ip,
        )

        unifi_cli_row = await conn.fetchrow(
            """
            SELECT mac, ip_address, hostname, oui_vendor, is_wired, network_name,
                   vlan_id, sw_mac, sw_port, ap_mac, essid, channel, rssi, signal_dbm,
                   rx_bps, tx_bps, rx_bytes, tx_bytes, uptime_sec, last_seen
            FROM unifi_clients
            WHERE ip_address = $1
            """,
            device_ip,
        )

        log_rows = await conn.fetch(
            """
            SELECT time, source_ip, hostname, facility, severity, severity_num,
                   app_name, proc_id, message
            FROM device_logs
            WHERE source_ip = $1
            ORDER BY time DESC
            LIMIT 100
            """,
            device_ip,
        )

    def _serialize(row: Optional[asyncpg.Record]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        d = dict(row)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        for json_col in ("port_table", "matched_mibs", "extended_metrics"):
            if isinstance(d.get(json_col), str):
                try:
                    d[json_col] = json.loads(d[json_col])
                except Exception:
                    d[json_col] = [] if json_col != "extended_metrics" else {}
        return d

    host_dict = _serialize(host_row)
    snmp_sys_dict = _serialize(snmp_sys_row)
    unifi_dev_dict = _serialize(unifi_dev_row)
    unifi_cli_dict = _serialize(unifi_cli_row)

    # Ensure snmp_system or a fallback vendor MIB match is always populated for previously discovered devices
    if snmp_sys_dict and not snmp_sys_dict.get("matched_mibs"):
        matched = match_device_vendor_profile(
            snmp_sys_dict.get("sys_object_id") or "",
            snmp_sys_dict.get("sys_descr") or (host_dict or {}).get("vendor") or "",
            snmp_sys_dict.get("sys_name") or (host_dict or {}).get("hostname") or "",
        )
        snmp_sys_dict["vendor_name"] = snmp_sys_dict.get("vendor_name") or matched.get("vendor_name")
        snmp_sys_dict["device_category"] = snmp_sys_dict.get("device_category") or matched.get("category")
        snmp_sys_dict["matched_mibs"] = matched.get("mibs") or []

    fallback_mib_match = match_device_vendor_profile(
        (snmp_sys_dict or {}).get("sys_object_id") or "",
        " ".join(
            filter(
                None,
                [
                    (snmp_sys_dict or {}).get("sys_descr"),
                    (host_dict or {}).get("vendor"),
                    (unifi_dev_dict or {}).get("model"),
                    (unifi_cli_dict or {}).get("oui_vendor"),
                ],
            )
        ),
        (snmp_sys_dict or {}).get("sys_name")
        or (host_dict or {}).get("hostname")
        or (unifi_dev_dict or {}).get("name")
        or device_ip,
    )

    return {
        "ip_address": device_ip,
        "discovered_host": host_dict,
        "snmp_config": _serialize(snmp_cfg_row),
        "snmp_system": snmp_sys_dict,
        "matched_mib_profile": fallback_mib_match,
        "snmp_interfaces": [_serialize(r) for r in snmp_if_rows],
        "unifi_device": unifi_dev_dict,
        "unifi_client": unifi_cli_dict,
        "recent_logs": [_serialize(r) for r in log_rows],
    }


@app.post("/api/devices/rematch-mibs")
async def trigger_rematch_all_device_mibs() -> Dict[str, str]:
    """Immediately re-matches all previously discovered devices with the MIB catalog and probes SNMP."""
    await _notify_worker("rematch_mibs")
    return {
        "status": "triggered",
        "message": "Auto-matching MIB profiles and probing SNMP across all discovered devices.",
    }


@app.post("/api/devices/{device_ip}/poll")
async def trigger_device_poll(device_ip: str) -> Dict[str, str]:
    await _notify_worker("rematch_mibs")
    return {"status": "triggered", "message": f"Immediate SNMP & MIB poll queued for {device_ip}."}


@app.get("/api/mibs")
async def get_mib_catalog() -> Dict[str, Any]:
    """Returns all MIB files stored on the backend server (/app/mibs) and the Vendor Fingerprint Catalog."""
    return {
        "stored_mibs": list_backend_mibs(),
        "vendor_catalog": VENDOR_CATALOG,
    }


@app.post("/api/mibs")
async def upload_custom_mib(payload: CustomMIBUploadPayload) -> Dict[str, Any]:
    """Saves a custom .mib file to /app/mibs on the server backend and indexes its OIDs."""
    raw_name = Path(payload.filename.strip() or "custom.mib").name
    if not (raw_name.endswith(".mib") or raw_name.endswith(".txt")):
        raw_name = f"{raw_name}.mib"
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="MIB content cannot be empty.")

    MIBS_DIR.mkdir(parents=True, exist_ok=True)
    target_path = MIBS_DIR / raw_name
    target_path.write_text(content, encoding="utf-8")

    parsed = parse_mib_text(content, raw_name)
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO custom_mibs (
                module_name, filename, organization, description, enterprise_pen, object_count, objects
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT (module_name) DO UPDATE SET
                filename = EXCLUDED.filename,
                organization = EXCLUDED.organization,
                description = EXCLUDED.description,
                enterprise_pen = EXCLUDED.enterprise_pen,
                object_count = EXCLUDED.object_count,
                objects = EXCLUDED.objects,
                uploaded_at = NOW()
            """,
            parsed["module_name"],
            raw_name,
            parsed["organization"],
            parsed["description"],
            parsed["enterprise_pen"],
            parsed["object_count"],
            json.dumps(parsed["objects"]),
        )

    await _notify_worker("snmp_now")
    return await get_mib_catalog()


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
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if msg and msg.get("data"):
                raw = msg["data"]
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                await ws.send_text(raw)
            else:
                await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket closed: %s", exc)
    finally:
        await pubsub.close()
