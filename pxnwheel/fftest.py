"""Force-feedback test effects, played straight on the real wheel."""
from evdev import ff, ecodes as e

LEFT, RIGHT = 0x4000, 0xC000
_ENV = ff.Envelope(0, 0, 0, 0)


def _eff(kind, direction, length, u):
    return ff.Effect(kind, -1, direction, ff.Trigger(0, 0), ff.Replay(length, 0), u)


def constant(strength_pct, side):
    lvl = int(32767 * strength_pct / 100)
    return _eff(e.FF_CONSTANT, LEFT if side == "left" else RIGHT, 0,
                ff.EffectType(ff_constant_effect=ff.Constant(lvl, _ENV)))


def condition(kind, strength_pct):
    sat = int(65535 * strength_pct / 100)
    c = ff.Condition(sat, sat, 0x7FFF, 0x7FFF, 0, 0)
    return _eff(kind, 0, 0, ff.EffectType(ff_condition_effect=(ff.Condition * 2)(c, c)))


def sine(strength_pct, period_ms=60):
    mag = int(32767 * strength_pct / 100)
    return _eff(e.FF_PERIODIC, 0x4000, 0, ff.EffectType(
        ff_periodic_effect=ff.Periodic(e.FF_SINE, period_ms, mag, 0, 0, _ENV, 0, None)))


def build(name, strength_pct):
    return {
        "constant_left": lambda: constant(strength_pct, "left"),
        "constant_right": lambda: constant(strength_pct, "right"),
        "spring": lambda: condition(e.FF_SPRING, strength_pct),
        "damper": lambda: condition(e.FF_DAMPER, strength_pct),
        "friction": lambda: condition(e.FF_FRICTION, strength_pct),
        "sine": lambda: sine(strength_pct),
    }[name]()


class Player:
    """Holds at most one running test effect on the wheel."""
    def __init__(self, dev):
        self.dev, self.eid = dev, None

    def play(self, name, strength_pct, gain_pct=100):
        self.stop()
        self.dev.write(e.EV_FF, e.FF_GAIN, int(0xFFFF * gain_pct / 100))
        self.eid = self.dev.upload_effect(build(name, strength_pct))
        self.dev.write(e.EV_FF, self.eid, 1)

    def stop(self):
        if self.eid is not None:
            try:
                self.dev.write(e.EV_FF, self.eid, 0)
                self.dev.erase_effect(self.eid)
            except OSError:
                pass
            self.eid = None
