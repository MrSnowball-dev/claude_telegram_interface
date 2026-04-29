#!/bin/sh
# Install systemd units for tg-interface. Run as root.
set -eu

SRC="$(cd "$(dirname "$0")/systemd" && pwd)"
DST=/etc/systemd/system

cp "$SRC/tg-interface.service"        "$DST/"
cp "$SRC/tg-interface-watch.service"  "$DST/"
cp "$SRC/tg-interface-watch.path"     "$DST/"

systemctl daemon-reload
systemctl enable --now tg-interface.service
systemctl enable --now tg-interface-watch.path

echo "Installed. Logs: journalctl -u tg-interface -f"
