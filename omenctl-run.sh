#!/bin/sh
# Supervisor for `omenctl run`. The safety contract has two layers:
#
#   * this shell -- if the daemon exits for ANY reason on its own (exception,
#     crash, SIGKILL of the child, OOM), release the fan latch and hand
#     control back to the EC;
#   * the unit's ExecStopPost -- the same cleanup for stop-initiated deaths
#     (clean stop, watchdog kill), where this shell dies with the cgroup.
#
# Found by the kill -9 test (2026-10-02): ExecStopPost alone does NOT run when
# the main process dies by itself -- systemd only executes it for stops that
# systemd itself initiated. This wrapper closes that hole.
#
# Nothing else loads ec_sys at boot, so we do. The module needs write_support=1
# or the EC io file is read-only.
set -u
if [ ! -d /sys/module/ec_sys ]; then
    /usr/bin/modprobe ec_sys write_support=1 2>/dev/null || \
        /usr/bin/modprobe ec_sys 2>/dev/null || true
fi

/usr/bin/python3 /usr/local/bin/omenctl run
rc=$?
/usr/bin/python3 /usr/local/bin/omenctl reset --raw
exit $rc
