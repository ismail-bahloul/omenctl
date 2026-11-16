#!/usr/bin/env python3
"""Omenctl -- fans and power for the HP OMEN 15.

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
    doctor   read-only self-check: model, EC, SMU, dGPU, WiFi, units, fans.
    profile  apply a CPU/GPU power profile (ac|battery|perf|quiet|eco|auto|guard).
             The result is read back; a mismatch is retried once and exit 1.
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
import json
import math
import os
import re
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
CRITICAL_TEMP = 100.0      # both fans to 100 % at or above this (last-resort fail-safe)
FAN_OVERRIDE = "/run/omenctl/fans"   # manual speeds, "cpu=40" / "gpu=auto" lines
OVERRIDE_MIN = 0           # % -- manual speeds span the EC's full 0-100 range
FAN_KEYS = {"cpu": SET_CPU, "gpu": SET_GPU}
CURVE_FILE = os.environ.get("OMENCTL_CURVE_FILE", "/etc/omenctl/fans.json")

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


def wifi_iface():
    """The first wireless netdev (``wlan0`` is not stable across machines)."""
    for path in sorted(glob.glob("/sys/class/net/*/wireless")):
        return os.path.basename(os.path.dirname(path))
    return None


def dgpu_pci():
    """The NVIDIA VGA/3D PCI function, by vendor and class (not by address)."""
    for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
        if (sysfs(dev + "/vendor") == "0x10de"
                and sysfs(dev + "/class") in ("0x030000", "0x030200")):
            return os.path.basename(dev)
    return None


def dgpu_driver():
    """The driver bound to the dGPU (nvidia, vfio-pci, ...), or None."""
    pci = dgpu_pci()
    if not pci:
        return None
    try:
        return os.path.basename(os.readlink("/sys/bus/pci/devices/%s/driver" % pci))
    except OSError:
        return None


def power_supply(kind):
    """The power_supply device of a given type (Battery, Mains), or None."""
    for dev in sorted(glob.glob("/sys/class/power_supply/*")):
        if sysfs(dev + "/type") == kind:
            return dev
    return None


def wifi_ps_state(iface):
    """'on' / 'off' as reported by iw, or None if it cannot be read."""
    if not iface:
        return None
    try:
        out = subprocess.run(["iw", "dev", iface, "get", "power_save"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "Power save: on" in out:
        return "on"
    if "Power save: off" in out:
        return "off"
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

# The embedded defaults, before any /etc/omenctl/fans.json override. The TUI
# editor's `r` restores one of these.
DEFAULT_THR = [[tuple(x) for x in f["thr"]] for f in FANS]


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


# --------------------------------------------------------------- fan curve --

def curve_error(thr):
    """Why `thr` is not a valid curve, or None. The rules keep the nbfc
    hysteresis shape: up strictly increases, down sits between the previous
    step's down and up, and duty never decreases with temperature."""
    if not thr:
        return "curve is empty"
    prev_up = prev_down = prev_duty = -1
    for i, row in enumerate(thr):
        try:
            up, down, duty = row
        except (TypeError, ValueError):
            return "step %d: bad row" % (i + 1)
        if not (0 <= down < up <= 120):
            return "step %d: need 0 <= down < up <= 120" % (i + 1)
        if up <= prev_up:
            return "step %d: up must increase" % (i + 1)
        if i and down < prev_down:
            return "step %d: down must not decrease" % (i + 1)
        if i and down > prev_up:
            return "step %d: down must stay below the previous up" % (i + 1)
        if not (0 <= duty <= 100):
            return "step %d: duty must be 0-100" % (i + 1)
        if duty < prev_duty:
            return "step %d: duty must not decrease" % (i + 1)
        prev_up, prev_down, prev_duty = up, down, duty
    return None


def curve_data():
    return {"version": 1,
            "cpu": [list(x) for x in FANS[0]["thr"]],
            "gpu": [list(x) for x in FANS[1]["thr"]]}


def load_curve_file():
    """Apply /etc/omenctl/fans.json to FANS. False on missing/invalid."""
    try:
        with open(CURVE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    try:
        curves = []
        for key in ("cpu", "gpu"):
            thr = [(int(a), int(b), int(c)) for a, b, c in data[key]]
            if curve_error(thr):
                return False
            curves.append(thr)
    except (KeyError, TypeError, ValueError):
        return False
    for i, thr in enumerate(curves):
        FANS[i]["thr"] = thr
    return True


def save_curve_file():
    """Validate both curves and replace the file atomically. None on success."""
    for key, fan in zip(("cpu", "gpu"), FANS):
        err = curve_error(fan["thr"])
        if err:
            return "%s: %s" % (key, err)
    try:
        directory = os.path.dirname(CURVE_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = CURVE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(curve_data(), f, indent=2)
            f.write("\n")
        os.replace(tmp, CURVE_FILE)
    except OSError as e:
        return "cannot write %s: %s" % (CURVE_FILE, e)
    return None


def curve_mtime():
    try:
        return os.stat(CURVE_FILE).st_mtime_ns
    except OSError:
        return None


def curve_current_index(thr, temp):
    """The step the temperature falls in. The daemon's hysteresis state can
    differ by one; the editor labels this as the temperature step."""
    if temp is None:
        return None
    idx = 0
    for i, row in enumerate(thr):
        if temp >= row[0]:
            idx = i
    return idx


def curve_duty_at(thr, temp):
    """The duty the curve asks for at `temp` (up thresholds, no hysteresis)."""
    idx = curve_current_index(thr, temp)
    return thr[idx][2] if idx is not None and thr else 0


def curve_graph(thr, width=40, height=8, tmin=30, tmax=100):
    """Temperature -> duty area chart as `height` rows of `width` cells.

    The area under the curve is filled from the bottom in eighths of a cell,
    so 8 * height duty levels fit. Returns (rows, codes): `codes[y][x]` is the
    severity colour of that column's temperature, or None for the empty part.
    The curve editor paints the cells; this function stays pure and testable.
    """
    if not thr or width < 2 or height < 1 or tmax <= tmin:
        return [], []
    rows = [["·"] * width for _ in range(height)]
    codes = [[None] * width for _ in range(height)]
    for x in range(width):
        temp = tmin + (tmax - tmin) * x / (width - 1)
        duty = curve_duty_at(thr, temp)
        eighths = int(round(duty / 100.0 * height * 8))
        code = _temp_code(temp)
        for yb in range(height):                 # yb: rows from the bottom
            fill = max(0, min(8, eighths - yb * 8))
            if not fill:
                break
            y = height - 1 - yb
            rows[y][x] = "█" if fill == 8 else SPARK[fill - 1]
            codes[y][x] = code
    return rows, codes


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
PARTIAL = " ▏▎▍▌▋▊▉"             # a cell filled in eighths, from the left


def _color(on):
    def paint(code, text):
        return "\033[%sm%s\033[0m" % (_ansi(code), text) if on else text
    return paint


# Semantic colours from the 256-colour cube (>= 16), so a terminal theme cannot
# remap them the way it remaps ANSI 30-37. The text markers (the `!` on a hot
# die, the TUI legend) carry the same meaning without colour.
GREEN, YELLOW, ORANGE, RED, BLUE, PURPLE = (
    "38;5;114", "38;5;221", "38;5;214", "38;5;203", "38;5;39", "38;5;111")
# Text and card edges in the TUI, tinted by the Konsole scheme (None = just dim them).
MUTED = OUTLINE = None
# index -> the exact #rrggbb it stands for, for a terminal that can show true colour
THEME_EXACT = {}


def _lab(r, g, b):
    """sRGB 0-255 -> CIELAB, where a distance is close to a perceived difference."""
    def linear(v):
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    def f(t):
        return t ** (1 / 3.0) if t > 0.008856 else 7.787 * t + 16 / 116.0
    r, g, b = linear(r), linear(g), linear(b)
    x = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047)
    y = f(0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    return 116 * y - 16, 500 * (x - y), 200 * (y - z)


def _hex_256(h):
    """Nearest-looking xterm-256 index (16-255) for a #rrggbb colour: plain RGB distance
    would trade a saturated accent for a grey-blue that merely has closer channels."""
    h = h.lstrip("#")
    want = _lab(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    steps = (0, 95, 135, 175, 215, 255)
    best = (float("inf"), 16)
    for i in range(16, 256):
        if i < 232:
            n = i - 16
            rgb = steps[n // 36], steps[n // 6 % 6], steps[n % 6]
        else:
            rgb = (8 + 10 * (i - 232),) * 3
        d = sum((p - q) ** 2 for p, q in zip(want, _lab(*rgb)))
        if d < best[0]:
            best = (d, i)
    return best[1]


def _theme_code(h):
    """A theme colour as a "38;5;N" code. N is its nearest 256-colour entry, or, when a
    different theme colour already took that entry, a number from 256 up: the TUI draws
    both exactly (THEME_EXACT), and _ansi() turns the code back into a real one."""
    index = _hex_256(h)
    while THEME_EXACT.get(index, h).lower() != h.lower():
        index = index + 1 if index >= 256 else 256
    THEME_EXACT[index] = h
    return "38;5;%d" % index


_INDEX_RE = re.compile(r"38;5;(\d+)")


def _code_index(code):
    """The colour index in a "38;5;N" code (bold may precede), or None."""
    m = _INDEX_RE.search(code or "")
    return int(m.group(1)) if m else None


def _ansi(code):
    """`code` as something a terminal understands (see _theme_code); bold may precede."""
    def real(m):
        index = int(m.group(1))
        return "38;5;%d" % (_hex_256(THEME_EXACT[index]) if index in THEME_EXACT and index >= 256
                            else index)
    return _INDEX_RE.sub(real, code)


def _mix_hex(a, b, t):
    """Blend two #rrggbb colours, t=0 -> a, t=1 -> b."""
    a, b = a.lstrip("#"), b.lstrip("#")
    av = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    bv = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(av, bv))


def _hls(h):
    import colorsys
    return colorsys.rgb_to_hls(*[int(h.lstrip("#")[i:i + 2], 16) / 255.0 for i in (0, 2, 4)])


def _palette_ramp(colors, steps=4):
    """`steps` colours picked from the scheme itself, coolest first, hottest last: the
    palette sorted by how far its hue is from red, near-duplicates dropped. A wallpaper
    with warm colours gives a real green-to-red ramp; a blue one gives a blue-to-lilac
    ramp. None if the scheme has too few distinct colours."""
    def warmth(c):                       # 0 = red-orange, 180 = the coldest hue
        return abs(((_hls(c)[0] * 360 - 10 + 180) % 360) - 180)
    ramp = []
    for c in sorted(set(colors), key=warmth, reverse=True):
        lab = _lab(*[int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)])
        if all(sum((x - y) ** 2 for x, y in zip(lab, o[1])) > 9 ** 2 for o in ramp):
            ramp.append((c, lab))
    if len(ramp) < steps:
        return None
    return [ramp[round(i * (len(ramp) - 1) / float(steps - 1))][0] for i in range(steps)]


def _user_home():
    home = os.path.expanduser("~")
    user = os.environ.get("SUDO_USER")
    if user:
        try:
            import pwd
            home = pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    return home


def _theme_path():
    """The KDE colour scheme Material You generated, or None."""
    env = os.environ.get("OMENCTL_THEME")
    if env:
        return None if env.lower() in ("none", "off", "0") else env
    home = _user_home()
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


def _konsole_theme_path():
    """The Konsole scheme used by Zed/btop/kitty/prompt, or None."""
    env = os.environ.get("OMENCTL_KONSOLE_THEME")
    if env:
        return None if env.lower() in ("none", "off", "0") else env
    home = _user_home()
    for name in ("MaterialYou.colorscheme", "MaterialYouAlt.colorscheme"):
        path = os.path.join(home, ".local", "share", "konsole", name)
        if os.path.exists(path):
            return path
    return None


def _konsole_color(path, section):
    import configparser
    c = configparser.ConfigParser()
    try:
        c.read(path)
        raw = c.get(section, "Color", fallback=None)
    except (OSError, configparser.Error):
        return None
    if not raw:
        return None
    try:
        r, g, b = [int(x) for x in raw.split(",")]
    except ValueError:
        return None
    return "#%02x%02x%02x" % (r, g, b)


def load_theme():
    """Material You colours, from the Konsole scheme: the source Zed, btop, kitty and
    the prompt all derive from, regenerated on every wallpaper change.

    Severity uses nothing else: the scheme's own colours, ordered from its coolest hue
    to its hottest (_palette_ramp). It is only a green-to-red ramp when the wallpaper
    has warm colours; on a blue wallpaper it runs blue to lilac, and the `!` on a hot
    die carries the alarm. Without a Konsole scheme (or with too few colours in it),
    the semantic roles of the KDE scheme are used (Positive, Neutral, Negative).
    """
    global GREEN, YELLOW, ORANGE, RED, BLUE, PURPLE, MUTED, OUTLINE
    THEME_EXACT.clear()

    path = _theme_path()
    if path:
        import configparser
        c = configparser.ConfigParser()
        try:
            c.read(path)
        except (OSError, configparser.Error):
            c = None
        if c is not None:
            def role(key):
                for section in ("Colors:View", "Colors:Button", "Colors:Window"):
                    if c.has_section(section) and c.has_option(section, key):
                        return c.get(section, key)
                return None

            pos = role("ForegroundPositive")
            neu = role("ForegroundNeutral")
            neg = role("ForegroundNegative")
            if pos:
                GREEN = _theme_code(pos)
            if neu:
                ORANGE = _theme_code(neu)
            if neg:
                RED = _theme_code(neg)
            if pos and neu:
                YELLOW = _theme_code(_mix_hex(pos, neu, 0.5))

    kpath = _konsole_theme_path()
    if kpath:
        accent = _konsole_color(kpath, "Color1") or _konsole_color(kpath, "Color1Intense")
        accent2 = _konsole_color(kpath, "Color3") or _konsole_color(kpath, "Color3Intense")
        if accent:
            BLUE = _theme_code(accent)
            hues = [_konsole_color(kpath, "Color%d%s" % (i, tone))
                    for i in range(1, 8) for tone in ("", "Intense")]
            ramp = _palette_ramp([h for h in hues if h])
            if ramp:
                GREEN, YELLOW, ORANGE, RED = [_theme_code(c) for c in ramp]
        if accent2:
            PURPLE = _theme_code(accent2)
        fg, bg = _konsole_color(kpath, "Foreground"), _konsole_color(kpath, "Background")
        if fg and bg:
            MUTED = _theme_code(_mix_hex(bg, fg, 0.6))
            OUTLINE = _theme_code(_mix_hex(bg, fg, 0.3))


def _temp_code(t):
    return GREEN if t < 60 else YELLOW if t < 75 else ORANGE if t < 85 else RED


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _vislen(text):
    """Display width of a painted string: ANSI escapes excluded."""
    return len(_ANSI_RE.sub("", text))


def _bar(frac, width, code, paint):
    """A gauge `width` cells wide, filled in eighths of a cell."""
    eighths = max(0, min(width * 8, round(frac * width * 8)))
    full, part = eighths // 8, eighths % 8
    fill = "█" * full + (PARTIAL[part] if part else "")
    return paint(code, fill) + paint("90", "░" * (width - len(fill)))


def _temp_gauge(temp, peak, width, paint):
    """Temperature on a 30-100 °C scale, each cell in the colour of the temperature
    it stands for; a tick marks `peak` while it is above the current reading."""
    def cells(t):
        return (max(30, min(100, t)) - 30) / 70.0 * width
    eighths = round(cells(temp) * 8)
    full, part = eighths // 8, eighths % 8
    tick = min(width - 1, int(cells(peak))) if peak > temp else -1
    out = []
    for x in range(width):
        code = _temp_code(30 + 70 * (x + 0.5) / width)
        if x < full:
            out.append(paint(code, "█"))
        elif x == full and part:
            out.append(paint(code, PARTIAL[part]))
        elif x == tick:
            out.append(paint("1;" + _temp_code(peak), "┃"))
        else:
            out.append(paint("90", "░"))
    return "".join(out)


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


def power_state(ra, rig, pkg_w, battery=None):
    """Platform/power facts for `status`, from sysfs and the SMU PM table. `battery`
    = (capacity, extender) if the caller already read them from the EC."""
    cap, extender = battery or (battery_capacity(rig), battery_extender(rig))
    cpu0 = "/sys/devices/system/cpu/cpu0/cpufreq/"
    gov = sysfs(cpu0 + "scaling_governor")
    fmax = read_int(cpu0 + "scaling_max_freq")
    ac = sysfs((power_supply("Mains") or "/sys/class/power_supply/ACAD") + "/online") == "1"
    drv = dgpu_driver()
    dpm = None
    for d in os.listdir("/sys/class/drm"):
        p = "/sys/class/drm/%s/device/" % d
        if os.path.exists(p + "pp_dpm_fclk"):
            dpm = sysfs(p + "power_dpm_force_performance_level")
            break
    bat = (power_supply("Battery") or "/sys/class/power_supply/BAT0") + "/"
    power_now = read_int(bat + "power_now")
    return {"gov": gov, "fmax": fmax, "boost": sysfs(BOOST) == "1", "ac": ac,
            "mode": read_mode(),
            "dgpu": drv, "dpm": dpm, "ra": ra, "pkg_w": pkg_w,
            "bat_pct": read_int(bat + "capacity"),
            "bat_status": sysfs(bat + "status"),
            "bat_w": power_now / 1e6 if power_now else None,
            "bat_wh": (read_int(bat + "energy_now") or 0) / 1e6,
            "bat_cap": cap,
            "bat_extender": extender,
            "bat_cycles": read_int(bat + "cycle_count"),
            "profile": sysfs("/sys/firmware/acpi/platform_profile")}


def _section(title, width, paint):
    return (paint("90", "─ ") + paint("1;" + BLUE, title)
            + paint("90", " " + "─" * max(4, width - len(title) - 4)))


def _row(label, text, paint):
    return "  " + paint("90", "%-8s" % label) + text


def render_power(p, bar_w, paint, width):
    ra = p["ra"]
    lines = ["", _section("Power", width, paint)]
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
    lines.append(_row("cpu", "%s · max %s · %s" % (p["gov"], ghz, boost), paint))
    if ra:
        lines.append(_row("limits", paint("90", "slow %g W · fast %g W · apu %g W · tctl %g °C" % (
            ra.get("PPT LIMIT SLOW", 0), ra.get("PPT LIMIT FAST", 0),
            ra.get("PPT LIMIT APU", 0), ra.get("THM LIMIT CORE", 0))), paint))
    d = {"nvidia": "nvidia (active)", "vfio-pci": "vfio-pci (off)"}.get(p["dgpu"], p["dgpu"])
    lines.append(_row("gpu", "dGPU %s · iGPU dpm %s" % (d or "none", p["dpm"] or "?"), paint))
    if p["bat_pct"] is not None:
        # on AC the sensor reads the power flowing INTO the battery (what btop/KDE
        # show), not what the machine draws -- the adapter reports nothing
        into_battery = (" · %.1f W into the battery" % p["bat_w"]
                        if p["bat_status"] == "Charging" and p["bat_w"] else "")
        lines += ["", _section("Battery", width, paint)]
        lines.append(_row("charge", "%s %s" % (paint("1", "%d%%" % p["bat_pct"]),
                                               paint("90", p["bat_status"] + into_battery)),
                          paint))
        if p["bat_cap"]:
            design, full = p["bat_cap"]
            health = 100.0 * full / design
            code = GREEN if health >= 80 else YELLOW if health >= 60 else RED
            cycles = " · %d cycles" % p["bat_cycles"] if p["bat_cycles"] else ""
            lines.append(_row("health", "%s %s" % (
                paint("1;" + code, "%.0f%%" % health),
                paint("90", "(%.1f / %.1f Wh%s)" % (full, design, cycles))), paint))
        if p["bat_extender"] is not None:
            lines.append(_row("limit", paint(YELLOW, "adaptive extender on (BIOS caps the charge)")
                              if p["bat_extender"] else paint("90", "adaptive extender off"),
                              paint))
    return lines


def render(fans, cpu, gpu, regs, rpms, hist, width, paint, gpu_offset, ec57, power, live,
           manual=None):
    """One frame of `status`, as a list of lines, at most 78 columns wide."""
    width = max(40, min(width, 78)) - 2
    bar_w = max(8, min(24, width - 46))
    mode = current_mode(power).upper()
    src = "AC" if power["ac"] else "BATTERY"
    right = (paint("1;" + BLUE, mode) + paint("90", " · ")
             + paint(GREEN if power["ac"] else YELLOW, "●") + paint("1", " " + src))
    pad = max(1, width - _vislen(" Omenctl") - _vislen(right))
    lines = [paint("1", " Omenctl") + " " * pad + right, "", _section("Thermal", width, paint)]
    for f in fans:
        is_cpu = f["write_reg"] == SET_CPU
        temp = cpu if is_cpu else gpu
        setp = regs[f["write_reg"] - SET_CPU]
        label = "CPU" if is_cpu else "GPU"
        warn = "!" if temp >= 85 else " "
        head = "%s %s  " % (paint("1", label),
                            paint("1;" + _temp_code(temp), "%4.0f°C%s" % (temp, warn)))
        if setp == RESET_VALUE:
            fan = paint(ORANGE, "EC auto (not owned by omenctl)")
        else:
            rpm = rpms[0 if is_cpu else 1]
            fan = "%s %s %s" % (_bar(setp / f["speed_max"], bar_w, BLUE, paint),
                                paint("1", "%3d%%" % setp),
                                paint("90", "%5s rpm" % (rpm if rpm is not None else "?")))
        if manual and f["write_reg"] in manual and setp != RESET_VALUE:
            fan += paint(ORANGE, " manual")
        # the temperature gauge takes what is left after the fan gauge and the numbers
        room = width - (bar_w + 35)
        peak = max(hist[label][-PEAK_SAMPLES:], default=temp)
        gauge = _temp_gauge(temp, peak, room, paint) if room >= 8 else ""
        lines.append("  " + head + fan + (("  " + gauge) if gauge else ""))
    lines += render_power(power, bar_w, paint, width)
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


BATTERY_EVERY = 30.0        # s -- battery capacity and charge cap, read from the EC
HISTORY_SAMPLES = 1000      # enough for a chart across a very wide window
PEAK_SAMPLES = 120          # the "recent peak": 2 minutes at one sample a second


class Monitor:
    """Everything `status` shows, sampled once per call: the single source for the
    text view, the JSON output and the TUI."""

    def __init__(self, gpu_offset):
        self.gpu_offset = gpu_offset
        self.rig = Rig(gpu_offset)
        self.pkg = PkgPower()
        self.hist = {"CPU": [], "GPU": []}
        self.battery, self.battery_at = None, 0.0

    def sample(self, pkg_settle=0.3):
        rig = self.rig
        cpu, gpu, regs = rig.temps()
        for label, t in (("CPU", cpu), ("GPU", gpu)):
            self.hist[label] = (self.hist[label] + [t])[-HISTORY_SAMPLES:]
        rpms = [read_int(rig.hp + "/fan%d_input" % i) if rig.hp else None for i in (1, 2)]
        # 7 of the 13 EC bytes a sample would read, for values that barely ever change
        if self.battery is None or time.monotonic() - self.battery_at >= BATTERY_EVERY:
            self.battery = (battery_capacity(rig), battery_extender(rig))
            self.battery_at = time.monotonic()
        return {"cpu": cpu, "gpu": gpu, "regs": regs, "rpms": rpms,
                "manual": overrides_read(),
                "ec57": rig.ec_read(CPU_TEMP)[0],
                "power": power_state(pm_limit_values(), rig, self.pkg.watts(settle=pkg_settle),
                                     self.battery)}

    def lines(self, d, width, paint, live):
        return render(FANS, d["cpu"], d["gpu"], d["regs"], d["rpms"], self.hist, width,
                      paint, self.gpu_offset, d["ec57"], d["power"], live, d["manual"])


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
    """Manual fan speed. The value is applied as asked at any temperature; only
    the critical fail-safe (both fans 100 % at CRITICAL_TEMP) overrides it.
    Manual speeds are forgotten when the daemon restarts or on reboot."""
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
          "%d%% manual" % value))
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
# `omenctl tui`: a dashboard of its own (`status` keeps the plain render() layout).
# Hardware reads and slow actions run on a worker thread, so input never waits on
# them; the screen is only redrawn when something changed, and gauges ease towards
# new values at up to 30 frames per second, only for changes large enough to see.

def _attr_for(code, pairs, color=True):
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
            if color:
                attr |= curses.color_pair(pairs.get(int(toks[i + 2]), pairs[3]))
            i += 2
        elif color and t.isdigit() and 31 <= int(t) <= 37:
            attr |= curses.color_pair(pairs[int(t) - 30])
        i += 1
    return attr


TUI_MODES = (("1", "auto"), ("2", "quiet"), ("3", "eco"), ("4", "perf"))


FAN_ROWS = (("cpu", "CPU", SET_CPU, 0), ("gpu", "GPU", SET_GPU, 1))
SAMPLE_EVERY = 1.0          # s -- hardware is read at this pace, off the UI thread
DRAG_WRITE_EVERY = 0.04     # s -- at most ~25 file writes/s while a fader is dragged
TUI_FRAME = 0.033           # s -- frame time while a gauge is moving
TUI_IDLE = 0.25             # s -- how often an idle UI looks for a new sample
TUI_GLIDE_FROM = 3.0        # °C or % -- smaller changes are drawn at once, not animated
TOAST_SECS = 5.0            # s -- how long a message stays on the status line


def use_direct_color():
    """Before curses starts: pick the terminal description that gives true colour, so
    the theme is drawn exactly and not as its nearest 256-colour entries. The semicolon
    form of konsole-direct is the one every true-colour terminal understands; under sudo
    COLORTERM is usually gone, but kitty still says who it is."""
    term = os.environ.get("TERM", "")
    if term == "xterm-kitty":
        direct = "kitty-direct"
    elif (os.environ.get("COLORTERM") in ("truecolor", "24bit")
          and term.split("-")[0] in ("xterm", "konsole")):
        direct = "konsole-direct"
    else:
        return
    for base in (os.path.expanduser("~/.terminfo"), "/etc/terminfo", "/usr/share/terminfo",
                 "/lib/terminfo", "/usr/lib/terminfo"):
        if os.path.exists(os.path.join(base, direct[0], direct)):
            os.environ["TERM"] = direct
            return


def init_pairs():
    """Initialise the curses colour pairs from the theme roles (load_theme first)."""
    import curses
    pairs = {}
    for n in range(1, 8):
        curses.init_pair(n, n, -1)
        pairs[n] = n
    if curses.COLORS >= 256:
        codes = {_code_index(c) for c in (GREEN, YELLOW, ORANGE, RED, BLUE, PURPLE, MUTED, OUTLINE)}
        for i, code in enumerate(sorted(c for c in codes if c is not None), start=8):
            if curses.COLORS > 256 and code in THEME_EXACT:   # true colour: the real value
                curses.init_pair(i, int(THEME_EXACT[code].lstrip("#"), 16), -1)
                pairs[code] = i
            else:                                  # 256 colours: the nearest entry
                real = _hex_256(THEME_EXACT[code]) if code >= 256 else code
                if real < curses.COLORS:
                    curses.init_pair(i, real, -1)
                    pairs[code] = i
    return pairs


class TuiCanvas:
    """Curses set up for Omenctl, and what the TUI and the curve view draw with."""

    def __init__(self, stdscr):
        import curses
        self.win = stdscr
        curses.set_escdelay(25)      # ms ncurses waits to tell Esc from an escape sequence
        curses.curs_set(0)
        curses.use_default_colors()
        load_theme()
        self.pairs = init_pairs()
        self.color = not os.environ.get("NO_COLOR")
        self.attrs = {}
        self.width = stdscr.getmaxyx()[1]
        self.theme_seen = self._theme_stamp()
        curses.mouseinterval(0)                   # raw press/release, no click synthesis
        curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED
                         | curses.BUTTON1_RELEASED | curses.REPORT_MOUSE_POSITION
                         | getattr(curses, "BUTTON4_PRESSED", 0)
                         | getattr(curses, "BUTTON5_PRESSED", 0))
        # ncurses only turns on mouse mode 1000 (press/release): a terminal then never
        # reports motion, so a drag would only show up on release. 1002 adds "motion
        # while a button is held"; the commands turn it off again on the way out.
        sys.stdout.write("\x1b[?1002h")
        sys.stdout.flush()

    @staticmethod
    def _theme_stamp():
        try:
            return os.stat(_konsole_theme_path() or "").st_mtime
        except OSError:
            return None

    def follow_theme(self):
        """Take a new wallpaper's colours. Cheap: call it once per sample."""
        stamp = self._theme_stamp()
        if stamp != self.theme_seen:
            self.theme_seen = stamp
            load_theme()
            self.pairs = init_pairs()
            self.attrs.clear()

    def begin(self):
        """Start a frame. -> (height, width)."""
        self.win.erase()
        h, self.width = self.win.getmaxyx()
        return h, self.width

    def attr(self, code):
        if code not in self.attrs:
            themed = self.color and (MUTED if code == "90" else OUTLINE if code == "edge" else None)
            self.attrs[code] = _attr_for(themed or ("90" if code == "edge" else code),
                                         self.pairs, self.color)
        return self.attrs[code]

    def put(self, y, x, text, a=0):       # a too-small terminal must not crash us
        import curses
        try:
            self.win.addnstr(y, x, text, max(0, self.width - x), a)
        except curses.error:
            pass

    def segs(self, y, x, segs, room):
        """Draw [(code, text)], clipped to `room` cells, merging equal neighbours (a
        gauge is one addstr per colour, not one per cell). -> x after the text."""
        merged = []
        for code, text in segs:
            if merged and merged[-1][0] == code:
                merged[-1][1] += text
            else:
                merged.append([code, text])
        for code, text in merged:
            if room <= 0:
                break
            text = text[:room]
            self.put(y, x, text, self.attr(code))
            x += len(text)
            room -= len(text)
        return x

    def runs(self, y, x, row, codes):
        """Draw a row with one colour code per column, skipping blanks: one addstr per
        run of a colour."""
        start = None
        for c in range(len(row) + 1):
            inside = c < len(row) and row[c] != " "
            if start is not None and (not inside or codes[c] != codes[start]):
                self.put(y, x + start, row[start:c], self.attr(codes[start] or ""))
                start = None
            if inside and start is None:
                start = c

    def card(self, y, x, ch, cw, title, code=None, note=None):
        edge = self.attr("edge")
        self.put(y, x, "╭─ ", edge)
        self.put(y, x + 3, title, self.attr("1;" + (code or BLUE)))
        tx = x + 3 + len(title)
        if note:
            self.put(y, tx, " " + note[0], self.attr(note[1]))
            tx += len(note[0]) + 1
        self.put(y, tx, " " + "─" * max(0, x + cw - tx - 2) + "╮", edge)
        for yy in range(y + 1, y + ch - 1):
            self.put(yy, x, "│", edge)
            self.put(yy, x + cw - 1, "│", edge)
        self.put(y + ch - 1, x, "╰" + "─" * max(0, cw - 2) + "╯", edge)


class TuiFeed:
    """Reads the sensors and runs slow actions on one worker thread, one at a time,
    so a mode change never overlaps a read and the UI never waits for either."""

    def __init__(self, mon):
        import queue
        import threading
        self.mon = mon
        self.jobs = queue.Queue()
        self.empty = queue.Empty
        self.lock = threading.Lock()
        self.fresh = None             # (sample, jobs finished before it was read)
        self.notes = []
        self.submitted = self.finished = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, job):
        """Queue job() -> message; the returned ticket is passed by take() once a
        sample read after the job is available."""
        self.submitted += 1
        self.jobs.put(job)
        return self.submitted

    def take(self):
        """-> (newest unseen sample or None, tickets it covers, messages)."""
        with self.lock:
            fresh, self.fresh = self.fresh, None
            notes, self.notes = self.notes, []
        sample, done = fresh or (None, 0)
        return sample, done, notes

    def stop(self, timeout=10.0):
        """Let a running job finish: a mode change must not die half-applied."""
        self.jobs.put(None)
        self.thread.join(timeout)

    def _note(self, text):
        with self.lock:
            self.notes.append(text)

    def _run(self):
        next_read = time.monotonic() + SAMPLE_EVERY
        while True:
            try:
                job = self.jobs.get(timeout=max(0.0, next_read - time.monotonic()))
            except self.empty:
                job = False
            if job is None:
                return
            if job:
                try:
                    self._note(job())
                except (Exception, SystemExit) as e:   # keep the UI alive and say so
                    self._note("failed: %s" % e)
                self.finished += 1
            try:
                sample = self.mon.sample()
            except Exception as e:                     # keep showing the last values
                self._note("sensor read failed: %s" % e)
            else:
                with self.lock:
                    self.fresh = (sample, self.finished)
            next_read = time.monotonic() + SAMPLE_EVERY


def _ease(cur, target, dt, tau=0.12, snap=0.1):
    """One animation step of `cur` towards `target` (exponential, `tau` s)."""
    cur += (target - cur) * (1.0 - math.exp(-dt / tau))
    return target if abs(target - cur) < snap else cur


def _trend(hist):
    """'↗' / '↘' / '→' from the last 20 samples, '' with too few of them."""
    if len(hist) < 6:
        return ""
    recent, before = hist[-5:], hist[-20:-5]
    delta = sum(recent) / len(recent) - sum(before) / len(before)
    return "↗" if delta >= 1.5 else "↘" if delta <= -1.5 else "→"


def _temp_graph(hist, width, height, min_span=20):
    """A filled history chart, one column per sample, newest at the right. The scale
    uses round tens of °C and spans `min_span` at least, so it reads like an axis and
    does not jump with every sample.
    -> (rows of text, one colour code per column, scale top, scale bottom)."""
    if width < 1 or height < 1 or not hist:
        return [], [], 0, 0
    samples = hist[-width:]
    lo = int(min(samples) // 10 * 10)
    hi = int(-(-max(samples) // 10) * 10)
    hi = max(hi, lo + min_span)                  # headroom above: heat is what matters
    pad = width - len(samples)
    rows = [[" "] * width for _ in range(height)]
    for i, t in enumerate(samples):
        level = max(1, round((t - lo) / float(hi - lo) * height * 8))  # eighths of a cell
        for r in range(height):
            fill = min(8, max(0, level - (height - 1 - r) * 8))
            if fill:
                rows[r][pad + i] = SPARK[fill - 1]
    codes = [None] * pad + [_temp_code(t) for t in samples]
    return ["".join(r) for r in rows], codes, hi, lo


def _tui_power_rows(p):
    """The Power card, most useful row first: (label, gauge or None, [(code, text)]),
    where gauge = (animation key, fraction, colour code)."""
    ra, rows = p["ra"], []
    pkg, lim = p["pkg_w"], ra.get("STAPM LIMIT")
    if pkg is not None and lim:
        rows.append(("cpu pkg", ("pkg", pkg / lim, PURPLE),
                     [("1", "%4.1f" % pkg), ("90", " / %g W" % lim)]))
    if p["bat_w"] and p["bat_status"] == "Discharging":
        hours = p["bat_wh"] / p["bat_w"]
        # fixed 0-60 W reading scale: the machine has no system power limit
        rows.append(("system", ("system", p["bat_w"] / 60.0, YELLOW),
                     [("1", "%4.1f W" % p["bat_w"]),
                      ("90", "  ~%dh%02d" % (hours, (hours % 1) * 60))]))
    ghz = "%.2f GHz" % (p["fmax"] / 1e6) if p["fmax"] else "?"
    rows.append(("cpu", None, [("", "%s · %s · " % (p["gov"], ghz)),
                               (GREEN, "boost on") if p["boost"] else (YELLOW, "boost off")]))
    dgpu = {"nvidia": "nvidia (active)", "vfio-pci": "vfio-pci (off)"}.get(p["dgpu"], p["dgpu"])
    rows.append(("gpu", None, [("", "%s · iGPU %s" % (dgpu or "none", p["dpm"] or "?"))]))
    return rows


def _tui_battery_rows(p):
    """The Battery card, same shape as _tui_power_rows(); empty without a battery."""
    pct = p["bat_pct"]
    if pct is None:
        return []
    code = GREEN if pct >= 30 else YELLOW if pct >= 15 else RED
    # on AC the sensor reads the power flowing INTO the battery (what btop/KDE show),
    # not what the machine draws -- the adapter reports nothing
    into = (" · %.1f W in" % p["bat_w"] if p["bat_status"] == "Charging" and p["bat_w"] else "")
    rows = [("charge", ("battery", pct / 100.0, code),
             [("1", "%3d%%" % pct), ("90", " %s%s" % (p["bat_status"] or "?", into))])]
    if p["bat_cap"]:
        design, full = p["bat_cap"]
        health = 100.0 * full / design
        code = GREEN if health >= 80 else YELLOW if health >= 60 else RED
        cycles = " · %d cycles" % p["bat_cycles"] if p["bat_cycles"] else ""
        rows.append(("health", None, [("1;" + code, "%.0f%%" % health),
                                      ("90", " %.1f / %.1f Wh%s" % (full, design, cycles))]))
    if p["bat_extender"]:
        rows.append(("limit", None, [(YELLOW, "adaptive extender on")]))
    return rows


def _tui_plan(h, w, power_rows, battery_rows, debug=False):
    """What fits in an h x w window. The fan faders always stay; then the temperatures,
    then the Power/Battery rows, and all the height left goes to the charts: side by
    side on a wide window, stacked on a narrow one that is tall enough."""
    cols = 2 if w >= 72 else 1
    left = h - 4 - (1 if debug else 0) - 4        # header+gap, message+hint, Fans card
    base = 3 if cols == 2 else 4
    thermal = left >= base
    if thermal:
        left -= base
    want = max(power_rows, battery_rows) if cols == 2 else power_rows + battery_rows
    panel = max(0, min(want, left - 2))
    if panel:
        left -= panel + 2
    if cols == 2:
        graph = left if thermal and left >= 2 else 0
    else:                                         # two cards instead of a shared one: +2
        graph = (left - 2) // 2 if thermal and left >= 6 else 0
    return {"cols": cols, "thermal": thermal, "panel": panel, "graph": graph}


def tui_main(stdscr, args):
    import contextlib
    import curses
    import io
    canvas = TuiCanvas(stdscr)
    put, attr, card, segs_at = canvas.put, canvas.attr, canvas.card, canvas.segs
    wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
    wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)
    # The RAPL power meter averages over 0.3 s on its first call; that is useful
    # for `status` but would leave the TUI blank. Draw something first, then take
    # the first sample without the settle (the next one, one second later, is
    # accurate).
    stdscr.erase()
    try:
        stdscr.addstr(0, 1, " Omenctl", curses.A_BOLD)
        stdscr.addstr(2, 2, "reading sensors…", curses.A_DIM)
    except curses.error:
        pass
    stdscr.refresh()
    mon = Monitor(args.gpu_offset)
    syslog.openlog("omenctl-profile")
    d = mon.sample(pkg_settle=0.0)
    d["manual"] = overrides_read()
    feed = TuiFeed(mon)

    msg, msg_until, sel, debug = "", 0.0, 0, False
    panels = (None, [], [])      # (sample, Power rows, Battery rows): rebuilt per sample
    drag = None                  # {"i": fan row, "val": %, "wrote": %, "t": s, "x0", "tw"}
    buttons = []                 # (y, x0, x1, action) of the frame on screen
    anim = {}                    # animation key -> value on screen
    moving = False               # some gauge has not reached its value yet
    editor = None                # the curve view, while it is open
    dirty = True
    last_frame = time.monotonic()
    frame_dt = 0.0
    pending_mode, pending_ticket = None, 0

    graphs = {}                  # (position, size) -> (history, its _temp_graph())
    gliding = set()              # animation keys on their way to a far-away value

    def say(text):
        nonlocal msg, msg_until
        msg, msg_until = text, time.monotonic() + TOAST_SECS

    def eased(key, target):
        """The value to draw this frame for `key`, which is heading to `target`."""
        nonlocal moving
        cur = anim.get(key, target)
        if key not in gliding and abs(target - cur) < TUI_GLIDE_FROM:
            anim[key] = target                 # sensor noise: not worth 10 frames
            return target
        gliding.add(key)
        value = _ease(cur, target, frame_dt)
        anim[key] = value
        if value == target:
            gliding.discard(key)
        else:
            moving = True
        return value

    def collect():
        """A paint() for _bar()/_temp_gauge() that records segments, -> (paint, segs)."""
        segs = []

        def paint(code, text):
            segs.append((code, text))
            return text
        return paint, segs

    def temp_row(y, x, room, name, temp, hist):
        """'48°C ↗ ███▏░░░░░ max 71°', optionally preceded by the sensor's name."""
        peak = max(hist[-PEAK_SAMPLES:], default=temp)
        code = _temp_code(temp)
        if name:
            put(y, x, name, attr("1"))
            x += len(name) + 1
            room -= len(name) + 1
        put(y, x, "%3.0f°C" % temp, attr("1;" + code))
        put(y, x + 5, "!" if temp >= 85 else " ", attr("1;" + code))
        put(y, x + 6, _trend(hist) or " ", attr("90"))
        tail = "max %.0f°" % peak
        width = room - 8 - len(tail) - 1
        if width >= 4:
            paint, segs = collect()
            shown = eased("temp%d,%d" % (y, x), temp)
            _temp_gauge(shown, shown, width, paint)
            segs_at(y, x + 8, segs, width)
            put(y, x + 8 + width + 1, tail, attr("90"))

    def graph(y, x, ch, room, hist):
        """The history under a temperature: its scale at the right, its time span
        in the bottom edge of the card."""
        width = room - 4
        key = (y, x, ch, room)
        if graphs.get(key, (None,))[0] is not hist:   # new samples replace the list
            graphs[key] = (hist, _temp_graph(hist, width, ch))
        rows, codes, top, bottom = graphs[key][1]
        for r, row in enumerate(rows):
            canvas.runs(y + r, x, row, codes)
        if rows:
            taken = set()
            for v in [top, bottom] + list(range(bottom + 10, top, 10)):   # ends first
                line = round((top - v) / float(top - bottom) * (ch - 1))
                if not taken & {line - 1, line, line + 1} or v in (top, bottom):
                    taken.add(line)
                    put(y + line, x + width + 1, "%d°" % v, attr("90"))
            span = "%d s" % (min(len(hist), width) * SAMPLE_EVERY)
            put(y + ch, x + 1, " %s ago " % span, attr("90"))
            put(y + ch, x + width - 4, " now ", attr("90"))

    def info_card(y, x, ch, cw, title, code, rows):
        card(y, x, ch, cw, title, code)
        for r, (label, gauge, segs) in enumerate(rows[:ch - 2]):
            yy, xx, room = y + 1 + r, x + 2, cw - 4
            put(yy, xx, "%-8s" % label, attr("90"))
            xx += 8
            room -= 8
            used = sum(len(t) for _c, t in segs)
            width = room - used - 1
            if gauge and width >= 6:
                paint, cells = collect()
                _bar(eased(gauge[0], max(0.0, min(1.0, gauge[1]))), width, gauge[2], paint)
                segs_at(yy, xx, cells, width)
                xx += width + 1
                room -= width + 1
            segs_at(yy, xx, segs, room)

    def fans_card(y, width):
        owned = d["regs"][0] != RESET_VALUE
        card(y, 0, 4, width, "Fans", None,
             None if owned else ("· EC auto, not owned by omenctl (f to take over)", ORANGE))
        for i, (_key, label, reg, ri) in enumerate(FAN_ROWS):
            yy = y + 1 + i
            manual_v = d["manual"].get(reg)
            dragged = drag is not None and drag["i"] == i
            if dragged:
                manual_v = drag["val"]
            # with the daemon stopped the EC decides: no speed of ours is in effect
            known = owned or dragged
            value = (OVERRIDE_MIN if not known
                     else manual_v if manual_v is not None else d["regs"][ri])
            selected = i == sel
            put(yy, 2, "▸" if selected else " ", attr("1;" + BLUE))
            put(yy, 4, label, attr("1") if selected else (0 if owned else attr("90")))
            if manual_v is None:
                put(yy, 8, "● auto", attr("" if owned else "90"))
            else:
                put(yy, 8, "○ manual", attr(ORANGE if owned else "90"))
            buttons.append((yy, 8, 16, ("auto_toggle", i)))
            tx = 18
            tw = max(10, width - tx - 18)
            # a dragged knob stays under the mouse; anything else glides to its value
            shown = value if dragged else eased("fan%d" % i, value)
            if dragged:
                anim["fan%d" % i] = value
            frac = max(0.0, min(1.0, (shown - OVERRIDE_MIN) / float(100 - OVERRIDE_MIN)))
            knob = round(frac * (tw - 1))
            live = owned and manual_v is not None
            fill = attr(("1;" + BLUE) if live else BLUE if owned else "90")
            put(yy, tx, "━" * knob, fill)
            put(yy, tx + knob, "●" if live else "○", fill)
            put(yy, tx + knob + 1, "─" * (tw - knob - 1), attr("90"))
            buttons.append((yy, tx, tx + tw, ("track", i, tx, tw)))
            put(yy, tx + tw + 2, "%3d%%" % value if known else "   —",
                attr("1") if owned else attr("90"))
            rpm = d["rpms"][ri]
            if tx + tw + 16 < width:             # a tiny window keeps the percentage
                put(yy, tx + tw + 7, "%5s rpm" % (rpm if rpm is not None else "?"), attr("90"))

    def header(w):
        put(0, 1, "Omenctl", attr("1"))
        owned = d["regs"][0] != RESET_VALUE
        shown_mode = pending_mode or d["power"]["mode"]
        x = 10
        for k, m in TUI_MODES:
            label = " %s %s " % (k, m)
            if m == shown_mode:
                put(0, x, label, attr("1;" + BLUE) | curses.A_REVERSE)
            else:
                put(0, x, label, attr("90"))
            buttons.append((0, x, x + len(label), ("mode", m)))
            x += len(label) + 1
        label = " f %s " % ("stop fans" if owned else "start fans")
        src = "AC" if d["power"]["ac"] else "battery"
        right = w - 3 - len(src)
        if x + 2 + len(label) < w:
            bx = x + 2
            put(0, bx, label, attr("") if owned else attr(ORANGE) | curses.A_REVERSE)
            buttons.append((0, bx, bx + len(label), ("fans", owned)))
            if right > bx + len(label):
                put(0, right, "●", attr(GREEN if d["power"]["ac"] else YELLOW))
                put(0, right + 2, src, attr("1"))

    def draw():
        nonlocal moving, frame_dt, last_frame, panels
        now = time.monotonic()
        frame_dt, last_frame = min(0.05, now - last_frame), now
        moving = False
        buttons.clear()
        h, w = canvas.begin()
        if editor:
            editor.draw(canvas, h, w, d["cpu"], d["gpu"])
            if msg and not editor.msg and now < msg_until:   # e.g. a failed sensor read
                put(h - 2, 2, msg[:max(0, w - 3)], attr("1"))
            stdscr.refresh()
            return
        if panels[0] is not d:                 # the rows only change with a new sample
            panels = (d, _tui_power_rows(d["power"]), _tui_battery_rows(d["power"]))
        _d, power_rows, battery_rows = panels
        plan = _tui_plan(h, w, len(power_rows), len(battery_rows), debug)
        header(w)
        y = 2
        temps = (("CPU", d["cpu"]), ("GPU", d["gpu"]))
        if plan["thermal"] and (plan["cols"] == 2 or plan["graph"]):
            ch, left_w = 3 + plan["graph"], w // 2
            if plan["cols"] == 2:
                spots = ((y, 0, left_w), (y, left_w, w - left_w))
                y += ch
            else:
                spots = ((y, 0, w), (y + ch, 0, w))
                y += 2 * ch
            for (name, temp), (cy, x, cw) in zip(temps, spots):
                card(cy, x, ch, cw, name, _temp_code(temp))
                hist = mon.hist[name]
                # the card's title names the sensor; the key keeps the animation apart
                temp_row(cy + 1, x + 2, cw - 4, None, temp, hist)
                if plan["graph"]:
                    graph(cy + 2, x + 2, plan["graph"], cw - 4, hist)
        elif plan["thermal"]:
            card(y, 0, 4, w, "Temperature")
            for r, (name, temp) in enumerate(temps):
                temp_row(y + 1 + r, 2, w - 4, name, temp, mon.hist[name])
            y += 4
        fans_card(y, w)
        y += 4
        if plan["panel"]:
            ch = plan["panel"] + 2
            if plan["cols"] == 1 or not battery_rows:
                rows = power_rows[:1] + battery_rows[:1] + power_rows[1:] + battery_rows[1:]
                info_card(y, 0, ch, w, "Power", PURPLE, rows)
            else:
                left_w = w // 2
                info_card(y, 0, ch, left_w, "Power", PURPLE, power_rows)
                info_card(y, left_w, ch, w - left_w, "Battery", GREEN, battery_rows)
        if debug:
            ra = d["power"]["ra"]
            put(h - 3, 2, "ec57=%d ec49=%d (+%g) · slow %g fast %g apu %g W tctl %g °C" % (
                d["ec57"], d["gpu"] - mon.gpu_offset, mon.gpu_offset, ra.get("PPT LIMIT SLOW", 0),
                ra.get("PPT LIMIT FAST", 0), ra.get("PPT LIMIT APU", 0),
                ra.get("THM LIMIT CORE", 0)), attr("90"))
        if msg and time.monotonic() < msg_until:
            put(h - 2, 2, msg[:max(0, w - 3)], attr("1"))
        put(h - 1, 2, "←→ speed · ↑↓ fan · a auto · c curve · q quit", attr("90"))
        stdscr.refresh()

    def pct(value):
        return max(OVERRIDE_MIN, min(100, int(value)))

    def speed_now(i):
        """Fan row i's speed: the manual one if set, else what the EC reports."""
        manual = d["manual"].get(FAN_ROWS[i][2])
        return manual if manual is not None else d["regs"][FAN_ROWS[i][3]]

    def store(i, value):
        """Write fan row i's manual speed (None = auto); the daemon applies it."""
        cur = {k: d["manual"].get(r) for k, _l, r, _ri in FAN_ROWS}
        cur[FAN_ROWS[i][0]] = None if value is None else pct(value)
        overrides_write(cur)
        d["manual"] = overrides_read()

    def set_fan(i, value):
        if d["regs"][0] == RESET_VALUE:
            return "the fan daemon is stopped: press f to start it"
        store(i, value)
        return "%s fan: %s" % (FAN_ROWS[i][1],
                               "auto (curve)" if value is None else "%d%% manual" % pct(value))

    def pct_at(mx, x0, tw):
        return pct(round((mx - x0) / float(max(1, tw - 1)) * (100 - OVERRIDE_MIN)) + OVERRIDE_MIN)

    def nudge(i, delta):
        say(set_fan(i, speed_now(i) + delta))

    def run_action(action):
        nonlocal sel, pending_mode, pending_ticket
        if action[0] == "mode":
            mode = action[1]

            def apply_job():
                with contextlib.redirect_stdout(io.StringIO()):
                    set_mode(mode, Ops(False))
                return "mode: %s" % mode
            pending_mode, pending_ticket = mode, feed.submit(apply_job)
            say("mode: %s…" % mode)
        elif action[0] == "fans":
            verb = "stop" if action[1] else "start"

            def fans_job():
                subprocess.run(["systemctl", verb, "omenctl"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                return "fans: handed to the EC" if verb == "stop" else "fans: omenctl owns them"
            feed.submit(fans_job)
            say("fans: %s…" % ("stopping" if verb == "stop" else "starting"))
        elif action[0] == "auto_toggle":
            sel = action[1]
            auto = d["manual"].get(FAN_ROWS[sel][2]) is None   # auto -> manual at today's speed
            say(set_fan(sel, speed_now(sel) if auto else None))

    def mouse():
        nonlocal sel, drag
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return
        if wheel_up and bstate & (wheel_up | wheel_down):    # wheel over a fader: +-1 %
            for (yy, _x0, _x1, act) in buttons:
                if act[0] == "track" and my == yy:
                    sel = act[1]
                    nudge(sel, 1 if bstate & wheel_up else -1)
        elif bstate & curses.REPORT_MOUSE_POSITION:          # motion
            if drag:
                drag["val"] = pct_at(mx, drag["x0"], drag["tw"])
                if (drag["val"] != drag["wrote"]
                        and time.monotonic() - drag["t"] >= DRAG_WRITE_EVERY):
                    store(drag["i"], drag["val"])
                    drag["wrote"], drag["t"] = drag["val"], time.monotonic()
        elif bstate & curses.BUTTON1_RELEASED:
            if drag:                       # the value under the release always lands
                drag["val"] = pct_at(mx, drag["x0"], drag["tw"])
                if drag["val"] != drag["wrote"]:
                    store(drag["i"], drag["val"])
                say("%s fan: %d%% manual" % (FAN_ROWS[drag["i"]][1], drag["val"]))
                drag = None
        elif drag:
            pass                                              # a stray press mid-drag
        elif bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            for (yy, x0, x1, act) in list(buttons):
                if my == yy and x0 <= mx < x1:
                    if act[0] == "track" and d["regs"][0] != RESET_VALUE:
                        sel = act[1]
                        val = pct_at(mx, act[2], act[3])
                        store(act[1], val)
                        drag = {"i": act[1], "val": val, "wrote": val,
                                "t": time.monotonic(), "x0": act[2], "tw": act[3]}
                        if bstate & curses.BUTTON1_CLICKED:   # no separate release
                            say("%s fan: %d%% manual" % (FAN_ROWS[act[1]][1], val))
                            drag = None
                    elif act[0] == "track":
                        say("the fan daemon is stopped: press f to start it")
                    else:
                        run_action(act)

    def handle(key):
        """One input event. -> True to quit."""
        nonlocal sel, debug, editor
        if editor:                          # the curve view has the keyboard while open
            if editor.handle(key):
                editor = None
            return False
        if key in (ord("q"), 27):
            return True
        if key == curses.KEY_RESIZE:
            graphs.clear()
        elif key in (ord("1"), ord("2"), ord("3"), ord("4")):
            run_action(("mode", TUI_MODES[key - ord("1")][1]))
        elif key == ord("f"):
            run_action(("fans", d["regs"][0] != RESET_VALUE))
        elif key in (curses.KEY_UP, curses.KEY_DOWN, 9):
            sel = 1 - sel
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_PPAGE, curses.KEY_NPAGE):
            nudge(sel, {curses.KEY_LEFT: -1, curses.KEY_RIGHT: 1,
                        curses.KEY_PPAGE: 10, curses.KEY_NPAGE: -10}[key])
        elif key in (ord("a"), ord(" ")):
            run_action(("auto_toggle", sel))
        elif key == ord("c"):
            editor = CurveEditor(sel)
        elif key == ord("d"):
            debug = not debug
            say("debug: %s" % ("on" if debug else "off"))
        elif key == curses.KEY_MOUSE:
            mouse()
        return False

    feed.start()
    try:
        toast_up = False
        while True:
            sample, done, notes = feed.take()
            if sample is not None:
                canvas.follow_theme()
                sample["manual"] = overrides_read()    # newer than the sample's own copy
                d = sample
                dirty = True
                if pending_mode and done >= pending_ticket:
                    pending_mode = None
            for note in notes:
                say(note)
                dirty = True
            shown = bool(msg) and time.monotonic() < msg_until
            if shown != toast_up:                      # a message just appeared or expired
                toast_up = shown
                dirty = True
            if dirty or moving:
                draw()
                dirty = False
            stdscr.timeout(int((TUI_FRAME if moving else TUI_IDLE) * 1000))
            key = stdscr.getch()
            while key != -1:                 # a burst of mouse motion costs a single frame
                if handle(key):
                    return 0
                dirty = True
                stdscr.timeout(0)
                key = stdscr.getch()
    finally:
        feed.stop()


def _curve_layout(h, w, steps):
    """Where the curve editor's cards go in an h x w window: the table always, the chart
    if it fits. Rows that do not fit scroll; the two status lines go first."""
    avail = max(3, h - 4)                    # header + gap above, message + hint below
    side = w >= 72
    wanted = steps + 5                       # borders, column heading, two status lines
    table_h = avail if side else min(avail, wanted)
    inner = table_h - 2 - 1                  # minus the borders and the heading
    status = max(0, min(2, inner - min(steps, max(1, inner - 2))))
    table_w = 38 if side else w
    if side:
        chart = (2, table_w, avail, w - table_w) if w - table_w >= 20 and avail >= 6 else None
    else:
        chart = (2 + table_h, 0, avail - table_h, w) if avail - table_h >= 6 else None
    return {"table_h": table_h, "table_w": table_w, "visible": max(1, inner - status),
            "status": status, "chart": chart}


def _scroll_first(row, count, visible):
    """The first step to show so that step `row` stays in a list of `visible` rows, kept
    around the middle of it when the list scrolls."""
    if count <= visible:
        return 0
    return max(0, min(count - visible, row - visible // 2))


class CurveEditor:
    """Edit the fan curves in FANS; the daemon reloads /etc/omenctl/fans.json on mtime.
    Used by the TUI's `c` view and by `omenctl curve`. Everything on this thread."""

    COLUMNS = ((9, 5), (16, 7), (26, 6))     # x in the card, width: up, down, duty

    def __init__(self, fan=0):
        for i, f in enumerate(FANS):         # what the file says, not a discarded edit
            f["thr"] = [tuple(x) for x in DEFAULT_THR[i]]
        load_curve_file()
        self.fan, self.row, self.col = fan, 0, 0
        self.dirty = self.confirm_quit = False
        self.msg = ""
        self.buttons = []                    # (y, x0, x1, action) of the frame on screen
        self.chart_cache = (None, None)

    def bump(self, delta):
        thr = FANS[self.fan]["thr"]
        if not thr:
            return
        value = list(thr[self.row])
        if self.col == 0:                    # up
            value[0] = max(value[1] + 1, min(120, value[0] + delta))
        elif self.col == 1:                  # down
            value[1] = max(0, min(value[0] - 1, value[1] + delta))
        else:                                # duty
            value[2] = max(0, min(100, value[2] + delta))
        thr[self.row] = tuple(value)
        self.dirty = True

    def select_fan(self, fan):
        self.fan, self.row, self.col = fan, 0, 0

    def click(self, mx, my, bstate):
        """A mouse event: wheel over a cell is +-1, a click selects a cell or a fan."""
        import curses
        up = getattr(curses, "BUTTON4_PRESSED", 0)
        down = getattr(curses, "BUTTON5_PRESSED", 0)
        for yy, x0, x1, action in self.buttons:
            if my != yy or not x0 <= mx < x1:
                continue
            if action[0] == "fan" and bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                self.select_fan(action[1])
            elif action[0] == "cell":
                if bstate & (up | down):
                    self.row, self.col = action[1], action[2]
                    self.bump(1 if bstate & up else -1)
                elif bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                    self.row, self.col = action[1], action[2]
            return

    def handle(self, key):
        """One input event. -> True to leave the editor."""
        import curses
        if key == curses.KEY_RESIZE:
            return False
        confirming, self.confirm_quit = self.confirm_quit, False
        self.msg = ""
        thr = FANS[self.fan]["thr"]
        if key in (ord("q"), 27):
            if self.dirty and not confirming:
                self.msg = "unsaved curve: s save · q again to discard"
                self.confirm_quit = True
                return False
            return True
        if key == ord("Q"):
            return True
        if key == ord("s"):
            err = save_curve_file()
            if err:
                self.msg = err
            else:
                self.dirty = False
                self.msg = "saved; the daemon reloads within 1 s"
        elif key == ord("r"):
            FANS[self.fan]["thr"] = [tuple(x) for x in DEFAULT_THR[self.fan]]
            self.row = self.col = 0
            self.dirty = True
            self.msg = "reset to the embedded defaults (s to save)"
        elif key in (ord("1"), ord("2")):
            self.select_fan(key - ord("1"))
        elif key == curses.KEY_UP:
            self.row = max(0, self.row - 1)
        elif key == curses.KEY_DOWN:
            self.row = min(len(thr) - 1, self.row + 1)
        elif key == 9:                       # Tab
            self.col = (self.col + 1) % 3
        elif key == curses.KEY_BTAB:
            self.col = (self.col - 1) % 3
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self.bump(-1 if key == curses.KEY_LEFT else 1)
        elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.bump(-5 if key == curses.KEY_PPAGE else 5)
        elif key == curses.KEY_MOUSE:
            try:
                _id, mx, my, _z, bstate = curses.getmouse()
            except curses.error:
                return False
            self.click(mx, my, bstate)
        return False

    def _chart(self, thr, width, height):
        key = (tuple(thr), width, height, GREEN, YELLOW, ORANGE, RED)   # a new theme recolours
        if self.chart_cache[0] != key:
            self.chart_cache = (key, curve_graph(thr, width, height))
        return self.chart_cache[1]

    def draw(self, c, h, w, cpu, gpu):
        import curses
        self.buttons.clear()
        thr = FANS[self.fan]["thr"]
        temp = cpu if self.fan == 0 else gpu
        layout = _curve_layout(h, w, len(thr))
        c.put(0, 1, "Omenctl", c.attr("1"))
        c.put(0, 9, "› curve", c.attr("90"))
        x = 18
        for i, row in enumerate(FAN_ROWS):
            label = " %d %s " % (i + 1, row[1])
            on = i == self.fan
            c.put(0, x, label, c.attr("1;" + BLUE) | curses.A_REVERSE if on else c.attr("90"))
            self.buttons.append((0, x, x + len(label), ("fan", i)))
            x += len(label) + 1
        if self.dirty:
            c.put(0, w - 10, "● unsaved", c.attr(ORANGE))
        self._table(c, thr, temp, layout)
        if layout["chart"]:
            self._graph(c, thr, temp, layout["chart"])
        err = curve_error(thr)
        if self.msg:
            c.put(h - 2, 2, self.msg[:max(0, w - 3)], c.attr("1"))
        elif err:
            c.put(h - 2, 2, ("invalid: " + err)[:max(0, w - 3)], c.attr("1;" + RED))
        hint = ("↑↓ step · Tab column · ←→ ±1 · PgUp/PgDn ±5 · 1/2 fan · s save · r reset · q back"
                if w >= 85 else "↑↓ step · Tab column · ←→ ±1 · s save · q back")
        c.put(h - 1, 2, hint[:max(0, w - 3)], c.attr("90"))

    def _table(self, c, thr, temp, layout):
        import curses
        ch, cw = layout["table_h"], layout["table_w"]
        c.card(2, 0, ch, cw, "Steps")
        heading = c.attr("90")
        c.put(3, 5, "#", heading)
        c.put(3, 9, "up °C", heading)
        c.put(3, 16, "down °C", heading)
        c.put(3, 28, "duty", heading)
        current = curve_current_index(thr, temp)
        first = _scroll_first(self.row, len(thr), layout["visible"])
        for i in range(first, min(len(thr), first + layout["visible"])):
            y = 4 + i - first
            selected = i == self.row
            c.put(y, 2, "▸" if selected else " ", c.attr("1;" + BLUE))
            if i == current:
                c.put(y, 3, "●", c.attr(_temp_code(temp)))
            c.put(y, 4, "%2d" % (i + 1), c.attr("1") if selected else c.attr("90"))
            texts = ("%5d" % thr[i][0], "%7d" % thr[i][1], "%4d %%" % thr[i][2])
            for col, ((x, width), text) in enumerate(zip(self.COLUMNS, texts)):
                a = c.attr("1") if selected else 0
                if selected and col == self.col:
                    a = c.attr("1;" + BLUE) | curses.A_REVERSE
                c.put(y, x, text, a)
                self.buttons.append((y, x, x + width, ("cell", i, col)))
        if layout["status"] and temp is not None and current is not None:
            y = 2 + ch - 1 - layout["status"]
            c.put(y, 2, "now  %3.0f°C → %d %%" % (temp, thr[current][2])
                  + (" !" if temp >= 85 else ""), c.attr("1;" + _temp_code(temp)))
            if layout["status"] > 1 and current + 1 < len(thr):
                nxt = thr[current + 1]
                c.put(y + 1, 2, "next %3d°C → %d %%" % (nxt[0], nxt[2]), c.attr("90"))
        elif layout["status"] and temp is None:
            c.put(2 + ch - 1 - layout["status"], 2, "temperature unavailable", c.attr(ORANGE))

    def _graph(self, c, thr, temp, spot):
        y, x, ch, cw = spot
        c.card(y, x, ch, cw, "Duty by temperature")
        width, height = cw - 9, ch - 3       # room for the labels and the marker row
        rows, codes = self._chart(thr, width, height)
        for r, (row, code) in enumerate(zip(rows, codes)):
            c.runs(y + 2 + r, x + 2, "".join(row).replace("·", " "), code)
        for pct, line in ((100, 0), (50, (height - 1) // 2), (0, height - 1)):
            c.put(y + 2 + line, x + 2 + width + 1, "%3d%%" % pct, c.attr("90"))
        bottom = y + ch - 1
        c.put(bottom, x + 2, " 30° ", c.attr("90"))
        c.put(bottom, x + 2 + width - 6, " 100° ", c.attr("90"))

        def column(t):
            return int(round((t - 30) / 70.0 * (width - 1)))
        up = thr[self.row][0] if thr else None
        if up is not None and 30 <= up <= 100:          # ties the table to the chart
            c.put(bottom, x + 2 + column(up), "┴", c.attr("1;" + BLUE))
        if temp is not None and 30 <= temp <= 100:      # where the temperature is now
            mx = column(temp)
            c.put(y + 1, x + 2 + mx, "▼", c.attr("1;" + _temp_code(temp)))
            for r, row in enumerate(rows):
                if row[mx] == "·":
                    c.put(y + 2 + r, x + 2 + mx, "│", c.attr("edge"))


def curve_main(stdscr, fan_key):
    """`omenctl curve`: the TUI's curve view on its own."""
    canvas = TuiCanvas(stdscr)
    editor = CurveEditor(0 if fan_key == "cpu" else 1)
    rig = None
    try:
        rig = Rig(3.0)
    except OSError:
        pass
    cpu = gpu = None
    next_read = 0.0
    try:
        while True:
            if time.monotonic() >= next_read:
                next_read = time.monotonic() + SAMPLE_EVERY
                canvas.follow_theme()
                if rig is not None:
                    try:
                        cpu, gpu, _ = rig.temps()
                    except OSError:
                        cpu = gpu = None
            h, w = canvas.begin()
            editor.draw(canvas, h, w, cpu, gpu)
            stdscr.refresh()
            stdscr.timeout(max(1, int((next_read - time.monotonic()) * 1000)))
            key = stdscr.getch()
            if key != -1 and editor.handle(key):
                return 0
    finally:
        if rig is not None:
            rig.close()


def cmd_curve(args):
    import curses
    if not sys.stdout.isatty():
        sys.exit("omenctl curve needs a terminal")
    use_direct_color()
    try:
        return curses.wrapper(curve_main, args.fan) or 0
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\x1b[?1002l")
        sys.stdout.flush()


def cmd_tui(args):
    import curses
    if not sys.stdout.isatty():
        sys.exit("omenctl tui needs a terminal")
    use_direct_color()
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
    return dgpu_driver() == "nvidia"


RYZENADJ_TO_PM = {
    # ryzenadj argument -> (PM-table field, unit scale)
    "--stapm-limit": ("STAPM LIMIT", 0.001),
    "--fast-limit": ("PPT LIMIT FAST", 0.001),
    "--slow-limit": ("PPT LIMIT SLOW", 0.001),
    "--apu-slow-limit": ("PPT LIMIT APU", 0.001),
    "--tctl-temp": ("THM LIMIT CORE", 1.0),   # degrees C, not mW
}


def expected_limits(p):
    """The PM-table values a profile's ryzenadj arguments should produce (W)."""
    want = {}
    for arg in p["ryzenadj"]:
        key, _, value = arg.partition("=")
        entry = RYZENADJ_TO_PM.get(key)
        if entry and value.isdigit():
            pm, scale = entry
            want[pm] = int(value) * scale
    return want


def verify_profile(name, boost, iface):
    """Read back what apply_profile just wrote: [] means it took.

    This is the repo's own rule applied to the tool: 'applied' is not 'took'.
    The tolerance covers the SMU's float rounding (~1 W).
    """
    p = PROFILES[name]
    problems = []

    got = pm_limit_values()
    if got:
        for key, value in expected_limits(p).items():
            if key not in got or abs(got[key] - value) > 1.5:
                unit = "C" if key == "THM LIMIT CORE" else "W"
                problems.append("%s %s != %g %s" % (
                    key, got.get(key, "?"), value, unit))
    else:
        problems.append("PM table unreadable")

    cpu0 = "/sys/devices/system/cpu/cpu0/cpufreq/"
    gov = sysfs(cpu0 + "scaling_governor")
    if gov != p["gov"]:
        problems.append("governor %s != %s" % (gov, p["gov"]))
    fmax = read_int(cpu0 + "scaling_max_freq")
    allowed = [p["fmax"]]
    if not boost:
        # boost off clamps scaling_max_freq (and cpuinfo_max_freq) back to the
        # base clock; that is the intended state, not a failed write.
        clamped = read_int(cpu0 + "cpuinfo_max_freq")
        if clamped:
            allowed.append(clamped)
    if fmax is None or min(abs(fmax - v) for v in allowed) > 1000:
        problems.append("max_freq %s != %s" % (
            fmax, " or ".join(str(v) for v in allowed)))
    epp = sysfs(cpu0 + "energy_performance_preference")
    if epp != p["epp"]:
        problems.append("epp %s != %s" % (epp, p["epp"]))
    want_boost = "1" if boost else "0"
    if sysfs(BOOST) != want_boost:
        problems.append("boost %s != %s" % (sysfs(BOOST), want_boost))

    path = igpu_dpm_path()
    if path and sysfs(path) != p["igpu_dpm"]:
        problems.append("igpu_dpm %s != %s" % (sysfs(path), p["igpu_dpm"]))

    if iface:
        state = wifi_ps_state(iface)
        if state != p["wifi_ps"]:
            problems.append("wifi_ps %s != %s" % (state, p["wifi_ps"]))
    return problems


def apply_profile(name, ops, boost=True):
    """Apply a profile, then prove it took; retry once on a mismatch."""
    p = PROFILES[name]
    plog("%s profile" % name.upper())
    iface = wifi_iface()
    problems = []
    for attempt in (1, 2):
        # boost first, so the freq cap below is not clamped under it
        ops.write(BOOST, "1" if boost else "0")
        for f, v in (("scaling_governor", p["gov"]),
                     ("scaling_max_freq", p["fmax"]),
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
                if ops.run(["nvidia-smi"] + args) != 0 and not ops.dry:
                    plog("WARN: nvidia-smi %s failed" % " ".join(args))
        if iface:
            if ops.run(["iw", "dev", iface, "set", "power_save", p["wifi_ps"]]) != 0 \
                    and not ops.dry:
                plog("WARN: iw power_save failed on %s" % iface)
        elif not ops.dry:
            plog("WARN: no wireless interface; wifi power save not set")
        path = igpu_dpm_path()
        if path is None:
            plog("WARN: no amdgpu DPM node found")
        elif ops.dry or sysfs(path) != p["igpu_dpm"]:   # already there: no log spam
            if ops.write(path, p["igpu_dpm"]):
                plog("iGPU DPM level -> %s" % p["igpu_dpm"])

        if ops.dry:
            break
        problems = verify_profile(name, boost, iface)
        if not problems:
            break
        if attempt == 1:
            plog("WARN: %s not fully applied (%s); retrying once"
                 % (name, "; ".join(problems)))

    print(">>> %s: %s" % (name.upper(), p["label"]))
    if problems:
        plog("WARN: %s verify failed: %s" % (name, "; ".join(problems)))
        print("!!! %s: not fully applied (%s)"
              % (name.upper(), "; ".join(problems)))
    else:
        plog("OK %s: %s" % (name.upper(), p["label"]))
    return not problems


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
    ac = power_supply("Mains") or "/sys/class/power_supply/ACAD"
    return "ac" if sysfs(ac + "/online", "0") == "1" else "battery"


def apply_mode(mode, ops):
    """auto = the envelope of the power source; quiet = auto with boost off;
    eco = the battery envelope whatever the source; perf = everything unlocked.
    Returns True when the read-back matched (or the run was a dry run)."""
    if mode == "perf":
        return apply_profile("perf", ops)
    elif mode == "eco":
        return apply_profile("battery", ops)
    return apply_profile(source_profile(), ops, boost=(mode != "quiet"))


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
    want_boost = None
    if args.state in ("on", "off"):
        want_boost = args.state == "on"
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
    actual = sysfs(BOOST) == "1"
    print("boost %s (mode: %s) · max %s GHz"
          % ("on" if actual else "off", mode,
             "%.2f" % (fmax / 1e6) if fmax else "?"))
    return 0 if want_boost is None or actual == want_boost else 1


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
            return None
        log_boot_limits()
        ok = apply_mode(read_mode(), ops)
        if ok is not False:            # a failed apply must not debounce the retry
            open(LAST_APPLY, "w").close()
    return ok


def set_mode(mode, ops):
    """Store `mode` and apply it, serialised against any other apply."""
    write_mode(mode)
    log_boot_limits()
    with open(APPLY_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ok = apply_mode(mode, ops)
        if ok is not False:
            open(LAST_APPLY, "w").close()
    return ok


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
        ok = apply_stored_mode(ops, quiet_if_recent=2.0)
        return 0 if ok is not False else 1
    if not ops.dry:
        ok = set_mode(mode, ops)
    else:
        ok = apply_mode(mode, ops)
    print({"auto": ">>> AUTO: follows the power source",
           "quiet": ">>> QUIET: auto with boost off (the power cap does the cooling)",
           "eco": ">>> ECO: battery envelope kept on AC",
           "perf": ">>> PERF: unlocked, stays until another mode is chosen"}[mode])
    return 0 if ok else 1


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


def target_pm_limits():
    """The PM-table tuple the stored mode should produce (for the watcher)."""
    mode = read_mode()
    name = ("perf" if mode == "perf" else
            "battery" if mode == "eco" else source_profile())
    want = expected_limits(PROFILES[name])
    keys = ("STAPM LIMIT", "PPT LIMIT FAST", "PPT LIMIT SLOW", "THM LIMIT CORE")
    if not all(k in want for k in keys):
        return None
    return tuple(round(want[k], 1) for k in keys)


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
        """True = refresh `known`; False = the apply failed, keep the target."""
        if time.monotonic() - last[0] < WATCH_MIN_INTERVAL:
            return True                # debounced: another apply just ran
        last[0] = t0 = time.monotonic()
        try:
            done = apply_stored_mode(Ops(False), quiet_if_recent=1.0)
            elapsed = time.monotonic() - t0
            if done is None:
                print("skipped after %s: another apply just ran" % reason, flush=True)
                return True
            print(("re-applied after %s in %.2fs" if done else
                   "FAILED to apply after %s in %.2fs") % (reason, elapsed),
                  flush=True)
            return done
        except OSError as e:
            print("FAILED to re-apply after %s: %s" % (reason, e), flush=True)
            return False

    print("watching %s and the SMU limits (now %s)"
          % (PLATFORM_PROFILE, describe(pm_limits())), flush=True)
    # Known-good state first: a watcher started while the limits are already off
    # target would otherwise adopt the wrong values and never correct them.
    startup_ok = reapply("startup")
    known = pm_limits() if startup_ok else target_pm_limits()
    print("baseline after startup apply: %s" % describe(known), flush=True)
    while not stop:
        r, _, _ = select.select([fd], [], [], WATCH_POLL)
        if r:                                             # 1. platform_profile write
            drain(fd)
            time.sleep(WATCH_DEBOUNCE)
            drain(fd)
            if reapply("platform_profile -> %r" % sysfs(PLATFORM_PROFILE, "?")):
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
    load_curve_file()
    fans = FANS
    curve_stamp = curve_mtime()

    regs = rig.ec_read(SET_CPU, 4)
    idx = {f["write_reg"]: initial_index(f["thr"], regs[f["write_reg"] - SET_CPU + 2])
           for f in fans}
    notified = False
    read_fails = write_fails = failsafe_fails = 0
    try:
        os.unlink(FAN_OVERRIDE)               # a fresh start is always on the curve
    except OSError:
        pass
    print("run: owning setpoints 0x2C/0x2D (contract: 255 on exit + watchdog)",
          flush=True)

    while not stop:
        try:
            cpu, gpu, regs = rig.temps()
            read_fails = failsafe_fails = 0
        except OSError as e:
            read_fails += 1
            print("read failed (%d): %s" % (read_fails, e), flush=True)
            try:
                rig.open()                         # descriptors may have gone stale
            except OSError:
                pass
            if read_fails >= 3:                    # fail safe: full speed
                failed = 0
                for f in fans:
                    try:
                        rig.ec_write(f["write_reg"], 100)
                    except OSError as e:
                        failed += 1
                        print("fail-safe write failed: %s" % e, flush=True)
                if failed:
                    failsafe_fails += 1
                    if failsafe_fails >= 5:
                        print("giving up; EC unavailable, unit stop path will reset",
                              flush=True)
                        return 1
                else:
                    failsafe_fails = 0
            time.sleep(1)
            continue

        stamp = curve_mtime()
        if stamp != curve_stamp:
            if load_curve_file():
                for f in fans:
                    wr = f["write_reg"]
                    idx[wr] = min(idx.get(wr, 0), len(f["thr"]) - 1)
                print("run: curve reloaded from %s" % CURVE_FILE, flush=True)
            else:
                print("run: ignoring invalid or missing %s (keeping the old curve)"
                      % CURVE_FILE, flush=True)
            curve_stamp = stamp

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


def model_info():
    root = os.environ.get("OMENCTL_DMI_DIR", "/sys/class/dmi/id")
    return (sysfs(root + "/product_name", ""),
            sysfs(root + "/board_name", ""))


def model_supported():
    product, board = model_info()
    return (product.startswith(SUPPORTED_PRODUCT) and board in SUPPORTED_BOARDS,
            product, board)


def model_check_required(args):
    """`reset --raw` is the emergency latch release: it must write the 255s even
    when the DMI is unreadable (e.g. after a flash experiment). Every other
    command keeps the model gate."""
    return args.cmd != "doctor" and not (
        args.cmd == "reset" and getattr(args, "raw", False))


def check_model():
    """Refuse to touch the EC on hardware this was not measured on.

    OMENCTL_DMI_DIR only exists so the refusal can be tested without such a
    machine; it is not a way around the check."""
    ok, product, board = model_supported()
    if ok:
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


def cmd_doctor(_args):
    """Read-only self-check: model, EC, SMU, dGPU, WiFi, units, fans.

    Exit 1 on a FAIL; warnings do not fail. It never writes the EC, the SMU or
    an EFI variable -- the one exception is opening the EC O_RDWR to prove the
    daemon will be able to. Nothing is written through that descriptor.
    """
    import shutil
    fails = warns = 0

    def line(tag, label, detail=""):
        nonlocal fails, warns
        if tag == "FAIL":
            fails += 1
        elif tag == "WARN":
            warns += 1
        print("[%s] %-18s %s" % (tag, label, detail))

    ok, product, board = model_supported()
    line("OK" if ok else "FAIL", "model", "%s / %s" % (product or "?", board or "?"))
    bios = sysfs("/sys/class/dmi/id/bios_version", "?")
    line("OK" if bios != "?" else "WARN", "bios", bios)

    param = sysfs("/sys/module/ec_sys/parameters/write_support")
    if param is None:
        line("FAIL", "ec_sys", "not loaded (modprobe ec_sys write_support=1)")
    elif param != "Y":
        line("FAIL", "ec_sys", "write_support=%s (need Y)" % param)
    else:
        line("OK", "ec_sys", "write_support=Y")
    try:
        fd = os.open(ECIO, os.O_RDONLY)
        try:
            temp = os.pread(fd, 1, CPU_TEMP)[0]
        finally:
            os.close(fd)
        line("OK", "EC read", "0x%02X = %d C" % (CPU_TEMP, temp))
    except (OSError, IndexError) as e:
        line("FAIL", "EC read", str(e))
    try:
        fd = os.open(ECIO, os.O_RDWR)
        os.close(fd)
        line("OK", "EC write", "O_RDWR accepted")
    except OSError as e:
        line("FAIL", "EC write", str(e))

    lim = pm_limit_values()
    if lim:
        line("OK", "ryzen_smu", "STAPM %g W / Tctl %g C" % (
            lim.get("STAPM LIMIT", 0), lim.get("THM LIMIT CORE", 0)))
    else:
        line("FAIL", "ryzen_smu", "PM table unreadable (%s)" % PM_TABLE)

    if os.access("/usr/bin/ryzenadj", os.X_OK):
        line("OK", "ryzenadj", "/usr/bin/ryzenadj")
    else:
        found = shutil.which("ryzenadj")
        if found:
            line("WARN", "ryzenadj", "%s, but apply_profile uses /usr/bin/ryzenadj" % found)
        else:
            line("FAIL", "ryzenadj", "missing (the SMU limits cannot be written)")

    pci = dgpu_pci()
    if not pci:
        line("WARN", "dGPU", "no NVIDIA VGA/3D function found")
    else:
        drv = dgpu_driver()
        if drv == "nvidia":
            smi = shutil.which("nvidia-smi")
            line("OK" if smi else "WARN", "dGPU",
                 "%s: nvidia%s" % (pci, "" if smi else " (nvidia-smi missing)"))
        elif drv == "vfio-pci":
            line("OK", "dGPU", "%s: vfio-pci (D3cold, ~6.7 W saved)" % pci)
        else:
            line("WARN", "dGPU", "%s: %s" % (pci, drv or "no driver"))

    iface = wifi_iface()
    if not iface:
        line("WARN", "wifi", "no wireless interface")
    elif not shutil.which("iw"):
        line("WARN", "wifi", "%s: iw missing (power_save not applied)" % iface)
    else:
        line("OK", "wifi", "%s: power_save=%s" % (iface, wifi_ps_state(iface) or "?"))

    mains = power_supply("Mains")
    bat = power_supply("Battery")
    line("OK" if mains else "WARN", "AC",
         "%s: online=%s" % (mains, sysfs(mains + "/online", "?")) if mains
         else "no Mains power_supply")
    line("OK" if bat else "WARN", "battery",
         "%s: %s%% %s" % (bat, read_int(bat + "/capacity"), sysfs(bat + "/status"))
         if bat else "no Battery power_supply")

    profile = sysfs("/sys/firmware/acpi/platform_profile")
    line("OK" if profile else "WARN", "platform_profile", profile or "not exposed")

    if not os.path.exists(CURVE_FILE):
        line("OK", "fan curve", "embedded defaults (%s absent)" % CURVE_FILE)
    else:
        try:
            with open(CURVE_FILE) as f:
                data = json.load(f)
            errs = []
            for key in ("cpu", "gpu"):
                thr = [(int(a), int(b), int(c)) for a, b, c in data[key]]
                err = curve_error(thr)
                if err:
                    errs.append("%s: %s" % (key, err))
            line("FAIL" if errs else "OK", "fan curve", "; ".join(errs) or CURVE_FILE)
        except (OSError, ValueError, KeyError, TypeError) as e:
            line("FAIL", "fan curve", "invalid %s: %s" % (CURVE_FILE, e))

    def systemctl(*argv):
        try:
            return subprocess.run(["systemctl"] + list(argv), stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True,
                                  timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "?"

    for unit, wanted in (("omenctl.service", ("enabled", "active")),
                         ("omenctl-watch.service", ("enabled", "active")),
                         ("omenctl-profile.timer", ("enabled", "active"))):
        enabled = systemctl("is-enabled", unit)
        active = systemctl("is-active", unit)
        line("OK" if (enabled, active) == wanted else "WARN", unit,
             "enabled=%s active=%s" % (enabled, active))
    prof = systemctl("is-enabled", "omenctl-profile.service")
    line("OK" if prof == "enabled" else "WARN", "omenctl-profile.service", prof)
    nbfc = systemctl("is-enabled", "nbfc_service") or "not-found"
    line("OK" if nbfc in ("disabled", "not-found") else "WARN", "nbfc_service", nbfc)
    ppd = systemctl("is-enabled", "power-profiles-daemon") or "not-found"
    line("OK" if ppd in ("masked", "disabled", "not-found") else "WARN",
         "power-profiles-daemon", ppd)

    running = daemon_running()
    line("OK" if running else "WARN", "fan daemon",
         "running" if running else "not running")
    try:
        rig = Rig(0)
        try:
            cpu, gpu, regs = rig.temps()
            rpms = [read_int(rig.hp + "/fan%d_input" % i) if rig.hp else None
                    for i in (1, 2)]
            cduty = "EC" if regs[0] == RESET_VALUE else "%d%%" % regs[0]
            gduty = "EC" if regs[1] == RESET_VALUE else "%d%%" % regs[1]
            line("OK", "fans", "CPU %.0fC %s %s rpm | GPU %.0fC %s %s rpm" % (
                cpu, cduty, rpms[0] if rpms[0] is not None else "?",
                gpu, gduty, rpms[1] if rpms[1] is not None else "?"))
            for name, setp, rpm in (("cpu", regs[0], rpms[0]),
                                    ("gpu", regs[1], rpms[1])):
                if setp != RESET_VALUE and setp >= 30 and not rpm:
                    line("WARN", "fan %s" % name,
                         "setpoint %d%% but rpm=%s (stall?)" % (setp, rpm))
            cap = battery_capacity(rig)
            if cap:
                line("OK", "battery health", "%.1f / %.1f Wh (%.0f%%)" % (
                    cap[1], cap[0], 100.0 * cap[1] / cap[0]))
            abe = battery_extender(rig)
            line("OK" if abe is not None else "WARN", "battery extender",
                 "on" if abe else "off" if abe is not None else "unreadable")
        finally:
            rig.close()
    except OSError as e:
        line("WARN", "fans", "EC read failed: %s" % e)

    print()
    print("doctor: %d failure(s), %d warning(s)" % (fails, warns))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(
        prog="omenctl",
        description="Fans and power for the HP OMEN 15 (design notes: see the "
                    "top of omenctl.py and ../ec-map.md).")
    sub = ap.add_subparsers(dest="cmd", required=True, title="commands",
                            metavar="COMMAND")
    p = sub.add_parser("doctor", help="read-only self-check: model, EC, SMU, dGPU, WiFi, units")
    p.set_defaults(func=cmd_doctor)

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

    p = sub.add_parser("curve", help="edit the fan curve: curve cpu|gpu")
    p.add_argument("fan", nargs="?", default="cpu", choices=["cpu", "gpu"])
    p.set_defaults(func=cmd_curve)

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
    if model_check_required(args):  # the doctor reports the model; reset --raw is the release
        check_model()
    if os.geteuid() != 0:
        sys.exit("root needed for the EC io file")
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
