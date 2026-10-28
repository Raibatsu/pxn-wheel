"""Background service: grabs the real wheel, applies calibration/remaps, re-exposes it as a
virtual uinput wheel, forwards force feedback to the real one, and streams raw state to the GUI."""
import grp
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import evdev
from evdev import AbsInfo, UInput, ecodes as e

from . import core, fftest

EV_UINPUT = 0x0101
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2
FF_GAIN = 0x60
BROADCAST_HZ = 40


def log(msg):
    print(msg, flush=True)


def device_nodes(real):
    """Every /dev node of the physical wheel: evdev, js, and the hidraw of each USB interface."""
    inp = (Path("/sys/class/input") / Path(real.path).name / "device").resolve()
    nodes = {real.path}
    nodes |= {f"/dev/input/{p.name}" for p in inp.glob("js*")}
    usb = inp
    while usb != usb.parent and not (usb / "idVendor").exists():
        usb = usb.parent
    if (usb / "idVendor").exists():
        nodes |= {f"/dev/{p.name}" for p in usb.glob("*/*/hidraw/hidraw*")}
    return sorted(n for n in nodes if os.path.exists(n))


def hide_nodes(nodes):
    """Make the real wheel unopenable for normal users so games can only pick the virtual one.
    Handles that are already open keep working (so Steam must be restarted once)."""
    if os.geteuid() != 0:
        log("not root: cannot hide the real wheel from games")
        return
    for n in nodes:
        subprocess.run(["setfacl", "-b", n], capture_output=True)
        os.chown(n, 0, 0)
        os.chmod(n, 0o600)
    log(f"hid {len(nodes)} real wheel nodes from games")


def restore_nodes(nodes):
    """Let udev re-apply the normal permissions."""
    if os.geteuid() != 0:
        return
    for n in nodes:
        subprocess.run(["udevadm", "trigger", "--action=change", f"--name-match={n}"], capture_output=True)
    subprocess.run(["udevadm", "settle", "--timeout=3"], capture_output=True)


class Bridge:
    def __init__(self, real):
        self.real = real
        self.cfg = core.load_config()
        self.cfg_mtime = self._mtime()
        self.idmap = {}            # virtual effect id -> real effect id
        self.raw = {}              # raw abs values
        self.keys = set()          # raw keys currently down
        self.clients = []
        self.last_bc = 0.0
        self.player = fftest.Player(real)
        self.nodes = device_nodes(real)

        caps = real.capabilities(absinfo=True)
        keys = caps.get(e.EV_KEY, [])
        abs_caps = [(c, i) for c, i in caps.get(e.EV_ABS, [])]
        ff = caps.get(e.EV_FF, [])
        self.codes = keys
        self.ui = UInput(
            {e.EV_KEY: keys, e.EV_ABS: abs_caps, e.EV_FF: ff},
            name=core.VIRTUAL_NAME, vendor=core.VENDOR, product=core.PRODUCT,
            version=real.info.version, bustype=real.info.bustype, phys=core.VIRTUAL_PHYS,
            max_effects=max(16, real.ff_effects_count),
        )
        for code, info in abs_caps:
            self.raw[code] = info.value
        for k in real.active_keys():
            self.keys.add(k)
        self.set_gain()
        self.push_full_state()
        hide_nodes(self.nodes)

    # -- config -------------------------------------------------------------------
    def _mtime(self):
        try:
            return core.CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            return 0

    def maybe_reload(self):
        m = self._mtime()
        if m != self.cfg_mtime:
            self.cfg_mtime = m
            self.cfg = core.load_config()
            self.set_gain()
            self.push_full_state()
            log("config reloaded")

    def set_gain(self):
        try:
            self.real.write(e.EV_FF, FF_GAIN, int(0xFFFF * self.cfg["ff"]["gain"] / 100))
        except OSError:
            pass

    # -- input path ---------------------------------------------------------------
    def push_full_state(self):
        for code, val in self.raw.items():
            self.emit_abs(code, val)
        out_down = {core.map_button(k, self.cfg) for k in self.keys}
        for k in self.codes:
            self.ui.write(e.EV_KEY, k, 1 if k in out_down else 0)
        self.ui.syn()

    def emit_abs(self, code, val):
        m = core.map_axis(code, val, self.cfg)
        if m is None:
            self.ui.write(e.EV_ABS, code, val)
        else:
            self.ui.write(e.EV_ABS, m[0], m[1])

    def on_real_event(self, ev):
        if ev.type == e.EV_ABS:
            self.raw[ev.code] = ev.value
            self.emit_abs(ev.code, ev.value)
        elif ev.type == e.EV_KEY and ev.value in (0, 1):
            (self.keys.add if ev.value else self.keys.discard)(ev.code)
            tgt = core.map_button(ev.code, self.cfg)
            if tgt >= 0:
                self.ui.write(e.EV_KEY, tgt, ev.value)
        elif ev.type == e.EV_SYN:
            self.ui.syn()

    # -- force feedback path (game -> virtual device -> real wheel) -----------------
    def on_ui_event(self, ev):
        if ev.type == EV_UINPUT and ev.code == UI_FF_UPLOAD:
            up = self.ui.begin_upload(ev.value)
            vid = up.effect.id
            up.effect.id = self.idmap.get(vid, -1)
            try:
                rid = self.real.upload_effect(up.effect, writeback_id=False)
                self.idmap[vid] = rid
                up.retval = 0
            except OSError as err:
                up.retval = -err.errno
            up.effect.id = vid
            self.ui.end_upload(up)
        elif ev.type == EV_UINPUT and ev.code == UI_FF_ERASE:
            er = self.ui.begin_erase(ev.value)
            rid = self.idmap.pop(er.effect_id, None)
            er.retval = 0
            if rid is not None:
                try:
                    self.real.erase_effect(rid)
                except OSError:
                    pass
            self.ui.end_erase(er)
        elif ev.type == e.EV_FF:
            try:
                if ev.code == FF_GAIN:
                    self.real.write(e.EV_FF, FF_GAIN, ev.value * self.cfg["ff"]["gain"] // 100)
                elif ev.code in self.idmap:
                    self.real.write(e.EV_FF, self.idmap[ev.code], ev.value)
                elif ev.code == 0x61:  # FF_AUTOCENTER
                    self.real.write(e.EV_FF, ev.code, ev.value)
            except OSError:
                pass

    # -- GUI socket -----------------------------------------------------------------
    def broadcast(self):
        now = time.monotonic()
        if now - self.last_bc < 1.0 / BROADCAST_HZ or not self.clients:
            return
        self.last_bc = now
        line = (json.dumps({"abs": self.raw, "keys": sorted(self.keys)}) + "\n").encode()
        for c in list(self.clients):
            try:
                c.send(line)
            except (BlockingIOError, OSError):
                self.clients.remove(c)
                self.on_client_gone(c)

    on_client_gone = staticmethod(lambda c: c.close())

    def command(self, msg):
        """Commands from the GUI (force-feedback tests run on the wheel we exclusively own)."""
        try:
            if msg.get("cmd") == "ff_play":
                self.player.play(msg["name"], int(msg["strength"]), int(msg.get("gain", 100)))
            elif msg.get("cmd") == "ff_stop":
                self.player.stop()
        except (OSError, KeyError, ValueError) as err:
            log(f"command failed: {err!r}")

    def close(self):
        self.player.stop()
        restore_nodes(self.nodes)
        for c in self.clients:
            c.close()
        try:
            self.ui.close()
        except Exception:
            pass


def _unreg(sel, fd):
    try:
        sel.unregister(fd)
    except (KeyError, ValueError):
        pass


def serve(stop):
    sel = selectors.DefaultSelector()
    core.SOCKET_PATH.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(core.SOCKET_PATH))
    srv.listen(4)
    srv.setblocking(False)
    try:  # the GUI runs as a normal user in group "input"
        os.chown(core.SOCKET_PATH, 0, grp.getgrnam("input").gr_gid)
        os.chmod(core.SOCKET_PATH, 0o660)
    except (KeyError, PermissionError, OSError):
        os.chmod(core.SOCKET_PATH, 0o666)
    sel.register(srv, selectors.EVENT_READ, "srv")
    bridge = None
    real = None
    try:
        while not stop["flag"]:
            if bridge is None:
                real = core.find_wheel()
                if real is None:
                    time.sleep(1.5)
                    continue
                try:
                    real.grab()
                    bridge = Bridge(real)
                except Exception as err:  # never leave the wheel grabbed on a failed start
                    log(f"cannot start: {err!r}")
                    try:
                        real.ungrab()
                    except OSError:
                        pass
                    real.close()
                    time.sleep(2)
                    continue
                sel.register(real, selectors.EVENT_READ, "real")
                sel.register(bridge.ui, selectors.EVENT_READ, "ui")
                bridge.on_client_gone = lambda c: (_unreg(sel, c), c.close())
                log(f"bridging {real.path} -> {bridge.ui.device.path}")
            try:
                for key, _ in sel.select(timeout=0.25):
                    if key.data == "srv":
                        c, _ = srv.accept()
                        c.setblocking(False)
                        if bridge:
                            bridge.clients.append(c)
                            sel.register(c, selectors.EVENT_READ, "cli")
                        else:
                            c.close()
                    elif key.data == "cli":
                        c = key.fileobj
                        try:
                            data = c.recv(4096)
                        except BlockingIOError:
                            continue
                        except OSError:
                            data = b""
                        if not data:  # GUI went away: stop any running test effect
                            sel.unregister(c)
                            if c in bridge.clients:
                                bridge.clients.remove(c)
                            c.close()
                            bridge.player.stop()
                            continue
                        for line in data.splitlines():
                            try:
                                bridge.command(json.loads(line))
                            except ValueError:
                                pass
                    elif key.data == "real":
                        for ev in real.read():
                            bridge.on_real_event(ev)
                    elif key.data == "ui":
                        while (ev := bridge.ui.read_one()) is not None:
                            bridge.on_ui_event(ev)
                bridge.maybe_reload()
                bridge.broadcast()
            except OSError as err:  # wheel unplugged
                log(f"wheel lost ({err}); waiting for it to come back")
                for fd in (real, bridge.ui, *bridge.clients):
                    _unreg(sel, fd)
                bridge.close()
                real.close()
                bridge = None
    finally:
        if bridge:
            try:
                real.ungrab()
            except OSError:
                pass
            bridge.close()
        srv.close()
        core.SOCKET_PATH.unlink(missing_ok=True)


def main():
    stop = {"flag": False}
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.__setitem__("flag", True))
    log("pxn-wheeld starting")
    serve(stop)
    log("pxn-wheeld stopped")


if __name__ == "__main__":
    main()
