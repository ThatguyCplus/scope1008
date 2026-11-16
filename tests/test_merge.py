"""Session merge: the Feather's clock is recovered from D13 sync edges recorded on CH4."""
import csv
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scope1008.feather import FeatherState  # noqa: E402
from scope1008.merge import merge  # noqa: E402


def _make_session(tmp_path, offset=12.345, drift=1.0005, dt=1 / 1983):
    rng = np.random.default_rng(3)
    # Feather timeline: sync edges at pseudo-random 200-1000 ms, angle at 100 Hz, rpm step
    t_edges = np.cumsum(rng.uniform(0.2, 1.0, 20)) + 1.0
    levels = np.arange(1, 21) % 2
    lines = ["# eps_bench_nep: test"]
    events = [(t, f"S,{int(t * 1e6)},{lv},{i + 1}") for i, (t, lv) in enumerate(zip(t_edges, levels))]
    events += [(t, f"A,{int(t * 1e6)},{10 * np.sin(t):.1f},0,{10 * np.sin(t):.1f}")
               for t in np.arange(0.5, t_edges[-1], 0.01)]
    events += [(0.6, "N,600000,0"), (5.0, "N,5000000,900")]
    events += [(t, f"C,{int(t * 1e6)},4C6,0,0,8,0011223344556677") for t in np.arange(1.0, 4.0, 0.5)]
    for t, msg in sorted(events):
        lines.append(f"{1.7e9 + t:.6f}\t{msg}")
    (tmp_path / "feather.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Scope timeline = drift * feather + offset; CH4 follows D13 (0 / 3.3 V)
    t_scope = np.arange(0, (t_edges[-1] + 1) * drift + 0.5, dt)
    t_f = (t_scope - offset + offset) / drift  # scope t -> feather t (offset applied below)
    ch4 = np.where(np.searchsorted(t_edges, t_f, side="right") % 2 == 1, 3.3, 0.0)
    with open(tmp_path / "scope.csv", "w", newline="", encoding="utf-8") as f:
        f.write("# Scope1008 roll recording\n")
        w = csv.writer(f)
        w.writerow(["time_s", "CH3_V", "CH4_V"])
        for t, v in zip(t_scope + offset, ch4):
            w.writerow([f"{t:.6f}", "12.0", f"{v:.2f}"])
    return t_edges


def test_merge_recovers_alignment(tmp_path):
    _make_session(tmp_path)
    r = merge(tmp_path / "scope.csv", tmp_path / "feather.log", tmp_path / "merged.csv")
    assert r.ok, r.message
    assert r.edges >= 15 and r.residual_ms < 1.0
    rows = [row for row in csv.reader(open(tmp_path / "merged.csv", encoding="utf-8"))
            if row and not row[0].startswith("#")]
    head, body = rows[0], rows[1:]
    assert head[-4:] == ["sas_deg", "sas_dps", "sas_zeroed_deg", "nep_rpm"]
    assert any(r[-1] == "900" for r in body) and any(r[-1] == "0" for r in body)
    can = [row for row in csv.reader(open(tmp_path / "can.csv", encoding="utf-8"))
           if row and not row[0].startswith("#")]
    assert can[0][:3] == ["time_s", "feather_us", "id_hex"] and len(can) == 7 and r.can_frames == 6
    assert can[1][2] == "4C6" and can[1][6] == "0011223344556677"


def test_merge_reports_missing_sync_channel(tmp_path):
    _make_session(tmp_path)
    r = merge(tmp_path / "scope.csv", tmp_path / "feather.log", tmp_path / "m.csv", sync="CH7")
    assert not r.ok and "CH7" in r.message


def test_feather_state_parses_lines():
    st = FeatherState()
    for line in ["# hello", "N,1,900", "A,2,12.5,3,2.5", "S,3,1,7", "A,bad"]:
        st.update(line)
    assert st.rpm == 900 and st.sas_zeroed == 2.5 and st.sync_count == 7
    assert st.last_comment == "hello"
