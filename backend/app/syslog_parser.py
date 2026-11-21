from datetime import datetime, timezone
import gzip
import ipaddress
import json
import re
from typing import Any, Dict, List, Optional

FACILITY_NAMES: Dict[int, str] = {
    0: "kern",
    1: "user",
    2: "mail",
    3: "daemon",
    4: "auth",
    5: "syslog",
    6: "lpr",
    7: "news",
    8: "uucp",
    9: "cron",
    10: "authpriv",
    11: "ftp",
    12: "ntp",
    13: "audit",
    14: "alert",
    15: "clock",
    16: "local0",
    17: "local1",
    18: "local2",
    19: "local3",
    20: "local4",
    21: "local5",
    22: "local6",
    23: "local7",
}

SEVERITY_NAMES: Dict[int, str] = {
    0: "emerg",
    1: "alert",
    2: "crit",
    3: "err",
    4: "warning",
    5: "notice",
    6: "info",
    7: "debug",
}

_MONTH_MAP: Dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# RFC 5424: <PRI>VERSION SP TIMESTAMP SP HOSTNAME SP APP-NAME SP PROCID SP MSGID [SP STRUCTURED-DATA [SP MSG]]
_RFC5424_RE = re.compile(
    r"^(?:<(\d{1,3})>)?([1-9]\d{0,2})\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.*))?$"
)

# RFC 3164 (classic BSD date): <PRI>Mmm DD HH:MM:SS HOSTNAME TAG[PID]: MSG
_RFC3164_BSD_RE = re.compile(
    r"^(?:<(\d{1,3})>)?([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(\S+)\s+(.*)$"
)

# Variant with ISO-8601 timestamp without version: <PRI>2026-11-21T20:00:00Z HOSTNAME TAG: MSG
_RFC3164_ISO_RE = re.compile(
    r"^(?:<(\d{1,3})>)?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+(\S+)\s+(.*)$"
)

# Leading <PRI> only (non-standard devices that omit timestamp/header)
_PRI_ONLY_RE = re.compile(r"^<(\d{1,3})>(.*)$")

# Tag / procid prefix: e.g., "sshd[1234]: msg", "kernel: msg", "ubios-udapi-server[901]: msg"
_TAG_PREFIX_RE = re.compile(r"^([A-Za-z0-9_.\-/]{1,64})(?:\[([^\]]{1,32})\])?:\s*(.*)$")


def _normalize_source_ip(raw_ip: Any) -> str:
    """Extracts clean IP string from socket peer strings (IPv4:port, [IPv6]:port, ::ffff:x.x.x.x)."""
    val = str(raw_ip or "").replace("\x00", "").strip()
    if not val:
        return "0.0.0.0"

    # Strip bracketed [IPv6]:port
    if val.startswith("["):
        end_idx = val.find("]")
        if end_idx > 1:
            val = val[1:end_idx]

    if val.lower().startswith("::ffff:"):
        val = val[7:]

    # Strip :port ONLY when valid IPv4:port or host:port with single colon
    if val.count(":") == 1:
        host_part, port_part = val.split(":", 1)
        if host_part and port_part.isdigit():
            val = host_part

    # Handle bare IPv6 with zone or already valid IPv4/IPv6
    try:
        return str(ipaddress.ip_address(val))
    except ValueError:
        return val[:128] if val else "0.0.0.0"


def _parse_syslog_timestamp(
    raw_ts: str, fallback: Optional[datetime] = None
) -> datetime:
    """Parses RFC 5424 ISO-8601 or RFC 3164 BSD timestamps into UTC datetime."""
    now_dt = fallback or datetime.now(timezone.utc)
    ts_str = (raw_ts or "").strip()
    if not ts_str or ts_str == "-":
        return now_dt

    # 1. Try ISO-8601 / RFC 3339
    iso_cand = ts_str
    if iso_cand.endswith("Z"):
        iso_cand = iso_cand[:-1] + "+00:00"
    elif re.search(r"[+-]\d{4}$", iso_cand):
        iso_cand = iso_cand[:-2] + ":" + iso_cand[-2:]
    try:
        dt = datetime.fromisoformat(iso_cand)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # 2. Try RFC 3164 BSD: "Nov 21 14:35:50" or "Nov  1 08:01:02"
    m = re.match(
        r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})$",
        ts_str,
    )
    if m:
        mon = _MONTH_MAP.get(m.group(1).lower())
        if mon:
            day = int(m.group(2))
            hour = int(m.group(3))
            minute = int(m.group(4))
            second = int(m.group(5))
            year = now_dt.year
            try:
                dt = datetime(year, mon, day, hour, minute, second, tzinfo=timezone.utc)
                # Handle Dec log arriving on Jan 1 (or slight clock skew)
                if (dt - now_dt).days > 180:
                    dt = dt.replace(year=year - 1)
                elif (now_dt - dt).days > 180:
                    dt = dt.replace(year=year + 1)
                return dt
            except ValueError:
                return now_dt

    return now_dt


def _decode_pri(pri_str: Optional[str]) -> tuple[str, str, int]:
    """Converts syslog <PRI> integer string into (facility_name, severity_name, severity_num)."""
    if pri_str is None:
        return "user", "info", 6
    try:
        pri = int(pri_str)
        fac_code = pri >> 3
        sev_code = pri & 7
        return (
            FACILITY_NAMES.get(fac_code, f"facility{fac_code}"),
            SEVERITY_NAMES.get(sev_code, "info"),
            sev_code,
        )
    except Exception:
        return "user", "info", 6


def _strip_rfc5424_sd(rest: str) -> str:
    """Strips RFC 5424 STRUCTURED-DATA ('-' or one or more '[...]') to yield MSG."""
    s = (rest or "").lstrip()
    if not s:
        return ""
    if s.startswith("- "):
        return s[2:].strip()
    if s == "-":
        return ""
    if s.startswith("["):
        idx = 0
        n = len(s)
        in_brackets = False
        escaped = False
        while idx < n:
            ch = s[idx]
            if not in_brackets:
                if ch == "[":
                    in_brackets = True
                elif ch == " ":
                    # End of SD elements
                    return s[idx + 1 :].strip()
                else:
                    return s[idx:].strip()
            else:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == "]":
                    in_brackets = False
            idx += 1
        return ""
    return s


def _infer_keyword_severity(text: str) -> tuple[str, int]:
    low = text.lower()
    if any(k in low for k in ("emerg", "panic", "fatal")):
        return "crit", 2
    if any(k in low for k in ("error", "err:", "fail", "crit", "denied", "blocked")):
        return "err", 3
    if any(k in low for k in ("warn", "drop", "disconnect", "timeout", "retry")):
        return "warning", 4
    return "info", 6


def _clean_field(val: Optional[str], fallback: str) -> str:
    cleaned = (val or "").replace("\x00", "").strip()
    if not cleaned or cleaned == "-":
        return fallback
    return cleaned


def parse_syslog_event(raw_event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalizes one raw Vector socket/syslog JSON event into `device_logs` columns."""
    raw_line = str(raw_event.get("message") or "").replace("\x00", "").strip()
    if not raw_line:
        return None

    src_ip = _normalize_source_ip(raw_event.get("source_ip") or raw_event.get("host"))
    now_utc = datetime.now(timezone.utc)

    # Allow already-structured event fields as optional hints
    evt_time = now_utc
    if raw_event.get("timestamp"):
        evt_time = _parse_syslog_timestamp(str(raw_event["timestamp"]), now_utc)

    # 1. Try RFC 5424
    m5424 = _RFC5424_RE.match(raw_line)
    if m5424 and m5424.group(1) is not None:
        fac, sev, sev_num = _decode_pri(m5424.group(1))
        ts = _parse_syslog_timestamp(m5424.group(3), evt_time)
        hostname = _clean_field(m5424.group(4), src_ip)
        app_name = _clean_field(m5424.group(5), "system")
        proc_id = "" if (m5424.group(6) or "-") == "-" else str(m5424.group(6)).strip()
        msg_body = _strip_rfc5424_sd(m5424.group(8) or "")
        msg = _clean_field(msg_body, raw_line)
        return {
            "time": ts,
            "source_ip": src_ip,
            "hostname": hostname,
            "facility": fac,
            "severity": sev,
            "severity_num": sev_num,
            "app_name": app_name,
            "proc_id": proc_id,
            "message": msg,
            "raw_log": raw_line,
        }

    # 2. Try RFC 3164 (BSD timestamp) or ISO-timestamped syslog (common on UniFi UDM Pro / rsyslog)
    m3164 = _RFC3164_BSD_RE.match(raw_line) or _RFC3164_ISO_RE.match(raw_line)
    if m3164:
        pri_grp, ts_grp, host_grp, body_grp = (
            m3164.group(1),
            m3164.group(2),
            m3164.group(3),
            m3164.group(4),
        )
        if pri_grp is not None:
            fac, sev, sev_num = _decode_pri(pri_grp)
        else:
            fac = "daemon"
            sev, sev_num = _infer_keyword_severity(body_grp)
        ts = _parse_syslog_timestamp(ts_grp, evt_time)
        hostname = _clean_field(host_grp, src_ip)
        app_name = "system"
        proc_id = ""
        msg_text = body_grp.strip()

        # Parse tag[pid]: message unless body starts immediately with CEF:
        if not msg_text.startswith("CEF:"):
            m_tag = _TAG_PREFIX_RE.match(msg_text)
            if m_tag:
                app_name = _clean_field(m_tag.group(1), "system")
                proc_id = (m_tag.group(2) or "").strip()
                msg_text = (m_tag.group(3) or "").strip() or msg_text
        else:
            app_name = "unifi-cef"

        return {
            "time": ts,
            "source_ip": src_ip,
            "hostname": hostname,
            "facility": fac,
            "severity": sev,
            "severity_num": sev_num,
            "app_name": app_name,
            "proc_id": proc_id,
            "message": _clean_field(msg_text, raw_line),
            "raw_log": raw_line,
        }

    # 3. Fallback: optional leading <PRI> + arbitrary vendor/netconsole line
    pri_grp: Optional[str] = None
    remainder = raw_line
    m_pri = _PRI_ONLY_RE.match(raw_line)
    if m_pri:
        pri_grp = m_pri.group(1)
        remainder = m_pri.group(2).strip()

    if pri_grp is not None:
        fac, sev, sev_num = _decode_pri(pri_grp)
    else:
        fac = "daemon"
        sev, sev_num = _infer_keyword_severity(remainder or raw_line)

    app_name = "raw-syslog"
    proc_id = ""
    msg_final = remainder or raw_line
    m_tag = _TAG_PREFIX_RE.match(msg_final)
    if m_tag:
        app_name = _clean_field(m_tag.group(1), "raw-syslog")
        proc_id = (m_tag.group(2) or "").strip()
        msg_final = (m_tag.group(3) or "").strip() or msg_final

    return {
        "time": evt_time,
        "source_ip": src_ip,
        "hostname": src_ip,
        "facility": fac,
        "severity": sev,
        "severity_num": sev_num,
        "app_name": app_name,
        "proc_id": proc_id,
        "message": _clean_field(msg_final, raw_line),
        "raw_log": raw_line,
    }


def parse_ingest_payload(body: bytes, content_encoding: str = "") -> List[Dict[str, Any]]:
    """Decodes JSON array, single JSON object, or NDJSON (optionally gzip-compressed) events."""
    if not body:
        return []

    raw_bytes = body
    if "gzip" in (content_encoding or "").lower() or raw_bytes.startswith(b"\x1f\x8b"):
        raw_bytes = gzip.decompress(raw_bytes)

    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return []

    # Try full JSON array/object first
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            pass

    # Parse NDJSON lines
    events: List[Dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                events.append(obj)
        except json.JSONDecodeError:
            events.append({"message": stripped})
    return events
