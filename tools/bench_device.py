"""Time the real scope's USB path: connect/init, burst captures and roll mode.

Close Scope1008 first (only one program can hold the USB device).

    .venv\\Scripts\\python tools\\bench_device.py
"""
from __future__ import annotations

import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope1008 import backend as B  # noqa: E402
from scope1008.hantek1008 import Hantek1008Raw  # noqa: E402

stats = {"calls": 0, "usb_s": 0.0}
_orig = Hantek1008Raw._Hantek1008Raw__write_and_receive


def counted(self, *a, **k):
    t = time.perf_counter()
    try:
        return _orig(self, *a, **k)
    finally:
        stats["calls"] += 1
        stats["usb_s"] += time.perf_counter() - t


Hantek1008Raw._Hantek1008Raw__write_and_receive = counted


def sleep_resolution():
    xs = []
    for _ in range(50):
        t = time.perf_counter()
        time.sleep(0.002)
        xs.append((time.perf_counter() - t) * 1e3)
    return statistics.median(xs), max(xs)


def main():
    logging.basicConfig(level=logging.WARNING)
    med, mx = sleep_resolution()
    print(f"time.sleep(2 ms) actually takes: median {med:.2f} ms, max {mx:.2f} ms")

    b = B.DeviceBackend()
    t = time.perf_counter()
    b.open()
    print(f"open+init: {time.perf_counter() - t:.2f} s, {stats['calls']} USB commands")

    for active in ([0], [0, 1], list(range(8))):
        cfg = B.AcqConfig(active=active, ns_per_div=500_000)
        b.configure(cfg)
        times, calls, ns, none = [], [], [], 0
        for _ in range(8):
            stats.update(calls=0, usb_s=0.0)
            t = time.perf_counter()
            fr = b.acquire_burst()
            times.append(time.perf_counter() - t)
            calls.append(stats["calls"])
            if fr is None:
                none += 1
            else:
                ns.append(min(len(v) for v in fr.data.values()))
        print(f"burst {len(active)} ch: {statistics.median(times) * 1e3:.0f} ms/frame "
              f"(min {min(times) * 1e3:.0f}, max {max(times) * 1e3:.0f}), "
              f"{statistics.median(calls):.0f} USB cmds/frame, samples/ch {ns[:1]}, no-trigger {none}/8")

    cfg = B.AcqConfig(active=[0, 1], roll=True, roll_rate=440)
    b.configure(cfg)
    b.roll_start(440)
    t0 = time.perf_counter()
    gaps, n, last = [], 0, t0
    while time.perf_counter() - t0 < 4:
        stats.update(calls=0)
        fr = b.roll_read()
        now = time.perf_counter()
        if fr is not None:
            n += min(len(v) for v in fr.data.values())
            gaps.append(now - last)
            last = now
    el = time.perf_counter() - t0
    print(f"roll 2 ch @440: {n / el:.0f} Sa/s/ch measured (expect ~{440 * B.rate_factor(2):.0f}), "
          f"{len(gaps)} chunks, gap median {statistics.median(gaps) * 1e3:.0f} ms max {max(gaps) * 1e3:.0f} ms")
    b.close()


if __name__ == "__main__":
    main()
