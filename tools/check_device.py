"""Hardware check: frame rate, recalibration, roll mode, and time-axis consistency.

If SAMPLES_PER_DIV is right, a steady signal measures the same frequency on every
timebase. Close Scope1008 first.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope1008 import backend as B  # noqa: E402
from scope1008 import dsp  # noqa: E402


def frames(b, n):
    out, ts = [], []
    for _ in range(n):
        t = time.perf_counter()
        fr = b.acquire_burst()
        ts.append(time.perf_counter() - t)
        if fr:
            out.append(fr)
    return out, statistics.median(ts)


def main():
    b = B.DeviceBackend()
    t = time.perf_counter()
    b.open()
    print(f"open+init {time.perf_counter() - t:.2f} s, fast_io={b.dev.fast_io}")

    b.configure(B.AcqConfig(active=list(range(8)), ns_per_div=500_000))
    frs, dt = frames(b, 5)
    print(f"8 ch burst: {dt * 1e3:.0f} ms/frame ({1 / dt:.1f} fps)")
    print("per-channel mean/pp (V):",
          " ".join(f"CH{c + 1} {v.mean():+.2f}/{np.ptp(v):.2f}" for c, v in sorted(frs[-1].data.items())))
    sig = max(frs[-1].data, key=lambda c: np.ptp(frs[-1].data[c]))
    print(f"busiest channel: CH{sig + 1}")

    print("timebase check on that channel:")
    for ns in (100_000, 200_000, 500_000, 1_000_000, 2_000_000, 5_000_000):
        b.configure(B.AcqConfig(active=[sig], ns_per_div=ns, trig_ch=sig))
        frs, dt = frames(b, 3)
        if not frs:
            print(f"  {ns / 1e3:>6.0f} us/div: no frame")
            continue
        v = frs[-1].data[sig]
        m = dsp.measure(v, frs[-1].dt)
        print(f"  {ns / 1e3:>6.0f} us/div: {dt * 1e3:4.0f} ms/frame  n={v.size}  "
              f"freq {dsp.si(m['freq'], 'Hz')}  pp {dsp.si(m['pp'], 'V')}  duty {m['duty']:.1f}%")

    t = time.perf_counter()
    b.recalibrate()
    print(f"recalibrate {time.perf_counter() - t:.2f} s, fast_io={b.dev.fast_io}")
    frs, dt = frames(b, 3)
    print(f"after recal: {dt * 1e3:.0f} ms/frame, frames {len(frs)}/3")

    b.configure(B.AcqConfig(active=[sig], roll=True, roll_rate=440))
    b.roll_start(440)
    t0, n = time.perf_counter(), 0
    while time.perf_counter() - t0 < 3:
        fr = b.roll_read()
        if fr:
            n += fr.data[sig].size
    print(f"roll 1 ch: {n / (time.perf_counter() - t0):.0f} Sa/s (expect ~{440 * B.rate_factor(1):.0f})")
    b.close()


if __name__ == "__main__":
    main()
