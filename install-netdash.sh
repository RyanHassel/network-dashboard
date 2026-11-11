#!/usr/bin/env bash
# ==============================================================================
# NetDash — Proxmox VE Automated VM Creator & Guest Bootstrap Script
# Style: Proxmox VE Helper-Scripts (community-scripts.org)
#
# Run directly in the Proxmox VE Host Shell (CLI):
#    Public repo:
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyanHassel/network-dashboard/main/install-netdash.sh)"
#    Private repo (with GitHub Personal Access Token):
#     export GITHUB_TOKEN="ghp_xxx" && bash -c "$(curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" https://raw.githubusercontent.com/RyanHassel/network-dashboard/main/install-netdash.sh)"
#
# Dual-Mode Support:
#   1. Proxmox VE Host CLI -> Launches interactive whiptail wizard, downloads
#      Debian 12/13 GenericCloud image, creates & tunes KVM VM, injects
#      Cloud-Init config, and bootstraps the entire NetDash Docker stack.
#   2. Debian 12/13 Guest VM -> Installs Docker CE, sysctl tuning, UFW, clones
#      the GitHub repo into /opt/netdash, enables 5m GitOps auto-updater, and
#      starts the NetDash containers.
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
    _   __     __  ____ash
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
# MODE 1: PROXMOX VE HOST CLI — VM PROVISIONER (whiptail TUI + qm + Cloud-Init)
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

  # Default first pool to ON
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

ensure_snippets_storage() {
  # Ensure 'local' storage supports 'snippets' so we can attach Cloud-Init vendor-data cleanly
  local current_content
  current_content="$(pvesm status | awk '$1=="local" {print $1}')"
  if [[ -z "${current_content}" ]]; then
    die "Proxmox 'local' storage pool not found."
  fi
  mkdir -p /var/lib/vz/snippets
  local cfg_content
  cfg_content="$(awk '/^dir: local$/,/^$/' /etc/pve/storage.cfg | awk '/content/ {print $2}')"
  if [[ "${cfg_content}" != *"snippets"* ]]; then
    msg_info "Enabling 'snippets' content type on Proxmox 'local' storage"
    if [[ -n "${cfg_content}" ]]; then
      pvesm set local --content "${cfg_content},snippets"
    else
      pvesm set local --content "iso,vztmpl,backup,snippets"
    fi
    msg_ok "Enabled 'snippets' on 'local' storage"
  fi
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
  STATIC_IP=""
  GATEWAY_IP=""
  DNS_SERVER=""
  CI_USER="netdash"
  CI_PASSWORD="netdash"
  SSH_KEY=""
  START_VM="yes"
}

proxmox_wizard() {
  proxmox_default_settings

  if ! whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --title "${APP} KVM VM Installer" \
    --yesno "This will create a new ${APP} Virtual Machine on Proxmox VE and configure the entire observability stack automatically.\n\nHost specs recommended: 4 vCPUs, 8 GB RAM, 120 GB SSD.\n\nProceed?" 14 68; then
    die "Exited installer."
  fi

  local install_mode
  install_mode=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --title "Settings Mode" \
    --radiolist "\nChoose VM provisioning settings mode:\n" 12 68 2 \
    "default" "Default Settings (4 vCPU, 8GB RAM, 120GB SSD, DHCP)" ON \
    "advanced" "Advanced Settings (Custom ID, VLAN, Static IP, SSH)" OFF \
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

    BRG=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nSet Proxmox Network Bridge:" 10 58 "${BRG}" \
      --title "NETWORK BRIDGE" 3>&1 1>&2 2>&3) || die "Cancelled."

    VLAN=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nSet Management VLAN Tag (leave blank for untagged):" 10 58 "" \
      --title "VLAN TAG" 3>&1 1>&2 2>&3) || die "Cancelled."

    NET_MODE=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --title "IPv4 ADDRESS CONFIGURATION" \
      --radiolist "\nSelect IPv4 configuration for the VM:" 12 64 2 \
      "dhcp" "Use DHCP" ON \
      "static" "Set Static IPv4 Address" OFF \
      3>&1 1>&2 2>&3) || die "Cancelled."

    if [[ "${NET_MODE}" == "static" ]]; then
      STATIC_IP=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
        --inputbox "\nEnter Static IPv4 & CIDR (e.g. 192.168.1.50/24):" 10 58 "192.168.1.50/24" \
        --title "STATIC IPv4" 3>&1 1>&2 2>&3) || die "Cancelled."

      GATEWAY_IP=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
        --inputbox "\nEnter Default Gateway IPv4 (e.g. 192.168.1.1):" 10 58 "192.168.1.1" \
        --title "GATEWAY IPv4" 3>&1 1>&2 2>&3) || die "Cancelled."

      DNS_SERVER=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
        --inputbox "\nEnter DNS Server IPv4 (e.g. 192.168.1.1):" 10 58 "${GATEWAY_IP}" \
        --title "DNS SERVER" 3>&1 1>&2 2>&3) || die "Cancelled."
    fi

    CI_PASSWORD=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --passwordbox "\nSet SSH Password for user 'netdash' (leave default 'netdash' if blank):" 10 64 "${CI_PASSWORD}" \
      --title "SSH PASSWORD" 3>&1 1>&2 2>&3) || die "Cancelled."
    [[ -z "${CI_PASSWORD}" ]] && CI_PASSWORD="netdash"

    SSH_KEY=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nOptional SSH Public Key (ssh-ed25519 / ssh-rsa, or leave blank):" 10 68 "" \
      --title "SSH AUTHORIZED KEY" 3>&1 1>&2 2>&3) || die "Cancelled."

    REPO_URL=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --inputbox "\nGitHub Repository HTTPS URL to clone into /opt/netdash:" 10 68 "${DEFAULT_REPO_URL}" \
      --title "GITHUB REPOSITORY" 3>&1 1>&2 2>&3) || die "Cancelled."
  else
    REPO_URL="${DEFAULT_REPO_URL}"
  fi

  # --- NetDash Application Telemetry Settings (Prompted in Both Default & Advanced) ---
  if [[ -z "${GITHUB_TOKEN}" ]]; then
    GITHUB_TOKEN=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
      --passwordbox "\nGitHub Personal Access Token (required for Private Repo; leave blank if Public):" 10 72 "" \
      --title "GITHUB ACCESS TOKEN (PRIVATE REPO)" 3>&1 1>&2 2>&3) || die "Cancelled."
  fi

  UNIFI_HOST=$(whiptail --backtitle "Proxmox VE Helper-Scripts — ${APP}" \
    --inputbox "\nUDM Pro Gateway IP / Hostname:" 10 68 "${GATEWAY_IP:-192.168.1.1}" \
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
    --inputbox "\nAllowed Subnets for Nmap + ARP Scanner (comma-separated CIDRs):" 10 68 "192.168.1.0/24" \
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
  msg_ok "GitHub Repo:    ${BL}${REPO_URL}${CL}"
  echo ""

  ensure_snippets_storage

  # 1. Download Debian GenericCloud Image
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

  # 2. Generate Cloud-Init Vendor-Data Snippet (Self-Contained Guest Bootstrap)
  local db_pass snippet_file="/var/lib/vz/snippets/netdash-${VMID}-vendor.yaml"
  db_pass="$(openssl rand -hex 20)"

  msg_info "Generating Cloud-Init vendor bootstrap config (${snippet_file})"
  cat << EOF > "${snippet_file}"
#cloud-config
package_update: true
package_upgrade: true
packages:
  - qemu-guest-agent
  - ca-certificates
  - curl
  - gnupg
  - git
  - ufw
  - openssl
  - lsb-release

write_files:
  - path: /etc/sysctl.d/99-netdash.conf
    permissions: '0644'
    content: |
      net.core.rmem_max = 26214400
      net.core.rmem_default = 4194304
      net.core.wmem_max = 26214400
      net.core.netdev_max_backlog = 10000
      net.core.somaxconn = 4096
      vm.overcommit_memory = 1

  - path: /etc/netdash-seed.env
    permissions: '0600'
    content: |
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

  - path: /usr/local/bin/netdash-update
    permissions: '0755'
    content: |
      #!/usr/bin/env bash
      set -euo pipefail
      INSTALL_DIR="/opt/netdash"
      FORCE_BUILD="\${1:-}"
      cd "\${INSTALL_DIR}"
      [[ -d .git ]] || exit 0
      git fetch origin main --quiet
      LOCAL_SHA="\$(git rev-parse HEAD)"
      REMOTE_SHA="\$(git rev-parse origin/main)"
      if [[ "\${LOCAL_SHA}" != "\${REMOTE_SHA}" ]] || [[ "\${FORCE_BUILD}" == "--force" ]]; then
        logger -t netdash-updater "Updating NetDash from \${LOCAL_SHA:0:7} to \${REMOTE_SHA:0:7}..."
        git reset --hard origin/main
        if [[ -f backend/Dockerfile && -f frontend/Dockerfile ]]; then
          docker compose --env-file "\${INSTALL_DIR}/.env" up -d --build --remove-orphans
        elif [[ -f docker-compose.yml ]]; then
          docker compose --env-file "\${INSTALL_DIR}/.env" up -d timescaledb redis vector
        fi
        docker image prune -f >/dev/null 2>&1 || true
      fi

  - path: /etc/systemd/system/netdash-updater.service
    permissions: '0644'
    content: |
      [Unit]
      Description=NetDash GitHub Auto-Updater Service
      After=network-online.target docker.service
      Wants=network-online.target

      [Service]
      Type=oneshot
      ExecStart=/usr/local/bin/netdash-update
      WorkingDirectory=/opt/netdash

  - path: /etc/systemd/system/netdash-updater.timer
    permissions: '0644'
    content: |
      [Unit]
      Description=Poll GitHub every 5 minutes for NetDash updates

      [Timer]
      OnBootSec=2min
      OnUnitActiveSec=5min
      Persistent=true

      [Install]
      WantedBy=timers.target

  - path: /usr/local/bin/netdash-firstboot.sh
    permissions: '0755'
    content: |
      #!/usr/bin/env bash
      set -euo pipefail
      exec > >(tee -a ${BOOTSTRAP_LOG}) 2>&1
      echo "[NetDash Bootstrap] Starting automated VM provisioning at \$(date -u)"
      systemctl enable --now qemu-guest-agent || true
      sysctl --system || true

      # Install Docker CE
      if ! command -v docker >/dev/null 2>&1; then
        echo "[NetDash Bootstrap] Installing Docker CE & Compose plugin..."
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        DEBIAN_CODENAME="\$(. /etc/os-release && echo "\${VERSION_CODENAME:-bookworm}")"
        echo "deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \${DEBIAN_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        systemctl enable --now docker
      fi

      source /etc/netdash-seed.env
      if [[ -n "\${GITHUB_TOKEN:-}" ]]; then
        git config --global credential.helper store
        echo "https://x-access-token:\${GITHUB_TOKEN}@github.com" > /root/.git-credentials
        chmod 600 /root/.git-credentials
      fi

      if [[ -d "${INSTALL_DIR}/.git" ]]; then
        git -C "${INSTALL_DIR}" fetch origin main
        git -C "${INSTALL_DIR}" reset --hard origin/main
      else
        mkdir -p "${INSTALL_DIR}"
        git clone --branch main "\${REPO_URL}" "${INSTALL_DIR}" || {
          echo "[NetDash Bootstrap] WARN: Could not clone \${REPO_URL} yet; initializing directory."
        }
      fi

      mkdir -p "${INSTALL_DIR}"
      grep -vE '^(REPO_URL|GITHUB_TOKEN)=' /etc/netdash-seed.env > "${ENV_FILE}"
      chmod 600 "${ENV_FILE}"

      # Configure UFW
      ufw allow OpenSSH || true
      ufw allow "\${WEB_PORT}/tcp" || true
      ufw allow "\${SYSLOG_PORT}/udp" || true
      ufw allow "\${SYSLOG_PORT}/tcp" || true
      ufw --force enable || true

      systemctl daemon-reload
      systemctl enable --now netdash-updater.timer

      # Start Docker Compose stack
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
      echo "[NetDash Bootstrap] COMPLETE"

runcmd:
  - [ systemctl, enable, --now, qemu-guest-agent ]
  - [ /usr/local/bin/netdash-firstboot.sh ]
EOF
  msg_ok "Cloud-Init vendor config generated"

  # 3. Create and Configure the Proxmox KVM Virtual Machine
  msg_info "Creating Proxmox VM ${VMID} (${HN})"
  local net_arg="virtio,bridge=${BRG},firewall=1"
  [[ -n "${VLAN}" ]] && net_arg="${net_arg},tag=${VLAN}"
  [[ -n "${MAC}" ]] && net_arg="virtio=${MAC},bridge=${BRG},firewall=1${VLAN:+,tag=${VLAN}}"

  qm create "${VMID}" \
    --name "${HN}" \
    --ostype l26 \
    --machine q35 \
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

  # 4. Import Cloud Image Disk & Resize
  msg_info "Importing disk into storage pool '${STORAGE_POOL}' and resizing to ${DISK_SIZE}G"
  qm importdisk "${VMID}" "${img_path}" "${STORAGE_POOL}" >/dev/null 2>&1
  local disk_ref
  disk_ref="$(qm config "${VMID}" | awk -F': ' '/^unused0:/ {print $2}')"
  if [[ -z "${disk_ref}" ]]; then
    die "Failed to locate imported disk (unused0) on VM ${VMID}."
  fi

  qm set "${VMID}" \
    --scsi0 "${disk_ref},discard=on,iothread=1,ssd=1" \
    --boot "order=scsi0" \
    --ide2 "${STORAGE_POOL}:cloudinit" >/dev/null

  qm resize "${VMID}" scsi0 "${DISK_SIZE}G" >/dev/null
  msg_ok "Configured ${DISK_SIZE}G VirtIO SCSI SSD disk (discard=on, iothread=1, ssd=1)"

  # 5. Apply Cloud-Init Network, Credentials, and Vendor Snippet
  msg_info "Applying Cloud-Init parameters to VM ${VMID}"
  if [[ "${NET_MODE}" == "static" ]]; then
    qm set "${VMID}" --ipconfig0 "ip=${STATIC_IP},gw=${GATEWAY_IP}" >/dev/null
    [[ -n "${DNS_SERVER}" ]] && qm set "${VMID}" --nameserver "${DNS_SERVER}" >/dev/null
  else
    qm set "${VMID}" --ipconfig0 "ip=dhcp" >/dev/null
  fi

  qm set "${VMID}" \
    --ciuser "${CI_USER}" \
    --cipassword "${CI_PASSWORD}" \
    --cicustom "vendor=local:snippets/netdash-${VMID}-vendor.yaml" >/dev/null

  if [[ -n "${SSH_KEY}" ]]; then
    local tmp_key="/tmp/netdash-${VMID}.pub"
    echo "${SSH_KEY}" > "${tmp_key}"
    qm set "${VMID}" --sshkeys "${tmp_key}" >/dev/null
    rm -f "${tmp_key}"
  fi

  # Set community-scripts style HTML description in Proxmox UI Notes
  local vm_notes
  vm_notes="<div align='center'>
  <h2>NetDash — Network Observability Dashboard</h2>
  <p><b>GitHub Auto-Update:</b> Polls <code>origin/main</code> every 5m via <code>netdash-updater.timer</code></p>
  <p><b>Web UI Port:</b> <code>${WEB_PORT}/tcp</code> &nbsp;|&nbsp; <b>Syslog Port:</b> <code>${SYSLOG_PORT}/udp+tcp</code></p>
</div>"
  qm set "${VMID}" --description "${vm_notes}" >/dev/null
  msg_ok "Applied Cloud-Init configuration and Proxmox UI Notes"

  # 6. Start VM and Monitor Guest Bootstrap via QEMU Guest Agent
  msg_info "Starting VM ${VMID} (${HN})"
  qm start "${VMID}"
  msg_ok "Started VM ${VMID}"

  msg_info "Waiting for QEMU Guest Agent & IPv4 lease inside VM ${VMID} (this takes ~45-90s)"
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
    [[ -z "${vm_ip}" ]] && vm_ip="<VM-DHCP-IP>"
    msg_warn "QEMU Guest Agent still initializing; continuing bootstrap in background."
  fi

  # Wait for /usr/local/bin/netdash-firstboot.sh to complete if Guest Agent is responsive
  if qm guest cmd "${VMID}" ping >/dev/null 2>&1; then
    msg_info "Provisioning Docker CE, TimescaleDB, Redis, Vector & NetDash inside VM ${VMID}"
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
  echo -e "   ${INFO} ${BOLD}Dashboard URL:${CL}     ${BGN}http://${vm_ip}:${WEB_PORT}${CL}"
  echo -e "   ${INFO} ${BOLD}Syslog Receiver:${CL}   ${GN}${vm_ip}:${SYSLOG_PORT} (UDP/TCP)${CL}"
  echo -e "   ${INFO} ${BOLD}SSH Access:${CL}        ${BL}ssh ${CI_USER}@${vm_ip}${CL} (sudo enabled)"
  echo -e "   ${INFO} ${BOLD}Bootstrap Log:${CL}     ${BL}qm guest exec ${VMID} -- tail -n 50 ${BOOTSTRAP_LOG}${CL}"
  echo -e "   ${INFO} ${BOLD}Manual Git Pull:${CL}   ${BL}sudo netdash-update --force${CL} (inside VM)"
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
  if [[ -z "${GITHUB_TOKEN}" ]]; then
    prompt_guest_input "GitHub Personal Access Token (for Private Repo; leave blank if Public)" "" GITHUB_TOKEN "true"
  fi
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

  cat << 'EOF' > "${UPDATER_BIN}"
#!/usr/bin/env bash
set -euo pipefail
INSTALL_DIR="/opt/netdash"
FORCE_BUILD="${1:-}"
cd "${INSTALL_DIR}"
git fetch origin main --quiet
LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse origin/main)"
if [[ "${LOCAL_SHA}" != "${REMOTE_SHA}" ]] || [[ "${FORCE_BUILD}" == "--force" ]]; then
  logger -t netdash-updater "Updating NetDash from ${LOCAL_SHA:0:7} to ${REMOTE_SHA:0:7}..."
  git reset --hard origin/main
  if [[ -f backend/Dockerfile && -f frontend/Dockerfile ]]; then
    docker compose --env-file "${INSTALL_DIR}/.env" up -d --build --remove-orphans
  else
    docker compose --env-file "${INSTALL_DIR}/.env" up -d timescaledb redis vector
  fi
  docker image prune -f >/dev/null 2>&1 || true
fi
EOF
  chmod +x "${UPDATER_BIN}"

  cat << 'EOF' > /etc/systemd/system/netdash-updater.service
[Unit]
Description=NetDash GitHub Auto-Updater Service
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/netdash-update
WorkingDirectory=/opt/netdash
EOF

  cat << 'EOF' > /etc/systemd/system/netdash-updater.timer
[Unit]
Description=Poll GitHub every 5 minutes for NetDash updates

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now netdash-updater.timer
  msg_ok "Enabled netdash-updater.timer (5-minute GitOps polling)"

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
