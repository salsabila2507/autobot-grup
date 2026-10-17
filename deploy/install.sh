#!/usr/bin/env bash
# HOTTI Engine installer. Creates dirs, venv, systemd units.
#
#   sudo ./deploy/install.sh            # install, do not start
#   sudo ./deploy/install.sh --start    # install and start all four services
#
# Ground rules respected here:
#  - secrets are only referenced by path, never written by this script
#  - no service is started before config.yaml exists and config loads
#  - one session per process: queue_bot uses the bot token, userbot its own StringSession

set -euo pipefail

APP_DIR="/home/ubuntu/bot-eg/hotti"
SECRETS_DIR="/home/ubuntu/.secrets"
VENV="${APP_DIR}/venv"
UNITS=(hotti-queue-bot hotti-userbot hotti-x-reader hotti-scheduler)

log() { printf '[install] %s\n' "$*"; }

log "app dir: ${APP_DIR}"

mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs" "${SECRETS_DIR}" "${SECRETS_DIR}/x_cookies"
chmod 700 "${SECRETS_DIR}"

if [ ! -f "${APP_DIR}/config.yaml" ]; then
  log "config.yaml missing, creating from config.example.yaml"
  cp "${APP_DIR}/config.example.yaml" "${APP_DIR}/config.yaml"
  log "EDIT ${APP_DIR}/config.yaml BEFORE STARTING SERVICES"
fi

if [ ! -x "${VENV}/bin/python" ]; then
  log "creating venv"
  python3 -m venv "${VENV}"
  "${VENV}/bin/python" -m pip install --upgrade pip
  "${VENV}/bin/python" -m pip install telethon httpx pyyaml pytest
fi

log "validating config"
HOTTI_CONFIG="${APP_DIR}/config.yaml" "${VENV}/bin/python" -c "
import sys; sys.path.insert(0, '${APP_DIR}')
from engine import config
cfg = config.load('${APP_DIR}/config.yaml')
print('config ok:', [g.key for g in cfg.groups], cfg.enforcement.mode)
if cfg.enforcement.mode != 'list_only':
    print('WARNING: enforcement mode is not list_only')
"

run_preflight() {
  log "running preflight"
  set +e
  HOTTI_CONFIG="${APP_DIR}/config.yaml" "${VENV}/bin/python" -m services.preflight
  local rc=$?
  set -e
  if [ "${rc}" -ne 0 ]; then
    log "PREFLIGHT FAILED (rc=${rc}), fix the FAIL rows above"
    return 1
  fi
  return 0
}

run_preflight || true

for unit in "${UNITS[@]}"; do
  install -m 644 "${APP_DIR}/deploy/${unit}.service" "/etc/systemd/system/${unit}.service"
  log "installed ${unit}.service"
done

systemctl daemon-reload

if [ "${1:-}" = "--start" ]; then
  run_preflight || { log "refusing to start with a failing preflight"; exit 1; }
  for unit in "${UNITS[@]}"; do
    systemctl enable "${unit}"
    systemctl restart "${unit}"
    log "started ${unit}"
  done
  systemctl --no-pager status "${UNITS[@]}" | head -30
else
  log "not starting services (pass --start to enable and start)"
fi

log "done"