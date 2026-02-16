#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root inside the LXC container."
  exit 1
fi

APP_DIR="${APP_DIR:-/opt/ai-agents-hub}"
DATA_DIR="${DATA_DIR:-/var/lib/ai-agents-hub}"
CONFIG_DIR="${CONFIG_DIR:-/etc/ai-agents-hub}"
SERVICE_NAME="ai-agents-hub"

echo "[1/10] Installing OS packages..."
apt-get update
apt-get install -y python3 python3-venv python3-pip rsync ca-certificates curl

echo "[2/10] Creating service user..."
if ! id -u aihub >/dev/null 2>&1; then
  useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin aihub
fi

echo "[3/10] Preparing directories..."
mkdir -p "${APP_DIR}" "${DATA_DIR}/memories" "${DATA_DIR}/obsidian" "${CONFIG_DIR}/prompts/specialists" /var/log/ai-agents-hub

echo "[4/10] Syncing application files..."
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rsync -a --delete \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "__pycache__" \
  "${REPO_ROOT}/" "${APP_DIR}/"

echo "[5/10] Building virtual environment..."
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -e "${APP_DIR}"

echo "[6/10] Installing config and service unit..."
if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  cp "${APP_DIR}/config.yaml" "${CONFIG_DIR}/config.yaml"
fi
cp "${APP_DIR}/deploy/systemd/ai-agents-hub.service" "/etc/systemd/system/${SERVICE_NAME}.service"

echo "[7/10] Installing environment file..."
if [[ ! -f "${CONFIG_DIR}/ai-agents-hub.env" ]]; then
  cat > "${CONFIG_DIR}/ai-agents-hub.env" <<'EOF'
OPENAI_API_KEY=
GEMINI_API_KEY=
AI_AGENTS_HUB_API_KEY=change-me
EOF
fi
chmod 600 "${CONFIG_DIR}/ai-agents-hub.env"

echo "[8/10] Installing prompt files..."
for prompt_file in "${APP_DIR}/prompts/specialists/"*.md; do
  prompt_name="$(basename "${prompt_file}")"
  [[ -f "${CONFIG_DIR}/prompts/specialists/${prompt_name}" ]] || cp "${prompt_file}" "${CONFIG_DIR}/prompts/specialists/${prompt_name}"
done

echo "[9/10] Applying ownership..."
chown -R aihub:aihub "${APP_DIR}" "${DATA_DIR}" "${CONFIG_DIR}" /var/log/ai-agents-hub

echo "[10/10] Enabling service..."
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl --no-pager status "${SERVICE_NAME}" || true

cat <<'EOF'

Installation complete.

Next steps:
1) Run onboarding: ai-agents-hub onboard
2) Restart service: systemctl restart ai-agents-hub
3) Verify diagnostics:
   - curl http://<lxc-ip>:8080/healthz
   - curl http://<lxc-ip>:8080/readyz
   - curl http://<lxc-ip>:8080/diagnostics

EOF
