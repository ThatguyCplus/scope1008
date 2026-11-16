"""Record a combined session (Hantek roll + Feather serial) through the GUI and merge it.

Sends nothing to the Feather unless --start/--end are given (e.g. --start r900 --end r0).
Needs CH4 wired to Feather D13. Restores saved settings.
Close Scope1008 and any serial monitor on the Feather port first.

    .venv\\Scripts\\python tools\\gui_session_test.py --port COM11 --seconds 15
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from scope1008.app import MainWindow, dark_palette  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="COM11")
ap.add_argument("--seconds", type=float, default=15)
ap.add_argument("--start", default="", help="Feather command sent when recording starts")
ap.add_argument("--end", default="", help="Feather command sent before recording stops")
ap.add_argument("--channels", default="3,4", help="channels to record, e.g. 1,2,3,4")
ap.add_argument("--labels", default="", help="labels in channel order, e.g. VT3,VT6,NEP,SYNC")
ap.add_argument("--keep-labels", action="store_true", help="save the labels to your settings")
a = ap.parse_args()

app = QApplication(sys.argv[:1])
dark_palette(app)
st = QSettings("Scope1008", "Scope1008")
saved = st.value("state")
w = MainWindow(demo=False)
w.resize(1600, 950)
w.show()


def pump(sec, until=None):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.003)
        if until and until():
            return True
    return False


ok = pump(10, lambda: "connected" in w.status_lbl.text())
print("scope:", "ok" if ok else "FAILED " + w.banner_lbl.text()[:200])
i = w.fe_port.findData(a.port)
print("ports:", [w.fe_port.itemText(k) for k in range(w.fe_port.count())])
w.fe_port.setCurrentIndex(i)
w.fe_conn.setChecked(True)
pump(3)
print("feather:", w.fe_msg.text(), "|", w.fe_info.text())

chans = [int(x) - 1 for x in a.channels.split(",")]
labels = a.labels.split(",") if a.labels else []
for k, c in enumerate(w.cards):
    c.btn.setChecked(k in chans)
for k, name in zip(chans, labels):
    w.cards[k].name.setText(name)
    w.cards[k].name.editingFinished.emit()
w.set_mode("roll")
w._sync_all()
w.push_config()
pump(2)
w.rec_btn.setChecked(True)
print("recording:", w.status_lbl.text())
if a.start:
    pump(2)
    w.feather.send(a.start)
    print("sent", a.start)
pump(a.seconds)
if a.end:
    w.feather.send(a.end)
    print("sent", a.end)
    pump(2)
print("live:", w.fe_info.text(), "|", w.info_lbl.text())
w.rec_btn.setChecked(False)
pump(1)
print("result:", w.status_lbl.text())
sess = sorted((Path.home() / "Scope1008 sessions").glob("*"))[-1]
for f in sorted(sess.iterdir()):
    print(f"  {f.name:12s} {f.stat().st_size:>9,d} bytes")
if (sess / "session.txt").exists():
    print((sess / "session.txt").read_text(encoding="utf-8"))
w.close()
pump(0.5)
if saved is None:
    st.remove("state")
else:
    st.setValue("state", saved)
if a.keep_labels and labels and saved:
    import json
    s = json.loads(saved)
    for k, c in enumerate(s["channels"]):  # keep the recorded channels on, the rest off
        c["enabled"] = k in chans
    for k, name in zip(chans, labels):
        s["channels"][k]["name"] = name
    st.setValue("state", json.dumps(s))
    print("saved labels:", {f"CH{k + 1}": n for k, n in zip(chans, labels)})
