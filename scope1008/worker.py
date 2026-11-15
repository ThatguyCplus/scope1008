"""Acquisition thread: owns the backend, applies config changes between captures."""
from __future__ import annotations

import logging
import threading
import traceback

from PySide6.QtCore import QThread, Signal

from .backend import AcqConfig, BackendError, DemoBackend, DeviceBackend

log = logging.getLogger(__name__)


class AcqWorker(QThread):
    frame = Signal(object)
    status = Signal(str, str)        # state ("connecting"|"ok"|"error"|"demo"), message
    opened = Signal(str)             # backend name

    def __init__(self, demo: bool, cfg: AcqConfig, parent=None):
        super().__init__(parent)
        self.demo = demo
        self._lock = threading.Lock()
        self._pending: AcqConfig | None = cfg.copy()
        self._cfg = cfg.copy()
        self._running = True
        self._stop = False
        self._recal = False
        self.backend = None

    # called from GUI thread ---------------------------------------------------------
    def set_config(self, cfg: AcqConfig) -> None:
        with self._lock:
            self._pending = cfg.copy()

    def set_running(self, on: bool) -> None:
        self._running = on

    def recalibrate(self) -> None:
        self._recal = True

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)

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
        while not self._stop:
            try:
                with self._lock:
                    pending, self._pending = self._pending, None
                if pending is not None:
                    if roll_active:
                        self.backend.roll_stop()
                        roll_active = False
                    self._cfg = pending
                    self.backend.configure(pending)
                if self._recal:
                    self._recal = False
                    self.status.emit("connecting", "Recalibrating zero (leave inputs open)...")
                    self.backend.recalibrate()
                    roll_active = False
                    self.status.emit("demo" if self.demo else "ok", "Zero offsets recalibrated")
                if not self._running:
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
                log.error("acquisition error: %s", traceback.format_exc())
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
        except Exception:
            pass
