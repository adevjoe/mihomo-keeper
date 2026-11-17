#!/usr/bin/env bash
# mihomo-keeper installer (Linux + systemd, run as root).
#
#   sudo ./deploy/install.sh [--enable-timer] [--no-start] [--force-units]
#
# Environment:
#   MIHOMO_DIR      target directory (default /etc/mihomo)
#   MIHOMO_BIN      mihomo binary    (default /usr/local/bin/mihomo)
#   MIHOMO_SUB_URL  if set and $MIHOMO_DIR/subscription.url does not exist, it is written there (chmod 600)
#
# What it does (idempotent, never overwrites your config/override/secret):
#   1. copies manager.py / merge-config.py / update-subscription.sh into $MIHOMO_DIR
#   2. creates override.yaml from override.example.yaml if missing
#   3. creates manager.secret with a random password if missing (printed once)
#   4. installs /etc/default/mihomo-manager from manager.env.example if missing
#   5. installs systemd units (mihomo.service only if it does not exist yet, unless --force-units)
#   6. enables + starts mihomo-manager.service (skip with --no-start);
#      the 6h subscription timer is only enabled with --enable-timer
set -euo pipefail

ENABLE_TIMER=0
START=1
FORCE_UNITS=0
for a in "$@"; do
  case "$a" in
    --enable-timer) ENABLE_TIMER=1 ;;
    --no-start) START=0 ;;
    --force-units) FORCE_UNITS=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIHOMO_DIR="${MIHOMO_DIR:-/etc/mihomo}"
MIHOMO_BIN="${MIHOMO_BIN:-/usr/local/bin/mihomo}"
UNIT_DIR=/etc/systemd/system
ENV_FILE=/etc/default/mihomo-manager

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

[ "$(id -u)" = 0 ] || { echo "please run as root" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
if ! python3 -c 'import yaml' 2>/dev/null; then
  echo "PyYAML is missing. Install it first, e.g.: apt install python3-yaml  /  pip3 install -r requirements.txt" >&2
  exit 1
fi
command -v curl >/dev/null || warn "curl not found: subscription / kernel / geo downloads will fail"
[ -x "$MIHOMO_BIN" ] || warn "mihomo binary not found at $MIHOMO_BIN (install it, or set MIHOMO_BIN)"

say "install scripts into $MIHOMO_DIR"
install -d -m 755 "$MIHOMO_DIR"
install -m 700 "$REPO_DIR/manager.py" "$MIHOMO_DIR/manager.py"
install -m 700 "$REPO_DIR/merge-config.py" "$MIHOMO_DIR/merge-config.py"
install -m 700 "$REPO_DIR/update-subscription.sh" "$MIHOMO_DIR/update-subscription.sh"

if [ ! -e "$MIHOMO_DIR/override.yaml" ]; then
  say "create $MIHOMO_DIR/override.yaml from override.example.yaml"
  install -m 600 "$REPO_DIR/override.example.yaml" "$MIHOMO_DIR/override.yaml"
fi

if [ ! -e "$MIHOMO_DIR/subscription.url" ] && [ -n "${MIHOMO_SUB_URL:-}" ]; then
  say "write subscription URL to $MIHOMO_DIR/subscription.url"
  ( umask 077; printf '%s\n' "$MIHOMO_SUB_URL" > "$MIHOMO_DIR/subscription.url" )
fi

NEW_PASSWORD=''
if [ ! -e "$MIHOMO_DIR/manager.secret" ]; then
  NEW_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  ( umask 077; printf '%s\n' "$NEW_PASSWORD" > "$MIHOMO_DIR/manager.secret" )
fi

if [ ! -e "$ENV_FILE" ]; then
  say "install $ENV_FILE"
  install -m 600 "$REPO_DIR/deploy/manager.env.example" "$ENV_FILE"
fi

install_unit() {  # $1 = unit file name, $2 = 1 to overwrite existing
  local src="$REPO_DIR/deploy/$1" dst="$UNIT_DIR/$1"
  if [ -e "$dst" ] && [ "$2" != 1 ]; then
    say "keep existing $dst"
    return
  fi
  say "install $dst"
  sed -e "s#/etc/mihomo#${MIHOMO_DIR}#g" -e "s#/usr/local/bin/mihomo#${MIHOMO_BIN}#g" "$src" > "$dst"
  chmod 644 "$dst"
}
install_unit mihomo-manager.service 1
install_unit mihomo-sub-update.service 1
install_unit mihomo-sub-update.timer 1
install_unit mihomo.service "$FORCE_UNITS"
systemctl daemon-reload

if [ "$START" = 1 ]; then
  say "enable + (re)start mihomo-manager.service"
  systemctl enable mihomo-manager.service >/dev/null
  systemctl restart mihomo-manager.service
fi
if [ "$ENABLE_TIMER" = 1 ]; then
  say "enable mihomo-sub-update.timer (every 6h)"
  systemctl enable --now mihomo-sub-update.timer
fi

echo
say "done."
[ -e "$MIHOMO_DIR/subscription.url" ] || [ -n "${MIHOMO_SUB_URL:-}" ] || \
  warn "no subscription URL yet: write it to $MIHOMO_DIR/subscription.url (chmod 600)"
if [ -n "$NEW_PASSWORD" ]; then
  echo "    web manager user: admin"
  echo "    web manager password (saved in $MIHOMO_DIR/manager.secret): $NEW_PASSWORD"
fi
echo "    listen address: see MANAGER_LISTEN in $ENV_FILE (default 0.0.0.0:9092)"
