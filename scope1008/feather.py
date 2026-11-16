"""USB serial link to the eps_bench_nep Feather (Adafruit Feather M4 CAN).

Reads lines in a thread, timestamps them with the PC clock, optionally writes them to a
log in the same format as throttleECU's feather_log.py ("<unix time>\\t<line>"), and keeps
the latest values for a live readout. Commands (r900, r0, z, ?) are sent with send().

Line format from the firmware:
    N,us,rpm                     NEP command
    A,us,deg,dps,zeroed_deg      steering angle sensor (CAN 0x25)
    C,us,id,ext,rtr,len,hex      every CAN frame on the bus
    S,us,level,n                 D13 sync edge
    # ...                        comments / status
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)

BAUD = 115200
ADAFRUIT_VID = 0x239A
POLL_S = 0.02  # serial read batching interval
MAX_LINE = 512  # longer lines are cut; the firmware never sends more than ~120 chars


def list_ports() -> List[tuple[str, str]]:
    """(device, description) for every serial port, Feather-looking ones first."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    out = []
    for p in lp.comports():
        desc = p.description or ""
        if p.vid == ADAFRUIT_VID:
            desc = "Feather " + desc
        out.append((p.device, desc, p.vid == ADAFRUIT_VID or "bluetooth" not in desc.lower()))
    out.sort(key=lambda x: (not x[1].startswith("Feather"), not x[2], x[0]))
    return [(d, desc) for d, desc, _ in out]


@dataclass
class FeatherState:
    rpm: Optional[float] = None
    sas_deg: Optional[float] = None
    sas_zeroed: Optional[float] = None
    sas_dps: Optional[float] = None
    sync_count: int = 0
    lines: int = 0
    last_comment: str = ""
    last_rx: float = 0.0
    counts: dict = field(default_factory=dict)
    can_ids: dict = field(default_factory=dict)

    def update(self, line: str) -> None:
        self.lines += 1
        self.last_rx = time.time()
        if line.startswith("#"):
            self.last_comment = line[1:].strip()[:120]
            return
        p = line.split(",")
        kind = p[0]
        self.counts[kind] = self.counts.get(kind, 0) + 1
        try:
            if kind == "N" and len(p) >= 3:
                self.rpm = float(p[2])
            elif kind == "A" and len(p) >= 5:
                self.sas_deg, self.sas_dps, self.sas_zeroed = float(p[2]), float(p[3]), float(p[4])
            elif kind == "S" and len(p) >= 4:
                self.sync_count = int(p[3])
            elif kind == "C" and len(p) >= 3:
                self.can_ids[p[2]] = self.can_ids.get(p[2], 0) + 1
        except ValueError:
            pass


class FeatherLink(QThread):
    line = Signal(float, str)        # host unix time, line text
    status = Signal(str, str)        # "ok" | "error" | "closed", message

    def __init__(self, port: str, parent=None):
        super().__init__(parent)
        self.port = port
        self.state = FeatherState()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._ser = None
        self._log = None
        self._log_lines = 0

    # GUI thread --------------------------------------------------------------------
    def send(self, text: str) -> None:
        cmd = "".join(ch for ch in text if ch.isprintable())[:64].strip()
        if not cmd or self._ser is None:
            return
        try:
            self._ser.write((cmd + "\n").encode("ascii", "replace"))
        except Exception as e:  # port vanished
            self.status.emit("error", f"send failed: {e}")

    def start_log(self, path: Path) -> None:
        f = open(path, "w", encoding="utf-8", newline="\n")  # closed in stop_log
        f.write(f"# feather_log (Scope1008) {time.strftime('%Y-%m-%d %H:%M:%S')} port={self.port}\n")
        with self._lock:
            self._log, self._log_lines = f, 0

    def stop_log(self) -> int:
        with self._lock:
            f, self._log = self._log, None
            n = self._log_lines
        if f:
            f.close()
        return n

    def stop(self, timeout_ms: int = 2000) -> None:
        self._stop.set()
        self.wait(timeout_ms)

    # thread ------------------------------------------------------------------------
    def run(self) -> None:
        try:
            import serial
            self._ser = serial.Serial(self.port, BAUD, timeout=0.1)
        except Exception as e:
            self.status.emit("error", f"cannot open {self.port}: {e}")
            return
        self.status.emit("ok", f"{self.port} connected")
        buf = b""
        try:
            while not self._stop.is_set():
                # Batch reads every ~20 ms. Reading byte by byte at ~180 lines/s kept this
                # thread holding the GIL so often that the scope's USB reader fell behind and
                # its roll FIFO overflowed (gaps in the recording). Host time is only a
                # reference; alignment uses the Feather's own micros() via the D13 sync.
                n = self._ser.in_waiting
                if not n:
                    time.sleep(POLL_S)
                    continue
                chunk = self._ser.read(n)
                t = time.time()
                buf += chunk
                *lines, buf = buf.split(b"\n")
                if len(buf) > MAX_LINE:
                    buf = b""
                for raw in lines:
                    text = raw[:MAX_LINE].decode("ascii", "replace").strip()
                    if not text:
                        continue
                    self.state.update(text)
                    with self._lock:
                        if self._log:
                            self._log.write(f"{t:.6f}\t{text}\n")
                            self._log_lines += 1
                            if self._log_lines % 200 == 0:
                                self._log.flush()
                    if text.startswith("#"):  # data lines only update self.state (polled by the GUI)
                        self.line.emit(t, text)
        except Exception as e:
            log.exception("feather link")
            self.status.emit("error", f"{self.port} lost: {e}")
        finally:
            try:
                self._ser.close()
            except Exception as e:
                log.debug("serial close: %s", e)
            self._ser = None
            n = self.stop_log()
            if n:
                log.info("feather log closed with %d lines", n)
        if self._stop.is_set():
            self.status.emit("closed", f"{self.port} disconnected")
