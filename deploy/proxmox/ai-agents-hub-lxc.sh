#!/usr/bin/env bash
set -Eeuo pipefail

APP="AI Agents Hub"

CTID="${CTID:-}"
CT_HOSTNAME="${CT_HOSTNAME:-ai-agents-hub}"
CORES="${CORES:-2}"
MEMORY="${MEMORY:-4096}"
SWAP="${SWAP:-512}"
DISK="${DISK:-8}"
UNPRIVILEGED="${UNPRIVILEGED:-1}"
ONBOOT="${ONBOOT:-1}"
OS_VERSION="${OS_VERSION:-12}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
ROOTFS_STORAGE="${ROOTFS_STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
NET0="${NET0:-name=eth0,bridge=${BRIDGE},ip=dhcp}"
FEATURES="${FEATURES:-nesting=1,keyctl=1}"
REPO_URL="${REPO_URL:-https://github.com/zigamilek/ai-agents-hub.git}"
REPO_REF="${REPO_REF:-}"
REUSE_EXISTING="${REUSE_EXISTING:-0}"

info() { echo -e "[INFO] $*"; }
warn() { echo -e "[WARN] $*"; }
error() { echo -e "[ERROR] $*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || error "Required command not found: $1"
}

ensure_ctid() {
  if [[ -n "${CTID}" ]]; then
    return
  fi
  if command -v pvesh >/dev/null 2>&1; then
    CTID="$(pvesh get /cluster/nextid)"
  fi
  [[ -n "${CTID}" ]] || error "Could not determine CTID automatically. Set CTID=<id>."
}

template_name() {
  pveam available | awk '{print $2}' | grep -E "debian-${OS_VERSION}-standard.*amd64.tar.zst" | sort -V | tail -n1
}

template_exists_locally() {
  local template="$1"
  pveam list "${TEMPLATE_STORAGE}" | awk '{print $2}' | grep -Fxq "${template}"
}

main() {
  need_cmd pct
  need_cmd pveam
  need_cmd awk
  need_cmd grep
  need_cmd sort
  need_cmd tail

  ensure_ctid

  ct_exists=0
  if pct status "${CTID}" >/dev/null 2>&1; then
    ct_exists=1
  fi

  if [[ "${ct_exists}" -eq 1 && "${REUSE_EXISTING}" != "1" ]]; then
    error "CTID ${CTID} already exists. Set a different CTID or run with REUSE_EXISTING=1."
  fi

  info "${APP} one-liner installer starting..."
  info "CTID=${CTID} HOSTNAME=${CT_HOSTNAME} CORES=${CORES} MEMORY=${MEMORY} DISK=${DISK}G"

  if [[ "${ct_exists}" -eq 0 ]]; then
    info "Refreshing LXC templates..."
    pveam update >/dev/null

    TEMPLATE="$(template_name)"
    [[ -n "${TEMPLATE}" ]] || error "No matching Debian ${OS_VERSION} template found."
    info "Using template: ${TEMPLATE}"

    if ! template_exists_locally "${TEMPLATE}"; then
      info "Downloading template to storage '${TEMPLATE_STORAGE}'..."
      pveam download "${TEMPLATE_STORAGE}" "${TEMPLATE}"
    else
      info "Template already present in '${TEMPLATE_STORAGE}'."
    fi

    info "Creating container ${CTID}..."
    pct create "${CTID}" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
      --hostname "${CT_HOSTNAME}" \
      --cores "${CORES}" \
      --memory "${MEMORY}" \
      --swap "${SWAP}" \
      --rootfs "${ROOTFS_STORAGE}:${DISK}" \
      --net0 "${NET0}" \
      --unprivileged "${UNPRIVILEGED}" \
      --features "${FEATURES}" \
      --onboot "${ONBOOT}"
  else
    info "Reusing existing container ${CTID}."
    pct set "${CTID}" --hostname "${CT_HOSTNAME}" >/dev/null 2>&1 || true
  fi

  info "Starting container ${CTID}..."
  pct start "${CTID}" >/dev/null 2>&1 || true
  sleep 4

  info "Installing git/curl inside container..."
  pct exec "${CTID}" -- bash -lc "apt-get update && apt-get install -y git ca-certificates curl"

  info "Cloning repository inside container..."
  if [[ -n "${REPO_REF}" ]]; then
    info "Using repository ref: ${REPO_REF}"
    pct exec "${CTID}" -- bash -lc "rm -rf /root/ai-agents-hub && git clone --depth 1 --branch '${REPO_REF}' '${REPO_URL}' /root/ai-agents-hub"
  else
    info "Using repository default branch (remote HEAD)."
    pct exec "${CTID}" -- bash -lc "rm -rf /root/ai-agents-hub && git clone --depth 1 '${REPO_URL}' /root/ai-agents-hub"
  fi

  info "Running LXC installer..."
  pct exec "${CTID}" -- bash -lc "cd /root/ai-agents-hub && chmod +x deploy/install_lxc.sh && ./deploy/install_lxc.sh"

  local ct_ip
  ct_ip="$(pct exec "${CTID}" -- bash -lc "hostname -I | awk '{print \$1}'" | tr -d '\r\n' || true)"

  echo
  info "${APP} installation complete."
  echo "------------------------------------------------------------"
  echo "Container ID      : ${CTID}"
  echo "Hostname          : ${CT_HOSTNAME}"
  echo "Container IP      : ${ct_ip:-<discover with: pct exec ${CTID} -- hostname -I>}"
  echo "Config file       : /etc/ai-agents-hub/config.yaml"
  echo "Env file          : /etc/ai-agents-hub/ai-agents-hub.env"
  echo "Prompt files      : /opt/ai-agents-hub/prompts/specialists/*.md"
  echo "Service           : systemctl status ai-agents-hub --no-pager"
  echo "Diagnostics       : curl http://<ct-ip>:8080/diagnostics"
  echo "Open WebUI URL    : http://<ct-ip>:8080/v1"
  echo "------------------------------------------------------------"
  echo
  echo "Next required step:"
  echo "  1) pct exec ${CTID} -- nano /etc/ai-agents-hub/ai-agents-hub.env"
  echo "  2) pct exec ${CTID} -- systemctl restart ai-agents-hub"
}

main "$@"
