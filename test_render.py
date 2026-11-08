#!/usr/bin/env python3
"""Headless layout tests for render() -- no hardware, no root.

`render()` is the layout of `status` and `status -w`; the TUI's own pieces are
tested here too, up to the curses calls. These checks cover the widths, plus the
text markers that carry the temperature meaning when the terminal remaps colours.
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
    """Drop the ANSI colours, leaving what the terminal shows."""
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


def render(width, no_color):
    paint = oc._color(not no_color)
    return oc.render(oc.FANS, 86.0, 77.0, REGS, RPMS, HIST, width, paint, 3.0, 85, POWER, False)


def theme_test():
    """The Konsole scheme decides; the KDE one is only the fallback."""
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

[Foreground]
Color=228,216,241

[Background]
Color=12,10,15
"""
    fd, kde_path = tempfile.mkstemp(suffix=".colors")
    with os.fdopen(fd, "w") as f:
        f.write(kde)
    fd, konsole_path = tempfile.mkstemp(suffix=".colorscheme")
    with os.fdopen(fd, "w") as f:
        f.write(konsole)
    old = (oc.GREEN, oc.YELLOW, oc.ORANGE, oc.RED, oc.BLUE, oc.PURPLE, oc.MUTED, oc.OUTLINE)
    os.environ["OMENCTL_THEME"] = kde_path
    os.environ["OMENCTL_KONSOLE_THEME"] = konsole_path
    try:
        oc.load_theme()
        # the Konsole scheme wins: accents as they are
        exact = oc.THEME_EXACT
        assert exact[int(oc.BLUE.rsplit(";", 1)[1])] == "#929feb"
        assert exact[int(oc.PURPLE.rsplit(";", 1)[1])] == "#b99fea"
        # only two colours in this scheme: too few for a ramp, so the KDE roles stay
        assert exact[int(oc.GREEN.rsplit(";", 1)[1])] == "#31C193"
        assert oc.GREEN != oc.RED
        assert oc.MUTED and oc.OUTLINE and oc.MUTED != oc.OUTLINE, "edges tinted by the scheme"
    finally:
        os.environ.pop("OMENCTL_THEME", None)
        os.environ.pop("OMENCTL_KONSOLE_THEME", None)
        oc.GREEN, oc.YELLOW, oc.ORANGE, oc.RED, oc.BLUE, oc.PURPLE, oc.MUTED, oc.OUTLINE = old
        os.unlink(kde_path)
        os.unlink(konsole_path)
    print("PASS  Material You theme (everything from the Konsole scheme)")


def harmony_test():
    """Severity is picked from the scheme itself, whatever the wallpaper; true colour
    is only asked for when the terminal is known to have it."""
    blue = ["#6492de", "#b7a7c7", "#a1add0", "#a5b4de", "#7cc2fe", "#afbbdf", "#c8b5dd"]
    ramp = oc._palette_ramp(blue)
    assert len(ramp) == 4 and set(ramp) <= set(blue), ramp
    assert ramp[0] == "#7cc2fe" and ramp[-1] in ("#b7a7c7", "#c8b5dd"), "cool first, hot last"
    warm = ["#e2977e", "#d3a886", "#e8a1a6", "#92c9a0", "#929feb", "#b99fea", "#eea5cf"]
    ramp = oc._palette_ramp(warm)
    assert ramp[0] in ("#929feb", "#92c9a0") and ramp[-1] in ("#e2977e", "#d3a886"), ramp
    assert len(set(ramp)) == 4 and set(ramp) <= set(warm)
    assert oc._palette_ramp(["#6492de", "#6493de", "#6592de", "#6492df"]) is None, "no ramp"
    assert oc._palette_ramp([]) is None

    old = {k: os.environ.get(k) for k in ("TERM", "COLORTERM")}
    try:
        for term, colorterm, want in (("xterm-kitty", None, "kitty-direct"),
                                      ("xterm-256color", "truecolor", "konsole-direct"),
                                      ("xterm-256color", None, "xterm-256color"),
                                      ("tmux-256color", "truecolor", "tmux-256color"),
                                      ("linux", None, "linux")):
            os.environ["TERM"] = term
            os.environ.pop("COLORTERM", None)
            if colorterm:
                os.environ["COLORTERM"] = colorterm
            oc.use_direct_color()
            assert os.environ["TERM"] == want, (term, colorterm, os.environ["TERM"])
    finally:
        for key, value in old.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
    print("PASS  harmonised severity on any wallpaper, true colour detection")


def code_test():
    """Two theme colours that share a 256-colour entry keep their own codes."""
    oc.THEME_EXACT.clear()
    one, two = oc._theme_code("#a5b4de"), oc._theme_code("#a1add0")
    assert oc._hex_256("#a5b4de") == oc._hex_256("#a1add0"), "fixture must collide"
    assert one != two and oc.THEME_EXACT[int(two.rsplit(";", 1)[1])] == "#a1add0"
    assert oc._theme_code("#a5b4de") == one, "the same colour keeps its code"
    assert oc._ansi(two) == one, "status falls back to the shared entry"
    assert oc._ansi("38;5;114") == "38;5;114" and oc._ansi("1;38;5;9") == "1;38;5;9"
    assert oc._ansi("1;" + two) == "1;" + one, "bold prefix"
    assert "38;5;%d" % oc._hex_256("#a1add0") in oc._color(True)(two, "x")
    print("PASS  colliding theme colours keep distinct codes")


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


def gauge_test():
    """The temperature gauge: fixed width, scale ends, peak tick."""
    paint = lambda code, text: text
    assert oc._temp_gauge(30, 30, 20, paint) == "░" * 20
    assert oc._temp_gauge(100, 100, 20, paint) == "█" * 20
    assert oc._temp_gauge(65, 65, 20, paint) == "█" * 10 + "░" * 10
    assert oc._temp_gauge(20, 130, 20, paint) == "░" * 19 + "┃", "out-of-scale values clamp"
    ticked = oc._temp_gauge(44, 86, 20, paint)
    assert len(ticked) == 20 and ticked.count("┃") == 1 and ticked.index("┃") == 16
    assert "┃" not in oc._temp_gauge(86, 44, 20, paint), "no tick below the reading"
    bands = oc._temp_gauge(100, 100, 7, lambda code, text: code + " ").split()
    assert bands[0] == oc.GREEN and bands[-1] == oc.RED, "colour follows the scale"
    # a first frame has no history yet, and must still get its gauge
    row = oc.render(oc.FANS, 86.0, 77.0, REGS, RPMS, {"CPU": [], "GPU": []}, 78,
                    lambda code, text: text, 3.0, 85, POWER, False)[3]
    assert "░" in row and "┃" not in row, row
    print("PASS  temperature gauge (scale, clamping, peak tick, colours)")


def tui_test():
    """The TUI's pure parts: planning, graph, easing, rows, the worker thread."""
    import threading
    # the faders always fit; the rest goes in priority order, then the graphs
    assert oc._tui_plan(10, 120, 4, 3) == {"cols": 2, "thermal": False, "panel": 0, "graph": 0}
    assert oc._tui_plan(11, 120, 4, 3)["thermal"]
    roomy = oc._tui_plan(40, 120, 4, 3)
    assert roomy["panel"] == 4 and roomy["graph"] == 23, "the charts take the spare height"
    assert oc._tui_plan(200, 500, 4, 3)["graph"] == 183
    assert oc._tui_plan(24, 120, 4, 3)["graph"] == 7
    narrow = oc._tui_plan(40, 60, 4, 3)
    assert narrow["cols"] == 1 and narrow["graph"] == 8 and narrow["panel"] == 7, narrow
    assert oc._tui_plan(24, 60, 4, 3)["graph"] == 0, "short and narrow: one shared card"
    for h in range(1, 60):
        for w in (40, 71, 72, 120):
            for debug in (False, True):
                plan = oc._tui_plan(h, w, 4, 3, debug)
                base = (3 + plan["graph"] if plan["cols"] == 2
                        else 6 + 2 * plan["graph"] if plan["graph"] else 4)
                used = (4 + (1 if debug else 0) + 4 + (base if plan["thermal"] else 0)
                        + (plan["panel"] + 2 if plan["panel"] else 0))
                assert used <= max(h, 8 + (1 if debug else 0)), (h, w, debug, plan)

    # round axis, at least 20 °C, newest at the right
    rows, codes, top, bottom = oc._temp_graph([48, 49, 48, 50, 47, 48], 10, 4)
    assert len(rows) == 4 and all(len(r) == 10 for r in rows) and len(codes) == 10
    assert (top, bottom) == (60, 40), (top, bottom)
    assert rows[-1][:4] == "    " and codes[0] is None and rows[-1][-1] == "█"
    assert oc._temp_graph([], 10, 4)[0] == [] and oc._temp_graph([50], 0, 4)[0] == []
    # a fuller column for a hotter sample, and the colour follows each sample
    rows, codes, top, bottom = oc._temp_graph([40, 90], 2, 2)
    assert (top, bottom) == (90, 40) and rows[0] == " █" and codes == [oc.GREEN, oc.RED]
    assert rows[1] == "▁█", rows
    # the scale stays put while a value moves inside it
    assert oc._temp_graph([51, 53], 5, 4)[2:] == oc._temp_graph([52, 58], 5, 4)[2:]
    assert oc._temp_graph([100, 100], 5, 4)[2:] == (120, 100)
    # jitter around a mid-scale value must not flip the axis
    assert oc._temp_graph([54, 55], 5, 4)[2:] == oc._temp_graph([55, 56], 5, 4)[2:]

    assert oc._trend([50] * 5) == ""
    assert oc._trend([50] * 15 + [60] * 5) == "↗"
    assert oc._trend([60] * 15 + [50] * 5) == "↘"
    assert oc._trend([50] * 20) == "→"

    assert oc._ease(50.0, 50.0, 0.03) == 50.0
    assert 50.0 < oc._ease(50.0, 60.0, 0.03) < 60.0
    assert oc._ease(59.95, 60.0, 0.03) == 60.0, "must settle, or the UI never goes idle"

    labels = [r[0] for r in oc._tui_power_rows(POWER)]
    assert labels == ["cpu pkg", "cpu", "gpu"], labels
    assert [r[0] for r in oc._tui_battery_rows(POWER)] == ["charge", "health", "limit"]
    assert "limit" not in [r[0] for r in oc._tui_battery_rows(dict(POWER, bat_extender=False))]
    assert oc._tui_battery_rows(dict(POWER, bat_pct=None)) == []
    discharging = dict(POWER, bat_status="Discharging", bat_w=20.0)
    assert [r[0] for r in oc._tui_power_rows(discharging)][1] == "system"

    class Mon:
        fail = False

        def sample(self):
            if self.fail:
                raise OSError("EC gone")
            return {"cpu": 50.0}

    mon, started = Mon(), threading.Event()
    feed = oc.TuiFeed(mon)
    feed.start()
    ticket = feed.submit(lambda: (started.set(), "done")[1])
    assert started.wait(2)
    for _ in range(200):
        sample, done, notes = feed.take()
        if sample is not None:
            break
        threading.Event().wait(0.01)
    assert sample == {"cpu": 50.0} and done >= ticket and notes == ["done"], (sample, done, notes)

    def boom():
        raise SystemExit("no such mode")
    mon.fail = True
    feed.submit(boom)
    seen = []
    for _ in range(200):
        seen += feed.take()[2]
        if len(seen) >= 2:
            break
        threading.Event().wait(0.01)
    assert seen[0] == "failed: no such mode" and "EC gone" in seen[1], seen
    feed.stop()
    assert not feed.thread.is_alive()
    print("PASS  TUI plan, graph, trend, easing, rows, worker thread")


def editor_test():
    """The curve view's logic, without a screen."""
    import curses
    import tempfile
    old_file, old_thr = oc.CURVE_FILE, [list(f["thr"]) for f in oc.FANS]
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    oc.CURVE_FILE = path
    try:
        e = oc.CurveEditor(1)
        assert e.fan == 1 and not e.dirty and oc.FANS[1]["thr"] == list(oc.DEFAULT_THR[1])

        # bounds of each column, and a dirty flag
        before = oc.FANS[1]["thr"][0]
        e.col = 2
        assert not e.handle(curses.KEY_RIGHT)
        assert oc.FANS[1]["thr"][0][2] == before[2] + 1 and e.dirty
        for _ in range(30):
            e.handle(curses.KEY_NPAGE)
        assert oc.FANS[1]["thr"][0][2] == 100
        e.col = 1
        for _ in range(200):
            e.handle(curses.KEY_RIGHT)
        assert oc.FANS[1]["thr"][0][1] == oc.FANS[1]["thr"][0][0] - 1, "down stays below up"

        # leaving with unsaved edits asks once; another key cancels the question
        assert not e.handle(ord("q")) and e.confirm_quit and "unsaved" in e.msg
        assert not e.handle(curses.KEY_UP) and not e.confirm_quit and e.msg == ""
        assert not e.handle(27)
        assert e.handle(27), "second Esc discards"
        assert oc.CurveEditor(1).dirty is False
        assert oc.FANS[1]["thr"] == list(oc.DEFAULT_THR[1]), "a discarded edit must not stay"

        # save, and what is saved comes back
        e = oc.CurveEditor(0)
        e.col = 2
        e.handle(curses.KEY_RIGHT)
        saved = list(oc.FANS[0]["thr"])
        assert not e.handle(ord("s")) and not e.dirty and "saved" in e.msg
        assert e.handle(ord("q")), "nothing unsaved: leaves at once"
        assert oc.CurveEditor(0) and oc.FANS[0]["thr"] == saved

        e = oc.CurveEditor(0)
        e.handle(ord("r"))
        assert oc.FANS[0]["thr"] == list(oc.DEFAULT_THR[0]) and e.dirty
        e.handle(ord("2"))
        assert e.fan == 1 and e.row == 0
        assert e.handle(ord("Q"))

        # the mouse follows the frame's hit boxes
        e = oc.CurveEditor(0)
        e.buttons = [(0, 18, 25, ("fan", 1)), (5, 26, 32, ("cell", 1, 2))]
        e.click(28, 5, curses.BUTTON1_PRESSED)
        assert (e.row, e.col) == (1, 2) and not e.dirty
        duty = oc.FANS[0]["thr"][1][2]
        e.click(28, 5, getattr(curses, "BUTTON4_PRESSED", 0))
        assert oc.FANS[0]["thr"][1][2] == duty + 1 and e.dirty
        e.click(20, 0, curses.BUTTON1_PRESSED)
        assert e.fan == 1
        e.click(70, 20, curses.BUTTON1_PRESSED)
        assert e.fan == 1, "a click elsewhere does nothing"

        # layout: the table scrolls in a small window, nothing is out of reach
        assert oc._scroll_first(0, 8, 8) == 0
        assert oc._scroll_first(7, 8, 3) == 5 and oc._scroll_first(0, 8, 3) == 0
        for row in range(8):
            first = oc._scroll_first(row, 8, 3)
            assert first <= row < first + 3, row
        big = oc._curve_layout(40, 120, 8)
        assert big["visible"] >= 8 and big["status"] == 2 and big["chart"][3] == 82
        small = oc._curve_layout(9, 120, 8)
        assert small["visible"] == 1 and small["status"] == 1, small
        narrow = oc._curve_layout(40, 60, 8)
        assert narrow["table_w"] == 60 and narrow["chart"][0] == 2 + narrow["table_h"]
        assert oc._curve_layout(16, 60, 8)["chart"] is None, "no room for a chart"
    finally:
        oc.CURVE_FILE = old_file
        for i, thr in enumerate(old_thr):
            oc.FANS[i]["thr"] = thr
        if os.path.exists(path):
            os.unlink(path)
    print("PASS  curve view (bounds, leaving, save, reset, mouse, layout)")


def main():
    checks = 0
    for width in (40, 60, 78, 92, 120):
        for no_color in (False, True):
            lines = render(width, no_color)
            assert lines, (width, no_color)
            for line in lines:
                text = plain(line)
                assert len(text) <= 78 + 8, (width, text)
            checks += 1

    status = "\n".join(plain(l) for l in render(78, True))
    assert "86°C!" in status, "hot-die marker missing"
    assert "● AC" in status, "power-source dot missing"
    assert "fan " not in status, "status view should stay compact"
    assert "ec57" in status, "status keeps the debug line"
    assert oc._vislen("\x1b[1;31mabc\x1b[0m") == 3

    print("PASS  %d layout combinations" % checks)
    print("PASS  hot-die marker, power-source dot, debug line")
    theme_test()
    harmony_test()
    code_test()
    curve_test()
    graph_test()
    gauge_test()
    tui_test()
    editor_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
