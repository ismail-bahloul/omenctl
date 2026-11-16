#!/usr/bin/env python3
"""Failure-path tests for the fan daemon, on a simulated EC (no hardware, no root).

These are the paths the safety contract depends on but that cannot be provoked on
the real machine without risk: EC read failures, critical temperatures, EC write
failures, and the clean stop.

    python3 omenctl/test_failpaths.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("oc", os.path.join(HERE, "omenctl.py"))
oc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc)
oc.LOCK = tempfile.mktemp()
oc.FAN_OVERRIDE = tempfile.mktemp()
oc.sd_notify = lambda msg: None
time.sleep = lambda s: None


class FakeRig:
    """The 4 setpoint/readback bytes 0x2C..0x2F live in memory; writes stick."""
    insts = []
    script = None
    ticks = 0
    fail_writes = False

    def __init__(self, *a, **k):
        self.regs = bytearray([12, 12, 12, 12])
        self.writes = []
        self.n = 0
        self.hp = None
        FakeRig.insts.append(self)

    def ec_read(self, off, n=1):
        return bytes(self.regs[off - 0x2C:off - 0x2C + n])

    def ec_write(self, off, v):
        if FakeRig.fail_writes:
            raise OSError("EIO")
        self.writes.append((off, v))
        self.regs[off - 0x2C] = v
        if off - 0x2C + 2 < 4:
            self.regs[off - 0x2C + 2] = v        # readback follows the setpoint

    def temps(self):
        self.n += 1
        if self.n > FakeRig.ticks:
            oc.stop = True
        step = FakeRig.script(self.n)
        if step[0] == "readfail":
            raise OSError("EIO")
        return step[1], step[2], bytes(self.regs)

    def open(self):
        pass

    def close(self):
        pass


oc.Rig = FakeRig


def run(script, ticks, fail_writes=False):
    oc.stop = False
    FakeRig.script, FakeRig.ticks = script, ticks
    FakeRig.fail_writes, FakeRig.insts = fail_writes, []
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = oc.cmd_run(types.SimpleNamespace(gpu_offset=3.0, raw=True))
    daemon = FakeRig.insts[0]
    reset = FakeRig.insts[-1] if len(FakeRig.insts) > 1 else None
    return rc, daemon.writes, (reset.writes if reset else []), out.getvalue()


def run_manual(manual, script, ticks):
    """Daemon run with a manual-speed file already in place (e.g. {"cpu": 30})."""
    oc.overrides_write(manual)
    # cmd_run clears the file at startup, so re-create it on the first tick
    first = {"done": False}
    inner = script

    def with_file(n):
        if not first["done"]:
            first["done"] = True
            oc.overrides_write(manual)
        return inner(n)
    return run(with_file, ticks)


def check(name, cond, extra=""):
    print("%s  %s %s" % ("PASS" if cond else "FAIL", name, extra))
    return cond


def main():
    CPU, GPU = 0x2C, 0x2D
    ok = []
    rc, w, r, out = run(lambda n: ("readfail",) if n <= 4 else ("ok", 50, 50), 6)
    ok.append(check("3+ EC read failures force both fans to 100 %",
                    (CPU, 100) in w and (GPU, 100) in w))
    rc, w, r, out = run(lambda n: ("readfail",) if n in (2, 3) else ("ok", 50, 50), 6)
    ok.append(check("2 read failures (below the threshold) do not", all(v != 100 for _, v in w)))
    rc, w, r, out = run(lambda n: ("ok", 101, 60) if n >= 2 else ("ok", 50, 50), 4)
    ok.append(check("CPU at 101 C: both fans 100 %", (CPU, 100) in w and (GPU, 100) in w))
    rc, w, r, out = run(lambda n: ("ok", 60, 100) if n >= 2 else ("ok", 50, 50), 4)
    ok.append(check("GPU reading 100 C: both fans 100 %", (CPU, 100) in w and (GPU, 100) in w))
    rc, w, r, out = run(lambda n: ("ok", 75, 60), 20, fail_writes=True)
    ok.append(check("5 EC write failures: gives up with rc=1",
                    rc == 1 and "giving up" in out and out.count("write failed") == 5))
    rc, w, r, out = run(lambda n: ("readfail",), 8, fail_writes=True)
    ok.append(check("EC gone: failed fail-safe writes give up too",
                    rc == 1 and "giving up" in out
                    and out.count("fail-safe write failed") >= 5, str(w)))
    rc, w, r, out = run(lambda n: ("ok", 75, 60), 5)
    ok.append(check("steady 75/60 C: curve written once per fan, not every tick",
                    w == [(CPU, 28), (GPU, 16)], str(w)))
    ok.append(check("clean stop: rc=0 and the reset writes 255 to both",
                    rc == 0 and r == [(CPU, 255), (GPU, 255)], str(r)))
    # ---- manual fan speeds --------------------------------------------------
    rc, w, r, out = run_manual({"cpu": 30}, lambda n: ("ok", 60, 60), 4)
    ok.append(check("manual CPU 30 % below the curve (16 %): applied, GPU stays on the curve",
                    (CPU, 30) in w and (GPU, 16) in w and (CPU, 16) not in w, str(w)))
    rc, w, r, out = run_manual({"cpu": 20}, lambda n: ("ok", 85, 60), 4)
    ok.append(check("manual CPU 20 % is kept at 85 C (no auto-drop)",
                    (CPU, 20) in w and "dropped" not in out
                    and oc.overrides_read() == {CPU: 20}, str(w)))
    rc, w, r, out = run_manual({"cpu": 20, "gpu": 25}, lambda n: ("ok", 60, 85), 4)
    ok.append(check("manual GPU 25 % is kept at 85 C too",
                    (CPU, 20) in w and (GPU, 25) in w
                    and oc.overrides_read() == {CPU: 20, GPU: 25}, str(w)))
    rc, w, r, out = run_manual({"cpu": 12, "gpu": 12}, lambda n: ("ok", 101, 60), 4)
    ok.append(check("critical temperature beats any manual speed: both fans 100 %",
                    (CPU, 100) in w and (GPU, 100) in w and (CPU, 12) not in w))
    open(oc.FAN_OVERRIDE, "w").write("cpu=3\ngpu=500\n")
    ok.append(check("out-of-range values are clamped to 0..100",
                    oc.overrides_read() == {CPU: 3, GPU: 100}))
    rc, w, r, out = run_manual({"cpu": 0}, lambda n: ("ok", 60, 60), 4)
    ok.append(check("manual 0 %: a fan can be stopped while cool (60 C)", (CPU, 0) in w, str(w)))
    rc, w, r, out = run_manual({"cpu": 0}, lambda n: ("ok", 66, 60), 4)
    ok.append(check("manual 0 % is kept at 66 C (no low-speed limit)",
                    (CPU, 0) in w and "dropped" not in out
                    and oc.overrides_read() == {CPU: 0}, str(w)))
    rc, w, r, out = run_manual({"cpu": 12}, lambda n: ("ok", 90, 60), 4)
    ok.append(check("12 % is kept at 90 C as well",
                    "dropped" not in out and oc.overrides_read() == {CPU: 12}
                    and (CPU, 50) not in w, str(w)))
    oc.overrides_write({"cpu": 50})
    rc, w, r, out = run(lambda n: ("ok", 60, 60), 3)
    ok.append(check("a daemon start clears manual speeds (always begins on the curve)",
                    (CPU, 50) not in w and (CPU, 16) in w, str(w)))
    print("\n%d/%d passed" % (sum(ok), len(ok)))
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
