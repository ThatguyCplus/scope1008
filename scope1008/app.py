"""Scope1008: a faster, saner front end for the Hantek 1008A/B/C."""
from __future__ import annotations

import csv
import html
import json
import logging
import math
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, QSettings, Signal
from PySide6.QtGui import QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QPushButton,
    QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from . import dsp
from .backend import (
    AcqConfig, BURST_NS_PER_DIV, HW_SCALES, N_CH, ROLL_S_PER_DIV, DemoBackend, Frame,
    burst_hw_timebase, hw_full_scale, pick_roll_rate,
)
from .worker import AcqWorker

COLORS = ["#f5d90a", "#22d3ee", "#f472b6", "#4ade80", "#fb923c", "#818cf8", "#f87171", "#e5e7eb"]
VDIVS = [m * 10 ** e for e in range(-3, 3) for m in (1, 2, 5)]
PROBES = [1, 10, 20, 100]
BG = "#0b0f14"
PANEL = "#121821"
BORDER = "#1f2a37"
TEXT = "#d1d5db"
MUTED = "#7b8794"
X_DIVS, Y_DIVS = 10, 8
MAX_NAME = 24

log = logging.getLogger(__name__)


@dataclass
class Channel:
    enabled: bool = False
    name: str = ""
    vdiv: float = 1.0
    offset: float = 0.0      # divisions
    probe: int = 1
    coupling: str = "DC"
    range: str = "Auto"      # Auto or one of the HW labels


RANGE_LABELS = {1.0: "±20 V", 0.125: "±2.5 V", 0.02: "±0.4 V"}
COUPLINGS = ("DC", "AC")
MODES = ("auto", "normal", "roll")


def clean_name(text: object) -> str:
    """Channel label: printable characters only, bounded length."""
    t = "".join(ch for ch in str(text) if ch.isprintable())
    return t.strip()[:MAX_NAME]


def csv_cell(text: str) -> str:
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of a cell)."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _num(v, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, f)) if math.isfinite(f) else default


def _idx(v, default: int, n: int) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < n else default


def channel_from_dict(d: object, i: int) -> Channel:
    """Build a Channel from saved settings, falling back to defaults for anything odd."""
    base = Channel(enabled=i < 2)
    if not isinstance(d, dict):
        return base
    vdiv = _num(d.get("vdiv"), base.vdiv, VDIVS[0], VDIVS[-1])
    probe = d.get("probe")
    return Channel(
        enabled=bool(d.get("enabled", base.enabled)),
        name=clean_name(d.get("name", "")),
        vdiv=vdiv,
        offset=_num(d.get("offset"), 0.0, -Y_DIVS / 2, Y_DIVS / 2),
        probe=probe if probe in PROBES else 1,
        coupling=d.get("coupling") if d.get("coupling") in COUPLINGS else "DC",
        range=d.get("range") if d.get("range") in ("Auto", *RANGE_LABELS.values()) else "Auto",
    )


def fmt_tdiv(seconds: float) -> str:
    return dsp.si_short(seconds, "s") + "/div"


# ------------------------------------------------------------------------------------
# Widgets
# ------------------------------------------------------------------------------------

class ScopeViewBox(pg.ViewBox):
    wheel = Signal(int)

    def wheelEvent(self, ev, axis=None):
        self.wheel.emit(1 if ev.delta() > 0 else -1)
        ev.accept()


class ChannelCard(QFrame):
    changed = Signal()

    def __init__(self, idx: int, ch: Channel):
        super().__init__()
        self.idx, self.ch = idx, ch
        color = COLORS[idx]
        self.setObjectName("card")
        self.setStyleSheet(f"#card {{ background:{PANEL}; border:1px solid {BORDER};"
                           f" border-left:3px solid {color}; border-radius:6px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)

        top = QHBoxLayout()
        self.btn = QPushButton(f"CH{idx + 1}")
        self.btn.setCheckable(True)
        self.btn.setFixedWidth(52)
        self.btn.setToolTip(f"Toggle channel {idx + 1} (key {idx + 1})")
        self.btn.setStyleSheet(
            f"QPushButton {{ background:#1a222d; color:{MUTED}; border:1px solid {BORDER};"
            f" border-radius:4px; font-weight:600; padding:3px; }}"
            f"QPushButton:checked {{ background:{color}; color:#0b0f14; border-color:{color}; }}")
        self.name = QLineEdit(ch.name)
        self.name.setMaxLength(MAX_NAME)
        self.name.setPlaceholderText("label")
        self.scale_lbl = QLabel()
        self.scale_lbl.setStyleSheet(f"color:{color}; font-family:monospace; font-weight:600;")
        self.clip = QLabel("CLIP")
        self.clip.setStyleSheet("background:#dc2626; color:white; border-radius:3px; padding:0 4px;"
                                " font-size:10px; font-weight:700;")
        self.clip.setVisible(False)
        self.clip.setToolTip("Input hit the ADC limit. Use a bigger range or a probe/attenuator.")
        top.addWidget(self.btn)
        top.addWidget(self.name, 1)
        top.addWidget(self.clip)
        top.addWidget(self.scale_lbl)
        lay.addLayout(top)

        self.details = QWidget()
        g = QGridLayout(self.details)
        g.setContentsMargins(0, 2, 0, 0)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(3)
        self.vdiv = QComboBox()
        for v in VDIVS:
            self.vdiv.addItem(dsp.si_short(v, "V") + "/div", v)
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-10, 10)
        self.offset.setSingleStep(0.1)
        self.offset.setDecimals(2)
        self.offset.setSuffix(" div")
        self.probe = QComboBox()
        for p in PROBES:
            self.probe.addItem(f"{p}x", p)
        self.coupling = QComboBox()
        self.coupling.addItems(["DC", "AC"])
        self.range = QComboBox()
        self.range.addItem("Auto")
        for s in HW_SCALES:
            self.range.addItem(RANGE_LABELS[s])
        self.range.setToolTip("Hardware input range. Auto picks the most sensitive range that\n"
                              "still covers the whole screen at the current V/div and offset.")
        for i, (lbl, w) in enumerate([("Scale", self.vdiv), ("Offset", self.offset),
                                      ("Probe", self.probe), ("Coupling", self.coupling),
                                      ("Range", self.range)]):
            l = QLabel(lbl)
            l.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            r, c = divmod(i, 2)
            g.addWidget(l, r * 2, c)
            g.addWidget(w, r * 2 + 1, c)
        self.range_info = QLabel()
        self.range_info.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        g.addWidget(self.range_info, 5, 1)
        lay.addWidget(self.details)

        self.load(ch)
        self.btn.toggled.connect(self._ui)
        self.name.editingFinished.connect(self._ui)
        self.vdiv.currentIndexChanged.connect(self._ui)
        self.offset.valueChanged.connect(self._ui)
        self.probe.currentIndexChanged.connect(self._ui)
        self.coupling.currentIndexChanged.connect(self._ui)
        self.range.currentIndexChanged.connect(self._ui)

    def load(self, ch: Channel):
        widgets = [self.btn, self.name, self.vdiv, self.offset, self.probe, self.coupling, self.range]
        for w in widgets:
            w.blockSignals(True)
        self.btn.setChecked(ch.enabled)
        self.name.setText(ch.name)
        self.vdiv.setCurrentIndex(int(np.argmin([abs(math.log(v / ch.vdiv)) for v in VDIVS])))
        self.offset.setValue(ch.offset)
        self.probe.setCurrentIndex(PROBES.index(ch.probe) if ch.probe in PROBES else 0)
        self.coupling.setCurrentText(ch.coupling)
        self.range.setCurrentText(ch.range)
        for w in widgets:
            w.blockSignals(False)
        self._refresh()

    def _ui(self, *_):
        c = self.ch
        c.enabled = self.btn.isChecked()
        c.name = clean_name(self.name.text())
        c.vdiv = self.vdiv.currentData() or c.vdiv
        c.offset = self.offset.value()
        c.probe = self.probe.currentData()
        c.coupling = self.coupling.currentText()
        c.range = self.range.currentText()
        self._refresh()
        self.changed.emit()

    def _refresh(self):
        self.details.setVisible(self.ch.enabled)
        self.scale_lbl.setText(dsp.si_short(self.ch.vdiv, "V") if self.ch.enabled else "")

    def step_vdiv(self, d: int):
        self.vdiv.setCurrentIndex(max(0, min(len(VDIVS) - 1, self.vdiv.currentIndex() + d)))

    def set_range_info(self, scale: float):
        tip = self.ch.probe * hw_full_scale(scale)
        self.range_info.setText(f"hw {RANGE_LABELS[scale]}  (±{dsp.si(tip, 'V', 3)} at tip)")


def pill(text: str, bg: str, fg: str = "#0b0f14") -> str:
    return (f"<span style='background:{bg}; color:{fg}; font-weight:700;'>&nbsp;{text}&nbsp;</span>")


# ------------------------------------------------------------------------------------
# Main window
# ------------------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.setWindowTitle("Scope1008")
        self.resize(1500, 900)
        self.settings = QSettings("Scope1008", "Scope1008")

        # --- state -----------------------------------------------------------------
        self.channels: List[Channel] = [Channel(enabled=i < 2, name="") for i in range(N_CH)]
        self.mode = "auto"           # auto | normal | roll
        self.tdiv_i = BURST_NS_PER_DIV.index(500_000)
        self.roll_i = ROLL_S_PER_DIV.index(1)
        self.trig_ch, self.trig_rising, self.trig_level = 0, True, 1.0
        self.hpos = 0.0
        self.running = True
        self.single = False
        self.measure_ch = 0
        self.load_state()

        self.worker: Optional[AcqWorker] = None
        self.demo = demo
        self.last_x: Optional[np.ndarray] = None
        self.last_data: Dict[int, np.ndarray] = {}
        self.last_dt = 1e-5
        self.refs: Dict[int, tuple] = {}
        self.roll_buf: Dict[int, np.ndarray] = {}
        self.roll_dt = 1 / 440
        self.rec_file = None
        self.rec_writer = None
        self.rec_t = 0.0
        self.rec_count = 0
        self._rec_flush = 0.0
        self.frame_times: List[float] = []
        self.trig_state = ""
        self._last_meas = 0.0
        self._last_roll_draw = 0.0

        self._build_ui()
        self._apply_theme_bits()
        self._sync_all()
        self.start_worker(demo)

    # ---------------------------------------------------------------- persistence
    def load_state(self):
        raw = self.settings.value("state")
        if not raw:
            return
        # Every field is checked: a hand-edited or corrupted settings store must never
        # crash startup or push out-of-range values to the scope.
        try:
            s = json.loads(raw) if isinstance(raw, str) and len(raw) < 100_000 else None
        except ValueError:
            s = None
        if not isinstance(s, dict):
            log.warning("ignoring unreadable saved settings")
            return
        chans = s.get("channels")
        if isinstance(chans, list):
            self.channels = [channel_from_dict(chans[i] if i < len(chans) else None, i) for i in range(N_CH)]
        mode = s.get("mode", "auto")
        self.mode = "normal" if mode == "single" else (mode if mode in MODES else "auto")
        self.tdiv_i = _idx(s.get("tdiv_i"), self.tdiv_i, len(BURST_NS_PER_DIV))
        self.roll_i = _idx(s.get("roll_i"), self.roll_i, len(ROLL_S_PER_DIV))
        self.trig_ch = _idx(s.get("trig_ch"), 0, N_CH)
        self.trig_rising = bool(s.get("trig_rising", True))
        self.trig_level = _num(s.get("trig_level"), 1.0, -1000.0, 1000.0)
        self.hpos = _num(s.get("hpos"), 0.0, -X_DIVS * 10, X_DIVS * 10)
        self.measure_ch = _idx(s.get("measure_ch"), 0, N_CH)

    def save_state(self):
        s = dict(channels=[asdict(c) for c in self.channels], mode=self.mode, tdiv_i=self.tdiv_i,
                 roll_i=self.roll_i, trig_ch=self.trig_ch, trig_rising=self.trig_rising,
                 trig_level=self.trig_level, hpos=self.hpos, measure_ch=self.measure_ch)
        self.settings.setValue("state", json.dumps(s))

    # ---------------------------------------------------------------- UI build
    def _btn(self, text, tip="", checkable=False, cls="tb"):
        b = QPushButton(text)
        b.setCheckable(checkable)
        b.setToolTip(tip)
        b.setProperty("cls", cls)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 4)
        outer.setSpacing(6)

        # --- top bar --------------------------------------------------------------
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.run_btn = self._btn("RUN", "Run / Stop  (Space)", True, "run")
        self.run_btn.setFixedWidth(84)
        self.single_btn = self._btn("Single", "Capture one triggered frame then stop  (S)")
        self.mode_group = QButtonGroup(self)
        mode_box = QHBoxLayout()
        mode_box.setSpacing(0)
        self.mode_btns = {}
        for i, (key, label, tip) in enumerate([
                ("auto", "Auto", "Free-runs if no trigger is found"),
                ("normal", "Normal", "Only updates on a valid trigger"),
                ("roll", "Roll", "Continuous strip-chart / data logger mode (up to 440 Sa/s per channel x rate factor)")]):
            b = self._btn(label, tip, True, "seg")
            b.setProperty("pos", "l" if i == 0 else ("r" if i == 2 else "m"))
            self.mode_group.addButton(b)
            self.mode_btns[key] = b
            mode_box.addWidget(b)
        self.tdiv_dn = self._btn("◀", "Faster timebase  (←, or scroll on the plot)")
        self.tdiv_lbl = QLabel()
        self.tdiv_lbl.setMinimumWidth(110)
        self.tdiv_lbl.setAlignment(Qt.AlignCenter)
        self.tdiv_lbl.setStyleSheet("font-family:monospace; font-size:15px; font-weight:700;")
        self.tdiv_up = self._btn("▶", "Slower timebase  (→, or scroll on the plot)")
        hl = QLabel("H-pos")
        hl.setStyleSheet(f"color:{MUTED};")
        self.hpos_spin = QDoubleSpinBox()
        self.hpos_spin.setRange(-50, 50)
        self.hpos_spin.setSingleStep(0.5)
        self.hpos_spin.setSuffix(" div")
        self.hpos_spin.setToolTip("Horizontal position of the trigger point (0 = centre)")

        self.cur_btn = self._btn("Cursors", "Show time/voltage cursors  (C)", True)
        self.fft_btn = self._btn("FFT", "Spectrum of the measure channel  (F)", True)
        self.ref_btn = self._btn("Ref", "Freeze current traces as reference overlay  (R). Right-click to clear.")
        self.csv_btn = self._btn("CSV", "Export what is on screen to CSV  (Ctrl+S)")
        self.png_btn = self._btn("PNG", "Save a screenshot of the plot  (Ctrl+P)")
        self.rec_btn = self._btn("● Rec", "Roll mode: stream every sample to a CSV file", True, "rec")
        self.dev_btn = QToolButton()
        self.dev_btn.setText("Device")
        self.dev_btn.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(self)
        m.addAction("Connect Hantek 1008", lambda: self.start_worker(False))
        m.addAction("Demo signals", lambda: self.start_worker(True))
        m.addSeparator()
        m.addAction("Recalibrate zero (inputs open)", lambda: self.worker and self.worker.recalibrate())
        self.dev_btn.setMenu(m)

        for w in [self.run_btn, self.single_btn]:
            bar.addWidget(w)
        bar.addSpacing(8)
        bar.addLayout(mode_box)
        bar.addSpacing(8)
        for w in [self.tdiv_dn, self.tdiv_lbl, self.tdiv_up]:
            bar.addWidget(w)
        bar.addSpacing(4)
        bar.addWidget(hl)
        bar.addWidget(self.hpos_spin)
        bar.addStretch(1)
        for w in [self.cur_btn, self.fft_btn, self.ref_btn, self.csv_btn, self.png_btn, self.rec_btn]:
            bar.addWidget(w)
        bar.addSpacing(8)
        bar.addWidget(self.dev_btn)
        outer.addLayout(bar)

        # --- banner ---------------------------------------------------------------
        self.banner = QFrame()
        self.banner.setStyleSheet("background:#3b2f0b; border:1px solid #a16207; border-radius:6px;")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(10, 4, 6, 4)
        self.banner_lbl = QLabel()
        self.banner_lbl.setWordWrap(True)
        self.banner_lbl.setStyleSheet("color:#fde68a; border:none;")
        retry = self._btn("Retry scope")
        retry.clicked.connect(lambda: self.start_worker(False))
        demo = self._btn("Use demo")
        demo.clicked.connect(lambda: self.start_worker(True))
        bl.addWidget(self.banner_lbl, 1)
        bl.addWidget(retry)
        bl.addWidget(demo)
        self.banner.hide()
        outer.addWidget(self.banner)

        # --- main split -----------------------------------------------------------
        split = QSplitter(Qt.Horizontal)
        outer.addWidget(split, 1)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)
        vsplit = QSplitter(Qt.Vertical)
        lv.addWidget(vsplit, 1)

        pg.setConfigOptions(antialias=True, background=BG, foreground=MUTED)
        self.vb = ScopeViewBox()
        self.vb.setMouseEnabled(False, False)
        self.vb.setMenuEnabled(False)
        self.plot = pg.PlotWidget(viewBox=self.vb)
        pi = self.plot.getPlotItem()
        pi.hideButtons()
        pi.hideAxis("left")
        pi.setLabel("bottom", units="s")
        pi.getAxis("bottom").setPen(pg.mkPen(BORDER))
        pi.getAxis("bottom").setTextPen(pg.mkPen(MUTED))
        self.grid_lines = []
        for i in range(X_DIVS + 1):
            ln = pg.InfiniteLine(angle=90, pen=pg.mkPen("#2b3645" if i != X_DIVS // 2 else "#3d4b5e",
                                                        width=1, style=Qt.DotLine if i % X_DIVS else Qt.SolidLine))
            self.plot.addItem(ln, ignoreBounds=True)
            self.grid_lines.append(ln)
        for j in range(-Y_DIVS // 2, Y_DIVS // 2 + 1):
            ln = pg.InfiniteLine(pos=j, angle=0, pen=pg.mkPen("#2b3645" if j else "#3d4b5e", width=1,
                                                               style=Qt.DotLine if abs(j) != Y_DIVS // 2 else Qt.SolidLine))
            self.plot.addItem(ln, ignoreBounds=True)
        self.ref_curves = []
        self.curves = []
        self.gnd_marks = []
        for i in range(N_CH):
            rc = self.plot.plot(pen=pg.mkPen(QColor(COLORS[i]).darker(260), width=1.2))
            self.ref_curves.append(rc)
        for i in range(N_CH):
            c = self.plot.plot(pen=pg.mkPen(COLORS[i], width=1.4))
            c.setDownsampling(auto=True, method="peak")
            c.setClipToView(True)
            self.curves.append(c)
            t = pg.TextItem(f"{i + 1}▶", color=COLORS[i], anchor=(0, 0.5))
            t.setZValue(20)
            self.plot.addItem(t, ignoreBounds=True)
            self.gnd_marks.append(t)
        self.trig_line = pg.InfiniteLine(angle=0, movable=True, pen=pg.mkPen(COLORS[0], width=1, style=Qt.DashLine),
                                         hoverPen=pg.mkPen("white", width=2))
        self.trig_line.setZValue(30)
        self.plot.addItem(self.trig_line, ignoreBounds=True)
        self.trig_mark = pg.InfiniteLine(angle=90, pen=pg.mkPen("#f59e0b", width=1, style=Qt.DashLine))
        self.plot.addItem(self.trig_mark, ignoreBounds=True)
        self.trig_txt = pg.TextItem("T", color="#f59e0b", anchor=(0.5, 0))
        self.plot.addItem(self.trig_txt, ignoreBounds=True)

        cpen = pg.mkPen("#a3e635", width=1, style=Qt.DashDotLine)
        self.cx = [pg.InfiniteLine(angle=90, movable=True, pen=cpen, hoverPen=pg.mkPen("white", width=2),
                                   label=n, labelOpts=dict(position=0.97, color="#a3e635"))
                   for n in ("X1", "X2")]
        self.cy = [pg.InfiniteLine(angle=0, movable=True, pen=cpen, hoverPen=pg.mkPen("white", width=2),
                                   label=n, labelOpts=dict(position=0.02, color="#a3e635"))
                   for n in ("Y1", "Y2")]
        for ln in self.cx + self.cy:
            ln.setZValue(40)
            ln.setVisible(False)
            self.plot.addItem(ln, ignoreBounds=True)
        self.cy[0].setValue(1)
        self.cy[1].setValue(-1)

        self.overlay = pg.TextItem("", color="#93a3b5", anchor=(1, 0))
        self.plot.addItem(self.overlay, ignoreBounds=True)
        vsplit.addWidget(self.plot)

        self.fft = pg.PlotWidget()
        fpi = self.fft.getPlotItem()
        fpi.setLabel("bottom", units="Hz")
        fpi.setLabel("left", "dBV")
        fpi.showGrid(x=True, y=True, alpha=0.15)
        self.fft.setMouseEnabled(x=True, y=False)
        self.fft_curve = self.fft.plot(pen=pg.mkPen(COLORS[0], width=1.2))
        self.fft_peak = pg.TextItem("", color=TEXT, anchor=(0, 0))
        self.fft.addItem(self.fft_peak, ignoreBounds=True)
        self.fft.hide()
        vsplit.addWidget(self.fft)
        vsplit.setSizes([700, 260])

        # cursor readout + measurements
        self.cur_lbl = QLabel()
        self.cur_lbl.setStyleSheet(f"background:{PANEL}; border:1px solid {BORDER}; border-radius:6px;"
                                   " padding:4px 8px; font-family:monospace; color:#d9f99d;")
        self.cur_lbl.hide()
        lv.addWidget(self.cur_lbl)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(["Ch", "Vpp", "Max", "Min", "Mean", "RMS", "Freq",
                                              "Period", "Duty", "Rise"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMaximumHeight(200)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        lv.addWidget(self.table)
        split.addWidget(left)

        # --- right sidebar --------------------------------------------------------
        side = QWidget()
        side.setMinimumWidth(300)
        side.setMaximumWidth(380)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(6)

        trig = QFrame()
        trig.setObjectName("trig")
        trig.setStyleSheet(f"#trig {{ background:{PANEL}; border:1px solid {BORDER}; border-radius:6px; }}")
        tg = QGridLayout(trig)
        tg.setContentsMargins(8, 6, 8, 8)
        title = QLabel("TRIGGER")
        title.setStyleSheet("font-weight:700; letter-spacing:1px;")
        self.trig_state_lbl = QLabel()
        tg.addWidget(title, 0, 0)
        tg.addWidget(self.trig_state_lbl, 0, 1, Qt.AlignRight)
        self.trig_src = QComboBox()
        for i in range(N_CH):
            self.trig_src.addItem(f"CH{i + 1}")
        self.slope_btn = self._btn("↑ Rising", "Trigger slope  (T)")
        self.level_spin = QDoubleSpinBox()
        self.level_spin.setRange(-1000, 1000)
        self.level_spin.setDecimals(3)
        self.level_spin.setSingleStep(0.1)
        self.level_spin.setSuffix(" V")
        self.level_spin.setToolTip("Trigger level. You can also drag the dashed line on the plot.")
        self.level50 = self._btn("50%", "Set level to the middle of the trigger signal")
        for r, (a, b) in enumerate([("Source", self.trig_src), ("Slope", self.slope_btn)]):
            l = QLabel(a)
            l.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            tg.addWidget(l, 1, r)
            tg.addWidget(b, 2, r)
        l = QLabel("Level")
        l.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        tg.addWidget(l, 3, 0)
        tg.addWidget(self.level_spin, 4, 0)
        tg.addWidget(self.level50, 4, 1)
        l = QLabel("Measure / FFT / cursor channel")
        l.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        tg.addWidget(l, 5, 0, 1, 2)
        self.meas_src = QComboBox()
        for i in range(N_CH):
            self.meas_src.addItem(f"CH{i + 1}")
        tg.addWidget(self.meas_src, 6, 0, 1, 2)
        sv.addWidget(trig)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(0, 0, 4, 0)
        hv.setSpacing(6)
        self.cards = []
        for i in range(N_CH):
            card = ChannelCard(i, self.channels[i])
            card.changed.connect(self._on_channel_change)
            hv.addWidget(card)
            self.cards.append(card)
        hv.addStretch(1)
        scroll.setWidget(holder)
        sv.addWidget(scroll, 1)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setSizes([1150, 340])

        # status bar
        self.status_lbl = QLabel()
        self.info_lbl = QLabel()
        self.info_lbl.setStyleSheet(f"color:{MUTED}; font-family:monospace;")
        self.statusBar().addWidget(self.status_lbl, 1)
        self.statusBar().addPermanentWidget(self.info_lbl)

        # --- signals --------------------------------------------------------------
        self.run_btn.toggled.connect(self.set_running)
        self.single_btn.clicked.connect(self.do_single)
        for key, b in self.mode_btns.items():
            b.clicked.connect(lambda _=False, k=key: self.set_mode(k))
        self.tdiv_dn.clicked.connect(lambda: self.step_tdiv(-1))
        self.tdiv_up.clicked.connect(lambda: self.step_tdiv(1))
        self.vb.wheel.connect(lambda d: self.step_tdiv(-d))
        self.hpos_spin.valueChanged.connect(self._on_hpos)
        self.cur_btn.toggled.connect(self._on_cursors)
        self.fft_btn.toggled.connect(lambda on: (self.fft.setVisible(on), self.redraw()))
        self.ref_btn.clicked.connect(self.save_ref)
        self.ref_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.ref_btn.customContextMenuRequested.connect(lambda _: self.clear_ref())
        self.csv_btn.clicked.connect(self.export_csv)
        self.png_btn.clicked.connect(self.export_png)
        self.rec_btn.toggled.connect(self.toggle_record)
        self.trig_src.currentIndexChanged.connect(self._on_trig)
        self.slope_btn.clicked.connect(self._toggle_slope)
        self.level_spin.valueChanged.connect(self._on_level_spin)
        self.level50.clicked.connect(self.level_50)
        self.trig_line.sigPositionChanged.connect(self._on_level_drag)
        self.meas_src.currentIndexChanged.connect(self._on_meas_src)
        for ln in self.cx + self.cy:
            ln.sigPositionChanged.connect(self.update_cursor_readout)

        sc = lambda key, fn: QShortcut(QKeySequence(key), self, activated=fn)
        sc("Space", lambda: self.run_btn.toggle())
        sc("S", self.do_single)
        sc("Left", lambda: self.step_tdiv(-1))
        sc("Right", lambda: self.step_tdiv(1))
        sc("C", lambda: self.cur_btn.toggle())
        sc("F", lambda: self.fft_btn.toggle())
        sc("R", self.save_ref)
        sc("T", self._toggle_slope)
        sc("Ctrl+S", self.export_csv)
        sc("Ctrl+P", self.export_png)
        sc("Up", lambda: self.cards[self.measure_ch].step_vdiv(1))
        sc("Down", lambda: self.cards[self.measure_ch].step_vdiv(-1))
        for i in range(N_CH):
            sc(str(i + 1), lambda i=i: self.cards[i].btn.toggle())

        self.cfg_timer = QTimer(self, singleShot=True, interval=120)
        self.cfg_timer.timeout.connect(self.push_config)

    def _apply_theme_bits(self):
        self.setStyleSheet(f"""
            QWidget {{ color:{TEXT}; font-size:12px; }}
            QMainWindow, QScrollArea > QWidget > QWidget {{ background:{BG}; }}
            QPushButton[cls="tb"], QPushButton[cls="seg"], QPushButton[cls="rec"], QToolButton {{
                background:#18212c; border:1px solid {BORDER}; border-radius:5px; padding:5px 10px; }}
            QPushButton[cls="tb"]:hover, QPushButton[cls="seg"]:hover, QToolButton:hover {{ background:#223041; }}
            QPushButton[cls="tb"]:checked {{ background:#1e3a5f; border-color:#3b82f6; }}
            QPushButton[cls="seg"] {{ border-radius:0; padding:5px 14px; }}
            QPushButton[cls="seg"][pos="l"] {{ border-top-left-radius:5px; border-bottom-left-radius:5px; }}
            QPushButton[cls="seg"][pos="r"] {{ border-top-right-radius:5px; border-bottom-right-radius:5px; }}
            QPushButton[cls="seg"]:checked {{ background:#1e3a5f; border-color:#3b82f6; color:white; }}
            QPushButton[cls="run"] {{ background:#7f1d1d; border:1px solid #b91c1c; border-radius:5px;
                padding:5px; font-weight:800; letter-spacing:1px; color:white; }}
            QPushButton[cls="run"]:checked {{ background:#14532d; border-color:#16a34a; }}
            QPushButton[cls="rec"]:checked {{ background:#7f1d1d; border-color:#ef4444; color:white; }}
            QComboBox, QLineEdit, QDoubleSpinBox {{ background:#0f151d; border:1px solid {BORDER};
                border-radius:4px; padding:2px 6px; min-height:20px; }}
            QComboBox QAbstractItemView {{ background:#0f151d; selection-background-color:#1e3a5f; }}
            QTableWidget {{ background:{PANEL}; alternate-background-color:#0f151d; border:1px solid {BORDER};
                border-radius:6px; font-family:monospace; }}
            QHeaderView::section {{ background:{PANEL}; color:{MUTED}; border:none; padding:3px; }}
            QStatusBar {{ background:{BG}; }}
            QSplitter::handle {{ background:{BG}; }}
            QMenu {{ background:{PANEL}; border:1px solid {BORDER}; }}
            QMenu::item:selected {{ background:#1e3a5f; }}
            QToolTip {{ background:{PANEL}; color:{TEXT}; border:1px solid {BORDER}; }}
        """)

    # ---------------------------------------------------------------- worker
    def start_worker(self, demo: bool):
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self.demo = demo
        self.banner.hide()
        self.roll_buf = {}
        self.worker = AcqWorker(demo, self.build_config())
        self.worker.frame.connect(self.on_frame)
        self.worker.status.connect(self.on_status)
        self.worker.progress.connect(self.on_progress)
        self.worker.set_running(self.running)
        self.worker.start()

    def on_status(self, state: str, msg: str):
        colors = {"ok": "#4ade80", "demo": "#fbbf24", "connecting": "#60a5fa", "warn": "#fbbf24",
                  "error": "#f87171"}
        safe = html.escape(msg)  # device/OS error text is shown as rich text
        self.status_lbl.setText(f"<span style='color:{colors.get(state, TEXT)}'>●</span> {safe}")
        if state == "warn":
            return
        if state == "error":
            self.banner_lbl.setText(f"<b>Scope not available.</b> {safe}")
            self.banner.show()
            if not self.demo:
                # keep the UI alive with demo signals so it's still useful
                QTimer.singleShot(0, self._fallback_demo)
        self.dev_btn.setText({"ok": "Hantek 1008", "demo": "Demo", "connecting": "…"}.get(state, "Device"))
        if self.demo and state == "demo":
            for i, c in enumerate(self.channels):
                if not c.name:
                    self.cards[i].name.setPlaceholderText(DemoBackend.LABELS[i])

    def _fallback_demo(self):
        msg = self.banner_lbl.text()
        self.start_worker(True)
        self.banner_lbl.setText(msg + "<br>Showing demo signals meanwhile.")
        self.banner.show()

    # ---------------------------------------------------------------- config
    def active(self) -> List[int]:
        return [i for i, c in enumerate(self.channels) if c.enabled]

    def hw_scale(self, i: int) -> float:
        c = self.channels[i]
        if c.range != "Auto":
            return {v: k for k, v in RANGE_LABELS.items()}[c.range]
        need = (Y_DIVS / 2 + abs(c.offset)) * c.vdiv / c.probe
        if c.coupling == "AC":
            need *= 1.5  # leave room for the DC part we can't see
        for s in reversed(HW_SCALES):  # most sensitive first
            if hw_full_scale(s) >= need:
                return s
        return HW_SCALES[0]

    def tdiv(self) -> float:
        if self.mode == "roll":
            return ROLL_S_PER_DIV[self.roll_i]
        return BURST_NS_PER_DIV[self.tdiv_i] * 1e-9

    def build_config(self) -> AcqConfig:
        act = self.active() or [0]
        scales = [self.hw_scale(i) for i in range(N_CH)]
        tc = self.channels[self.trig_ch]
        return AcqConfig(
            active=act, hw_scales=scales, ns_per_div=BURST_NS_PER_DIV[self.tdiv_i],
            trig_ch=self.trig_ch, trig_slope="rising" if self.trig_rising else "falling",
            trig_level_in=self.trig_level / tc.probe, roll=self.mode == "roll",
            roll_rate=pick_roll_rate(ROLL_S_PER_DIV[self.roll_i], len(act)))

    def schedule_config(self):
        self.cfg_timer.start()

    def push_config(self):
        if self.worker:
            self.worker.set_config(self.build_config())

    # ---------------------------------------------------------------- UI handlers
    def _sync_all(self):
        self.run_btn.blockSignals(True)
        self.run_btn.setChecked(self.running)
        self.run_btn.setText("RUN" if self.running else "STOP")
        self.run_btn.blockSignals(False)
        for k, b in self.mode_btns.items():
            b.setChecked(k == self.mode)
        self.tdiv_lbl.setText(fmt_tdiv(self.tdiv()))
        self.hpos_spin.blockSignals(True)
        self.hpos_spin.setValue(self.hpos)
        self.hpos_spin.setEnabled(self.mode != "roll")
        self.hpos_spin.blockSignals(False)
        self.trig_src.blockSignals(True)
        self.trig_src.setCurrentIndex(self.trig_ch)
        self.trig_src.blockSignals(False)
        self.meas_src.blockSignals(True)
        self.meas_src.setCurrentIndex(self.measure_ch)
        self.meas_src.blockSignals(False)
        self.slope_btn.setText("↑ Rising" if self.trig_rising else "↓ Falling")
        self.level_spin.blockSignals(True)
        self.level_spin.setValue(self.trig_level)
        self.level_spin.blockSignals(False)
        self.rec_btn.setEnabled(self.mode == "roll")
        self.single_btn.setEnabled(self.mode != "roll")
        for i, card in enumerate(self.cards):
            card.set_range_info(self.hw_scale(i))
        self.fft_curve.setPen(pg.mkPen(COLORS[self.measure_ch], width=1.2))
        self._place_static()

    def _on_channel_change(self):
        if self.mode == "roll":
            self.roll_buf = {}
        self._sync_all()
        self.schedule_config()
        self.redraw()

    def set_running(self, on: bool):
        self.running = on
        if on:
            self.single = False
        self.run_btn.setText("RUN" if on else "STOP")
        if self.worker:
            self.worker.set_running(on)
        if not on and self.rec_btn.isChecked():
            self.rec_btn.setChecked(False)

    def do_single(self):
        if self.mode == "roll":
            return
        self.single = True
        self.trig_state = "ARMED"
        self.run_btn.blockSignals(True)
        self.run_btn.setChecked(True)
        self.run_btn.setText("RUN")
        self.run_btn.blockSignals(False)
        self.running = True
        if self.worker:
            self.worker.set_running(True)

    def set_mode(self, mode: str):
        if mode == self.mode:
            self._sync_all()
            return
        self.mode = mode
        self.single = False
        self.roll_buf = {}
        self.last_x = None
        if mode != "roll" and self.rec_btn.isChecked():
            self.rec_btn.setChecked(False)
        self._sync_all()
        self.push_config()
        self.redraw()

    def step_tdiv(self, d: int):
        if self.mode == "roll":
            self.roll_i = max(0, min(len(ROLL_S_PER_DIV) - 1, self.roll_i + d))
            if self.rec_btn.isChecked():
                pass  # rate may change; recording continues with new dt
        else:
            self.tdiv_i = max(0, min(len(BURST_NS_PER_DIV) - 1, self.tdiv_i + d))
        self._sync_all()
        self.schedule_config()
        self.redraw()

    def _on_hpos(self, v):
        self.hpos = v
        self._place_static()
        self.redraw()

    def _on_cursors(self, on):
        for ln in self.cx + self.cy:
            ln.setVisible(on)
        self.cur_lbl.setVisible(on)
        if on:
            l, r = self.x_range()
            w = r - l
            self.cx[0].setValue(l + 0.3 * w)
            self.cx[1].setValue(l + 0.7 * w)
        self.update_cursor_readout()

    def _on_trig(self, i):
        self.trig_ch = i
        self._sync_all()
        self.schedule_config()
        self.redraw()

    def _toggle_slope(self):
        self.trig_rising = not self.trig_rising
        self._sync_all()
        self.schedule_config()

    def _on_level_spin(self, v):
        self.trig_level = v
        self._place_static()
        self.schedule_config()

    def _on_level_drag(self):
        c = self.channels[self.trig_ch]
        v = (self.trig_line.value() - c.offset) * c.vdiv
        if abs(v - self.trig_level) > 1e-9:
            self.trig_level = v
            self.level_spin.blockSignals(True)
            self.level_spin.setValue(v)
            self.level_spin.blockSignals(False)
            self.schedule_config()

    def level_50(self):
        v = self._channel_view(self.trig_ch)
        if v is not None and v.size:
            self.trig_level = float((v.max() + v.min()) / 2)
            self._sync_all()
            self.schedule_config()

    def _on_meas_src(self, i):
        self.measure_ch = i
        self._sync_all()
        self.redraw()

    # ---------------------------------------------------------------- geometry
    def x_range(self):
        td = self.tdiv()
        if self.mode == "roll":
            return -X_DIVS * td, 0.0
        return -X_DIVS / 2 * td + self.hpos * td, X_DIVS / 2 * td + self.hpos * td

    def _place_static(self):
        l, r = self.x_range()
        td = self.tdiv()
        self.vb.setRange(xRange=(l, r), yRange=(-Y_DIVS / 2, Y_DIVS / 2), padding=0)
        for i, ln in enumerate(self.grid_lines):
            ln.setValue(l + i * td)
        for c, m in zip(self.channels, self.gnd_marks):
            m.setVisible(c.enabled)
            m.setPos(l, max(-Y_DIVS / 2 + 0.15, min(Y_DIVS / 2 - 0.15, c.offset)))
        tc = self.channels[self.trig_ch]
        show_trig = self.mode != "roll"
        self.trig_line.setVisible(show_trig)
        self.trig_mark.setVisible(show_trig)
        self.trig_txt.setVisible(show_trig)
        self.trig_line.setPen(pg.mkPen(COLORS[self.trig_ch], width=1, style=Qt.DashLine))
        self.trig_line.blockSignals(True)
        self.trig_line.setValue(self.trig_level / tc.vdiv + tc.offset)
        self.trig_line.blockSignals(False)
        self.trig_mark.setValue(0)
        self.trig_txt.setPos(0, Y_DIVS / 2)
        self.overlay.setPos(r, Y_DIVS / 2)
        self.update_cursor_readout()

    # ---------------------------------------------------------------- data path
    def _proc(self, i: int, v: np.ndarray, ac: bool = True) -> np.ndarray:
        c = self.channels[i]
        out = v * c.probe
        if ac and c.coupling == "AC" and out.size:
            out = out - out.mean()
        return out

    def _channel_view(self, i: int) -> Optional[np.ndarray]:
        if self.mode == "roll":
            return self._roll_view(i)
        return self.last_data.get(i)

    def _roll_view(self, i):
        v = self.roll_buf.get(i)
        if v is None:
            return None
        c = self.channels[i]
        out = v * c.probe
        if c.coupling == "AC" and out.size:
            out = out - out.mean()
        return out

    def on_frame(self, fr: Frame):
        if not self.running:
            return
        now = time.time()
        self.frame_times = [t for t in self.frame_times if now - t < 2] + [now]
        for i in range(N_CH):
            self.cards[i].clip.setVisible(bool(fr.clipped.get(i)))
        if fr.kind == "roll":
            if self.mode != "roll":
                return
            self._on_roll(fr)
            return
        if self.mode == "roll":
            return
        data = {i: self._proc(i, v) for i, v in fr.data.items() if self.channels[i].enabled}
        if not data:
            return
        n = min(v.size for v in data.values())
        dt = fr.dt
        td = self.tdiv()
        trig_pos = None
        if self.trig_ch in data:
            c = self.channels[self.trig_ch]
            pos = dsp.edge_crossings(data[self.trig_ch][:n], self.trig_level, self.trig_rising,
                                     hyst=max(0.12 * c.vdiv, 1e-4))
            pre = max(0.0, (X_DIVS / 2 - self.hpos) * td / dt)
            post = max(0.0, (X_DIVS / 2 + self.hpos) * td / dt)
            trig_pos = dsp.choose_trigger(pos, n, pre, post)
        if trig_pos is not None:
            self.trig_state = "TRIG'D"
            x = (np.arange(n) - trig_pos) * dt
        else:
            if self.mode == "normal" or self.single:
                self.trig_state = "WAIT"
                self._update_info(n, dt)
                return
            self.trig_state = "AUTO"
            x = (np.arange(n) - n / 2) * dt
        self.last_x = x
        self.last_dt = dt
        self.last_data = {i: v[:n] for i, v in data.items()}
        self.redraw()
        self._update_info(n, dt)
        if self.single and trig_pos is not None:
            self.single = False
            self.run_btn.setChecked(False)
            self.trig_state = "STOP"
            self._update_trig_lbl()

    def _on_roll(self, fr: Frame):
        sdiv = ROLL_S_PER_DIV[self.roll_i]
        maxlen = int(X_DIVS * sdiv / fr.dt) + 2
        if abs(fr.dt - self.roll_dt) > 1e-12:
            self.roll_buf = {}
            self.roll_dt = fr.dt
        for i, v in fr.data.items():
            if not self.channels[i].enabled:
                continue
            b = self.roll_buf.get(i)
            b = v if b is None else np.concatenate([b, v])
            self.roll_buf[i] = b[-maxlen:]
        if self.rec_writer is not None:
            chans = sorted(fr.data)
            n = min(len(fr.data[c]) for c in chans)
            cols = [fr.data[c][:n] * self.channels[c].probe for c in chans]
            t = self.rec_t + np.arange(n) * fr.dt
            self.rec_t += n * fr.dt
            try:
                self.rec_writer.writerows(zip(*([np.round(t, 6)] + [np.round(c, 5) for c in cols])))
                if time.time() - self._rec_flush > 1.0:
                    self.rec_file.flush()
                    self._rec_flush = time.time()
            except OSError as e:
                log.error("recording write failed: %s", e)
                self.rec_btn.setChecked(False)  # closes the file via toggle_record
                self._set_status(f"Recording stopped: {e}")
                return
            self.rec_count += n
        now = time.time()
        if now - self._last_roll_draw > 1 / 30:
            self._last_roll_draw = now
            self.trig_state = "ROLL"
            self.redraw()
            n = max((b.size for b in self.roll_buf.values()), default=0)
            self._update_info(n, fr.dt)

    def redraw(self):
        if self.mode == "roll":
            views = {i: self._roll_view(i) for i in self.roll_buf}
            xs = {i: (np.arange(v.size) - (v.size - 1)) * self.roll_dt for i, v in views.items() if v is not None}
            dt = self.roll_dt
        else:
            views = self.last_data
            xs = {i: self.last_x for i in views} if self.last_x is not None else {}
            dt = self.last_dt
        for i in range(N_CH):
            c = self.channels[i]
            v = views.get(i)
            if c.enabled and v is not None and i in xs:
                self.curves[i].setData(xs[i], v / c.vdiv + c.offset)
            else:
                self.curves[i].setData([], [])
            ref = self.refs.get(i)
            if ref is not None and c.enabled:
                self.ref_curves[i].setData(ref[0], ref[1] / c.vdiv + c.offset)
            else:
                self.ref_curves[i].setData([], [])
        self.overlay.setText(f"{self.tdiv_lbl.text()}   " + "  ".join(
            f"{i + 1}:{dsp.si_short(self.channels[i].vdiv, 'V')}" for i in self.active()))
        now = time.time()
        if now - self._last_meas > 0.2:
            self._last_meas = now
            self.update_measurements(views, dt)
            if self.fft.isVisible():
                self.update_fft(views.get(self.measure_ch), dt)
        self.update_cursor_readout()
        self._update_trig_lbl()

    def update_measurements(self, views, dt):
        rows = [i for i in self.active() if views.get(i) is not None]
        self.table.setRowCount(len(rows))
        for r, i in enumerate(rows):
            m = dsp.measure(views[i], dt)
            name = self.channels[i].name or f"CH{i + 1}"
            vals = [name, dsp.si(m["pp"], "V"), dsp.si(m["max"], "V"), dsp.si(m["min"], "V"),
                    dsp.si(m["mean"], "V"), dsp.si(m["rms"], "V"), dsp.si(m["freq"], "Hz"),
                    dsp.si(m["period"], "s"),
                    "--" if not math.isfinite(m["duty"]) else f"{m['duty']:.1f} %",
                    dsp.si(m["rise"], "s")]
            for col, txt in enumerate(vals):
                it = self.table.item(r, col)
                if it is None:
                    it = QTableWidgetItem()
                    it.setTextAlignment(Qt.AlignCenter)
                    self.table.setItem(r, col, it)
                it.setText(txt)
                it.setForeground(QColor(COLORS[i] if col == 0 else TEXT))

    def update_fft(self, v, dt):
        if v is None or v.size < 16:
            self.fft_curve.setData([], [])
            self.fft_peak.setText("")
            return
        f, db = dsp.fft_dbv(v, dt)
        self.fft_curve.setData(f[1:], db[1:])
        k = int(np.argmax(db[1:])) + 1
        self.fft_peak.setText(f"peak {dsp.si(f[k], 'Hz')}  {db[k]:.1f} dBV")
        self.fft_peak.setPos(f[1], float(db[1:].max()))
        self.fft.setYRange(max(-100, float(np.percentile(db, 5)) - 5), float(db[1:].max()) + 6, padding=0)
        self.fft.setXRange(0, f[-1], padding=0)

    def update_cursor_readout(self, *_):
        if not self.cur_btn.isChecked():
            return
        c = self.channels[self.measure_ch]
        x1, x2 = self.cx[0].value(), self.cx[1].value()
        y1 = (self.cy[0].value() - c.offset) * c.vdiv
        y2 = (self.cy[1].value() - c.offset) * c.vdiv
        dx = x2 - x1
        parts = [f"X1 {dsp.si(x1, 's')}", f"X2 {dsp.si(x2, 's')}", f"ΔX {dsp.si(dx, 's')}",
                 f"1/ΔX {dsp.si(1 / dx, 'Hz') if dx else '--'}",
                 f"│ CH{self.measure_ch + 1}: Y1 {dsp.si(y1, 'V')}", f"Y2 {dsp.si(y2, 'V')}",
                 f"ΔY {dsp.si(y2 - y1, 'V')}"]
        v = self._channel_view(self.measure_ch)
        if v is not None and v.size:
            if self.mode == "roll":
                xs = (np.arange(v.size) - (v.size - 1)) * self.roll_dt
            else:
                xs = self.last_x
            if xs is not None and xs.size == v.size:
                a, b = np.interp([x1, x2], xs, v)
                parts.append(f"│ trace@X1 {dsp.si(a, 'V')}  @X2 {dsp.si(b, 'V')}")
        self.cur_lbl.setText("   ".join(parts))

    def on_progress(self, frac: float):
        """A slow capture is still filling: show it instead of looking frozen."""
        if self.mode == "roll" or not self.running:
            return
        # only worth showing when the capture takes noticeably longer than a frame
        if burst_hw_timebase(BURST_NS_PER_DIV[self.tdiv_i])[1] < 0.3:
            return
        self.trig_state = f"FILL {frac:.0%}"
        self._update_trig_lbl()

    def _update_trig_lbl(self):
        st = self.trig_state
        if st.startswith("FILL"):
            self.trig_state_lbl.setText(pill(st, "#60a5fa") if self.running else pill("STOP", "#9ca3af"))
            return
        col = {"TRIG'D": "#4ade80", "AUTO": "#fbbf24", "WAIT": "#f87171", "ROLL": "#60a5fa",
               "ARMED": "#f59e0b", "STOP": "#9ca3af"}.get(st, MUTED)
        if not self.running and st != "STOP":
            st, col = "STOP", "#9ca3af"
        self.trig_state_lbl.setText(pill(st, col) if st else "")

    def _update_info(self, n, dt):
        fps = len(self.frame_times) / 2.0
        if self.mode == "roll":
            fs = 1 / dt if dt else 0
            rec = f"  REC {self.rec_count} rows" if self.rec_writer else ""
            self.info_lbl.setText(f"{dsp.si(fs, 'Sa/s')} per ch   {n} pts   window {dsp.si(n * dt, 's')}{rec}")
        else:
            self.info_lbl.setText(f"{dsp.si(1 / dt, 'Sa/s')}   {n} pts/ch   {fps:.0f} fps")
        self._update_trig_lbl()

    # ---------------------------------------------------------------- refs / export
    def save_ref(self):
        if self.mode == "roll":
            self.refs = {i: ((np.arange(v.size) - (v.size - 1)) * self.roll_dt, v.copy())
                         for i in self.roll_buf if (v := self._roll_view(i)) is not None}
        elif self.last_x is not None:
            self.refs = {i: (self.last_x.copy(), v.copy()) for i, v in self.last_data.items()}
        self.status_lbl.setText("Reference saved (right-click Ref to clear)")
        self.redraw()

    def clear_ref(self):
        self.refs = {}
        self.redraw()

    def _set_status(self, text: str) -> None:
        self.status_lbl.setText(html.escape(text))

    def _ask_path(self, title, suffix, filt):
        default = Path.home() / f"scope1008_{time.strftime('%Y%m%d_%H%M%S')}.{suffix}"
        p, _ = QFileDialog.getSaveFileName(self, title, str(default), filt)
        return p

    def export_csv(self):
        if self.mode == "roll":
            views = {i: self._roll_view(i) for i in self.roll_buf}
            if not views:
                return
            n = min(v.size for v in views.values())
            x = (np.arange(n) - (n - 1)) * self.roll_dt
            views = {i: v[-n:] for i, v in views.items()}
        else:
            if self.last_x is None:
                return
            views, x = self.last_data, self.last_x
        p = self._ask_path("Export CSV", "csv", "CSV (*.csv)")
        if not p:
            return
        chans = sorted(views)
        try:
            with open(p, "w", newline="", encoding="utf-8") as f:
                f.write(f"# Scope1008 export {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# mode={self.mode} timebase={self.tdiv_lbl.text()} trigger=CH{self.trig_ch + 1} "
                        f"{'rising' if self.trig_rising else 'falling'} {self.trig_level:.4g} V\n")
                for i in chans:
                    c = self.channels[i]
                    f.write(f"# CH{i + 1} '{clean_name(c.name)}' probe={c.probe}x coupling={c.coupling} "
                            f"range={RANGE_LABELS[self.hw_scale(i)]}\n")
                w = csv.writer(f)
                w.writerow(["time_s"] + [csv_cell(f"CH{i + 1}{'_' + clean_name(self.channels[i].name) if self.channels[i].name else ''}_V")
                                         for i in chans])
                w.writerows(zip(np.round(x, 9), *[np.round(views[i], 5) for i in chans]))
        except OSError as e:
            log.error("CSV export failed: %s", e)
            self._set_status(f"Could not save CSV: {e}")
            return
        self._set_status(f"Saved {p}")

    def export_png(self):
        p = self._ask_path("Save screenshot", "png", "PNG (*.png)")
        if p:
            if self.centralWidget().grab().save(p):
                self._set_status(f"Saved {p}")
            else:
                self._set_status(f"Could not save PNG to {p}")

    def toggle_record(self, on: bool):
        if on:
            p = self._ask_path("Record to CSV", "csv", "CSV (*.csv)")
            if not p:
                self.rec_btn.blockSignals(True)
                self.rec_btn.setChecked(False)
                self.rec_btn.blockSignals(False)
                return
            try:
                f = open(p, "w", newline="", encoding="utf-8")  # closed in toggle_record(False)
                f.write(f"# Scope1008 roll recording started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# sample rate {1 / self.roll_dt:.3f} Sa/s per channel\n")
                w = csv.writer(f)
                w.writerow(["time_s"] + [f"CH{i + 1}_V" for i in self.active()])
            except OSError as e:
                log.error("cannot start recording: %s", e)
                self.rec_btn.blockSignals(True)
                self.rec_btn.setChecked(False)
                self.rec_btn.blockSignals(False)
                self._set_status(f"Could not record to {p}: {e}")
                return
            self.rec_file, self.rec_writer = f, w
            self.rec_t = 0.0
            self.rec_count = 0
            self._rec_flush = time.time()
            self._set_status(f"Recording to {p}")
        else:
            f, self.rec_file, self.rec_writer = self.rec_file, None, None
            if f:
                try:
                    f.close()
                    self._set_status(f"Recording stopped, {self.rec_count} rows")
                except OSError as e:
                    self._set_status(f"Recording stopped with a write error: {e}")

    # ---------------------------------------------------------------- close
    def closeEvent(self, ev):
        self.save_state()
        if self.worker:  # stop acquisition first so no frame arrives after the file closes
            self.worker.stop()
        if self.rec_file:
            self.rec_btn.setChecked(False)
        super().closeEvent(ev)


def dark_palette(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    for role, col in [(QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, "#0f151d"),
                      (QPalette.AlternateBase, PANEL), (QPalette.Text, TEXT), (QPalette.Button, "#18212c"),
                      (QPalette.ButtonText, TEXT), (QPalette.Highlight, "#1e3a5f"),
                      (QPalette.HighlightedText, "white"), (QPalette.ToolTipBase, PANEL),
                      (QPalette.ToolTipText, TEXT), (QPalette.PlaceholderText, "#4b5563")]:
        p.setColor(role, QColor(col))
    app.setPalette(p)
