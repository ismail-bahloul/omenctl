#!/bin/sh
# Remove omenctl. The stop path writes 255, so the fans return to the EC.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo ./uninstall.sh" >&2; exit 1; }

systemctl disable --now omenctl.service 2>/dev/null || true
rm -f /usr/local/bin/omenctl \
      /usr/local/bin/omenctl-run.sh \
      /etc/systemd/system/omenctl.service
systemctl daemon-reload

echo "omenctl removed. The fans are on the EC's own curve."
