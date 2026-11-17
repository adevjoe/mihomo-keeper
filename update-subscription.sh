#!/usr/bin/env bash
# Download the mihomo subscription, merge override.yaml into it, validate with `mihomo -t`,
# back up the old config.yaml, replace it and restart the mihomo service.
#
# All settings come from environment variables (see README.md "配置"):
#   MIHOMO_SUB_URL        subscription URL (highest priority)
#   MIHOMO_SUB_URL_FILE   file whose first non-comment line is the URL (default: $MIHOMO_DIR/subscription.url)
#   MIHOMO_DIR            mihomo working directory              (default: /etc/mihomo)
#   MIHOMO_BIN            mihomo binary                         (default: /usr/local/bin/mihomo)
#   MIHOMO_SERVICE        systemd unit name of mihomo           (default: mihomo)
#   MERGE_SCRIPT          path to merge-config.py               (default: next to this script)
#   BACKUP_KEEP           number of config.yaml backups to keep (default: 20)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIHOMO_DIR="${MIHOMO_DIR:-/etc/mihomo}"
MIHOMO_BIN="${MIHOMO_BIN:-/usr/local/bin/mihomo}"
MIHOMO_SERVICE="${MIHOMO_SERVICE:-mihomo}"
SUB_URL_FILE="${MIHOMO_SUB_URL_FILE:-${MIHOMO_DIR}/subscription.url}"
CONFIG_FILE="${MIHOMO_DIR}/config.yaml"
OVERRIDE_FILE="${MIHOMO_DIR}/override.yaml"
MERGE_SCRIPT="${MERGE_SCRIPT:-${SCRIPT_DIR}/merge-config.py}"
BACKUP_DIR="${MIHOMO_DIR}/backup"
BACKUP_KEEP="${BACKUP_KEEP:-20}"
TMP_SUB_FILE="${MIHOMO_DIR}/config.yaml.sub.tmp"
TMP_CONFIG_FILE="${MIHOMO_DIR}/config.yaml.tmp"

if [ -t 1 ] && [ "${TERM:-dumb}" != 'dumb' ] && [ -z "${NO_COLOR:-}" ]; then
  COLOR_BLUE='\033[1;34m'
  COLOR_GREEN='\033[1;32m'
  COLOR_RED='\033[1;31m'
  COLOR_RESET='\033[0m'
else
  COLOR_BLUE=''
  COLOR_GREEN=''
  COLOR_RED=''
  COLOR_RESET=''
fi

log_step() {
  printf '%b[%s] %s%b\n' "$COLOR_BLUE" "$(date '+%F %T')" "$*" "$COLOR_RESET"
}

log_success() {
  printf '%b[%s] %s%b\n' "$COLOR_GREEN" "$(date '+%F %T')" "$*" "$COLOR_RESET"
}

log_error() {
  printf '%b[%s] %s%b\n' "$COLOR_RED" "$(date '+%F %T')" "$*" "$COLOR_RESET" >&2
}

# resolve subscription URL: env > file
if [ -n "${MIHOMO_SUB_URL:-}" ]; then
  SUB_URL="$MIHOMO_SUB_URL"
elif [ -s "$SUB_URL_FILE" ]; then
  SUB_URL="$(grep -v -E '^[[:space:]]*(#|$)' "$SUB_URL_FILE" | head -n 1 | tr -d '[:space:]')"
else
  SUB_URL=''
fi
if [ -z "$SUB_URL" ]; then
  log_error "subscription URL not configured: set MIHOMO_SUB_URL or write it to ${SUB_URL_FILE}"
  exit 2
fi

cleanup() {
  rm -f "$TMP_SUB_FILE" "$TMP_CONFIG_FILE"
}
trap cleanup EXIT

log_step 'start updating mihomo subscription'
log_step "download subscription to ${TMP_SUB_FILE}"
curl -fsSL "$SUB_URL" -o "$TMP_SUB_FILE"
chmod 600 "$TMP_SUB_FILE"

if [ -s "$OVERRIDE_FILE" ]; then
  log_step "merge local override ${OVERRIDE_FILE} into subscription"
  python3 "$MERGE_SCRIPT" "$TMP_SUB_FILE" "$OVERRIDE_FILE" "$TMP_CONFIG_FILE"
  chmod 600 "$TMP_CONFIG_FILE"
else
  log_step "no override file at ${OVERRIDE_FILE}, use subscription as-is"
  mv "$TMP_SUB_FILE" "$TMP_CONFIG_FILE"
fi

log_step 'validate merged config with mihomo -t'
"$MIHOMO_BIN" -t -d "$MIHOMO_DIR" -f "$TMP_CONFIG_FILE" >/dev/null

if [ -f "$CONFIG_FILE" ] && cmp -s "$TMP_CONFIG_FILE" "$CONFIG_FILE"; then
  log_success 'config unchanged, skip restart'
  exit 0
fi

if [ -f "$CONFIG_FILE" ]; then
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
  BACKUP_FILE="${BACKUP_DIR}/config.yaml.$(date '+%Y%m%d-%H%M%S')"
  log_step "backup ${CONFIG_FILE} -> ${BACKUP_FILE}"
  cp -p "$CONFIG_FILE" "$BACKUP_FILE"
  # keep only the newest $BACKUP_KEEP backups
  ls -1t "${BACKUP_DIR}"/config.yaml.* 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f
fi

log_step "replace ${CONFIG_FILE}"
mv "$TMP_CONFIG_FILE" "$CONFIG_FILE"

log_step "restart ${MIHOMO_SERVICE} service"
systemctl restart "$MIHOMO_SERVICE"

log_step "check ${MIHOMO_SERVICE} service status"
if systemctl is-active --quiet "$MIHOMO_SERVICE"; then
  log_success "${MIHOMO_SERVICE} service is active"
else
  log_error "${MIHOMO_SERVICE} service is not active"
  systemctl status "$MIHOMO_SERVICE" --no-pager
  exit 1
fi

log_step 'check mihomo controller /version'
mapfile -t controller_meta < <(python3 - "$CONFIG_FILE" <<'PY'
import sys
import yaml

cfg = yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}
external_controller = str(cfg.get('external-controller') or '127.0.0.1:9090')
secret = str(cfg.get('secret') or '')

host, port = external_controller.rsplit(':', 1)
if host in ('0.0.0.0', '*', '::', '[::]'):
    host = '127.0.0.1'
print(f'http://{host}:{port}/version')
print(secret)
PY
)
CONTROLLER_URL="${controller_meta[0]}"
CONTROLLER_SECRET="${controller_meta[1]:-}"
if [ -n "$CONTROLLER_SECRET" ]; then
  VERSION_JSON="$(curl -fsS -H "Authorization: Bearer ${CONTROLLER_SECRET}" "$CONTROLLER_URL")"
else
  VERSION_JSON="$(curl -fsS "$CONTROLLER_URL")"
fi
log_success "mihomo controller /version: ${VERSION_JSON}"

log_success 'mihomo subscription update finished'
