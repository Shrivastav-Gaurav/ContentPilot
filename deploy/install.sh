#!/usr/bin/env bash
# Install or update ContentPilot's systemd services on the VM. Run from anywhere:
#   bash deploy/install.sh            # venv + packages + units, then (re)start the services
#   bash deploy/install.sh --no-start # same, but leave the services stopped (first deploy, before data is copied)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AS="$(id -un)"
cd "$REPO"

python3 -c 'import sys; assert sys.version_info >= (3, 10), "ContentPilot needs Python 3.10+"' 

if [ ! -x venv/bin/python ]; then
  python3 -m venv venv
fi
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements-vm.txt

if [ ! -f deploy/contentpilot.env ]; then
  cp deploy/contentpilot.env.example deploy/contentpilot.env
  echo "Created deploy/contentpilot.env from the example (edit ports there if needed)."
fi
mkdir -p data "$HOME/contentpilot-backups"
[ -f .env ] || echo "NOTE: $REPO/.env is missing; copy it before starting (deploy/README-vm.md, step 7)."

for unit in deploy/systemd/*.service deploy/systemd/*.timer; do
  sed -e "s|@USER@|$RUN_AS|g" -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME|g" "$unit" \
    | sudo tee "/etc/systemd/system/$(basename "$unit")" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --quiet contentpilot-web contentpilot-api contentpilot-worker contentpilot-backup.timer
sudo systemctl start contentpilot-backup.timer

if [ "${1:-}" = "--no-start" ]; then
  echo "Installed. Services enabled but not started."
else
  sudo systemctl restart contentpilot-web contentpilot-api contentpilot-worker
  sleep 3
  systemctl --no-pager --lines=0 status contentpilot-web contentpilot-api contentpilot-worker | grep -E "●|Active:"
fi
