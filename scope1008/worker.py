"""Acquisition thread: owns the backend, applies config changes between captures."""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QThread, Signal

from .backend import AcqConfig, BackendError, ConfigError, DemoBackend, DeviceBackend

log = logging.getLogger(__name__)


class AcqWorker(QThread):
    frame = Signal(object)
    status = Signal(str, str)        # state ("connecting"|"ok"|"warn"|"error"|"demo"), message
    opened = Signal(str)             # backend name

    def __init__(self, demo: bool, cfg: AcqConfig, parent=None):
        super().__init__(parent)
        self.demo = demo
        self._lock = threading.Lock()
        self._pending: AcqConfig | None = cfg.copy()
        self._cfg = cfg.copy()
        self._running = threading.Event()
        self._running.set()
        self._stop = threading.Event()
        self._recal = threading.Event()
        self.backend = None

    # called from GUI thread ---------------------------------------------------------
    def set_config(self, cfg: AcqConfig) -> None:
        with self._lock:
            self._pending = cfg.copy()

    def set_running(self, on: bool) -> None:
        if on:
            self._running.set()
        else:
            self._running.clear()

    def recalibrate(self) -> None:
        self._recal.set()

    def stop(self, timeout_ms: int = 3000) -> bool:
        """Ask the thread to finish and wait. False if it is still stuck in a USB call."""
        self._stop.set()
        done = self.wait(timeout_ms)
        if not done:
            log.warning("acquisition thread did not stop within %d ms", timeout_ms)
        return done

    # thread ------------------------------------------------------------------------
    def run(self) -> None:
        self.backend = DemoBackend() if self.demo else DeviceBackend()
        self.status.emit("connecting", "Connecting and calibrating zero offsets (leave inputs open)...")
        try:
            self.backend.open()
        except BackendError as e:
            self.status.emit("error", str(e))
            return
        except Exception as e:
            self.status.emit("error", f"{type(e).__name__}: {e}")
            return
        self.opened.emit(self.backend.name)
        self.status.emit("demo" if self.demo else "ok",
                         "Demo signals (no scope)" if self.demo else "Hantek 1008 connected")
        roll_active = False
        errors = 0
        while not self._stop.is_set():
            try:
                with self._lock:
                    pending, self._pending = self._pending, None
                if pending is not None:
                    if roll_active:
                        self.backend.roll_stop()
                        roll_active = False
                    try:
                        self.backend.configure(pending)
                    except ConfigError as e:
                        # A bad setting is not a lost scope: report it, keep the last good one.
                        log.error("rejected config: %s", e)
                        self.status.emit("warn", f"Invalid setting rejected: {e}")
                        pending = None
                    if pending is not None:
                        self._cfg = pending
                if self._recal.is_set():
                    self._recal.clear()
                    self.status.emit("connecting", "Recalibrating zero (leave inputs open)...")
                    self.backend.recalibrate()
                    roll_active = False
                    self.status.emit("demo" if self.demo else "ok", "Zero offsets recalibrated")
                if not self._running.is_set():
                    if roll_active:
                        self.backend.roll_stop()
                        roll_active = False
                    self.msleep(20)
                    continue
                if self._cfg.roll:
                    if not roll_active:
                        self.backend.roll_start(self._cfg.roll_rate)
                        roll_active = True
                    fr = self.backend.roll_read()
                else:
                    fr = self.backend.acquire_burst()
                if fr is not None:
                    self.frame.emit(fr)
                errors = 0
            except Exception as e:
                errors += 1
                log.exception("acquisition error")
                roll_active = False
                if errors >= 5:
                    self.status.emit("error", f"Lost the scope: {type(e).__name__}: {e}")
                    break
                self.msleep(100)
                with self._lock:
                    if self._pending is None:
                        self._pending = self._cfg.copy()
        try:
            self.backend.close()
        except Exception as e:
            log.warning("backend close failed: %s", e)
