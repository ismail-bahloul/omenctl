#!/usr/bin/env python3
"""Headless tests for the profile read-back -- no hardware, no root.

Covers the unit conversion (mW vs degrees C) and the verify logic: a matching
state must produce no problem, a moved field must name it, and boost off must
accept the base-clock clamp. Run with:

    python3 omenctl/test_profile.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("oc", os.path.join(HERE, "omenctl.py"))
oc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc)

CPU0 = "/sys/devices/system/cpu/cpu0/cpufreq/"


def expected_test():
    want = oc.expected_limits(oc.PROFILES["ac"])
    assert want["STAPM LIMIT"] == 28.0, want
    assert want["PPT LIMIT FAST"] == 36.0, want
    assert want["PPT LIMIT SLOW"] == 28.0, want
    assert want["PPT LIMIT APU"] == 22.0, want
    assert want["THM LIMIT CORE"] == 85.0, want

    perf = oc.expected_limits(oc.PROFILES["perf"])
    assert perf["STAPM LIMIT"] == 54.0, perf
    assert perf["THM LIMIT CORE"] == 90.0, perf   # degrees C, not 0.090
    print("PASS  ryzenadj -> PM table units (W and degrees C)")


def verify_test():
    old = (oc.pm_limit_values, oc.sysfs, oc.read_int,
           oc.igpu_dpm_path, oc.wifi_ps_state)
    limits = {"STAPM LIMIT": 28.0, "PPT LIMIT FAST": 36.0,
              "PPT LIMIT SLOW": 28.0, "PPT LIMIT APU": 22.0,
              "THM LIMIT CORE": 85.0}
    state = {CPU0 + "scaling_governor": "powersave",
             CPU0 + "energy_performance_preference": "balance_power",
             oc.BOOST: "1", "/dpm": "auto"}
    ints = {CPU0 + "scaling_max_freq": 4465000,
            CPU0 + "cpuinfo_max_freq": 4465000}

    oc.pm_limit_values = lambda: dict(limits)
    oc.sysfs = lambda path, default=None: state.get(path, default)
    oc.read_int = lambda path: ints.get(path)
    oc.igpu_dpm_path = lambda: "/dpm"
    oc.wifi_ps_state = lambda iface: "off"
    try:
        assert oc.verify_profile("ac", True, "wlan0") == []

        # boost off: the kernel clamps max_freq to the base clock; accepted.
        state[oc.BOOST] = "0"
        ints[CPU0 + "scaling_max_freq"] = 3201000
        ints[CPU0 + "cpuinfo_max_freq"] = 3201000
        assert oc.verify_profile("ac", False, "wlan0") == []

        # but with boost on, the same 3.2 GHz is a real mismatch.
        assert any("max_freq" in p for p in oc.verify_profile("ac", True, "wlan0"))

        limits["STAPM LIMIT"] = 15.0
        assert any("STAPM" in p for p in oc.verify_profile("ac", True, "wlan0"))

        limits["STAPM LIMIT"] = 28.0
        oc.wifi_ps_state = lambda iface: "on"
        assert any("wifi_ps" in p for p in oc.verify_profile("ac", True, "wlan0"))
    finally:
        oc.pm_limit_values, oc.sysfs, oc.read_int, \
            oc.igpu_dpm_path, oc.wifi_ps_state = old
    print("PASS  verify_profile: match, boost-off clamp, moved limit, WiFi")


def target_test():
    old_mode, old_source = oc.read_mode, oc.source_profile
    oc.read_mode = lambda: "eco"
    oc.source_profile = lambda: "ac"
    try:
        assert oc.target_pm_limits() == (15.0, 18.0, 15.0, 65.0), oc.target_pm_limits()
        oc.read_mode = lambda: "auto"
        assert oc.target_pm_limits() == (28.0, 36.0, 28.0, 85.0), oc.target_pm_limits()
    finally:
        oc.read_mode, oc.source_profile = old_mode, old_source
    print("PASS  watcher target limits follow the stored mode")


def stored_mode_test():
    """A failed apply must not debounce the next attempt; a skip must."""
    old = (oc.APPLY_LOCK, oc.LAST_APPLY, oc.BOOT_SEEN, oc.apply_mode, oc.read_mode)
    tmp = tempfile.mkdtemp()
    oc.APPLY_LOCK = os.path.join(tmp, "lock")
    oc.LAST_APPLY = os.path.join(tmp, "last")
    oc.BOOT_SEEN = os.path.join(tmp, "boot")
    open(oc.BOOT_SEEN, "w").close()
    oc.read_mode = lambda: "auto"
    calls = []
    oc.apply_mode = lambda mode, ops: calls.append(mode) or False
    try:
        assert oc.apply_stored_mode(oc.Ops(True), quiet_if_recent=1.0) is False
        assert not os.path.exists(oc.LAST_APPLY), "a failed apply must not debounce"

        oc.apply_mode = lambda mode, ops: True
        assert oc.apply_stored_mode(oc.Ops(True), quiet_if_recent=1.0) is True
        assert os.path.exists(oc.LAST_APPLY)

        assert oc.apply_stored_mode(oc.Ops(True), quiet_if_recent=1.0) is None
        assert calls == ["auto"], calls
    finally:
        oc.APPLY_LOCK, oc.LAST_APPLY, oc.BOOT_SEEN, \
            oc.apply_mode, oc.read_mode = old
        shutil.rmtree(tmp)
    print("PASS  failed apply does not debounce; a recent success does")


def main():
    expected_test()
    verify_test()
    target_test()
    stored_mode_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
