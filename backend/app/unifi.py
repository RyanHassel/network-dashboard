from datetime import datetime, timezone
import ipaddress
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import asyncpg
import httpx

from app.db_schema import ensure_device_ids

logger = logging.getLogger("netdash.unifi")


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _safe_opt_int(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


class UniFiCollector:
    """Polls optional UniFi controllers configured in unifi_controllers_config."""

    @staticmethod
    async def _fetch_endpoint(
        client: httpx.AsyncClient,
        host: str,
        api_key: str,
        site: str,
        path: str,
        timeout: float = 12.0,
    ) -> List[Dict[str, Any]]:
        base_url = host if host.startswith("http") else f"https://{host}"
        base_url = base_url.rstrip("/")

        urls = [
            f"{base_url}/proxy/network/api/s/{site}/{path.lstrip('/')}",
            f"{base_url}/api/s/{site}/{path.lstrip('/')}",
        ]
        headers = {
            "X-API-KEY": api_key,
            "Accept": "application/json",
        }

        for url in urls:
            try:
                resp = await client.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, dict) and "data" in payload:
                        return payload["data"]
            except Exception as exc:
                logger.debug("UniFi endpoint %s failed: %s", url, exc)
        return []

    async def poll_and_store(self, pool: asyncpg.Pool) -> Optional[Dict[str, Any]]:
        async with pool.acquire() as conn:
            controllers = await conn.fetch(
                """
                SELECT id, name, host, api_key, site_name, verify_ssl
                FROM unifi_controllers_config
                WHERE enabled = TRUE AND host != '' AND api_key != ''
                ORDER BY id ASC
                """
            )

        if not controllers:
            return None

        last_summary: Optional[Dict[str, Any]] = None

        for ctrl in controllers:
            host = str(ctrl["host"]).strip()
            api_key = str(ctrl["api_key"]).strip()
            site = str(ctrl["site_name"] or "default").strip()
            verify_ssl = bool(ctrl["verify_ssl"])

            async with httpx.AsyncClient(verify=verify_ssl) as client:
                health_data = await self._fetch_endpoint(client, host, api_key, site, "stat/health")
                device_data = await self._fetch_endpoint(client, host, api_key, site, "stat/device")
                client_data = await self._fetch_endpoint(client, host, api_key, site, "stat/sta")

            if not health_data and not device_data:
                logger.warning("No telemetry returned from UniFi host (%s).", host)
                continue

            subsystems = {item.get("subsystem"): item for item in health_data if isinstance(item, dict)}
            wan_sub = subsystems.get("wan", {})
            www_sub = subsystems.get("www", {})
            lan_sub = subsystems.get("lan", {})
            wlan_sub = subsystems.get("wlan", {})

            wan_ip = wan_sub.get("wan_ip") or www_sub.get("wan_ip") or ""
            wan_isp = wan_sub.get("isp_name") or ""
            wan_status = wan_sub.get("status") or www_sub.get("status") or "online"
            wan_rx_bps = _safe_float(wan_sub.get("rx_bytes-r")) * 8.0
            wan_tx_bps = _safe_float(wan_sub.get("tx_bytes-r")) * 8.0
            wan_latency_ms = _safe_float(www_sub.get("latency", wan_sub.get("latency")))
            wan_loss_pct = _safe_float(www_sub.get("drops"))
            wan_uptime_sec = _safe_int(wan_sub.get("uptime", www_sub.get("uptime")))

            active_lan_clients = _safe_int(lan_sub.get("num_user"))
            active_wlan_clients = _safe_int(wlan_sub.get("num_user"))

            gateway_mac = ""
            gateway_name = str(ctrl["name"] or "UniFi Gateway")
            gateway_model = "UDMPRO"
            gateway_version = ""
            gw_cpu = 0.0
            gw_mem = 0.0
            gw_temp: Optional[float] = None
            total_poe_watts = 0.0
            online_devices = 0

            async with pool.acquire() as conn:
                unifi_ips: List[str] = []
                sys_candidates: List[tuple] = []
                for dev in device_data:
                    if not isinstance(dev, dict):
                        continue
                    try:
                        mac = str(dev.get("mac", "")).lower()
                        if not mac:
                            continue
                        ip_addr = str(dev.get("ip", ""))
                        cfg_net = (
                            dev.get("config_network")
                            if isinstance(dev.get("config_network"), dict)
                            else {}
                        )
                        lan_ip = str(dev.get("lan_ip") or cfg_net.get("ip") or "").strip()
                        name = str(dev.get("name") or dev.get("model") or mac)
                        model = str(dev.get("model", ""))
                        dev_type = str(dev.get("type", "udm"))
                        serial_num = str(dev.get("serial") or "").strip()
                        fw_ver = str(dev.get("version", ""))
                        is_gw = dev_type in ("ugw", "udm", "uxg") or "UDM" in model.upper()
                        if not lan_ip and is_gw:
                            parsed_host = (
                                urlparse(host if "://" in host else f"https://{host}").hostname or ""
                            )
                            try:
                                ipaddress.IPv4Address(parsed_host)
                                lan_ip = parsed_host
                            except ValueError:
                                pass
                        for cand_ip in (lan_ip, ip_addr):
                            c_ip = str(cand_ip or "").strip()
                            if c_ip and c_ip not in unifi_ips:
                                unifi_ips.append(c_ip)
                        state_code = _safe_int(dev.get("state", 1), default=0)
                        status = "online" if state_code == 1 else "offline"
                        if state_code == 1:
                            online_devices += 1

                        uptime_sec = _safe_int(dev.get("uptime"))
                        sys_stats = dev.get("system-stats") or {}
                        if not isinstance(sys_stats, dict):
                            sys_stats = {}
                        cpu_pct = _safe_float(sys_stats.get("cpu"))
                        mem_pct = _safe_float(sys_stats.get("mem"))

                        temp_val: Optional[float] = None
                        temps = dev.get("temperatures") or []
                        if isinstance(temps, list) and temps and isinstance(temps[0], dict):
                            first_t = temps[0].get("value")
                            if first_t is not None and first_t != "":
                                temp_val = _safe_float(first_t)
                        elif dev.get("general_temperature") not in (None, ""):
                            temp_val = _safe_float(dev.get("general_temperature"))

                        port_table = dev.get("port_table") or []
                        dev_poe_watts = 0.0
                        slim_ports = []
                        if isinstance(port_table, list):
                            for p in port_table:
                                if not isinstance(p, dict):
                                    continue
                                poe_w = _safe_float(p.get("poe_power"))
                                dev_poe_watts += poe_w
                                slim_ports.append(
                                    {
                                        "port_idx": p.get("port_idx"),
                                        "name": p.get("name"),
                                        "up": bool(p.get("up", False)),
                                        "speed": p.get("speed", 0),
                                        "poe_enable": bool(p.get("poe_enable", False)),
                                        "poe_power": round(poe_w, 2),
                                        "rx_bps": round(_safe_float(p.get("rx_bytes-r")) * 8.0, 1),
                                        "tx_bps": round(_safe_float(p.get("tx_bytes-r")) * 8.0, 1),
                                    }
                                )

                        total_poe_watts += dev_poe_watts
                        poe_max = _safe_float(dev.get("total_max_power"))
                        num_sta = _safe_int(dev.get("num_sta"))
                        rx_bps = _safe_float(dev.get("rx_bytes-r")) * 8.0
                        tx_bps = _safe_float(dev.get("tx_bytes-r")) * 8.0

                        if is_gw:
                            gateway_mac = mac
                            gateway_name = name
                            gateway_model = model
                            gateway_version = fw_ver
                            gw_cpu = cpu_pct
                            gw_mem = mem_pct
                            gw_temp = temp_val
                            if not wan_uptime_sec:
                                wan_uptime_sec = uptime_sec

                        await conn.execute(
                            """
                            INSERT INTO unifi_devices (
                                mac, ip_address, lan_ip, name, model, device_type, serial_number, firmware_version,
                                status, uptime_sec, cpu_usage_pct, mem_usage_pct, temp_celsius,
                                poe_total_watts, poe_max_watts, num_sta, rx_bps, tx_bps, port_table, last_seen
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, $7, $8,
                                $9, $10, $11, $12, $13,
                                $14, $15, $16, $17, $18, $19::jsonb, NOW()
                            )
                            ON CONFLICT (mac) DO UPDATE SET
                                ip_address = EXCLUDED.ip_address,
                                lan_ip = COALESCE(NULLIF(EXCLUDED.lan_ip, ''), unifi_devices.lan_ip),
                                name = EXCLUDED.name,
                                model = EXCLUDED.model,
                                device_type = EXCLUDED.device_type,
                                serial_number = COALESCE(NULLIF(EXCLUDED.serial_number, ''), unifi_devices.serial_number),
                                firmware_version = EXCLUDED.firmware_version,
                                status = EXCLUDED.status,
                                uptime_sec = EXCLUDED.uptime_sec,
                                cpu_usage_pct = EXCLUDED.cpu_usage_pct,
                                mem_usage_pct = EXCLUDED.mem_usage_pct,
                                temp_celsius = EXCLUDED.temp_celsius,
                                poe_total_watts = EXCLUDED.poe_total_watts,
                                poe_max_watts = EXCLUDED.poe_max_watts,
                                num_sta = EXCLUDED.num_sta,
                                rx_bps = EXCLUDED.rx_bps,
                                tx_bps = EXCLUDED.tx_bps,
                                port_table = EXCLUDED.port_table,
                                last_seen = NOW()
                            """,
                            mac,
                            ip_addr,
                            lan_ip,
                            name,
                            model,
                            dev_type,
                            serial_num,
                            fw_ver,
                            status,
                            uptime_sec,
                            cpu_pct,
                            mem_pct,
                            temp_val,
                            round(dev_poe_watts, 2),
                            poe_max,
                            num_sta,
                            rx_bps,
                            tx_bps,
                            json.dumps(slim_ports),
                        )
                        primary_sys_ip = lan_ip or ip_addr
                        if (
                            status == "online"
                            and primary_sys_ip
                            and (cpu_pct > 0.0 or mem_pct > 0.0 or temp_val is not None)
                        ):
                            sys_candidates.append((primary_sys_ip, cpu_pct, mem_pct, temp_val))
                    except Exception as exc:
                        logger.warning(
                            "UniFi device ingest error (%s): %s",
                            dev.get("mac") or dev.get("name") or "unknown",
                            exc,
                        )

                for sta in client_data:
                    if not isinstance(sta, dict):
                        continue
                    try:
                        mac = str(sta.get("mac", "")).lower()
                        if not mac:
                            continue
                        ip_addr = str(sta.get("ip", ""))
                        if ip_addr and ip_addr not in unifi_ips:
                            unifi_ips.append(ip_addr)
                        hostname = str(sta.get("name") or sta.get("hostname") or ip_addr or mac)
                        oui = str(sta.get("oui", ""))
                        is_wired = bool(sta.get("is_wired", False))
                        net_name = str(sta.get("network", ""))
                        vlan_id = _safe_opt_int(sta.get("vlan"))
                        sw_mac = str(sta.get("sw_mac", "")).lower() or None
                        sw_port = _safe_opt_int(sta.get("sw_port"))
                        ap_mac = str(sta.get("ap_mac", "")).lower() or None
                        essid = str(sta.get("essid", "")) or None
                        channel = _safe_opt_int(sta.get("channel"))
                        rssi = _safe_opt_int(sta.get("rssi"))
                        signal = _safe_opt_int(sta.get("signal"))
                        rx_bps = _safe_float(sta.get("rx_bytes-r")) * 8.0
                        tx_bps = _safe_float(sta.get("tx_bytes-r")) * 8.0
                        rx_bytes = _safe_int(sta.get("rx_bytes"))
                        tx_bytes = _safe_int(sta.get("tx_bytes"))
                        uptime_sec = _safe_int(sta.get("uptime"))

                        await conn.execute(
                            """
                            INSERT INTO unifi_clients (
                                mac, ip_address, hostname, oui_vendor, is_wired, network_name,
                                vlan_id, sw_mac, sw_port, ap_mac, essid, channel, rssi, signal_dbm,
                                rx_bps, tx_bps, rx_bytes, tx_bytes, uptime_sec, last_seen
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6,
                                $7, $8, $9, $10, $11, $12, $13, $14,
                                $15, $16, $17, $18, $19, NOW()
                            )
                            ON CONFLICT (mac) DO UPDATE SET
                                ip_address = EXCLUDED.ip_address,
                                hostname = EXCLUDED.hostname,
                                oui_vendor = EXCLUDED.oui_vendor,
                                is_wired = EXCLUDED.is_wired,
                                network_name = EXCLUDED.network_name,
                                vlan_id = EXCLUDED.vlan_id,
                                sw_mac = EXCLUDED.sw_mac,
                                sw_port = EXCLUDED.sw_port,
                                ap_mac = EXCLUDED.ap_mac,
                                essid = EXCLUDED.essid,
                                channel = EXCLUDED.channel,
                                rssi = EXCLUDED.rssi,
                                signal_dbm = EXCLUDED.signal_dbm,
                                rx_bps = EXCLUDED.rx_bps,
                                tx_bps = EXCLUDED.tx_bps,
                                rx_bytes = EXCLUDED.rx_bytes,
                                tx_bytes = EXCLUDED.tx_bytes,
                                uptime_sec = EXCLUDED.uptime_sec,
                                last_seen = NOW()
                            """,
                            mac,
                            ip_addr,
                            hostname,
                            oui,
                            is_wired,
                            net_name,
                            vlan_id,
                            sw_mac,
                            sw_port,
                            ap_mac,
                            essid,
                            channel,
                            rssi,
                            signal,
                            rx_bps,
                            tx_bps,
                            rx_bytes,
                            tx_bytes,
                            uptime_sec,
                        )
                    except Exception as exc:
                        logger.warning(
                            "UniFi client ingest error (%s): %s",
                            sta.get("mac") or sta.get("ip") or "unknown",
                            exc,
                        )

                if unifi_ips:
                    try:
                        await ensure_device_ids(conn, unifi_ips, touch_last_seen=True)
                    except Exception as exc:
                        logger.warning("UniFi ensure_device_ids failed: %s", exc)

                if sys_candidates:
                    try:
                        cand_ips = list({row[0] for row in sys_candidates})
                        covered_rows = await conn.fetch(
                            """
                            SELECT DISTINCT device_ip
                            FROM device_system_metrics_raw
                            WHERE device_ip = ANY($1::text[])
                              AND time > NOW() - INTERVAL '3 minutes'
                              AND (storage_usage_pct IS NOT NULL OR mem_total_bytes IS NOT NULL)
                            """,
                            cand_ips,
                        )
                        snmp_covered = {str(r["device_ip"]) for r in covered_rows}
                        fallback_rows = [
                            (ip_val, cpu_val, mem_val, t_val)
                            for ip_val, cpu_val, mem_val, t_val in sys_candidates
                            if ip_val not in snmp_covered
                        ]
                        if fallback_rows:
                            await conn.executemany(
                                """
                                INSERT INTO device_system_metrics_raw (
                                    time, device_ip, cpu_usage_pct, mem_usage_pct, temp_celsius
                                ) VALUES (NOW(), $1, $2, $3, $4)
                                """,
                                fallback_rows,
                            )
                    except Exception as exc:
                        logger.debug("UniFi device_system_metrics_raw fallback skipped: %s", exc)

                await conn.execute(
                    """
                    INSERT INTO unifi_gateway_metrics (
                        time, site_name, gateway_mac, gateway_name, gateway_model, gateway_version,
                        wan_ip, wan_isp, wan_status, wan_rx_bps, wan_tx_bps, wan_latency_ms,
                        wan_packet_loss_pct, wan_uptime_sec, cpu_usage_pct, mem_usage_pct,
                        temp_celsius, total_poe_watts, active_wan_clients, active_lan_clients,
                        active_wlan_clients, total_devices, online_devices, raw_health
                    ) VALUES (
                        NOW(), $1, $2, $3, $4, $5,
                        $6, $7, $8, $9, $10, $11,
                        $12, $13, $14, $15,
                        $16, $17, $18, $19,
                        $20, $21, $22, $23::jsonb
                    )
                    """,
                    site,
                    gateway_mac,
                    gateway_name,
                    gateway_model,
                    gateway_version,
                    wan_ip,
                    wan_isp,
                    wan_status,
                    wan_rx_bps,
                    wan_tx_bps,
                    wan_latency_ms,
                    wan_loss_pct,
                    wan_uptime_sec,
                    gw_cpu,
                    gw_mem,
                    gw_temp,
                    round(total_poe_watts, 2),
                    active_lan_clients + active_wlan_clients,
                    active_lan_clients,
                    active_wlan_clients,
                    len(device_data),
                    online_devices,
                    json.dumps(subsystems),
                )

            last_summary = {
                "gateway_name": gateway_name,
                "wan_ip": wan_ip,
                "wan_status": wan_status,
                "wan_rx_bps": wan_rx_bps,
                "wan_tx_bps": wan_tx_bps,
                "wan_latency_ms": wan_latency_ms,
                "cpu_usage_pct": gw_cpu,
                "mem_usage_pct": gw_mem,
                "temp_celsius": gw_temp,
                "total_poe_watts": round(total_poe_watts, 2),
                "active_clients": active_lan_clients + active_wlan_clients,
                "online_devices": online_devices,
                "total_devices": len(device_data),
            }

        return last_summary

    async def poll_live_wan(
        self,
        controllers: List[Dict[str, Any]],
        client_map: Dict[int, httpx.AsyncClient],
    ) -> Optional[Dict[str, Any]]:
        """Lightweight high-frequency WAN throughput & latency sampler for live streaming."""
        if not controllers:
            return None

        total_rx_bps = 0.0
        total_tx_bps = 0.0
        latency_ms = 0.0
        responded = 0

        for ctrl in controllers:
            ctrl_id = int(ctrl["id"])
            host = str(ctrl["host"]).strip()
            api_key = str(ctrl["api_key"]).strip()
            site = str(ctrl.get("site_name") or "default").strip()
            verify_ssl = bool(ctrl.get("verify_ssl", False))
            if not host or not api_key:
                continue

            client = client_map.get(ctrl_id)
            if client is None:
                client = httpx.AsyncClient(verify=verify_ssl)
                client_map[ctrl_id] = client

            health_data = await self._fetch_endpoint(
                client, host, api_key, site, "stat/health", timeout=3.0
            )
            if not health_data:
                continue

            subsystems = {
                item.get("subsystem"): item for item in health_data if isinstance(item, dict)
            }
            wan_sub = subsystems.get("wan", {})
            www_sub = subsystems.get("www", {})

            total_rx_bps += _safe_float(wan_sub.get("rx_bytes-r")) * 8.0
            total_tx_bps += _safe_float(wan_sub.get("tx_bytes-r")) * 8.0
            lat = _safe_float(www_sub.get("latency", wan_sub.get("latency")))
            if lat > 0.0 and latency_ms == 0.0:
                latency_ms = lat
            responded += 1

        if responded == 0:
            return None

        return {
            "time": datetime.now(timezone.utc).isoformat(),
            "rx_bps": round(total_rx_bps, 1),
            "tx_bps": round(total_tx_bps, 1),
            "latency_ms": round(latency_ms, 2),
            "source": "unifi",
            "device_count": responded,
        }
