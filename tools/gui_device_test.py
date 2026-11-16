"""Scripted end-to-end test of the GUI against the real scope.

Drives the window like a user: channel setup, timebase sweep, Normal/Single, roll mode
and CSV export. Saves screenshots to shots/device_*.png and prints one line per step.
Restores your saved settings afterwards. Close Scope1008 first.

    .venv\\Scripts\\python tools\\gui_device_test.py --ch 3 --level 6 --vdiv "5 V/div"
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from scope1008.app import MainWindow, dark_palette  # noqa: E402
from scope1008.backend import BURST_NS_PER_DIV  # noqa: E402

problems: list[str] = []


class Catch(logging.Handler):
    def emit(self, rec):
        if rec.levelno >= logging.WARNING:
            problems.append(f"{rec.levelname} {rec.name}: {rec.getMessage()[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, default=3)
    ap.add_argument("--level", type=float, default=6.0)
    ap.add_argument("--vdiv", default="5 V/div")
    ap.add_argument("--out", default=str(ROOT / "shots"))
    a = ap.parse_args()
    ch = a.ch - 1
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    logging.getLogger().addHandler(Catch())

    app = QApplication(sys.argv[:1])
    dark_palette(app)
    st = QSettings("Scope1008", "Scope1008")
    saved = st.value("state")

    w = MainWindow(demo=False)
    w.resize(1600, 950)
    w.show()
    frames = []

    def pump(sec, until=None):
        end = time.time() + sec
        while time.time() < end:
            app.processEvents()
            time.sleep(0.005)
            if w.worker is not None and not getattr(w.worker, "_hooked", False):
                w.worker.frame.connect(lambda fr: frames.append(fr))
                w.worker._hooked = True
            if until and until():
                return True
        return False

    def row(i):
        t = w.table
        for r in range(t.rowCount()):
            name = t.item(r, 0).text() if t.item(r, 0) else ""
            if name in (f"CH{i + 1}", w.channels[i].name):
                return [t.item(r, c).text() for c in range(t.columnCount())]
        return None

    t0 = time.time()
    ok = pump(10, lambda: "connected" in w.status_lbl.text())
    print(f"connect: {'ok' if ok else 'FAILED'} in {time.time() - t0:.1f} s | {w.status_lbl.text()[-60:]}")
    if not ok:
        print("banner:", w.banner_lbl.text()[:300])
        return 1

    # channel setup: only the signal channel, sensible scale, trigger on it
    for i, c in enumerate(w.cards):
        c.btn.setChecked(i == ch)
    w.cards[ch].vdiv.setCurrentIndex(w.cards[ch].vdiv.findText(a.vdiv))
    w.cards[ch].offset.setValue(-2.0)
    w.trig_src.setCurrentIndex(ch)
    w.meas_src.setCurrentIndex(ch)
    w.trig_level = a.level
    w.set_mode("auto")
    w._sync_all()
    w.push_config()
    pump(1.5)

    print(f"{'time/div':>9} {'frames':>6} {'state':>8} {'first frame':>11}  measurements (pp, freq, period, duty)")
    for ns in BURST_NS_PER_DIV:
        if ns < 500_000:
            continue
        w.tdiv_i = BURST_NS_PER_DIV.index(ns)
        w._sync_all()
        w.push_config()
        frames.clear()
        t = time.time()
        span = 10 * ns * 1e-9
        pump(max(2.5, 3.2 * span + 1.5), lambda: len(frames) >= 3)
        first = f"{(time.time() - t):.1f} s" if frames else "-"
        pump(0.4)
        r = row(ch)
        meas = f"{r[1]}, {r[6]}, {r[7]}, {r[8]}" if r else "no row"
        print(f"{ns / 1e6:7.1f}ms {len(frames):6d} {w.trig_state:>8} {first:>11}  {meas}")
        if ns in (1_000_000, 20_000_000, 100_000_000):
            w.grab().save(str(out / f"device_{ns // 1_000_000}ms.png"))
        if not frames:
            problems.append(f"no frames at {ns / 1e6:g} ms/div")

    # Normal + Single at 20 ms/div
    w.tdiv_i = BURST_NS_PER_DIV.index(20_000_000)
    w.set_mode("normal")
    w._sync_all()
    w.push_config()
    pump(3)
    print(f"normal @20ms: state {w.trig_state}")
    w.do_single()
    stopped = pump(6, lambda: not w.running)
    print(f"single: {'stopped after capture' if stopped else 'DID NOT STOP'} ({w.trig_state})")
    if not stopped:
        problems.append("single did not stop")
    w.run_btn.setChecked(True)
    w.set_mode("auto")

    # roll mode
    w.set_mode("roll")
    w._sync_all()
    w.push_config()
    pump(5)
    w.grab().save(str(out / "device_roll.png"))
    n = max((b.size for b in w.roll_buf.values()), default=0)
    print(f"roll: {n} points buffered | {w.info_lbl.text()}")
    if n == 0:
        problems.append("roll mode produced no data")

    # CSV export of the current screen
    csv_path = out / "device_export.csv"
    w._ask_path = lambda *a_, **k: str(csv_path)
    w.set_mode("auto")
    w.tdiv_i = BURST_NS_PER_DIV.index(10_000_000)
    w._sync_all()
    w.push_config()
    pump(3, lambda: w.last_x is not None and len(frames) > 0)
    pump(1)
    w.export_csv()
    lines = csv_path.read_text(encoding="utf-8").splitlines() if csv_path.exists() else []
    print(f"csv: {len(lines)} lines, header {lines[4] if len(lines) > 4 else '-'}")

    alive = w.worker is not None and w.worker.isRunning()
    print(f"worker still running: {alive}")
    w.close()
    pump(0.5)
    if saved is None:
        st.remove("state")
    else:
        st.setValue("state", saved)
    print("problems:" if problems else "problems: none")
    for p in problems:
        print("  ", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
