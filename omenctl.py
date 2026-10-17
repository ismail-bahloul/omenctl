#!/usr/bin/env python3
"""omenctl -- fans and power for the HP OMEN 15.

Subcommands:

    start | stop | restart | enable | disable   the fan daemon (stop = the EC
             takes the fans back; enable/disable = at boot, and now)
    watch    re-apply the stored mode when the firmware clobbers the limits
             (run by omenctl-watch.service).
    run      the daemon itself, as run by the systemd unit: owns both fan
             setpoints, 1 Hz, under the safety contract -- 255 on every exit
             path, watchdog.
    reset    alias of stop (`reset --raw`: only write the 255s; the unit's
             stop path and the wrapper use that).
    status   fans + power state: temps, setpoints, profile, limits (-w: live).
    profile  apply a CPU/GPU power profile (ac|battery|perf|quiet|eco|auto|guard).
    boost    CPU boost on|off (persistent; off caps at the 3.2 GHz base clock).

(`shadow.py`, next to this file, is the read-only P0 parity check.)

Safety contract, from the measurements in ../ec-map.md:

    * the setpoint bytes are a latch -- only an explicit 255 releases them to
      the EC, and a killed writer leaves them frozen (measured 2026-10-02);
    * therefore the unit's ExecStopPost ALWAYS runs `reset --raw`, and this daemon
      pings systemd's watchdog every tick. The fallback is the EC's own curve
      (255): steeper than ours -- ~50 % at 65 C -- so louder, never unsafe;
    * a failed EC read fails SAFE: both fans to 100 %;
    * a failed EC write for 5 ticks exits, so the unit's stop path runs.

The curves live in FANS below (ported from nbfc's `my-nbfc` config, parity
verified by shadow.py). CPU temperature = k10temp; GPU = EC 0x49 + --gpu-offset
(NVML API reads ~3 C hotter on driver 615; calibrated in the P0 load campaign).

EC traffic: every byte read through ec_sys is one ACPI-EC transaction (~170 us,
under the EC lock shared with the kernel's ACPI). The loop therefore reads only
the 4 setpoint/readback bytes + the GPU temp (~1 ms/tick) instead of the whole
256-byte image (~44 ms/tick, measured 2026-10-03), through descriptors opened
once.
"""
import argparse
import fcntl
import glob
import os
import signal
import socket
import struct
import subprocess
import sys
import syslog
import time

ECIO = "/sys/kernel/debug/ec/ec0/io"
HWMON = "/sys/class/hwmon"
LOCK = "/run/omenctl.lock"      # held by the one running daemon: one writer only

CPU_TEMP, GPU_TEMP = 0x57, 0x49
SET_CPU, SET_GPU = 0x2C, 0x2D          # setpoints; readbacks sit at +2
RESET_VALUE = 255          # releases the latch back to the EC
CRITICAL_TEMP = 100.0      # both fans to 100 % at or above this
FAN_OVERRIDE = "/run/omenctl/fans"   # manual speeds, "cpu=40" / "gpu=auto" lines
OVERRIDE_MIN = 0           # % -- a fan may be stopped by hand...
LOW_SPEED = 12             # ...but below this (the curve's lowest step) it is only
LOW_SPEED_MAX_TEMP = 65.0  # trusted up to this temperature (C)
OVERRIDE_MAX_TEMP = 80.0   # any manual speed is dropped for a fan this hot (C)
FAN_KEYS = {"cpu": SET_CPU, "gpu": SET_GPU}

stop = False


def on_signal(_sig, _frm):
    global stop
    stop = True


def sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.send(msg.encode())
    except OSError:
        pass


def hwmon_dir(name):
    for d in os.listdir(HWMON):
        try:
            with open("%s/%s/name" % (HWMON, d)) as f:
                if f.read().strip() == name:
                    return "%s/%s" % (HWMON, d)
        except OSError:
            continue
    return None


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


# The fan curves. Each step is (up, down, speed %): move up to this step when
# temp >= up, back down past it when temp <= down (nbfc's hysteresis model).
# The 12 % first step with down=0 avoids the 0<->12 % yo-yo around 48-50 C and the fan never stops once spun up.
FANS = [
    {"name": "CPU fan", "write_reg": SET_CPU, "speed_max": 100,
     "thr": [(50, 0, 12), (60, 49, 16), (65, 58, 20), (69, 63, 24), (74, 67, 28),
             (79, 71, 35), (83, 76, 40), (87, 80, 50), (91, 85, 65), (94, 89, 80),
             (100, 92, 100)]},
    {"name": "GPU fan", "write_reg": SET_GPU, "speed_max": 100,
     "thr": [(50, 0, 12), (60, 49, 16), (65, 58, 22), (69, 63, 26), (74, 67, 28),
             (79, 71, 35), (83, 76, 45), (87, 80, 55), (91, 85, 65), (94, 89, 80),
             (100, 92, 100)]},
]


def overrides_read():
    """Manual fan speeds as {SET_CPU|SET_GPU: percent}; absent = follow the curve.
    A text file, not JSON: the daemon reads it every tick and must not pay for an
    import. Written by `omenctl fan` and the TUI (root only: /run/omenctl)."""
    out = {}
    try:
        with open(FAN_OVERRIDE) as f:
            for line in f:
                key, _, val = line.strip().partition("=")
                if key in FAN_KEYS and val.isdigit():
                    out[FAN_KEYS[key]] = max(OVERRIDE_MIN, min(100, int(val)))
    except OSError:
        pass
    return out


def overrides_write(manual):
    """`manual`: {"cpu": 40, "gpu": None}; None/absent = auto. Atomic replace."""
    os.makedirs(os.path.dirname(FAN_OVERRIDE), exist_ok=True)
    tmp = FAN_OVERRIDE + ".tmp"
    with open(tmp, "w") as f:
        for key in FAN_KEYS:
            v = manual.get(key)
            f.write("%s=%s\n" % (key, "auto" if v is None else int(v)))
    os.replace(tmp, FAN_OVERRIDE)


def initial_index(thr, speed):
    for i, t in enumerate(thr):
        if t[2] == speed:
            return i
    return min(range(len(thr)), key=lambda i: abs(thr[i][2] - speed))


def step(thr, temp, idx):
    """One tick of the hysteresis, from the threshold table alone."""
    while idx + 1 < len(thr) and temp >= thr[idx + 1][0]:
        idx += 1
    while idx > 0 and temp <= thr[idx][1]:
        idx -= 1
    return idx


class Rig:
    """The sensors and the EC, with descriptors held open across ticks."""

    def __init__(self, gpu_offset, writable=False):
        self.gpu_offset = gpu_offset
        self.hp = hwmon_dir("hp")
        k10 = hwmon_dir("k10temp")
        self.k10_path = k10 + "/temp1_input" if k10 else None
        self.ec_flags = os.O_RDWR if writable else os.O_RDONLY
        self.ec = self.k10 = None
        self.open()

    def open(self):
        self.close()
        self.ec = os.open(ECIO, self.ec_flags)
        if self.k10_path:
            try:
                self.k10 = os.open(self.k10_path, os.O_RDONLY)
            except OSError:
                self.k10 = None

    def close(self):
        for attr in ("ec", "k10"):
            fd = getattr(self, attr)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)

    def ec_read(self, off, n=1):
        if self.ec is None:
            raise OSError("EC not open")
        return os.pread(self.ec, n, off)

    def ec_write(self, off, value):
        if self.ec is None:
            raise OSError("EC not open")
        os.pwrite(self.ec, bytes([value]), off)

    def temps(self):
        """-> (cpu C, gpu C, setpoint/readback block 0x2C..0x2F). Raises OSError."""
        cpu = None
        if self.k10 is not None:
            try:
                cpu = int(os.pread(self.k10, 16, 0)) / 1000.0
            except (OSError, ValueError):
                pass
        if cpu is None:
            cpu = float(self.ec_read(CPU_TEMP)[0])
        regs = self.ec_read(SET_CPU, 4)
        gpu = self.ec_read(GPU_TEMP)[0] + self.gpu_offset
        if len(regs) != 4:
            raise OSError("short EC read")
        return cpu, gpu, regs


def fan_temp(fan, cpu, gpu):
    return cpu if fan["write_reg"] == SET_CPU else gpu


# ---------------------------------------------------------------- commands --

def cmd_reset(args):
    """Hand the fans to the EC. By hand this must STICK, so a running daemon is
    stopped first (it would rewrite the setpoints within a second); the unit's
    stop path and the wrapper use --raw, which only writes the 255s."""
    if not getattr(args, "raw", False) and subprocess.run(
            ["systemctl", "is-active", "--quiet", "omenctl"]).returncode == 0:
        rc = subprocess.run(["systemctl", "stop", "omenctl"]).returncode
        print("stopped omenctl: fans are on the EC's own curve "
              "(systemctl start omenctl to take them back)")
        return rc
    ok = True
    try:
        rig = Rig(0, writable=True)
    except OSError as e:
        print("reset: cannot open EC: %s" % e)
        return 1
    for reg in (SET_CPU, SET_GPU):
        try:
            rig.ec_write(reg, RESET_VALUE)
            back = rig.ec_read(reg)[0]
            print("reset: 0x%02X -> %d (readback %d)" % (reg, RESET_VALUE, back))
            ok = ok and back == RESET_VALUE
        except OSError as e:
            print("reset: 0x%02X FAILED: %s" % (reg, e))
            ok = False
    rig.close()
    return 0 if ok else 1


SPARK = "▁▂▃▄▅▆▇█"


def _color(on):
    def paint(code, text):
        return "\033[%sm%s\033[0m" % (code, text) if on else text
    return paint


# Semantic colours from the 256-colour cube (>= 16), so a terminal theme cannot
# remap them the way it remaps ANSI 30-37. The text markers (the `!` on a hot
# die, the TUI legend) carry the same meaning without colour.
GREEN, YELLOW, ORANGE, RED, BLUE, PURPLE = (
    "38;5;114", "38;5;221", "38;5;214", "38;5;203", "38;5;39", "38;5;111")


def _hex_256(h):
    """Nearest xterm-256 index (16-255) for a #rrggbb colour."""
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    steps = (0, 95, 135, 175, 215, 255)
    best = (10 ** 9, 16)
    for ri, rv in enumerate(steps):
        for gi, gv in enumerate(steps):
            for bi, bv in enumerate(steps):
                d = (r - rv) ** 2 + (g - gv) ** 2 + (b - bv) ** 2
                if d < best[0]:
                    best = (d, 16 + 36 * ri + 6 * gi + bi)
    gv = max(0, min(23, round(((r + g + b) / 3.0 - 8) / 10.0)))
    gray = 8 + 10 * gv
    d = (r - gray) ** 2 + (g - gray) ** 2 + (b - gray) ** 2
    if d < best[0]:
        best = (d, 232 + gv)
    return best[1]


def _mix_hex(a, b, t):
    """Blend two #rrggbb colours, t=0 -> a, t=1 -> b."""
    a, b = a.lstrip("#"), b.lstrip("#")
    av = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    bv = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(av, bv))


def _theme_path():
    """The KDE colour scheme Material You generated, or None."""
    env = os.environ.get("OMENCTL_THEME")
    if env:
        return None if env.lower() in ("none", "off", "0") else env
    home = os.path.expanduser("~")
    user = os.environ.get("SUDO_USER")
    if user:
        try:
            import pwd
            home = pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    import configparser
    name = None
    try:
        c = configparser.ConfigParser()
        c.read(os.path.join(home, ".config", "kdeglobals"))
        if c.has_section("General") and c.has_option("General", "ColorScheme"):
            name = c.get("General", "ColorScheme")
    except (OSError, configparser.Error):
        pass
    candidates = []
    if name:
        candidates.append(os.path.join(home, ".local", "share", "color-schemes",
                                       name + ".colors"))
    candidates.append(os.path.join(home, ".local", "share", "color-schemes",
                                   "MaterialYouDark.colors"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def load_theme():
    """Use the KDE Material You semantic roles for the TUI colours.

    The Konsole 16-colour scheme is all accent hues (its Color1 is blue, not
    red), so severity must come from the KDE roles: Positive is the green,
    Neutral the orange, Negative the red/pink, Link the accent blue and
    DecorationFocus the purple. Missing roles keep the fixed palette.
    """
    global GREEN, YELLOW, ORANGE, RED, BLUE, PURPLE
    path = _theme_path()
    if not path:
        return
    import configparser
    c = configparser.ConfigParser()
    try:
        c.read(path)
    except (OSError, configparser.Error):
        return

    def role(key):
        for section in ("Colors:View", "Colors:Button", "Colors:Window"):
            if c.has_section(section) and c.has_option(section, key):
                return c.get(section, key)
        return None

    pos = role("ForegroundPositive")
    neu = role("ForegroundNeutral")
    neg = role("ForegroundNegative")
    link = role("ForegroundLink")
    focus = role("DecorationFocus")
    if pos:
        GREEN = "38;5;%d" % _hex_256(pos)
    if neu:
        ORANGE = "38;5;%d" % _hex_256(neu)
    if neg:
        RED = "38;5;%d" % _hex_256(neg)
    if link:
        BLUE = "38;5;%d" % _hex_256(link)
    if focus:
        PURPLE = "38;5;%d" % _hex_256(focus)
    if pos and neu:
        YELLOW = "38;5;%d" % _hex_256(_mix_hex(pos, neu, 0.5))


def _temp_code(t):
    return GREEN if t < 60 else YELLOW if t < 75 else ORANGE if t < 85 else RED


def _plain_paint(code, text):
    """NO_COLOR / --no-color: same text, no ANSI (the TUI still uses attributes)."""
    return text


def _bar(frac, width, code, paint):
    """A gauge `width` cells wide, filled in eighths of a cell."""
    eighths = max(0, min(width * 8, round(frac * width * 8)))
    full, part = eighths // 8, eighths % 8
    fill = "█" * full + (" ▏▎▍▌▋▊▉"[part] if part else "")
    return paint(code, fill) + paint("90", "░" * (width - len(fill)))


def _spark(hist, paint):
    return "".join(paint(_temp_code(t), SPARK[max(0, min(7, int((t - 30) / 70 * 8)))])
                   for t in hist)


def sysfs(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def pm_read(nbytes=0x44):
    """The head of the SMU PM table (one read, no SMU command), or None."""
    try:
        with open(PM_TABLE, "rb") as f:
            raw = f.read(nbytes)
    except OSError:
        return None
    return raw if len(raw) >= nbytes else None


PM_LIMITS = {"STAPM LIMIT": 0x00, "PPT LIMIT FAST": 0x08, "PPT LIMIT SLOW": 0x10,
             "PPT LIMIT APU": 0x18, "THM LIMIT CORE": 0x40}    # ../pm-table.md


def pm_limit_values():
    """The SMU power/thermal limits in effect, {name: float} ({} without
    ryzen_smu). Same table ryzenadj --info prints, without the fork."""
    raw = pm_read()
    if raw is None:
        return {}
    return {k: struct.unpack_from("<f", raw, o)[0] for k, o in PM_LIMITS.items()}


RAPL = "/sys/class/powercap/intel-rapl:0/"     # package energy counter (Zen: same name)


class PkgPower:
    """CPU package watts from the RAPL energy counter, averaged over the time
    since the previous call -- what btop shows, with no ryzenadj spawn."""

    def __init__(self):
        self.wrap = read_int(RAPL + "max_energy_range_uj") or 0
        self.last = self._read()

    @staticmethod
    def _read():
        e = read_int(RAPL + "energy_uj")
        return None if e is None else (e, time.monotonic())

    def watts(self, settle=0.3):
        if self.last is None:
            return None
        if time.monotonic() - self.last[1] < 0.2:     # first call: take a short sample
            time.sleep(settle)
        now = self._read()
        if now is None:
            return None
        (e0, t0), (e1, t1) = self.last, now
        self.last = now
        if e1 < e0:
            e1 += self.wrap
        return (e1 - e0) / 1e6 / (t1 - t0) if t1 > t0 else None


BAT_REGS = 0x70     # EC: BADC design mAh, BFCC reported-full mAh, BADV mV (u16 LE)


def battery_capacity(rig):
    """-> (design Wh, full Wh) from the EC, or None. sysfs is no use here: the
    DSDT copies BFCC into both energy_full and energy_full_design, so the real
    design capacity (BADC) is never exported (../battery-charge-control.md)."""
    try:
        raw = rig.ec_read(BAT_REGS, 6)
        badc, bfcc, badv = (int.from_bytes(raw[i:i + 2], "little") for i in (0, 2, 4))
    except (OSError, ValueError):
        return None
    if len(raw) != 6 or not (badc and bfcc and badv):
        return None
    return badc * badv / 1e6, bfcc * badv / 1e6


def current_mode(p):
    """The user-facing profile in effect: auto | quiet | eco | perf."""
    return p["mode"]


def battery_extender(rig):
    """BIOS 'Adaptive Battery Extender' (SHEN, EC 0xC5 bit 7): True = capping the
    charge by lowering the reported full capacity. Read-only -- the EC re-asserts
    the bit within ~150 ms of any write (../battery-charge-control.md)."""
    try:
        return bool(rig.ec_read(0xC5)[0] & 0x80)
    except (OSError, IndexError):
        return None


def power_state(ra, rig, pkg_w):
    """Platform/power facts for `status`, from sysfs and the SMU PM table."""
    cpu0 = "/sys/devices/system/cpu/cpu0/cpufreq/"
    gov = sysfs(cpu0 + "scaling_governor")
    fmax = read_int(cpu0 + "scaling_max_freq")
    ac = sysfs("/sys/class/power_supply/ACAD/online") == "1"
    drv = os.path.basename(os.readlink("/sys/bus/pci/devices/0000:01:00.0/driver")) \
        if os.path.exists("/sys/bus/pci/devices/0000:01:00.0/driver") else None
    dpm = None
    for d in os.listdir("/sys/class/drm"):
        p = "/sys/class/drm/%s/device/" % d
        if os.path.exists(p + "pp_dpm_fclk"):
            dpm = sysfs(p + "power_dpm_force_performance_level")
            break
    bat = "/sys/class/power_supply/BAT0/"
    power_now = read_int(bat + "power_now")
    return {"gov": gov, "fmax": fmax, "boost": sysfs(BOOST) == "1", "ac": ac,
            "mode": read_mode(),
            "dgpu": drv, "dpm": dpm, "ra": ra, "pkg_w": pkg_w,
            "bat_pct": read_int(bat + "capacity"),
            "bat_status": sysfs(bat + "status"),
            "bat_w": power_now / 1e6 if power_now else None,
            "bat_wh": (read_int(bat + "energy_now") or 0) / 1e6,
            "bat_cap": battery_capacity(rig),
            "bat_extender": battery_extender(rig),
            "bat_cycles": read_int(bat + "cycle_count"),
            "profile": sysfs("/sys/firmware/acpi/platform_profile")}


def _section(title, width, paint):
    return paint("90", "─ ") + paint("1", title) + " " + paint("90", "─" * max(4, width - len(title) - 4))


def _row(label, text, paint):
    return "  " + paint("90", "%-8s" % label) + text


def render_power(p, bar_w, paint, width, level=0):
    """level 0 = everything, 1 = no blank lines / limits / gpu / extender, 2 = only
    the numbers that matter (no section titles)."""
    ra = p["ra"]
    lines = [] if level else ["", _section("Power", width, paint)]
    if level == 1:
        lines.append(_section("Power", width, paint))
    if p["bat_w"] and p["bat_status"] == "Discharging":
        hours = p["bat_wh"] / p["bat_w"]
        # fixed 0-60 W reading scale: the machine has no system power limit
        lines.append(_row("system", "%s %s%s" % (
            _bar(p["bat_w"] / 60.0, bar_w, YELLOW, paint), paint("1", "%4.1f W" % p["bat_w"]),
            paint("90", "  ~%dh%02d left" % (hours, (hours % 1) * 60))), paint))
    pkg = p["pkg_w"]
    lim = ra.get("STAPM LIMIT")
    if pkg is not None and lim:
        lines.append(_row("cpu pkg", "%s %s%s" % (
            _bar(pkg / lim, bar_w, PURPLE, paint),
            paint("1", "%4.1f" % pkg), paint("90", " / %g W" % lim)), paint))
    ghz = "%.2f GHz" % (p["fmax"] / 1e6) if p["fmax"] else "?"
    boost = paint(GREEN, "boost on") if p["boost"] else paint(YELLOW, "boost off")
    if level < 2:
        lines.append(_row("cpu", "%s · max %s · %s" % (p["gov"], ghz, boost), paint))
    if ra and level == 0:
        lines.append(_row("limits", paint("90", "slow %g W · fast %g W · apu %g W · tctl %g °C" % (
            ra.get("PPT LIMIT SLOW", 0), ra.get("PPT LIMIT FAST", 0),
            ra.get("PPT LIMIT APU", 0), ra.get("THM LIMIT CORE", 0))), paint))
    d = {"nvidia": "nvidia (active)", "vfio-pci": "vfio-pci (off)"}.get(p["dgpu"], p["dgpu"])
    if level == 0:
        lines.append(_row("gpu", "dGPU %s · iGPU dpm %s" % (d or "none", p["dpm"] or "?"), paint))
    if p["bat_pct"] is not None:
        # on AC the sensor reads the power flowing INTO the battery (what btop/KDE
        # show), not what the machine draws -- the adapter reports nothing
        into_battery = (" · %.1f W into the battery" % p["bat_w"]
                        if p["bat_status"] == "Charging" and p["bat_w"] else "")
        if level == 0:
            lines += ["", _section("Battery", width, paint)]
        elif level == 1:
            lines.append(_section("Battery", width, paint))
        lines.append(_row("charge", "%s %s" % (paint("1", "%d%%" % p["bat_pct"]),
                                               paint("90", p["bat_status"] + into_battery)),
                          paint))
        if p["bat_cap"] and level < 2:
            design, full = p["bat_cap"]
            health = 100.0 * full / design
            code = GREEN if health >= 80 else YELLOW if health >= 60 else RED
            cycles = " · %d cycles" % p["bat_cycles"] if p["bat_cycles"] else ""
            lines.append(_row("health", "%s %s" % (
                paint("1;" + code, "%.0f%%" % health),
                paint("90", "(%.1f / %.1f Wh%s)" % (full, design, cycles))), paint))
        if p["bat_extender"] is not None and level == 0:
            lines.append(_row("limit", paint(YELLOW, "adaptive extender on (BIOS caps the charge)")
                              if p["bat_extender"] else paint("90", "adaptive extender off"),
                              paint))
    return lines


def render(fans, cpu, gpu, regs, rpms, hist, width, paint, gpu_offset, ec57, power, live,
           manual=None, level=0, max_width=78, show_debug=True):
    """One frame of `status`, as a list of lines. `max_width` caps the layout (78
    keeps the plain view tidy; the TUI passes its full width); `level` trades detail
    for height: 0 everything, 1 no blank lines/footer/minor rows, 2 the essentials."""
    width = max(40, min(width, max_width)) - 2
    bar_w = max(8, min(24 if max_width <= 78 else 30, width - 46))
    mode = current_mode(power).upper()
    src = "AC" if power["ac"] else "BATTERY"
    pad = max(1, width - len(" omenctl") - len(mode) - len(src) - 3)
    lines = [paint("1", " omenctl") + " " * pad
             + paint("1;" + BLUE, mode) + paint("90", " · ") + paint("1", src)]
    if level == 0:
        lines += ["", _section("Thermal", width, paint)]
    elif level == 1:
        lines.append(_section("Thermal", width, paint))
    for f in fans:
        o = f["write_reg"] - SET_CPU
        is_cpu = f["write_reg"] == SET_CPU
        temp = cpu if is_cpu else gpu
        setp = regs[o]
        label = "CPU" if is_cpu else "GPU"
        warn = "!" if temp >= 85 else " "
        head = "%s %s  " % (paint("1", label),
                            paint("1;" + _temp_code(temp), "%4.0f°C%s" % (temp, warn)))
        if setp == RESET_VALUE:
            fan = paint(ORANGE, "EC auto (not owned by omenctl)")
        else:
            rpm = rpms[0 if is_cpu else 1]
            fan_prefix = paint("90", "fan ") if max_width > 78 else ""
            fan = "%s%s %s %s" % (fan_prefix,
                                  _bar(setp / f["speed_max"], bar_w, BLUE, paint),
                                  paint("1", "%3d%%" % setp),
                                  paint("90", "%5s rpm" % (rpm if rpm is not None else "?")))
        if manual and f["write_reg"] in manual and setp != RESET_VALUE:
            fan += paint(ORANGE, " manual")
        # the graph takes whatever width is left after the gauge and the numbers
        extra = 4 if max_width > 78 else 0
        room = max(12, width - (bar_w + 35 + extra))
        spark = _spark(hist[label][-room:], paint) if len(hist[label]) > 2 else ""
        lines.append("  " + head + fan + "  " + spark)
    if max_width > 78 and level == 0:
        lines.append(paint("90", "  bar = fan duty · spark = temperature history (30–100 °C)"))
    lines += render_power(power, bar_w, paint, width, level)
    if level == 0 and show_debug:
        foot = "ec57=%d ec49=%d (+%g)" % (ec57, gpu - gpu_offset, gpu_offset)
        lines += ["", paint("90", "  " + (foot + " · Ctrl-C to quit" if live else foot))]
    return lines


def cmd_service(args):
    """start / stop / restart the fan daemon, and enable / disable it at boot
    (both also act now). `stop`/`disable` leave the fans on the EC's own curve
    (the stop path writes the 255s); `start`/`enable` take them back."""
    argv = {"enable": ["enable", "--now"], "disable": ["disable", "--now"]}.get(
        args.cmd, [args.cmd])
    rc = subprocess.run(["systemctl"] + argv + ["omenctl"]).returncode
    if rc == 0:
        print({"start": "omenctl owns the fans",
               "restart": "restarted: omenctl owns the fans",
               "enable": "enabled at boot and started: omenctl owns the fans",
               "stop": "stopped: fans are on the EC's own curve "
                       "(omenctl start to take them back)",
               "disable": "disabled at boot and stopped: fans are on the EC's own "
                          "curve (omenctl enable to undo)"}[args.cmd])
    return rc


class Monitor:
    """Everything `status` shows, sampled once per call: the single source for the
    text view, the JSON output and the TUI."""

    def __init__(self, gpu_offset):
        self.gpu_offset = gpu_offset
        self.rig = Rig(gpu_offset)
        self.pkg = PkgPower()
        self.hist = {"CPU": [], "GPU": []}

    def sample(self):
        rig = self.rig
        cpu, gpu, regs = rig.temps()
        for label, t in (("CPU", cpu), ("GPU", gpu)):
            self.hist[label] = (self.hist[label] + [t])[-120:]
        rpms = [read_int(rig.hp + "/fan%d_input" % i) if rig.hp else None for i in (1, 2)]
        return {"cpu": cpu, "gpu": gpu, "regs": regs, "rpms": rpms,
                "manual": overrides_read(),
                "ec57": rig.ec_read(CPU_TEMP)[0],
                "power": power_state(pm_limit_values(), rig, self.pkg.watts())}

    def lines(self, d, width, paint, live, level=0, max_width=78, show_debug=True):
        return render(FANS, d["cpu"], d["gpu"], d["regs"], d["rpms"], self.hist, width,
                      paint, self.gpu_offset, d["ec57"], d["power"], live, d["manual"],
                      level, max_width, show_debug)


def state_json(d):
    """The sample as a plain dict, for `status --json` (and future GUIs)."""
    p, regs = d["power"], d["regs"]
    fans = {}
    for i, (name, temp) in enumerate((("cpu", d["cpu"]), ("gpu", d["gpu"]))):
        fans[name] = {"temp_c": round(temp, 1),
                      "fan_percent": None if regs[i] == RESET_VALUE else regs[i],
                      "fan_rpm": d["rpms"][i], "owned_by_omenctl": regs[i] != RESET_VALUE,
                      "manual": d["manual"].get(SET_CPU if i == 0 else SET_GPU)}
    cap = p["bat_cap"]
    return {
        "mode": p["mode"], "source": "ac" if p["ac"] else "battery",
        "fans": fans,
        "power": {"cpu_pkg_w": None if p["pkg_w"] is None else round(p["pkg_w"], 2),
                  "governor": p["gov"], "max_mhz": p["fmax"] and p["fmax"] // 1000,
                  "boost": p["boost"], "igpu_dpm": p["dpm"], "dgpu_driver": p["dgpu"],
                  "limits": {k: round(v, 1) for k, v in p["ra"].items()}},
        "battery": {"percent": p["bat_pct"], "status": p["bat_status"],
                    "power_w": p["bat_w"], "cycles": p["bat_cycles"],
                    "design_wh": cap and round(cap[0], 2), "full_wh": cap and round(cap[1], 2),
                    "health_percent": cap and round(100.0 * cap[1] / cap[0], 1),
                    "adaptive_extender": p["bat_extender"]}}


def daemon_running():
    """True if the fan daemon holds its lock (no process spawn)."""
    try:
        with open(LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False                      # we got it: nobody else has it
    except OSError:
        return True


def cmd_fan(args):
    """Manual fan speed. Safety lives in the daemon, not here: a manual speed is
    dropped for a fan at >= OVERRIDE_MAX_TEMP (LOW_SPEED_MAX_TEMP below LOW_SPEED %),
    ignored at CRITICAL_TEMP, and forgotten when the daemon restarts or on reboot."""
    manual = overrides_read()
    cur = {k: manual.get(r) for k, r in FAN_KEYS.items()}
    if args.fan is None:
        for k in FAN_KEYS:
            print("%s fan: %s" % (k, "auto (curve)" if cur[k] is None else "%d%% (manual)" % cur[k]))
        return 0
    if not daemon_running():
        print("omenctl is not running the fans: `omenctl start` first")
        return 1
    value = None if args.speed in (None, "auto") else args.speed
    if value is not None:
        if not value.isdigit() or not OVERRIDE_MIN <= int(value) <= 100:
            print("speed must be auto or %d-100 (%%)" % OVERRIDE_MIN)
            return 1
        value = int(value)
    for k in (FAN_KEYS if args.fan == "all" else (args.fan,)):
        cur[k] = value
    overrides_write(cur)
    print("%s fan: %s" % (args.fan, "auto (curve)" if value is None else
          "%d%% manual (dropped at %.0f C)" % (
              value, OVERRIDE_MAX_TEMP if value >= LOW_SPEED else LOW_SPEED_MAX_TEMP)))
    return 0


def cmd_status(args):
    import json
    import shutil                 # lazy: the fan daemon never needs these
    mon = Monitor(args.gpu_offset)
    if args.json:
        while True:
            print(json.dumps(state_json(mon.sample())), flush=True)
            if not args.watch:
                return 0
            time.sleep(1)
    load_theme()
    tty = sys.stdout.isatty()
    paint = _color(tty and "NO_COLOR" not in os.environ)
    if args.watch and tty:
        sys.stdout.write("\033[?1049h\033[?25l")        # alt screen, hide cursor
    try:
        while True:
            width = shutil.get_terminal_size((80, 24)).columns
            lines = mon.lines(mon.sample(), width, paint, args.watch)
            if not args.watch:
                print("\n".join(lines))
                return 0
            sys.stdout.write("\033[H" + "".join(l + "\033[K\n" for l in lines)
                             + "\033[J")
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        if args.watch and tty:
            sys.stdout.write("\033[?25h\033[?1049l")


# -------------------------------------------------------------------- tui --
# `omenctl tui`: the status view, live, with the modes as clickable buttons
# (mouse) or keys 1-4. It reuses render(): the painter below marks the colour
# codes instead of emitting ANSI, and draw_marked() turns them into curses
# attributes, so there is one layout for text, watch and TUI.

def _marker_paint(code, text):
    return "\x01%s\x02%s\x03" % (code, text)


def _attr_for(code, pairs):
    """ANSI-ish code ('1;36', '90', '38;5;208') -> curses attribute."""
    import curses
    attr, toks, i = 0, code.split(";"), 0
    while i < len(toks):
        t = toks[i]
        if t == "1":
            attr |= curses.A_BOLD
        elif t == "90":
            attr |= curses.A_DIM
        elif t == "38" and i + 2 < len(toks):
            attr |= curses.color_pair(pairs.get(int(toks[i + 2]), pairs[3]))
            i += 2
        elif t.isdigit() and 31 <= int(t) <= 37:
            attr |= curses.color_pair(pairs[int(t) - 30])
        i += 1
    return attr


def draw_marked(win, y, x, text, pairs, maxx):
    import curses
    import re
    attr, pos = 0, 0
    for m in re.finditer(r"\x01([^\x02]*)\x02(.*?)\x03|([^\x01]+)", text):
        seg, a = (m.group(2), _attr_for(m.group(1), pairs)) if m.group(2) is not None \
            else (m.group(3), 0)
        if x + pos >= maxx:
            break
        try:
            win.addstr(y, x + pos, seg[:maxx - x - pos], a)
        except curses.error:
            pass
        pos += len(seg)


TUI_MODES = (("1", "auto"), ("2", "quiet"), ("3", "eco"), ("4", "perf"))


FAN_ROWS = (("cpu", "CPU fan", SET_CPU, 0), ("gpu", "GPU fan", SET_GPU, 1))
SAMPLE_EVERY = 1.0          # s -- hardware is read at this pace; drawing follows events
DRAG_WRITE_EVERY = 0.04     # s -- at most ~25 file writes/s while a fader is dragged


def tui_main(stdscr, args):
    import contextlib
    import curses
    import io
    curses.set_escdelay(25)          # ms ncurses waits to tell Esc from an escape sequence
    curses.curs_set(0)
    curses.use_default_colors()
    load_theme()
    pairs = {}
    for n in range(1, 8):
        curses.init_pair(n, n, -1)
        pairs[n] = n
    if curses.COLORS >= 256:
        # render() uses the 256-colour cube (>= 16); register the semantic
        # roles actually in use (load_theme() may have replaced them).
        codes = set()
        for code in (GREEN, YELLOW, ORANGE, RED, BLUE, PURPLE):
            if code.startswith("38;5;"):
                try:
                    codes.add(int(code.rsplit(";", 1)[1]))
                except ValueError:
                    pass
        for i, code in enumerate(sorted(codes), start=8):
            if code < curses.COLORS:
                curses.init_pair(i, code, -1)
                pairs[code] = i
    curses.mouseinterval(0)                       # raw press/release, no click synthesis
    wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
    wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)
    curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED
                     | curses.BUTTON1_RELEASED | curses.REPORT_MOUSE_POSITION
                     | wheel_up | wheel_down)
    # ncurses only turns on mouse mode 1000 (press/release): a terminal then never
    # reports motion, so a drag would only show up on release. 1002 adds "motion
    # while a button is held"; cmd_tui turns it off again on the way out.
    sys.stdout.write("\x1b[?1002h")
    sys.stdout.flush()
    mon = Monitor(args.gpu_offset)
    syslog.openlog("omenctl-profile")
    paint = _plain_paint if os.environ.get("NO_COLOR") else _marker_paint
    msg, sel, debug = "", 0, False
    TX = 24                                       # slider track column
    drag = None                                   # {"i": fan row, "val": %, "wrote": %, "t": s}

    def put(y, x, text, attr=0):          # a too-small terminal must not crash us
        try:
            stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def store(d, i, value):
        """Write fan row i's manual speed (None = auto); the daemon applies it."""
        cur = {k: d["manual"].get(r) for k, _l, r, _ri in FAN_ROWS}
        cur[FAN_ROWS[i][0]] = None if value is None else max(OVERRIDE_MIN, min(100, int(value)))
        overrides_write(cur)

    def set_fan(d, i, value):
        if d["regs"][0] == RESET_VALUE:
            return "the fan daemon is stopped: press f to start it"
        store(d, i, value)
        return "%s: %s" % (FAN_ROWS[i][1], "auto (curve)" if value is None
                           else "%d%% manual" % max(OVERRIDE_MIN, min(100, int(value))))

    def pct_at(mx, x0, tw):
        return max(OVERRIDE_MIN, min(100, round((mx - x0) / float(max(1, tw - 1))
                                                * (100 - OVERRIDE_MIN)) + OVERRIDE_MIN))

    d = mon.sample()
    next_sample = time.monotonic() + SAMPLE_EVERY
    while True:
        d["manual"] = overrides_read()                # cheap, and always current
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        # fit the view to the window: the most detailed level that leaves room for
        # the controls (buttons + 2 faders = 4 rows with their spacing, more when tall)
        for level in (0, 1, 2):
            lines = mon.lines(d, w, paint, False, level, 400, debug)
            if len(lines) + 8 <= h or level == 2:
                break
        room = h - len(lines)                         # rows left for the controls
        if room < 3:                                  # tiny window: trim the view itself
            lines = lines[:max(0, h - 3)]
            room = h - len(lines)
        for y, line in enumerate(lines):
            draw_marked(stdscr, y, 0, line, pairs, w)
        spaced = room >= 8                            # blank rows between the groups
        by = len(lines) + (1 if spaced else 0)        # buttons row
        fy = by + (2 if spaced else 1)                # first fader row
        my = fy + 2 + (1 if spaced else 0)            # message row (when there is room)
        hy = my + 1                                   # hint row
        x, buttons = 2, []
        cur = d["power"]["mode"]
        owned = d["regs"][0] != RESET_VALUE
        items = [("%s %s %s " % ("●" if m == cur else " ", k, m), ("mode", m), m == cur)
                 for k, m in TUI_MODES]
        items.append((" f %s fans " % ("stop" if owned else "start"), ("fans", owned), False))
        for label, action, active in items:
            if by < h and x + len(label) < w:
                put(by, x, label, curses.A_REVERSE | curses.A_BOLD if active
                    else curses.A_REVERSE | curses.A_DIM)
            buttons.append((by, x, x + len(label), action))
            x += len(label) + 1
        # manual fan speed: a fader and an "auto" checkbox per fan; the fill moves
        # in eighths of a cell, so a drag looks smoother than the cell grid
        tw = max(10, min(100, w - TX - 9))       # <= 100 cells: 1 % per cell or finer
        for i, (key, label, reg, ri) in enumerate(FAN_ROWS):
            y = fy + i
            if y >= h:
                break
            manual_v = d["manual"].get(reg)
            if drag and drag["i"] == i:
                manual_v = drag["val"]
            value = d["regs"][ri] if manual_v is None else manual_v
            value = d["regs"][ri] if value == RESET_VALUE else value
            dim = 0 if owned else curses.A_DIM
            put(y, 2, "▸" if i == sel else " ", curses.A_BOLD)
            put(y, 4, label, (curses.A_BOLD if i == sel else 0) | dim)
            put(y, 13, "[%s] auto" % ("x" if manual_v is None else " "),
                (curses.A_BOLD if manual_v is None else 0) | dim)
            frac = max(0.0, min(1.0, (value - OVERRIDE_MIN) / float(100 - OVERRIDE_MIN)))
            eighths = round(frac * tw * 8)
            full, part = eighths // 8, eighths % 8
            fill = ((curses.color_pair(pairs[6]) | curses.A_BOLD) if manual_v is not None
                    else curses.A_DIM) if owned else curses.A_DIM
            put(y, TX, "█" * full, fill)
            if part and full < tw:
                put(y, TX + full, " ▏▎▍▌▋▊▉"[part], fill)
            rest = tw - full - (1 if part else 0)
            put(y, TX + tw - rest, "░" * rest, curses.A_DIM)
            put(y, TX + tw + 1, "%3d%%" % value, curses.A_BOLD | dim)
            buttons.append((y, 13, 21, ("auto_toggle", i)))
            buttons.append((y, TX, TX + tw, ("track", i, TX, tw)))
        hint = ("drag/←→ 1 %  ·  a auto  ·  ↑/↓ fan  ·  1-4 mode  ·  f fans  ·  d debug  ·  q quit"
                if w >= 90 else
                "←→ 1 %  ·  a auto  ·  1-4 mode  ·  f fans  ·  d debug  ·  q quit")
        for yy, text, attr in ((my, msg, 0), (hy, hint, curses.A_DIM)):
            if yy < h and text:
                put(yy, 2, text[:max(0, w - 3)], attr)
        stdscr.refresh()
        # sleep until the next hardware sample, but wake at once on any input
        stdscr.timeout(max(1, int((next_sample - time.monotonic()) * 1000)))
        key = stdscr.getch()
        if time.monotonic() >= next_sample:
            d = mon.sample()
            next_sample = time.monotonic() + SAMPLE_EVERY
        action, mx = None, 0
        if key in (ord("q"), 27):
            return 0
        if key in (ord("1"), ord("2"), ord("3"), ord("4")):
            action = ("mode", TUI_MODES[key - ord("1")][1])
        elif key == ord("f"):
            action = ("fans", owned)
        elif key in (curses.KEY_UP, curses.KEY_DOWN, 9):
            sel = 1 - sel
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_PPAGE, curses.KEY_NPAGE):
            delta = {curses.KEY_LEFT: -1, curses.KEY_RIGHT: 1,
                     curses.KEY_PPAGE: 10, curses.KEY_NPAGE: -10}[key]
            now = d["manual"].get(FAN_ROWS[sel][2])
            base = now if now is not None else d["regs"][FAN_ROWS[sel][3]]
            msg = set_fan(d, sel, base + delta)
        elif key in (ord("a"), ord(" ")):
            action = ("auto_toggle", sel)
        elif key == ord("d"):
            debug = not debug
            msg = "debug: %s" % ("on" if debug else "off")
        elif key == curses.KEY_MOUSE:
            try:
                _id, mx, my, _z, bstate = curses.getmouse()
            except curses.error:
                continue
            if wheel_up and bstate & (wheel_up | wheel_down):    # wheel over a fader: +-1 %
                for (yy, _x0, _x1, act) in buttons:
                    if act[0] == "track" and my == yy:
                        sel = act[1]
                        now = d["manual"].get(FAN_ROWS[sel][2])
                        base = now if now is not None else d["regs"][FAN_ROWS[sel][3]]
                        msg = set_fan(d, sel, base + (1 if bstate & wheel_up else -1))
            elif bstate & curses.REPORT_MOUSE_POSITION:          # motion
                if drag:
                    drag["val"] = pct_at(mx, TX, tw)
                    if (drag["val"] != drag["wrote"]
                            and time.monotonic() - drag["t"] >= DRAG_WRITE_EVERY):
                        store(d, drag["i"], drag["val"])
                        drag["wrote"], drag["t"] = drag["val"], time.monotonic()
            elif bstate & curses.BUTTON1_RELEASED:
                if drag:                       # the value under the release always lands
                    drag["val"] = pct_at(mx, TX, tw)
                    if drag["val"] != drag["wrote"]:
                        store(d, drag["i"], drag["val"])
                    msg = "%s: %d%% manual" % (FAN_ROWS[drag["i"]][1], drag["val"])
                    drag = None
            elif drag:
                pass                                              # a stray press mid-drag
            elif bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                for (yy, x0, x1, act) in buttons:
                    if my == yy and x0 <= mx < x1:
                        if act[0] == "track" and owned:
                            sel = act[1]
                            val = pct_at(mx, act[2], act[3])
                            store(d, act[1], val)
                            drag = {"i": act[1], "val": val, "wrote": val,
                                    "t": time.monotonic()}
                            if bstate & curses.BUTTON1_CLICKED:   # no separate release
                                msg, drag = "%s: %d%% manual" % (FAN_ROWS[act[1]][1], val), None
                        elif act[0] == "track":
                            msg = "the fan daemon is stopped: press f to start it"
                        else:
                            action = act
        if action and action[0] == "mode":
            with contextlib.redirect_stdout(io.StringIO()):
                set_mode(action[1], Ops(False))
            msg = "mode: %s" % action[1]
        elif action and action[0] == "fans":
            verb = "stop" if action[1] else "start"
            subprocess.run(["systemctl", verb, "omenctl"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            msg = "fans: handed to the EC" if verb == "stop" else "fans: omenctl owns them"
        elif action and action[0] == "auto_toggle":
            sel = action[1]
            if d["manual"].get(FAN_ROWS[sel][2]) is None:    # auto -> manual at today's speed
                msg = set_fan(d, sel, d["regs"][FAN_ROWS[sel][3]])
            else:
                msg = set_fan(d, sel, None)


def cmd_tui(args):
    import curses
    if not sys.stdout.isatty():
        sys.exit("omenctl tui needs a terminal")
    try:
        return curses.wrapper(tui_main, args) or 0
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\x1b[?1002l")
        sys.stdout.flush()


# ---------------------------------------------------------------- profiles --
# Power profiles for the Ryzen 5800H + RTX 3070 (ported from the power-profile
# bash script; rationale for the numbers is in ../efficiency.md and
# ../dgpu-control.md). ryzenadj limits are mW; frequencies kHz.

PROFILES = {
    "perf": {
        "label": "4.47 GHz / 54W / GPU unlocked",
        "gov": "performance", "fmax": 4465000, "epp": "performance",
        "ryzenadj": ["--stapm-limit=54000", "--fast-limit=65000",
                     "--slow-limit=54000", "--apu-slow-limit=42000",
                     "--tctl-temp=90"],
        "nvidia": [["-rmc"], ["-lgc", "500,2100"]],   # floor kept: deep P8 = mini-freezes
        "wifi_ps": "off", "igpu_dpm": "auto",
    },
    "ac": {
        "label": "no freq cap / 28W / GPU 500-1800 MHz",
        "gov": "powersave", "fmax": 4465000, "epp": "balance_power",
        "ryzenadj": ["--stapm-limit=28000", "--fast-limit=36000",
                     "--slow-limit=28000", "--apu-slow-limit=22000",
                     "--tctl-temp=85"],
        "nvidia": [["-rmc"], ["-lgc", "500,1800"]],
        "wifi_ps": "off", "igpu_dpm": "auto",
    },
    "battery": {
        "label": "2.4 GHz / 15W / GPU 400 MHz",
        "gov": "powersave", "fmax": 2400000, "epp": "power",
        "ryzenadj": ["--stapm-limit=15000", "--fast-limit=18000",
                     "--slow-limit=15000", "--apu-slow-limit=15000",
                     "--tctl-temp=65", "--power-saving"],
        "nvidia": [["-lgc", "0,400"], ["-lmc", "405"]],
        "wifi_ps": "on", "igpu_dpm": "low",   # ~-3 W idle, costs CPU memory bandwidth
    },
}
DGPU = "/sys/bus/pci/devices/0000:01:00.0/driver"
BOOT_SEEN = "/run/omenctl.boot-seen"
BOOST = "/sys/devices/system/cpu/cpufreq/boost"
MODE_FILE = "/var/lib/omenctl/mode"       # the user's choice: auto|quiet|eco|perf
MODES = ("auto", "quiet", "eco", "perf")
APPLY_LOCK = "/run/omenctl-profile.lock"    # serialises concurrent profile applies
LAST_APPLY = "/run/omenctl-profile.last"    # mtime = end of the last apply
PLATFORM_PROFILE = "/sys/class/platform-profile/platform-profile-0/profile"
PM_TABLE = "/sys/kernel/ryzen_smu_drv/pm_table"
# The EC register map below (fan setpoints 0x2C/0x2D, temps 0x49/0x57, battery
# 0x70...) was measured on this model only. Other OMENs map those bytes to other
# things, so omenctl refuses to run anywhere else (see check_model).
SUPPORTED_PRODUCT = "OMEN Laptop 15-en1"       # prefix of DMI product_name
SUPPORTED_BOARDS = ("88D2",)                   # DMI board_name verified so far
LEGACY_BOOST = "/var/lib/omenctl/boost"   # older per-setting files, migrated once
LEGACY_ECO = "/var/lib/omenctl/eco"


def plog(msg):
    syslog.syslog(msg)


class Ops:
    """Side effects of a profile, executed or (dry-run) just printed."""

    def __init__(self, dry):
        self.dry = dry

    def write(self, path, value):
        if self.dry:
            print("write %s <- %s" % (path, value))
            return True
        try:
            with open(path, "w") as f:
                f.write(str(value))
            return True
        except OSError:
            plog("WARN: %s write failed (%s)" % (path, value))
            return False

    def run(self, argv):
        if self.dry:
            print("run   " + " ".join(argv))
            return 0
        try:
            return subprocess.run(argv, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=30).returncode
        except (OSError, subprocess.SubprocessError):
            return 1


def igpu_dpm_path():
    """The amdgpu device that owns pp_dpm_fclk (card numbers are not stable;
    the dGPU has no such node)."""
    for dev in sorted(glob.glob("/sys/class/drm/card*/device")):
        if os.path.exists(dev + "/pp_dpm_fclk"):
            return dev + "/power_dpm_force_performance_level"
    return None


def nvidia_ready():
    """Gate on the PCI binding symlink: when nvidia does not own the dGPU
    (vfio-pci, D3cold) every nvidia-smi call costs ~0.8 s and fails."""
    try:
        return os.path.basename(os.readlink(DGPU)) == "nvidia"
    except OSError:
        return False


def apply_profile(name, ops, boost=True):
    p = PROFILES[name]
    plog("%s profile" % name.upper())
    # boost first, so the freq cap below is not clamped under it
    ops.write(BOOST, "1" if boost else "0")
    for f, v in (("scaling_governor", p["gov"]), ("scaling_max_freq", p["fmax"]),
                 ("energy_performance_preference", p["epp"])):
        paths = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/" + f)
        if ops.dry:
            ops.write("cpu*/cpufreq/" + f, v)
        else:
            for path in paths:
                ops.write(path, v)
    if ops.run(["/usr/bin/ryzenadj"] + p["ryzenadj"]) != 0:
        plog("WARN: ryzenadj %s failed" % name)
    if nvidia_ready():
        for args in p["nvidia"]:
            ops.run(["nvidia-smi"] + args)
    ops.run(["iw", "dev", "wlan0", "set", "power_save", p["wifi_ps"]])
    path = igpu_dpm_path()
    if path is None:
        plog("WARN: no amdgpu DPM node found")
    elif ops.dry or sysfs(path) != p["igpu_dpm"]:   # already there: no log spam
        if ops.write(path, p["igpu_dpm"]):
            plog("iGPU DPM level -> %s" % p["igpu_dpm"])
    print(">>> %s: %s" % (name.upper(), p["label"]))
    plog("OK %s: %s" % (name.upper(), p["label"]))


def write_mode(mode):
    os.makedirs(os.path.dirname(MODE_FILE), exist_ok=True)
    with open(MODE_FILE, "w") as f:
        f.write(mode)


def read_mode():
    """The stored mode (auto if none). Migrates the older eco/boost files once."""
    mode = sysfs(MODE_FILE)
    if mode in MODES:
        return mode
    mode = ("eco" if os.path.exists(LEGACY_ECO)
            else "quiet" if sysfs(LEGACY_BOOST) == "off" else "auto")
    try:
        write_mode(mode)
        for old in (LEGACY_BOOST, LEGACY_ECO):
            if os.path.exists(old):
                os.unlink(old)
    except OSError:
        pass
    return mode


def source_profile():
    return "ac" if sysfs("/sys/class/power_supply/ACAD/online", "0") == "1" else "battery"


def apply_mode(mode, ops):
    """auto = the envelope of the power source; quiet = auto with boost off;
    eco = the battery envelope whatever the source; perf = everything unlocked."""
    if mode == "perf":
        apply_profile("perf", ops)
    elif mode == "eco":
        apply_profile("battery", ops)
    else:
        apply_profile(source_profile(), ops, boost=(mode != "quiet"))


def log_boot_limits():
    """Once per boot, journal the SMU limits the firmware seeded at POST -- how
    you tell whether a BIOS change took effect, since this overwrites them."""
    if os.path.exists(BOOT_SEEN):
        return
    ra = pm_limit_values()
    keys = ("STAPM LIMIT", "PPT LIMIT FAST", "PPT LIMIT SLOW", "THM LIMIT CORE")
    if all(k in ra for k in keys):
        plog("firmware/POST limits: " + " ".join("%s=%g" % (k, ra[k]) for k in keys))
    try:
        open(BOOT_SEEN, "w").close()
    except OSError:
        pass


def cmd_boost(args):
    """CPU boost is part of the mode: off = quiet, on = auto. It only matters in
    those two (perf needs it on, eco's 2.4 GHz cap is below the base clock)."""
    mode = read_mode()
    if args.state in ("on", "off"):
        want = "quiet" if args.state == "off" else "auto"
        if mode in ("auto", "quiet"):
            if mode != want:
                write_mode(want)
                apply_mode(want, Ops(False))
        elif args.state == "off":
            print("boost: mode %s needs it as is; switch with `omenctl profile auto|quiet`"
                  % mode)
            return 1
        mode = read_mode()
    fmax = read_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    print("boost %s (mode: %s) · max %s GHz"
          % ("on" if sysfs(BOOST) == "1" else "off", mode,
             "%.2f" % (fmax / 1e6) if fmax else "?"))
    return 0


PROFILE_HELP = """\
modes (the one you choose is remembered across reboots):
  auto    follows the power source: AC or battery envelope (the default)
  quiet   auto with boost off: cooler and quieter
  eco     battery envelope (15 W, 2.4 GHz) kept even on AC
  perf    everything unlocked (54 W, 4.47 GHz): runs hot, fans ramp up

With no mode, shows the one in effect.
"""


def profile_mode(value):
    if value in MODES or value == "guard":     # guard: internal, unlisted
        return value
    raise argparse.ArgumentTypeError(
        "unknown mode %r (choose from auto, quiet, eco, perf)" % value)


def apply_stored_mode(ops, quiet_if_recent=0.0):
    """Re-apply the stored mode (what `guard` and the watcher do), serialised
    against any other apply, and skipped when another one finished less than
    `quiet_if_recent` seconds ago (udev and the watcher fire together on a
    plug event: the second one has nothing left to fix)."""
    with open(APPLY_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            age = time.time() - os.stat(LAST_APPLY).st_mtime
        except OSError:
            age = 1e9
        if age < quiet_if_recent:
            return False
        log_boot_limits()
        apply_mode(read_mode(), ops)
        open(LAST_APPLY, "w").close()
    return True


def set_mode(mode, ops):
    """Store `mode` and apply it, serialised against any other apply."""
    write_mode(mode)
    log_boot_limits()
    with open(APPLY_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        apply_mode(mode, ops)
        open(LAST_APPLY, "w").close()


def cmd_profile(args):
    """Choosing a mode stores it, so it survives reboots and sleeps. `guard` (the
    timer, udev and the watcher re-applying after the firmware clobbers the
    limits; not listed in the help) just re-applies the stored one."""
    if args.mode is None:
        print("%s  (omenctl profile auto|quiet|eco|perf)" % read_mode())
        return 0
    syslog.openlog("omenctl-profile")
    ops = Ops(args.dry_run)
    mode = args.mode
    if mode == "guard":
        apply_stored_mode(ops, quiet_if_recent=2.0)
        return 0
    if not ops.dry:
        set_mode(mode, ops)
    else:
        apply_mode(mode, ops)
    print({"auto": ">>> AUTO: follows the power source",
           "quiet": ">>> QUIET: auto with boost off (the power cap does the cooling)",
           "eco": ">>> ECO: battery envelope kept on AC",
           "perf": ">>> PERF: unlocked, stays until another mode is chosen"}[mode])
    return 0


# ------------------------------------------------------------------ watch --
# Re-applies the stored mode the instant the firmware clobbers the limits.
# Ported from power-profile-watch; two independent ways the limits leave the
# profile, with different signatures:
#
# 1. A write to platform_profile. inotify works on that attribute, so it is
#    caught the moment it happens (the EC re-applies its own limits within ~1 s).
# 2. Drift with NOTHING written (seen ~1-2 min after an apply, and after S3
#    resume): invisible to inotify, so the limits are polled. Reading them is
#    nearly free -- the SMU PM table is one read(), no SMU command, no ACPI call.
#
# The profile is only re-applied when the values actually move.

IN_MODIFY, IN_CLOSE_WRITE, IN_NONBLOCK = 0x2, 0x8, 0o4000
WATCH_DEBOUNCE = 0.4        # one write = MODIFY then CLOSE_WRITE: coalesce
WATCH_POLL = 1.0
WATCH_MIN_INTERVAL = 3.0    # never re-apply more often: a flapping EC must not spin us
PM_FIELDS = (0x00, 0x08, 0x10, 0x40)   # STAPM, PPT fast, PPT slow, Tctl


def pm_limits():
    """Rounded (STAPM, fast, slow, Tctl) from the PM table, or None."""
    raw = pm_read()
    if raw is None:
        return None
    return tuple(round(struct.unpack_from("<f", raw, o)[0], 1) for o in PM_FIELDS)


def describe(lim):
    return "limits unavailable" if lim is None else "/".join("%g" % v for v in lim[:3])


def drain(fd):
    try:
        while os.read(fd, 4096):
            pass
    except OSError:
        pass


def cmd_watch(_args):
    import ctypes
    import ctypes.util
    import select
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    syslog.openlog("omenctl-profile")
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    fd = libc.inotify_init1(IN_NONBLOCK)
    if fd < 0:
        sys.exit("omenctl watch: inotify_init1 failed")
    if libc.inotify_add_watch(fd, PLATFORM_PROFILE.encode(),
                              IN_MODIFY | IN_CLOSE_WRITE) < 0:
        sys.exit("omenctl watch: cannot watch %s (errno %d)"
                 % (PLATFORM_PROFILE, ctypes.get_errno()))
    last = [0.0]

    def reapply(reason):
        if time.monotonic() - last[0] < WATCH_MIN_INTERVAL:
            return False
        last[0] = t0 = time.monotonic()
        try:
            done = apply_stored_mode(Ops(False), quiet_if_recent=1.0)
            print("re-applied after %s in %.2fs" % (reason, time.monotonic() - t0)
                  if done else "skipped after %s: another apply just ran" % reason,
                  flush=True)
        except OSError as e:
            print("FAILED to re-apply after %s: %s" % (reason, e), flush=True)
        return True

    print("watching %s and the SMU limits (now %s)"
          % (PLATFORM_PROFILE, describe(pm_limits())), flush=True)
    # Known-good state first: a watcher started while the limits are already off
    # target would otherwise adopt the wrong values and never correct them.
    reapply("startup")
    known = pm_limits()
    print("baseline after startup apply: %s" % describe(known), flush=True)
    while not stop:
        r, _, _ = select.select([fd], [], [], WATCH_POLL)
        if r:                                             # 1. platform_profile write
            drain(fd)
            time.sleep(WATCH_DEBOUNCE)
            drain(fd)
            reapply("platform_profile -> %r" % sysfs(PLATFORM_PROFILE, "?"))
            known = pm_limits()
            continue
        cur = pm_limits()                                 # 2. silent drift
        if cur is not None and known is not None and cur != known:
            print("limits moved %s -> %s" % (describe(known), describe(cur)), flush=True)
            # adopt the new value only if we really re-applied; a debounced event
            # keeps the old one so the next tick retries
            if reapply("silent limit change"):
                known = pm_limits()
        elif cur is not None:
            known = cur
    return 0


def cmd_run(args):
    lock = open(LOCK, "w")                    # kept open (= held) until we exit
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("omenctl run: another instance already owns the fans "
              "(systemctl stop omenctl, or stop the other one)", flush=True)
        return 1
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    rig = Rig(args.gpu_offset, writable=True)
    fans = FANS

    regs = rig.ec_read(SET_CPU, 4)
    idx = {f["write_reg"]: initial_index(f["thr"], regs[f["write_reg"] - SET_CPU + 2])
           for f in fans}
    notified = False
    read_fails = write_fails = 0
    try:
        os.unlink(FAN_OVERRIDE)               # a fresh start is always on the curve
    except OSError:
        pass
    print("run: owning setpoints 0x2C/0x2D (contract: 255 on exit + watchdog)",
          flush=True)

    while not stop:
        try:
            cpu, gpu, regs = rig.temps()
            read_fails = 0
        except OSError as e:
            read_fails += 1
            print("read failed (%d): %s" % (read_fails, e), flush=True)
            try:
                rig.open()                         # descriptors may have gone stale
            except OSError:
                pass
            if read_fails >= 3:                    # fail safe: full speed
                for f in fans:
                    try:
                        rig.ec_write(f["write_reg"], 100)
                    except OSError:
                        pass
            time.sleep(1)
            continue

        critical = cpu >= CRITICAL_TEMP or gpu >= CRITICAL_TEMP
        manual = overrides_read()
        status = []
        for f in fans:
            wr = f["write_reg"]
            temp = fan_temp(f, cpu, gpu)
            idx[wr] = step(f["thr"], temp, idx[wr])     # keeps the curve state warm
            target = 100 if critical else f["thr"][idx[wr]][2]
            tag = ""
            if wr in manual and not critical:
                limit = OVERRIDE_MAX_TEMP if manual[wr] >= LOW_SPEED else LOW_SPEED_MAX_TEMP
                if temp >= limit:                       # too hot to trust a manual speed
                    left = {k: manual[r] for k, r in FAN_KEYS.items()
                            if r in manual and r != wr}
                    try:
                        overrides_write(left)
                    except OSError:
                        pass
                    print("manual speed dropped for 0x%02X: %.0f C >= %.0f C, back to "
                          "the curve" % (wr, temp, limit), flush=True)
                else:
                    target, tag = manual[wr], " manual"
            target = min(target, f["speed_max"])
            status.append("%s %d%%(%.0fC)%s" % (f["name"].split()[0], target, temp, tag))
            current = regs[wr - SET_CPU]
            if current != target:
                try:
                    rig.ec_write(wr, target)
                    write_fails = 0
                    print("set 0x%02X %d -> %d" % (wr, current, target), flush=True)
                except OSError as e:
                    write_fails += 1
                    print("write failed (%d): %s" % (write_fails, e), flush=True)
                    if write_fails >= 5:
                        print("giving up; unit stop path will reset",
                              flush=True)
                        return 1

        sd_notify("WATCHDOG=1\nSTATUS=%s" % " ".join(status))
        if not notified:
            sd_notify("READY=1")
            notified = True
        time.sleep(1)

    # clean stop: release the latch ourselves before the unit's stop path runs
    rig.close()
    args.raw = True                      # we ARE the daemon: only write the 255s
    return cmd_reset(args)


def check_model():
    """Refuse to touch the EC on hardware this was not measured on.

    OMENCTL_DMI_DIR only exists so the refusal can be tested without such a
    machine; it is not a way around the check."""
    root = os.environ.get("OMENCTL_DMI_DIR", "/sys/class/dmi/id")
    product = sysfs(root + "/product_name", "")
    board = sysfs(root + "/board_name", "")
    if product.startswith(SUPPORTED_PRODUCT) and board in SUPPORTED_BOARDS:
        return
    sys.exit(
        "omenctl: refusing to run. It writes embedded-controller registers whose "
        "layout was measured only on the %s*xxx (board %s).\n"
        "  this machine: product=%r board=%r\n"
        "On another model the same bytes can mean something else. If you own a "
        "%s*xxx with a different board and want it supported, open an issue with:\n"
        "  cat /sys/class/dmi/id/{product_name,board_name,bios_version}"
        % (SUPPORTED_PRODUCT, "/".join(SUPPORTED_BOARDS), product, board,
           SUPPORTED_PRODUCT))


def main():
    ap = argparse.ArgumentParser(
        prog="omenctl",
        description="Fans and power for the HP OMEN 15 (design notes: see the "
                    "top of omenctl.py and ../ec-map.md).")
    sub = ap.add_subparsers(dest="cmd", required=True, title="commands",
                            metavar="COMMAND")
    p = sub.add_parser("status", help="fans, power, battery (-w: live view)")
    p.add_argument("-w", "--watch", action="store_true",
                   help="refresh every second until Ctrl-C")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output (one line per second with -w)")
    p.add_argument("--gpu-offset", type=float, default=3.0, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("fan", help="manual fan speed: fan cpu|gpu|all 0-100|auto")
    p.add_argument("fan", nargs="?", choices=["cpu", "gpu", "all"])
    p.add_argument("speed", nargs="?", help="percent, or auto (the curve)")
    p.set_defaults(func=cmd_fan)

    p = sub.add_parser("tui", help="live terminal UI: modes as buttons (mouse or keys)")
    p.add_argument("--gpu-offset", type=float, default=3.0, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_tui)

    p = sub.add_parser("profile", formatter_class=argparse.RawDescriptionHelpFormatter,
                       help="apply a CPU/GPU power profile", description=PROFILE_HELP)
    p.add_argument("mode", nargs="?", default=None, metavar="MODE", type=profile_mode)
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="print what would be written/run, change nothing")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("boost", help="CPU boost on|off (off = quiet mode)")
    p.add_argument("state", nargs="?", default="status", choices=["on", "off", "status"])
    p.set_defaults(func=cmd_boost)

    for name, text in (("start", "take the fans (start the daemon)"),
                       ("stop", "stop the daemon: the EC takes the fans back"),
                       ("restart", "restart the daemon"),
                       ("enable", "start the daemon at every boot (and now)"),
                       ("disable", "don't start at boot (and stop now): the EC keeps the fans")):
        sub.add_parser(name, help=text).set_defaults(func=cmd_service)

    # internal: the systemd unit runs `run` and `reset --raw`; `reset` = `stop`
    p = sub.add_parser("watch")
    p.set_defaults(func=cmd_watch)
    p = sub.add_parser("run")
    p.add_argument("--gpu-offset", type=float, default=3.0, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("reset")
    p.add_argument("--raw", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_reset)

    args = ap.parse_args()
    check_model()
    if os.geteuid() != 0:
        sys.exit("root needed for the EC io file")
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
