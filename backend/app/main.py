import asyncio
import gzip
import importlib.metadata
import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import socket
import struct
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.db_schema import (
    ensure_device_ids,
    ensure_dynamic_config_tables,
    repair_implausible_snmp_spikes,
)
from app.icmp_poller import resolve_reverse_dns
from app.mib_catalog import (
    MIBS_DIR,
    VENDOR_CATALOG,
    list_backend_mibs,
    match_device_vendor_profile,
    parse_mib_text,
)
from app.syslog_parser import parse_ingest_payload, parse_syslog_event

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


_GIT_TRAILER_PREFIXES = (
    "signed-off-by:",
    "co-authored-by:",
    "reviewed-by:",
    "acked-by:",
    "tested-by:",
)


def _summarize_changed_areas(file_paths: List[str]) -> List[str]:
    """Maps changed repository file paths to ordered, deduplicated component labels."""
    areas: List[str] = []

    def _add(label: str) -> None:
        if label not in areas:
            areas.append(label)

    for raw_path in file_paths:
        path = raw_path.strip().lstrip("./")
        if not path:
            continue
        if path.startswith("frontend/"):
            _add("Frontend UI")
        elif path in ("backend/app/snmp_poller.py", "backend/app/mib_catalog.py") or path.startswith("backend/mibs/"):
            _add("SNMP & MIBs")
        elif path == "backend/app/unifi.py":
            _add("UniFi Telemetry")
        elif path == "backend/app/scanner.py":
            _add("Subnet Scanner")
        elif path == "backend/app/syslog_parser.py" or path.startswith("vector/"):
            _add("Syslog / Vector")
        elif path == "backend/app/db_schema.py" or path.startswith("db-init/"):
            _add("Database Schema")
        elif path.startswith("backend/app/") or path.startswith("backend/"):
            _add("Backend API")
        elif path in ("docker-compose.yml", "install-netdash.sh") or path.startswith("scripts/"):
            _add("Docker & Ops")
        else:
            _add("Docs & Config")
    return areas


def _extract_commit_bullets(subject: str, body: str = "") -> List[str]:
    """Extracts clean per-commit bullet points from body or subject fallback."""
    bullets: List[str] = []
    has_active_bullet = False

    for bline in (body or "").splitlines():
        bclean = bline.strip()
        if not bclean:
            has_active_bullet = False
            continue
        if bclean.lower().startswith(_GIT_TRAILER_PREFIXES):
            has_active_bullet = False
            continue
        if bclean.startswith(("- ", "* ", "• ")):
            btext = bclean[2:].strip()
            if btext and btext not in bullets:
                bullets.append(btext)
                has_active_bullet = True
            else:
                has_active_bullet = False
        elif has_active_bullet and bullets:
            bullets[-1] = f"{bullets[-1]} {bclean}".strip()

    if bullets:
        return bullets

    # Fallback when body uses prose lines instead of explicit bullet markers
    for bline in (body or "").splitlines():
        bclean = bline.strip()
        if not bclean or bclean.lower().startswith(_GIT_TRAILER_PREFIXES):
            continue
        if bclean not in bullets:
            bullets.append(bclean)

    if bullets:
        return bullets

    subj_clean = (subject or "").strip()
    if "; " in subj_clean:
        for clause in subj_clean.split("; "):
            c = clause.strip().rstrip(".")
            if c:
                formatted = c[0].upper() + c[1:]
                if formatted not in bullets:
                    bullets.append(formatted)
    elif subj_clean:
        c = subj_clean.rstrip(".")
        if c:
            bullets.append(c[0].upper() + c[1:])
    return bullets


async def _compute_git_version_map(ref: str = "origin/main") -> Dict[str, str]:
    """Maps commit short SHAs to [Year.Month.Day.UpdateCount] version strings."""
    if not (REPO_MOUNT_DIR / ".git").exists():
        return {}

    async def _git_lines(target_ref: str) -> List[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-c",
                f"safe.directory={REPO_MOUNT_DIR}",
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
    from_sha = str(data.get("from_sha") or "").strip()
    started_at_str = data.get("started_at")
    started_dt: Optional[datetime] = None
    if isinstance(started_at_str, str) and started_at_str:
        try:
            started_dt = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
        except Exception:
            started_dt = None

    async def _run_git_safe(*args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-c",
                f"safe.directory={REPO_MOUNT_DIR}",
                "-C",
                str(REPO_MOUNT_DIR),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            return out.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""

    async def _mark_completed(resolved_sha: str) -> Dict[str, Any]:
        ver_map = await _compute_git_version_map("HEAD")
        target_ver = ver_map.get(resolved_sha, str(data.get("target_version") or resolved_sha))
        completed = {
            **data,
            "target_sha": resolved_sha,
            "target_version": target_ver,
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

    if not trigger_exists and (REPO_MOUNT_DIR / ".git").exists():
        head_sha = await _run_git_safe("rev-parse", "--short=7", "HEAD")
        behind_str = await _run_git_safe("rev-list", "HEAD..origin/main", "--count")
        commits_behind = int(behind_str) if behind_str.isdigit() else -1
        git_updated = bool(head_sha) and (
            (target_sha and head_sha == target_sha)
            or (from_sha and head_sha != from_sha and commits_behind == 0)
        )

        if git_updated:
            # Fast-path completion if API restarted after update started
            if started_dt is not None and API_START_TIME > started_dt:
                return await _mark_completed(head_sha)

            # Fallback for legacy host netdash-update scripts (or frontend-only updates where API wasn't recreated)
            if data.get("phase") == "queued":
                if age_sec >= 65.0:
                    return await _mark_completed(head_sha)
                legacy_pct = min(85, max(20, int(20 + age_sec)))
                step_pct = min(90, max(25, int(25 + age_sec)))
                ver_map = await _compute_git_version_map("HEAD")
                resolved_ver = ver_map.get(
                    head_sha, str(data.get("target_version") or head_sha)
                )
                return {
                    **data,
                    "phase": "legacy_build",
                    "overall_pct": legacy_pct,
                    "target_sha": head_sha,
                    "target_version": resolved_ver,
                    "message": f"Source updated to {resolved_ver} — rebuilding containers on host...",
                    "updated_at": now.isoformat(),
                    "components": [
                        {
                            **c,
                            "status": "completed" if c.get("id") == "git" else "running",
                            "pct": 100 if c.get("id") == "git" else step_pct,
                            "detail": (
                                f"Source updated to {resolved_ver}"
                                if c.get("id") == "git"
                                else "Rebuilding & restarting container..."
                            ),
                        }
                        for c in (data.get("components") or [])
                    ],
                }

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


class ISPSettingsPayload(BaseModel):
    isp_name: str = ""
    download_mbps: float = 1000.0
    upload_mbps: float = 1000.0


class OverviewGraphDeviceSelection(BaseModel):
    device_ip: str
    if_indices: List[int] = []


class OverviewGraphSettingsPayload(BaseModel):
    mode: str = "auto"
    selections: List[OverviewGraphDeviceSelection] = []


class DeviceSettingsPayload(BaseModel):
    custom_name: Optional[str] = None
    custom_location: Optional[str] = None
    custom_device_type: Optional[str] = None
    notes: Optional[str] = None
    snmp_enabled: Optional[bool] = None
    snmp_profile_id: Optional[int] = None


class ICMPTargetPayload(BaseModel):
    device_ip: str
    label: str = ""
    poll_interval: int = 30
    enabled: bool = True


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
            "--pretty=format:%x1e%h%x1f%s%x1f%an%x1f%ar%x1f%cs%x1f%b%x1f",
            "--name-only",
        )
        for rec in log_out.split("\x1e"):
            rec = rec.strip()
            if not rec:
                continue
            parts = rec.split("\x1f", 6)
            if len(parts) >= 5:
                sha_val = parts[0].strip()
                subj_val = parts[1].strip()
                auth_val = parts[2].strip()
                date_val = parts[3].strip()
                commit_date_val = parts[4].strip()
                body_val = parts[5].strip() if len(parts) > 5 else ""
                files_raw = parts[6] if len(parts) > 6 else ""
                changed_files = [ln.strip() for ln in files_raw.splitlines() if ln.strip()]
                areas = _summarize_changed_areas(changed_files)
                files_changed = len(changed_files)

                per_bullets = _extract_commit_bullets(subj_val, body_val)
                commits.append(
                    {
                        "version": ver_map.get(sha_val, sha_val),
                        "sha": sha_val,
                        "subject": subj_val,
                        "summary_bullets": per_bullets,
                        "areas": areas,
                        "files_changed": files_changed,
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


DOCKER_SOCKET_PATH = os.getenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock")

MANAGED_DOCKER_SERVICES: Dict[str, Dict[str, Any]] = {
    "api": {"name": "NetDash API Server", "container": "netdash-api", "critical": True},
    "worker": {"name": "Background Telemetry Worker", "container": "netdash-worker", "critical": False},
    "timescaledb": {"name": "TimescaleDB / PostgreSQL", "container": "netdash-timescaledb", "critical": True},
    "redis": {"name": "Redis Cache & Pub/Sub", "container": "netdash-redis", "critical": False},
    "vector": {"name": "Vector Syslog Receiver", "container": "netdash-vector", "critical": False},
    "frontend": {"name": "Nginx Web Frontend", "container": "netdash-frontend", "critical": True},
    "proxy": {"name": "Web Edge Proxy", "container": "netdash-proxy", "critical": True},
}


def _docker_socket_available() -> bool:
    sock = Path(DOCKER_SOCKET_PATH)
    try:
        return sock.exists() and sock.is_socket()
    except OSError:
        return False


async def _docker_api_request(
    method: str,
    path: str,
    timeout: float = 20.0,
) -> Tuple[int, bytes]:
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET_PATH)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://docker",
        timeout=timeout,
    ) as client:
        resp = await client.request(method, path)
        return resp.status_code, resp.content


def _decode_docker_log_stream(raw: bytes) -> str:
    """Decodes Docker Engine API multiplexed stdout/stderr frames or falls back to plain UTF-8."""
    if not raw:
        return ""
    chunks: List[bytes] = []
    pos = 0
    n = len(raw)
    matched_frames = 0
    while pos + 8 <= n:
        stream_type = raw[pos]
        if stream_type not in (0, 1, 2) or raw[pos + 1 : pos + 4] != b"\x00\x00\x00":
            break
        frame_len = struct.unpack(">I", raw[pos + 4 : pos + 8])[0]
        if frame_len > n - (pos + 8):
            break
        chunks.append(raw[pos + 8 : pos + 8 + frame_len])
        pos += 8 + frame_len
        matched_frames += 1
    if matched_frames > 0 and pos == n:
        return b"".join(chunks).decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


async def _inspect_docker_container(container_name: str) -> Optional[Dict[str, Any]]:
    try:
        status_code, raw_body = await _docker_api_request(
            "GET", f"/containers/{container_name}/json", timeout=2.5
        )
        if status_code != 200:
            return None
        data = json.loads(raw_body.decode("utf-8", errors="replace"))
        state = data.get("State") or {}
        status_str = str(state.get("Status") or "").lower()
        running = bool(state.get("Running"))
        restarting = bool(state.get("Restarting"))
        exit_code = state.get("ExitCode")
        err_msg = str(state.get("Error") or "").strip()
        restart_count = int(data.get("RestartCount") or 0)
        started_at = str(state.get("StartedAt") or "")
        uptime_sec: Optional[int] = None
        if running and started_at and not started_at.startswith("0001-01-01"):
            try:
                iso = re.sub(r"\.(\d{6})\d+", r".\1", started_at.replace("Z", "+00:00"))
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                uptime_sec = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
            except Exception:
                uptime_sec = None
        return {
            "state": status_str or ("running" if running else "exited"),
            "running": running,
            "restarting": restarting,
            "exit_code": exit_code,
            "error": err_msg,
            "restart_count": restart_count,
            "started_at": started_at,
            "uptime_sec": uptime_sec,
        }
    except Exception:
        return None


async def _execute_docker_service_action(
    container_name: str, action: str
) -> Tuple[int, bytes]:
    if action == "start":
        return await _docker_api_request("POST", f"/containers/{container_name}/start", timeout=20.0)
    if action == "stop":
        return await _docker_api_request(
            "POST", f"/containers/{container_name}/stop?t=10", timeout=20.0
        )
    if action == "restart":
        return await _docker_api_request(
            "POST", f"/containers/{container_name}/restart?t=10", timeout=25.0
        )
    raise ValueError(f"Unsupported Docker action: {action}")


async def _delayed_docker_action(container_name: str, action: str) -> None:
    await asyncio.sleep(0.35)
    try:
        await _execute_docker_service_action(container_name, action)
    except Exception as exc:
        logger.warning("Deferred Docker action %s on %s failed: %s", action, container_name, exc)


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
                ("icmp_poller", "ICMP Ping Poller"),
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
        raw_tag = _read_compose_image_tag("vector", "0.49.0-alpine")
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
                asyncio.open_connection("vector", 1514),
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
                "detail": f"UDP/TCP :{syslog_port} -> :1514 listening • {logs_24h} logs ingested (24h)",
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
                "detail": f"Port {syslog_port} (internal :1514) unreachable • {logs_24h} logs stored (24h)",
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

    docker_ctrl_ok = _docker_socket_available()
    inspected: Dict[str, Optional[Dict[str, Any]]] = {}
    if docker_ctrl_ok:
        svc_keys = list(MANAGED_DOCKER_SERVICES.keys())
        inspected_list = await asyncio.gather(
            *[
                _inspect_docker_container(MANAGED_DOCKER_SERVICES[k]["container"])
                for k in svc_keys
            ]
        )
        inspected = dict(zip(svc_keys, inspected_list))

    db_size_bytes = ts_svc.pop("db_size_bytes", None)
    services = [api_svc, worker_svc, ts_svc, redis_svc, vector_svc, frontend_svc]
    if proxy_svc is not None:
        services.append(proxy_svc)
    elif inspected.get("proxy") is not None:
        services.append(
            {
                "id": "proxy",
                "name": "Web Edge Proxy",
                "container": "netdash-proxy",
                "status": "stopped",
                "version": "nginx/1.27",
                "uptime_sec": None,
                "latency_ms": None,
                "detail": f"Container present on host port :{web_port}",
            }
        )

    for svc in services:
        sid = str(svc.get("id") or "")
        meta = MANAGED_DOCKER_SERVICES.get(sid, {})
        c_info = inspected.get(sid)
        svc["docker_state"] = c_info["state"] if c_info else None
        svc["exit_code"] = c_info["exit_code"] if c_info else None
        svc["restart_count"] = c_info["restart_count"] if c_info else None
        svc["can_control"] = bool(docker_ctrl_ok and meta)
        svc["critical"] = bool(meta.get("critical", False))
        if svc.get("uptime_sec") is None and c_info and c_info.get("uptime_sec") is not None:
            svc["uptime_sec"] = c_info["uptime_sec"]
        if c_info:
            d_state = c_info["state"]
            d_exit = c_info["exit_code"]
            if svc["status"] == "stopped" and d_state:
                if d_state == "restarting":
                    svc["status"] = "degraded"
                suffix = f"Docker: {d_state}" + (
                    f", exit {d_exit}" if d_exit not in (None, 0) else ""
                )
                svc["detail"] = f"{svc['detail']} ({suffix})"
            elif d_state == "restarting":
                svc["status"] = "degraded"

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
            "docker_control_available": docker_ctrl_ok,
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


@app.post("/api/system/services/{service_id}/{action}")
async def control_managed_service(service_id: str, action: str) -> Dict[str, Any]:
    """Starts, stops, or restarts an allowlisted NetDash container via Docker Engine socket."""
    svc_meta = MANAGED_DOCKER_SERVICES.get(service_id)
    if not svc_meta:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_id}")
    norm_action = (action or "").strip().lower()
    if norm_action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="Action must be 'start', 'stop', or 'restart'.")
    if not _docker_socket_available():
        raise HTTPException(
            status_code=503,
            detail="Docker socket unavailable. Mount /var/run/docker.sock on netdash-api to manage services.",
        )

    container_name = svc_meta["container"]
    if service_id in ("api", "proxy") and norm_action in ("stop", "restart"):
        asyncio.create_task(_delayed_docker_action(container_name, norm_action))
        return {
            "ok": True,
            "service_id": service_id,
            "container": container_name,
            "action": norm_action,
            "deferred": True,
            "message": f"Scheduled {norm_action} for {svc_meta['name']} ({container_name}).",
        }

    status_code, body = await _execute_docker_service_action(container_name, norm_action)
    ok_codes = (204, 304) if norm_action in ("start", "stop") else (204,)
    if status_code not in ok_codes:
        err_detail = body.decode("utf-8", errors="replace").strip() or f"HTTP {status_code}"
        raise HTTPException(
            status_code=500,
            detail=f"Docker {norm_action} failed for {container_name}: {err_detail}",
        )

    if norm_action in ("start", "restart"):
        await asyncio.sleep(0.6)
    c_info = await _inspect_docker_container(container_name)
    docker_state = c_info["state"] if c_info else None
    return {
        "ok": True,
        "service_id": service_id,
        "container": container_name,
        "action": norm_action,
        "deferred": False,
        "docker_state": docker_state,
        "message": f"Completed {norm_action} on {svc_meta['name']} ({container_name}).",
    }


@app.get("/api/system/services/{service_id}/logs")
async def get_managed_service_logs(
    service_id: str,
    tail: int = Query(default=100, ge=10, le=500),
) -> Dict[str, Any]:
    """Returns recent stdout/stderr logs for an allowlisted NetDash container."""
    svc_meta = MANAGED_DOCKER_SERVICES.get(service_id)
    if not svc_meta:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_id}")
    if not _docker_socket_available():
        raise HTTPException(
            status_code=503,
            detail="Docker socket unavailable. Mount /var/run/docker.sock on netdash-api to view logs.",
        )

    container_name = svc_meta["container"]
    status_code, raw_body = await _docker_api_request(
        "GET",
        f"/containers/{container_name}/logs?stdout=1&stderr=1&timestamps=1&tail={tail}",
        timeout=5.0,
    )
    if status_code != 200:
        err_detail = raw_body.decode("utf-8", errors="replace").strip() or f"HTTP {status_code}"
        raise HTTPException(
            status_code=500,
            detail=f"Unable to read logs for {container_name}: {err_detail}",
        )

    return {
        "service_id": service_id,
        "container": container_name,
        "logs": _decode_docker_log_stream(raw_body),
    }


# ==============================================================================
# Dynamic Configuration & First-Time Onboarding Endpoints
# ==============================================================================


async def _read_isp_settings(conn: asyncpg.Connection) -> Dict[str, Any]:
    raw = await conn.fetchval("SELECT value FROM app_settings WHERE key = 'isp_settings'")
    parsed: Dict[str, Any] = {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
    elif isinstance(raw, dict):
        parsed = dict(raw)

    def _safe_mbps(val: Any, default: float = 1000.0) -> float:
        try:
            num = float(val)
            if num < 0:
                return 0.0
            return round(min(num, 1_000_000.0), 2)
        except Exception:
            return default

    return {
        "isp_name": str(parsed.get("isp_name") or "").strip()[:80],
        "download_mbps": _safe_mbps(parsed.get("download_mbps"), 1000.0)
        if "download_mbps" in parsed
        else 1000.0,
        "upload_mbps": _safe_mbps(parsed.get("upload_mbps"), 1000.0)
        if "upload_mbps" in parsed
        else 1000.0,
    }


def _normalize_overview_graph_settings(raw: Any) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
    elif isinstance(raw, dict):
        parsed = dict(raw)

    mode = str(parsed.get("mode") or "auto").strip().lower()
    if mode not in ("auto", "custom"):
        mode = "auto"

    selections: List[Dict[str, Any]] = []
    by_ip: Dict[str, Dict[str, Any]] = {}
    raw_sels = parsed.get("selections")
    if isinstance(raw_sels, list):
        for entry in raw_sels:
            if not isinstance(entry, dict):
                continue
            ip = str(entry.get("device_ip") or "").strip()
            if not ip:
                continue
            raw_idxs = entry.get("if_indices")
            clean_idxs: List[int] = []
            if isinstance(raw_idxs, list):
                seen_idx = set()
                for v in raw_idxs:
                    try:
                        iv = int(v)
                        if iv > 0 and iv not in seen_idx:
                            seen_idx.add(iv)
                            clean_idxs.append(iv)
                    except Exception:
                        continue
            clean_idxs.sort()
            if ip not in by_ip:
                obj = {"device_ip": ip, "if_indices": clean_idxs}
                by_ip[ip] = obj
                selections.append(obj)
            else:
                if not by_ip[ip]["if_indices"] or not clean_idxs:
                    by_ip[ip]["if_indices"] = []
                else:
                    merged = sorted(set(by_ip[ip]["if_indices"]) | set(clean_idxs))
                    by_ip[ip]["if_indices"] = merged

    if mode == "custom" and not selections:
        mode = "auto"

    return {"mode": mode, "selections": selections}


async def _read_overview_graph_settings(conn: asyncpg.Connection) -> Dict[str, Any]:
    raw = await conn.fetchval(
        "SELECT value FROM app_settings WHERE key = 'overview_graph_settings'"
    )
    return _normalize_overview_graph_settings(raw)


async def _describe_overview_graph_source(
    conn: asyncpg.Connection,
    graph_settings: Dict[str, Any],
    has_unifi_gw: bool,
) -> Dict[str, Any]:
    mode = graph_settings.get("mode", "auto")
    selections = graph_settings.get("selections") or []
    if mode == "custom" and selections:
        sel_ips = [s["device_ip"] for s in selections if s.get("device_ip")]
        host_rows = await conn.fetch(
            """
            SELECT ip_address AS ip, COALESCE(NULLIF(hostname, ''), ip_address) AS name
            FROM discovered_hosts
            WHERE ip_address = ANY($1::text[])
            """,
            sel_ips,
        )
        snmp_rows = await conn.fetch(
            """
            SELECT device_ip AS ip, COALESCE(NULLIF(sys_name, ''), device_ip) AS name
            FROM snmp_devices
            WHERE device_ip = ANY($1::text[])
            """,
            sel_ips,
        )
        tgt_rows = await conn.fetch(
            """
            SELECT device_ip AS ip, COALESCE(NULLIF(label, ''), device_ip) AS name
            FROM snmp_targets_config
            WHERE device_ip = ANY($1::text[])
            """,
            sel_ips,
        )
        name_map: Dict[str, str] = {}
        for row in host_rows:
            name_map[str(row["ip"])] = str(row["name"])
        for row in tgt_rows:
            if row["name"] and str(row["name"]) != str(row["ip"]):
                name_map[str(row["ip"])] = str(row["name"])
        for row in snmp_rows:
            if row["name"] and str(row["name"]) != str(row["ip"]):
                name_map[str(row["ip"])] = str(row["name"])

        if_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (device_ip, if_index)
                device_ip, if_index, COALESCE(NULLIF(if_name, ''), NULLIF(if_descr, ''), 'if' || if_index::text) AS if_name
            FROM snmp_metrics_raw
            WHERE device_ip = ANY($1::text[])
              AND time > NOW() - INTERVAL '7 days'
            ORDER BY device_ip, if_index, time DESC
            """,
            sel_ips,
        )
        if_map: Dict[tuple[str, int], str] = {
            (str(r["device_ip"]), int(r["if_index"])): str(r["if_name"]) for r in if_rows
        }

        total_spec_ifs = 0
        has_all_if = False
        parts: List[str] = []
        for s in selections:
            ip = str(s["device_ip"])
            idxs = [int(i) for i in (s.get("if_indices") or [])]
            d_label = name_map.get(ip, ip)
            if d_label != ip:
                d_disp = f"{d_label} ({ip})"
            else:
                d_disp = ip
            if not idxs:
                has_all_if = True
                parts.append(f"{d_disp}: All Interfaces")
            else:
                total_spec_ifs += len(idxs)
                if_names = [if_map.get((ip, idx), f"#{idx}") for idx in idxs[:3]]
                if len(idxs) > 3:
                    if_names.append(f"+{len(idxs) - 3}")
                parts.append(f"{d_disp}: {', '.join(if_names)}")

        if len(selections) == 1:
            summary = parts[0]
        else:
            if total_spec_ifs > 0 and not has_all_if:
                summary = f"{len(selections)} devices • {total_spec_ifs} interface{'s' if total_spec_ifs != 1 else ''}"
            elif total_spec_ifs > 0:
                summary = f"{len(selections)} devices ({total_spec_ifs} specific + all-ports)"
            else:
                summary = f"{len(selections)} devices (All Interfaces)"

        return {
            "mode": "custom",
            "source": "custom_snmp",
            "device_count": len(selections),
            "interface_count": total_spec_ifs,
            "has_all_interfaces_device": has_all_if,
            "summary": summary,
        }

    return {
        "mode": "auto",
        "source": "unifi" if has_unifi_gw else "snmp_all",
        "device_count": 0,
        "interface_count": 0,
        "has_all_interfaces_device": False,
        "summary": "Auto (UniFi Gateway WAN)"
        if has_unifi_gw
        else "Auto (All Polled SNMP Interfaces)",
    }


@app.get("/api/config")
async def get_all_config() -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        ob_val = await conn.fetchval("SELECT value FROM app_settings WHERE key = 'onboarding'")
        onboarding = json.loads(ob_val) if isinstance(ob_val, str) else (ob_val or {"completed": False, "skipped": False})
        isp_settings = await _read_isp_settings(conn)
        overview_graph_settings = await _read_overview_graph_settings(conn)

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
        icmp_targets = await conn.fetch(
            """
            SELECT id, device_ip, label, poll_interval, enabled, resolved_hostname,
                   is_reachable, last_rtt_ms, packet_loss_pct, status_since, last_polled
            FROM icmp_targets_config
            ORDER BY id ASC
            """
        )
        target_id_map = await ensure_device_ids(
            conn,
            [str(r["device_ip"]) for r in targets if r["device_ip"]]
            + [str(r["device_ip"]) for r in icmp_targets if r["device_ip"]],
        )

    targets_out = []
    for r in targets:
        d = dict(r)
        d["device_id"] = target_id_map.get(str(r["device_ip"]))
        targets_out.append(d)

    icmp_targets_out = []
    for r in icmp_targets:
        d = dict(r)
        d["device_id"] = target_id_map.get(str(r["device_ip"]))
        if d.get("status_since"):
            d["status_since"] = d["status_since"].isoformat()
        if d.get("last_polled"):
            d["last_polled"] = d["last_polled"].isoformat()
        icmp_targets_out.append(d)

    return {
        "onboarding": onboarding,
        "isp_settings": isp_settings,
        "overview_graph_settings": overview_graph_settings,
        "snmp_profiles": [dict(r) for r in profiles],
        "snmp_targets": targets_out,
        "icmp_targets": icmp_targets_out,
        "subnets": [dict(r) for r in subnets],
        "unifi_controllers": [dict(r) for r in unifi_ctrls],
    }


@app.get("/api/config/overview-graph/options")
async def get_overview_graph_options() -> Dict[str, Any]:
    """Returns all discovered & SNMP-configured devices alongside their polled interfaces."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        host_rows = await conn.fetch(
            """
            SELECT ip_address, hostname, vendor, is_online, subnet_cidr
            FROM discovered_hosts
            """
        )
        tgt_rows = await conn.fetch(
            """
            SELECT device_ip, label, enabled
            FROM snmp_targets_config
            """
        )
        snmp_rows = await conn.fetch(
            f"""
            SELECT device_ip, sys_name, sys_descr, vendor_name, device_category,
                   matched_mibs, is_reachable,
                   (extended_metrics ? 'ups_telemetry') AS has_ups_telemetry,
                   {_HAS_WIRELESS_SQL} AS has_wireless_telemetry
            FROM snmp_devices
            """
        )
        clean_in_z = _clean_bps_sql("in_bps", coalesce_zero=True)
        clean_out_z = _clean_bps_sql("out_bps", coalesce_zero=True)
        if_rows = await conn.fetch(
            f"""
            SELECT DISTINCT ON (device_ip, if_index)
                device_ip, if_index, if_name, if_descr, if_alias,
                if_speed_bps, if_oper_status,
                {clean_in_z} AS in_bps,
                {clean_out_z} AS out_bps
            FROM snmp_metrics_raw
            WHERE time > NOW() - INTERVAL '7 days'
              AND {_NON_LOOPBACK_SQL}
            ORDER BY device_ip, if_index, time DESC
            """
        )
        opt_id_map = await ensure_device_ids(
            conn,
            [str(hr["ip_address"]).strip() for hr in host_rows if hr["ip_address"]]
            + [str(tr["device_ip"]).strip() for tr in tgt_rows if tr["device_ip"]]
            + [str(sr["device_ip"]).strip() for sr in snmp_rows if sr["device_ip"]]
            + [str(ir["device_ip"]).strip() for ir in if_rows if ir["device_ip"]],
        )

    dev_map: Dict[str, Dict[str, Any]] = {}

    def _ensure_dev(ip: str) -> Dict[str, Any]:
        if ip not in dev_map:
            dev_map[ip] = {
                "device_id": opt_id_map.get(ip),
                "device_ip": ip,
                "hostname": ip,
                "vendor": None,
                "device_category": None,
                "device_profile_type": "network",
                "is_online": False,
                "snmp_enabled": False,
                "snmp_reachable": None,
                "subnet_cidr": None,
                "interfaces": [],
                "_raw_snmp": None,
            }
        return dev_map[ip]

    for hr in host_rows:
        ip = str(hr["ip_address"]).strip()
        if not ip:
            continue
        d = _ensure_dev(ip)
        if hr["hostname"]:
            d["hostname"] = str(hr["hostname"]).strip()
        if hr["vendor"]:
            d["vendor"] = str(hr["vendor"]).strip()
        d["is_online"] = bool(hr["is_online"])
        if hr["subnet_cidr"]:
            d["subnet_cidr"] = str(hr["subnet_cidr"])

    for tr in tgt_rows:
        ip = str(tr["device_ip"]).strip()
        if not ip:
            continue
        d = _ensure_dev(ip)
        d["snmp_enabled"] = bool(tr["enabled"])
        if tr["label"] and (not d["hostname"] or d["hostname"] == ip):
            d["hostname"] = str(tr["label"]).strip()

    for sr in snmp_rows:
        ip = str(sr["device_ip"]).strip()
        if not ip:
            continue
        d = _ensure_dev(ip)
        raw_snmp = dict(sr)
        d["_raw_snmp"] = raw_snmp
        if sr["sys_name"] and str(sr["sys_name"]).strip() and str(sr["sys_name"]).strip() != ip:
            d["hostname"] = str(sr["sys_name"]).strip()
        if sr["vendor_name"]:
            d["vendor"] = str(sr["vendor_name"]).strip()
        if sr["device_category"]:
            d["device_category"] = str(sr["device_category"]).strip()
        if sr["is_reachable"] is not None:
            d["snmp_reachable"] = bool(sr["is_reachable"])
            if sr["is_reachable"]:
                d["is_online"] = True

    for ir in if_rows:
        ip = str(ir["device_ip"]).strip()
        if not ip:
            continue
        d = _ensure_dev(ip)
        d["interfaces"].append(
            {
                "if_index": int(ir["if_index"]),
                "if_name": str(ir["if_name"] or f"if{ir['if_index']}"),
                "if_descr": str(ir["if_descr"] or ""),
                "if_alias": str(ir["if_alias"] or ""),
                "if_speed_bps": int(ir["if_speed_bps"] or 0),
                "if_oper_status": int(ir["if_oper_status"] or 0),
                "in_bps": round(float(ir["in_bps"] or 0.0), 1),
                "out_bps": round(float(ir["out_bps"] or 0.0), 1),
            }
        )

    devices_out: List[Dict[str, Any]] = []
    for d in dev_map.values():
        raw_snmp = d.pop("_raw_snmp", None)
        if isinstance(raw_snmp, dict) and isinstance(raw_snmp.get("matched_mibs"), str):
            try:
                raw_snmp["matched_mibs"] = json.loads(raw_snmp["matched_mibs"])
            except Exception:
                raw_snmp["matched_mibs"] = []
        d["device_profile_type"] = _classify_device_profile(
            raw_snmp,
            None,
            {"sys_name": d["hostname"], "vendor_name": d["vendor"]},
        )
        d["interfaces"].sort(
            key=lambda x: (
                0 if x["if_oper_status"] == 1 else 1,
                -(x["in_bps"] + x["out_bps"]),
                x["if_index"],
            )
        )
        devices_out.append(d)

    def _dev_sort_key(item: Dict[str, Any]) -> tuple:
        ip = str(item.get("device_ip") or "")
        try:
            ip_key = (0, int(ipaddress.IPv4Address(ip)))
        except Exception:
            ip_key = (1, ip)
        return (
            0 if item["interfaces"] else 1,
            0 if item["snmp_enabled"] else 1,
            0 if item["is_online"] else 1,
            ip_key,
        )

    devices_out.sort(key=_dev_sort_key)
    return {"devices": devices_out}


@app.put("/api/config/overview-graph")
async def save_overview_graph_settings(
    payload: OverviewGraphSettingsPayload,
) -> Dict[str, Any]:
    raw_mode = (payload.mode or "auto").strip().lower()
    if raw_mode not in ("auto", "custom"):
        raise HTTPException(status_code=400, detail="Mode must be 'auto' or 'custom'.")

    normalized = _normalize_overview_graph_settings(
        {
            "mode": raw_mode,
            "selections": [
                {"device_ip": s.device_ip, "if_indices": s.if_indices}
                for s in payload.selections
            ],
        }
    )
    if raw_mode == "custom" and not normalized["selections"]:
        raise HTTPException(
            status_code=400,
            detail="Select at least one device when using Custom mode.",
        )

    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES ('overview_graph_settings', $1::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """,
            json.dumps(normalized),
        )

    if redis_client:
        try:
            await redis_client.delete("netdash:live:traffic")
        except Exception:
            pass
    await _notify_worker("live_reload")
    return await get_all_config()


@app.put("/api/config/isp")
async def save_isp_settings(payload: ISPSettingsPayload) -> Dict[str, Any]:
    dl = float(payload.download_mbps if payload.download_mbps is not None else 0.0)
    ul = float(payload.upload_mbps if payload.upload_mbps is not None else 0.0)
    if dl < 0 or ul < 0 or dl > 1_000_000 or ul > 1_000_000:
        raise HTTPException(
            status_code=400, detail="Internet speeds must be between 0 and 1,000,000 Mbps."
        )
    val = {
        "isp_name": payload.isp_name.strip()[:80],
        "download_mbps": round(dl, 2),
        "upload_mbps": round(ul, 2),
    }
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES ('isp_settings', $1::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """,
            json.dumps(val),
        )
    return await get_all_config()


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

        added_target_ips: List[str] = []
        for t in payload.snmp_targets:
            ip_val = t.device_ip.strip()
            if not ip_val:
                continue
            added_target_ips.append(ip_val)
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
        if added_target_ips:
            await ensure_device_ids(conn, added_target_ips)

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
        await ensure_device_ids(conn, [ip_str])
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
        await ensure_device_ids(conn, [ip_str])
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


@app.post("/api/config/icmp-targets")
async def create_icmp_target(payload: ICMPTargetPayload) -> Dict[str, Any]:
    raw_ip = payload.device_ip.strip()
    if not raw_ip:
        raise HTTPException(status_code=400, detail="Host IPv4 address is required.")
    try:
        ip_str = str(ipaddress.ip_address(raw_ip))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid IP address: {raw_ip}") from exc

    label_str = payload.label.strip()
    poll_int = max(10, min(3600, int(payload.poll_interval or 30)))
    dns_host = await resolve_reverse_dns(ip_str, timeout=0.6)

    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO icmp_targets_config (device_ip, label, poll_interval, enabled, resolved_hostname)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (device_ip) DO UPDATE SET
                label = EXCLUDED.label,
                poll_interval = EXCLUDED.poll_interval,
                enabled = EXCLUDED.enabled,
                resolved_hostname = COALESCE(EXCLUDED.resolved_hostname, icmp_targets_config.resolved_hostname)
            """,
            ip_str,
            label_str,
            poll_int,
            bool(payload.enabled),
            dns_host,
        )
        await ensure_device_ids(conn, [ip_str])
    await _notify_worker("icmp_now")
    return await get_all_config()


@app.put("/api/config/icmp-targets/{target_id}")
async def update_icmp_target(target_id: int, payload: ICMPTargetPayload) -> Dict[str, Any]:
    raw_ip = payload.device_ip.strip()
    if not raw_ip:
        raise HTTPException(status_code=400, detail="Host IPv4 address is required.")
    try:
        ip_str = str(ipaddress.ip_address(raw_ip))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid IP address: {raw_ip}") from exc

    label_str = payload.label.strip()
    poll_int = max(10, min(3600, int(payload.poll_interval or 30)))

    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE icmp_targets_config
            SET device_ip = $1, label = $2, poll_interval = $3, enabled = $4
            WHERE id = $5
            """,
            ip_str,
            label_str,
            poll_int,
            bool(payload.enabled),
            target_id,
        )
        await ensure_device_ids(conn, [ip_str])
    await _notify_worker("icmp_now")
    return await get_all_config()


@app.delete("/api/config/icmp-targets/{target_id}")
async def delete_icmp_target(target_id: int) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM icmp_targets_config WHERE id = $1",
            target_id,
        )
    await _notify_worker("icmp_now")
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


_NON_LOOPBACK_SQL = (
    "COALESCE(LOWER(if_name), '') NOT IN ('lo', 'lo0', 'loopback', 'loopback0', 'null0') "
    "AND COALESCE(LOWER(if_name), '') NOT LIKE '%loopback%' "
    "AND COALESCE(LOWER(if_descr), '') NOT LIKE '%loopback%'"
)


def _clean_bps_sql(col: str, coalesce_zero: bool = False) -> str:
    """SQL expression that masks physically impossible SNMP throughput counter spikes."""
    expr = (
        f"CASE WHEN (COALESCE(if_speed_bps, 0) > 0 "
        f"AND {col} > if_speed_bps * 1.15) "
        f"OR {col} > 40000000000.0 THEN NULL ELSE {col} END"
    )
    return f"COALESCE({expr}, 0)" if coalesce_zero else expr


def _merge_traffic_gap_buckets(
    primary_rows: List[Dict[str, Any]],
    fallback_rows: List[Dict[str, Any]],
    bucket_sec: int,
) -> List[Dict[str, Any]]:
    """Preserves primary UniFi gateway buckets and fills missing gap buckets from SNMP WAN."""
    merged: List[Dict[str, Any]] = []
    primary_ts: List[float] = []
    for row in primary_rows:
        b_dt = row.get("bucket")
        if b_dt is None:
            continue
        merged.append(dict(row))
        primary_ts.append(b_dt.timestamp())

    min_sep = max(30.0, float(bucket_sec) * 0.5)
    for row in fallback_rows:
        b_dt = row.get("bucket")
        if b_dt is None:
            continue
        ts_val = b_dt.timestamp()
        if any(abs(ts_val - pts) < min_sep for pts in primary_ts):
            continue
        merged.append(dict(row))

    merged.sort(key=lambda r: r["bucket"])
    return merged


async def _query_overview_traffic_series(
    conn: asyncpg.Connection,
    range_key: str = "1d",
    graph_settings: Optional[Dict[str, Any]] = None,
) -> tuple[List[Dict[str, Any]], str]:
    rk = (range_key or "1d").strip().lower()
    if rk == "24h":
        rk = "1d"
    elif rk == "30d":
        rk = "1m"
    if rk not in ("1h", "12h", "1d", "7d", "1m"):
        rk = "1d"

    range_specs = {
        "1h": ("1 hour", "30 seconds", 30),
        "12h": ("12 hours", "2 minutes", 120),
        "1d": ("24 hours", "5 minutes", 300),
        "7d": ("7 days", "30 minutes", 1800),
        "1m": ("30 days", "2 hours", 7200),
    }
    sql_interval, sql_bucket, bucket_sec = range_specs[rk]

    if graph_settings is None:
        graph_settings = await _read_overview_graph_settings(conn)

    selections = graph_settings.get("selections") or []
    use_custom = graph_settings.get("mode") == "custom" and bool(selections)
    clean_avg_in = _clean_bps_sql("avg_in_bps")
    clean_avg_out = _clean_bps_sql("avg_out_bps")
    clean_raw_in = _clean_bps_sql("in_bps")
    clean_raw_out = _clean_bps_sql("out_bps")

    wan_history = []
    if use_custom:
        all_if_ips: List[str] = []
        spec_ips: List[str] = []
        spec_indices: List[int] = []
        for s in selections:
            ip = str(s.get("device_ip") or "").strip()
            if not ip:
                continue
            idxs = [int(i) for i in (s.get("if_indices") or []) if int(i) > 0]
            if not idxs:
                all_if_ips.append(ip)
            else:
                for idx in idxs:
                    spec_ips.append(ip)
                    spec_indices.append(idx)

        custom_where = f"""
            (
                (device_ip = ANY($1::text[]) AND {_NON_LOOPBACK_SQL})
                OR (device_ip, if_index) IN (SELECT * FROM unnest($2::text[], $3::int[]))
            )
        """

        if rk == "7d":
            wan_history = await conn.fetch(
                f"""
                SELECT bucket,
                       SUM({clean_avg_in}) AS rx_bps,
                       SUM({clean_avg_out}) AS tx_bps,
                       0.0 AS latency_ms
                FROM snmp_metrics_hourly
                WHERE bucket > NOW() - INTERVAL '7 days'
                  AND {custom_where}
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                all_if_ips,
                spec_ips,
                spec_indices,
            )
        elif rk == "1m":
            wan_history = await conn.fetch(
                f"""
                SELECT bucket,
                       SUM(if_rx) AS rx_bps,
                       SUM(if_tx) AS tx_bps,
                       0.0 AS latency_ms
                FROM (
                    SELECT time_bucket('4 hours', bucket) AS bucket,
                           device_ip,
                           if_index,
                           AVG({clean_avg_in}) AS if_rx,
                           AVG({clean_avg_out}) AS if_tx
                    FROM snmp_metrics_hourly
                    WHERE bucket > NOW() - INTERVAL '30 days'
                      AND {custom_where}
                    GROUP BY 1, device_ip, if_index
                ) sub
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                all_if_ips,
                spec_ips,
                spec_indices,
            )

        if not wan_history:
            wan_history = await conn.fetch(
                f"""
                SELECT bucket,
                       SUM(if_rx) AS rx_bps,
                       SUM(if_tx) AS tx_bps,
                       0.0 AS latency_ms
                FROM (
                    SELECT time_bucket('{sql_bucket}', time) AS bucket,
                           device_ip,
                           if_index,
                           AVG({clean_raw_in}) AS if_rx,
                           AVG({clean_raw_out}) AS if_tx
                    FROM snmp_metrics_raw
                    WHERE time > NOW() - INTERVAL '{sql_interval}'
                      AND {custom_where}
                    GROUP BY bucket, device_ip, if_index
                ) sub
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                all_if_ips,
                spec_ips,
                spec_indices,
            )
    else:
        wan_history = await conn.fetch(
            f"""
            SELECT time_bucket('{sql_bucket}', time) AS bucket,
                   AVG(wan_rx_bps) AS rx_bps,
                   AVG(wan_tx_bps) AS tx_bps,
                   AVG(wan_latency_ms) AS latency_ms
            FROM unifi_gateway_metrics
            WHERE time > NOW() - INTERVAL '{sql_interval}'
            GROUP BY bucket
            ORDER BY bucket ASC
            """
        )

        # Detect trailing or internal gaps in UniFi gateway history and backfill from SNMP WAN
        if wan_history:
            now_utc = datetime.now(timezone.utc)
            last_bucket = wan_history[-1]["bucket"]
            has_gap = (now_utc - last_bucket).total_seconds() > (bucket_sec * 2)
            if not has_gap and len(wan_history) >= 2:
                for idx in range(1, len(wan_history)):
                    if (
                        wan_history[idx]["bucket"] - wan_history[idx - 1]["bucket"]
                    ).total_seconds() > (bucket_sec * 2):
                        has_gap = True
                        break
            if has_gap:
                gw_ip = await conn.fetchval(
                    """
                    SELECT COALESCE(NULLIF(lan_ip, ''), NULLIF(ip_address, ''))
                    FROM unifi_devices
                    WHERE device_type IN ('udm', 'ugw', 'uxg')
                      AND (COALESCE(lan_ip, '') != '' OR COALESCE(ip_address, '') != '')
                    ORDER BY last_seen DESC NULLS LAST
                    LIMIT 1
                    """
                )
                if gw_ip:
                    gw_ip_str = str(gw_ip).strip()
                    wan_if_index = await conn.fetchval(
                        f"""
                        SELECT if_index
                        FROM snmp_metrics_raw
                        WHERE device_ip = $1
                          AND time > NOW() - INTERVAL '{sql_interval}'
                          AND {_NON_LOOPBACK_SQL}
                        GROUP BY if_index
                        ORDER BY
                            MAX(
                                CASE
                                    WHEN COALESCE(if_name, '') ~* '^(eth8|eth9|eth10|ppp|wan)'
                                      OR COALESCE(if_descr, '') ~* 'wan'
                                      OR COALESCE(if_alias, '') ~* 'wan'
                                    THEN 1
                                    ELSE 0
                                END
                            ) DESC,
                            AVG(COALESCE({clean_raw_in}, 0) + COALESCE({clean_raw_out}, 0)) DESC
                        LIMIT 1
                        """,
                        gw_ip_str,
                    )
                    if wan_if_index is not None:
                        if rk == "7d":
                            fb_rows = await conn.fetch(
                                f"""
                                SELECT bucket,
                                       AVG({clean_avg_in}) AS rx_bps,
                                       AVG({clean_avg_out}) AS tx_bps,
                                       0.0 AS latency_ms
                                FROM snmp_metrics_hourly
                                WHERE device_ip = $1
                                  AND if_index = $2
                                  AND bucket > NOW() - INTERVAL '7 days'
                                GROUP BY bucket
                                ORDER BY bucket ASC
                                """,
                                gw_ip_str,
                                int(wan_if_index),
                            )
                        elif rk == "1m":
                            fb_rows = await conn.fetch(
                                f"""
                                SELECT time_bucket('4 hours', bucket) AS bucket,
                                       AVG({clean_avg_in}) AS rx_bps,
                                       AVG({clean_avg_out}) AS tx_bps,
                                       0.0 AS latency_ms
                                FROM snmp_metrics_hourly
                                WHERE device_ip = $1
                                  AND if_index = $2
                                  AND bucket > NOW() - INTERVAL '30 days'
                                GROUP BY 1
                                ORDER BY 1 ASC
                                """,
                                gw_ip_str,
                                int(wan_if_index),
                            )
                        else:
                            fb_rows = await conn.fetch(
                                f"""
                                SELECT time_bucket('{sql_bucket}', time) AS bucket,
                                       AVG({clean_raw_in}) AS rx_bps,
                                       AVG({clean_raw_out}) AS tx_bps,
                                       0.0 AS latency_ms
                                FROM snmp_metrics_raw
                                WHERE device_ip = $1
                                  AND if_index = $2
                                  AND time > NOW() - INTERVAL '{sql_interval}'
                                GROUP BY bucket
                                ORDER BY bucket ASC
                                """,
                                gw_ip_str,
                                int(wan_if_index),
                            )
                        if fb_rows:
                            wan_history = _merge_traffic_gap_buckets(
                                [dict(r) for r in wan_history],
                                [dict(r) for r in fb_rows],
                                bucket_sec,
                            )

        # Fallback: aggregate SNMP traffic across all non-loopback interfaces
        if not wan_history:
            if rk in ("7d", "1m"):
                if rk == "7d":
                    wan_history = await conn.fetch(
                        f"""
                        SELECT bucket,
                               SUM({clean_avg_in}) AS rx_bps,
                               SUM({clean_avg_out}) AS tx_bps,
                               0.0 AS latency_ms
                        FROM snmp_metrics_hourly
                        WHERE bucket > NOW() - INTERVAL '7 days'
                          AND {_NON_LOOPBACK_SQL}
                        GROUP BY bucket
                        ORDER BY bucket ASC
                        """
                    )
                else:
                    wan_history = await conn.fetch(
                        f"""
                        SELECT bucket,
                               SUM(if_rx) AS rx_bps,
                               SUM(if_tx) AS tx_bps,
                               0.0 AS latency_ms
                        FROM (
                            SELECT time_bucket('4 hours', bucket) AS bucket,
                                   device_ip,
                                   if_index,
                                   AVG({clean_avg_in}) AS if_rx,
                                   AVG({clean_avg_out}) AS if_tx
                            FROM snmp_metrics_hourly
                            WHERE bucket > NOW() - INTERVAL '30 days'
                              AND {_NON_LOOPBACK_SQL}
                            GROUP BY 1, device_ip, if_index
                        ) sub
                        GROUP BY bucket
                        ORDER BY bucket ASC
                        """
                    )

            if not wan_history:
                wan_history = await conn.fetch(
                    f"""
                    SELECT bucket,
                           SUM(if_rx) AS rx_bps,
                           SUM(if_tx) AS tx_bps,
                           0.0 AS latency_ms
                    FROM (
                        SELECT time_bucket('{sql_bucket}', time) AS bucket,
                               device_ip,
                               if_index,
                               AVG({clean_raw_in}) AS if_rx,
                               AVG({clean_raw_out}) AS if_tx
                        FROM snmp_metrics_raw
                        WHERE time > NOW() - INTERVAL '{sql_interval}'
                          AND {_NON_LOOPBACK_SQL}
                        GROUP BY bucket, device_ip, if_index
                    ) sub
                    GROUP BY bucket
                    ORDER BY bucket ASC
                    """
                )

    series = [
        {
            "time": r["bucket"].isoformat(),
            "rx_bps": round(float(r["rx_bps"] or 0.0), 1),
            "tx_bps": round(float(r["tx_bps"] or 0.0), 1),
            "latency_ms": round(float(r["latency_ms"] or 0.0), 2),
        }
        for r in wan_history
    ]
    return series, rk


@app.get("/api/overview")
async def get_pulse_overview(
    range_key: str = Query(default="1d", alias="range"),
) -> Dict[str, Any]:
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

        overview_graph_settings = await _read_overview_graph_settings(conn)
        wan_series, normalized_range = await _query_overview_traffic_series(
            conn, range_key, graph_settings=overview_graph_settings
        )
        overview_graph_source = await _describe_overview_graph_source(
            conn, overview_graph_settings, bool(gw_row)
        )
        isp_settings = await _read_isp_settings(conn)

        unifi_devs = await conn.fetch(
            """
            SELECT mac, ip_address, lan_ip, name, model, device_type, firmware_version,
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

        ov_ips = [str(s["device_ip"]).strip() for s in snmp_devs if s["device_ip"]]
        for d in unifi_devs:
            for cand in (d["lan_ip"], d["ip_address"]):
                c_ip = str(cand or "").strip()
                if c_ip:
                    ov_ips.append(c_ip)
        ov_id_map = await ensure_device_ids(conn, ov_ips)

    devices_list = []
    for d in unifi_devs:
        item = dict(d)
        if isinstance(item.get("port_table"), str):
            try:
                item["port_table"] = json.loads(item["port_table"])
            except Exception:
                item["port_table"] = []
        item["last_seen"] = item["last_seen"].isoformat() if item.get("last_seen") else None
        unifi_key = str(item.get("lan_ip") or item.get("ip_address") or "").strip()
        item["device_id"] = ov_id_map.get(unifi_key)
        devices_list.append(item)

    snmp_list = []
    for s in snmp_devs:
        item = dict(s)
        item["device_id"] = ov_id_map.get(str(item.get("device_ip") or "").strip())
        item["last_polled"] = item["last_polled"].isoformat() if item.get("last_polled") else None
        snmp_list.append(item)

    gw_dict = dict(gw_row) if gw_row else None
    if gw_dict and gw_dict.get("time"):
        gw_dict["time"] = gw_dict["time"].isoformat()

    return {
        "gateway": gw_dict,
        "wan_series": wan_series,
        "series_range": normalized_range,
        "isp_settings": isp_settings,
        "overview_graph_settings": overview_graph_settings,
        "overview_graph_source": overview_graph_source,
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


@app.get("/api/overview/traffic")
async def get_overview_traffic(
    range_key: str = Query(default="1d", alias="range"),
) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        overview_graph_settings = await _read_overview_graph_settings(conn)
        has_gw = bool(
            await conn.fetchval("SELECT 1 FROM unifi_gateway_metrics LIMIT 1")
        )
        series, norm_range = await _query_overview_traffic_series(
            conn, range_key, graph_settings=overview_graph_settings
        )
        overview_graph_source = await _describe_overview_graph_source(
            conn, overview_graph_settings, has_gw
        )
    return {
        "range": norm_range,
        "series": series,
        "overview_graph_settings": overview_graph_settings,
        "overview_graph_source": overview_graph_source,
    }


@app.get("/api/snmp/interfaces")
async def get_snmp_interfaces(device_ip: Optional[str] = None) -> Dict[str, Any]:
    pool = _get_pool()
    c_in_z = _clean_bps_sql("in_bps", coalesce_zero=True)
    c_out_z = _clean_bps_sql("out_bps", coalesce_zero=True)
    async with pool.acquire() as conn:
        if device_ip:
            rows = await conn.fetch(
                f"""
                SELECT DISTINCT ON (device_ip, if_index)
                    device_ip, hostname, if_index, if_name, if_descr, if_alias,
                    if_speed_bps, if_oper_status,
                    {c_in_z} AS in_bps,
                    {c_out_z} AS out_bps,
                    in_errors, out_errors, cpu_usage, sys_uptime, time
                FROM snmp_metrics_raw
                WHERE device_ip = $1 AND time > NOW() - INTERVAL '24 hours'
                ORDER BY device_ip, if_index, time DESC
                """,
                device_ip,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT DISTINCT ON (device_ip, if_index)
                    device_ip, hostname, if_index, if_name, if_descr, if_alias,
                    if_speed_bps, if_oper_status,
                    {c_in_z} AS in_bps,
                    {c_out_z} AS out_bps,
                    in_errors, out_errors, cpu_usage, sys_uptime, time
                FROM snmp_metrics_raw
                WHERE time > NOW() - INTERVAL '24 hours'
                ORDER BY device_ip, if_index, time DESC
                """
            )
        if_id_map = await ensure_device_ids(
            conn, [str(r["device_ip"]).strip() for r in rows if r["device_ip"]]
        )
    items = []
    for r in rows:
        d = dict(r)
        d["device_id"] = if_id_map.get(str(d.get("device_ip") or "").strip())
        d["time"] = d["time"].isoformat() if d.get("time") else None
        items.append(d)
    return {"interfaces": items}


_HAS_WIRELESS_SQL = (
    "COALESCE(("
    "jsonb_array_length(COALESCE(extended_metrics->'wireless_telemetry'->'radios', '[]'::jsonb)) > 0"
    " OR jsonb_array_length(COALESCE(extended_metrics->'wireless_telemetry'->'vaps', '[]'::jsonb)) > 0"
    " OR jsonb_array_length(COALESCE(extended_metrics->'wireless_telemetry'->'stations', '[]'::jsonb)) > 0"
    " OR jsonb_array_length(COALESCE(extended_metrics->'wireless_radios', '[]'::jsonb)) > 0"
    "), FALSE)"
)
_HAS_WIRELESS_SQL_D = (
    "COALESCE(("
    "jsonb_array_length(COALESCE(d.extended_metrics->'wireless_telemetry'->'radios', '[]'::jsonb)) > 0"
    " OR jsonb_array_length(COALESCE(d.extended_metrics->'wireless_telemetry'->'vaps', '[]'::jsonb)) > 0"
    " OR jsonb_array_length(COALESCE(d.extended_metrics->'wireless_telemetry'->'stations', '[]'::jsonb)) > 0"
    " OR jsonb_array_length(COALESCE(d.extended_metrics->'wireless_radios', '[]'::jsonb)) > 0"
    "), FALSE)"
)


def _has_wireless_telemetry(snmp_sys: Optional[Dict[str, Any]]) -> bool:
    """Returns True only when SNMP wireless radios, VAPs, stations, or legacy wireless_radios are present."""
    if not isinstance(snmp_sys, dict):
        return False
    if snmp_sys.get("has_wireless_telemetry"):
        return True
    ext = snmp_sys.get("extended_metrics")
    if isinstance(ext, dict):
        wl = ext.get("wireless_telemetry")
        if isinstance(wl, dict):
            if (
                (wl.get("radios") or [])
                or (wl.get("vaps") or [])
                or (wl.get("stations") or [])
            ):
                return True
        w_rad = ext.get("wireless_radios")
        if isinstance(w_rad, list) and len(w_rad) > 0:
            return True
    return False


def _classify_device_profile(
    snmp_sys: Optional[Dict[str, Any]],
    unifi_dev: Optional[Dict[str, Any]],
    fallback_mib: Optional[Dict[str, Any]],
) -> str:
    """Classifies a managed device into ups, wireless, printer, nas, network, or server."""
    if snmp_sys:
        ext = snmp_sys.get("extended_metrics")
        if isinstance(ext, dict) and ext.get("ups_telemetry"):
            return "ups"
        if snmp_sys.get("has_ups_telemetry"):
            return "ups"

    mibs: List[str] = []
    texts: List[str] = []
    for src in (snmp_sys, fallback_mib):
        if not isinstance(src, dict):
            continue
        for key in ("device_category", "category", "vendor_name", "sys_descr", "sys_name"):
            val = src.get(key)
            if val:
                texts.append(str(val))
        for mib_col in ("matched_mibs", "mibs"):
            raw_m = src.get(mib_col)
            if isinstance(raw_m, list):
                mibs.extend(str(x) for x in raw_m)

    combined = " ".join(texts + mibs).lower()
    if any(m in ("PowerNet-UPS-MIB", "UPS-MIB", "CPS-MIB") for m in mibs) or re.search(
        r"\b(ups|powernet|uninterruptible|battery backup|smart-ups|back-ups|apc|cyberpower|eaton|symmetra|tripp lite)\b",
        combined,
    ):
        return "ups"

    if _has_wireless_telemetry(snmp_sys):
        return "wireless"

    if "Printer-MIB" in mibs or re.search(r"\b(printer|mfp|laserjet|officejet)\b", combined):
        return "printer"
    if any(
        m in ("SYNOLOGY-SYSTEM-MIB", "SYNOLOGY-DISK-MIB", "SYNOLOGY-RAID-MIB") for m in mibs
    ) or re.search(r"\b(nas|storage server|storage appliance|synology|truenas|freenas|qnap)\b", combined):
        return "nas"

    if isinstance(unifi_dev, dict) and str(unifi_dev.get("device_type") or "").lower() in (
        "udm",
        "ugw",
        "uxg",
        "usw",
        "usp",
    ):
        return "network"

    id_parts: List[str] = []
    for src in (snmp_sys, unifi_dev):
        if not isinstance(src, dict):
            continue
        for key in ("sys_descr", "sys_name", "hardware_model", "model", "name"):
            val = src.get(key)
            if val:
                id_parts.append(str(val))
    if isinstance(fallback_mib, dict):
        for key in ("sys_descr", "sys_name", "hardware_model", "model"):
            val = fallback_mib.get(key)
            if val:
                id_parts.append(str(val))
    identity_text = " ".join(id_parts).lower()

    if re.search(
        r"\b(udm[-\s]?pro|udm[-\s]?se|udm[-\s]?max|ucg|uxg|usg|usw|edgeswitch|edgerouter|dream machine pro)\b",
        identity_text,
    ):
        return "network"

    if isinstance(unifi_dev, dict) and str(unifi_dev.get("device_type") or "").lower() == "uap":
        return "wireless"

    if re.search(
        r"(?:^|[^a-z0-9])(uap|u6(?:\+|-(?:pro|lite|lr|mesh|iw|enterprise|extender)|\s+(?:pro|lite|lr|mesh|iw|enterprise|extender))|u7(?:\+|-(?:pro|lite|max|wall|outdoor)|\s+(?:pro|lite|max|wall|outdoor))|nanohd|flexhd|beaconhd|iw-hd|uk-ultra|uwb|nanobeam|powerbeam|litebeam|airfiber|access point|wireless ap|eap\d+|aironet|iap-\d+)",
        identity_text,
    ):
        return "wireless"

    if re.search(
        r"\b(cisco\s+ucs|ucs[-\s]?c?\d{3,4}|ucsc[-\s]?\w+|ucsb[-\s]?\w+|cimc)\b",
        f"{combined} {identity_text}",
    ):
        return "server"

    if unifi_dev or re.search(
        r"\b(switch|gateway|router|firewall|ubiquiti|unifi|udm|mikrotik|cisco|fortinet|juniper|aruba|arista|netgear|access point)\b",
        combined,
    ):
        return "network"
    return "server"


_VALID_CUSTOM_DEVICE_TYPES = {
    "router",
    "switch",
    "ap",
    "firewall",
    "server",
    "nas",
    "ups",
    "printer",
    "network",
    "client",
    "icmp",
}


def _role_to_profile_type(role: str) -> str:
    """Maps a device_role override to its corresponding device_profile_type."""
    r = (role or "").strip().lower()
    if r == "ups":
        return "ups"
    if r in ("ap", "wireless"):
        return "wireless"
    if r == "nas":
        return "nas"
    if r == "printer":
        return "printer"
    if r == "server":
        return "server"
    if r == "client":
        return "client"
    if r == "icmp":
        return "icmp"
    return "network"


def _classify_device_role(
    profile_type: str,
    snmp_sys: Optional[Dict[str, Any]],
    unifi_dev: Optional[Dict[str, Any]],
    sources: Optional[List[str]] = None,
    interface_count: int = 0,
    is_unifi_client: bool = False,
) -> Optional[str]:
    """Classifies granular hardware role (router, switch, ap, firewall, server, nas, ups, printer, network, client)."""
    if is_unifi_client and not unifi_dev and "SNMP" not in (sources or []):
        return "client"

    ptype = (profile_type or "").strip().lower()
    if ptype == "ups":
        return "ups"

    unifi_dtype = (
        str(unifi_dev.get("device_type") or "").strip().lower()
        if isinstance(unifi_dev, dict)
        else ""
    )
    if ptype == "wireless" or unifi_dtype == "uap":
        return "ap"
    if ptype == "printer":
        return "printer"
    if ptype == "nas":
        return "nas"

    id_parts: List[str] = []
    mibs: List[str] = []
    for src in (snmp_sys, unifi_dev):
        if not isinstance(src, dict):
            continue
        for key in (
            "device_category",
            "vendor_name",
            "sys_descr",
            "sys_name",
            "hardware_model",
            "model",
            "name",
            "hostname",
        ):
            val = src.get(key)
            if val:
                id_parts.append(str(val))
        for mcol in ("matched_mibs", "mibs"):
            raw_m = src.get(mcol)
            if isinstance(raw_m, list):
                mibs.extend(str(x) for x in raw_m)

    identity = " ".join(id_parts).lower()

    if re.search(
        r"\b(fortigate|palo\s*alto|pfsense|opnsense|sophos|sonicwall|firewall|cisco\s+asa|watchguard|firepower)\b",
        identity,
    ):
        return "firewall"

    if unifi_dtype in ("udm", "ugw", "uxg") or re.search(
        r"\b(udm[-\s]?pro|udm[-\s]?se|udm[-\s]?max|ucg|uxg|usg|dream\s*machine|edgerouter|router|gateway|vyos|mikrotik.*\b(?:rb|ccr|hex|router))\b",
        identity,
    ):
        return "router"

    if (
        unifi_dtype == "usw"
        or re.search(
            r"\b(usw|edgeswitch|switch|catalyst|procurve|arista|nexus|crs\d+|css\d+|netgear.*\bgs\d+)\b",
            identity,
        )
        or (ptype == "network" and int(interface_count or 0) >= 8)
    ):
        return "switch"

    if re.search(
        r"\b(cisco\s+ucs|ucs[-\s]?c?\d{3,4}|ucsc[-\s]?\w+|ucsb[-\s]?\w+|cimc)\b",
        identity,
    ):
        return "server"

    if ptype == "network":
        return "network"

    has_server_mib = any(
        m in ("HOST-RESOURCES-MIB", "UCD-SNMP-MIB", "LM-SENSORS-MIB") for m in mibs
    )
    has_server_identity = bool(
        re.search(
            r"\b(server|proxmox|pve|esxi|vmware|linux|ubuntu|debian|centos|rhel|rocky|alma|alpine|freebsd|windows\s*server|idrac|ilo|ipmi|bmc|cimc|cisco\s+ucs|ucs[-\s]?c?\d{3,4}|ucsc[-\s]?\w+|ucsb[-\s]?\w+|supermicro|poweredge|proliant|raspberry|docker|kubernetes|hypervisor)\b",
            identity,
        )
    )
    if has_server_mib or has_server_identity or ("SNMP" in (sources or []) and ptype == "server"):
        return "server"

    return None


@app.post("/api/snmp/poll-all")
async def trigger_snmp_poll_all() -> Dict[str, str]:
    await _notify_worker("snmp_now")
    return {"status": "triggered", "message": "Immediate SNMP fleet poll queued."}


@app.post("/api/snmp/repair-spikes")
async def trigger_snmp_spike_repair() -> Dict[str, Any]:
    """Scans recent raw SNMP interface metrics across all devices and zeros out counter-spike artifacts."""
    return await repair_implausible_snmp_spikes(_get_pool(), force_refresh=True)


@app.get("/api/snmp/telemetry")
async def get_snmp_telemetry(
    range_key: str = Query(default="24h", alias="range"),
) -> Dict[str, Any]:
    """Returns application-level SNMP ingestion, query latency, OID volume, and per-target health."""
    rk = (range_key or "24h").strip().lower()
    if rk == "1d":
        rk = "24h"
    if rk not in ("1h", "6h", "24h", "7d", "30d"):
        rk = "24h"

    range_specs = {
        "1h": ("1 hour", "1 minute", 60.0),
        "6h": ("6 hours", "5 minutes", 360.0),
        "24h": ("24 hours", "15 minutes", 1440.0),
        "7d": ("7 days", "1 hour", 10080.0),
        "30d": ("30 days", "6 hours", 43200.0),
    }
    sql_interval, sql_bucket, window_mins = range_specs[rk]

    pool = _get_pool()
    async with pool.acquire() as conn:
        poll_buckets = await conn.fetch(
            f"""
            SELECT time_bucket('{sql_bucket}', time) AS bucket,
                   COALESCE(SUM(oids_collected), 0) AS oids_collected,
                   COALESCE(SUM(interfaces_ingested), 0) AS interface_samples,
                   COALESCE(SUM(oid_walks), 0) AS oid_walks,
                   COALESCE(AVG(poll_duration_ms) FILTER (WHERE success), 0) AS avg_latency_ms,
                   COALESCE(MAX(poll_duration_ms), 0) AS max_latency_ms,
                   COUNT(*) FILTER (WHERE success) AS polls_ok,
                   COUNT(*) FILTER (WHERE NOT success) AS polls_failed,
                   COUNT(DISTINCT device_ip) AS devices_polled
            FROM snmp_poll_metrics
            WHERE time > NOW() - INTERVAL '{sql_interval}'
            GROUP BY bucket
            ORDER BY bucket ASC
            """
        )

        raw_buckets = await conn.fetch(
            f"""
            SELECT time_bucket('{sql_bucket}', time) AS bucket,
                   COUNT(*) AS interface_samples,
                   COUNT(DISTINCT device_ip) AS devices_polled
            FROM snmp_metrics_raw
            WHERE time > NOW() - INTERVAL '{sql_interval}'
            GROUP BY bucket
            ORDER BY bucket ASC
            """
        )

        global_row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS total_polls,
                   COUNT(*) FILTER (WHERE success) AS successful_polls,
                   COUNT(*) FILTER (WHERE NOT success) AS failed_polls,
                   COALESCE(SUM(oid_walks), 0) AS total_walks,
                   COALESCE(SUM(oids_collected), 0) AS total_oids_ingested,
                   COALESCE(SUM(interfaces_ingested), 0) AS total_interface_samples,
                   COALESCE(AVG(poll_duration_ms) FILTER (WHERE success), 0) AS avg_latency_ms,
                   COALESCE(percentile_disc(0.95) WITHIN GROUP (ORDER BY poll_duration_ms) FILTER (WHERE success), 0) AS p95_latency_ms,
                   COALESCE(MAX(poll_duration_ms), 0) AS max_latency_ms,
                   MIN(time) AS first_poll_time
            FROM snmp_poll_metrics
            WHERE time > NOW() - INTERVAL '{sql_interval}'
            """
        )

        per_dev_stats_rows = await conn.fetch(
            f"""
            SELECT device_ip,
                   COUNT(*) AS total_polls,
                   COUNT(*) FILTER (WHERE success) AS polls_ok,
                   COUNT(*) FILTER (WHERE NOT success) AS polls_failed,
                   COALESCE(AVG(poll_duration_ms) FILTER (WHERE success), 0) AS avg_latency_ms,
                   COALESCE(AVG(oids_collected) FILTER (WHERE success), 0) AS oids_per_poll,
                   COALESCE(AVG(oid_walks) FILTER (WHERE success), 0) AS walks_per_poll
            FROM snmp_poll_metrics
            WHERE time > NOW() - INTERVAL '{sql_interval}'
            GROUP BY device_ip
            """
        )

        target_rows = await conn.fetch(
            f"""
            SELECT t.device_ip, t.label, t.enabled,
                   COALESCE(p.name, 'Default') AS profile_name,
                   COALESCE(p.version, 'v2c') AS snmp_version,
                   d.sys_name, d.sys_descr, d.sys_object_id, d.vendor_name,
                   d.device_category, d.matched_mibs, d.interface_count,
                   d.is_reachable, d.last_poll_duration_ms, d.last_polled,
                   (d.extended_metrics ? 'ups_telemetry') AS has_ups_telemetry,
                   {_HAS_WIRELESS_SQL_D} AS has_wireless_telemetry,
                   d.extended_metrics -> 'responding_mibs' AS responding_mibs
            FROM snmp_targets_config t
            LEFT JOIN snmp_profiles p ON p.id = t.profile_id
            LEFT JOIN snmp_devices d ON d.device_ip = t.device_ip
            ORDER BY t.id ASC
            """
        )

        raw_rows_24h = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM snmp_metrics_raw WHERE time > NOW() - INTERVAL '24 hours'"
            )
            or 0
        )
        telemetry_id_map = await ensure_device_ids(
            conn, [str(r["device_ip"]).strip() for r in target_rows if r["device_ip"]]
        )

    dev_stats_map = {str(r["device_ip"]): dict(r) for r in per_dev_stats_rows}

    targets_out: List[Dict[str, Any]] = []
    enabled_targets = 0
    reachable_targets = 0
    unreachable_targets = 0
    total_interfaces_tracked = 0
    active_mibs_set: set[str] = set()
    version_counts: Dict[str, int] = {}

    for rec in target_rows:
        r = dict(rec)
        ip = str(r["device_ip"])
        enabled = bool(r.get("enabled", True))
        ver = str(r.get("snmp_version") or "v2c").lower()
        if enabled:
            enabled_targets += 1
            version_counts[ver] = version_counts.get(ver, 0) + 1
            if r.get("is_reachable") is True:
                reachable_targets += 1
            elif r.get("is_reachable") is False:
                unreachable_targets += 1

        raw_matched = r.get("matched_mibs")
        if isinstance(raw_matched, str):
            try:
                raw_matched = json.loads(raw_matched)
            except Exception:
                raw_matched = []
        matched_mibs = raw_matched if isinstance(raw_matched, list) else []

        raw_resp = r.get("responding_mibs")
        if isinstance(raw_resp, str):
            try:
                raw_resp = json.loads(raw_resp)
            except Exception:
                raw_resp = None
        resp_mibs = raw_resp if isinstance(raw_resp, list) else matched_mibs

        for m in resp_mibs:
            active_mibs_set.add(str(m))

        if_cnt = int(r.get("interface_count") or 0)
        if enabled:
            total_interfaces_tracked += if_cnt

        profile_type = _classify_device_profile(
            {
                "device_category": r.get("device_category"),
                "vendor_name": r.get("vendor_name"),
                "sys_descr": r.get("sys_descr"),
                "sys_name": r.get("sys_name") or r.get("label"),
                "matched_mibs": matched_mibs,
                "has_ups_telemetry": bool(r.get("has_ups_telemetry")),
                "has_wireless_telemetry": bool(r.get("has_wireless_telemetry")),
            },
            None,
            None,
        )

        st = dev_stats_map.get(ip, {})
        tot_p = int(st.get("total_polls") or 0)
        ok_p = int(st.get("polls_ok") or 0)
        fail_p = int(st.get("polls_failed") or 0)
        success_pct = (
            round((ok_p / tot_p) * 100.0, 1)
            if tot_p > 0
            else (100.0 if r.get("is_reachable") else 0.0)
        )
        last_lat = float(r.get("last_poll_duration_ms") or 0.0)
        avg_lat = round(float(st.get("avg_latency_ms") or last_lat), 1)

        targets_out.append(
            {
                "device_id": telemetry_id_map.get(ip),
                "device_ip": ip,
                "sys_name": r.get("sys_name") or r.get("label") or ip,
                "vendor_name": r.get("vendor_name") or "Standard SNMP Device",
                "device_category": r.get("device_category") or "Network Device",
                "device_profile_type": profile_type,
                "enabled": enabled,
                "is_reachable": r.get("is_reachable"),
                "snmp_version": ver,
                "profile_name": r.get("profile_name") or "Default",
                "interface_count": if_cnt,
                "responding_mib_count": len(resp_mibs),
                "responding_mibs": resp_mibs,
                "last_poll_duration_ms": round(last_lat, 1),
                "avg_latency_ms": avg_lat,
                "oids_per_poll": round(float(st.get("oids_per_poll") or 0.0), 1),
                "walks_per_poll": round(float(st.get("walks_per_poll") or 0.0), 1),
                "success_rate_pct": success_pct,
                "failed_polls": fail_p,
                "last_polled": r["last_polled"].isoformat() if r.get("last_polled") else None,
            }
        )

    lat_samples = [
        t["last_poll_duration_ms"] for t in targets_out if t["last_poll_duration_ms"] > 0
    ]
    fallback_avg_lat = round(sum(lat_samples) / len(lat_samples), 1) if lat_samples else 0.0

    buckets_by_iso: Dict[str, Dict[str, Any]] = {}
    for b in poll_buckets:
        ts_iso = b["bucket"].isoformat()
        buckets_by_iso[ts_iso] = {
            "time": ts_iso,
            "oids_collected": int(b["oids_collected"] or 0),
            "interface_samples": int(b["interface_samples"] or 0),
            "oid_walks": int(b["oid_walks"] or 0),
            "avg_latency_ms": round(float(b["avg_latency_ms"] or 0.0), 1),
            "max_latency_ms": round(float(b["max_latency_ms"] or 0.0), 1),
            "polls_ok": int(b["polls_ok"] or 0),
            "polls_failed": int(b["polls_failed"] or 0),
            "devices_polled": int(b["devices_polled"] or 0),
        }

    for rb in raw_buckets:
        ts_iso = rb["bucket"].isoformat()
        if ts_iso not in buckets_by_iso:
            devs = int(rb["devices_polled"] or 0)
            samples = int(rb["interface_samples"] or 0)
            buckets_by_iso[ts_iso] = {
                "time": ts_iso,
                "oids_collected": samples,
                "interface_samples": samples,
                "oid_walks": 0,
                "avg_latency_ms": fallback_avg_lat,
                "max_latency_ms": max(lat_samples, default=0.0),
                "polls_ok": devs,
                "polls_failed": 0,
                "devices_polled": devs,
            }

    series = [buckets_by_iso[k] for k in sorted(buckets_by_iso.keys())]

    total_polls = int(global_row["total_polls"] or 0) if global_row else 0
    successful_polls = int(global_row["successful_polls"] or 0) if global_row else 0
    failed_polls = int(global_row["failed_polls"] or 0) if global_row else 0
    total_walks = int(global_row["total_walks"] or 0) if global_row else 0
    total_oids = int(global_row["total_oids_ingested"] or 0) if global_row else 0
    total_if_samples = int(global_row["total_interface_samples"] or 0) if global_row else 0
    avg_lat_ms = float(global_row["avg_latency_ms"] or 0.0) if global_row else 0.0
    p95_lat_ms = float(global_row["p95_latency_ms"] or 0.0) if global_row else 0.0
    max_lat_ms = float(global_row["max_latency_ms"] or 0.0) if global_row else 0.0

    # Immediate fallback metrics from snmp_metrics_raw & snmp_devices right after initial upgrade
    if total_polls == 0 and series:
        total_if_samples = sum(pt["interface_samples"] for pt in series)
        total_oids = total_if_samples
        successful_polls = sum(pt["polls_ok"] for pt in series)
        total_polls = successful_polls
        avg_lat_ms = fallback_avg_lat
        p95_lat_ms = max(lat_samples, default=0.0)
        max_lat_ms = p95_lat_ms

    elapsed_mins = window_mins
    first_poll_time = global_row["first_poll_time"] if global_row else None
    if first_poll_time is not None:
        dt_mins = (datetime.now(timezone.utc) - first_poll_time).total_seconds() / 60.0
        elapsed_mins = max(1.0, min(window_mins, dt_mins + 1.0))

    success_rate_pct = (
        round((successful_polls / total_polls) * 100.0, 1)
        if total_polls > 0
        else (100.0 if reachable_targets > 0 else 0.0)
    )

    return {
        "range": rk,
        "summary": {
            "enabled_targets": enabled_targets,
            "reachable_targets": reachable_targets,
            "unreachable_targets": unreachable_targets,
            "total_polls": total_polls,
            "successful_polls": successful_polls,
            "failed_polls": failed_polls,
            "success_rate_pct": success_rate_pct,
            "total_walks": total_walks,
            "total_oids_ingested": total_oids,
            "total_interface_samples": total_if_samples,
            "oids_per_min": round(total_oids / elapsed_mins, 1) if total_oids > 0 else 0.0,
            "avg_latency_ms": round(avg_lat_ms, 1),
            "p95_latency_ms": round(p95_lat_ms, 1),
            "max_latency_ms": round(max_lat_ms, 1),
            "total_interfaces_tracked": total_interfaces_tracked,
            "raw_table_rows_24h": raw_rows_24h,
            "active_mibs_fleet": sorted(active_mibs_set),
            "version_breakdown": version_counts,
        },
        "series": series,
        "targets": targets_out,
    }


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
        c_in_raw = _clean_bps_sql("in_bps")
        c_out_raw = _clean_bps_sql("out_bps")
        c_in_raw_z = _clean_bps_sql("in_bps", coalesce_zero=True)
        c_out_raw_z = _clean_bps_sql("out_bps", coalesce_zero=True)
        c_avg_in = _clean_bps_sql("avg_in_bps")
        c_max_in = _clean_bps_sql("max_in_bps")
        c_p95_in = _clean_bps_sql("p95_in_bps")
        c_avg_out = _clean_bps_sql("avg_out_bps")
        c_max_out = _clean_bps_sql("max_out_bps")
        c_p95_out = _clean_bps_sql("p95_out_bps")

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
                               AVG({c_in_raw}) AS if_avg_in,
                               MAX({c_in_raw}) AS if_max_in,
                               AVG({c_out_raw}) AS if_avg_out,
                               MAX({c_out_raw}) AS if_max_out,
                               AVG(cpu_usage) AS if_cpu
                        FROM snmp_metrics_raw
                        WHERE device_ip = $1
                          AND time > NOW() - INTERVAL '{sql_interval}'
                          AND {_NON_LOOPBACK_SQL}
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
                           SUM({c_avg_in}) AS avg_in_bps,
                           SUM({c_max_in}) AS max_in_bps,
                           SUM({c_p95_in}) AS p95_in_bps,
                           SUM({c_avg_out}) AS avg_out_bps,
                           SUM({c_max_out}) AS max_out_bps,
                           SUM({c_p95_out}) AS p95_out_bps,
                           AVG(avg_cpu_usage) AS avg_cpu_usage
                    FROM snmp_metrics_hourly
                    WHERE device_ip = $1
                      AND bucket > NOW() - INTERVAL '{sql_interval}'
                      AND {_NON_LOOPBACK_SQL}
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
                           SUM({c_avg_in}) AS avg_in_bps,
                           SUM({c_max_in}) AS max_in_bps,
                           SUM({c_p95_in}) AS p95_in_bps,
                           SUM({c_avg_out}) AS avg_out_bps,
                           SUM({c_max_out}) AS max_out_bps,
                           SUM({c_p95_out}) AS p95_out_bps,
                           AVG(avg_cpu_usage) AS avg_cpu_usage
                    FROM snmp_metrics_daily
                    WHERE device_ip = $1
                      {where_time}
                      AND {_NON_LOOPBACK_SQL}
                    GROUP BY bucket
                    ORDER BY bucket ASC
                    """,
                    device_ip,
                )
                source_table = "snmp_metrics_daily (All Interfaces Combined)"
        else:
            c_avg_in_z = _clean_bps_sql("avg_in_bps", coalesce_zero=True)
            c_max_in_z = _clean_bps_sql("max_in_bps", coalesce_zero=True)
            c_p95_in_z = _clean_bps_sql("p95_in_bps", coalesce_zero=True)
            c_avg_out_z = _clean_bps_sql("avg_out_bps", coalesce_zero=True)
            c_max_out_z = _clean_bps_sql("max_out_bps", coalesce_zero=True)
            c_p95_out_z = _clean_bps_sql("p95_out_bps", coalesce_zero=True)

            if rk in ("1h", "6h", "24h"):
                sql_interval = interval_map[rk]
                rows = await conn.fetch(
                    f"""
                    SELECT time AS bucket,
                           {c_in_raw_z} AS avg_in_bps,
                           {c_in_raw_z} AS max_in_bps,
                           {c_in_raw_z} AS p95_in_bps,
                           {c_out_raw_z} AS avg_out_bps,
                           {c_out_raw_z} AS max_out_bps,
                           {c_out_raw_z} AS p95_out_bps,
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
                           {c_avg_in_z} AS avg_in_bps,
                           {c_max_in_z} AS max_in_bps,
                           {c_p95_in_z} AS p95_in_bps,
                           {c_avg_out_z} AS avg_out_bps,
                           {c_max_out_z} AS max_out_bps,
                           {c_p95_out_z} AS p95_out_bps,
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
                           {c_avg_in_z} AS avg_in_bps,
                           {c_max_in_z} AS max_in_bps,
                           {c_p95_in_z} AS p95_in_bps,
                           {c_avg_out_z} AS avg_out_bps,
                           {c_max_out_z} AS max_out_bps,
                           {c_p95_out_z} AS p95_out_bps,
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
        host_id_map = await ensure_device_ids(
            conn, [str(r["ip_address"]).strip() for r in rows if r["ip_address"]]
        )

    hosts = []
    for r in rows:
        d = dict(r)
        d["device_id"] = host_id_map.get(str(d.get("ip_address") or "").strip())
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


async def _resolve_device_ref(
    conn: asyncpg.Connection, device_ref: str
) -> Tuple[Optional[int], str]:
    """Resolves either a numeric device_id or an IP address into (device_id, ip_address)."""
    ref = str(device_ref or "").strip()
    if not ref:
        raise HTTPException(status_code=404, detail="Device reference is required.")

    if ref.isdigit():
        row = await conn.fetchrow(
            "SELECT id, ip_address FROM device_registry WHERE id = $1",
            int(ref),
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"Device ID {ref} not found.")
        return int(row["id"]), str(row["ip_address"])

    try:
        norm_ip = str(ipaddress.ip_address(ref))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown device reference: {ref}") from exc

    existing_id = await conn.fetchval(
        "SELECT id FROM device_registry WHERE ip_address = $1",
        norm_ip,
    )
    if existing_id is not None:
        return int(existing_id), norm_ip

    known = await conn.fetchval(
        """
        SELECT (
            EXISTS (SELECT 1 FROM discovered_hosts WHERE host(ip_address) = $1)
            OR EXISTS (SELECT 1 FROM snmp_targets_config WHERE device_ip = $1)
            OR EXISTS (SELECT 1 FROM snmp_devices WHERE device_ip = $1)
            OR EXISTS (SELECT 1 FROM icmp_targets_config WHERE device_ip = $1)
            OR EXISTS (SELECT 1 FROM unifi_devices WHERE ip_address = $1 OR lan_ip = $1)
            OR EXISTS (SELECT 1 FROM unifi_clients WHERE ip_address = $1)
            OR EXISTS (SELECT 1 FROM device_logs WHERE source_ip = $1 AND time > NOW() - INTERVAL '30 days')
        )
        """,
        norm_ip,
    )
    if known:
        id_map = await ensure_device_ids(conn, [norm_ip])
        return id_map.get(norm_ip), norm_ip

    return None, norm_ip


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
            f"""
            SELECT device_ip, sys_name, sys_descr, sys_object_id, sys_location, sys_contact,
                   sys_uptime_ticks, cpu_usage_pct, mem_usage_pct, temp_celsius, interface_count,
                   vendor_name, device_category, matched_mibs, hardware_model, serial_number,
                   firmware_version, mac_address, subnet_cidr, first_polled,
                   (extended_metrics ? 'ups_telemetry') AS has_ups_telemetry,
                   {_HAS_WIRELESS_SQL} AS has_wireless_telemetry,
                   is_reachable, last_poll_duration_ms, last_polled
            FROM snmp_devices
            """
        )

        unifi_dev_rows = await conn.fetch(
            """
            SELECT mac, ip_address, lan_ip, name, model, device_type, serial_number, firmware_version,
                   status, uptime_sec, cpu_usage_pct, mem_usage_pct, temp_celsius,
                   poe_total_watts, num_sta, last_seen
            FROM unifi_devices
            WHERE (ip_address IS NOT NULL AND ip_address != '')
               OR (lan_ip IS NOT NULL AND lan_ip != '')
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

        icmp_rows = await conn.fetch(
            """
            SELECT id, device_ip, label, poll_interval, enabled, resolved_hostname,
                   is_reachable, last_rtt_ms, packet_loss_pct, status_since, last_polled
            FROM icmp_targets_config
            """
        )

        icmp_uptime_rows = await conn.fetch(
            """
            SELECT device_ip,
                   ROUND(
                       (COUNT(*) FILTER (WHERE is_reachable) * 100.0 / NULLIF(COUNT(*), 0))::numeric,
                       1
                   )::double precision AS uptime_pct_24h
            FROM icmp_metrics_raw
            WHERE time > NOW() - INTERVAL '24 hours'
            GROUP BY device_ip
            """
        )
        icmp_uptime_map = {
            str(r["device_ip"]): float(r["uptime_pct_24h"])
            for r in icmp_uptime_rows
            if r["uptime_pct_24h"] is not None
        }

        all_dev_ips: List[str] = []
        for r in disc_rows:
            if r["ip_address"]:
                all_dev_ips.append(str(r["ip_address"]).strip())
        for r in snmp_cfg_rows:
            if r["device_ip"]:
                all_dev_ips.append(str(r["device_ip"]).strip())
        for r in snmp_dev_rows:
            if r["device_ip"]:
                all_dev_ips.append(str(r["device_ip"]).strip())
        for r in icmp_rows:
            if r["device_ip"]:
                all_dev_ips.append(str(r["device_ip"]).strip())
        for r in unifi_dev_rows:
            for cand_ip in (r["lan_ip"], r["ip_address"]):
                c_ip = str(cand_ip or "").strip()
                if c_ip:
                    all_dev_ips.append(c_ip)
        for r in unifi_cli_rows:
            if r["ip_address"]:
                all_dev_ips.append(str(r["ip_address"]).strip())
        for r in log_counts:
            if r["source_ip"]:
                all_dev_ips.append(str(r["source_ip"]).strip())
        dev_id_map = await ensure_device_ids(conn, all_dev_ips)
        reg_rows = await conn.fetch(
            """
            SELECT ip_address, custom_name, custom_location, custom_device_type, notes
            FROM device_registry
            WHERE custom_name IS NOT NULL
               OR custom_location IS NOT NULL
               OR custom_device_type IS NOT NULL
               OR notes IS NOT NULL
            """
        )
        reg_overrides = {str(r["ip_address"]): dict(r) for r in reg_rows if r["ip_address"]}

    by_ip: Dict[str, Dict[str, Any]] = {}

    def _ensure_ip(ip_str: str) -> Dict[str, Any]:
        if ip_str not in by_ip:
            by_ip[ip_str] = {
                "device_id": dev_id_map.get(ip_str),
                "ip_address": ip_str,
                "hostname": ip_str,
                "mac_address": None,
                "vendor": None,
                "hardware_model": None,
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
                "is_unifi_client": False,
                "dns_hostname": None,
                "icmp_enabled": False,
                "icmp_reachable": None,
                "icmp_rtt_ms": None,
                "icmp_uptime_pct_24h": None,
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
        if r.get("mac_address") and not item["mac_address"]:
            item["mac_address"] = r["mac_address"]
        if r.get("subnet_cidr") and not item["subnet_cidr"]:
            item["subnet_cidr"] = r["subnet_cidr"]
        item["snmp_reachable"] = bool(r.get("is_reachable"))
        if r.get("is_reachable"):
            item["is_online"] = True
        if r.get("hardware_model"):
            item["hardware_model"] = r.get("hardware_model")
        if r.get("vendor_name") and not item["vendor"]:
            item["vendor"] = r["vendor_name"]
        item["device_category"] = r.get("device_category") or item["device_category"]
        if r.get("has_ups_telemetry"):
            item["has_ups_telemetry"] = True
        if r.get("has_wireless_telemetry"):
            item["has_wireless_telemetry"] = True
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
        cand_ips: List[str] = []
        for raw_ip in (r["lan_ip"], r["ip_address"]):
            ip_val = str(raw_ip or "").strip()
            if ip_val and ip_val not in cand_ips:
                cand_ips.append(ip_val)
        for ip_str in cand_ips:
            item = _ensure_ip(ip_str)
            if r["name"]:
                item["hostname"] = r["name"]
            item["mac_address"] = r["mac"] or item["mac_address"]
            if r["model"]:
                item["hardware_model"] = item["hardware_model"] or str(r["model"]).strip() or None
            item["vendor"] = item["vendor"] or "Ubiquiti"
            if r["status"] == "online":
                item["is_online"] = True
            item["cpu_usage_pct"] = float(r["cpu_usage_pct"] or 0.0)
            item["uptime_sec"] = int(r["uptime_sec"] or 0)
            if r["device_type"]:
                item["_unifi_device_type"] = str(r["device_type"])
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
        item["is_unifi_client"] = True
        if "UniFi" not in item["sources"] and "UniFi Client" not in item["sources"]:
            item["sources"].append("UniFi Client")

    for r in log_counts:
        ip_str = str(r["source_ip"])
        item = _ensure_ip(ip_str)
        item["log_count_24h"] = int(r["log_count_24h"] or 0)
        if "Syslog" not in item["sources"]:
            item["sources"].append("Syslog")

    now_utc = datetime.now(timezone.utc)
    for r in icmp_rows:
        ip_str = str(r["device_ip"])
        item = _ensure_ip(ip_str)
        dns_name = str(r["resolved_hostname"] or "").strip() or None
        label_name = str(r["label"] or "").strip() or None
        if dns_name:
            item["dns_hostname"] = dns_name
        if label_name and (item["hostname"] == ip_str or not item["hostname"]):
            item["hostname"] = label_name
        elif dns_name and (item["hostname"] == ip_str or not item["hostname"]):
            item["hostname"] = dns_name
        item["icmp_enabled"] = bool(r["enabled"])
        item["icmp_reachable"] = bool(r["is_reachable"]) if r["last_polled"] else None
        if r["last_rtt_ms"] is not None:
            item["icmp_rtt_ms"] = round(float(r["last_rtt_ms"]), 2)
        item["icmp_uptime_pct_24h"] = icmp_uptime_map.get(ip_str)
        if r["is_reachable"]:
            item["is_online"] = True
            if item["uptime_sec"] is None and r["status_since"]:
                delta_sec = int((now_utc - r["status_since"]).total_seconds())
                if delta_sec >= 0:
                    item["uptime_sec"] = delta_sec
        if "ICMP" not in item["sources"]:
            item["sources"].append("ICMP")
        if r["last_polled"] and not item["last_seen"]:
            item["last_seen"] = r["last_polled"].isoformat()

    # Auto-match vendor MIB profile for any previously discovered device missing matched_mibs
    for item in by_ip.values():
        is_unifi_infra = bool(item.get("_unifi_device_type"))
        has_snmp = "SNMP" in (item.get("sources") or [])
        has_icmp = "ICMP" in (item.get("sources") or [])
        is_client_only = bool(item.get("is_unifi_client")) and not is_unifi_infra and not has_snmp
        is_icmp_only = has_icmp and not has_snmp and not is_unifi_infra and not bool(item.get("is_unifi_client"))

        if not is_client_only and not is_icmp_only and not item.get("matched_mibs"):
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

        unifi_hint = (
            {"device_type": item.get("_unifi_device_type")}
            if is_unifi_infra
            else None
        )
        if is_client_only:
            item["device_profile_type"] = "client"
            item["device_role"] = "client"
        elif is_icmp_only:
            item["device_profile_type"] = "icmp"
            item["device_role"] = "icmp"
        else:
            item["device_profile_type"] = _classify_device_profile(
                {
                    "device_category": item.get("device_category"),
                    "vendor_name": item.get("vendor"),
                    "hardware_model": item.get("hardware_model"),
                    "sys_descr": item.get("sys_descr"),
                    "sys_name": item.get("hostname"),
                    "matched_mibs": item.get("matched_mibs") or [],
                    "has_ups_telemetry": bool(item.get("has_ups_telemetry")),
                    "has_wireless_telemetry": bool(item.get("has_wireless_telemetry")),
                },
                unifi_hint,
                None,
            )
            item["device_role"] = _classify_device_role(
                item["device_profile_type"],
                {
                    "device_category": item.get("device_category"),
                    "vendor_name": item.get("vendor"),
                    "hardware_model": item.get("hardware_model"),
                    "sys_descr": item.get("sys_descr"),
                    "sys_name": item.get("hostname"),
                    "matched_mibs": item.get("matched_mibs") or [],
                },
                unifi_hint,
                sources=item.get("sources") or [],
                interface_count=int(item.get("interface_count") or 0),
                is_unifi_client=bool(item.get("is_unifi_client")),
            )

        ovr = reg_overrides.get(item["ip_address"])
        if ovr:
            if ovr.get("custom_name"):
                item["hostname"] = str(ovr["custom_name"]).strip()
            if ovr.get("custom_location"):
                item["sys_location"] = str(ovr["custom_location"]).strip()
            ovr_type = str(ovr.get("custom_device_type") or "").strip().lower()
            if ovr_type in _VALID_CUSTOM_DEVICE_TYPES:
                item["device_role"] = ovr_type
                item["device_profile_type"] = _role_to_profile_type(ovr_type)
            if ovr.get("notes"):
                item["notes"] = str(ovr["notes"])

        item["is_unifi_client"] = bool(item.get("is_unifi_client"))
        item.pop("has_ups_telemetry", None)
        item.pop("has_wireless_telemetry", None)
        item.pop("_unifi_device_type", None)

    def _ip_sort_key(d: Dict[str, Any]):
        try:
            return (0, int(ipaddress.ip_address(d["ip_address"])))
        except ValueError:
            return (1, d["ip_address"])

    devices_list = sorted(by_ip.values(), key=_ip_sort_key)
    return {"devices": devices_list}


@app.get("/api/devices/{device_ref}")
async def get_device_detail(device_ref: str) -> Dict[str, Any]:
    """Returns all polled and correlated information for a specific device ID or IP."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        device_id, device_ip = await _resolve_device_ref(conn, device_ref)
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
            SELECT t.id AS target_id, t.device_ip, t.label, t.profile_id, t.enabled, t.created_at,
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
                   firmware_version, mac_address, subnet_cidr, first_polled,
                   extended_metrics, is_reachable, last_poll_duration_ms, last_polled
            FROM snmp_devices
            WHERE device_ip = $1
            """,
            device_ip,
        )

        c_dev_in_z = _clean_bps_sql("in_bps", coalesce_zero=True)
        c_dev_out_z = _clean_bps_sql("out_bps", coalesce_zero=True)
        snmp_if_rows = await conn.fetch(
            f"""
            SELECT DISTINCT ON (if_index)
                if_index, if_name, if_descr, if_alias, if_speed_bps, if_oper_status,
                in_octets, out_octets,
                {c_dev_in_z} AS in_bps,
                {c_dev_out_z} AS out_bps,
                in_errors, out_errors,
                in_discards, out_discards, cpu_usage, sys_uptime, time
            FROM snmp_metrics_raw
            WHERE device_ip = $1
            ORDER BY if_index ASC, time DESC
            """,
            device_ip,
        )

        unifi_dev_row = await conn.fetchrow(
            """
            SELECT mac, ip_address, lan_ip, name, model, device_type, serial_number, firmware_version,
                   status, uptime_sec, cpu_usage_pct, mem_usage_pct, temp_celsius,
                   poe_total_watts, poe_max_watts, num_sta, rx_bps, tx_bps, port_table, last_seen
            FROM unifi_devices
            WHERE ip_address = $1 OR lan_ip = $1
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

        cfg_subnets = await conn.fetch(
            """
            SELECT subnet_cidr, label, vlan_id
            FROM scan_subnets_config
            WHERE enabled = TRUE
            ORDER BY id ASC
            """
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

        reg_row = await conn.fetchrow(
            """
            SELECT custom_name, custom_location, custom_device_type, notes
            FROM device_registry
            WHERE ip_address = $1
            """,
            device_ip,
        )

        icmp_row = await conn.fetchrow(
            """
            SELECT id, device_ip, label, poll_interval, enabled, resolved_hostname,
                   is_reachable, last_rtt_ms, packet_loss_pct, status_since, last_polled,
                   created_at
            FROM icmp_targets_config
            WHERE device_ip = $1
            """,
            device_ip,
        )
        icmp_uptime_row = await conn.fetchrow(
            """
            SELECT
                ROUND(
                    (COUNT(*) FILTER (WHERE time > NOW() - INTERVAL '24 hours' AND is_reachable) * 100.0
                     / NULLIF(COUNT(*) FILTER (WHERE time > NOW() - INTERVAL '24 hours'), 0))::numeric,
                    1
                )::double precision AS uptime_pct_24h,
                ROUND(
                    (COUNT(*) FILTER (WHERE is_reachable) * 100.0
                     / NULLIF(COUNT(*), 0))::numeric,
                    1
                )::double precision AS uptime_pct_7d,
                COUNT(*) FILTER (WHERE time > NOW() - INTERVAL '24 hours') AS samples_24h
            FROM icmp_metrics_raw
            WHERE device_ip = $1
              AND time > NOW() - INTERVAL '7 days'
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
    snmp_cfg_dict = _serialize(snmp_cfg_row)
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

    matched_cfg_subnet: Optional[str] = None
    matched_cfg_vlan: Optional[int] = None
    try:
        ip_obj = ipaddress.IPv4Address(device_ip)
        for s_row in cfg_subnets:
            cidr_str = str(s_row["subnet_cidr"] or "").strip()
            if not cidr_str:
                continue
            try:
                if ip_obj in ipaddress.IPv4Network(cidr_str, strict=False):
                    matched_cfg_subnet = cidr_str
                    matched_cfg_vlan = s_row["vlan_id"]
                    break
            except ValueError:
                continue
    except ValueError:
        pass

    def _first_non_ip_hostname() -> str:
        for cand in (
            (host_dict or {}).get("hostname"),
            (snmp_sys_dict or {}).get("sys_name"),
            (unifi_dev_dict or {}).get("name"),
            (unifi_cli_dict or {}).get("hostname"),
            (snmp_cfg_dict or {}).get("label"),
        ):
            s = str(cand or "").strip()
            if s and s != device_ip:
                return s
        return device_ip

    def _pick_vendor() -> Optional[str]:
        for cand in (
            (host_dict or {}).get("vendor"),
            (unifi_cli_dict or {}).get("oui_vendor"),
            (snmp_sys_dict or {}).get("vendor_name"),
            "Ubiquiti Networks" if unifi_dev_dict else None,
        ):
            s = str(cand or "").strip()
            if s:
                return s
        fb = str((fallback_mib_match or {}).get("vendor_name") or "").strip()
        if fb and fb not in ("Standard SNMP Device", "Generic / Standard RFC") and not fb.startswith("Enterprise PEN #"):
            return fb
        return None

    def _pick_ts(candidates: List[Optional[str]], pick_min: bool) -> Optional[str]:
        parsed: List[tuple[datetime, str]] = []
        for raw_ts in candidates:
            if not raw_ts:
                continue
            try:
                dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                parsed.append((dt, str(raw_ts)))
            except ValueError:
                continue
        if not parsed:
            return None
        chosen = min(parsed, key=lambda x: x[0]) if pick_min else max(parsed, key=lambda x: x[0])
        return chosen[1]

    src_parts: List[str] = []
    if (host_dict or {}).get("discovery_source"):
        src_parts.append(str(host_dict["discovery_source"]))
    if snmp_sys_dict and not any("snmp" in p.lower() for p in src_parts):
        src_parts.append("snmp")
    if (unifi_dev_dict or unifi_cli_dict) and not any("unifi" in p.lower() for p in src_parts):
        src_parts.append("unifi")

    resolved_identity = {
        "hostname": _first_non_ip_hostname(),
        "mac_address": (
            (host_dict or {}).get("mac_address")
            or (unifi_dev_dict or {}).get("mac")
            or (unifi_cli_dict or {}).get("mac")
            or (snmp_sys_dict or {}).get("mac_address")
            or None
        ),
        "vendor": _pick_vendor(),
        "subnet_cidr": (
            (host_dict or {}).get("subnet_cidr")
            or (snmp_sys_dict or {}).get("subnet_cidr")
            or matched_cfg_subnet
            or None
        ),
        "vlan_id": (
            (host_dict or {}).get("vlan_id")
            if (host_dict or {}).get("vlan_id") is not None
            else (
                (unifi_cli_dict or {}).get("vlan_id")
                if (unifi_cli_dict or {}).get("vlan_id") is not None
                else matched_cfg_vlan
            )
        ),
        "discovery_source": " / ".join(src_parts) if src_parts else "manual",
        "first_seen": _pick_ts(
            [
                (host_dict or {}).get("first_seen"),
                (snmp_sys_dict or {}).get("first_polled"),
                (snmp_cfg_dict or {}).get("created_at"),
            ],
            pick_min=True,
        ),
        "last_seen": _pick_ts(
            [
                (host_dict or {}).get("last_seen"),
                (snmp_sys_dict or {}).get("last_polled"),
                (unifi_dev_dict or {}).get("last_seen"),
                (unifi_cli_dict or {}).get("last_seen"),
            ],
            pick_min=False,
        ),
    }

    icmp_host_dict: Optional[Dict[str, Any]] = None
    if icmp_row:
        ih = dict(icmp_row)
        uptime_sec_val: Optional[int] = None
        if ih.get("is_reachable") and ih.get("status_since"):
            delta_s = int((datetime.now(timezone.utc) - ih["status_since"]).total_seconds())
            if delta_s >= 0:
                uptime_sec_val = delta_s
        icmp_host_dict = {
            "id": ih.get("id"),
            "device_ip": ih.get("device_ip"),
            "label": ih.get("label") or "",
            "poll_interval": int(ih.get("poll_interval") or 30),
            "enabled": bool(ih.get("enabled")),
            "resolved_hostname": str(ih.get("resolved_hostname") or "").strip() or None,
            "is_reachable": bool(ih.get("is_reachable")) if ih.get("last_polled") else None,
            "last_rtt_ms": round(float(ih["last_rtt_ms"]), 2) if ih.get("last_rtt_ms") is not None else None,
            "packet_loss_pct": round(float(ih["packet_loss_pct"]), 1) if ih.get("packet_loss_pct") is not None else 0.0,
            "status_since": ih["status_since"].isoformat() if ih.get("status_since") else None,
            "uptime_sec": uptime_sec_val,
            "uptime_pct_24h": float(icmp_uptime_row["uptime_pct_24h"]) if icmp_uptime_row and icmp_uptime_row["uptime_pct_24h"] is not None else None,
            "uptime_pct_7d": float(icmp_uptime_row["uptime_pct_7d"]) if icmp_uptime_row and icmp_uptime_row["uptime_pct_7d"] is not None else None,
            "samples_24h": int(icmp_uptime_row["samples_24h"] or 0) if icmp_uptime_row else 0,
            "last_polled": ih["last_polled"].isoformat() if ih.get("last_polled") else None,
            "created_at": ih["created_at"].isoformat() if ih.get("created_at") else None,
        }
        resolved_identity["first_seen"] = _pick_ts(
            [resolved_identity.get("first_seen"), icmp_host_dict.get("created_at")],
            pick_min=True,
        )
        resolved_identity["last_seen"] = _pick_ts(
            [resolved_identity.get("last_seen"), icmp_host_dict.get("last_polled")],
            pick_min=False,
        )
        if icmp_host_dict["resolved_hostname"]:
            resolved_identity["dns_hostname"] = icmp_host_dict["resolved_hostname"]
            if resolved_identity["hostname"] == device_ip:
                resolved_identity["hostname"] = icmp_host_dict["resolved_hostname"]
        elif icmp_host_dict["label"] and resolved_identity["hostname"] == device_ip:
            resolved_identity["hostname"] = icmp_host_dict["label"]

    is_client_only = (
        bool(unifi_cli_dict)
        and not bool(unifi_dev_dict)
        and not bool(snmp_cfg_dict)
        and not bool(snmp_sys_dict)
    )
    is_icmp_only = (
        bool(icmp_host_dict)
        and not bool(snmp_cfg_dict)
        and not bool(snmp_sys_dict)
        and not bool(unifi_dev_dict)
        and not bool(unifi_cli_dict)
    )
    if is_client_only:
        device_profile_type = "client"
        device_role = "client"
    elif is_icmp_only:
        device_profile_type = "icmp"
        device_role = "icmp"
    else:
        device_profile_type = _classify_device_profile(
            snmp_sys_dict, unifi_dev_dict, fallback_mib_match
        )
        detail_sources: List[str] = []
        if snmp_cfg_dict or snmp_sys_dict:
            detail_sources.append("SNMP")
        device_role = _classify_device_role(
            device_profile_type,
            snmp_sys_dict or resolved_identity,
            unifi_dev_dict,
            sources=detail_sources,
            interface_count=len(snmp_if_rows),
            is_unifi_client=bool(unifi_cli_dict),
        )

    reg_dict = dict(reg_row) if reg_row else {}
    custom_name = str(reg_dict.get("custom_name") or "").strip() or None
    custom_location = str(reg_dict.get("custom_location") or "").strip() or None
    raw_custom_type = str(reg_dict.get("custom_device_type") or "").strip().lower()
    custom_device_type = raw_custom_type if raw_custom_type in _VALID_CUSTOM_DEVICE_TYPES else None
    notes_val = str(reg_dict.get("notes") or "") or None

    if custom_name:
        resolved_identity["hostname"] = custom_name
    if custom_location:
        resolved_identity["location"] = custom_location
        if isinstance(snmp_sys_dict, dict):
            snmp_sys_dict["sys_location"] = custom_location
    if custom_device_type:
        device_role = custom_device_type
        device_profile_type = _role_to_profile_type(custom_device_type)

    device_settings_out = {
        "custom_name": custom_name,
        "custom_location": custom_location,
        "custom_device_type": custom_device_type,
        "notes": notes_val,
        "snmp_enabled": bool(snmp_cfg_dict.get("enabled")) if snmp_cfg_dict else False,
        "snmp_configured": bool(snmp_cfg_dict),
        "snmp_profile_id": snmp_cfg_dict.get("profile_id") if snmp_cfg_dict else None,
    }

    return {
        "device_id": device_id,
        "ip_address": device_ip,
        "device_profile_type": device_profile_type,
        "device_role": device_role,
        "device_settings": device_settings_out,
        "discovered_host": host_dict,
        "snmp_config": snmp_cfg_dict,
        "snmp_system": snmp_sys_dict,
        "icmp_host": icmp_host_dict,
        "matched_mib_profile": fallback_mib_match,
        "resolved_identity": resolved_identity,
        "snmp_interfaces": [_serialize(r) for r in snmp_if_rows],
        "unifi_device": unifi_dev_dict,
        "unifi_client": unifi_cli_dict,
        "recent_logs": [_serialize(r) for r in log_rows],
    }


@app.put("/api/devices/{device_ref}/settings")
async def update_device_settings(
    device_ref: str,
    payload: DeviceSettingsPayload,
) -> Dict[str, Any]:
    """Updates per-device name/location/type/notes overrides and SNMP polling enable/disable state."""
    pool = _get_pool()
    custom_name = (payload.custom_name or "").strip() or None
    custom_location = (payload.custom_location or "").strip() or None
    notes_val = (payload.notes or "").strip() or None
    raw_type = (payload.custom_device_type or "").strip().lower()
    if raw_type in ("", "auto"):
        custom_device_type: Optional[str] = None
    elif raw_type in _VALID_CUSTOM_DEVICE_TYPES:
        custom_device_type = raw_type
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported device type override: {raw_type}",
        )

    snmp_changed = False
    trigger_poll_now = False
    async with pool.acquire() as conn:
        device_id, device_ip = await _resolve_device_ref(conn, device_ref)
        await ensure_device_ids(conn, [device_ip])
        await conn.execute(
            """
            UPDATE device_registry
            SET custom_name = $2,
                custom_location = $3,
                custom_device_type = $4,
                notes = $5
            WHERE ip_address = $1
            """,
            device_ip,
            custom_name,
            custom_location,
            custom_device_type,
            notes_val,
        )

        if payload.snmp_enabled is not None:
            existing_target = await conn.fetchrow(
                "SELECT id, profile_id, label, enabled FROM snmp_targets_config WHERE device_ip = $1",
                device_ip,
            )
            if existing_target:
                new_profile_id = (
                    payload.snmp_profile_id
                    if payload.snmp_profile_id is not None
                    else existing_target["profile_id"]
                )
                new_label = custom_name if custom_name is not None else (existing_target["label"] or "")
                await conn.execute(
                    """
                    UPDATE snmp_targets_config
                    SET enabled = $2,
                        profile_id = $3,
                        label = $4
                    WHERE id = $1
                    """,
                    existing_target["id"],
                    bool(payload.snmp_enabled),
                    new_profile_id,
                    new_label,
                )
                snmp_changed = True
                if payload.snmp_enabled and not existing_target["enabled"]:
                    trigger_poll_now = True
            elif payload.snmp_enabled:
                chosen_profile_id = payload.snmp_profile_id
                if chosen_profile_id is None:
                    chosen_profile_id = await conn.fetchval(
                        "SELECT id FROM snmp_profiles ORDER BY id ASC LIMIT 1"
                    )
                await conn.execute(
                    """
                    INSERT INTO snmp_targets_config (device_ip, label, profile_id, enabled)
                    VALUES ($1, $2, $3, TRUE)
                    """,
                    device_ip,
                    custom_name or device_ip,
                    chosen_profile_id,
                )
                snmp_changed = True
                trigger_poll_now = True

        target_ref = str(device_id) if device_id is not None else device_ip

    if snmp_changed:
        await _notify_worker("reload_config")
        if trigger_poll_now:
            await _notify_worker("snmp_now")

    return await get_device_detail(target_ref)


@app.delete("/api/devices/{device_ref}")
async def remove_device(device_ref: str) -> Dict[str, Any]:
    """Removes a device from discovered hosts, SNMP targets/inventory, UniFi caches, and device registry."""
    pool = _get_pool()
    removed_snmp_target = False
    async with pool.acquire() as conn:
        device_id, device_ip = await _resolve_device_ref(conn, device_ref)
        res_tgt = await conn.execute(
            "DELETE FROM snmp_targets_config WHERE device_ip = $1",
            device_ip,
        )
        if res_tgt and not res_tgt.endswith(" 0"):
            removed_snmp_target = True
        await conn.execute("DELETE FROM snmp_devices WHERE device_ip = $1", device_ip)
        await conn.execute("DELETE FROM icmp_targets_config WHERE device_ip = $1", device_ip)
        await conn.execute("DELETE FROM discovered_hosts WHERE host(ip_address) = $1", device_ip)
        await conn.execute(
            "DELETE FROM unifi_devices WHERE ip_address = $1 OR lan_ip = $1",
            device_ip,
        )
        await conn.execute("DELETE FROM unifi_clients WHERE ip_address = $1", device_ip)
        await conn.execute("DELETE FROM device_registry WHERE ip_address = $1", device_ip)

    if removed_snmp_target:
        await _notify_worker("reload_config")

    return {
        "status": "deleted",
        "device_id": device_id,
        "ip_address": device_ip,
        "removed_snmp_target": removed_snmp_target,
    }


@app.get("/api/devices/{device_ref}/ups-history")
async def get_device_ups_history(
    device_ref: str,
    range_key: str = Query(default="24h", alias="range"),
) -> Dict[str, Any]:
    """Returns time-series battery, load, runtime, voltage, and wattage telemetry for a UPS device."""
    rk = (range_key or "24h").strip().lower()
    if rk == "1d":
        rk = "24h"
    if rk not in ("1h", "6h", "24h", "7d", "30d"):
        rk = "24h"

    range_map = {
        "1h": ("1 hour", None),
        "6h": ("6 hours", None),
        "24h": ("24 hours", "5 minutes"),
        "7d": ("7 days", "15 minutes"),
        "30d": ("30 days", "1 hour"),
    }
    sql_interval, bucket_size = range_map[rk]

    pool = _get_pool()
    async with pool.acquire() as conn:
        device_id, device_ip = await _resolve_device_ref(conn, device_ref)
        if bucket_size is None:
            rows = await conn.fetch(
                f"""
                SELECT time AS bucket,
                       battery_charge_pct,
                       output_load_pct,
                       runtime_remaining_sec,
                       input_voltage_v,
                       output_voltage_v,
                       output_watts,
                       battery_temp_c
                FROM ups_metrics_raw
                WHERE device_ip = $1
                  AND time > NOW() - INTERVAL '{sql_interval}'
                ORDER BY time ASC
                """,
                device_ip,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT time_bucket('{bucket_size}', time) AS bucket,
                       AVG(battery_charge_pct) AS battery_charge_pct,
                       AVG(output_load_pct) AS output_load_pct,
                       AVG(runtime_remaining_sec) AS runtime_remaining_sec,
                       AVG(input_voltage_v) AS input_voltage_v,
                       AVG(output_voltage_v) AS output_voltage_v,
                       AVG(output_watts) AS output_watts,
                       AVG(battery_temp_c) AS battery_temp_c
                FROM ups_metrics_raw
                WHERE device_ip = $1
                  AND time > NOW() - INTERVAL '{sql_interval}'
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                device_ip,
            )

    def _opt_round(v: Any, digits: int = 1) -> Optional[float]:
        return round(float(v), digits) if v is not None else None

    series = [
        {
            "time": r["bucket"].isoformat(),
            "battery_charge_pct": _opt_round(r["battery_charge_pct"], 1),
            "output_load_pct": _opt_round(r["output_load_pct"], 1),
            "runtime_remaining_sec": (
                int(round(float(r["runtime_remaining_sec"])))
                if r["runtime_remaining_sec"] is not None
                else None
            ),
            "input_voltage_v": _opt_round(r["input_voltage_v"], 1),
            "output_voltage_v": _opt_round(r["output_voltage_v"], 1),
            "output_watts": _opt_round(r["output_watts"], 1),
            "battery_temp_c": _opt_round(r["battery_temp_c"], 1),
        }
        for r in rows
    ]
    return {"device_id": device_id, "device_ip": device_ip, "range": rk, "series": series}


@app.get("/api/devices/{device_ref}/wireless-history")
async def get_device_wireless_history(
    device_ref: str,
    range_key: str = Query(default="24h", alias="range"),
) -> Dict[str, Any]:
    """Returns time-series wireless clients, channel utilization, interference, noise, CCQ, signal, and bps."""
    rk = (range_key or "24h").strip().lower()
    if rk == "1d":
        rk = "24h"
    if rk not in ("1h", "6h", "24h", "7d", "30d"):
        rk = "24h"

    range_map = {
        "1h": ("1 hour", None),
        "6h": ("6 hours", None),
        "24h": ("24 hours", "5 minutes"),
        "7d": ("7 days", "15 minutes"),
        "30d": ("30 days", "1 hour"),
    }
    sql_interval, bucket_size = range_map[rk]

    pool = _get_pool()
    async with pool.acquire() as conn:
        device_id, device_ip = await _resolve_device_ref(conn, device_ref)
        if bucket_size is None:
            rows = await conn.fetch(
                f"""
                SELECT time AS bucket,
                       total_clients,
                       avg_channel_util_pct,
                       max_channel_util_pct,
                       avg_self_rx_pct,
                       avg_self_tx_pct,
                       avg_interference_pct,
                       avg_noise_floor_dbm,
                       avg_ccq_pct,
                       avg_signal_dbm,
                       rx_bps,
                       tx_bps
                FROM wireless_metrics_raw
                WHERE device_ip = $1
                  AND time > NOW() - INTERVAL '{sql_interval}'
                ORDER BY time ASC
                """,
                device_ip,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT time_bucket('{bucket_size}', time) AS bucket,
                       AVG(total_clients) AS total_clients,
                       AVG(avg_channel_util_pct) AS avg_channel_util_pct,
                       MAX(max_channel_util_pct) AS max_channel_util_pct,
                       AVG(avg_self_rx_pct) AS avg_self_rx_pct,
                       AVG(avg_self_tx_pct) AS avg_self_tx_pct,
                       AVG(avg_interference_pct) AS avg_interference_pct,
                       AVG(avg_noise_floor_dbm) AS avg_noise_floor_dbm,
                       AVG(avg_ccq_pct) AS avg_ccq_pct,
                       AVG(avg_signal_dbm) AS avg_signal_dbm,
                       AVG(rx_bps) AS rx_bps,
                       AVG(tx_bps) AS tx_bps
                FROM wireless_metrics_raw
                WHERE device_ip = $1
                  AND time > NOW() - INTERVAL '{sql_interval}'
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                device_ip,
            )

    def _opt_round(v: Any, digits: int = 1) -> Optional[float]:
        return round(float(v), digits) if v is not None else None

    series = [
        {
            "time": r["bucket"].isoformat(),
            "total_clients": (
                int(round(float(r["total_clients"])))
                if r["total_clients"] is not None
                else None
            ),
            "avg_channel_util_pct": _opt_round(r["avg_channel_util_pct"], 1),
            "max_channel_util_pct": _opt_round(r["max_channel_util_pct"], 1),
            "avg_self_rx_pct": _opt_round(r["avg_self_rx_pct"], 1),
            "avg_self_tx_pct": _opt_round(r["avg_self_tx_pct"], 1),
            "avg_interference_pct": _opt_round(r["avg_interference_pct"], 1),
            "avg_noise_floor_dbm": _opt_round(r["avg_noise_floor_dbm"], 1),
            "avg_ccq_pct": _opt_round(r["avg_ccq_pct"], 1),
            "avg_signal_dbm": _opt_round(r["avg_signal_dbm"], 1),
            "rx_bps": _opt_round(r["rx_bps"], 1),
            "tx_bps": _opt_round(r["tx_bps"], 1),
        }
        for r in rows
    ]
    return {"device_id": device_id, "device_ip": device_ip, "range": rk, "series": series}


@app.get("/api/devices/{device_ref}/system-history")
async def get_device_system_history(
    device_ref: str,
    range_key: str = Query(default="24h", alias="range"),
) -> Dict[str, Any]:
    """Returns time-series CPU, memory, storage, temperature, and load telemetry for a device."""
    rk = (range_key or "24h").strip().lower()
    if rk == "1d":
        rk = "24h"
    if rk not in ("1h", "6h", "24h", "7d", "30d", "1y", "all"):
        rk = "24h"

    range_map = {
        "1h": ("1 hour", None),
        "6h": ("6 hours", "2 minutes"),
        "24h": ("24 hours", "5 minutes"),
        "7d": ("7 days", "1 hour"),
        "30d": ("30 days", "2 hours"),
        "1y": ("365 days", "1 day"),
        "all": (None, "1 day"),
    }
    sql_interval, bucket_size = range_map[rk]

    pool = _get_pool()
    async with pool.acquire() as conn:
        device_id, device_ip = await _resolve_device_ref(conn, device_ref)
        where_clause = f"AND time > NOW() - INTERVAL '{sql_interval}'" if sql_interval else ""

        if bucket_size is None:
            sys_rows = await conn.fetch(
                f"""
                SELECT time AS bucket,
                       cpu_usage_pct,
                       mem_usage_pct,
                       mem_used_bytes,
                       mem_total_bytes,
                       storage_usage_pct,
                       storage_used_bytes,
                       storage_total_bytes,
                       temp_celsius,
                       load_1m,
                       volume_metrics,
                       cpu_core_metrics
                FROM device_system_metrics_raw
                WHERE device_ip = $1
                  {where_clause}
                ORDER BY time ASC
                """,
                device_ip,
            )
        else:
            sys_rows = await conn.fetch(
                f"""
                SELECT time_bucket('{bucket_size}', time) AS bucket,
                       AVG(cpu_usage_pct) AS cpu_usage_pct,
                       AVG(mem_usage_pct) AS mem_usage_pct,
                       ROUND(AVG(mem_used_bytes))::bigint AS mem_used_bytes,
                       MAX(mem_total_bytes) AS mem_total_bytes,
                       AVG(storage_usage_pct) AS storage_usage_pct,
                       ROUND(AVG(storage_used_bytes))::bigint AS storage_used_bytes,
                       MAX(storage_total_bytes) AS storage_total_bytes,
                       AVG(temp_celsius) AS temp_celsius,
                       AVG(load_1m) AS load_1m,
                       (ARRAY_AGG(volume_metrics ORDER BY time DESC) FILTER (
                           WHERE volume_metrics IS NOT NULL
                             AND jsonb_typeof(volume_metrics) = 'array'
                             AND jsonb_array_length(volume_metrics) > 0
                       ))[1] AS volume_metrics,
                       (ARRAY_AGG(cpu_core_metrics ORDER BY time DESC) FILTER (
                           WHERE cpu_core_metrics IS NOT NULL
                             AND jsonb_typeof(cpu_core_metrics) = 'array'
                             AND jsonb_array_length(cpu_core_metrics) > 0
                       ))[1] AS cpu_core_metrics
                FROM device_system_metrics_raw
                WHERE device_ip = $1
                  {where_clause}
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                device_ip,
            )

        earliest_ts = sys_rows[0]["bucket"] if sys_rows else datetime.now(timezone.utc)
        backfill_rows: List[asyncpg.Record] = []

        if rk in ("1h", "6h", "24h"):
            raw_bucket = "1 minute" if rk == "1h" else (bucket_size or "5 minutes")
            backfill_rows = await conn.fetch(
                f"""
                SELECT time_bucket('{raw_bucket}', time) AS bucket,
                       AVG(NULLIF(cpu_usage, 0)) AS cpu_usage_pct,
                       AVG(NULLIF(mem_usage, 0)) AS mem_usage_pct
                FROM snmp_metrics_raw
                WHERE device_ip = $1
                  AND time < $2
                  {where_clause}
                  AND (cpu_usage > 0 OR mem_usage > 0)
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                device_ip,
                earliest_ts,
            )
        elif rk in ("7d", "30d"):
            agg_where = f"AND bucket > NOW() - INTERVAL '{sql_interval}'" if sql_interval else ""
            backfill_rows = await conn.fetch(
                f"""
                SELECT bucket,
                       AVG(NULLIF(avg_cpu_usage, 0)) AS cpu_usage_pct,
                       AVG(NULLIF(avg_mem_usage, 0)) AS mem_usage_pct
                FROM snmp_metrics_hourly
                WHERE device_ip = $1
                  AND bucket < $2
                  {agg_where}
                  AND (avg_cpu_usage > 0 OR avg_mem_usage > 0)
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                device_ip,
                earliest_ts,
            )
        else:
            agg_where = f"AND bucket > NOW() - INTERVAL '{sql_interval}'" if sql_interval else ""
            backfill_rows = await conn.fetch(
                f"""
                SELECT bucket,
                       AVG(NULLIF(avg_cpu_usage, 0)) AS cpu_usage_pct,
                       AVG(NULLIF(avg_mem_usage, 0)) AS mem_usage_pct
                FROM snmp_metrics_daily
                WHERE device_ip = $1
                  AND bucket < $2
                  {agg_where}
                  AND (avg_cpu_usage > 0 OR avg_mem_usage > 0)
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                device_ip,
                earliest_ts,
            )

        gw_backfill_rows: List[asyncpg.Record] = []
        if not backfill_rows:
            is_gw_ip = bool(
                await conn.fetchval(
                    """
                    SELECT 1 FROM unifi_devices
                    WHERE (ip_address = $1 OR lan_ip = $1)
                      AND LOWER(COALESCE(device_type, '')) IN ('udm', 'ugw', 'uxg')
                    LIMIT 1
                    """,
                    device_ip,
                )
            )
            if is_gw_ip:
                if bucket_size is None:
                    gw_backfill_rows = await conn.fetch(
                        f"""
                        SELECT time AS bucket, cpu_usage_pct, mem_usage_pct, temp_celsius
                        FROM unifi_gateway_metrics
                        WHERE time < $1
                          {where_clause}
                        ORDER BY time ASC
                        """,
                        earliest_ts,
                    )
                else:
                    gw_backfill_rows = await conn.fetch(
                        f"""
                        SELECT time_bucket('{bucket_size}', time) AS bucket,
                               AVG(cpu_usage_pct) AS cpu_usage_pct,
                               AVG(mem_usage_pct) AS mem_usage_pct,
                               AVG(temp_celsius) AS temp_celsius
                        FROM unifi_gateway_metrics
                        WHERE time < $1
                          {where_clause}
                        GROUP BY bucket
                        ORDER BY bucket ASC
                        """,
                        earliest_ts,
                    )

    def _opt_round(v: Any, digits: int = 1) -> Optional[float]:
        return round(float(v), digits) if v is not None else None

    def _opt_int_val(v: Any) -> Optional[int]:
        return int(v) if v is not None else None

    available_volumes: List[str] = []
    seen_vols = set()
    available_cores: List[str] = []
    seen_cores = set()
    series: List[Dict[str, Any]] = []

    for r in backfill_rows or gw_backfill_rows:
        rd = dict(r)
        series.append(
            {
                "time": rd["bucket"].isoformat(),
                "cpu_usage_pct": _opt_round(rd.get("cpu_usage_pct"), 1),
                "mem_usage_pct": _opt_round(rd.get("mem_usage_pct"), 1),
                "mem_used_bytes": None,
                "mem_total_bytes": None,
                "storage_usage_pct": None,
                "storage_used_bytes": None,
                "storage_total_bytes": None,
                "temp_celsius": _opt_round(rd.get("temp_celsius"), 1),
                "load_1m": None,
                "volumes": {},
                "cores": {},
            }
        )

    for r in sys_rows:
        raw_vols = r["volume_metrics"]
        if isinstance(raw_vols, str):
            try:
                raw_vols = json.loads(raw_vols)
            except Exception:
                raw_vols = []
        vols_map: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_vols, list):
            for v in raw_vols:
                if not isinstance(v, dict):
                    continue
                mnt = str(v.get("mount") or "").strip()
                if not mnt:
                    continue
                if mnt not in seen_vols:
                    seen_vols.add(mnt)
                    available_volumes.append(mnt)
                vols_map[mnt] = {
                    "usage_pct": round(float(v.get("usage_pct") or 0.0), 1),
                    "used_bytes": int(v.get("used_bytes") or 0),
                    "total_bytes": int(v.get("total_bytes") or 0),
                }

        raw_cores = r["cpu_core_metrics"]
        if isinstance(raw_cores, str):
            try:
                raw_cores = json.loads(raw_cores)
            except Exception:
                raw_cores = []
        cores_map: Dict[str, float] = {}
        if isinstance(raw_cores, list):
            for c in raw_cores:
                if not isinstance(c, dict):
                    continue
                core_lbl = str(c.get("label") or "").strip()
                if not core_lbl:
                    core_num = c.get("core")
                    if core_num is not None:
                        core_lbl = f"CPU {core_num}"
                if not core_lbl:
                    continue
                if core_lbl not in seen_cores:
                    seen_cores.add(core_lbl)
                    available_cores.append(core_lbl)
                cores_map[core_lbl] = round(float(c.get("usage_pct") or 0.0), 1)

        series.append(
            {
                "time": r["bucket"].isoformat(),
                "cpu_usage_pct": _opt_round(r["cpu_usage_pct"], 1),
                "mem_usage_pct": _opt_round(r["mem_usage_pct"], 1),
                "mem_used_bytes": _opt_int_val(r["mem_used_bytes"]),
                "mem_total_bytes": _opt_int_val(r["mem_total_bytes"]),
                "storage_usage_pct": _opt_round(r["storage_usage_pct"], 1),
                "storage_used_bytes": _opt_int_val(r["storage_used_bytes"]),
                "storage_total_bytes": _opt_int_val(r["storage_total_bytes"]),
                "temp_celsius": _opt_round(r["temp_celsius"], 1),
                "load_1m": _opt_round(r["load_1m"], 2),
                "volumes": vols_map,
                "cores": cores_map,
            }
        )

    return {
        "device_id": device_id,
        "device_ip": device_ip,
        "range": rk,
        "volumes": available_volumes,
        "cpu_cores": available_cores,
        "series": series,
    }


@app.get("/api/devices/{device_ref}/icmp-history")
async def get_device_icmp_history(
    device_ref: str,
    range_key: str = Query(default="24h", alias="range"),
) -> Dict[str, Any]:
    """Returns time-bucketed ICMP latency (ms), uptime %, and packet loss history for a device."""
    rk = (range_key or "24h").strip().lower()
    if rk == "1d":
        rk = "24h"
    if rk not in ("1h", "6h", "24h", "7d", "30d"):
        rk = "24h"

    range_specs = {
        "1h": ("1 hour", None),
        "6h": ("6 hours", None),
        "24h": ("24 hours", None),
        "7d": ("7 days", "5 minutes"),
        "30d": ("30 days", "30 minutes"),
    }
    sql_interval, bucket_size = range_specs[rk]

    pool = _get_pool()
    async with pool.acquire() as conn:
        device_id, device_ip = await _resolve_device_ref(conn, device_ref)
        if bucket_size is None:
            rows = await conn.fetch(
                f"""
                SELECT time AS bucket,
                       rtt_ms,
                       packet_loss_pct,
                       CASE WHEN is_reachable THEN 100.0 ELSE 0.0 END AS uptime_pct,
                       is_reachable,
                       CASE
                           WHEN (NOT is_reachable OR COALESCE(packet_loss_pct, 0) > 0) THEN TRUE
                           ELSE FALSE
                       END AS had_drop
                FROM icmp_metrics_raw
                WHERE device_ip = $1
                  AND time > NOW() - INTERVAL '{sql_interval}'
                ORDER BY time ASC
                """,
                device_ip,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT time_bucket('{bucket_size}', time) AS bucket,
                       AVG(rtt_ms) FILTER (WHERE is_reachable) AS rtt_ms,
                       MAX(packet_loss_pct) AS packet_loss_pct,
                       (COUNT(*) FILTER (WHERE is_reachable) * 100.0 / NULLIF(COUNT(*), 0))::double precision AS uptime_pct,
                       BOOL_AND(is_reachable) AS is_reachable,
                       BOOL_OR(NOT is_reachable OR COALESCE(packet_loss_pct, 0) > 0) AS had_drop
                FROM icmp_metrics_raw
                WHERE device_ip = $1
                  AND time > NOW() - INTERVAL '{sql_interval}'
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                device_ip,
            )

    series = [
        {
            "time": r["bucket"].isoformat(),
            "rtt_ms": round(float(r["rtt_ms"]), 2) if r["rtt_ms"] is not None else None,
            "packet_loss_pct": round(float(r["packet_loss_pct"]), 1)
            if r["packet_loss_pct"] is not None
            else 0.0,
            "uptime_pct": round(float(r["uptime_pct"]), 1)
            if r["uptime_pct"] is not None
            else 0.0,
            "is_reachable": bool(r["is_reachable"]),
            "had_drop": bool(r["had_drop"]),
        }
        for r in rows
    ]

    return {
        "device_id": device_id,
        "device_ip": device_ip,
        "range": rk,
        "series": series,
    }


@app.post("/api/devices/rematch-mibs")
async def trigger_rematch_all_device_mibs() -> Dict[str, str]:
    """Immediately re-matches all previously discovered devices with the MIB catalog and probes SNMP."""
    await _notify_worker("rematch_mibs")
    return {
        "status": "triggered",
        "message": "Auto-matching MIB profiles and probing SNMP across all discovered devices.",
    }


@app.post("/api/devices/{device_ref}/poll")
async def trigger_device_poll(device_ref: str) -> Dict[str, Any]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        device_id, device_ip = await _resolve_device_ref(conn, device_ref)
    done_key = f"snmp:poll_done:{device_ip}"
    if redis_client:
        try:
            await redis_client.delete(done_key)
        except Exception:
            pass
    await _notify_worker(f"poll_device:{device_ip}")
    if redis_client:
        for _ in range(75):
            await asyncio.sleep(0.2)
            try:
                raw_done = await redis_client.get(done_key)
                if raw_done:
                    parsed = json.loads(raw_done)
                    ok_val = bool(parsed.get("ok"))
                    msg = (
                        (parsed.get("detail") or f"SNMP poll succeeded for {device_ip}.")
                        if ok_val
                        else (
                            parsed.get("error")
                            or f"SNMP poll failed for {device_ip}. Check SNMP credentials, version, and network reachability."
                        )
                    )
                    return {
                        "status": "completed",
                        "device_id": device_id,
                        "ok": ok_val,
                        "message": msg,
                        "poll_ms": parsed.get("poll_ms"),
                        "interfaces": parsed.get("interfaces"),
                        "vendor_name": parsed.get("vendor_name"),
                    }
            except Exception:
                break
    return {
        "status": "timeout",
        "device_id": device_id,
        "ok": False,
        "message": f"SNMP poll for {device_ip} did not complete within 15s — check worker status or SNMP reachability.",
    }


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

        log_ips = list(
            dict.fromkeys(
                [str(r["source_ip"]).strip() for r in rows if r["source_ip"]]
                + [str(s["source_ip"]).strip() for s in sources if s["source_ip"]]
            )
        )
        log_id_map: Dict[str, int] = {}
        if log_ips:
            reg_rows = await conn.fetch(
                "SELECT id, ip_address FROM device_registry WHERE ip_address = ANY($1::text[])",
                log_ips,
            )
            log_id_map = {str(reg["ip_address"]): int(reg["id"]) for reg in reg_rows}

    logs = []
    for r in rows:
        d = dict(r)
        d["device_id"] = log_id_map.get(str(d.get("source_ip") or "").strip())
        d["time"] = d["time"].isoformat() if d.get("time") else None
        logs.append(d)

    sources_out = []
    for s in sources:
        sd = dict(s)
        sd["device_id"] = log_id_map.get(str(sd.get("source_ip") or "").strip())
        sources_out.append(sd)

    return {
        "logs": logs,
        "sources": sources_out,
    }


@app.post("/api/logs/ingest")
async def ingest_syslog_events(request: Request) -> Dict[str, int]:
    """Ingests batched JSON/NDJSON syslog events from Vector into TimescaleDB `device_logs`."""
    raw_body = await request.body()
    content_encoding = request.headers.get("content-encoding", "")
    raw_events = parse_ingest_payload(raw_body, content_encoding=content_encoding)
    if not raw_events:
        return {"ingested": 0}

    rows_to_insert: List[Tuple[Any, ...]] = []
    for raw_evt in raw_events:
        norm = parse_syslog_event(raw_evt)
        if norm is None:
            continue
        rows_to_insert.append(
            (
                norm["time"],
                norm["source_ip"],
                norm["hostname"],
                norm["facility"],
                norm["severity"],
                norm["severity_num"],
                norm["app_name"],
                norm["proc_id"],
                norm["message"],
                norm["raw_log"],
            )
        )

    if not rows_to_insert:
        return {"ingested": 0}

    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO device_logs (
                time, source_ip, hostname, facility, severity, severity_num,
                app_name, proc_id, message, raw_log
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            rows_to_insert,
        )

    if redis_client:
        try:
            await redis_client.publish(
                "netdash:live",
                json.dumps(
                    {
                        "type": "syslog_update",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "data": {"count": len(rows_to_insert)},
                    }
                ),
            )
        except Exception:
            pass

    return {"ingested": len(rows_to_insert)}


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
