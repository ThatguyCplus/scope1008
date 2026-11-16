"""Compare Auto-mode behaviour with 2 vs 4 active channels on the real scope.

Reports frames/s, trigger state and how much the trigger point jitters between frames.
Restores your saved settings afterwards. Close Scope1008 first.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from scope1008.app import MainWindow, dark_palette  # noqa: E402
from scope1008.backend import BURST_NS_PER_DIV  # noqa: E402

app = QApplication(sys.argv[:1])
dark_palette(app)
st = QSettings("Scope1008", "Scope1008")
saved = st.value("state")
w = MainWindow(demo=False)
w.resize(1600, 950)
w.show()
frames, draws = [], []


def pump(sec, until=None):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.003)
        if w.worker is not None and not getattr(w.worker, "_hooked", False):
            w.worker.frame.connect(lambda fr: frames.append(time.perf_counter()))
            w.worker._hooked = True
        if until and until():
            return True
    return False


orig_redraw = w.redraw


def timed_redraw():
    t = time.perf_counter()
    orig_redraw()
    draws.append(time.perf_counter() - t)


w.redraw = timed_redraw
pump(10, lambda: "connected" in w.status_lbl.text())
print("status:", w.status_lbl.text()[-40:])


def run(chans, trig, ns, label):
    for i, c in enumerate(w.cards):
        c.btn.setChecked(i in chans)
    w.trig_src.setCurrentIndex(trig)
    w.trig_level = 6.0 if trig == 2 else w.trig_level
    w.tdiv_i = BURST_NS_PER_DIV.index(ns)
    w.set_mode("auto")
    w._sync_all()
    w.push_config()
    pump(1.5)
    frames.clear()
    draws.clear()
    states, xs0 = [], []
    t0 = time.time()
    while time.time() - t0 < 4:
        pump(0.05)
        states.append(w.trig_state)
        if w.last_x is not None:
            xs0.append(float(w.last_x[0]))
    fps = len(frames) / 4
    jit = np.std(xs0) / (ns * 1e-9) if xs0 else float("nan")
    print(f"{label:34s} fps {fps:5.1f}  redraw {np.median(draws) * 1e3 if draws else 0:5.1f} ms  "
          f"states {sorted(set(states))}  trace jitter {jit:5.2f} div  "
          f"cfg active={w.build_config().active}")


for ns in (1_000_000, 10_000_000):
    run([2, 3], 2, ns, f"2 ch (3,4) trig CH3 @{ns // 1_000_000} ms")
    run([2, 3, 4, 5], 2, ns, f"4 ch (3-6) trig CH3 @{ns // 1_000_000} ms")
    run([2, 3, 4, 5], 0, ns, f"4 ch (3-6) trig CH1(off) @{ns // 1_000_000} ms")
w.close()
pump(0.5)
if saved is None:
    st.remove("state")
else:
    st.setValue("state", saved)
