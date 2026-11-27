#!/usr/bin/env bash
# Install or update ContentPilot's systemd services on the VM (same layout as PostPilot:
# a dedicated app user owns the code, systemd runs it). Run as an admin with sudo:
#   sudo bash /home/contentpilot/ContentPilot/deploy/install.sh            # packages + units, (re)start
#   sudo bash /home/contentpilot/ContentPilot/deploy/install.sh --no-start # leave services stopped
# The app user is whoever owns the repo folder (contentpilot). PostPilot is not touched.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash $0 $*"; exit 1; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(stat -c %U "$REPO")"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
[ "$APP_USER" != "root" ] || { echo "The repo must be owned by the app user (e.g. contentpilot), not root."; exit 1; }
as_app() { sudo -u "$APP_USER" -H "$@"; }

as_app python3 -c 'import sys; assert sys.version_info >= (3, 10), "ContentPilot needs Python 3.10+"'
[ -x "$REPO/venv/bin/python" ] || as_app python3 -m venv "$REPO/venv"
as_app "$REPO/venv/bin/pip" install --quiet --upgrade pip
as_app "$REPO/venv/bin/pip" install --quiet -r "$REPO/requirements-vm.txt"

if [ ! -f "$REPO/deploy/contentpilot.env" ]; then
  as_app cp "$REPO/deploy/contentpilot.env.example" "$REPO/deploy/contentpilot.env"
  echo "Created deploy/contentpilot.env from the example."
fi
as_app mkdir -p "$REPO/data" "$APP_HOME/contentpilot-backups"
if [ -f "$REPO/.env" ]; then chmod 600 "$REPO/.env"; else echo "NOTE: $REPO/.env is missing (deploy/README-vm.md, data move)."; fi

for unit in "$REPO"/deploy/systemd/*.service "$REPO"/deploy/systemd/*.timer; do
  sed -e "s|@USER@|$APP_USER|g" -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$APP_HOME|g" "$unit" \
    > "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
systemctl enable --quiet contentpilot-web contentpilot-api contentpilot-worker contentpilot-backup.timer
systemctl start contentpilot-backup.timer

if [ "${1:-}" = "--no-start" ]; then
  echo "Installed. Services enabled, not started."
else
  systemctl restart contentpilot-web contentpilot-api contentpilot-worker
  sleep 4
  systemctl --no-pager --lines=0 status contentpilot-web contentpilot-api contentpilot-worker | grep -E "●|Active:"
fi
