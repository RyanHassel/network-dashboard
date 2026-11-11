# NetDash — Self-Hosted Network Observability Dashboard

A custom, high-density network observability stack for **UniFi Dream Machine Pro (UDM Pro)** telemetry, **asynchronous SNMPv2c/v3 polling** with indefinite trend rollups, **allowed-subnet Nmap + ARP discovery**, and **RFC 3164/5424 remote syslog ingestion**.

---

## Quick Start — Proxmox VE One-Line Installer (`community-scripts.org` style)

To create a new **NetDash KVM Virtual Machine** and automatically configure Debian 12/13, Docker CE, kernel `sysctl` network buffers, UFW firewall, GitOps auto-updater, and the NetDash container stack, run the command below directly in the **Proxmox VE Host Shell**:

### Private Repository (Recommended while private)
Replace `ghp_YOUR_TOKEN` with a GitHub Personal Access Token (`repo` / `Contents: Read-only` scope):

```bash
GITHUB_TOKEN="ghp_YOUR_TOKEN" bash -c "$(curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" https://raw.githubusercontent.com/RyanHassel/network-dashboard/main/install-netdash.sh)"
```

*(Because `GITHUB_TOKEN` is exported in the command, the installer automatically passes it into the VM's `/root/.git-credentials` (`0600`) so both the initial `git clone` and the 5-minute `netdash-updater.timer` authenticate to your private repository seamlessly.)*

### Public Repository (If made public later)
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyanHassel/network-dashboard/main/install-netdash.sh)"
```

### What the Proxmox VE Installer Does
1. **Interactive `whiptail` Wizard (Default or Advanced Mode)**:
   - **Default Mode**: Auto-selects the next free `VMID`, `4 vCPUs (host)`, `8 GB fixed RAM`, `120 GB VirtIO SCSI SSD` (`discard=on, iothread=1, ssd=1`), `vmbr0` (`DHCP`), and prompts only for your storage pool and NetDash telemetry settings (UDM Pro IP & API key, SNMP community & targets, allowed subnets).
   - **Advanced Mode**: Customize `VMID`, hostname, Debian 12 (Bookworm) or Debian 13 (Trixie), CPU cores, RAM, disk size, network bridge, Management **VLAN tag**, **Static IPv4/Gateway/DNS**, SSH password, and SSH public key.
2. **Cloud-Image & Cloud-Init Provisioning**:
   - Downloads the official Debian GenericCloud `.qcow2` image into `/var/lib/vz/template/cache/`.
   - Creates the KVM VM (`qm create` / `qm importdisk` / `qm resize`) and attaches a Cloud-Init vendor snippet.
   - Adds formatted HTML status notes and tags (`netdash;observability;docker`) to the VM in the Proxmox Web UI.
3. **Zero-Touch Guest Bootstrap & 5-Minute GitOps Auto-Update**:
   - Inside the new VM, Cloud-Init automatically installs `qemu-guest-agent`, `docker-ce`, `docker-compose-plugin`, `git`, and `ufw`, applies high-rate UDP syslog kernel `sysctl` tuning, clones this repository into `/opt/netdash`, generates `/opt/netdash/.env` (`0600`), and starts the Docker Compose stack.
   - Enables `netdash-updater.timer` (`systemd`), which polls `origin/main` every **5 minutes** and automatically runs `docker compose up -d --build --remove-orphans` whenever you push a commit to GitHub.
   - Tracks provisioning via the QEMU Guest Agent and prints the live **Dashboard URL** (`http://<VM_IP>:8080`) and **Syslog endpoint** (`<VM_IP>:514`) right in your Proxmox shell.

---

## Alternative: Direct Debian 12 / 13 Guest Installation

If you already have a running Debian 12 or Debian 13 VM/server, running the exact same command inside the guest OS will automatically detect it is inside Debian and bootstrap NetDash directly:

```bash
GITHUB_TOKEN="ghp_YOUR_TOKEN" bash -c "$(curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" https://raw.githubusercontent.com/RyanHassel/network-dashboard/main/install-netdash.sh)"
```

## Useful Commands (Inside the NetDash VM)

| Action | Command |
| :--- | :--- |
| Force immediate GitHub pull & container rebuild | `sudo netdash-update --force` |
| View auto-updater timer status | `systemctl status netdash-updater.timer` |
| View first-boot Cloud-Init provisioning log | `sudo tail -f /var/log/netdash-bootstrap.log` |
| View running containers | `cd /opt/netdash && sudo docker compose ps` |
