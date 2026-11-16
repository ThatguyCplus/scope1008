import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import numpy as np
from scope1008 import dsp


def test_pwm():
    dt = 1e-6
    t = np.arange(5000) * dt
    v = np.where((t * 1000) % 1 < 0.3, 5.0, 0.0) + np.random.default_rng(0).normal(0, 0.02, t.size)
    m = dsp.measure(v, dt)
    assert abs(m["freq"] - 1000) < 1 and abs(m["duty"] - 30) < 1


def test_flat_noise_has_no_freq():
    v = np.round((4.9 + np.random.default_rng(0).normal(0, 0.012, 1024)) / 0.01) * 0.01
    assert np.isnan(dsp.measure(v, 2e-5)["freq"])


def test_edges_interpolated():
    dt = 1e-6
    s = 3 * np.sin(2 * np.pi * 12345 * np.arange(5000) * dt)
    p = dsp.edge_crossings(s, 0, True, 0.1)
    assert abs(np.diff(p).mean() * dt - 1 / 12345) < 1e-8


def test_device_config_sends_only_diffs():
    from scope1008.hantek1008 import Hantek1008
    from scope1008.backend import DeviceBackend, AcqConfig
    sent = []

    def fake(self, msg, n, **k):
        sent.append(msg.hex())
        return bytes([msg[0]]) + bytes(n - 1) if n else b""
    Hantek1008._Hantek1008Raw__write_and_receive = fake
    d = Hantek1008()
    d._zero_offsets = {1.0: [2048.0] * 8, 0.125: [2050.0] * 8, 0.02: [2047.0] * 8}
    b = DeviceBackend(); b.dev = d
    cfg = AcqConfig(active=[0, 2, 5], hw_scales=[1.0, 0.125] + [1.0] * 6, ns_per_div=200000,
                    trig_ch=1, trig_slope="falling", trig_level_in=0.5)
    b.configure(cfg)
    assert sent == ["f3", "a003", "aa0100010000010000", "a20302030303030303", "a310", "c10101", "ab0992"]
    sent.clear(); cfg.ns_per_div = 500000; b.configure(cfg)
    assert sent == ["f3", "a311"]


def test_single_sample_glitch_after_edge_ignored():
    # Pattern seen on a real 1008B: one sample right after a rising edge drops back low.
    dt = 26.79e-6
    t = np.arange(4000) * dt
    v = np.where((t * 22.2) % 1 < 0.5, 12.0, 0.0)
    rises = np.flatnonzero(np.diff(v) > 6) + 1
    v[rises + 1] = 0.0
    m = dsp.measure(v, dt)
    assert abs(m["freq"] - 22.2) < 0.3 and abs(m["duty"] - 50) < 2
