#!/usr/bin/env bash
source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)
# Copyright (c) 2021-2026 community-scripts ORG
# Author: community-scripts contributors
# Maintainer: zigamilek
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Source: https://github.com/zigamilek/ai-agents-hub

APP="AI Agents Hub"
var_tags="${var_tags:-ai;agents;llm}"
var_cpu="${var_cpu:-2}"
var_ram="${var_ram:-4096}"
var_disk="${var_disk:-8}"
var_os="${var_os:-debian}"
var_version="${var_version:-12}"
var_unprivileged="${var_unprivileged:-1}"
var_gpu="${var_gpu:-no}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  local APP_DIR="/opt/ai-agents-hub"
  local CONFIG_DIR="/etc/ai-agents-hub"
  local DATA_DIR="/var/lib/ai-agents-hub"
  local SERVICE_NAME="ai-agents-hub"
  local REPO_URL="${REPO_URL:-https://github.com/zigamilek/ai-agents-hub.git}"
  local REPO_REF="${REPO_REF:-}"

  msg_info "Stopping ${SERVICE_NAME} service"
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  msg_ok "Stopped ${SERVICE_NAME} service"

  msg_info "Installing/updating OS dependencies"
  $STD apt-get update
  $STD apt-get install -y git python3 python3-venv python3-pip rsync ca-certificates curl
  msg_ok "Dependencies installed"

  msg_info "Ensuring service user exists"
  if ! id -u aihub >/dev/null 2>&1; then
    useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin aihub
  fi
  msg_ok "Service user ready"

  msg_info "Updating repository"
  if [[ ! -d "${APP_DIR}/.git" ]]; then
    rm -rf "${APP_DIR}"
    if [[ -n "${REPO_REF}" ]]; then
      git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" "${APP_DIR}"
    else
      git clone --depth 1 "${REPO_URL}" "${APP_DIR}"
    fi
  else
    pushd "${APP_DIR}" >/dev/null
    if [[ -n "${REPO_REF}" ]]; then
      git fetch origin "${REPO_REF}"
      git checkout "${REPO_REF}"
      git pull --ff-only origin "${REPO_REF}"
    else
      git pull --ff-only
    fi
    popd >/dev/null
  fi
  msg_ok "Repository updated"

  msg_info "Rebuilding Python environment"
  python3 -m venv "${APP_DIR}/.venv"
  "${APP_DIR}/.venv/bin/pip" install --upgrade pip
  "${APP_DIR}/.venv/bin/pip" install -e "${APP_DIR}"
  msg_ok "Python environment ready"

  msg_info "Refreshing runtime files"
  mkdir -p "${CONFIG_DIR}" "${CONFIG_DIR}/prompts/specialists" "${DATA_DIR}/memories" "${DATA_DIR}/obsidian" /var/log/ai-agents-hub
  [[ -f "${CONFIG_DIR}/config.yaml" ]] || cp "${APP_DIR}/config.yaml" "${CONFIG_DIR}/config.yaml"
  if [[ ! -f "${CONFIG_DIR}/ai-agents-hub.env" ]]; then
    cat <<'EOF' > "${CONFIG_DIR}/ai-agents-hub.env"
OPENAI_API_KEY=
GEMINI_API_KEY=
AI_AGENTS_HUB_API_KEY=change-me
EOF
  fi
  chmod 600 "${CONFIG_DIR}/ai-agents-hub.env"
  for prompt_file in "${APP_DIR}/prompts/specialists/"*.md; do
    prompt_name="$(basename "${prompt_file}")"
    [[ -f "${CONFIG_DIR}/prompts/specialists/${prompt_name}" ]] || cp "${prompt_file}" "${CONFIG_DIR}/prompts/specialists/${prompt_name}"
  done
  cp "${APP_DIR}/deploy/systemd/ai-agents-hub.service" "/etc/systemd/system/${SERVICE_NAME}.service"
  chown -R aihub:aihub "${APP_DIR}" "${CONFIG_DIR}" "${DATA_DIR}" /var/log/ai-agents-hub
  msg_ok "Runtime files refreshed"

  msg_info "Restarting ${SERVICE_NAME}"
  $STD systemctl daemon-reload
  $STD systemctl enable -q --now "${SERVICE_NAME}"
  msg_ok "Updated successfully"

  msg_ok "Use 'ai-agents-hub onboard' to update env/config interactively."
  exit
}

start
build_container
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW} Access it using the following URL:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:8080${CL}"
