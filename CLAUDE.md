# Scope1008: handoff

A desktop app that replaces Hantek's software for the **Hantek 1008B** (8-channel, 12-bit, 2.4 MSa/s
USB automotive scope). The main use case is automotive diagnostics.

## Status

The app works fully in demo mode. **It has never run against the real scope.** That is the next
step. Everything below "Unverified" is where to look first when hardware
testing starts.

## Stack and layout

Python 3.10+, PySide6, pyqtgraph, numpy, pyusb and libusb-package (which bundles the libusb DLL on
Windows). The target machine is Windows. Run it with `python -m scope1008 [--demo] [--debug] [--reset]`.

| File | Role |
|---|---|
| `scope1008/hantek1008.py` | Driver vendored from github.com/mfg92/hantek1008py (Apache-2.0). Its USB protocol came from reverse engineering a 1008C. Patches: `connect(backend, product_id)`; strict byte-compare asserts on calibration responses replaced with `_soft_check` (they matched only the original author's unit); the dummy `overrides` decorator fixed so import works without that package. |
| `scope1008/backend.py` | `DeviceBackend` (real USB) and `DemoBackend` (synthetic automotive signals). Shared interface: `open/close/configure(AcqConfig)/acquire_burst()/roll_start/roll_read/roll_stop/recalibrate`. Both return input-referred volts in a `Frame`. Constants: timebase lists, `HW_SCALES`, `SAMPLES_PER_DIV`, roll rate selection. |
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

## Unverified on real hardware (priority order)

1. **Connection on Windows** after swapping the driver to WinUSB with Zadig (USB ID 0783:5725).
   The 1008B might report a different product id; `SCOPE1008_PID` overrides it.
2. **`init()` on this unit.** The original driver still has hard asserts elsewhere, for example
   in `__send_a55a_command` and on response lengths. Run with `--debug` to log USB traffic.
3. **Burst-mode timing.** `SAMPLES_PER_DIV = 25` comes from the driver's comments. The number of
   samples per channel per burst, and whether it changes with the active channel count, are
   unknown. Check against a known frequency.
4. **Burst-mode behaviour with no trigger.** `__send_a55a_command` raises after 20 polls, and
   `acquire_burst` treats that as "waiting" and returns None. Check whether the device
   auto-triggers.
5. **Changing active channels and scales mid-session** by replaying the tail of `_init3`, and
   moving between roll and burst modes.
6. **Roll mode** is the best-proven path in the original project. `rate_factor` scales the
   sample rate up when fewer channels are active.

## Possible next work

- Expose the 8 digital outputs (waveform generator) through `set_generator_*` in the driver. The
  driver marks these as untested.
- Package as a Windows .exe (PyInstaller); libusb-package already bundles the DLL.
- Automotive presets (ignition, injector, crank/cam correlation) and protocol decode (UART/CAN).
- Persistence display mode and per-channel math (A−B and so on).

## Verify after changes

```
python -m pytest -q tests/test_dsp.py
QT_QPA_PLATFORM=offscreen python tests/gui_smoke.py burst
QT_QPA_PLATFORM=offscreen python tests/gui_smoke.py roll
```
