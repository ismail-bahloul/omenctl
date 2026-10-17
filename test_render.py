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


def render(width, level, max_width, show_debug, no_color, spacious=False):
    paint = (lambda code, text: text) if no_color else oc._marker_paint
    return oc.render(oc.FANS, 86.0, 77.0, REGS, RPMS, HIST, width, paint, 3.0, 85,
                     POWER, False, None, level, max_width, show_debug, spacious)


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


def curve_test():
    """Curve validation and the save/load round-trip."""
    import tempfile
    old_file = oc.CURVE_FILE
    old_thr = [list(f["thr"]) for f in oc.FANS]
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    oc.CURVE_FILE = path
    try:
        assert oc.curve_error(oc.FANS[0]["thr"]) is None
        assert "up must increase" in oc.curve_error([(50, 0, 12), (50, 49, 16)])
        assert "duty must not decrease" in oc.curve_error([(50, 0, 12), (60, 49, 10)])
        assert oc.save_curve_file() is None
        oc.FANS[0]["thr"] = [(40, 0, 10)]
        oc.FANS[1]["thr"] = [(40, 0, 10)]
        assert oc.load_curve_file()
        assert len(oc.FANS[0]["thr"]) == len(old_thr[0])
        assert len(oc.FANS[1]["thr"]) == len(old_thr[1])
    finally:
        oc.CURVE_FILE = old_file
        for i, thr in enumerate(old_thr):
            oc.FANS[i]["thr"] = thr
        os.unlink(path)
    print("PASS  curve validation and save/load")


def graph_test():
    """The curve editor's temperature -> duty graph."""
    thr = oc.FANS[0]["thr"]
    assert oc.curve_duty_at(thr, 30) == 12
    assert oc.curve_duty_at(thr, 65) == 20
    assert oc.curve_duty_at(thr, 100) == 100
    assert oc.curve_duty_at(thr, None) == 0

    rows, codes = oc.curve_graph(thr, 40, 8)
    assert len(rows) == len(codes) == 8
    assert all(len(r) == 40 and len(c) == 40 for r, c in zip(rows, codes))
    assert set("".join("".join(r) for r in rows)) <= set(oc.SPARK + "·█")
    duties = [oc.curve_duty_at(thr, 30 + 70 * x / 39) for x in range(40)]
    assert duties == sorted(duties), "graph must not go down with temperature"
    assert rows[-1][0] != "·" and rows[-1][-1] != "·", "area missing"
    assert all(code is None for code in codes[0][:10]), "duty 12% should not reach the top"
    assert oc.curve_graph([], 40, 8) == ([], [])
    assert oc.curve_graph(thr, 1, 8) == ([], [])
    print("PASS  curve graph (shape, monotonic duty, severity codes)")


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
    assert "● AC" in tui, "power-source dot missing"
    assert "fan " in tui, "fan-duty label missing"
    assert "bar = fan duty" in tui and "temperature history" in tui, "legend missing"
    assert "ec57" not in tui, "debug line should be hidden in the TUI"

    wide = [plain(l) for l in render(120, 0, 400, False, True)]
    assert len(wide[0]) == len(wide[3]) == len(wide[4]) == 118, \
        "wide rows should fill the layout width (right-aligned spark)"
    assert oc._vislen("\x01" + oc.BLUE + "\x02abc\x03") == 3
    assert oc._vislen("\x1b[1;31mabc\x1b[0m") == 3

    normal = render(120, 0, 400, False, True)
    airy = render(120, 0, 400, False, True, spacious=True)
    assert len(airy) == len(normal) + 3, "spacious view adds one blank per section"
    assert airy[3] == "", "Thermal header should be followed by a blank line"

    debug = "\n".join(plain(l) for l in render(120, 0, 400, True, True))
    assert "ec57" in debug, "debug toggle should show the EC line"

    status = "\n".join(plain(l) for l in render(78, 0, 78, True, True))
    assert "86°C!" in status
    assert "fan " not in status, "status view should stay compact"
    assert "ec57" in status, "status keeps the debug line"

    print("PASS  %d layout combinations" % checks)
    print("PASS  hot-die marker, power-source dot, fan label, legend, debug toggle")
    print("PASS  wide rows fill the width (right-aligned spark, accent titles)")
    print("PASS  spacious vertical rhythm for tall windows")
    theme_test()
    curve_test()
    graph_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
