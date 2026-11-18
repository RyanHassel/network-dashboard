# NetDash — Self-Hosted Network Observability Dashboard

A custom, high-density network observability stack for **UniFi Dream Machine Pro (UDM Pro)** telemetry, **asynchronous SNMPv2c/v3 polling** with indefinite trend rollups, **allowed-subnet Nmap + ARP discovery**, and **RFC 3164/5424 remote syslog ingestion**.

---

## Quick Start — Proxmox VE One-Line Installer (`community-scripts.org` style)

To create a new **NetDash KVM Virtual Machine** and automatically configure Debian 12/13, Docker CE, kernel `sysctl` network buffers, UFW firewall, GitOps auto-updater, and the NetDash container stack, run the command below directly in the **Proxmox VE Host Shell**:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyanHassel/network-dashboard/main/install-netdash.sh)"
```

### What the Proxmox VE Installer Does
1. **Interactive `whiptail` Wizard (Default or Advanced Mode)**:
   - **Default Mode**: Auto-selects the next free `VMID`, `4 vCPUs (host)`, `8 GB fixed RAM`, `120 GB VirtIO SCSI SSD` (`discard=on, iothread=1, ssd=1`), lets you choose your Proxmox storage pool, **network bridge (`vmbr0`, `vmbr1`, etc.)**, optional **VLAN tag**, **DHCP vs. Static IPv4**, local console/SSH password, and NetDash telemetry settings (UDM Pro IP & API key, SNMP community & targets, allowed subnets).
   - **Advanced Mode**: Customize `VMID`, hostname, Debian 12 (Bookworm) or Debian 13 (Trixie), CPU cores, RAM, disk size, SSH public key, and custom repository URL.
2. **Cloud-Image & Cloud-Init Provisioning**:
   - Downloads the official Debian GenericCloud `.qcow2` image into `/var/lib/vz/template/cache/`.
   - Creates the KVM VM (`qm create` / `qm importdisk` / `qm resize`) and attaches a Cloud-Init vendor snippet.
   - Adds formatted HTML status notes and tags (`netdash;observability;docker`) to the VM in the Proxmox Web UI.
3. **Zero-Touch Guest Bootstrap & Prompt-Driven Update Workflow**:
   - Inside the new VM, the first-boot service automatically installs `qemu-guest-agent`, `net-tools`, `docker-ce`, `docker-compose-plugin`, `git`, and `ufw`, applies high-rate UDP syslog kernel `sysctl` tuning, clones this repository into `/opt/netdash`, generates `/opt/netdash/.env` (`0600`), and builds/starts the full 6-container NetDash stack (`timescaledb`, `redis`, `vector`, `api`, `worker`, and `frontend` Nginx on port `8080`).
   - **Never Auto-Installs Updates**: `netdash-check.timer` runs `git fetch origin main` in the background without modifying the running server. Whenever new commits exist on GitHub:
     - **In the Web UI (`http://<VM_IP>:8080`)**: An **"Update Available"** modal prompt and header badge appear with the pending commit changelog and a **"Download & Install Now"** button.
     - **In the VM Console / SSH**: An interactive `[y/N]` prompt appears on login asking if you want to download and install the pending commits now.

---

## Alternative: Direct Debian 12 / 13 Guest Installation

If you already have a running Debian 12 or Debian 13 VM/server, running the exact same command inside the guest OS will automatically detect it is inside Debian and bootstrap NetDash directly:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyanHassel/network-dashboard/main/install-netdash.sh)"
```

## Useful Commands (Inside the NetDash VM)

| Action | Command |
| :--- | :--- |
| Force immediate GitHub pull & container rebuild | `sudo netdash-update --force` |
| View auto-updater timer status | `systemctl status netdash-updater.timer` |
| View first-boot Cloud-Init provisioning log | `sudo tail -f /var/log/netdash-bootstrap.log` |
| View running containers | `cd /opt/netdash && sudo docker compose ps` |
