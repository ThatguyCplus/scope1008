"""Run the real GUI against the scope for a few seconds and report the display rate."""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from scope1008.app import MainWindow, dark_palette  # noqa: E402

app = QApplication(sys.argv[:1])
dark_palette(app)
w = MainWindow(demo=False)
if len(sys.argv) > 1:  # optional timebase in us/div, e.g. 500
    from scope1008.backend import BURST_NS_PER_DIV
    w.tdiv_i = BURST_NS_PER_DIV.index(int(float(sys.argv[1]) * 1000))
    w._sync_all()
    w.push_config()
w.resize(1600, 950)
w.show()
frames = []
fills = []
w.worker_frame_hook = None
end = time.time() + 10
while time.time() < end:
    app.processEvents()
    time.sleep(0.002)
    if w.worker is not None and not getattr(w.worker, "_hooked", False):
        w.worker.frame.connect(lambda fr: frames.append(time.perf_counter()))
        w.worker.progress.connect(lambda f: fills.append(round(f, 2)))
        w.worker._hooked = True
print("status:", w.status_lbl.text()[-80:])
print("info:", w.info_lbl.text())
print("fill updates:", len(fills), fills[:12])
late = [b - a for a, b in zip(frames[len(frames) // 2:], frames[len(frames) // 2 + 1:])]
if late:
    late.sort()
    print(f"frames {len(frames)}, steady-state gap median {late[len(late) // 2] * 1e3:.0f} ms, "
          f"95th pct {late[int(len(late) * .95)] * 1e3:.0f} ms, max {late[-1] * 1e3:.0f} ms")
w.close()
app.processEvents()
