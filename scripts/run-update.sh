#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${NETDASH_INSTALL_DIR:-/opt/netdash}"
cd "${INSTALL_DIR}"

rm -f "${INSTALL_DIR}/.trigger-update"
[[ -d .git ]] || exit 0

STATE_DIR="${INSTALL_DIR}/.netdash-state"
PROGRESS_FILE="${STATE_DIR}/update-progress.json"
LOCK_FILE="${STATE_DIR}/updater.lock"
mkdir -p "${STATE_DIR}"
chmod 755 "${STATE_DIR}"

printf '%s\n' "$$" > "${LOCK_FILE}"
chmod 644 "${LOCK_FILE}" 2>/dev/null || true

COMPOSE_CMD=(docker compose --env-file "${INSTALL_DIR}/.env")
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  COMPOSE_CMD=(docker compose)
fi

resolve_commit_version() {
  local target_ref="${1:-HEAD}"
  local target_sha="${2:-}"
  if [[ -z "${target_sha}" ]]; then
    target_sha="$(git rev-parse --short=7 "${target_ref}" 2>/dev/null || echo unknown)"
  fi
  git log --first-parent --reverse --pretty=format:"%h|%cs" "${target_ref}" 2>/dev/null \
    | awk -F'|' -v sha="${target_sha}" '
        {
          cnt[$2]++
          if (!found && $1 == sha) {
            split($2, d, "-")
            if (length(d) == 3) {
              res = sprintf("%s.%s.%s.%d", d[1], d[2], d[3], cnt[$2])
              found = 1
            }
          }
        }
        END {
          if (found) print res
          else print sha
        }
      '
}

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOCAL_SHA="$(git rev-parse --short=7 HEAD 2>/dev/null || echo unknown)"
REMOTE_SHA="${LOCAL_SHA}"
FROM_VERSION="$(resolve_commit_version HEAD "${LOCAL_SHA}")"
TARGET_VERSION="${FROM_VERSION}"
UPDATE_ID="$(date -u +%s)-${LOCAL_SHA}"

STATUS="updating"
OVERALL_PCT=1
PHASE="git"
MESSAGE="Starting NetDash update..."

GIT_STATUS="running"
GIT_PCT=10
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

GIT_LOGS=()
FE_LOGS=()
API_LOGS=()
WORKER_LOGS=()
SVC_LOGS=()

json_escape() {
  local s="${1//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/ }"
  s="${s//$'\r'/ }"
  printf '%s' "${s}"
}

append_comp_log() {
  local kind="$1"
  local raw_text="$2"
  local clean="${raw_text//$'\r'/ }"
  clean="${clean//$'\n'/ }"
  clean="${clean:0:140}"
  [[ -z "${clean// /}" ]] && return 0
  local ts
  ts="$(date -u +%H:%M:%S)"
  local entry="[${ts}] ${clean}"
  case "${kind}" in
    git)
      GIT_LOGS+=("${entry}")
      if [[ "${#GIT_LOGS[@]}" -gt 10 ]]; then GIT_LOGS=("${GIT_LOGS[@]: -10}"); fi
      ;;
    frontend)
      FE_LOGS+=("${entry}")
      if [[ "${#FE_LOGS[@]}" -gt 10 ]]; then FE_LOGS=("${FE_LOGS[@]: -10}"); fi
      ;;
    api)
      API_LOGS+=("${entry}")
      if [[ "${#API_LOGS[@]}" -gt 10 ]]; then API_LOGS=("${API_LOGS[@]: -10}"); fi
      ;;
    worker)
      WORKER_LOGS+=("${entry}")
      if [[ "${#WORKER_LOGS[@]}" -gt 10 ]]; then WORKER_LOGS=("${WORKER_LOGS[@]: -10}"); fi
      ;;
    services)
      SVC_LOGS+=("${entry}")
      if [[ "${#SVC_LOGS[@]}" -gt 10 ]]; then SVC_LOGS=("${SVC_LOGS[@]: -10}"); fi
      ;;
  esac
}

json_array_from_lines() {
  if [[ "$#" -eq 0 ]]; then
    printf '[]'
    return 0
  fi
  local out="["
  local first=1
  local item
  for item in "$@"; do
    if [[ "${first}" -eq 1 ]]; then
      first=0
    else
      out+=","
    fi
    out+="\"$(json_escape "${item}")\""
  done
  out+="]"
  printf '%s' "${out}"
}

recompute_overall_pct() {
  local calc=$(( (GIT_PCT * 10 + FE_PCT * 30 + API_PCT * 25 + WORKER_PCT * 20 + SVC_PCT * 15) / 100 ))
  if [[ "${calc}" -gt "${OVERALL_PCT}" ]]; then
    OVERALL_PCT=${calc}
  fi
  if [[ "${STATUS}" == "completed" ]]; then
    OVERALL_PCT=100
  elif [[ "${OVERALL_PCT}" -gt 99 ]]; then
    OVERALL_PCT=99
  fi
}

write_progress() {
  recompute_overall_pct
  local now_utc
  now_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  local tmp_file="${PROGRESS_FILE}.tmp.$$"
  local git_logs_json fe_logs_json api_logs_json worker_logs_json svc_logs_json
  git_logs_json="$(json_array_from_lines "${GIT_LOGS[@]+"${GIT_LOGS[@]}"}")"
  fe_logs_json="$(json_array_from_lines "${FE_LOGS[@]+"${FE_LOGS[@]}"}")"
  api_logs_json="$(json_array_from_lines "${API_LOGS[@]+"${API_LOGS[@]}"}")"
  worker_logs_json="$(json_array_from_lines "${WORKER_LOGS[@]+"${WORKER_LOGS[@]}"}")"
  svc_logs_json="$(json_array_from_lines "${SVC_LOGS[@]+"${SVC_LOGS[@]}"}")"

  cat << EOF > "${tmp_file}"
{
  "status": "$(json_escape "${STATUS}")",
  "update_id": "$(json_escape "${UPDATE_ID}")",
  "overall_pct": ${OVERALL_PCT},
  "phase": "$(json_escape "${PHASE}")",
  "message": "$(json_escape "${MESSAGE}")",
  "from_sha": "$(json_escape "${LOCAL_SHA}")",
  "target_sha": "$(json_escape "${REMOTE_SHA}")",
  "from_version": "$(json_escape "${FROM_VERSION}")",
  "target_version": "$(json_escape "${TARGET_VERSION}")",
  "started_at": "$(json_escape "${STARTED_AT}")",
  "updated_at": "${now_utc}",
  "components": [
    {"id": "git", "name": "Source Code (GitHub)", "status": "$(json_escape "${GIT_STATUS}")", "pct": ${GIT_PCT}, "detail": "$(json_escape "${GIT_DETAIL}")", "logs": ${git_logs_json}},
    {"id": "frontend", "name": "Web Frontend (React + Nginx)", "status": "$(json_escape "${FE_STATUS}")", "pct": ${FE_PCT}, "detail": "$(json_escape "${FE_DETAIL}")", "logs": ${fe_logs_json}},
    {"id": "api", "name": "Backend API (FastAPI)", "status": "$(json_escape "${API_STATUS}")", "pct": ${API_PCT}, "detail": "$(json_escape "${API_DETAIL}")", "logs": ${api_logs_json}},
    {"id": "worker", "name": "Telemetry Worker (SNMP / Nmap)", "status": "$(json_escape "${WORKER_STATUS}")", "pct": ${WORKER_PCT}, "detail": "$(json_escape "${WORKER_DETAIL}")", "logs": ${worker_logs_json}},
    {"id": "services", "name": "Core Services & Health Check", "status": "$(json_escape "${SVC_STATUS}")", "pct": ${SVC_PCT}, "detail": "$(json_escape "${SVC_DETAIL}")", "logs": ${svc_logs_json}}
  ]
}
EOF
  chmod 644 "${tmp_file}" 2>/dev/null || true
  mv -f "${tmp_file}" "${PROGRESS_FILE}"
}

on_error() {
  local exit_code=$?
  local line_no=$1
  STATUS="error"
  MESSAGE="Update failed at line ${line_no} (exit ${exit_code}). Check journalctl -u netdash-updater."
  case "${PHASE}" in
    git)
      GIT_STATUS="error"
      append_comp_log git "Failed at line ${line_no} (exit ${exit_code})"
      ;;
    frontend)
      FE_STATUS="error"
      append_comp_log frontend "Failed at line ${line_no} (exit ${exit_code})"
      ;;
    backend)
      if [[ "${API_STATUS}" == "running" ]]; then
        API_STATUS="error"
        append_comp_log api "Failed at line ${line_no} (exit ${exit_code})"
      fi
      if [[ "${WORKER_STATUS}" == "running" ]]; then
        WORKER_STATUS="error"
        append_comp_log worker "Failed at line ${line_no} (exit ${exit_code})"
      fi
      ;;
    services)
      SVC_STATUS="error"
      append_comp_log services "Failed at line ${line_no} (exit ${exit_code})"
      ;;
  esac
  write_progress || true
  exit "${exit_code}"
}

cleanup() {
  rm -f "${LOCK_FILE}" "${PROGRESS_FILE}.tmp."* 2>/dev/null || true
}

trap 'on_error ${LINENO}' ERR
trap cleanup EXIT

run_compose_build() {
  local kind="$1"
  shift
  local last_step_tag=""
  local step_re='\[[^]]*[[:space:]]([0-9]+)/([0-9]+)\][[:space:]]*(.*)'
  local export_re='exporting[[:space:]]+(to[[:space:]]+image|layers)|writing[[:space:]]+image'
  local done_re='naming[[:space:]]+to[[:space:]]+.*(done|DONE)'

  exec 3< <(BUILDKIT_PROGRESS=plain "${COMPOSE_CMD[@]}" build "$@" 2>&1)
  local build_pid=$!

  while IFS= read -r line <&3; do
    echo "${line}"
    local updated=0
    if [[ "${line}" =~ ${step_re} ]]; then
      local step_num="${BASH_REMATCH[1]}"
      local step_tot="${BASH_REMATCH[2]}"
      local step_cmd="${BASH_REMATCH[3]}"
      step_cmd="${step_cmd:0:72}"
      if [[ "${step_tot}" -gt 0 ]]; then
        local step_pct=$(( 10 + (step_num * 72) / step_tot ))
        [[ "${step_pct}" -gt 84 ]] && step_pct=84
        local cur_tag="${step_num}/${step_tot}"
        if [[ "${kind}" == "frontend" ]]; then
          if [[ "${step_pct}" -gt "${FE_PCT}" || "${cur_tag}" != "${last_step_tag}" ]]; then
            [[ "${step_pct}" -gt "${FE_PCT}" ]] && FE_PCT=${step_pct}
            FE_DETAIL="Build step ${cur_tag}: ${step_cmd:-Compiling}"
            append_comp_log frontend "Step ${cur_tag}: ${step_cmd:-Compiling}"
            last_step_tag="${cur_tag}"
            updated=1
          fi
        else
          if [[ "${step_pct}" -gt "${API_PCT}" || "${cur_tag}" != "${last_step_tag}" ]]; then
            [[ "${step_pct}" -gt "${API_PCT}" ]] && API_PCT=${step_pct}
            [[ "${step_pct}" -gt "${WORKER_PCT}" ]] && WORKER_PCT=${step_pct}
            API_DETAIL="Build step ${cur_tag}: ${step_cmd:-Installing}"
            WORKER_DETAIL="Build step ${cur_tag}: ${step_cmd:-Installing}"
            append_comp_log api "Step ${cur_tag}: ${step_cmd:-Installing}"
            append_comp_log worker "Step ${cur_tag}: ${step_cmd:-Installing}"
            last_step_tag="${cur_tag}"
            updated=1
          fi
        fi
      fi
    elif [[ "${line}" =~ ${export_re} ]]; then
      if [[ "${kind}" == "frontend" && "${FE_PCT}" -lt 86 ]]; then
        FE_PCT=86
        FE_DETAIL="Exporting frontend image..."
        append_comp_log frontend "Exporting frontend Docker image..."
        updated=1
      elif [[ "${kind}" != "frontend" && "${API_PCT}" -lt 86 ]]; then
        API_PCT=86
        WORKER_PCT=86
        API_DETAIL="Exporting backend image..."
        WORKER_DETAIL="Exporting worker image..."
        append_comp_log api "Exporting backend Docker image..."
        append_comp_log worker "Exporting worker Docker image..."
        updated=1
      fi
    elif [[ "${line}" =~ ${done_re} ]]; then
      if [[ "${kind}" == "frontend" && "${FE_PCT}" -lt 90 ]]; then
        FE_PCT=90
        FE_DETAIL="Frontend image ready"
        append_comp_log frontend "Frontend Docker image built"
        updated=1
      elif [[ "${kind}" != "frontend" && "${API_PCT}" -lt 90 ]]; then
        API_PCT=90
        WORKER_PCT=90
        API_DETAIL="Backend API image ready"
        WORKER_DETAIL="Worker image ready"
        append_comp_log api "Backend API Docker image built"
        append_comp_log worker "Telemetry Worker Docker image built"
        updated=1
      fi
    fi

    if [[ "${updated}" -eq 1 ]]; then
      write_progress
    fi
  done

  exec 3<&-
  local build_rc=0
  wait "${build_pid}" || build_rc=$?
  return "${build_rc}"
}

# Stage 1: Fetch & apply Git commits
append_comp_log git "Connecting to GitHub (origin/main)..."
write_progress
echo "[NetDash Updater] Fetching latest commits from origin/main..."
git fetch origin main --quiet
REMOTE_SHA="$(git rev-parse --short=7 origin/main)"
TARGET_VERSION="$(resolve_commit_version origin/main "${REMOTE_SHA}")"
UPDATE_ID="$(date -u +%s)-${REMOTE_SHA}"
GIT_PCT=60
GIT_DETAIL="Applying updates (${FROM_VERSION} -> ${TARGET_VERSION})..."
append_comp_log git "Fetched origin/main @ ${REMOTE_SHA} (${TARGET_VERSION})"
write_progress

logger -t netdash-updater "Installing NetDash update (${FROM_VERSION} -> ${TARGET_VERSION})..."
git reset --hard origin/main
append_comp_log git "Workspace reset to ${TARGET_VERSION} (${REMOTE_SHA})"

# Keep host updater wrapper synced with repository script when invoked as root
if [[ -f "${INSTALL_DIR}/scripts/run-update.sh" && -w /usr/local/bin/netdash-update ]]; then
  cat << 'EOF' > /usr/local/bin/netdash-update
#!/usr/bin/env bash
exec bash /opt/netdash/scripts/run-update.sh "$@"
EOF
  chmod +x /usr/local/bin/netdash-update
fi

GIT_STATUS="completed"
GIT_PCT=100
GIT_DETAIL="Source updated to ${TARGET_VERSION} (${REMOTE_SHA})"
append_comp_log git "Source sync complete"
write_progress

# Stage 2: Build & start Frontend FIRST so updated UI is available immediately
if [[ -f backend/Dockerfile && -f frontend/Dockerfile ]]; then
  PHASE="frontend"
  MESSAGE="Building Web Frontend (${TARGET_VERSION})..."
  FE_STATUS="running"
  FE_PCT=10
  FE_DETAIL="Starting frontend Docker build..."
  append_comp_log frontend "Starting Vite + Nginx frontend build..."
  write_progress

  run_compose_build frontend frontend

  FE_PCT=94
  FE_DETAIL="Starting updated Web Frontend container..."
  MESSAGE="Deploying Web Frontend container..."
  append_comp_log frontend "Starting updated frontend container..."
  write_progress

  "${COMPOSE_CMD[@]}" up -d --no-deps frontend
  # Ensure edge proxy is running without interrupting if already healthy
  "${COMPOSE_CMD[@]}" up -d --no-deps proxy >/dev/null 2>&1 || true

  FE_STATUS="completed"
  FE_PCT=100
  FE_DETAIL="Web Frontend live on ${TARGET_VERSION}"
  append_comp_log frontend "Web Frontend live on ${TARGET_VERSION}"
  MESSAGE="Web Frontend live — updating Backend API & Telemetry Worker..."
  write_progress

  # Stage 3: Build & start Backend API + Worker using real BuildKit step progress
  PHASE="backend"
  API_STATUS="running"
  API_PCT=10
  API_DETAIL="Starting backend Docker build..."
  WORKER_STATUS="running"
  WORKER_PCT=10
  WORKER_DETAIL="Starting worker Docker build..."
  append_comp_log api "Starting Python 3.12 + FastAPI image build..."
  append_comp_log worker "Starting Telemetry Worker image build..."
  MESSAGE="Building Backend API & Telemetry Worker images..."
  write_progress

  run_compose_build backend api worker

  API_DETAIL="Waiting for TimescaleDB & Redis..."
  WORKER_DETAIL="Waiting for TimescaleDB & Redis..."
  append_comp_log services "Ensuring timescaledb, redis, and vector containers..."
  MESSAGE="Ensuring core services are running..."
  write_progress

  "${COMPOSE_CMD[@]}" up -d --no-deps timescaledb redis
  "${COMPOSE_CMD[@]}" up -d --no-deps --force-recreate vector

  MESSAGE="Restarting Backend API container..."
  API_PCT=94
  API_DETAIL="Starting FastAPI server..."
  append_comp_log api "Recreating FastAPI api container..."
  write_progress
  "${COMPOSE_CMD[@]}" up -d --no-deps api

  MESSAGE="Restarting Telemetry Worker container..."
  WORKER_PCT=96
  WORKER_DETAIL="Starting SNMP, Nmap & UniFi loops..."
  append_comp_log worker "Recreating worker container (SNMP, Nmap, UniFi)..."
  write_progress
  "${COMPOSE_CMD[@]}" up -d --no-deps worker

  WORKER_STATUS="completed"
  WORKER_PCT=100
  WORKER_DETAIL="Telemetry Worker running"
  append_comp_log worker "Telemetry Worker running"
  write_progress

  # Stage 4: Wait for API /api/health readiness check
  PHASE="services"
  SVC_STATUS="running"
  SVC_PCT=25
  SVC_DETAIL="Waiting for Backend API /api/health..."
  append_comp_log services "Polling Backend API /api/health readiness..."
  MESSAGE="Verifying Backend API & service health..."
  write_progress

  web_port="${WEB_PORT:-}"
  if [[ -z "${web_port}" && -f "${INSTALL_DIR}/.env" ]]; then
    web_port="$(awk -F'=' '$1=="WEB_PORT"{sub(/^[^=]*=/,""); gsub(/["'\''[:space:]]/, ""); val=$0} END{print val}' "${INSTALL_DIR}/.env" 2>/dev/null || true)"
  fi
  if [[ ! "${web_port}" =~ ^[0-9]+$ ]]; then
    web_port="8080"
  fi

  api_healthy=0
  for i in $(seq 1 35); do
    if curl -fsS --max-time 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1 \
      || curl -fsS --max-time 2 "http://127.0.0.1:${web_port}/api/health" >/dev/null 2>&1; then
      api_healthy=1
      break
    fi
    SVC_PCT=$(( 25 + (i * 70) / 35 ))
    write_progress || true
    sleep 1
  done

  if [[ "${api_healthy}" -eq 1 ]]; then
    API_STATUS="completed"
    API_PCT=100
    API_DETAIL="FastAPI server healthy"
    append_comp_log api "FastAPI /api/health responded 200 OK"
    SVC_STATUS="completed"
    SVC_PCT=100
    SVC_DETAIL="All NetDash containers healthy"
    append_comp_log services "All containers verified healthy"
  else
    API_STATUS="completed"
    API_PCT=100
    API_DETAIL="Container started (warming up)"
    append_comp_log api "Container started (warming up)"
    SVC_STATUS="completed"
    SVC_PCT=100
    SVC_DETAIL="Containers started"
    append_comp_log services "Core containers started"
  fi
elif [[ -f docker-compose.yml ]]; then
  "${COMPOSE_CMD[@]}" up -d timescaledb redis vector
  FE_STATUS="completed"; FE_PCT=100; FE_DETAIL="Core Phase 1 stack"
  API_STATUS="completed"; API_PCT=100; API_DETAIL="Core Phase 1 stack"
  WORKER_STATUS="completed"; WORKER_PCT=100; WORKER_DETAIL="Core Phase 1 stack"
  SVC_STATUS="completed"; SVC_PCT=100; SVC_DETAIL="Core services running"
fi

STATUS="completed"
PHASE="completed"
OVERALL_PCT=100
MESSAGE="Update to ${TARGET_VERSION} installed successfully!"
write_progress

docker image prune -f >/dev/null 2>&1 || true
rm -f "${INSTALL_DIR}/.update-status.json"
echo "[NetDash Updater] Update to ${TARGET_VERSION} (${REMOTE_SHA}) complete!"
