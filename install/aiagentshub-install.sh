#!/usr/bin/env bash
source /dev/stdin <<< "$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors

APP="AI Agents Hub"
APP_DIR="/opt/ai-agents-hub"
DATA_DIR="/var/lib/ai-agents-hub"
CONFIG_DIR="/etc/ai-agents-hub"
SERVICE_NAME="ai-agents-hub"
REPO_URL="${REPO_URL:-https://github.com/zigamilek/ai-agents-hub.git}"
REPO_REF="${REPO_REF:-master}"

msg_info "Updating package index"
$STD apt-get update
msg_ok "Updated package index"

msg_info "Installing dependencies"
$STD apt-get install -y git python3 python3-venv python3-pip rsync ca-certificates curl
msg_ok "Installed dependencies"

msg_info "Creating service user"
if ! id -u aihub >/dev/null 2>&1; then
  useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin aihub
fi
msg_ok "Service user ready"

msg_info "Preparing directories"
mkdir -p "${APP_DIR}" "${DATA_DIR}/memories" "${DATA_DIR}/obsidian" "${CONFIG_DIR}/prompts/specialists" /var/log/ai-agents-hub
msg_ok "Directories prepared"

msg_info "Cloning repository"
if [[ -d "${APP_DIR}/.git" ]]; then
  rm -rf "${APP_DIR}"
fi
git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" "${APP_DIR}"
msg_ok "Repository cloned"

msg_info "Building Python environment"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -e "${APP_DIR}"
msg_ok "Python environment ready"

msg_info "Installing configuration"
if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  cp "${APP_DIR}/config.yaml" "${CONFIG_DIR}/config.yaml"
fi
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
msg_ok "Configuration installed"

msg_info "Installing systemd service"
cp "${APP_DIR}/deploy/systemd/ai-agents-hub.service" "/etc/systemd/system/${SERVICE_NAME}.service"

msg_info "Applying ownership"
chown -R aihub:aihub "${APP_DIR}" "${DATA_DIR}" "${CONFIG_DIR}" /var/log/ai-agents-hub
msg_ok "Ownership applied"

systemctl daemon-reload
systemctl enable -q --now "${SERVICE_NAME}"
msg_ok "Service installed and started"

msg_ok "Installation complete"
msg_ok "Run 'ai-agents-hub onboard' to configure keys and settings."
