"""Align a Feather serial log with a Scope1008 roll recording using the D13 sync pulses.

Ported from throttleECU steering_control_unit/data_capture/tools/merge_hantek_feather.py
so a recording session can be merged without leaving the app.

The Feather toggles D13 at pseudo-random 200-1000 ms intervals and logs the exact micros()
of every edge ("S,us,level,n"). One scope channel records D13. The interval pattern is
matched between both lists and  scope_time = a * feather_time + b  is fitted, which also
absorbs clock drift and the roll-mode sample-rate error. The Feather's values are then
added as columns on the scope's time axis (zero-order hold).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAS_STALE_S = 0.25


@dataclass
class MergeResult:
    ok: bool
    message: str
    edges: int = 0
    residual_ms: float = float("nan")
    interval_err_ms: float = float("nan")
    can_frames: int = 0


def read_scope(path: Path):
    with open(path, encoding="utf-8") as f:
        rows = list(csv.reader(line for line in f if not line.startswith("#")))
    header, body = rows[0], [r for r in rows[1:] if r]
    data = np.array([[float(x) if x not in ("", "nan") else np.nan for x in r] for r in body], dtype=float)
    return header, data.reshape(-1, len(header))


def scope_edges(t: np.ndarray, v: np.ndarray, lo: float, hi: float):
    """Edge times (interpolated at the midpoint) and directions, with hysteresis."""
    mid = (lo + hi) / 2
    times, dirs = [], []
    state = bool(v[0] > mid) if v.size else False
    for i in range(1, len(v)):
        if not state and v[i] > hi:
            j = i
            while j > 1 and v[j - 1] > mid:
                j -= 1
            rising = True
        elif state and v[i] < lo:
            j = i
            while j > 1 and v[j - 1] < mid:
                j -= 1
            rising = False
        else:
            continue
        a, b = v[j - 1], v[j]
        frac = (mid - a) / (b - a) if b != a else 0.0
        times.append(t[j - 1] + frac * (t[j] - t[j - 1]))
        dirs.append(1 if rising else 0)
        state = rising
    return np.array(times), np.array(dirs, dtype=int)


def read_can(path: Path):
    """C lines ("C,us,id,ext,rtr,len,hex") as (feather_s, raw_us, id, ext, rtr, len, hex)."""
    out, wraps, prev = [], 0, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "\tC," not in line:
                continue
            p = line.rstrip("\n").split("\t", 1)[1].split(",")
            if len(p) != 7:
                continue
            try:
                us = int(p[1])
                if prev is not None and prev - us > 2**31:
                    wraps += 1
                prev = us
                out.append(((us + wraps * 2**32) / 1e6, us, int(p[2], 16), int(p[3]), int(p[4]), int(p[5]),
                            p[6]))
            except ValueError:
                continue
    return out


def read_feather(path: Path):
    """S, A and N records from a feather log ("<host time>\\t<line>"), micros() unwrapped."""
    sync, sas, nep = [], [], []
    wraps, prev = 0, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#") or "\t" not in line:
                continue
            msg = line.rstrip("\n").split("\t", 1)[1]
            parts = msg.split(",")
            if parts[0] not in ("S", "A", "N") or len(parts) < 3:
                continue
            try:
                us = int(parts[1])
                if prev is not None and prev - us > 2**31:  # micros() wrap (71.6 min)
                    wraps += 1
                prev = us
                t = (us + wraps * 2**32) / 1e6
                if parts[0] == "S":
                    sync.append((t, int(parts[2])))
                elif parts[0] == "A" and len(parts) >= 5:
                    sas.append((t, float(parts[2]), float(parts[3]), float(parts[4])))
                elif parts[0] == "N":
                    nep.append((t, float(parts[2])))
            except ValueError:
                continue
    return (np.array(sync, dtype=float).reshape(-1, 2), np.array(sas, dtype=float).reshape(-1, 4),
            np.array(nep, dtype=float).reshape(-1, 2))


def match(ts, ds, tf, df, min_overlap=6):
    """Best index shift s (scope edge i <-> feather edge i + s) by interval pattern."""
    dts, dtf = np.diff(ts), np.diff(tf)
    best = None
    for s in range(-(len(ts) - min_overlap), len(tf) - min_overlap + 1):
        i0, i1 = max(0, -s), min(len(ts), len(tf) - s)
        if i1 - i0 < min_overlap:
            continue
        err = np.abs(dts[i0:i1 - 1] - dtf[i0 + s:i1 - 1 + s])
        score = np.median(err) + 0.1 * np.mean(ds[i0:i1] != df[i0 + s:i1 + s])
        if best is None or score < best[0]:
            best = (score, s, i0, i1)
    return best


def hold(t_query, t_src, values, max_age=None):
    """Zero-order hold: last value at or before each query time."""
    idx = np.searchsorted(t_src, t_query, side="right") - 1
    out = np.full((len(t_query),) + values.shape[1:], np.nan)
    ok = idx >= 0
    if max_age is not None:
        ok &= (t_query - t_src[np.clip(idx, 0, None)]) <= max_age
    out[ok] = values[idx[ok]]
    return out


def merge(scope_csv: Path, feather_log: Path, out_csv: Path, sync: str = "CH4",
          lo: float = 1.0, hi: float = 2.3) -> MergeResult:
    header, data = read_scope(scope_csv)
    col = next((i for i, h in enumerate(header) if h.upper().startswith(sync.upper())), None)
    if col is None:
        return MergeResult(False, f"{sync} was not recorded; switch it on and wire it to Feather D13")
    t_scope = data[:, 0]
    ts, ds = scope_edges(t_scope, data[:, col], lo, hi)
    syn, sas, nep = read_feather(feather_log)
    if len(ts) < 6 or len(syn) < 6:
        return MergeResult(False, f"need 6+ sync edges in both (scope {len(ts)}, Feather {len(syn)}); "
                                  f"is {sync} on Feather D13 and the recording longer than ~5 s?")
    best = match(ts, ds, syn[:, 0], syn[:, 1].astype(int))
    if best is None:
        return MergeResult(False, "scope and Feather logs do not overlap by 6 sync edges")
    score, s, i0, i1 = best
    a, b = np.polyfit(syn[i0 + s:i1 + s, 0], ts[i0:i1], 1)
    resid = ts[i0:i1] - (a * syn[i0 + s:i1 + s, 0] + b)
    rms = float(np.sqrt(np.mean(resid ** 2)))

    sas_cols = (hold(t_scope, a * sas[:, 0] + b, sas[:, 1:], SAS_STALE_S) if len(sas)
                else np.full((len(t_scope), 3), np.nan))
    nep_col = hold(t_scope, a * nep[:, 0] + b, nep[:, 1:]) if len(nep) else np.full((len(t_scope), 1), np.nan)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        f.write(f"# merged {scope_csv.name} + {feather_log.name}; feather->scope a={a:.9f} b={b:.6f}; "
                f"{i1 - i0} sync edges, residual {rms * 1e3:.2f} ms rms\n")
        w = csv.writer(f)
        w.writerow(header + ["sas_deg", "sas_dps", "sas_zeroed_deg", "nep_rpm"])
        for row, sc, nc in zip(data, sas_cols, nep_col):
            w.writerow([f"{x:.6g}" if not np.isnan(x) else "" for x in row]
                       + ["" if np.isnan(x) else f"{x:.1f}" for x in sc]
                       + ["" if np.isnan(nc[0]) else f"{nc[0]:.0f}"])
    frames = read_can(feather_log)
    if frames:
        with open(out_csv.with_name("can.csv"), "w", newline="", encoding="utf-8") as f:
            f.write("# every CAN frame the Feather saw; time_s is on the scope time axis\n")
            w = csv.writer(f)
            w.writerow(["time_s", "feather_us", "id_hex", "ext", "rtr", "len", "data_hex"])
            for ft, us, cid, ext, rtr, n, data in frames:
                w.writerow([f"{a * ft + b:.6f}", us, f"{cid:X}", ext, rtr, n, data])
    good = score <= 0.02
    msg = (f"merged {i1 - i0} sync edges, residual {rms * 1e3:.2f} ms rms"
           + ("" if good else f" (poor match, interval error {score * 1e3:.1f} ms)"))
    if frames:
        msg += f", {len(frames)} CAN frames"
    return MergeResult(good, msg, i1 - i0, rms * 1e3, score * 1e3, len(frames))
