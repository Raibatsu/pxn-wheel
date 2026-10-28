"""PXN V10 Ultra wheel manager (PySide6)."""
import copy
import json
import math
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
from evdev import ecodes as e

from . import core, fftest
from .source import Source

AXIS_NAMES = {e.ABS_Y: "Axis Y", e.ABS_Z: "Axis Z", e.ABS_RX: "Axis RX",
              e.ABS_RY: "Axis RY", e.ABS_RZ: "Axis RZ", e.ABS_THROTTLE: "Throttle axis"}
ROOT = Path(__file__).resolve().parent.parent
LIB_SCRIPT = "/usr/local/lib/pxn-wheel/service.sh"
INSTALL_SCRIPT = ROOT / "install-service.sh"


class WheelWidget(QWidget):
    """Steering wheel that rotates with the input; ghost wheel = physical, solid = after mapping."""
    def __init__(self):
        super().__init__()
        self.setMinimumSize(200, 200)
        self.phys = 0.0
        self.out = 0.0
        self.deg = 900

    def set_values(self, phys_norm, out_norm, degrees):
        self.phys, self.out, self.deg = phys_norm, out_norm, degrees
        self.update()

    def _draw(self, p, r, angle, color, width):
        p.save()
        p.rotate(angle)
        p.setPen(QPen(color, width))
        p.drawEllipse(QPointF(0, 0), r, r)
        for a in (270, 90, 180):  # spokes: left, right, bottom (T shape)
            p.drawLine(QPointF(0, 0), QPointF(r * math.sin(math.radians(a)), -r * math.cos(math.radians(a))))
        p.fillRect(QRectF(-4, -r - 6, 8, 12), QColor("#e5484d"))  # top marker
        p.restore()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        r = min(self.width(), self.height()) / 2 - 14
        ghost = QColor(self.palette().text().color())
        ghost.setAlpha(60)
        self._draw(p, r, self.phys * self.deg / 2, ghost, 4)
        self._draw(p, r, self.out * self.deg / 2, QColor("#3e8fe0"), 6)


class Card(QGroupBox):
    def __init__(self, title):
        super().__init__(title)
        self.lay = QVBoxLayout(self)


def slider_row(lo, hi, value, scale, fmt, on_change):
    """A slider with a live value label. Returns (container, slider)."""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(int(round(value * scale)))
    lab = QLabel(fmt(value))
    lab.setMinimumWidth(54)
    s.valueChanged.connect(lambda v: (lab.setText(fmt(v / scale)), on_change(v / scale)))
    h.addWidget(s)
    h.addWidget(lab)
    return w, s


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PXN V10 Ultra – Wheel Manager")
        self.resize(980, 720)
        self.cfg = core.load_config()
        self.raw, self.keys = {}, set()
        self.codes = []                      # key codes the wheel reports
        self.capture = {}                    # active min/max captures: key -> [lo, hi]
        self.detect_cb = None                # one-shot "press a button" callback
        self.prev_keys = set()

        self.save_timer = QTimer(singleShot=True, interval=300, timeout=self.save_now)
        self.src = Source()
        self.src.state.connect(self.on_state)
        self.src.mode_changed.connect(self.on_mode)

        self.status = QLabel("Looking for the wheel…")
        self.statusBar().addWidget(self.status)
        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        self.live = self._live_page()
        for name, page in (("Live", self.live), ("Steering", self._steering_page()),
                           ("Pedals and axes", self._axes_page()), ("Buttons", self._buttons_page()),
                           ("Force feedback", self._ff_page()), ("Service and profiles", self._service_page())):
            tabs.addTab(page, name)
        self.refresh_service()
        QTimer(self, interval=2000, timeout=self.refresh_service).start()

    # ---------- config helpers ----------
    def changed(self):
        self.save_timer.start()

    def save_now(self):
        core.save_config(self.cfg)

    def set_cfg(self, d, key, value):
        d[key] = value
        self.changed()

    # ---------- live data ----------
    def on_mode(self, m):
        txt = {"service": "Connected via background service (calibration is active in games)",
               "direct": "Reading the wheel directly (service not running – settings not applied in games yet)",
               "none": "Wheel not found – plug it in"}[m]
        self.status.setText(txt)
        if m == "direct" and self.src.dev:
            self.codes = core.button_codes(self.src.dev)
            self.fill_button_table()
        elif m == "service":
            d = core.find_wheel()
            if d:
                self.codes = core.button_codes(d)
                d.close()
                self.fill_button_table()

    def on_state(self, raw, keys):
        self.raw, self.keys = raw, keys
        pressed = keys - self.prev_keys
        self.prev_keys = set(keys)
        if self.detect_cb and pressed:
            cb, self.detect_cb = self.detect_cb, None
            cb(next(iter(pressed)))
        for k, v in self.capture.items():
            val = raw.get(k[1], 0)
            v[0], v[1] = min(v[0], val), max(v[1], val)
        self.update_live()
        self.update_steering()
        self.update_axes()
        self.update_buttons()

    # ---------- Live ----------
    def _live_page(self):
        w = QWidget()
        g = QGridLayout(w)
        self.live_wheel = WheelWidget()
        g.addWidget(self.live_wheel, 0, 0, 2, 1)
        self.live_steer = QLabel()
        self.live_steer.setAlignment(Qt.AlignCenter)
        g.addWidget(self.live_steer, 2, 0)
        bars = Card("Analog axes (bar = output after calibration)")
        self.live_bars = {}
        for a in core.PEDAL_AXES:
            row = QHBoxLayout()
            lab = QLabel()
            lab.setMinimumWidth(120)
            bar = QProgressBar()
            bar.setRange(0, core.PEDAL_MAX)
            bar.setTextVisible(False)
            row.addWidget(lab)
            row.addWidget(bar)
            bars.lay.addLayout(row)
            self.live_bars[a] = (lab, bar)
        g.addWidget(bars, 0, 1)
        btn = Card("Buttons (lit = pressed)")
        self.live_btn_grid = QGridLayout()
        btn.lay.addLayout(self.live_btn_grid)
        self.live_btn_labels = {}
        self.live_hat = QLabel()
        btn.lay.addWidget(self.live_hat)
        g.addWidget(btn, 1, 1, 2, 1)
        g.setColumnStretch(1, 1)
        return w

    def axis_label(self, a):
        return self.cfg["axes"][str(a)]["label"] or AXIS_NAMES[a]

    def update_live(self):
        s = self.cfg["steering"]
        r = self.raw.get(core.STEER, 0)
        out = core.map_steering(r, s)
        self.live_wheel.set_values(core.steering_norm(r, s), out / core.STEER_MAX,
                                   s["physical_degrees"])
        deg = out / core.STEER_MAX * s["physical_degrees"] / 2
        self.live_steer.setText(f"raw {r}   →   output {out}   ({deg:+.0f}° of {s['physical_degrees']}°)")
        for a, (lab, bar) in self.live_bars.items():
            lab.setText(self.axis_label(a))
            bar.setValue(core.map_pedal(self.raw.get(a, 0), self.cfg["axes"][str(a)]))
        for c, lab in self.live_btn_labels.items():
            on = c in self.keys
            lab.setStyleSheet("background:#3e8fe0;color:white;border-radius:4px;" if on else
                              "background:palette(midlight);border-radius:4px;")
        self.live_hat.setText(f"D-pad: x={self.raw.get(e.ABS_HAT0X, 0)}  y={self.raw.get(e.ABS_HAT0Y, 0)}")

    # ---------- Steering ----------
    def _steering_page(self):
        w = QWidget()
        h = QHBoxLayout(w)
        left = QVBoxLayout()
        h.addLayout(left, 1)
        s = self.cfg["steering"]

        cal = Card("1. Calibrate")
        self.steer_info = QLabel()
        self.steer_info.setWordWrap(True)
        cal.lay.addWidget(QLabel("Center the wheel straight, then press <b>Set center</b>. Then press "
                                 "<b>Capture lock-to-lock</b>, turn fully left, fully right, and press it again."))
        cal.lay.itemAt(0).widget().setWordWrap(True)
        row = QHBoxLayout()
        b_center = QPushButton("Set center here")
        b_center.clicked.connect(self.steer_set_center)
        self.b_cap = QPushButton("Capture lock-to-lock")
        self.b_cap.setCheckable(True)
        self.b_cap.toggled.connect(self.steer_capture)
        b_reset = QPushButton("Reset")
        b_reset.clicked.connect(self.steer_reset)
        for b in (b_center, self.b_cap, b_reset):
            row.addWidget(b)
        cal.lay.addLayout(row)
        cal.lay.addWidget(self.steer_info)
        left.addWidget(cal)

        tune = Card("2. Tune")
        f = QFormLayout()
        tune.lay.addLayout(f)
        self.sp_phys = QSpinBox(minimum=90, maximum=2520, singleStep=10, value=s["physical_degrees"],
                                suffix="° lock-to-lock")
        self.sp_phys.valueChanged.connect(lambda v: (self.set_cfg(s, "physical_degrees", v), self.sync_eff()))
        f.addRow("Wheel's physical rotation", self.sp_phys)
        self.sp_eff = QSpinBox(minimum=90, maximum=2520, singleStep=10, suffix="° lock-to-lock")
        self.sp_eff.valueChanged.connect(self.eff_changed)
        f.addRow("Rotation you want in games", self.sp_eff)
        self.sync_eff()
        w1, _ = slider_row(0, 30, s["deadzone"], 100, lambda v: f"{v * 100:.0f}%",
                           lambda v: self.set_cfg(s, "deadzone", v))
        f.addRow("Center deadzone", w1)
        w2, _ = slider_row(50, 200, s["gamma"], 100, lambda v: f"{v:.2f}",
                           lambda v: self.set_cfg(s, "gamma", v))
        f.addRow("Linearity (1.00 = linear)", w2)
        inv = QCheckBox("Invert steering")
        inv.setChecked(s["invert"])
        inv.toggled.connect(lambda v: self.set_cfg(s, "invert", v))
        f.addRow("", inv)
        note = QLabel("Rotation limiting is software-only: the real wheel still turns to its full lock, "
                      "and force-feedback end-stops come from the game for the <i>original</i> range.")
        note.setWordWrap(True)
        tune.lay.addWidget(note)
        left.addWidget(tune)
        left.addStretch()
        self.steer_wheel = WheelWidget()
        h.addWidget(self.steer_wheel, 1)
        return w

    def sync_eff(self):
        s = self.cfg["steering"]
        self.sp_eff.blockSignals(True)
        self.sp_eff.setValue(round(s["physical_degrees"] * s["range_pct"] / 100))
        self.sp_eff.blockSignals(False)

    def eff_changed(self, v):
        s = self.cfg["steering"]
        self.set_cfg(s, "range_pct", max(5.0, min(100.0, v / s["physical_degrees"] * 100)))

    def steer_set_center(self):
        self.set_cfg(self.cfg["steering"], "center", int(self.raw.get(core.STEER, 0)))

    def steer_capture(self, on):
        key = ("steer", core.STEER)
        if on:
            r = self.raw.get(core.STEER, 0)
            self.capture[key] = [r, r]
            self.b_cap.setText("Turn lock-to-lock, then press again to finish")
        else:
            lo, hi = self.capture.pop(key, [0, 0])
            self.b_cap.setText("Capture lock-to-lock")
            if hi - lo > 2000:
                s = self.cfg["steering"]
                s["min"], s["max"] = int(lo), int(hi)
                self.changed()
            else:
                QMessageBox.warning(self, "Capture", "Barely any movement detected – turn the wheel "
                                    "fully left and right while capturing.")

    def steer_reset(self):
        s = self.cfg["steering"]
        s.update(center=0, min=-core.STEER_MAX, max=core.STEER_MAX)
        self.changed()

    def update_steering(self):
        s = self.cfg["steering"]
        r = self.raw.get(core.STEER, 0)
        cap = self.capture.get(("steer", core.STEER))
        extra = f"   (capturing: {cap[0]} … {cap[1]})" if cap else ""
        self.steer_info.setText(f"Raw now: {r}   Center: {s['center']}   Min: {s['min']}   Max: {s['max']}{extra}")
        self.steer_wheel.set_values(core.steering_norm(r, s), core.map_steering(r, s) / core.STEER_MAX,
                                    s["physical_degrees"])

    # ---------- Pedals & axes ----------
    def _axes_page(self):
        inner = QWidget()
        v = QVBoxLayout(inner)
        self.ax_widgets = {}
        for a in core.PEDAL_AXES:
            c = self.cfg["axes"][str(a)]
            box = Card(AXIS_NAMES[a])
            top = QHBoxLayout()
            label = QLineEdit(c["label"])
            label.setPlaceholderText("Name (e.g. Throttle, Brake, Clutch)")
            label.textChanged.connect(lambda t, c=c: self.set_cfg(c, "label", t))
            top.addWidget(label, 1)
            target = QComboBox()
            for t in core.PEDAL_AXES:
                target.addItem(f"Send as {AXIS_NAMES[t]}", t)
            target.setCurrentIndex(core.PEDAL_AXES.index(c["target"]))
            target.currentIndexChanged.connect(lambda i, c=c, t=target: self.set_cfg(c, "target", t.itemData(i)))
            top.addWidget(target)
            cap = QPushButton("Calibrate (press fully, release)")
            cap.setCheckable(True)
            cap.toggled.connect(lambda on, a=a, b=cap: self.axis_capture(a, on, b))
            top.addWidget(cap)
            box.lay.addLayout(top)
            bars = QHBoxLayout()
            raw_bar, out_bar = QProgressBar(), QProgressBar()
            for b, t in ((raw_bar, "raw"), (out_bar, "output")):
                b.setRange(0, core.PEDAL_MAX)
                b.setFormat(f"{t} %v")
                bars.addWidget(b)
            box.lay.addLayout(bars)
            opts = QHBoxLayout()
            inv = QCheckBox("Invert")
            inv.setChecked(c["invert"])
            inv.toggled.connect(lambda x, c=c: self.set_cfg(c, "invert", x))
            opts.addWidget(inv)
            for lbl, key, mx in (("Dead zone start", "dz_low", 0.4), ("end", "dz_high", 0.4)):
                sp = QDoubleSpinBox(minimum=0, maximum=mx, singleStep=0.01, decimals=2, value=c[key])
                sp.valueChanged.connect(lambda x, c=c, k=key: self.set_cfg(c, k, x))
                opts.addWidget(QLabel(lbl))
                opts.addWidget(sp)
            gm = QDoubleSpinBox(minimum=0.3, maximum=3, singleStep=0.05, decimals=2, value=c["gamma"])
            gm.valueChanged.connect(lambda x, c=c: self.set_cfg(c, "gamma", x))
            opts.addWidget(QLabel("Curve"))
            opts.addWidget(gm)
            opts.addStretch()
            rng = QLabel()
            opts.addWidget(rng)
            box.lay.addLayout(opts)
            v.addWidget(box)
            self.ax_widgets[a] = (box, raw_bar, out_bar, rng, label)
        v.addStretch()
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(inner)
        return sc

    def axis_capture(self, a, on, btn):
        key = ("axis", a)
        if on:
            r = self.raw.get(a, 0)
            self.capture[key] = [r, r]
            btn.setText("Press fully & release, then click again")
        else:
            lo, hi = self.capture.pop(key, [0, 0])
            btn.setText("Calibrate (press fully, release)")
            if hi - lo > 500:
                c = self.cfg["axes"][str(a)]
                c["min"], c["max"] = int(lo), int(hi)
                self.changed()
            else:
                QMessageBox.warning(self, "Calibrate", "No movement detected on this axis.")

    def update_axes(self):
        for a, (box, rb, ob, rng, _) in self.ax_widgets.items():
            c = self.cfg["axes"][str(a)]
            r = self.raw.get(a, 0)
            rb.setValue(max(0, min(core.PEDAL_MAX, r)))
            ob.setValue(core.map_pedal(r, c))
            cap = self.capture.get(("axis", a))
            rng.setText(f"range {cap[0]}…{cap[1]} (capturing)" if cap else f"range {c['min']}…{c['max']}")

    # ---------- Buttons ----------
    def _buttons_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QHBoxLayout()
        self.b_detect = QPushButton("Detect: press a button on the wheel")
        self.b_detect.clicked.connect(self.start_detect)
        b_reset = QPushButton("Reset all remaps")
        b_reset.clicked.connect(lambda: (self.cfg["buttons"].clear(), self.changed(), self.fill_button_table()))
        top.addWidget(self.b_detect)
        top.addWidget(b_reset)
        top.addStretch()
        v.addLayout(top)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Physical button", "Pressed", "Acts as"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 70)
        self.table.verticalHeader().hide()
        v.addWidget(self.table)
        v.addWidget(QLabel("Remaps apply to games only while the background service is running."))
        return w

    def fill_button_table(self):
        self.table.setRowCount(len(self.codes))
        while self.live_btn_grid.count():
            self.live_btn_grid.takeAt(0).widget().deleteLater()
        self.live_btn_labels = {}
        for i, c in enumerate(self.codes):
            it = QTableWidgetItem(f"{core.button_label(self.codes, c)}   (code {c})")
            it.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(i, 0, it)
            ind = QTableWidgetItem("")
            ind.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(i, 1, ind)
            combo = QComboBox()
            combo.addItem("(default)", c)
            combo.addItem("Disabled", -1)
            for t in self.codes:
                combo.addItem(core.button_label(self.codes, t), t)
            cur = self.cfg["buttons"].get(str(c))
            if cur is not None:
                combo.setCurrentIndex(combo.findData(cur))
            combo.currentIndexChanged.connect(lambda _, c=c, cb=combo: self.set_button(c, cb.currentData()))
            self.table.setCellWidget(i, 2, combo)
            lab = QLabel(str(i + 1))
            lab.setAlignment(Qt.AlignCenter)
            lab.setMinimumSize(30, 22)
            self.live_btn_grid.addWidget(lab, i // 12, i % 12)
            self.live_btn_labels[c] = lab

    def set_button(self, src, tgt):
        if tgt == src:
            self.cfg["buttons"].pop(str(src), None)
        else:
            self.cfg["buttons"][str(src)] = tgt
        self.changed()

    def start_detect(self):
        self.b_detect.setText("Waiting for a button press…")
        self.detect_cb = self.detected

    def detected(self, code):
        self.b_detect.setText("Detect: press a button on the wheel")
        if code in self.codes:
            row = self.codes.index(code)
            self.table.selectRow(row)
            self.table.scrollToItem(self.table.item(row, 0))

    def update_buttons(self):
        for i, c in enumerate(self.codes):
            it = self.table.item(i, 1)
            if it:
                it.setText("●" if c in self.keys else "")

    # ---------- Force feedback ----------
    def _ff_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        g = Card("Overall strength")
        wd, _ = slider_row(0, 100, self.cfg["ff"]["gain"], 1, lambda x: f"{x:.0f}%", self.set_gain)
        g.lay.addWidget(wd)
        g.lay.addWidget(QLabel("Scales every effect games send (needs the background service)."))
        v.addWidget(g)
        t = Card("Test effects  –  HOLD THE WHEEL FIRMLY")
        wd2, self.test_strength = slider_row(5, 100, 30, 1, lambda x: f"{x:.0f}%", lambda _: None)
        t.lay.addWidget(QLabel("Test strength:"))
        t.lay.addWidget(wd2)
        row = QHBoxLayout()
        for label, name in (("Pull left", "constant_left"), ("Pull right", "constant_right"),
                            ("Spring (centering)", "spring"), ("Damper", "damper"),
                            ("Friction", "friction"), ("Vibration", "sine")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, n=name: self.ff_play(n))
            row.addWidget(b)
        t.lay.addLayout(row)
        stop = QPushButton("STOP")
        stop.setStyleSheet("background:#e5484d;color:white;font-weight:bold;padding:8px;")
        stop.clicked.connect(self.ff_stop)
        t.lay.addWidget(stop)
        v.addWidget(t)
        self.ff_msg = QLabel()
        v.addWidget(self.ff_msg)
        v.addStretch()
        self.ff_dev = self.ff_player = None
        return w

    def set_gain(self, v):
        self.set_cfg(self.cfg["ff"], "gain", int(v))

    def ff_open(self):
        if self.ff_dev is None:
            self.ff_dev = core.find_wheel()
            if self.ff_dev:
                self.ff_player = fftest.Player(self.ff_dev)
        return self.ff_player

    def ff_play(self, name):
        strength = int(self.test_strength.value())
        if self.src.mode == "service":
            ok = self.src.send({"cmd": "ff_play", "name": name, "strength": strength, "gain": self.cfg["ff"]["gain"]})
            self.ff_msg.setText(f"Playing: {name} via service (press STOP to end)" if ok else "Service connection lost.")
            return
        p = self.ff_open()
        if not p:
            self.ff_msg.setText("Wheel not found.")
            return
        try:
            p.play(name, strength, self.cfg["ff"]["gain"])
            self.ff_msg.setText(f"Playing: {name}  (press STOP to end)")
        except OSError as err:
            self.ff_msg.setText(f"Could not play effect: {err}")
            self.ff_dev = self.ff_player = None

    def ff_stop(self):
        if self.src.mode == "service":
            self.src.send({"cmd": "ff_stop"})
        elif self.ff_player:
            self.ff_player.stop()
        self.ff_msg.setText("Stopped.")

    # ---------- Service & profiles ----------
    def _service_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        sv = Card("Background service (applies everything above to games)")
        self.svc_label = QLabel()
        sv.lay.addWidget(self.svc_label)
        row = QHBoxLayout()
        for text, fn in (("Install && start at boot", self.svc_install), ("Start", lambda: self.svc("start")),
                         ("Stop", lambda: self.svc("stop")), ("Disable autostart", lambda: self.svc("disable"))):
            b = QPushButton(text.replace("&&", "&"))
            b.clicked.connect(fn)
            row.addWidget(b)
        sv.lay.addLayout(row)
        sv.lay.addWidget(QLabel("While the service runs, games should use the device named "
                                f"“{core.VIRTUAL_NAME}”. While it runs, the real wheel is hidden from games on purpose."))
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(300)
        sv.lay.addWidget(self.log)
        v.addWidget(sv, 1)
        pf = Card("Profiles")
        row = QHBoxLayout()
        for text, fn in (("Export settings…", self.export_cfg), ("Import settings…", self.import_cfg),
                         ("Reset everything", self.reset_cfg)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        pf.lay.addLayout(row)
        v.addWidget(pf)
        return w

    def svc(self, action):
        """Run a service action as root via pkexec (asks for your password)."""
        if not Path(LIB_SCRIPT).exists():
            return QMessageBox.information(self, "Service", "Install the service first.")
        self.run_root(LIB_SCRIPT, action)

    def svc_install(self):
        core.save_config(self.cfg)
        self.run_root(str(INSTALL_SCRIPT), "install")

    def run_root(self, script, action):
        r = subprocess.run(["pkexec", "/bin/sh", script, action], capture_output=True, text=True)
        if r.returncode:
            QMessageBox.warning(self, "Service", (r.stderr or r.stdout or "cancelled").strip() +
                                f"\n\nYou can also run:  sudo sh {script} {action}")
        elif action in ("install", "start", "restart"):
            QMessageBox.information(self, "Service", "Done. Restart Steam and your game once so they pick up the "
                                    "virtual wheel instead of the raw one.")
        self.refresh_service()

    def refresh_service(self):
        r = subprocess.run(["systemctl", "is-active", "pxn-wheeld.service"], capture_output=True, text=True)
        en = subprocess.run(["systemctl", "is-enabled", "pxn-wheeld.service"], capture_output=True, text=True)
        self.svc_label.setText(f"Status: <b>{r.stdout.strip() or 'not installed'}</b>   |   "
                               f"Autostart: <b>{en.stdout.strip() or 'not installed'}</b>")
        j = subprocess.run(["journalctl", "-u", "pxn-wheeld.service", "-n", "30", "--no-pager", "-o", "cat"],
                           capture_output=True, text=True)
        self.log.setPlainText(j.stdout)

    def export_cfg(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export settings", "pxn-wheel-profile.json", "JSON (*.json)")
        if p:
            core.save_config(self.cfg, p)

    def import_cfg(self):
        p, _ = QFileDialog.getOpenFileName(self, "Import settings", "", "JSON (*.json)")
        if p:
            self.cfg_replace(core.load_config(p))

    def reset_cfg(self):
        if QMessageBox.question(self, "Reset", "Reset all calibration and remaps?") == QMessageBox.Yes:
            self.cfg_replace(copy.deepcopy(core.DEFAULT_CONFIG))

    def cfg_replace(self, cfg):
        core.save_config(cfg)
        QMessageBox.information(self, "Settings replaced", "Settings loaded. The window will restart to show them.")
        self.close()
        subprocess.Popen([sys.executable, "-m", "pxnwheel.gui"], cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})

    def closeEvent(self, ev):
        if self.save_timer.isActive():
            self.save_now()
        self.ff_stop()
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    w = App()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
