#!/bin/sh
# Install omenctl (fan governor) into /usr/local and enable the unit.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo ./install.sh" >&2; exit 1; }

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

install -Dm755 "$HERE/omenctl.py" /usr/local/bin/omenctl
install -Dm755 "$HERE/omenctl-run.sh" /usr/local/bin/omenctl-run.sh
install -Dm644 "$HERE/omenctl.service" /etc/systemd/system/omenctl.service

systemctl daemon-reload
systemctl enable --now omenctl.service

echo "omenctl installed: the fan governor now owns the setpoints."
echo "Stop it with: sudo systemctl stop omenctl   (fans return to the EC)"
