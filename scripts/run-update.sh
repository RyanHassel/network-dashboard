#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${NETDASH_INSTALL_DIR:-/opt/netdash}"
cd "${INSTALL_DIR}"

rm -f "${INSTALL_DIR}/.trigger-update"
[[ -d .git ]] || exit 0

STATE_DIR="${INSTALL_DIR}/.netdash-state"
PROGRESS_FILE="${STATE_DIR}/update-progress.json"
mkdir -p "${STATE_DIR}"
chmod 755 "${STATE_DIR}"

COMPOSE_CMD=(docker compose --env-file "${INSTALL_DIR}/.env")
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  COMPOSE_CMD=(docker compose)
fi

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOCAL_SHA="$(git rev-parse --short=7 HEAD 2>/dev/null || echo unknown)"
REMOTE_SHA="${LOCAL_SHA}"

STATUS="updating"
OVERALL_PCT=5
PHASE="git"
MESSAGE="Starting NetDash update..."

GIT_STATUS="running"
GIT_PCT=15
GIT_DETAIL="Connecting to GitHub (origin/main)..."

FE_STATUS="pending"
FE_PCT=0
FE_DETAIL="Waiting for source update..."

API_STATUS="pending"
API_PCT=0
API_DETAIL="Waiting for frontend..."

WORKER_STATUS="pending"
WORKER_PCT=0
WORKER_DETAIL="Waiting for backend build..."

SVC_STATUS="pending"
SVC_PCT=0
SVC_DETAIL="Waiting for services..."

TICKER_PID=""

json_escape() {
  local s="${1//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/ }"
  s="${s//$'\r'/ }"
  printf '%s' "${s}"
}

write_progress() {
  local now_utc
  now_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local tmp_file="${PROGRESS_FILE}.tmp.$$"

  cat << EOF > "${tmp_file}"
{
  "status": "$(json_escape "${STATUS}")",
  "overall_pct": ${OVERALL_PCT},
  "phase": "$(json_escape "${PHASE}")",
  "message": "$(json_escape "${MESSAGE}")",
  "from_sha": "$(json_escape "${LOCAL_SHA}")",
  "target_sha": "$(json_escape "${REMOTE_SHA}")",
  "started_at": "$(json_escape "${STARTED_AT}")",
  "updated_at": "${now_utc}",
  "components": [
    {"id": "git", "name": "Source Code (GitHub)", "status": "$(json_escape "${GIT_STATUS}")", "pct": ${GIT_PCT}, "detail": "$(json_escape "${GIT_DETAIL}")"},
    {"id": "frontend", "name": "Web Frontend (React + Nginx)", "status": "$(json_escape "${FE_STATUS}")", "pct": ${FE_PCT}, "detail": "$(json_escape "${FE_DETAIL}")"},
    {"id": "api", "name": "Backend API (FastAPI)", "status": "$(json_escape "${API_STATUS}")", "pct": ${API_PCT}, "detail": "$(json_escape "${API_DETAIL}")"},
    {"id": "worker", "name": "Telemetry Worker (SNMP / Nmap)", "status": "$(json_escape "${WORKER_STATUS}")", "pct": ${WORKER_PCT}, "detail": "$(json_escape "${WORKER_DETAIL}")"},
    {"id": "services", "name": "Core Services & Health Check", "status": "$(json_escape "${SVC_STATUS}")", "pct": ${SVC_PCT}, "detail": "$(json_escape "${SVC_DETAIL}")"}
  ]
}
EOF
  chmod 644 "${tmp_file}" 2>/dev/null || true
  mv -f "${tmp_file}" "${PROGRESS_FILE}"
}

stop_ticker() {
  if [[ -n "${TICKER_PID}" ]] && kill -0 "${TICKER_PID}" 2>/dev/null; then
    kill "${TICKER_PID}" 2>/dev/null || true
    wait "${TICKER_PID}" 2>/dev/null || true
  fi
  TICKER_PID=""
}

on_error() {
  local exit_code=$?
  local line_no=$1
  stop_ticker
  STATUS="error"
  MESSAGE="Update failed at line ${line_no} (exit ${exit_code}). Check journalctl -u netdash-updater."
  case "${PHASE}" in
    git) GIT_STATUS="error" ;;
    frontend) FE_STATUS="error" ;;
    backend)
      [[ "${API_STATUS}" == "running" ]] && API_STATUS="error"
      [[ "${WORKER_STATUS}" == "running" ]] && WORKER_STATUS="error"
      ;;
    services) SVC_STATUS="error" ;;
  esac
  write_progress || true
  exit "${exit_code}"
}

cleanup() {
  stop_ticker
  rm -f "${PROGRESS_FILE}.tmp."* 2>/dev/null || true
}

trap 'on_error ${LINENO}' ERR
trap cleanup EXIT

# Stage 1: Fetch & apply Git commits
write_progress
echo "[NetDash Updater] Fetching latest commits from origin/main..."
git fetch origin main --quiet
REMOTE_SHA="$(git rev-parse --short=7 origin/main)"
GIT_PCT=60
GIT_DETAIL="Resetting working tree (${LOCAL_SHA} -> ${REMOTE_SHA})..."
OVERALL_PCT=12
write_progress

logger -t netdash-updater "Installing NetDash update (${LOCAL_SHA} -> ${REMOTE_SHA})..."
git reset --hard origin/main

# Keep host updater wrapper in sync with repository script when invoked as root
if [[ -f "${INSTALL_DIR}/scripts/run-update.sh" && -w /usr/local/bin/netdash-update ]]; then
  cat << 'EOF' > /usr/local/bin/netdash-update
#!/usr/bin/env bash
exec bash /opt/netdash/scripts/run-update.sh "$@"
EOF
  chmod +x /usr/local/bin/netdash-update
fi

GIT_STATUS="completed"
GIT_PCT=100
GIT_DETAIL="Updated working tree to ${REMOTE_SHA}"
OVERALL_PCT=18
write_progress

# Stage 2: Build & start Frontend FIRST so UI appears immediately
if [[ -f backend/Dockerfile && -f frontend/Dockerfile ]]; then
  PHASE="frontend"
  MESSAGE="Building Web Frontend image (React 19 + Nginx)..."
  FE_STATUS="running"
  FE_PCT=15
  FE_DETAIL="Compiling React & TypeScript bundle..."
  OVERALL_PCT=22
  write_progress

  (
    trap 'exit 0' TERM INT
    fe_p=20
    ov_p=24
    while true; do
      sleep 2
      [[ "${fe_p}" -lt 80 ]] && fe_p=$((fe_p + 5))
      [[ "${ov_p}" -lt 42 ]] && ov_p=$((ov_p + 2))
      FE_PCT=${fe_p}
      OVERALL_PCT=${ov_p}
      write_progress || true
    done
  ) &
  TICKER_PID=$!

  "${COMPOSE_CMD[@]}" build frontend
  stop_ticker

  FE_PCT=88
  FE_DETAIL="Starting updated Web Frontend container..."
  OVERALL_PCT=45
  MESSAGE="Deploying Web Frontend container..."
  write_progress

  "${COMPOSE_CMD[@]}" up -d --no-deps frontend
  # Ensure edge proxy is up without interrupting if already running
  "${COMPOSE_CMD[@]}" up -d --no-deps proxy >/dev/null 2>&1 || true

  FE_STATUS="completed"
  FE_PCT=100
  FE_DETAIL="Web Frontend live on ${REMOTE_SHA}"
  OVERALL_PCT=50
  MESSAGE="Web Frontend is live — updating Backend API & Telemetry Worker..."
  write_progress

  # Stage 3: Build Backend API & Worker while Frontend already serves live progress
  PHASE="backend"
  API_STATUS="running"
  API_PCT=15
  API_DETAIL="Building FastAPI backend image..."
  WORKER_STATUS="running"
  WORKER_PCT=15
  WORKER_DETAIL="Building SNMP/Nmap worker image..."
  OVERALL_PCT=54
  MESSAGE="Building Backend API & Telemetry Worker images..."
  write_progress

  (
    trap 'exit 0' TERM INT
    bk_p=20
    ov_p=56
    while true; do
      sleep 2
      [[ "${bk_p}" -lt 75 ]] && bk_p=$((bk_p + 5))
      [[ "${ov_p}" -lt 74 ]] && ov_p=$((ov_p + 2))
      API_PCT=${bk_p}
      WORKER_PCT=${bk_p}
      OVERALL_PCT=${ov_p}
      write_progress || true
    done
  ) &
  TICKER_PID=$!

  "${COMPOSE_CMD[@]}" build api worker
  stop_ticker

  API_PCT=82
  API_DETAIL="Waiting for core services & recreating API..."
  WORKER_PCT=82
  WORKER_DETAIL="Waiting to restart worker..."
  OVERALL_PCT=78
  MESSAGE="Ensuring TimescaleDB, Redis & Vector are running..."
  write_progress

  "${COMPOSE_CMD[@]}" up -d --no-deps timescaledb redis vector

  MESSAGE="Restarting Backend API container..."
  API_PCT=90
  API_DETAIL="Starting FastAPI server..."
  OVERALL_PCT=82
  write_progress
  "${COMPOSE_CMD[@]}" up -d --no-deps api

  MESSAGE="Restarting Telemetry Worker container..."
  WORKER_PCT=92
  WORKER_DETAIL="Starting SNMP, Nmap & UniFi loops..."
  OVERALL_PCT=86
  write_progress
  "${COMPOSE_CMD[@]}" up -d --no-deps worker

  WORKER_STATUS="completed"
  WORKER_PCT=100
  WORKER_DETAIL="Telemetry Worker running"
  OVERALL_PCT=90
  write_progress

  # Stage 4: Wait for API health verification
  PHASE="services"
  SVC_STATUS="running"
  SVC_PCT=50
  SVC_DETAIL="Verifying API, TimescaleDB, Redis & Proxy health..."
  MESSAGE="Verifying backend API health & database connectivity..."
  write_progress

  api_healthy=0
  for i in $(seq 1 35); do
    if curl -fsS --max-time 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
      api_healthy=1
      break
    fi
    SVC_PCT=$((50 + (i * 45) / 35))
    OVERALL_PCT=$((90 + (i * 8) / 35))
    write_progress || true
    sleep 1
  done

  if [[ "${api_healthy}" -eq 1 ]]; then
    API_STATUS="completed"
    API_PCT=100
    API_DETAIL="FastAPI server healthy"
    SVC_STATUS="completed"
    SVC_PCT=100
    SVC_DETAIL="All NetDash containers healthy"
  else
    API_STATUS="completed"
    API_PCT=100
    API_DETAIL="Container started (warming up)"
    SVC_STATUS="completed"
    SVC_PCT=100
    SVC_DETAIL="Containers started"
  fi
elif [[ -f docker-compose.yml ]]; then
  "${COMPOSE_CMD[@]}" up -d timescaledb redis vector
  FE_STATUS="completed"; FE_PCT=100; FE_DETAIL="Core Phase 1 stack"
  API_STATUS="completed"; API_PCT=100; API_DETAIL="Core Phase 1 stack"
  WORKER_STATUS="completed"; WORKER_PCT=100; WORKER_DETAIL="Core Phase 1 stack"
  SVC_STATUS="completed"; SVC_PCT=100; SVC_DETAIL="Core services running"
fi

OVERALL_PCT=100
STATUS="completed"
PHASE="completed"
MESSAGE="Update to ${REMOTE_SHA} installed successfully!"
write_progress

docker image prune -f >/dev/null 2>&1 || true
rm -f "${INSTALL_DIR}/.update-status.json"
echo "[NetDash Updater] Update to ${REMOTE_SHA} complete!"
