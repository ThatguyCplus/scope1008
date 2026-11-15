"""Acquisition backends: the real Hantek 1008 over USB, and a demo signal generator.

Both return input-referred volts (before probe attenuation). The GUI applies probe
factor, coupling, scaling and triggering.
"""
from __future__ import annotations

import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

log = logging.getLogger(__name__)

N_CH = 8
# Hardware pre-scale factors. 1 count = 10 mV * factor, ADC is 12 bit centred on ~2048.
HW_SCALES = [1.0, 0.125, 0.02]
COUNTS_HALF = 2047
# Measured on a 1008B: a burst record always spans 10 divisions (the 'ready' flag rises
# exactly 10 * time/div after arming at slow timebases) and holds 4000 samples split
# across the active channels. dt therefore = 10 div * time/div / samples per channel.
RECORD_DIVS = 10
BURST_SAMPLES_TOTAL = 4000
BURST_POLL_SLICE_S = 0.1  # max time acquire_burst blocks, so the worker stays responsive

# Burst (triggered) timebases in ns/div. The ADC tops out at 2.4 MSa/s, so anything
# faster than ~10 us/div is just interpolation; we start at 10 us.
BURST_NS_PER_DIV = [({0: 1, 1: 2, 2: 5}[i % 3] * 10 ** (i // 3)) for i in range(26)]
BURST_NS_PER_DIV = [v for v in BURST_NS_PER_DIV if 10_000 <= v <= 200_000_000]

# Roll mode: per-channel sample rates the device supports (Hz, before the channel-count factor)
ROLL_RATES = [440, 220, 88, 44, 22, 11, 5, 2, 1, 0.5, 0.25, 0.125, 1.0 / 16]
# Roll timebases in s/div
ROLL_S_PER_DIV = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30, 60]
ROLL_MAX_SAMPLES = 30_000

# With fewer active channels the device samples faster than nominal (from the driver)
_RATE_FACTOR = [4.56, 3.03, 2.27, 1.82, 1.51, 1.3, 1.14, 1.00]


def rate_factor(n_active: int) -> float:
    return _RATE_FACTOR[max(1, min(8, n_active)) - 1]


def hw_full_scale(scale: float) -> float:
    return COUNTS_HALF * 0.01 * scale


def pick_roll_rate(s_per_div: float, n_active: int) -> float:
    """Highest supported rate whose window still fits in the roll buffer."""
    for r in ROLL_RATES:
        if r * rate_factor(n_active) * 10 * s_per_div <= ROLL_MAX_SAMPLES:
            return r
    return ROLL_RATES[-1]


@dataclass
class AcqConfig:
    active: List[int] = field(default_factory=lambda: [0, 1])
    hw_scales: List[float] = field(default_factory=lambda: [1.0] * N_CH)
    ns_per_div: int = 500_000
    trig_ch: int = 0
    trig_slope: str = "rising"
    trig_level_in: float = 0.0  # input-referred volts
    roll: bool = False
    roll_rate: float = 440

    def copy(self) -> "AcqConfig":
        return AcqConfig(list(self.active), list(self.hw_scales), self.ns_per_div, self.trig_ch,
                         self.trig_slope, self.trig_level_in, self.roll, self.roll_rate)

    def validate(self) -> None:
        """Reject anything the device does not support before it reaches the USB link.

        The vendored driver checks its inputs with `assert`, which `python -O` strips, so
        this is the real gate between the GUI and the hardware.
        """
        act = list(self.active)
        if not all(isinstance(c, int) and 0 <= c < N_CH for c in act) or len(set(act)) != len(act):
            raise ConfigError(f"invalid active channels {act!r}")
        if len(self.hw_scales) != N_CH or any(s not in HW_SCALES for s in self.hw_scales):
            raise ConfigError(f"invalid hardware scales {self.hw_scales!r}")
        if self.ns_per_div not in BURST_NS_PER_DIV:
            raise ConfigError(f"unsupported timebase {self.ns_per_div!r} ns/div")
        if not (isinstance(self.trig_ch, int) and 0 <= self.trig_ch < N_CH):
            raise ConfigError(f"invalid trigger channel {self.trig_ch!r}")
        if self.trig_slope not in ("rising", "falling"):
            raise ConfigError(f"invalid trigger slope {self.trig_slope!r}")
        if not math.isfinite(self.trig_level_in):
            raise ConfigError("trigger level must be a finite number")
        if self.roll_rate not in ROLL_RATES:
            raise ConfigError(f"unsupported roll rate {self.roll_rate!r}")


@dataclass
class Frame:
    kind: str  # "burst" or "roll"
    dt: float
    data: Dict[int, np.ndarray]
    clipped: Dict[int, bool]
    wall: float = field(default_factory=time.time)


class BackendError(RuntimeError):
    pass


class ConfigError(ValueError):
    """A configuration the device cannot accept. Not a hardware fault."""


def product_id_from_env(default: int = 0x5725) -> int:
    """SCOPE1008_PID as a 16-bit hex USB product id, or the default."""
    raw = os.environ.get("SCOPE1008_PID", "").strip()
    if not raw:
        return default
    try:
        pid = int(raw, 16)
    except ValueError:
        raise BackendError(f"SCOPE1008_PID={raw!r} is not a hex USB product id (e.g. 5725)") from None
    if not 0 <= pid <= 0xFFFF:
        raise BackendError(f"SCOPE1008_PID={raw!r} is out of range (0000-ffff)")
    return pid


# --------------------------------------------------------------------------------------
# Real device
# --------------------------------------------------------------------------------------

def _usb_backend():
    try:
        import libusb_package  # ships libusb-1.0 DLL for Windows/macOS
        return libusb_package.get_libusb1_backend()
    except Exception as e:
        log.warning("libusb-package unavailable, falling back to the system libusb: %s", e)
        return None


class DeviceBackend:
    name = "Hantek 1008"
    VID, PID = 0x0783, 0x5725

    def __init__(self) -> None:
        self.dev = None
        self.cfg: Optional[AcqConfig] = None
        self._sent: dict = {}
        self._roll_gen = None
        self._roll_rate = ROLL_RATES[0]
        self._armed_at: Optional[float] = None  # monotonic time the current capture was started

    # -- discovery --------------------------------------------------------------------
    def _diagnose_missing(self, backend, pid: int, err: Exception) -> str:
        try:
            import usb.core
            found = list(usb.core.find(find_all=True, idVendor=self.VID, backend=backend) or [])
        except Exception as e:  # no libusb at all
            return (f"libusb not available ({e}). Run: pip install libusb-package")
        if any(d.idProduct == pid for d in found):
            # Enumerated but not openable. On Windows pyusb raises NotImplementedError
            # (a RuntimeError) when the device is bound to a non-WinUSB driver.
            if sys.platform.startswith("win"):
                return ("The Hantek 1008 is plugged in but still uses Hantek's driver, which libusb "
                        "cannot open. Switch it to WinUSB with Zadig (see README), then Retry. "
                        f"[{type(err).__name__}: {err}]")
            return (f"The Hantek 1008 is plugged in but could not be opened ({err}). On Linux this is "
                    "usually permissions: install the udev rule from the README and replug.")
        if found:
            pids = ", ".join(f"{d.idProduct:04x}" for d in found)
            return (f"Found a Hantek device with product id {pids}, expected {pid:04x}. "
                    "If it is a 1008 variant, set SCOPE1008_PID to that id and relaunch.")
        if sys.platform.startswith("win"):
            return ("No Hantek 1008 visible to libusb. On Windows the scope must use the WinUSB "
                    "driver: run Zadig, pick the Hantek device, install WinUSB (see README).")
        return ("No Hantek 1008 found. Check the cable, and on Linux install the udev rule "
                "from the README (or run once with sudo).")

    def open(self) -> None:
        from .hantek1008 import Hantek1008
        backend = _usb_backend()
        pid = product_id_from_env(self.PID)
        dev = Hantek1008(ns_per_div=500_000, vertical_scale_factor=1.0,
                         active_channels=list(range(N_CH)))
        try:
            dev.connect(backend=backend, product_id=pid)
        except RuntimeError as e:
            self._release(dev)
            raise BackendError(self._diagnose_missing(backend, pid, e)) from e
        except Exception as e:
            self._release(dev)
            raise BackendError(f"USB error while opening the scope: {e}") from e
        try:
            dev.init()  # includes zero-offset calibration for all 3 ranges
        except Exception as e:
            self._release(dev)
            raise BackendError(f"Scope found but init failed: {e!r}. Unplug/replug and retry.") from e
        dev.fast_io = os.environ.get("SCOPE1008_SAFE_IO", "") != "1"
        self.dev = dev
        self._sent = {}

    @staticmethod
    def _release(dev) -> None:
        """Free the USB interface so a retry (or another program) can claim it."""
        handle = getattr(dev, "_dev", None)
        if handle is None:
            return
        try:
            import usb.util
            usb.util.dispose_resources(handle)
        except Exception as e:
            log.debug("dispose_resources failed: %s", e)

    def close(self) -> None:
        self.roll_stop()
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception as e:
                log.warning("error while closing the scope: %s", e)
            self._release(self.dev)
        self.dev = None

    # -- private driver access (name-mangled in the vendored driver) --------------------
    def _cmd(self, name, *a):
        return getattr(self.dev, f"_Hantek1008Raw__{name}")(*a)

    def _set_attr(self, name, value):
        setattr(self.dev, f"_Hantek1008Raw__{name}", value)

    def zero(self, ch: int, scale: float) -> float:
        z = self.dev.get_zero_offsets()
        return z[scale][ch] if z else 2048.0

    def configure(self, cfg: AcqConfig) -> None:
        """Push only the settings that changed (vertical scale costs ~0.2 s)."""
        cfg.validate()
        self.roll_stop()
        self._armed_at = None
        self.cfg = cfg.copy()
        active = sorted(cfg.active) or [0]
        trig_scale = cfg.hw_scales[cfg.trig_ch]
        level = self.zero(cfg.trig_ch, trig_scale) + cfg.trig_level_in / (0.01 * trig_scale)
        level_raw = max(0, min(4095, int(round(level))))
        want = {
            "active": tuple(active),
            "scales": tuple(cfg.hw_scales),
            "tdiv": cfg.ns_per_div,
            "trig": (cfg.trig_ch, cfg.trig_slope),
            "level": level_raw,
        }
        self._set_attr("active_channels", list(active))
        self._set_attr("vertical_scale_factors", list(cfg.hw_scales))
        self._set_attr("ns_per_div", cfg.ns_per_div)
        self._cmd("send_ping")
        if self._sent.get("active") != want["active"]:
            self._cmd("send_set_active_channels", list(active))
        if self._sent.get("scales") != want["scales"]:
            self._cmd("send_set_vertical_scale", list(cfg.hw_scales))
        # timebase is always resent: roll mode reuses the same register
        self._cmd("send_set_time_div", cfg.ns_per_div)
        if self._sent.get("trig") != want["trig"]:
            self._cmd("send_set_trigger", cfg.trig_ch, cfg.trig_slope)
        if self._sent.get("level") != want["level"]:
            self._cmd("send_set_trigger_level", level_raw)
        self._sent = want

    def recalibrate(self) -> None:
        self.roll_stop()
        fast, self.dev.fast_io = self.dev.fast_io, False  # calibration keeps original timing
        try:
            self.dev._init2()
        finally:
            self.dev.fast_io = fast
        self._sent = {}
        if self.cfg:
            self.configure(self.cfg)

    def _to_volts(self, raw: Dict[int, list]):
        data, clipped = {}, {}
        for ch, vals in raw.items():
            if not (isinstance(ch, int) and 0 <= ch < N_CH):
                log.warning("ignoring data for unknown channel %r", ch)
                continue
            a = np.asarray(vals, dtype=np.float64)
            s = self.cfg.hw_scales[ch]
            data[ch] = (a - self.zero(ch, s)) * 0.01 * s
            clipped[ch] = bool(a.size and (a.min() <= 2 or a.max() >= 4093))
        return data, clipped

    def _send(self, cmd_id: int, parameter=b"", response_length: int = 0, echo_expected: bool = True,
              sec_till_response_request: float = 0.0) -> bytes:
        return self.dev._Hantek1008Raw__send_cmd(
            cmd_id, parameter=parameter, response_length=response_length, echo_expected=echo_expected,
            sec_till_response_request=sec_till_response_request)

    def capture_timeout(self) -> float:
        """How long a capture may take before it is considered lost and re-armed."""
        return RECORD_DIVS * self.cfg.ns_per_div * 1e-9 * 1.5 + 1.0

    def acquire_burst(self) -> Optional[Frame]:
        """One triggered record, or None if it is still filling.

        Same command sequence as the driver's request_samples_burst_mode, split into
        arm / poll / read so a slow capture (2 s at 200 ms/div) is polled across calls
        instead of being abandoned and restarted after a fixed 20 polls.
        """
        from .hantek1008 import Hantek1008Raw
        now = time.monotonic()
        if self._armed_at is not None and now - self._armed_at > self.capture_timeout():
            log.warning("burst capture not ready after %.1f s, re-arming", now - self._armed_at)
            self._armed_at = None
        if self._armed_at is None:
            self._cmd("send_ping")
            self._send(0xe4, [0x01])
            self._send(0xe6, [0x01], response_length=10, echo_expected=False)
            self._send(0xa4, [0x01], sec_till_response_request=0.015)
            self._send(0xc0)
            self._send(0xc2)
            self._armed_at = time.monotonic()
        deadline = time.monotonic() + BURST_POLL_SLICE_S
        while True:
            state = self._send(0xa5, [0x5a], response_length=1)[0]
            if state in (2, 3):
                break
            if state not in (0, 1):
                self._armed_at = None
                raise IOError(f"unexpected capture state 0x{state:02x}")
            if time.monotonic() >= deadline:
                return None  # still filling; poll again on the next call
            time.sleep(0.005)
            self._cmd("send_ping")
        self._armed_at = None
        samples = self._cmd("send_c6_a6_command", 0x02) + self._cmd("send_c6_a6_command", 0x03)
        self._send(0xe4, [0x01])
        self._send(0xe6, [0x01], response_length=10, echo_expected=False)
        shorts = Hantek1008Raw._Hantek1008Raw__from_bytes_to_shorts(samples)
        raw = Hantek1008Raw._Hantek1008Raw__to_per_channel_lists(shorts, sorted(self.cfg.active))
        data, clipped = self._to_volts(raw)
        n = min((v.size for v in data.values()), default=0)
        if n == 0:
            return None
        dt = RECORD_DIVS * self.cfg.ns_per_div * 1e-9 / n
        return Frame("burst", dt, data, clipped)

    def roll_start(self, rate: float) -> None:
        from .hantek1008 import Hantek1008Raw
        self.roll_stop()
        self._armed_at = None
        self._roll_rate = rate
        self._roll_gen = Hantek1008Raw.request_samples_roll_mode(self.dev, sampling_rate=rate)

    def roll_read(self) -> Optional[Frame]:
        if self._roll_gen is None:
            return None
        try:
            raw = next(self._roll_gen)
        except StopIteration:
            self._roll_gen = None
            raise BackendError("roll stream ended unexpectedly") from None
        if not raw:  # driver heartbeat: no samples yet, lets the worker check for stop
            return None
        data, clipped = self._to_volts(raw)
        dt = 1.0 / (self._roll_rate * rate_factor(len(self.cfg.active)))
        return Frame("roll", dt, data, clipped)

    def roll_stop(self) -> None:
        if self._roll_gen is not None:
            try:
                self._roll_gen.close()
            except Exception as e:
                log.debug("roll generator close: %s", e)
            self._roll_gen = None


# --------------------------------------------------------------------------------------
# Demo signals: automotive-flavoured so the UI can be tried without hardware
# --------------------------------------------------------------------------------------

def _square(t, f, duty=0.5):
    return ((t * f) % 1.0) < duty


class DemoBackend:
    name = "Demo"

    LABELS = ["PWM 1 kHz", "Crank VR 60-2", "Cam Hall", "Battery 14 V",
              "O2 sensor", "TPS", "UART 9600", "Noise"]

    def __init__(self) -> None:
        self.cfg = AcqConfig()
        self.rng = np.random.default_rng(1)
        self.t0 = time.monotonic()
        self._roll_rate = None
        self._roll_t = 0.0
        self._roll_last = 0.0

    def open(self) -> None:
        time.sleep(0.2)

    def close(self) -> None:
        pass

    def configure(self, cfg: AcqConfig) -> None:
        cfg.validate()
        self.cfg = cfg.copy()

    def recalibrate(self) -> None:
        time.sleep(0.5)

    def signal(self, ch: int, t: np.ndarray) -> np.ndarray:
        r = self.rng
        n = t.size
        if ch == 0:  # 5 V PWM, 1 kHz, duty sweeps slowly, small ringing
            duty = 0.3 + 0.15 * math.sin(2 * math.pi * 0.2 * t[0])
            v = np.where(_square(t, 1000.0, duty), 5.0, 0.05)
        elif ch == 1:  # variable reluctance crank sensor, 60-2 wheel at ~850 rpm
            rps = 850 / 60.0
            tooth = (t * rps * 60.0)
            idx = np.floor(tooth) % 60
            amp = 2.5 + 0.5 * math.sin(t[0])
            v = amp * np.sin(2 * np.pi * tooth)
            v = np.where(idx >= 58, 0.08 * np.sin(2 * np.pi * tooth * 0.5), v)
        elif ch == 2:  # cam hall sensor, once per 2 revs
            v = np.where(_square(t, 850 / 120.0, 0.15), 0.1, 4.9)
        elif ch == 3:  # charging system with alternator ripple
            v = 14.2 + 0.18 * np.abs(np.sin(2 * np.pi * 360 * t)) + 0.05 * np.sin(2 * np.pi * 30 * t)
        elif ch == 4:  # narrowband O2 switching ~1 Hz
            v = 0.45 + 0.4 * np.tanh(4 * np.sin(2 * np.pi * 1.1 * t))
        elif ch == 5:  # throttle position, slow blips
            v = 0.6 + 3.6 * np.clip(np.sin(2 * np.pi * 0.15 * t), 0, None) ** 3
        elif ch == 6:  # 3.3 V UART bursts at 9600 baud
            bit = np.floor(t * 9600).astype(np.int64)
            v = np.where((np.sin(bit * 12.9898) * 43758.5453) % 1.0 > 0.5, 3.3, 0.0)
            v = np.where((bit // 200) % 2 == 0, 3.3, v)
        else:
            v = np.zeros(n)
        return v + r.normal(0, 0.012, n)

    def _sample(self, t):
        data, clipped = {}, {}
        for ch in self.cfg.active:
            v = self.signal(ch, t)
            fs = hw_full_scale(self.cfg.hw_scales[ch])
            lsb = 0.01 * self.cfg.hw_scales[ch]
            clipped[ch] = bool(np.any(np.abs(v) > fs))
            data[ch] = np.round(np.clip(v, -fs, fs) / lsb) * lsb
        return data, clipped

    def acquire_burst(self) -> Optional[Frame]:
        n_act = max(1, len(self.cfg.active))
        n = BURST_SAMPLES_TOTAL // n_act  # same record shape as the real scope
        dt = RECORD_DIVS * self.cfg.ns_per_div * 1e-9 / n
        window = n * dt
        # pace like a USB scope: ~30 fps, slower for long captures
        time.sleep(max(0.03, min(window, 2.0)))
        start = time.monotonic() - self.t0
        t = start + np.arange(n) * dt
        data, clipped = self._sample(t)
        return Frame("burst", dt, data, clipped)

    def roll_start(self, rate: float) -> None:
        self._roll_rate = rate
        self._roll_last = time.monotonic()
        self._roll_t = self._roll_last - self.t0

    def roll_read(self) -> Optional[Frame]:
        if self._roll_rate is None:
            return None
        time.sleep(0.03)
        fs = self._roll_rate * rate_factor(len(self.cfg.active))
        now = time.monotonic()
        n = int((now - self._roll_last) * fs)
        if n <= 0:
            return None
        self._roll_last += n / fs
        t = self._roll_t + np.arange(n) / fs
        self._roll_t += n / fs
        data, clipped = self._sample(t)
        return Frame("roll", 1.0 / fs, data, clipped)

    def roll_stop(self) -> None:
        self._roll_rate = None
