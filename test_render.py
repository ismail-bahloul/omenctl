#!/usr/bin/env python3
"""Headless layout tests for render() -- no hardware, no root.

`render()` is the single layout used by `status`, `status -w` and `tui`, so a
crash or a missing label here breaks all three. These checks cover the widths
and detail levels the TUI picks between, plus the text markers that carry the
temperature meaning when the terminal remaps colours.
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("oc", os.path.join(HERE, "omenctl.py"))
oc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc)

POWER = {
    "ra": {"STAPM LIMIT": 28.0, "PPT LIMIT FAST": 36.0, "PPT LIMIT SLOW": 28.0,
           "PPT LIMIT APU": 22.0, "THM LIMIT CORE": 85.0},
    "bat_w": 18.0, "bat_status": "Full", "bat_wh": 59.2,
    "pkg_w": 18.0, "gov": "powersave", "fmax": 3200000, "boost": False,
    "dgpu": "nvidia", "dpm": "auto", "bat_pct": 100,
    "bat_cap": (70.9, 59.2), "bat_cycles": 133, "bat_extender": True,
    "ac": True, "mode": "quiet",
}

HIST = {"CPU": [80, 82, 84, 86], "GPU": [70, 72, 74, 77]}
REGS = [40, 28, 70, 46]          # CPU/GPU setpoints + readbacks
RPMS = [2269, 1553]


def plain(line):
    """Drop the TUI colour markers, leaving what the terminal shows."""
    return re.sub(r"\x01[^\x02]*\x02(.*?)\x03", r"\1", line)


def render(width, level, max_width, show_debug, no_color):
    paint = (lambda code, text: text) if no_color else oc._marker_paint
    return oc.render(oc.FANS, 86.0, 77.0, REGS, RPMS, HIST, width, paint, 3.0, 85,
                     POWER, False, None, level, max_width, show_debug)


def theme_test():
    """Hybrid theme: severity from KDE, accents from Konsole."""
    import tempfile
    kde = """[Colors:View]
ForegroundPositive=#31C193
ForegroundNeutral=#FF734A
ForegroundNegative=#FF6A9D
"""
    konsole = """[Color1]
Color=146,159,235

[Color3]
Color=185,159,234
"""
    fd, kde_path = tempfile.mkstemp(suffix=".colors")
    with os.fdopen(fd, "w") as f:
        f.write(kde)
    fd, konsole_path = tempfile.mkstemp(suffix=".colorscheme")
    with os.fdopen(fd, "w") as f:
        f.write(konsole)
    old = (oc.GREEN, oc.YELLOW, oc.ORANGE, oc.RED, oc.BLUE, oc.PURPLE)
    os.environ["OMENCTL_THEME"] = kde_path
    os.environ["OMENCTL_KONSOLE_THEME"] = konsole_path
    try:
        oc.load_theme()
        assert oc.GREEN == "38;5;72", oc.GREEN
        assert oc.ORANGE == "38;5;203", oc.ORANGE
        assert oc.RED == "38;5;205", oc.RED
        assert oc.YELLOW == "38;5;101", oc.YELLOW
        assert oc.BLUE == "38;5;110", oc.BLUE
        assert oc.PURPLE == "38;5;146", oc.PURPLE
    finally:
        os.environ.pop("OMENCTL_THEME", None)
        os.environ.pop("OMENCTL_KONSOLE_THEME", None)
        oc.GREEN, oc.YELLOW, oc.ORANGE, oc.RED, oc.BLUE, oc.PURPLE = old
        os.unlink(kde_path)
        os.unlink(konsole_path)
    print("PASS  Material You theme (KDE severity + Konsole accents)")


def main():
    checks = 0
    for width in (40, 60, 78, 92, 120, 160):
        for level in (0, 1, 2):
            for no_color in (False, True):
                max_width = 78 if width <= 78 else 400
                lines = render(width, level, max_width, True, no_color)
                assert lines, (width, level, no_color)
                for line in lines:
                    text = plain(line)
                    assert len(text) <= max(width, 78) + 8, (width, level, text)
                checks += 1

    tui = "\n".join(plain(l) for l in render(120, 0, 400, False, True))
    assert "86°C!" in tui, "hot-die marker missing"
    assert "fan " in tui, "fan-duty label missing"
    assert "bar = fan duty" in tui and "temperature history" in tui, "legend missing"
    assert "ec57" not in tui, "debug line should be hidden in the TUI"

    debug = "\n".join(plain(l) for l in render(120, 0, 400, True, True))
    assert "ec57" in debug, "debug toggle should show the EC line"

    status = "\n".join(plain(l) for l in render(78, 0, 78, True, True))
    assert "86°C!" in status
    assert "fan " not in status, "status view should stay compact"
    assert "ec57" in status, "status keeps the debug line"

    print("PASS  %d layout combinations" % checks)
    print("PASS  hot-die marker, fan label, legend, debug toggle")
    theme_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
