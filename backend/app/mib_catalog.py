import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

MIBS_DIR = Path(os.getenv("NETDASH_MIBS_DIR", "/app/mibs"))

# Built-in Vendor Enterprise PEN & Keyword Fingerprint Catalog
VENDOR_CATALOG: List[Dict[str, Any]] = [
    {
        "key": "ubiquiti",
        "pens": ["41112", "10002"],
        "keywords": ["ubiquiti", "unifi", "ubnt", "edgeos", "edgerouter", "edgeswitch", "u6-", "u7-", "uap", "usw", "udm"],
        "vendor_name": "Ubiquiti Networks",
        "category": "Network Switch / AP / Gateway",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "LLDP-MIB", "UBNT-UniFi-MIB", "FROGFOOT-RESOURCES-MIB", "UCD-SNMP-MIB"],
    },
    {
        "key": "cisco",
        "pens": ["9"],
        "keywords": ["cisco", "ios-xe", "nx-os", "catalyst", "adaptive security"],
        "vendor_name": "Cisco Systems",
        "category": "Enterprise Router / Switch / Firewall",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "LLDP-MIB", "CISCO-PROCESS-MIB", "CISCO-MEMORY-POOL-MIB", "CISCO-ENVMON-MIB"],
    },
    {
        "key": "mikrotik",
        "pens": ["14988"],
        "keywords": ["mikrotik", "routeros", "routerboard", "ccr", "crs"],
        "vendor_name": "MikroTik",
        "category": "RouterOS Router / Switch",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "LLDP-MIB", "MIKROTIK-MIB", "HOST-RESOURCES-MIB"],
    },
    {
        "key": "synology",
        "pens": ["6574"],
        "keywords": ["synology", "diskstation", "rackstation", "dsm"],
        "vendor_name": "Synology Inc.",
        "category": "NAS / Storage Server",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "HOST-RESOURCES-MIB", "UCD-SNMP-MIB", "SYNOLOGY-SYSTEM-MIB", "SYNOLOGY-DISK-MIB", "SYNOLOGY-RAID-MIB"],
    },
    {
        "key": "fortinet",
        "pens": ["12356"],
        "keywords": ["fortinet", "fortigate", "fortios", "fortiswitch"],
        "vendor_name": "Fortinet",
        "category": "FortiGate Security Appliance",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "LLDP-MIB", "FORTINET-FORTIGATE-MIB"],
    },
    {
        "key": "apc",
        "pens": ["318"],
        "keywords": ["apc", "smart-ups", "powernet", "schneider", "symmetra"],
        "vendor_name": "APC by Schneider Electric",
        "category": "UPS / Power Distribution",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "PowerNet-UPS-MIB", "UPS-MIB"],
    },
    {
        "key": "cyberpower",
        "pens": ["3808"],
        "keywords": ["cyberpower", "rmcard"],
        "vendor_name": "CyberPower Systems",
        "category": "UPS / Power Protection",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "UPS-MIB", "CPS-MIB"],
    },
    {
        "key": "eaton",
        "pens": ["534"],
        "keywords": ["eaton", "powerware"],
        "vendor_name": "Eaton Corporation",
        "category": "UPS / Power Management",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "UPS-MIB", "ENTITY-MIB"],
    },
    {
        "key": "truenas",
        "pens": ["50536"],
        "keywords": ["truenas", "freenas", "ixsystems"],
        "vendor_name": "iXsystems TrueNAS",
        "category": "ZFS Storage Appliance",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "HOST-RESOURCES-MIB", "UCD-SNMP-MIB", "LM-SENSORS-MIB"],
    },
    {
        "key": "qnap",
        "pens": ["24681"],
        "keywords": ["qnap", "qts"],
        "vendor_name": "QNAP Systems",
        "category": "NAS / Storage Server",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "HOST-RESOURCES-MIB", "UCD-SNMP-MIB"],
    },
    {
        "key": "paloalto",
        "pens": ["25461"],
        "keywords": ["palo alto", "pan-os"],
        "vendor_name": "Palo Alto Networks",
        "category": "Next-Gen Firewall",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "HOST-RESOURCES-MIB", "PAN-COMMON-MIB"],
    },
    {
        "key": "sonicwall",
        "pens": ["8741"],
        "keywords": ["sonicwall", "sonicos"],
        "vendor_name": "SonicWall",
        "category": "Network Firewall",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "SONICWALL-FIREWALL-IP-STATISTICS-MIB"],
    },
    {
        "key": "juniper",
        "pens": ["2636"],
        "keywords": ["juniper", "junos"],
        "vendor_name": "Juniper Networks",
        "category": "Router / Switch / Firewall",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "LLDP-MIB", "JUNIPER-MIB"],
    },
    {
        "key": "arista",
        "pens": ["30065"],
        "keywords": ["arista", "eos"],
        "vendor_name": "Arista Networks",
        "category": "Data Center Switch",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "HOST-RESOURCES-MIB", "LLDP-MIB"],
    },
    {
        "key": "aruba",
        "pens": ["14823"],
        "keywords": ["aruba", "arubaos", "instant"],
        "vendor_name": "HPE Aruba Networking",
        "category": "Switch / Wireless Controller",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "LLDP-MIB"],
    },
    {
        "key": "hpe",
        "pens": ["11"],
        "keywords": ["procurve", "hewlett-packard", "hpe", "officeconnect", "ilo"],
        "vendor_name": "Hewlett Packard Enterprise",
        "category": "Switch / Server / Printer",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "HOST-RESOURCES-MIB", "LLDP-MIB", "Printer-MIB"],
    },
    {
        "key": "dell",
        "pens": ["674"],
        "keywords": ["dell", "powerconnect", "powerswitch", "idrac", "poweredge"],
        "vendor_name": "Dell Technologies",
        "category": "Switch / PowerEdge Server",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "HOST-RESOURCES-MIB", "LLDP-MIB"],
    },
    {
        "key": "netgear",
        "pens": ["4526"],
        "keywords": ["netgear", "prosafe"],
        "vendor_name": "NETGEAR",
        "category": "Managed Switch",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "LLDP-MIB"],
    },
    {
        "key": "tplink",
        "pens": ["11863"],
        "keywords": ["tp-link", "jetstream", "omada"],
        "vendor_name": "TP-Link",
        "category": "Managed Switch / AP",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "LLDP-MIB"],
    },
    {
        "key": "printer",
        "pens": ["2435", "1347", "1602", "367", "641"],
        "keywords": ["brother", "canon", "kyocera", "ricoh", "lexmark", "laserjet", "officejet", "printer"],
        "vendor_name": "Network Printer",
        "category": "Printer / MFP",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "HOST-RESOURCES-MIB", "ENTITY-MIB", "Printer-MIB"],
    },
    {
        "key": "vmware",
        "pens": ["6876"],
        "keywords": ["vmware", "esxi"],
        "vendor_name": "VMware ESXi",
        "category": "Hypervisor Host",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "HOST-RESOURCES-MIB", "ENTITY-MIB", "VMWARE-SYSTEM-MIB"],
    },
    {
        "key": "windows",
        "pens": ["311"],
        "keywords": ["windows", "microsoft"],
        "vendor_name": "Microsoft Windows",
        "category": "Windows Host / Server",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "HOST-RESOURCES-MIB"],
    },
    {
        "key": "linux",
        "pens": ["8072", "2021"],
        "keywords": ["linux", "debian", "ubuntu", "proxmox", "pve", "red hat", "centos", "alpine", "net-snmp"],
        "vendor_name": "Linux / Net-SNMP",
        "category": "Linux Server / Hypervisor",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "HOST-RESOURCES-MIB", "UCD-SNMP-MIB", "LM-SENSORS-MIB", "LLDP-MIB"],
    },
]


def extract_enterprise_pen(sys_object_id: str) -> Optional[str]:
    """Extracts the IANA Private Enterprise Number (PEN) from sysObjectID (1.3.6.1.4.1.<PEN>...)."""
    clean = (sys_object_id or "").strip().lstrip(".")
    m = re.match(r"^1\.3\.6\.1\.4\.1\.(\d+)", clean)
    if m:
        return m.group(1)
    return None


def match_device_vendor_profile(sys_object_id: str, sys_descr: str, sys_name: str = "") -> Dict[str, Any]:
    """Matches a polled device's sysObjectID and sysDescr to the most specific vendor MIB profile."""
    pen = extract_enterprise_pen(sys_object_id)
    combined_text = f"{sys_descr or ''} {sys_name or ''}".lower()

    # 1. Check keyword match first for embedded Linux devices (like Ubiquiti/Synology which sometimes report Net-SNMP PEN 8072)
    for entry in VENDOR_CATALOG:
        if entry["key"] == "linux":
            continue
        for kw in entry["keywords"]:
            if kw in combined_text:
                return {
                    **entry,
                    "pen": pen or (entry["pens"][0] if entry["pens"] else None),
                }

    # 2. Check Enterprise PEN match
    if pen:
        for entry in VENDOR_CATALOG:
            if pen in entry["pens"]:
                return {
                    **entry,
                    "pen": pen,
                }

    # 3. Check Linux / fallback keywords
    for entry in VENDOR_CATALOG:
        for kw in entry["keywords"]:
            if kw in combined_text:
                return {
                    **entry,
                    "pen": pen,
                }

    # 4. Generic Standard SNMP Device
    return {
        "key": "generic",
        "pen": pen,
        "vendor_name": f"Enterprise PEN #{pen}" if pen else "Standard SNMP Device",
        "category": "Network Device",
        "mibs": ["SNMPv2-MIB", "IF-MIB", "ENTITY-MIB", "HOST-RESOURCES-MIB", "LLDP-MIB", "IP-MIB"],
    }


def parse_mib_text(content: str, filename: str = "custom.mib") -> Dict[str, Any]:
    """Parses a standard SMIv2 .mib file to extract module name, organization, enterprise PEN, and OBJECT-TYPEs."""
    mod_match = re.search(r"^\s*([A-Za-z0-9_-]+)\s+DEFINITIONS\s*::=\s*BEGIN", content, re.M)
    module_name = mod_match.group(1) if mod_match else Path(filename).stem

    org_match = re.search(r'ORGANIZATION\s+"([^"]+)"', content, re.S)
    organization = org_match.group(1).strip().replace("\n", " ") if org_match else ""

    desc_match = re.search(r'DESCRIPTION\s+"([^"]+)"', content, re.S)
    description = desc_match.group(1).strip().replace("\n", " ")[:240] if desc_match else ""

    pen_match = re.search(r"::=\s*\{\s*enterprises\s+(\d+)", content)
    pen = pen_match.group(1) if pen_match else ""

    objects: List[Dict[str, str]] = []
    for m in re.finditer(
        r"([A-Za-z0-9_-]+)\s+OBJECT-TYPE\s+.*?SYNTAX\s+([^\n\r]+).*?DESCRIPTION\s+\"([^\"]*)\"\s+::=\s*\{\s*([^}]+)\}",
        content,
        re.S,
    ):
        objects.append(
            {
                "name": m.group(1).strip(),
                "syntax": m.group(2).strip(),
                "description": m.group(3).strip().replace("\n", " ")[:140],
                "parent": m.group(4).strip(),
            }
        )

    return {
        "module_name": module_name,
        "filename": filename,
        "organization": organization,
        "description": description,
        "enterprise_pen": pen,
        "object_count": len(objects),
        "objects": objects[:50],
    }


def list_backend_mibs() -> List[Dict[str, Any]]:
    """Lists all .mib files stored on the backend server in /app/mibs."""
    MIBS_DIR.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    for p in sorted(MIBS_DIR.glob("*.mib")) + sorted(MIBS_DIR.glob("*.txt")):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            info = parse_mib_text(text, p.name)
            info["size_bytes"] = p.stat().st_size
            results.append(info)
        except Exception:
            continue
    return results
