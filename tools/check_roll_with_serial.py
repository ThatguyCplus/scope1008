"""Roll-mode continuity with and without a concurrent Feather serial reader (no GUI).

Separates "the serial thread starves the USB reader (GIL)" from "the Feather's USB traffic
disturbs the scope". Close Scope1008 first; NEP (r900) must be running on CH3.
"""
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope1008 import backend as B  # noqa: E402

sys.setswitchinterval(0.0005)


def roll(b, sec):
    b.configure(B.AcqConfig(active=[2, 3], roll=True, roll_rate=440))
    b.roll_start(440)
    chunks, durs, t0 = [], [], time.perf_counter()
    while time.perf_counter() - t0 < sec:
        t = time.perf_counter()
        fr = b.roll_read()
        durs.append(time.perf_counter() - t)
        if fr is not None:
            chunks.append(fr.data[2])
    b.roll_stop()
    v = np.concatenate(chunks)
    r = np.flatnonzero(np.diff((v > 6).astype(np.int8)) == 1) + 1
    per = np.diff(r)
    med = np.median(per)
    return int((np.abs(per - med) > max(2, .1 * med)).sum()), len(per), np.median(durs), max(durs)


def serial_reader(stop, mode, counter):
    import serial
    s = serial.Serial("COM11", 115200, timeout=0.1)
    while not stop.is_set():
        if mode == "batched":
            n = s.in_waiting
            if not n:
                time.sleep(0.02)
                continue
            data = s.read(n)
        else:
            data = s.read(1)
        counter[0] += len(data)
    s.close()


b = B.DeviceBackend()
b.open()
print("no serial      bad %d/%d  read median %.1f ms max %.1f ms" % (lambda r: (r[0], r[1], r[2] * 1e3, r[3] * 1e3))(roll(b, 10)))
for mode in ("batched", "bytewise"):
    stop, cnt = threading.Event(), [0]
    th = threading.Thread(target=serial_reader, args=(stop, mode, cnt), daemon=True)
    th.start()
    r = roll(b, 10)
    stop.set()
    th.join()
    print(f"serial {mode:8s} bad {r[0]}/{r[1]}  read median {r[2] * 1e3:.1f} ms max {r[3] * 1e3:.1f} ms  "
          f"({cnt[0] / 10 / 1000:.1f} kB/s from Feather)")
b.close()
