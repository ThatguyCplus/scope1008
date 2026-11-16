"""Probe raw burst timebase ids (command 0xa3) and measure what each one really does.

For each id, first set a known baseline id, then the probe id, then capture. If the
measured sample spacing follows the baseline, the device ignored the probe id. Uses a
square wave of known half period on one channel.

    .venv\\Scripts\\python tools\\probe_timebase_ids.py --ch 3 --half-ms 22.75

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

BASELINES = {21: "10ms", 25: "200ms"}  # ids confirmed: 10 ms/div and 200 ms/div


def half_periods(v, mid):
    hi = v > mid
    change = np.flatnonzero(np.diff(hi.astype(np.int8))) + 1
    runs = np.diff(np.r_[0, change, v.size])[1:-1]
    return runs[runs > 20]


def capture(b, ch, n, budget):
    recs, t0 = [], time.perf_counter()
    b._armed_at = None
    while len(recs) < n and time.perf_counter() - t0 < budget:
        f = b.acquire_burst()
        if f is not None:
            recs.append(f.data[ch])
    return recs, time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, required=True)
    ap.add_argument("--half-ms", type=float, required=True)
    ap.add_argument("--ids", default="0-31")
    a = ap.parse_args()
    lo, hi = (int(x) for x in a.ids.split("-"))
    ch, half = a.ch - 1, a.half_ms * 1e-3
    b = B.DeviceBackend()
    b.open()
    # 200 ms/div in cfg only so acquire_burst's capture timeout is generous (4 s)
    b.configure(B.AcqConfig(active=[ch], ns_per_div=200_000_000, trig_ch=ch))
    print(f"{'id':>3} {'after':>6} {'fps':>5} {'half-per samp':>14} {'spread':>6} {'dt':>10} "
          f"{'window':>9} {'edge-hit':>8}")
    for tid in range(lo, hi + 1):
        for base, label in BASELINES.items():
            b._send(0xa3, [base])
            capture(b, ch, 1, 6)  # let the baseline take effect
            b._send(0xa3, [tid])
            recs, el = capture(b, ch, 6, 8)
            if not recs:
                print(f"{tid:3d} {label:>6}  no frames in {el:.1f} s")
                continue
            n = recs[0].size
            hp = np.concatenate([half_periods(r, 6.0) for r in recs])
            hits = sum(int(np.any(np.diff((r > 6.0).astype(np.int8)) != 0)) for r in recs)
            if hp.size:
                med = float(np.median(hp))
                dt = half / med
                spread = float(np.std(hp) / med)
                print(f"{tid:3d} {label:>6} {len(recs) / el:5.1f} {med:14.0f} {spread:6.0%} "
                      f"{dt * 1e6:8.2f}us {dt * n * 1e3:7.1f}ms {hits}/{len(recs)}")
            else:
                win = hits / len(recs) * 2 * half / 2  # edges every half period
                print(f"{tid:3d} {label:>6} {len(recs) / el:5.1f} {'-':>14} {'':>6} {'-':>10} "
                      f"~{win * 1e3:6.2f}ms {hits}/{len(recs)}")
    b._send(0xa3, [25])
    b.close()


if __name__ == "__main__":
    main()
