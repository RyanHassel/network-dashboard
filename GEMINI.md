# NetDash — Project Context & Architectural Rules for Google Antigravity

This `GEMINI.md` file transfers the complete context, architectural decisions, and implementation roadmap for **NetDash**, a custom self-hosted Network Observability Dashboard. Google Antigravity automatically loads this file into workspace context on every turn.

## 1. Project Goal & Target Environment

- **Host Platform**: Proxmox VE KVM Virtual Machine (4 vCPUs `host` type, 8 GB fixed RAM, 120 GB VirtIO SCSI SSD, VirtIO NIC on Management VLAN).
- **Base OS**: Debian 13 (Trixie) or Debian 12 (Bookworm).
- **Deployment Model**: 100% containerized via Docker Compose (`docker-compose.yml`).
- **Bootstrap & Auto-Update Workflow**:
  - `install-netdash.sh` runs on a fresh Debian installation (`curl -fsSL https://raw.githubusercontent.com/<USER>/<REPO>/main/install-netdash.sh | sudo bash`).
  - It installs only core host prerequisites (`docker-ce`, `docker-compose-plugin`, `git`, `ufw`, kernel `sysctl` tuning), prompts interactively for `.env` configuration, clones this GitHub repository into `/opt/netdash`, and enables `netdash-updater.timer` (`systemd`).
  - Every 5 minutes (or via `sudo netdash-update`), the server fetches `origin/main` from GitHub and runs `docker compose up -d --build --remove-orphans` whenever a new commit is detected.

## 2. Core Functional Requirements

1. **UniFi Dream Machine Pro (UDM Pro) Telemetry**:
   - Connect to the local UDM Pro gateway (`https://${UNIFI_HOST}`) using `X-API-KEY` (UniFi Network Integration API) and local controller endpoints (`/proxy/network/api/s/${UNIFI_SITE}/stat/health`, `stat/device`, `stat/sta`).
   - Poll every `UNIFI_POLL_INTERVAL` (default 30s) for WAN throughput/latency, gateway CPU/RAM/temps, switch port PoE wattage, and active client signal metrics.
2. **Asynchronous SNMP Polling & Indefinite Trend Retention**:
   - Poll up to 50 devices every `SNMP_POLL_INTERVAL` (default 60s) using async `PySNMP` (`v2c` or `v3`).
   - Collect 64-bit interface counters (`IF-MIB::ifHCInOctets`, `ifHCOutOctets`, `ifInErrors`), `SNMPv2-MIB::sysUpTime`, and `HOST-RESOURCES-MIB` CPU/memory metrics.
   - Store raw 1-minute samples in TimescaleDB hypertable `snmp_metrics_raw` (compressed after 7 days, retained for `SNMP_RAW_RETENTION_DAYS`, default 180 days).
   - Roll up `min`, `avg`, `max`, and `p95` values into Continuous Aggregates (`snmp_metrics_hourly` and `snmp_metrics_daily`) retained **indefinitely** for multi-year trend graphing.
3. **Allowed-Subnet Network Scanner (<10 Subnets)**:
   - Sweep comma-separated CIDRs defined in `ALLOWED_SUBNETS` every `SUBNET_SCAN_MINUTES` (default 15m) using `python-nmap` (`nmap -sn -PE -PP`).
   - Correlate discovered IPs with MAC addresses and OUI vendors from the UDM Pro client table and router ARP MIBs (`IP-MIB::ipNetToMediaPhysAddress`) across routed VLANs.
4. **Remote Syslog Ingestion (30+ Day Retention)**:
   - Listen on UDP/TCP port `SYSLOG_PORT` (default 514) via a lightweight `Vector` container (`timberio/vector`).
   - Normalize RFC 3164/5424 syslog fields and batch-insert into the `device_logs` TimescaleDB hypertable.
   - Compress log chunks older than 3 days and automatically drop chunks older than `LOG_RETENTION_DAYS` (default 30 days).
5. **Modern, Simplistic Dashboard Styling**:
   - Frontend built with React 19 + TypeScript + Vite, Tailwind CSS v4, and shadcn/ui components served via Nginx.
   - Clean dark/slate monochrome aesthetic (`#090D16` background, `#111827` surface, `1px` borders, `tabular-nums` monospace metrics) with semantic status accents (Emerald `#10B981`, Amber `#F59E0B`, Rose `#F43F5E`).
   - Four primary views: **Pulse Overview**, **SNMP Trends & Ports**, **Subnet Scanner Matrix**, and **Live Syslog Stream**.

## 3. Target Repository File Structure

```text
netdash/
├── GEMINI.md                        # Antigravity workspace context & architectural rules
├── install-netdash.sh               # Base OS prerequisite & GitHub bootstrap installer
├── docker-compose.yml               # Production multi-container orchestration
├── .env.example                     # Template for runtime environment variables
├── db-init/
│   └── 01_init_timescaledb.sql      # Hypertables, compression & continuous aggregate policies
├── vector/
│   └── vector.yaml                  # UDP/TCP 514 syslog parser -> TimescaleDB sink
├── backend/
│   ├── Dockerfile                   # Python 3.12 + nmap + snmp + FastAPI/Worker runtime
│   ├── requirements.txt             # FastAPI, asyncpg/psycopg2, httpx, pysnmp, python-nmap, redis
│   └── app/
│       ├── main.py                  # FastAPI REST & WebSocket endpoints
│       ├── worker.py                # Async scheduler for UDM Pro, PySNMP, and Nmap loops
│       ├── unifi.py                 # UDM Pro Integration & Controller API client
│       ├── snmp_poller.py           # Async PySNMP GETBULK collector
│       └── scanner.py               # Allowed-CIDR Nmap + ARP correlation scanner
└── frontend/
    ├── Dockerfile                   # Multi-stage Node 22 build -> Nginx 1.27 Alpine
    ├── nginx.conf                   # SPA routing + /api and /ws reverse proxy
    └── src/                         # React 19 + Tailwind CSS v4 + Recharts/uPlot views
```

## 4. Coding & Architectural Conventions

- **Environment Variables**: Never hardcode IPs, API keys, or credentials. Always read from the variables generated by `install-netdash.sh` in `/opt/netdash/.env` (`DATABASE_URL`, `SYNC_DATABASE_URL`, `REDIS_URL`, `UNIFI_HOST`, `UNIFI_API_KEY`, `SNMP_VERSION`, `SNMP_COMMUNITY`, `ALLOWED_SUBNETS`, `LOG_RETENTION_DAYS`, `WEB_PORT`).
- **Database Queries**: Route time-range queries `<= 24h` to `snmp_metrics_raw` and queries `> 24h` (`7d`, `30d`, `1y`, `All`) to `snmp_metrics_hourly` or `snmp_metrics_daily`.
- **Worker Capabilities**: Keep `network_mode: host` and `cap_add: [NET_RAW, NET_ADMIN]` on the `worker` service in `docker-compose.yml` so Nmap and ARP discovery work across subnets.

## 5. Next Implementation Tasks for Antigravity Agent

When prompted to build out the repository, execute these phases in order:
1. **Phase 1**: Generate `docker-compose.yml`, `.env.example`, `db-init/01_init_timescaledb.sql`, and `vector/vector.yaml`.
2. **Phase 2**: Implement the FastAPI backend (`backend/app/main.py`) and async collector loops (`unifi.py`, `snmp_poller.py`, `scanner.py`, `worker.py`).
3. **Phase 3**: Scaffold the React + Vite + Tailwind CSS v4 frontend (`frontend/`) with the four core views and live WebSocket updates.
