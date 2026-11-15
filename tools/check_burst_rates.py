"""Frames per second the backend delivers at each timebase (calls acquire_burst in a loop
the way the worker does). Close Scope1008 first."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope1008 import backend as B  # noqa: E402

b = B.DeviceBackend()
b.open()
for ns in (100_000, 500_000, 2_000_000, 10_000_000, 50_000_000, 200_000_000):
    for nch in (1, 8):
        b.configure(B.AcqConfig(active=list(range(nch)), ns_per_div=ns))
        t0, frames, longest, dt = time.perf_counter(), 0, 0.0, None
        budget = max(2.0, 3 * B.RECORD_DIVS * ns * 1e-9 + 0.5)
        while time.perf_counter() - t0 < budget:
            t = time.perf_counter()
            fr = b.acquire_burst()
            longest = max(longest, time.perf_counter() - t)
            if fr:
                frames += 1
                dt, n = fr.dt, min(len(v) for v in fr.data.values())
        el = time.perf_counter() - t0
        print(f"{ns / 1e6:6.1f} ms/div {nch} ch: {frames / el:5.2f} fps "
              f"(ideal {1 / (B.RECORD_DIVS * ns * 1e-9 + 0.066):5.2f}), longest call {longest * 1e3:4.0f} ms, "
              f"{n} pts, {1 / dt / 1e3:.1f} kSa/s/ch")
b.close()
