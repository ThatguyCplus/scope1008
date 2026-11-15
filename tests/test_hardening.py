"""Input validation and failure handling: the paths that keep bad data away from the scope."""
import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scope1008.backend import (  # noqa: E402
    AcqConfig, BackendError, ConfigError, DemoBackend, DeviceBackend, product_id_from_env,
)


# -- device config gate ----------------------------------------------------------------

def test_default_config_is_valid():
    AcqConfig().validate()


@pytest.mark.parametrize("change", [
    dict(active=[0, 8]),
    dict(active=[1, 1]),
    dict(active=[-1]),
    dict(hw_scales=[1.0] * 7),
    dict(hw_scales=[0.5] + [1.0] * 7),
    dict(ns_per_div=123),
    dict(trig_ch=8),
    dict(trig_slope="sideways"),
    dict(trig_level_in=math.nan),
    dict(trig_level_in=math.inf),
    dict(roll_rate=1000),
])
def test_bad_config_rejected(change):
    cfg = AcqConfig(**change)
    with pytest.raises(ConfigError):
        cfg.validate()


def test_device_backend_refuses_bad_config_before_usb():
    b = DeviceBackend()
    b.dev = object()  # any USB call would raise AttributeError
    with pytest.raises(ConfigError):
        b.configure(AcqConfig(trig_ch=42))


def test_demo_backend_validates_too():
    with pytest.raises(ConfigError):
        DemoBackend().configure(AcqConfig(active=[9]))


# -- environment -----------------------------------------------------------------------

def test_pid_env(monkeypatch):
    monkeypatch.delenv("SCOPE1008_PID", raising=False)
    assert product_id_from_env() == 0x5725
    monkeypatch.setenv("SCOPE1008_PID", "  57a0 ")
    assert product_id_from_env() == 0x57A0
    for bad in ("zz", "1ffff", "-1"):
        monkeypatch.setenv("SCOPE1008_PID", bad)
        with pytest.raises(BackendError):
            product_id_from_env()


# -- roll mode must not block forever --------------------------------------------------

def test_roll_heartbeat_returns_none():
    b = DeviceBackend()
    b.cfg = AcqConfig()
    b._roll_gen = iter([{}])
    assert b.roll_read() is None
    with pytest.raises(BackendError):  # generator exhausted
        b.roll_read()


def test_unknown_channel_data_ignored():
    b = DeviceBackend()
    b.cfg = AcqConfig()

    class Dev:
        def get_zero_offsets(self):
            return None
    b.dev = Dev()
    data, _ = b._to_volts({0: [2048, 2049], 99: [1, 2]})
    assert set(data) == {0}


# -- GUI-side sanitising (pure functions, no window needed) ----------------------------

def test_csv_cell_blocks_formulas():
    from scope1008.app import csv_cell
    assert csv_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    for p in "+-@":
        assert csv_cell(p + "1").startswith("'")
    assert csv_cell("CH1_crank_V") == "CH1_crank_V"


def test_clean_name():
    from scope1008.app import MAX_NAME, clean_name
    assert clean_name("  cam\x00\x1b[31m  ") == "cam[31m"
    assert len(clean_name("x" * 500)) == MAX_NAME
    assert clean_name(None) == "None"


def test_channel_from_corrupt_settings():
    from scope1008.app import PROBES, VDIVS, channel_from_dict
    c = channel_from_dict({"vdiv": "nan", "probe": 7, "coupling": "<b>", "offset": 1e9,
                           "range": "lots", "name": 5, "unexpected": 1}, 3)
    assert c.vdiv == 1.0 and c.probe in PROBES and c.coupling == "DC"
    assert c.offset == 4.0 and c.range == "Auto" and c.name == "5"
    assert channel_from_dict("garbage", 0).enabled is True
    assert channel_from_dict({"vdiv": 1e9}, 0).vdiv == VDIVS[-1]
