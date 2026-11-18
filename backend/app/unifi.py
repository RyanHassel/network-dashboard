import json
import logging
import os
from typing import Any, Dict, List, Optional

import asyncpg
import httpx

logger = logging.getLogger("netdash.unifi")


class UniFiCollector:
    """Polls UniFi Dream Machine Pro (UDM Pro) via X-API-KEY for health, device, and client telemetry."""

    def __init__(self) -> None:
        self.host = os.getenv("UNIFI_HOST", "").strip()
        self.api_key = os.getenv("UNIFI_API_KEY", "").strip()
        self.site = os.getenv("UNIFI_SITE", "default").strip() or "default"
        self.verify_ssl = os.getenv("UNIFI_VERIFY_SSL", "false").lower() == "true"

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.api_key and self.api_key != "your_unifi_network_integration_api_key")

    async def _fetch_endpoint(self, client: httpx.AsyncClient, path: str) -> List[Dict[str, Any]]:
        base_url = self.host if self.host.startswith("http") else f"https://{self.host}"
        base_url = base_url.rstrip("/")

        # Try UniFi OS proxy path first, fallback to direct controller path
        urls = [
            f"{base_url}/proxy/network/api/s/{self.site}/{path.lstrip('/')}",
            f"{base_url}/api/s/{self.site}/{path.lstrip('/')}",
        ]
        headers = {
            "X-API-KEY": self.api_key,
            "Accept": "application/json",
        }

        for url in urls:
            try:
                resp = await client.get(url, headers=headers, timeout=12.0)
                if resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, dict) and "data" in payload:
                        return payload["data"]
            except Exception as exc:
                logger.debug("UniFi endpoint %s failed: %s", url, exc)
        return []

    async def poll_and_store(self, pool: asyncpg.Pool) -> Optional[Dict[str, Any]]:
        if not self.is_configured:
            logger.debug("UniFi API key not configured; skipping UDM Pro poll.")
            return None

        async with httpx.AsyncClient(verify=self.verify_ssl) as client:
            health_data = await self._fetch_endpoint(client, "stat/health")
            device_data = await self._fetch_endpoint(client, "stat/device")
            client_data = await self._fetch_endpoint(client, "stat/sta")

        if not health_data and not device_data:
            logger.warning("No telemetry returned from UDM Pro (%s).", self.host)
            return None

        # Parse subsystems from stat/health
        subsystems = {item.get("subsystem"): item for item in health_data if isinstance(item, dict)}
        wan_sub = subsystems.get("wan", {})
        www_sub = subsystems.get("www", {})
        lan_sub = subsystems.get("lan", {})
        wlan_sub = subsystems.get("wlan", {})

        wan_ip = wan_sub.get("wan_ip") or www_sub.get("wan_ip") or ""
        wan_isp = wan_sub.get("isp_name") or ""
        wan_status = wan_sub.get("status") or www_sub.get("status") or "online"
        wan_rx_bps = float(wan_sub.get("rx_bytes-r", 0.0)) * 8.0
        wan_tx_bps = float(wan_sub.get("tx_bytes-r", 0.0)) * 8.0
        wan_latency_ms = float(www_sub.get("latency", wan_sub.get("latency", 0.0)) or 0.0)
        wan_loss_pct = float(www_sub.get("drops", 0.0) or 0.0)
        wan_uptime_sec = int(wan_sub.get("uptime", www_sub.get("uptime", 0)) or 0)

        active_lan_clients = int(lan_sub.get("num_user", 0) or 0)
        active_wlan_clients = int(wlan_sub.get("num_user", 0) or 0)

        # Parse devices (gateway, switches, APs)
        gateway_mac = ""
        gateway_name = "UDM Pro"
        gateway_model = "UDMPRO"
        gateway_version = ""
        gw_cpu = 0.0
        gw_mem = 0.0
        gw_temp: Optional[float] = None
        total_poe_watts = 0.0
        online_devices = 0

        async with pool.acquire() as conn:
            for dev in device_data:
                mac = str(dev.get("mac", "")).lower()
                if not mac:
                    continue
                ip_addr = str(dev.get("ip", ""))
                name = str(dev.get("name") or dev.get("model") or mac)
                model = str(dev.get("model", ""))
                dev_type = str(dev.get("type", "udm"))
                fw_ver = str(dev.get("version", ""))
                state_code = int(dev.get("state", 1) or 0)
                status = "online" if state_code == 1 else "offline"
                if state_code == 1:
                    online_devices += 1

                uptime_sec = int(dev.get("uptime", 0) or 0)
                sys_stats = dev.get("system-stats") or {}
                cpu_pct = float(sys_stats.get("cpu", 0.0) or 0.0)
                mem_pct = float(sys_stats.get("mem", 0.0) or 0.0)

                temp_val: Optional[float] = None
                temps = dev.get("temperatures") or []
                if isinstance(temps, list) and temps:
                    first_t = temps[0].get("value")
                    if first_t is not None:
                        temp_val = float(first_t)
                elif dev.get("general_temperature") is not None:
                    temp_val = float(dev.get("general_temperature"))

                # Calculate switch PoE wattage across port_table
                port_table = dev.get("port_table") or []
                dev_poe_watts = 0.0
                slim_ports = []
                for p in port_table:
                    if not isinstance(p, dict):
                        continue
                    poe_w = float(p.get("poe_power", 0.0) or 0.0)
                    dev_poe_watts += poe_w
                    slim_ports.append(
                        {
                            "port_idx": p.get("port_idx"),
                            "name": p.get("name"),
                            "up": bool(p.get("up", False)),
                            "speed": p.get("speed", 0),
                            "poe_enable": bool(p.get("poe_enable", False)),
                            "poe_power": round(poe_w, 2),
                            "rx_bps": round(float(p.get("rx_bytes-r", 0.0) or 0.0) * 8.0, 1),
                            "tx_bps": round(float(p.get("tx_bytes-r", 0.0) or 0.0) * 8.0, 1),
                        }
                    )

                total_poe_watts += dev_poe_watts
                poe_max = float(dev.get("total_max_power", 0.0) or 0.0)
                num_sta = int(dev.get("num_sta", 0) or 0)
                rx_bps = float(dev.get("rx_bytes-r", 0.0) or 0.0) * 8.0
                tx_bps = float(dev.get("tx_bytes-r", 0.0) or 0.0) * 8.0

                if dev_type in ("ugw", "udm", "uxg") or "UDM" in model.upper():
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
                        mac, ip_address, name, model, device_type, firmware_version,
                        status, uptime_sec, cpu_usage_pct, mem_usage_pct, temp_celsius,
                        poe_total_watts, poe_max_watts, num_sta, rx_bps, tx_bps, port_table, last_seen
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6,
                        $7, $8, $9, $10, $11,
                        $12, $13, $14, $15, $16, $17::jsonb, NOW()
                    )
                    ON CONFLICT (mac) DO UPDATE SET
                        ip_address = EXCLUDED.ip_address,
                        name = EXCLUDED.name,
                        model = EXCLUDED.model,
                        device_type = EXCLUDED.device_type,
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
                    name,
                    model,
                    dev_type,
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

            # Upsert active clients
            for sta in client_data:
                mac = str(sta.get("mac", "")).lower()
                if not mac:
                    continue
                ip_addr = str(sta.get("ip", ""))
                hostname = str(sta.get("name") or sta.get("hostname") or ip_addr or mac)
                oui = str(sta.get("oui", ""))
                is_wired = bool(sta.get("is_wired", False))
                net_name = str(sta.get("network", ""))
                vlan_val = sta.get("vlan")
                vlan_id = int(vlan_val) if vlan_val is not None and str(vlan_val).isdigit() else None
                sw_mac = str(sta.get("sw_mac", "")).lower() or None
                sw_port = int(sta.get("sw_port")) if sta.get("sw_port") is not None else None
                ap_mac = str(sta.get("ap_mac", "")).lower() or None
                essid = str(sta.get("essid", "")) or None
                channel = int(sta.get("channel")) if sta.get("channel") is not None else None
                rssi = int(sta.get("rssi")) if sta.get("rssi") is not None else None
                signal = int(sta.get("signal")) if sta.get("signal") is not None else None
                rx_bps = float(sta.get("rx_bytes-r", 0.0) or 0.0) * 8.0
                tx_bps = float(sta.get("tx_bytes-r", 0.0) or 0.0) * 8.0
                rx_bytes = int(sta.get("rx_bytes", 0) or 0)
                tx_bytes = int(sta.get("tx_bytes", 0) or 0)
                uptime_sec = int(sta.get("uptime", 0) or 0)

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

            # Insert gateway hypertable sample
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
                self.site,
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

        return {
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
