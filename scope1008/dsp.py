"""Triggering, measurements and FFT. Pure numpy so it can be tested without a GUI."""
from __future__ import annotations

import math
from typing import Dict, Optional

import numpy as np


def deglitch(v: np.ndarray) -> np.ndarray:
    """3-point median: removes single-sample spikes, keeps edge positions.

    Seen on a real 1008B: one sample right after a fast edge can drop back to the old level,
    which would otherwise count as two extra edges and wreck frequency and trigger.
    """
    if v.size < 3:
        return v
    out = v.copy()
    out[1:-1] = np.median(np.stack([v[:-2], v[1:-1], v[2:]]), axis=0)
    return out


def edge_crossings(v: np.ndarray, level: float, rising: bool, hyst: float) -> np.ndarray:
    """Fractional sample positions of edges through `level`, with hysteresis.

    A rising edge counts only if the signal dipped below level-hyst since it was last
    above level, which kills noise-induced double triggers.
    """
    if v.size < 2:
        return np.empty(0)
    x = v if rising else -v
    lv = level if rising else -level
    above = x >= lv
    below = x < lv - hyst
    idx = np.arange(x.size)
    last_below = np.maximum.accumulate(np.where(below, idx, -1))
    last_above = np.maximum.accumulate(np.where(above, idx, -1))
    i = np.nonzero(above[1:] & ~above[:-1])[0] + 1
    if i.size == 0:
        return np.empty(0)
    ok = last_below[i - 1] > last_above[i - 1]
    i = i[ok]
    if i.size == 0:
        return np.empty(0)
    x0, x1 = x[i - 1], x[i]
    frac = np.where(x1 != x0, (lv - x0) / (x1 - x0), 0.0)
    return (i - 1) + frac


def choose_trigger(pos: np.ndarray, n: int, pre: float, post: float) -> Optional[float]:
    """Pick the first edge that leaves `pre` samples before and `post` after; else the
    edge closest to the middle of the record."""
    if pos.size == 0:
        return None
    ok = pos[(pos >= pre) & (pos <= n - 1 - post)]
    if ok.size:
        return float(ok[0])
    return float(pos[np.argmin(np.abs(pos - n / 2))])


def measure(v: np.ndarray, dt: float) -> Dict[str, float]:
    out = {k: math.nan for k in ("max", "min", "pp", "mean", "rms", "freq", "period", "duty", "rise")}
    if v.size < 4:
        return out
    vmax, vmin = float(v.max()), float(v.min())
    out.update(max=vmax, min=vmin, pp=vmax - vmin, mean=float(v.mean()),
               rms=float(np.sqrt(np.mean(v * v))))
    pp = vmax - vmin
    if pp <= 1e-6:
        return out
    mid = vmin + pp / 2
    # noise estimate from sample-to-sample jitter; a flat noisy line must not read as kHz
    noise = 1.4826 * float(np.median(np.abs(np.diff(v)))) / math.sqrt(2)
    hyst = max(0.1 * pp, 5 * noise)
    if hyst >= pp / 2:
        return out
    v = deglitch(v)  # timing measurements ignore 1-sample spikes; levels above stay raw
    r = edge_crossings(v, mid, True, hyst)
    if r.size >= 2:
        period = (r[-1] - r[0]) / (r.size - 1) * dt
        out["period"] = period
        out["freq"] = 1.0 / period if period > 0 else math.nan
        seg = v[int(math.ceil(r[0])):int(r[-1])]
        if seg.size:
            out["duty"] = float(np.mean(seg >= mid) * 100)
    # 10-90 rise time of the first full rising edge
    lo, hi = vmin + 0.1 * pp, vmin + 0.9 * pp
    a = edge_crossings(v, lo, True, 0.05 * pp)
    b = edge_crossings(v, hi, True, 0.05 * pp)
    if a.size and b.size:
        j = np.searchsorted(b, a[0])
        if j < b.size:
            out["rise"] = (b[j] - a[0]) * dt
    return out


def fft_dbv(v: np.ndarray, dt: float):
    """Single-sided spectrum in dBV RMS with a Hann window."""
    n = v.size
    if n < 16:
        return np.empty(0), np.empty(0)
    w = np.hanning(n)
    spec = np.fft.rfft((v - v.mean()) * w)
    mag = np.abs(spec) * 2 / w.sum() / math.sqrt(2)
    f = np.fft.rfftfreq(n, dt)
    return f, 20 * np.log10(np.maximum(mag, 1e-9))


def si_short(value: float, unit: str) -> str:
    """Like si() but drops trailing zeros: 2 V, 500 mV, 1.5 kHz."""
    s = si(value, unit, 3)
    num, _, rest = s.partition(" ")
    if "." in num:
        num = num.rstrip("0").rstrip(".")
    return f"{num} {rest}"


def si(value: float, unit: str, digits: int = 3) -> str:
    if value is None or not math.isfinite(value):
        return "--"
    if value == 0:
        return f"0 {unit}"
    prefixes = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")]
    a = abs(value)
    for scale, p in prefixes:  # noqa: B007 - p is used after the loop
        if a >= scale * 0.9995:
            break
    s = value / scale
    decimals = max(0, digits - 1 - int(math.floor(math.log10(abs(s)))) if s else 0)
    return f"{s:.{decimals}f} {p}{unit}"
