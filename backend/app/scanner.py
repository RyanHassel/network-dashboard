import asyncio
import ipaddress
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

import asyncpg
import nmap

logger = logging.getLogger("netdash.scanner")


class SubnetScanner:
    """Allowed-CIDR Nmap ping/ARP scanner with UniFi client & OS ARP cache correlation."""

    def __init__(self) -> None:
        raw_subnets = os.getenv("ALLOWED_SUBNETS", "").strip()
        self.subnets: List[str] = []
        for s in raw_subnets.split(","):
            cidr = s.strip()
            if not cidr:
                continue
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                if net.version == 4:
                    self.subnets.append(str(net))
            except ValueError:
                logger.warning("Ignoring invalid subnet CIDR in ALLOWED_SUBNETS: %s", cidr)
        # Cap at 10 subnets per architectural requirement
        self.subnets = self.subnets[:10]

    @staticmethod
    def _read_local_arp_table() -> Dict[str, str]:
        arp_map: Dict[str, str] = {}
        try:
            out = subprocess.check_output(["ip", "neigh", "show"], text=True, timeout=5)
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and "lladdr" in parts:
                    ip_str = parts[0]
                    idx = parts.index("lladdr")
                    if idx + 1 < len(parts):
                        arp_map[ip_str] = parts[idx + 1].lower()
        except Exception:
            pass
        return arp_map

    @staticmethod
    def _run_nmap_sweep(cidr: str) -> Dict[str, Dict[str, Any]]:
        nm = nmap.PortScanner()
        discovered: Dict[str, Dict[str, Any]] = {}
        try:
            nm.scan(hosts=cidr, arguments="-sn -PE -PP -T4 --max-retries 1 --host-timeout 5s")
            for host in nm.all_hosts():
                state = nm[host].state()
                if state != "up":
                    continue
                addresses = nm[host].get("addresses", {})
                mac = str(addresses.get("mac", "")).lower() or None
                vendor_dict = nm[host].get("vendor", {})
                vendor = next(iter(vendor_dict.values()), "") if isinstance(vendor_dict, dict) and vendor_dict else ""
                hostname = nm[host].hostname() or ""
                discovered[host] = {
                    "mac": mac,
                    "vendor": vendor,
                    "hostname": hostname,
                }
        except Exception as exc:
            logger.error("Nmap sweep failed on %s: %s", cidr, exc)
        return discovered

    async def scan_all_subnets(self, pool: asyncpg.Pool) -> List[Dict[str, Any]]:
        if not self.subnets:
            return []

        summaries: List[Dict[str, Any]] = []
        loop = asyncio.get_running_loop()

        # Pre-load UniFi client correlation lookup by IP and MAC
        unifi_by_ip: Dict[str, Dict[str, Any]] = {}
        unifi_by_mac: Dict[str, Dict[str, Any]] = {}
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT mac, ip_address, hostname, oui_vendor, vlan_id, sw_mac, sw_port
                FROM unifi_clients
                WHERE last_seen > NOW() - INTERVAL '24 hours'
                """
            )
            for r in rows:
                entry = dict(r)
                if entry.get("ip_address"):
                    unifi_by_ip[entry["ip_address"]] = entry
                if entry.get("mac"):
                    unifi_by_mac[entry["mac"].lower()] = entry

        for cidr in self.subnets:
            t0 = time.perf_counter()
            net_obj = ipaddress.ip_network(cidr, strict=False)
            total_ips = max(0, net_obj.num_addresses - 2)

            discovered = await loop.run_in_executor(None, self._run_nmap_sweep, cidr)
            arp_map = await loop.run_in_executor(None, self._read_local_arp_table)

            # Also include active UniFi clients known to be inside this subnet
            for u_ip, u_info in unifi_by_ip.items():
                try:
                    if ipaddress.ip_address(u_ip) in net_obj and u_ip not in discovered:
                        discovered[u_ip] = {
                            "mac": u_info.get("mac"),
                            "vendor": u_info.get("oui_vendor") or "",
                            "hostname": u_info.get("hostname") or "",
                            "source": "unifi",
                        }
                except ValueError:
                    continue

            new_hosts = 0
            async with pool.acquire() as conn:
                existing_ips = {
                    str(r["ip_address"])
                    for r in await conn.fetch(
                        "SELECT host(ip_address) AS ip_address FROM discovered_hosts WHERE subnet_cidr = $1",
                        cidr,
                    )
                }

                # Mark all existing hosts in this subnet offline initially, then mark online ones up
                await conn.execute(
                    "UPDATE discovered_hosts SET is_online = FALSE WHERE subnet_cidr = $1",
                    cidr,
                )

                for ip_str, info in discovered.items():
                    if ip_str not in existing_ips:
                        new_hosts += 1

                    mac: Optional[str] = info.get("mac") or arp_map.get(ip_str)
                    u_match = unifi_by_ip.get(ip_str) or (unifi_by_mac.get(mac) if mac else None) or {}
                    if not mac and u_match.get("mac"):
                        mac = str(u_match["mac"]).lower()

                    hostname = info.get("hostname") or u_match.get("hostname") or ip_str
                    vendor = info.get("vendor") or u_match.get("oui_vendor") or ""
                    vlan_id = u_match.get("vlan_id")
                    sw_mac = u_match.get("sw_mac")
                    sw_port = u_match.get("sw_port")
                    src = info.get("source", "nmap+arp" if mac else "nmap")

                    await conn.execute(
                        """
                        INSERT INTO discovered_hosts (
                            ip_address, subnet_cidr, mac_address, hostname, vendor,
                            vlan_id, is_online, discovery_source, switch_mac, switch_port,
                            first_seen, last_seen
                        ) VALUES (
                            $1::inet, $2, $3, $4, $5,
                            $6, TRUE, $7, $8, $9,
                            NOW(), NOW()
                        )
                        ON CONFLICT (ip_address) DO UPDATE SET
                            subnet_cidr = EXCLUDED.subnet_cidr,
                            mac_address = COALESCE(EXCLUDED.mac_address, discovered_hosts.mac_address),
                            hostname = CASE
                                WHEN EXCLUDED.hostname IS NOT NULL AND EXCLUDED.hostname != host(EXCLUDED.ip_address)
                                THEN EXCLUDED.hostname
                                ELSE COALESCE(discovered_hosts.hostname, EXCLUDED.hostname)
                            END,
                            vendor = CASE
                                WHEN EXCLUDED.vendor IS NOT NULL AND EXCLUDED.vendor != ''
                                THEN EXCLUDED.vendor
                                ELSE discovered_hosts.vendor
                            END,
                            vlan_id = COALESCE(EXCLUDED.vlan_id, discovered_hosts.vlan_id),
                            is_online = TRUE,
                            discovery_source = EXCLUDED.discovery_source,
                            switch_mac = COALESCE(EXCLUDED.switch_mac, discovered_hosts.switch_mac),
                            switch_port = COALESCE(EXCLUDED.switch_port, discovered_hosts.switch_port),
                            last_seen = NOW()
                        """,
                        ip_str,
                        cidr,
                        mac,
                        hostname,
                        vendor,
                        vlan_id,
                        src,
                        sw_mac,
                        sw_port,
                    )

                duration_sec = round(time.perf_counter() - t0, 2)
                await conn.execute(
                    """
                    INSERT INTO subnet_scan_runs (
                        scan_time, subnet_cidr, total_ips, hosts_up, new_hosts, scan_duration_sec
                    ) VALUES (
                        NOW(), $1, $2, $3, $4, $5
                    )
                    """,
                    cidr,
                    total_ips,
                    len(discovered),
                    new_hosts,
                    duration_sec,
                )

                summaries.append(
                    {
                        "subnet_cidr": cidr,
                        "total_ips": total_ips,
                        "hosts_up": len(discovered),
                        "new_hosts": new_hosts,
                        "scan_duration_sec": duration_sec,
                    }
                )

        return summaries
