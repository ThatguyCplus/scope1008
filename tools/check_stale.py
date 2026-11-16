"""Does each burst timebase deliver fresh captures, or the same old buffer again?

A real capture always carries some ADC noise, so two consecutive fresh records are never
identical. Identical records mean the device ignored the timebase code and re-sent the
previous buffer. Also reports the half-period of a square wave in samples, if one is present,
so the real dt can be checked: dt = half_period_seconds / samples.

    .venv\\Scripts\\python tools\\check_stale.py --ch 3 [--half-ms 22.75]

Close Scope1008 first.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope1008 import backend as B  # noqa: E402


def half_periods(v, mid):
    hi = v > mid
    change = np.flatnonzero(np.diff(hi.astype(np.int8))) + 1
    runs = np.diff(np.r_[0, change, v.size])[1:-1]  # drop partial first/last
    return runs[runs > 20]  # ignore single-sample noise flips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, required=True)
    ap.add_argument("--half-ms", type=float, default=None, help="known half period in ms")
    ap.add_argument("--order", default="up", choices=["up", "down", "shuffle"])
    a = ap.parse_args()
    ch = a.ch - 1
    b = B.DeviceBackend()
    b.open()
    tbs = list(B.BURST_NS_PER_DIV)
    if a.order == "down":
        tbs.reverse()
    elif a.order == "shuffle":
        np.random.default_rng(0).shuffle(tbs)
    print(f"{'time/div':>9} {'fresh':>6} {'fps':>5} {'half-period samples':>22} {'real dt':>9} {'model dt':>9}")
    for ns in tbs:
        b.configure(B.AcqConfig(active=[ch], ns_per_div=ns, trig_ch=ch))
        recs, t0 = [], time.perf_counter()
        while len(recs) < 6 and time.perf_counter() - t0 < max(4.0, 7 * B.RECORD_DIVS * ns * 1e-9 + 1):
            f = b.acquire_burst()
            if f is not None:
                recs.append(f)
        el = time.perf_counter() - t0
        if len(recs) < 2:
            print(f"{ns / 1e6:8.3f}m  only {len(recs)} frame(s)")
            continue
        same = sum(np.array_equal(recs[i].data[ch], recs[i + 1].data[ch]) for i in range(len(recs) - 1))
        fresh = f"{len(recs) - 1 - same}/{len(recs) - 1}"
        hp = np.concatenate([half_periods(r.data[ch], 6.0) for r in recs])
        hp_s = f"{np.median(hp):.0f} (n={hp.size}, {np.std(hp) / np.median(hp):.0%})" if hp.size else "-"
        real = f"{a.half_ms * 1e-3 / np.median(hp) * 1e6:.2f}us" if (hp.size and a.half_ms) else "-"
        print(f"{ns / 1e6:8.3f}m {fresh:>6} {len(recs) / el:5.1f} {hp_s:>22} {real:>9} {recs[0].dt * 1e6:7.2f}us")
    b.close()


if __name__ == "__main__":
    main()
