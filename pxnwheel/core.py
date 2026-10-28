"""Config model, device discovery and the pure mapping math (no Qt, no uinput)."""
import copy
import json
import os
from pathlib import Path

import evdev
from evdev import ecodes as e

VENDOR = 0x36E6
PRODUCT = 0x410B
VIRTUAL_PHYS = "pxn-wheel-virtual"
VIRTUAL_NAME = "PXN V10 Ultra (pxn-wheel)"

STEER = e.ABS_X
PEDAL_AXES = [e.ABS_Y, e.ABS_Z, e.ABS_RX, e.ABS_RY, e.ABS_RZ, e.ABS_THROTTLE]
STEER_MAX = 32767
PEDAL_MAX = 65535

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pxn-wheel"
CONFIG_PATH = CONFIG_DIR / "config.json"
SYSTEM_SOCKET = Path("/run/pxn-wheel.sock")  # used by the root system service
USER_SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "pxn-wheel.sock"  # manual/dev runs
SOCKET_PATH = SYSTEM_SOCKET if os.geteuid() == 0 else USER_SOCKET

DEFAULT_CONFIG = {
    "steering": {
        "center": 0, "min": -STEER_MAX, "max": STEER_MAX,
        "physical_degrees": 900,   # lock-to-lock rotation of the real wheel
        "range_pct": 100,          # effective range as % of the calibrated range
        "deadzone": 0.0, "gamma": 1.0, "invert": False,
    },
    "axes": {
        str(a): {"label": "", "min": 0, "max": PEDAL_MAX, "dz_low": 0.0, "dz_high": 0.0,
                 "gamma": 1.0, "invert": False, "target": a}
        for a in PEDAL_AXES
    },
    "buttons": {},                 # {"source code": target code, or -1 to disable}
    "ff": {"gain": 100},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "buttons":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=CONFIG_PATH):
    try:
        with open(path) as f:
            return _merge(DEFAULT_CONFIG, json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(cfg, path=CONFIG_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)  # atomic, so the service never reads a half-written file


def find_wheel():
    """Return the real wheel's joystick event device (not our virtual one), or None."""
    for p in evdev.list_devices():
        try:
            d = evdev.InputDevice(p)
        except OSError:
            continue
        caps = d.capabilities()
        if (d.info.vendor, d.info.product) == (VENDOR, PRODUCT) and d.phys != VIRTUAL_PHYS \
                and e.EV_ABS in caps and e.EV_KEY in caps:
            return d
        d.close()
    return None


def button_codes(dev):
    return sorted(dev.capabilities().get(e.EV_KEY, []))


def button_label(codes, code):
    return f"Button {codes.index(code) + 1}" if code in codes else f"Code {code}"


# ---- mapping math -------------------------------------------------------------------

def _shape(n, deadzone, gamma):
    """n in -1..1 -> deadzone removed, then gamma curve applied, sign kept."""
    s = -1.0 if n < 0 else 1.0
    a = abs(n)
    if deadzone > 0:
        a = max(0.0, a - deadzone) / (1.0 - deadzone)
    if gamma != 1.0:
        a = a ** gamma
    return s * min(1.0, a)


def steering_norm(raw, c):
    """Raw steering -> calibrated -1..1 *before* range/deadzone/curve (physical position)."""
    ctr, lo, hi = c["center"], c["min"], c["max"]
    if raw >= ctr:
        span = max(1, hi - ctr)
    else:
        span = max(1, ctr - lo)
    return max(-1.0, min(1.0, (raw - ctr) / span))


def map_steering(raw, c):
    n = steering_norm(raw, c)
    n = max(-1.0, min(1.0, n / max(0.05, c["range_pct"] / 100.0)))
    n = _shape(n, c["deadzone"], c["gamma"])
    if c["invert"]:
        n = -n
    return int(round(n * STEER_MAX))


def map_pedal(raw, c):
    span = max(1, c["max"] - c["min"])
    n = max(0.0, min(1.0, (raw - c["min"]) / span))
    lo, hi = c["dz_low"], c["dz_high"]
    n = max(0.0, min(1.0, (n - lo) / max(0.01, 1.0 - lo - hi)))
    if c["gamma"] != 1.0:
        n = n ** c["gamma"]
    if c["invert"]:
        n = 1.0 - n
    return int(round(n * PEDAL_MAX))


def map_axis(code, raw, cfg):
    """Return (target_code, output_value) for a raw axis event, or None to pass it unchanged."""
    if code == STEER:
        return STEER, map_steering(raw, cfg["steering"])
    a = cfg["axes"].get(str(code))
    if a is None:
        return None
    return int(a["target"]), map_pedal(raw, a)


def map_button(code, cfg):
    """-1 means disabled."""
    return int(cfg["buttons"].get(str(code), code))
