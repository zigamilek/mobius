#!/usr/bin/env bash
source /dev/stdin <<< "$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors

APP="Mobius"
APP_DIR="/opt/mobius"
CONFIG_DIR="/etc/mobius"
SERVICE_NAME="mobius"
SERVICE_USER="mobius"
REPO_URL="${REPO_URL:-https://github.com/zigamilek/mobius.git}"
REPO_REF="${REPO_REF:-master}"
BOOTSTRAP_LOCAL_DB="${MOBIUS_BOOTSTRAP_LOCAL_DB:-yes}"

_detect_service_port() {
  local config_file="${CONFIG_DIR}/config.yaml"
  local parsed=""
  if [[ -f "${config_file}" ]]; then
    parsed="$(
      awk '
        /^[[:space:]]*server:[[:space:]]*$/ { in_server=1; next }
        in_server && /^[^[:space:]]/ { in_server=0 }
        in_server && /^[[:space:]]*port:[[:space:]]*[0-9]+[[:space:]]*$/ {
          line = $0
          sub(/^[[:space:]]*port:[[:space:]]*/, "", line)
          sub(/[[:space:]]*$/, "", line)
          print line
          exit
        }
      ' "${config_file}" 2>/dev/null || true
    )"
  fi
  if [[ "${parsed}" =~ ^[0-9]+$ ]]; then
    echo "${parsed}"
  else
    echo "8080"
  fi
}

_verify_service_start() {
  local service_name="$1"
  local port
  local health_url
  local i

  if ! systemctl is-active --quiet "${service_name}"; then
    msg_error "${service_name} is not active after startup."
    systemctl status "${service_name}" --no-pager || true
    journalctl -u "${service_name}" -n 120 --no-pager || true
    return 1
  fi

  port="$(_detect_service_port)"
  health_url="http://127.0.0.1:${port}/healthz"
  for i in {1..20}; do
    if curl -fsS --max-time 2 "${health_url}" >/dev/null 2>&1; then
      msg_ok "Service health check passed: ${health_url}"
      return 0
    fi
    sleep 1
  done

  msg_error "Service failed health checks: ${health_url}"
  systemctl status "${service_name}" --no-pager || true
  journalctl -u "${service_name}" -n 120 --no-pager || true
  return 1
}

if [[ "${VERBOSE:-no}" == "yes" ]]; then
  msg_info "Verbose mode enabled: full installer output active (xtrace disabled)."
fi

msg_info "Updating package index"
$STD apt-get update
msg_ok "Updated package index"

msg_info "Installing dependencies"
$STD apt-get install -y git python3 python3-venv python3-pip rsync ca-certificates curl
msg_ok "Installed dependencies"

msg_info "Creating service user"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  $STD useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
msg_ok "Service user ready"

msg_info "Preparing directories"
$STD mkdir -p "${APP_DIR}" "${CONFIG_DIR}/system_prompts" /var/log/mobius /var/lib/mobius/state
msg_ok "Directories prepared"

msg_info "Cloning repository"
if [[ -d "${APP_DIR}/.git" ]]; then
  $STD rm -rf "${APP_DIR}"
fi
if [[ -n "${REPO_REF}" && "${REPO_REF}" != "HEAD" ]]; then
  $STD git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" "${APP_DIR}"
else
  $STD git clone --depth 1 "${REPO_URL}" "${APP_DIR}"
fi
msg_ok "Repository cloned"

msg_info "Building Python environment"
$STD python3 -m venv "${APP_DIR}/.venv"
$STD "${APP_DIR}/.venv/bin/pip" install --upgrade pip
$STD "${APP_DIR}/.venv/bin/pip" install -e "${APP_DIR}"
msg_ok "Python environment ready"

msg_info "Installing CLI command symlinks"
$STD ln -sf "${APP_DIR}/.venv/bin/mobius" /usr/local/bin/mobius
msg_ok "CLI command available: mobius"

msg_info "Installing configuration"
if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  $STD cp "${APP_DIR}/config.yaml" "${CONFIG_DIR}/config.yaml"
fi
if [[ ! -f "${CONFIG_DIR}/mobius.env" ]]; then
  cat <<'EOF' > "${CONFIG_DIR}/mobius.env"
OPENAI_API_KEY=
GEMINI_API_KEY=
MOBIUS_API_KEY=change-me
MOBIUS_STATE_DSN=
EOF
fi
$STD chmod 600 "${CONFIG_DIR}/mobius.env"

for prompt_file in "${APP_DIR}/system_prompts/"*.md; do
  prompt_name="$(basename "${prompt_file}")"
  [[ -f "${CONFIG_DIR}/system_prompts/${prompt_name}" ]] || $STD cp "${prompt_file}" "${CONFIG_DIR}/system_prompts/${prompt_name}"
done
msg_ok "Configuration installed"

msg_info "Installing systemd service"
$STD cp "${APP_DIR}/deploy/systemd/mobius.service" "/etc/systemd/system/${SERVICE_NAME}.service"

_should_bootstrap_local_db() {
  local value
  value="$(echo "${BOOTSTRAP_LOCAL_DB}" | tr '[:upper:]' '[:lower:]')"
  case "${value}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

_config_requires_state_dsn_bootstrap() {
  local config_file="${CONFIG_DIR}/config.yaml"
  local env_file="${CONFIG_DIR}/mobius.env"

  "${APP_DIR}/.venv/bin/python" - "${config_file}" "${env_file}" <<'PY'
import os
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
env_path = Path(sys.argv[2])

if env_path.exists():
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()

try:
    from mobius.config import load_config
except Exception:
    raise SystemExit(1)

try:
    load_config(config_path)
except Exception as exc:
    if "state.database.dsn must be set when state.enabled is true." in str(exc):
        raise SystemExit(10)
    raise SystemExit(1)

raise SystemExit(0)
PY
  local rc=$?
  [[ "${rc}" -eq 10 ]]
}

if _should_bootstrap_local_db; then
  msg_info "Bootstrapping local PostgreSQL for state features"
  if $STD /usr/local/bin/mobius db bootstrap-local --yes --no-restart; then
    msg_ok "Local PostgreSQL bootstrap completed"
  else
    msg_warn "Local PostgreSQL bootstrap failed; continuing install with state disabled"
    msg_warn "Run 'mobius db bootstrap-local' after install to retry"
  fi
else
  msg_info "Skipping local PostgreSQL bootstrap (MOBIUS_BOOTSTRAP_LOCAL_DB=${BOOTSTRAP_LOCAL_DB})"
fi

if _config_requires_state_dsn_bootstrap; then
  msg_warn "Detected state.enabled=true without MOBIUS_STATE_DSN after install prep"
  msg_warn "Running local PostgreSQL bootstrap automatically to keep service healthy"
  if $STD /usr/local/bin/mobius db bootstrap-local --yes --no-restart; then
    msg_ok "Local PostgreSQL bootstrap completed"
  else
    msg_error "Automatic DB bootstrap failed while state requires a DSN."
    msg_error "Run 'mobius db bootstrap-local --yes' and retry installation."
    return 1
  fi
fi

msg_info "Applying ownership"
$STD chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}" "${CONFIG_DIR}" /var/log/mobius /var/lib/mobius/state
msg_ok "Ownership applied"

$STD systemctl daemon-reload
$STD systemctl enable -q --now "${SERVICE_NAME}"
_verify_service_start "${SERVICE_NAME}"
msg_ok "Service installed and started"

msg_ok "Installation complete"
msg_ok "Run 'mobius onboarding' to configure keys and settings."
