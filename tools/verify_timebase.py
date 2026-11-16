"""End-to-end check of the burst time axis through DeviceBackend, using the frame's dt.

With a square wave of known half period, every timebase whose record can hold a full
half period should report that half period (in seconds) with a small spread, for one and
for several active channels.

    .venv\\Scripts\\python tools\\verify_timebase.py --ch 3 --half-ms 22.5 --also 1

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
    runs = np.diff(np.r_[0, change, v.size])[1:-1]
    return runs[runs > 5]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, required=True)
    ap.add_argument("--half-ms", type=float, required=True)
    ap.add_argument("--also", type=int, default=None, help="second channel to activate")
    a = ap.parse_args()
    ch = a.ch - 1
    sets = [[ch]] + ([[ch, a.also - 1]] if a.also else [])
    b = B.DeviceBackend()
    b.open()
    print(f"{'time/div':>9} {'chs':>3} {'hw t/div':>9} {'span':>8} {'fps':>5} {'half-period':>12} "
          f"{'error':>6} {'spread':>6}")
    worst = 0.0
    for ns in B.BURST_NS_PER_DIV:
        for act in sets:
            b.configure(B.AcqConfig(active=sorted(act), ns_per_div=ns, trig_ch=ch))
            hw, span = B.burst_hw_timebase(ns)
            recs, t0 = [], time.perf_counter()
            while len(recs) < 5 and time.perf_counter() - t0 < max(3.0, 6 * span + 1):
                f = b.acquire_burst()
                if f is not None:
                    recs.append(f)
            el = time.perf_counter() - t0
            hp = np.concatenate([half_periods(r.data[ch], 6.0) * r.dt for r in recs]) if recs else []
            tag = f"{ns / 1e6:8.3f}m {len(act):3d} {hw / 1e6:8.3f}m {span * 1e3:6.1f}ms {len(recs) / el:5.1f}"
            if len(hp) == 0:
                print(f"{tag} {'(window < half period)':>26}")
                continue
            med = float(np.median(hp))
            err = med / (a.half_ms * 1e-3) - 1
            spread = float(np.std(hp) / med)
            worst = max(worst, abs(err))
            print(f"{tag} {med * 1e3:10.2f}ms {err:+6.1%} {spread:6.1%}")
    b.close()
    print(f"worst error on measurable timebases: {worst:.1%}")


if __name__ == "__main__":
    main()
