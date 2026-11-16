# Scope1008: handoff

A desktop app that replaces Hantek's software for the **Hantek 1008B** (8-channel, 12-bit, 2.4 MSa/s
USB automotive scope). The main use case is automotive diagnostics.

## Status

The app runs against a real 1008B: about 16 fps burst capture at fast timebases, and roll mode
works. The open question is the time axis at some timebases; see "Hardware status".

## Stack and layout

Python 3.12+, PySide6-Essentials, pyqtgraph, numpy, pyusb and libusb-package (which bundles the libusb DLL on
Windows). The target machine is Windows. Run it with `python -m scope1008 [--demo] [--debug] [--reset]`.

| File | Role |
|---|---|
| `scope1008/hantek1008.py` | Driver vendored from github.com/mfg92/hantek1008py (Apache-2.0). Its USB protocol came from reverse engineering a 1008C. Patches: `connect(backend, product_id)`; strict byte-compare asserts on calibration responses replaced with `_soft_check` (they matched only the original author's unit); the dummy `overrides` decorator fixed so import works without that package. |
| `scope1008/backend.py` | `DeviceBackend` (real USB) and `DemoBackend` (synthetic automotive signals). Shared interface: `open/close/configure(AcqConfig)/acquire_burst()/roll_start/roll_read/roll_stop/recalibrate`. Both return input-referred volts in a `Frame`. Constants: timebase lists, `HW_SCALES`, `RECORD_DIVS`, `BURST_SAMPLES_TOTAL`, roll rate selection. |
| `scope1008/worker.py` | `AcqWorker(QThread)` owns the backend. It applies config changes between captures, emits `frame` and `status`, and gives up after 5 consecutive errors. |
| `scope1008/dsp.py` | Pure numpy: `edge_crossings` (hysteresis plus sub-sample interpolation), `choose_trigger`, `measure` (noise-aware frequency, duty and rise time), `fft_dbv`, and the `si`/`si_short` formatters. |
| `scope1008/app.py` | `MainWindow`, `ChannelCard` and the theme. Holds all display logic: probe/AC scaling, software trigger, roll ring buffer, cursors, measurements table, FFT, ref traces, CSV/PNG export, roll recording to CSV, and QSettings persistence. |
| `tests/test_dsp.py` | pytest: DSP checks, plus the device config path tested against a mocked USB link. |
| `tests/gui_smoke.py` | Headless GUI run: `QT_QPA_PLATFORM=offscreen python tests/gui_smoke.py burst|roll|nodevice`. Writes screenshots to `shots/`. |

## Key design decisions

- **The trigger runs in software** on the captured record. The hardware trigger is configured too,
  but the display does not depend on it.
- **Hardware range is chosen automatically** per channel. Auto range picks the most sensitive of
  ±20 V, ±2.5 V and ±0.4 V (`0.01 V * scale` per count, ±2047 counts) that covers ±4 div plus the
  offset at the current V/div and probe setting.
- **`DeviceBackend.configure` only resends settings that changed.** Changing the vertical scale
  costs about 0.2 s on the device, and the GUI also debounces config changes by 120 ms.
- **Name-mangled driver privates are accessed on purpose** through `_Hantek1008Raw__<name>`, to
  avoid rewriting the driver.
- **Without a device, the app falls back to demo mode** and shows a banner with Retry and Use demo.
- **Volts:** `(raw - zero_offset[scale][ch]) * 0.01 * scale`. Zero offsets are measured at
  `init()` on all three ranges, so the inputs must be open at startup.

## Hardware status

Confirmed on a 1008B (WinUSB via Zadig, PID 5725): connect, `init()` (3.4 s), recalibration,
burst capture and roll mode (measured rate within 1% of `rate_factor`).

Findings that changed the code:
- The driver's blanket 2 ms sleep before every USB command made a burst frame take 400 ms.
  `fast_io` (on after init, off during calibration, `SCOPE1008_SAFE_IO=1` disables) drops it:
  66 ms per frame, same data. Pipelining several a6 reads makes the device time out.
- The device free-runs: frames arrive even when the trigger level is never crossed.
- A burst record holds 4000 samples split across active channels and spans 10 divisions.
  `dt = 10 * time/div / samples per channel`. Slow timebases need the full 10 div to fill, so
  `acquire_burst` arms once and polls across calls in 100 ms slices (the driver gave up after
  20 polls and restarted, so 100 ms/div and slower never produced a frame).

- Timebase codes (command a3), probed 0-31 with `tools/probe_timebase_ids.py`: ids 18, 19, 22
  and 23 (1, 2, 20, 50 ms/div) are ignored, the device keeps the previous rate. Ids 26+ never
  report ready. `burst_hw_timebase()` maps each displayed timebase to a working code and its
  measured record span (`_MEASURED_SPAN_S`, `_SUBSTITUTE` in backend.py); the GUI shows the
  slice of the longer record. Spans at 5 and 10 ms/div are ~7 % longer than 10 div.
- Verified end to end (`tools/verify_timebase.py`, 22.5 Hz square wave): 1 to 200 ms/div within
  0.3 % with 1 and 2 channels. The record span does not depend on the channel count.
- Slow captures report progress (`capture_progress()`, worker `progress` signal); the GUI shows
  "FILL nn%".

Still unverified (priority order):
1. **500 us/div and faster** (ids 9-17): records are fresh and arrive at ~20 fps, but the span is
   still the nominal 10 div. The ADC limit (2.4 MSa/s) suggests 1-channel spans below about
   200 us/div cannot be 10 div. Needs a reference of 1 kHz or more; run
   `tools/verify_timebase.py --ch N --half-ms <half period>` and add measured spans.
2. **Changing active channels and scales mid-session** by replaying the tail of `_init3`.

Hardware scripts in `tools/` (close the app first, only one program can hold the USB device):
`verify_timebase.py`, `probe_timebase_ids.py`, `check_stale.py`, `bench_device.py`,
`check_device.py`, `check_capture_time.py`, `check_burst_rates.py`, `gui_device_fps.py [us/div]`.

## Possible next work

- Expose the 8 digital outputs (waveform generator) through `set_generator_*` in the driver. The
  driver marks these as untested.
- Package as a Windows .exe (PyInstaller); libusb-package already bundles the DLL.
- Automotive presets (ignition, injector, crank/cam correlation) and protocol decode (UART/CAN).
- Persistence display mode and per-channel math (A−B and so on).

## Dependencies and safety rules

- Dependencies are declared as ranges in `pyproject.toml` and pinned with hashes in
  `requirements.lock` / `requirements-dev.lock` (regenerate with pip-compile, see SECURITY.md).
  `tools/bootstrap.py` installs them into `.venv`; the launchers call it.
- Every `AcqConfig` goes through `validate()` before reaching USB. Add a check there for any new
  device setting.
- Saved settings are untrusted input: parse them through `channel_from_dict`, `_num`, `_idx`.
- Text from devices, the OS or the user shown in a rich-text label goes through `html.escape`
  (`_set_status`). CSV header cells go through `csv_cell`.
- The roll generator in the driver yields `{}` as a heartbeat when no data arrives for 0.25 s,
  so the worker can always stop.

## Verify after changes

```
python -m pytest -q
python -m ruff check .
QT_QPA_PLATFORM=offscreen python tests/gui_smoke.py burst
QT_QPA_PLATFORM=offscreen python tests/gui_smoke.py roll
```
