"""Find where roll-mode samples go missing inside the GUI. Counts backend restarts/configs
and checks continuity of the raw worker stream vs the recorded CSV. Close Scope1008 first;
the reference square wave (NEP r900 on CH3) must be running."""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from scope1008 import backend as B  # noqa: E402
from scope1008.app import MainWindow, dark_palette  # noqa: E402

calls = {"configure": 0, "roll_start": 0, "roll_stop": 0}
durs = []
_rr = B.DeviceBackend.roll_read


def timed_rr(self):
    t = time.perf_counter()
    try:
        return _rr(self)
    finally:
        durs.append(time.perf_counter() - t)


B.DeviceBackend.roll_read = timed_rr
for name in calls:
    orig = getattr(B.DeviceBackend, name)

    def wrap(self, *a, _o=orig, _n=name, **k):
        calls[_n] += 1
        return _o(self, *a, **k)
    setattr(B.DeviceBackend, name, wrap)

if len(sys.argv) > 1:
    sys.setswitchinterval(float(sys.argv[1]))
    print("switchinterval", sys.getswitchinterval())
app = QApplication(sys.argv[:1])
dark_palette(app)
st = QSettings("Scope1008", "Scope1008")
saved = st.value("state")
w = MainWindow(demo=False)
w.show()
raw = []


def pump(sec, until=None):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.003)
        if w.worker is not None and not getattr(w.worker, "_hooked", False):
            w.worker.frame.connect(lambda fr: raw.append(fr))
            w.worker._hooked = True
        if until and until():
            return


pump(10, lambda: "connected" in w.status_lbl.text())
for k, c in enumerate(w.cards):
    c.btn.setChecked(k in (2, 3))
w.set_mode("roll")
w._sync_all()
w.push_config()
pump(3)


def phase(label, sec):
    raw.clear()
    durs.clear()
    pump(sec)
    rolls = [f for f in raw if f.kind == "roll"]
    if not rolls:
        print(label, "no frames")
        return
    v = np.concatenate([f.data[2] for f in rolls])
    r = np.flatnonzero(np.diff((v > 6).astype(np.int8)) == 1) + 1
    per = np.diff(r)
    med = np.median(per)
    bad = int((np.abs(per - med) > max(2, .1 * med)).sum())
    d = np.array(durs)
    print(f"{label:22s} frames/s {len(rolls) / sec:6.1f}  samples {v.size:6d}  bad {bad:3d}/{len(per)}  "
          f"roll_read ms median {np.median(d) * 1e3:6.1f} p99 {np.percentile(d, 99) * 1e3:7.1f} max {d.max() * 1e3:7.1f}")


phase("plain", 8)
w.fe_port.setCurrentIndex(w.fe_port.findData("COM11"))
w.fe_conn.setChecked(True)
pump(1)
phase("feather connected", 8)
w.rec_btn.setChecked(True)
phase("feather + recording", 8)
w.rec_btn.setChecked(False)
pump(1)
w.fe_conn.setChecked(False)
w.close()
pump(0.5)
if saved is None:
    st.remove("state")
else:
    st.setValue("state", saved)
