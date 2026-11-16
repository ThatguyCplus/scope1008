"""Is the roll-mode stream continuous? Counts impossible periods of a steady square wave.

Records roll mode with fast_io on and off and reports how many periods of the reference
signal deviate from the median (each one is a gap or splice in the stream).

    .venv\\Scripts\\python tools\\check_roll_continuity.py --ch 3 --also 4 --seconds 20

Close Scope1008 first. The reference signal must be running (e.g. NEP r900 on CH3).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scope1008 import backend as B  # noqa: E402


def run(b, act, ch, seconds, rate):
    b.configure(B.AcqConfig(active=act, roll=True, roll_rate=rate))
    b.roll_start(rate)
    chunks, sizes, t0 = [], [], time.perf_counter()
    dt = None
    while time.perf_counter() - t0 < seconds:
        fr = b.roll_read()
        if fr is not None:
            chunks.append(fr.data[ch])
            sizes.append(fr.data[ch].size)
            dt = fr.dt
    b.roll_stop()
    v = np.concatenate(chunks)
    thr = (np.percentile(v, 5) + np.percentile(v, 95)) / 2
    r = np.flatnonzero(np.diff((v > thr).astype(np.int8)) == 1) + 1
    per = np.diff(r)
    med = np.median(per)
    bad = np.abs(per - med) > max(2, 0.1 * med)
    return v.size, dt, med, int(bad.sum()), len(per), np.median(sizes), max(sizes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch", type=int, default=3)
    ap.add_argument("--also", type=int, default=None)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--rate", type=float, default=440)
    a = ap.parse_args()
    ch = a.ch - 1
    act = sorted({ch} | ({a.also - 1} if a.also else set()))
    b = B.DeviceBackend()
    b.open()
    for fast in (True, False):
        b.dev.fast_io = fast
        n, dt, med, bad, total, cmed, cmax = run(b, act, ch, a.seconds, a.rate)
        print(f"fast_io={fast!s:5}: {n} samples ({n * dt:.1f} s of signal), period {med * dt * 1e3:.1f} ms, "
              f"bad periods {bad}/{total} ({bad / max(1, total) / a.seconds * total / max(1, total):.0%}) "
              f"-> {bad / a.seconds:.2f}/s | chunk median {cmed:.0f} max {cmax} samples")
    b.close()


if __name__ == "__main__":
    main()
