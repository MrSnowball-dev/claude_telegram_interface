#!/bin/sh
# Install the systemd unit for tg-interface. Run as root.
#
# This installs ONLY the bot service. The optional file-watch + auto-restart
# units (tg-interface-watch.path / .service) are left in deploy/systemd/ so
# you can opt into them manually:
#
#   cp deploy/systemd/tg-interface-watch.* /etc/systemd/system/
#   systemctl daemon-reload
#   systemctl enable --now tg-interface-watch.path
#
# Auto-restart turned out to interact poorly with mid-output service replacement
# (lost streams, rate-limited APIs on each restart). Manual `systemctl restart
# tg-interface` is the safer default.
set -eu

SRC="$(cd "$(dirname "$0")/systemd" && pwd)"
DST=/etc/systemd/system

cp "$SRC/tg-interface.service" "$DST/"
systemctl daemon-reload
systemctl enable --now tg-interface.service

echo "Installed. Logs: journalctl -u tg-interface -f"
