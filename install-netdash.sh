#!/usr/bin/env bash
# ==============================================================================
# NetDash — Proxmox VE Automated VM Creator & Guest Bootstrap Script
# Style: Proxmox VE Helper-Scripts (community-scripts.org)
#
# Run directly in the Proxmox VE Host Shell (CLI):
#    Private repo (with GitHub Personal Access Token):
#     export GITHUB_TOKEN="ghp_xxx" && bash -c "$(curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" https://raw.githubusercontent.com/RyanHassel/network-dashboard/main/install-netdash.sh)"
#    Public repo:
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyanHassel/network-dashboard/main/install-netdash.sh)"
#
# Architecture:
#   Uses `virt-customize` (libguestfs-tools) directly on the Debian GenericCloud
#   disk image before boot — baking in root/netdash credentials, SSH keys/config,
#   systemd-networkd (en* / eth* wildcard matching for DHCP or Static IP), and
#   the NetDash first-boot provisioning service so the VM never hangs on
#   Cloud-Init or DHCP wait-online and local console login works immediately.
# ==============================================================================

set -euo pipefail

APP="NetDash"
APP_SLUG="netdash"
INSTALL_DIR="/opt/netdash"
ENV_FILE="${INSTALL_DIR}/.env"
UPDATER_BIN="/usr/local/bin/netdash-update"
BOOTSTRAP_LOG="/var/log/netdash-bootstrap.log"
DEFAULT_REPO_URL="${NETDASH_REPO_URL:-https://github.com/RyanHassel/network-dashboard.git}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

# --- ANSI Colors & Formatting (community-scripts.org style) ---
YW=$(echo "\033[33m")
BL=$(echo "\033[36m")
RD=$(echo "\033[01;31m")
BGN=$(echo "\033[4;92m")
GN=$(echo "\033[1;92m")
DGN=$(echo "\033[32m")
CL=$(echo "\033[m")
BOLD=$(echo "\033[1m")
CM="${GN}✓${CL}"
CROSS="${RD}✗${CL}"
INFO="${BL}ℹ${CL}"

header_info() {
  clear || true
  cat <<"EOF"
    _   __     __  ____            __
   / | / /__  / /_/ __ \____ _____/ /_
  /  |/ / _ \/ __/ / / / __ `/ ___/ __ \
 / /|  /  __/ /_/ /_/ / /_/ (__  ) / / /
/_/ |_/\___/\__/_____/\__,_/____/_/ /_/
   Network Observability Dashboard — Proxmox VE Installer
EOF
  echo -e "${BL}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${CL}\n"
}

msg_info() {
  echo -e " ${INFO} ${YW}$1...${CL}"
}

msg_ok() {
  echo -e " ${CM} ${GN}$1${CL}"
}

msg_warn() {
  echo -e " ${YW}⚠ $1${CL}"
}

msg_error() {
  echo -e " ${CROSS} ${RD}$1${CL}" >&2
}

die() {
  msg_error "$1"
  exit 1
}

check_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "This script must be run as root (or with sudo)."
  fi
}

# ==============================================================================
# MODE 1: PROXMOX VE HOST CLI — VM PROVISIONER (whiptail + virt-customize + qm)
# ==============================================================================

check_proxmox_env() {
  if ! command -v pveversion >/dev/null 2>&1; then
    return 1
  fi
  local pve_ver
  pve_ver="$(pveversion | awk -F'/' '{print $2}' | awk -F'-' '{print $1}')"
  local pve_major="${pve_ver%%.*}"
  if [[ "${pve_major}" -lt 8 ]]; then
    die "Proxmox VE 8.0 or higher is required (detected ${pve_ver})."
  fi
  if [[ "$(dpkg --print-architecture)" != "amd64" ]]; then
    die "This Proxmox VM installer requires an amd64 host architecture."
  fi
  return 0
}

get_next_vmid() {
  local try_id
  try_id="$(pvesh get /cluster/nextid 2>/dev/null || echo 100)"
  while qm status "${try_id}" >/dev/null 2>&1 || pct status "${try_id}" >/dev/null 2>&1; do
    try_id=$((try_id + 1))
  done
  echo "${try_id}"
}

select_storage_pool() {
  local content_type="$1"
  local prompt_label="$2"
  local -a pools=()
  local line pool_name pool_type free_gb

  while read -r line; do
    pool_name="$(echo "${line}" | awk '{print $1}')"
    pool_type="$(echo "${line}" | awk '{print $2}')"
    free_gb="$(echo "${line}" | awk '{printf "%.1fGB free", $6/1048576}')"
    pools+=("${pool_name}" "${pool_type} (${free_gb})" "OFF")
  done < <(pvesm status -content "${content_type}" 2>/dev/null | awk 'NR>1 && $3=="active" {print $0}')

  if [[ "${#pools[@]}" -eq 0 ]]; then
    die "No active Proxmox storage pool supports content type '${content_type}'."
  fi

  pools[2]="ON"

  if [[ "${#pools[@]}" -eq 3 ]]; then
    echo "${pools[0]}"
    return 0
  fi

  local selected
  selected=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --title "Storage Pool (${prompt_label})" \
    --radiolist "\nSelect the Proxmox storage pool for ${prompt_label}:\n" \
    16 68 6 \
    "${pools[@]}" \
    3>&1 1>&2 2>&3) || die "Installation cancelled by user."

  echo "${selected}"
}

select_network_bridge() {
  local -a bridges=()
  local br br_ip vlan_info desc

  while read -r br; do
    [[ -z "${br}" ]] && continue
    br_ip="$(ip -4 -o addr show dev "${br}" 2>/dev/null | awk '{print $4}' | head -n1 || true)"
    vlan_info=""
    if awk "/^iface ${br} /,/^$/" /etc/network/interfaces 2>/dev/null | grep -q "bridge-vlan-aware yes"; then
      local vids
      vids="$(awk "/^iface ${br} /,/^$/" /etc/network/interfaces 2>/dev/null | awk '/bridge-vids/ {$1=""; print substr($0,2)}')"
      vlan_info=" [VLAN-aware${vids:+: ${vids}}]"
    fi
    desc="${br_ip:-no-ip}${vlan_info}"
    bridges+=("${br}" "${desc}" "OFF")
  done < <(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | cut -d'@' -f1)

  if [[ "${#bridges[@]}" -eq 0 ]]; then
    echo "vmbr0"
    return 0
  fi

  # Default first bridge to ON
  bridges[2]="ON"

  if [[ "${#bridges[@]}" -eq 3 ]]; then
    echo "${bridges[0]}"
    return 0
  fi

  local selected
  selected=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --title "Network Bridge Selection" \
    --radiolist "\nSelect the Proxmox Network Bridge for the VM:\n" \
    15 70 5 \
    "${bridges[@]}" \
    3>&1 1>&2 2>&3) || die "Installation cancelled by user."

  echo "${selected}"
}

proxmox_default_settings() {
  VMID="$(get_next_vmid)"
  HN="netdash"
  DEBIAN_VER="12"
  CPU_TYPE="host"
  CORE_COUNT="4"
  RAM_SIZE="8192"
  DISK_SIZE="120"
  BRG="vmbr0"
  VLAN=""
  MAC=""
  NET_MODE="dhcp"
  HOST_GW="$(ip -4 route show default 2>/dev/null | awk '{print $3}' | head -n1 || true)"
  [[ -z "${HOST_GW}" ]] && HOST_GW="192.168.1.1"
  HOST_SUBNET="$(ip -4 route show proto kernel scope link 2>/dev/null | awk '{print $1}' | head -n1 || true)"
  [[ -z "${HOST_SUBNET}" ]] && HOST_SUBNET="192.168.1.0/24"
  STATIC_IP=""
  GATEWAY_IP="${HOST_GW}"
  DNS_SERVER="${HOST_GW}"
  CI_USER="netdash"
  CI_PASSWORD="netdash"
  SSH_KEY=""
}

proxmox_wizard() {
  proxmox_default_settings

  if ! whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --title "${APP} KVM VM Installer" \
    --yesno "This will create a new ${APP} Virtual Machine on Proxmox VE and configure the entire observability stack automatically.\n\nDefault VM Specs: 4 vCPUs, 8 GB RAM, 120 GB SSD.\nDefault Console/SSH Login: root (or netdash) / password: netdash\n\nProceed?" 15 70; then
    die "Exited installer."
  fi

  local install_mode
  install_mode=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --title "Settings Mode" \
    --radiolist "\nChoose VM provisioning settings mode:\n" 12 70 2 \
    "default" "Default Hardware (4 vCPU, 8GB RAM, 120GB SSD)" ON \
    "advanced" "Advanced Hardware (Custom VMID, CPU, RAM, Disk, SSH Key)" OFF \
    3>&1 1>&2 2>&3) || die "Installation cancelled."

  STORAGE_POOL="$(select_storage_pool "images" "VM Virtual Disk")"

  if [[ "${install_mode}" == "advanced" ]]; then
    VMID=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nSet Virtual Machine ID:" 10 58 "${VMID}" \
      --title "VIRTUAL MACHINE ID" 3>&1 1>&2 2>&3) || die "Cancelled."

    HN=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nSet VM Hostname:" 10 58 "${HN}" \
      --title "HOSTNAME" 3>&1 1>&2 2>&3) || die "Cancelled."

    DEBIAN_VER=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --title "DEBIAN BASE OS" \
      --radiolist "\nSelect Base Debian Cloud Image:" 12 64 2 \
      "12" "Debian 12 (Bookworm) — Stable Recommended" ON \
      "13" "Debian 13 (Trixie)" OFF \
      3>&1 1>&2 2>&3) || die "Cancelled."

    CORE_COUNT=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nAllocate CPU Cores (Recommended: 4):" 10 58 "${CORE_COUNT}" \
      --title "CPU CORES" 3>&1 1>&2 2>&3) || die "Cancelled."

    RAM_SIZE=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nAllocate RAM in MiB (Recommended: 8192):" 10 58 "${RAM_SIZE}" \
      --title "RAM IN MiB" 3>&1 1>&2 2>&3) || die "Cancelled."

    DISK_SIZE=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nSet VirtIO SCSI Disk Size in GB (Recommended: 120):" 10 58 "${DISK_SIZE}" \
      --title "DISK SIZE (GB)" 3>&1 1>&2 2>&3) || die "Cancelled."

    SSH_KEY=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nOptional SSH Public Key (ssh-ed25519 / ssh-rsa, or leave blank):" 10 68 "" \
      --title "SSH AUTHORIZED KEY" 3>&1 1>&2 2>&3) || die "Cancelled."

    REPO_URL=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nGitHub Repository HTTPS URL to clone into /opt/netdash:" 10 68 "${DEFAULT_REPO_URL}" \
      --title "GITHUB REPOSITORY" 3>&1 1>&2 2>&3) || die "Cancelled."
  else
    REPO_URL="${DEFAULT_REPO_URL}"
  fi

  # --- Network & Login Settings (Always Prompted so Bridge / VLAN / Static IP / Password never trip up boot) ---
  BRG="$(select_network_bridge)"

  VLAN=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --inputbox "\nSet VLAN Tag on ${BRG} (e.g. 3, 5, 8 — leave blank if untagged):" 10 68 "" \
    --title "VLAN TAG (OPTIONAL)" 3>&1 1>&2 2>&3) || die "Cancelled."

  NET_MODE=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --title "IPv4 NETWORK CONFIGURATION" \
    --radiolist "\nHow should the VM obtain its IPv4 address on ${BRG}${VLAN:+ (VLAN ${VLAN})}?\n" 12 68 2 \
    "dhcp" "DHCP (Automatic IPv4 lease from router)" ON \
    "static" "Static IPv4 (Manually specify IP/CIDR, Gateway & DNS)" OFF \
    3>&1 1>&2 2>&3) || die "Cancelled."

  if [[ "${NET_MODE}" == "static" ]]; then
    STATIC_IP=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nEnter Static IPv4 & CIDR (e.g. ${HOST_GW%.*}.50/24):" 10 64 "${HOST_GW%.*}.50/24" \
      --title "STATIC IPv4 ADDRESS" 3>&1 1>&2 2>&3) || die "Cancelled."

    GATEWAY_IP=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nEnter Default Gateway IPv4:" 10 64 "${HOST_GW}" \
      --title "DEFAULT GATEWAY" 3>&1 1>&2 2>&3) || die "Cancelled."

    DNS_SERVER=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nEnter DNS Server IPv4:" 10 64 "${GATEWAY_IP}" \
      --title "DNS SERVER" 3>&1 1>&2 2>&3) || die "Cancelled."
  fi

  CI_PASSWORD=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --inputbox "\nSet Console & SSH Password for 'root' and 'netdash' users:" 10 68 "${CI_PASSWORD}" \
    --title "LOCAL CONSOLE & SSH PASSWORD" 3>&1 1>&2 2>&3) || die "Cancelled."
  [[ -z "${CI_PASSWORD}" ]] && CI_PASSWORD="netdash"

  # --- NetDash Application Telemetry Settings ---
  UNIFI_HOST=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --inputbox "\nUDM Pro Gateway IP / Hostname:" 10 68 "${GATEWAY_IP}" \
    --title "UNIFI GATEWAY HOST" 3>&1 1>&2 2>&3) || die "Cancelled."

  UNIFI_API_KEY=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --passwordbox "\nUDM Pro Integration API Key (X-API-KEY):" 10 68 "" \
    --title "UNIFI API KEY" 3>&1 1>&2 2>&3) || die "Cancelled."

  UNIFI_SITE=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --inputbox "\nUniFi Site Identifier (default is 'default'):" 10 68 "default" \
    --title "UNIFI SITE" 3>&1 1>&2 2>&3) || die "Cancelled."

  SNMP_COMMUNITY=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --inputbox "\nSNMP v2c Community String:" 10 68 "public" \
    --title "SNMP COMMUNITY" 3>&1 1>&2 2>&3) || die "Cancelled."

  SNMP_TARGETS=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --inputbox "\nSNMP Target Device IPs (comma-separated, up to 50 devices):" 10 68 "${UNIFI_HOST}" \
    --title "SNMP TARGETS" 3>&1 1>&2 2>&3) || die "Cancelled."

  ALLOWED_SUBNETS=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --inputbox "\nAllowed Subnets for Nmap + ARP Scanner (comma-separated CIDRs):" 10 68 "${HOST_SUBNET}" \
    --title "ALLOWED SUBNETS" 3>&1 1>&2 2>&3) || die "Cancelled."

  WEB_PORT="8080"
  SYSLOG_PORT="514"
  SNMP_RAW_RETENTION_DAYS="180"
  LOG_RETENTION_DAYS="30"
  SNMP_VERSION="v2c"
}

provision_proxmox_vm() {
  header_info
  proxmox_wizard
  header_info

  msg_ok "VM ID:          ${BL}${VMID}${CL}"
  msg_ok "Hostname:       ${BL}${HN}${CL}"
  msg_ok "Base OS:        ${BL}Debian ${DEBIAN_VER} GenericCloud (amd64)${CL}"
  msg_ok "Resources:      ${BL}${CORE_COUNT} vCPUs (${CPU_TYPE}), ${RAM_SIZE} MiB RAM, ${DISK_SIZE} GB SSD (${STORAGE_POOL})${CL}"
  msg_ok "Network:        ${BL}${BRG}${VLAN:+ (VLAN ${VLAN})} — ${NET_MODE^^}${STATIC_IP:+ (${STATIC_IP})}${CL}"
  msg_ok "Login Users:    ${BL}root & ${CI_USER} (password set)${CL}"
  msg_ok "GitHub Repo:    ${BL}${REPO_URL}${CL}"
  echo ""

  # 1. Ensure libguestfs-tools (virt-customize) is installed on Proxmox host
  if ! command -v virt-customize >/dev/null 2>&1; then
    msg_info "Installing libguestfs-tools on Proxmox host (one-time setup for image customization)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq libguestfs-tools >/dev/null 2>&1
    msg_ok "Installed libguestfs-tools"
  fi

  # 2. Download Debian GenericCloud Image
  local img_name img_url cache_dir="/var/lib/vz/template/cache"
  mkdir -p "${cache_dir}"
  if [[ "${DEBIAN_VER}" == "13" ]]; then
    img_name="debian-13-genericcloud-amd64.qcow2"
    img_url="https://cloud.debian.org/images/cloud/trixie/daily/latest/${img_name}"
  else
    img_name="debian-12-genericcloud-amd64.qcow2"
    img_url="https://cloud.debian.org/images/cloud/bookworm/latest/${img_name}"
  fi

  local img_path="${cache_dir}/${img_name}"
  if [[ ! -f "${img_path}" ]]; then
    msg_info "Downloading ${img_name} from cloud.debian.org"
    curl -fSL "${img_url}" -o "${img_path}.part"
    mv "${img_path}.part" "${img_path}"
    msg_ok "Downloaded ${img_name}"
  else
    msg_ok "Using cached cloud image ${img_path}"
  fi

  # 3. Stage files & customize a working copy of the disk image via virt-customize
  local work_img="/tmp/netdash-${VMID}.qcow2"
  local stage_dir="/tmp/netdash-stage-${VMID}"
  rm -rf "${work_img}" "${stage_dir}"
  mkdir -p "${stage_dir}"
  cp "${img_path}" "${work_img}"

  local db_pass
  db_pass="$(openssl rand -hex 20)"

  # 3a. Network config for systemd-networkd (matches any VirtIO interface: ens18, enp0s18, eth0)
  if [[ "${NET_MODE}" == "static" ]]; then
    cat << EOF > "${stage_dir}/10-netdash.network"
[Match]
Name=en* eth*

[Network]
Address=${STATIC_IP}
Gateway=${GATEWAY_IP}
DNS=${DNS_SERVER:-1.1.1.1}
DNS=1.1.1.1
EOF
  else
    cat << EOF > "${stage_dir}/10-netdash.network"
[Match]
Name=en* eth*

[Network]
DHCP=ipv4

[DHCPv4]
ClientIdentifier=mac
UseDNS=true
UseRoutes=true
SendHostname=true
EOF
  fi

  # 3b. Kernel sysctl tuning
  cat << 'EOF' > "${stage_dir}/99-netdash.conf"
net.core.rmem_max = 26214400
net.core.rmem_default = 4194304
net.core.wmem_max = 26214400
net.core.netdev_max_backlog = 10000
net.core.somaxconn = 4096
vm.overcommit_memory = 1
EOF

  # 3c. Seed environment file
  cat << EOF > "${stage_dir}/netdash-seed.env"
REPO_URL=${REPO_URL}
GITHUB_TOKEN=${GITHUB_TOKEN}
POSTGRES_USER=netdash
POSTGRES_PASSWORD=${db_pass}
POSTGRES_DB=netdash
DATABASE_URL=postgresql://netdash:${db_pass}@timescaledb:5432/netdash
SYNC_DATABASE_URL=postgresql://netdash:${db_pass}@timescaledb:5432/netdash
REDIS_URL=redis://redis:6379/0
UNIFI_HOST=${UNIFI_HOST}
UNIFI_API_KEY=${UNIFI_API_KEY}
UNIFI_SITE=${UNIFI_SITE}
UNIFI_VERIFY_SSL=false
UNIFI_POLL_INTERVAL=30
SNMP_VERSION=${SNMP_VERSION}
SNMP_COMMUNITY=${SNMP_COMMUNITY}
SNMP_TARGETS=${SNMP_TARGETS}
SNMP_PORT=161
SNMP_POLL_INTERVAL=60
SNMP_RAW_RETENTION_DAYS=${SNMP_RAW_RETENTION_DAYS}
ALLOWED_SUBNETS=${ALLOWED_SUBNETS}
SUBNET_SCAN_MINUTES=15
SYSLOG_PORT=${SYSLOG_PORT}
LOG_RETENTION_DAYS=${LOG_RETENTION_DAYS}
WEB_PORT=${WEB_PORT}
EOF

  # 3d. Update Checker (fetch-only, NO auto-install) + Manual/Prompt Update Installer
  cat << 'EOF' > "${stage_dir}/netdash-check-update"
#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="/opt/netdash"
cd "${INSTALL_DIR}"
[[ -d .git ]] || exit 0
git fetch origin main --quiet || exit 0
LOCAL_SHA="$(git rev-parse --short=7 HEAD)"
REMOTE_SHA="$(git rev-parse --short=7 origin/main)"
BEHIND="$(git rev-list HEAD..origin/main --count 2>/dev/null || echo 0)"
if [[ "${BEHIND}" -gt 0 ]]; then
  logger -t netdash-check "Update available on origin/main (${LOCAL_SHA} -> ${REMOTE_SHA}, ${BEHIND} commits behind)"
fi
EOF

  cat << 'EOF' > "${stage_dir}/netdash-update"
#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="/opt/netdash"
cd "${INSTALL_DIR}"
rm -f "${INSTALL_DIR}/.trigger-update"
[[ -d .git ]] || exit 0
echo "[NetDash Updater] Fetching latest commits from origin/main..."
git fetch origin main --quiet
LOCAL_SHA="$(git rev-parse --short=7 HEAD)"
REMOTE_SHA="$(git rev-parse --short=7 origin/main)"
logger -t netdash-updater "Installing NetDash update (${LOCAL_SHA} -> ${REMOTE_SHA})..."
echo "[NetDash Updater] Updating working tree from ${LOCAL_SHA} to ${REMOTE_SHA}..."
git reset --hard origin/main
if [[ -f backend/Dockerfile && -f frontend/Dockerfile ]]; then
  docker compose --env-file "${INSTALL_DIR}/.env" up -d --build --remove-orphans
elif [[ -f docker-compose.yml ]]; then
  docker compose --env-file "${INSTALL_DIR}/.env" up -d timescaledb redis vector
fi
docker image prune -f >/dev/null 2>&1 || true
rm -f "${INSTALL_DIR}/.update-status.json"
echo "[NetDash Updater] Update to ${REMOTE_SHA} complete!"
EOF

  cat << 'EOF' > "${stage_dir}/netdash-update-prompt.sh"
#!/usr/bin/env bash
# Interactive login prompt when new commits exist on origin/main (never auto-installs)
if [[ $- == *i* ]] && [[ -d /opt/netdash/.git ]]; then
  LOCAL_SHA="$(git -C /opt/netdash rev-parse --short=7 HEAD 2>/dev/null || true)"
  REMOTE_SHA="$(git -C /opt/netdash rev-parse --short=7 origin/main 2>/dev/null || true)"
  BEHIND="$(git -C /opt/netdash rev-list HEAD..origin/main --count 2>/dev/null || echo 0)"
  if [[ -n "${BEHIND}" && "${BEHIND}" -gt 0 ]]; then
    echo ""
    echo -e "\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
    echo -e " \033[1;32m[NetDash] UPDATE AVAILABLE:\033[0m ${BEHIND} new commit(s) on GitHub (${LOCAL_SHA} -> ${REMOTE_SHA})"
    git -C /opt/netdash log HEAD..origin/main -n 5 --pretty=format:"   • %h — %s (%ar)" 2>/dev/null || true
    echo ""
    echo -e "\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
    read -r -p " Download and install this update now? [y/N]: " ans
    if [[ "${ans}" =~ ^[Yy]$ ]]; then
      sudo /usr/local/bin/netdash-update
    else
      echo " Skipped update. Run 'sudo netdash-update' or install from the Web UI anytime."
    fi
    echo ""
  fi
fi
EOF

  cat << 'EOF' > "${stage_dir}/netdash-check.service"
[Unit]
Description=NetDash GitHub Update Check (Fetch Only — No Auto-Install)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/netdash-check-update
WorkingDirectory=/opt/netdash
EOF

  cat << 'EOF' > "${stage_dir}/netdash-check.timer"
[Unit]
Description=Check GitHub every 5 minutes for available NetDash updates (Fetch Only)

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

  cat << 'EOF' > "${stage_dir}/netdash-updater.service"
[Unit]
Description=NetDash Manual / Web-Triggered Update Installer
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/netdash-update
WorkingDirectory=/opt/netdash
EOF

  cat << 'EOF' > "${stage_dir}/netdash-updater.path"
[Unit]
Description=Watch for Web UI Update Install Trigger (/opt/netdash/.trigger-update)

[Path]
PathExists=/opt/netdash/.trigger-update
Unit=netdash-updater.service

[Install]
WantedBy=multi-user.target
EOF

  # 3e. First-boot provisioning script & systemd unit (non-blocking background service)
  cat << 'EOF' > "${stage_dir}/netdash-firstboot.sh"
#!/usr/bin/env bash
set -euo pipefail
BOOTSTRAP_LOG="/var/log/netdash-bootstrap.log"
INSTALL_DIR="/opt/netdash"
ENV_FILE="${INSTALL_DIR}/.env"

exec > >(tee -a "${BOOTSTRAP_LOG}") 2>&1
echo "[NetDash Bootstrap] Starting first-boot provisioning at $(date -u)"

# Expand root partition & filesystem to full disk size (e.g., 120G)
growpart /dev/sda 1 >/dev/null 2>&1 || growpart /dev/vda 1 >/dev/null 2>&1 || true
resize2fs /dev/sda1 >/dev/null 2>&1 || resize2fs /dev/vda1 >/dev/null 2>&1 || true

# Ensure networking, DNS, and SSH host keys are active
systemd-machine-id-setup >/dev/null 2>&1 || true
rm -f /etc/resolv.conf
cat << 'ERES' > /etc/resolv.conf
nameserver 1.1.1.1
nameserver 8.8.8.8
ERES
systemctl enable --now systemd-networkd >/dev/null 2>&1 || true
systemctl restart systemd-networkd >/dev/null 2>&1 || true
ssh-keygen -A >/dev/null 2>&1 || true
systemctl restart ssh || systemctl restart sshd || true
sysctl --system >/dev/null 2>&1 || true

# Wait up to 45s for network reachability
for i in {1..15}; do
  if ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1 || ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
    break
  fi
  echo "[NetDash Bootstrap] Waiting for network connectivity ($i/15)..."
  sleep 3
done

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq qemu-guest-agent net-tools ca-certificates curl gnupg git ufw openssl lsb-release
systemctl enable --now qemu-guest-agent || true

if ! command -v docker >/dev/null 2>&1; then
  echo "[NetDash Bootstrap] Installing Docker CE & Docker Compose plugin..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  DEBIAN_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${DEBIAN_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

source /etc/netdash-seed.env
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  git config --global credential.helper store
  echo "https://x-access-token:${GITHUB_TOKEN}@github.com" > /root/.git-credentials
  chmod 600 /root/.git-credentials
fi

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  git -C "${INSTALL_DIR}" fetch origin main
  git -C "${INSTALL_DIR}" reset --hard origin/main
else
  rm -rf "${INSTALL_DIR}"
  git clone --branch main "${REPO_URL}" "${INSTALL_DIR}"
fi

grep -vE '^(REPO_URL|GITHUB_TOKEN)=' /etc/netdash-seed.env > "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

ufw allow OpenSSH || true
ufw allow "${WEB_PORT}/tcp" || true
ufw allow "${SYSLOG_PORT}/udp" || true
ufw allow "${SYSLOG_PORT}/tcp" || true
ufw --force enable || true

systemctl daemon-reload
systemctl enable --now netdash-check.timer netdash-updater.path

if [[ -f "${INSTALL_DIR}/docker-compose.yml" ]]; then
  cd "${INSTALL_DIR}"
  if [[ -f backend/Dockerfile && -f frontend/Dockerfile ]]; then
    echo "[NetDash Bootstrap] Building and starting full NetDash stack..."
    docker compose --env-file "${ENV_FILE}" up -d --build --remove-orphans
  else
    echo "[NetDash Bootstrap] Starting Phase 1 core services (TimescaleDB, Redis, Vector)..."
    docker compose --env-file "${ENV_FILE}" up -d timescaledb redis vector
  fi
fi

systemctl disable netdash-firstboot.service || true
echo "[NetDash Bootstrap] COMPLETE"
EOF

  cat << 'EOF' > "${stage_dir}/netdash-firstboot.service"
[Unit]
Description=NetDash First-Boot Docker & Repository Provisioner
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/netdash-firstboot.sh
RemainAfterExit=yes
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF

  msg_info "Customizing Debian disk image (baking in root/${CI_USER} credentials, network & services)"
  export LIBGUESTFS_BACKEND=direct
  virt-customize -q -a "${work_img}" \
    --hostname "${HN}" \
    --root-password "password:${CI_PASSWORD}" \
    --run-command "id -u ${CI_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo ${CI_USER}" \
    --password "${CI_USER}:password:${CI_PASSWORD}" \
    --run-command "echo '${CI_USER} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/${CI_USER} && chmod 0440 /etc/sudoers.d/${CI_USER}" \
    --run-command "mkdir -p /etc/ssh/sshd_config.d && printf 'PermitRootLogin yes\nPasswordAuthentication yes\n' > /etc/ssh/sshd_config.d/99-netdash-auth.conf" \
    --run-command "rm -f /etc/systemd/network/* /etc/netplan/* 2>/dev/null || true" \
    --upload "${stage_dir}/10-netdash.network:/etc/systemd/network/10-netdash.network" \
    --run-command "chmod 0644 /etc/systemd/network/10-netdash.network && systemctl enable systemd-networkd" \
    --run-command "rm -f /etc/resolv.conf && printf 'nameserver ${DNS_SERVER:-1.1.1.1}\nnameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf" \
    --run-command "systemctl mask systemd-networkd-wait-online.service cloud-init-local.service cloud-init.service cloud-config.service cloud-final.service >/dev/null 2>&1 || true" \
    --upload "${stage_dir}/99-netdash.conf:/etc/sysctl.d/99-netdash.conf" \
    --upload "${stage_dir}/netdash-seed.env:/etc/netdash-seed.env" \
    --run-command "chmod 0600 /etc/netdash-seed.env" \
    --upload "${stage_dir}/netdash-check-update:/usr/local/bin/netdash-check-update" \
    --run-command "chmod 0755 /usr/local/bin/netdash-check-update" \
    --upload "${stage_dir}/netdash-update:/usr/local/bin/netdash-update" \
    --run-command "chmod 0755 /usr/local/bin/netdash-update" \
    --upload "${stage_dir}/netdash-update-prompt.sh:/etc/profile.d/netdash-update-prompt.sh" \
    --run-command "chmod 0755 /etc/profile.d/netdash-update-prompt.sh" \
    --upload "${stage_dir}/netdash-check.service:/etc/systemd/system/netdash-check.service" \
    --upload "${stage_dir}/netdash-check.timer:/etc/systemd/system/netdash-check.timer" \
    --upload "${stage_dir}/netdash-updater.service:/etc/systemd/system/netdash-updater.service" \
    --upload "${stage_dir}/netdash-updater.path:/etc/systemd/system/netdash-updater.path" \
    --upload "${stage_dir}/netdash-firstboot.sh:/usr/local/bin/netdash-firstboot.sh" \
    --run-command "chmod 0755 /usr/local/bin/netdash-firstboot.sh" \
    --upload "${stage_dir}/netdash-firstboot.service:/etc/systemd/system/netdash-firstboot.service" \
    --run-command "systemctl enable netdash-firstboot.service && systemd-machine-id-setup"

  if [[ -n "${SSH_KEY}" ]]; then
    virt-customize -q -a "${work_img}" \
      --ssh-inject "root:string:${SSH_KEY}" \
      --ssh-inject "${CI_USER}:string:${SSH_KEY}"
  fi
  msg_ok "Customized disk image (local console & SSH login ready immediately on boot)"

  # 4. Create and Configure the Proxmox KVM Virtual Machine (standard i440fx + VirtIO SCSI)
  msg_info "Creating Proxmox VM ${VMID} (${HN})"
  local net_arg="virtio,bridge=${BRG}"
  [[ -n "${VLAN}" ]] && net_arg="${net_arg},tag=${VLAN}"
  [[ -n "${MAC}" ]] && net_arg="virtio=${MAC},bridge=${BRG}${VLAN:+,tag=${VLAN}}"

  qm create "${VMID}" \
    --name "${HN}" \
    --ostype l26 \
    --scsihw virtio-scsi-single \
    --agent "enabled=1,fstrim_cloned_disks=1" \
    --cpu "${CPU_TYPE}" \
    --sockets 1 \
    --cores "${CORE_COUNT}" \
    --memory "${RAM_SIZE}" \
    --balloon 0 \
    --net0 "${net_arg}" \
    --onboot 1 \
    --serial0 socket \
    --tags "netdash;observability;docker" >/dev/null
  msg_ok "Created VM ${VMID} shell"

  # 5. Import Customized Disk & Resize
  msg_info "Importing disk into storage pool '${STORAGE_POOL}' and resizing to ${DISK_SIZE}G"
  qm importdisk "${VMID}" "${work_img}" "${STORAGE_POOL}" >/dev/null 2>&1
  rm -rf "${work_img}" "${stage_dir}"

  local disk_ref
  disk_ref="$(qm config "${VMID}" | awk -F': ' '/^unused0:/ {print $2}')"
  if [[ -z "${disk_ref}" ]]; then
    die "Failed to locate imported disk (unused0) on VM ${VMID}."
  fi

  qm set "${VMID}" \
    --scsi0 "${disk_ref},discard=on,iothread=1,ssd=1" \
    --boot "order=scsi0" >/dev/null

  qm resize "${VMID}" scsi0 "${DISK_SIZE}G" >/dev/null
  msg_ok "Configured ${DISK_SIZE}G VirtIO SCSI SSD disk (discard=on, iothread=1, ssd=1)"

  # Resize root partition inside the image on boot via cloud-guest-utils (already in Debian cloud image)
  local vm_notes
  vm_notes="<div align='center'>
  <h2>NetDash — Network Observability Dashboard</h2>
  <p><b>GitHub Auto-Update:</b> Polls <code>origin/main</code> every 5m via <code>netdash-updater.timer</code></p>
  <p><b>Web UI Port:</b> <code>${WEB_PORT}/tcp</code> &nbsp;|&nbsp; <b>Syslog Port:</b> <code>${SYSLOG_PORT}/udp+tcp</code></p>
</div>"
  qm set "${VMID}" --description "${vm_notes}" >/dev/null

  # 6. Start VM and Monitor Guest Bootstrap via QEMU Guest Agent
  msg_info "Starting VM ${VMID} (${HN})"
  qm start "${VMID}"
  msg_ok "Started VM ${VMID}"

  msg_info "Waiting for VM ${VMID} network & QEMU Guest Agent (~45-90s while packages install)"
  local vm_ip="" attempts=0
  while [[ "${attempts}" -lt 60 ]]; do
    sleep 3
    attempts=$((attempts + 1))
    if qm guest cmd "${VMID}" ping >/dev/null 2>&1; then
      vm_ip="$(qm guest cmd "${VMID}" network-get-interfaces 2>/dev/null \
        | grep -oE '"ip-address"[[:space:]]*:[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+"' \
        | grep -v '"127\.' \
        | head -n1 \
        | sed -E 's/.*"([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)"/\1/' || true)"
      if [[ -n "${vm_ip}" && "${vm_ip}" != "172."* ]]; then
        break
      fi
    fi
  done

  if [[ -n "${vm_ip}" ]]; then
    msg_ok "VM ${VMID} is online at IPv4: ${BGN}${vm_ip}${CL}"
  else
    vm_ip="${STATIC_IP%%/*}"
    [[ -z "${vm_ip}" ]] && vm_ip="<VM-IP>"
    msg_warn "QEMU Guest Agent still initializing in background; check Proxmox Console for live progress."
  fi

  if qm guest cmd "${VMID}" ping >/dev/null 2>&1; then
    msg_info "Waiting for Docker CE & NetDash container stack inside VM ${VMID}"
    local boot_tries=0
    while [[ "${boot_tries}" -lt 90 ]]; do
      sleep 4
      boot_tries=$((boot_tries + 1))
      if qm guest exec "${VMID}" -- grep -q "COMPLETE" "${BOOTSTRAP_LOG}" >/dev/null 2>&1; then
        msg_ok "Guest bootstrap & Docker Compose stack completed!"
        break
      fi
    done
  fi

  echo ""
  echo -e "${BL}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${CL}"
  msg_ok "${BOLD}${APP} Proxmox VM (${VMID}) has been provisioned!${CL}"
  echo -e "   ${INFO} ${BOLD}Console/SSH Login:${CL} ${BL}root${CL} or ${BL}${CI_USER}${CL} (password: ${BL}${CI_PASSWORD}${CL})"
  echo -e "   ${INFO} ${BOLD}Dashboard URL:${CL}     ${BGN}http://${vm_ip}:${WEB_PORT}${CL}"
  echo -e "   ${INFO} ${BOLD}Syslog Receiver:${CL}   ${GN}${vm_ip}:${SYSLOG_PORT} (UDP/TCP)${CL}"
  echo -e "   ${INFO} ${BOLD}SSH Command:${CL}       ${BL}ssh root@${vm_ip}${CL}"
  echo -e "   ${INFO} ${BOLD}Bootstrap Log:${CL}     ${BL}tail -f ${BOOTSTRAP_LOG}${CL} (inside VM)"
  echo -e "${BL}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${CL}"
}

# ==============================================================================
# MODE 2: DEBIAN 12/13 GUEST OS DIRECT INSTALLER (When run inside a VM/Server)
# ==============================================================================

prompt_guest_input() {
  local prompt_text="$1"
  local default_val="$2"
  local var_name="$3"
  local is_secret="${4:-false}"
  local input_val=""

  if [[ -e /dev/tty ]]; then
    if [[ "${is_secret}" == "true" ]]; then
      read -r -s -p "$(echo -e " ${INFO} ${prompt_text} [hidden]: ")" input_val < /dev/tty
      echo "" > /dev/tty
    else
      read -r -p "$(echo -e " ${INFO} ${prompt_text} [${default_val}]: ")" input_val < /dev/tty
    fi
  fi

  [[ -z "${input_val}" ]] && input_val="${default_val}"
  printf -v "${var_name}" '%s' "${input_val}"
}

install_debian_guest() {
  header_info
  msg_info "Detected Debian Guest OS ($(hostname)) — Running direct NetDash installer"

  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg git ufw openssl lsb-release
  msg_ok "Installed base system packages"

  if ! command -v docker >/dev/null 2>&1; then
    msg_info "Installing Docker CE & Docker Compose plugin"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    local codename
    codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${codename} stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    msg_ok "Installed Docker Engine & Compose plugin"
  else
    msg_ok "Docker Engine already installed ($(docker --version))"
  fi

  msg_info "Applying kernel sysctl network & memory tuning"
  cat << 'EOF' > /etc/sysctl.d/99-netdash.conf
net.core.rmem_max = 26214400
net.core.rmem_default = 4194304
net.core.wmem_max = 26214400
net.core.netdev_max_backlog = 10000
net.core.somaxconn = 4096
vm.overcommit_memory = 1
EOF
  sysctl --system >/dev/null 2>&1 || true
  msg_ok "Applied sysctl parameters"

  echo ""
  prompt_guest_input "GitHub Repository HTTPS URL" "${DEFAULT_REPO_URL}" REPO_URL
  if [[ -n "${GITHUB_TOKEN}" ]]; then
    git config --global credential.helper store
    echo "https://x-access-token:${GITHUB_TOKEN}@github.com" > /root/.git-credentials
    chmod 600 /root/.git-credentials
  fi
  prompt_guest_input "UDM Pro Gateway IP / Host" "192.168.1.1" UNIFI_HOST
  prompt_guest_input "UDM Pro Integration API Key (X-API-KEY)" "" UNIFI_API_KEY "true"
  prompt_guest_input "UniFi Site Name" "default" UNIFI_SITE
  prompt_guest_input "SNMP Version (v2c or v3)" "v2c" SNMP_VERSION
  prompt_guest_input "SNMP v2c Community String" "public" SNMP_COMMUNITY
  prompt_guest_input "SNMP Poll Targets (comma-separated IPs)" "${UNIFI_HOST}" SNMP_TARGETS
  prompt_guest_input "Allowed Subnets for Nmap Scanner (comma-separated CIDRs)" "192.168.1.0/24" ALLOWED_SUBNETS
  prompt_guest_input "Raw SNMP Retention Days" "180" SNMP_RAW_RETENTION_DAYS
  prompt_guest_input "Syslog Retention Days" "30" LOG_RETENTION_DAYS
  prompt_guest_input "Syslog Listen Port (UDP/TCP)" "514" SYSLOG_PORT
  prompt_guest_input "Dashboard Web HTTP Port" "8080" WEB_PORT

  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    msg_info "Updating existing repository in ${INSTALL_DIR}"
    git -C "${INSTALL_DIR}" fetch origin main
    git -C "${INSTALL_DIR}" reset --hard origin/main
  else
    msg_info "Cloning ${REPO_URL} into ${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}"
    git clone --branch main "${REPO_URL}" "${INSTALL_DIR}"
  fi
  msg_ok "Repository synchronized at ${INSTALL_DIR}"

  local db_pass
  db_pass="$(openssl rand -hex 20)"
  if [[ -f "${ENV_FILE}" ]]; then
    local existing_pass
    existing_pass="$(grep -E '^POSTGRES_PASSWORD=' "${ENV_FILE}" | cut -d'=' -f2- || true)"
    [[ -n "${existing_pass}" ]] && db_pass="${existing_pass}"
  fi

  cat << EOF > "${ENV_FILE}"
POSTGRES_USER=netdash
POSTGRES_PASSWORD=${db_pass}
POSTGRES_DB=netdash
DATABASE_URL=postgresql://netdash:${db_pass}@timescaledb:5432/netdash
SYNC_DATABASE_URL=postgresql://netdash:${db_pass}@timescaledb:5432/netdash
REDIS_URL=redis://redis:6379/0
UNIFI_HOST=${UNIFI_HOST}
UNIFI_API_KEY=${UNIFI_API_KEY}
UNIFI_SITE=${UNIFI_SITE}
UNIFI_VERIFY_SSL=false
UNIFI_POLL_INTERVAL=30
SNMP_VERSION=${SNMP_VERSION}
SNMP_COMMUNITY=${SNMP_COMMUNITY}
SNMP_TARGETS=${SNMP_TARGETS}
SNMP_PORT=161
SNMP_POLL_INTERVAL=60
SNMP_RAW_RETENTION_DAYS=${SNMP_RAW_RETENTION_DAYS}
ALLOWED_SUBNETS=${ALLOWED_SUBNETS}
SUBNET_SCAN_MINUTES=15
SYSLOG_PORT=${SYSLOG_PORT}
LOG_RETENTION_DAYS=${LOG_RETENTION_DAYS}
WEB_PORT=${WEB_PORT}
EOF
  chmod 600 "${ENV_FILE}"
  msg_ok "Written ${ENV_FILE} (mode 0600)"

  ufw allow OpenSSH >/dev/null 2>&1 || true
  ufw allow "${WEB_PORT}/tcp" >/dev/null 2>&1 || true
  ufw allow "${SYSLOG_PORT}/udp" >/dev/null 2>&1 || true
  ufw allow "${SYSLOG_PORT}/tcp" >/dev/null 2>&1 || true
  ufw --force enable >/dev/null 2>&1 || true
  msg_ok "Configured UFW firewall"

  cat << 'EOF' > /usr/local/bin/netdash-check-update
#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="/opt/netdash"
cd "${INSTALL_DIR}"
[[ -d .git ]] || exit 0
git fetch origin main --quiet || exit 0
EOF
  chmod +x /usr/local/bin/netdash-check-update

  cat << 'EOF' > "${UPDATER_BIN}"
#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="/opt/netdash"
cd "${INSTALL_DIR}"
rm -f "${INSTALL_DIR}/.trigger-update"
[[ -d .git ]] || exit 0
git fetch origin main --quiet
LOCAL_SHA="$(git rev-parse --short=7 HEAD)"
REMOTE_SHA="$(git rev-parse --short=7 origin/main)"
logger -t netdash-updater "Installing NetDash update (${LOCAL_SHA} -> ${REMOTE_SHA})..."
git reset --hard origin/main
if [[ -f backend/Dockerfile && -f frontend/Dockerfile ]]; then
  docker compose --env-file "${INSTALL_DIR}/.env" up -d --build --remove-orphans
else
  docker compose --env-file "${INSTALL_DIR}/.env" up -d timescaledb redis vector
fi
docker image prune -f >/dev/null 2>&1 || true
rm -f "${INSTALL_DIR}/.update-status.json"
EOF
  chmod +x "${UPDATER_BIN}"

  cat << 'EOF' > /etc/profile.d/netdash-update-prompt.sh
#!/usr/bin/env bash
if [[ $- == *i* ]] && [[ -d /opt/netdash/.git ]]; then
  LOCAL_SHA="$(git -C /opt/netdash rev-parse --short=7 HEAD 2>/dev/null || true)"
  REMOTE_SHA="$(git -C /opt/netdash rev-parse --short=7 origin/main 2>/dev/null || true)"
  BEHIND="$(git -C /opt/netdash rev-list HEAD..origin/main --count 2>/dev/null || echo 0)"
  if [[ -n "${BEHIND}" && "${BEHIND}" -gt 0 ]]; then
    echo ""
    echo -e "\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
    echo -e " \033[1;32m[NetDash] UPDATE AVAILABLE:\033[0m ${BEHIND} new commit(s) on GitHub (${LOCAL_SHA} -> ${REMOTE_SHA})"
    git -C /opt/netdash log HEAD..origin/main -n 5 --pretty=format:"   • %h — %s (%ar)" 2>/dev/null || true
    echo ""
    echo -e "\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
    read -r -p " Download and install this update now? [y/N]: " ans
    if [[ "${ans}" =~ ^[Yy]$ ]]; then
      sudo /usr/local/bin/netdash-update
    else
      echo " Skipped update. Run 'sudo netdash-update' or install from the Web UI anytime."
    fi
    echo ""
  fi
fi
EOF
  chmod +x /etc/profile.d/netdash-update-prompt.sh

  cat << 'EOF' > /etc/systemd/system/netdash-check.service
[Unit]
Description=NetDash GitHub Update Check (Fetch Only — No Auto-Install)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/netdash-check-update
WorkingDirectory=/opt/netdash
EOF

  cat << 'EOF' > /etc/systemd/system/netdash-check.timer
[Unit]
Description=Check GitHub every 5 minutes for available NetDash updates (Fetch Only)

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

  cat << 'EOF' > /etc/systemd/system/netdash-updater.service
[Unit]
Description=NetDash Manual / Web-Triggered Update Installer
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/netdash-update
WorkingDirectory=/opt/netdash
EOF

  cat << 'EOF' > /etc/systemd/system/netdash-updater.path
[Unit]
Description=Watch for Web UI Update Install Trigger (/opt/netdash/.trigger-update)

[Path]
PathExists=/opt/netdash/.trigger-update
Unit=netdash-updater.service

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now netdash-check.timer netdash-updater.path
  msg_ok "Enabled netdash-check.timer (fetch-only) & manual update prompt"

  cd "${INSTALL_DIR}"
  if [[ -f backend/Dockerfile && -f frontend/Dockerfile ]]; then
    msg_info "Building and starting full NetDash Docker Compose stack"
    docker compose --env-file "${ENV_FILE}" up -d --build --remove-orphans
  else
    msg_info "Starting Phase 1 core services (TimescaleDB, Redis, Vector)"
    docker compose --env-file "${ENV_FILE}" up -d timescaledb redis vector
  fi
  msg_ok "NetDash stack is running!"

  local lan_ip
  lan_ip="$(hostname -I | awk '{print $1}')"
  echo ""
  echo -e " ${CM} ${BOLD}Dashboard URL:${CL}   ${BGN}http://${lan_ip}:${WEB_PORT}${CL}"
  echo -e " ${CM} ${BOLD}Syslog Receiver:${CL} ${GN}${lan_ip}:${SYSLOG_PORT} (UDP/TCP)${CL}"
}

# ==============================================================================
# ENTRYPOINT: Auto-detect Proxmox VE Host vs Debian Guest VM
# ==============================================================================

check_root
if check_proxmox_env; then
  provision_proxmox_vm
else
  install_debian_guest
fi
