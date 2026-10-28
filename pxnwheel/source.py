"""Live wheel state for the GUI: from the service socket if it is running, else straight from the device."""
import json
import socket

from PySide6.QtCore import QObject, QTimer, Signal
from evdev import ecodes as e

from . import core


class Source(QObject):
    state = Signal(object, object)     # raw abs values, raw keys down
    mode_changed = Signal(str)     # "service" | "direct" | "none"

    def __init__(self):
        super().__init__()
        self.mode = "none"
        self.sock = None
        self.dev = None
        self.buf = b""
        self.abs = {}
        self.keys = set()
        self.codes = []
        self.abs_info = {}
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(20)
        self.retry = 0

    def _set_mode(self, m):
        if m != self.mode:
            self.mode = m
            self.mode_changed.emit(m)

    def _connect(self):
        for path in (core.SYSTEM_SOCKET, core.USER_SOCKET):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(str(path))
                s.setblocking(False)
                self.sock = s
                self._set_mode("service")
                return True
            except OSError:
                s.close()
        self.sock = None
        d = core.find_wheel()
        if d:
            self.dev = d
            caps = d.capabilities(absinfo=True)
            self.abs = {c: i.value for c, i in caps.get(e.EV_ABS, [])}
            self.keys = set(d.active_keys())
            self._set_mode("direct")
            return True
        self._set_mode("none")
        return False

    def send(self, msg):
        """Send a command to the service. Returns False if it isn't running."""
        if not self.sock:
            return False
        try:
            self.sock.send((json.dumps(msg) + "\n").encode())
            return True
        except OSError:
            return False

    def _drop(self):
        for o in (self.sock, self.dev):
            try:
                o and o.close()
            except OSError:
                pass
        self.sock = self.dev = None
        self.buf = b""
        self._set_mode("none")

    def tick(self):
        if self.sock is None and self.dev is None:
            self.retry -= 1
            if self.retry > 0 or not self._connect():
                self.retry = max(self.retry, 50)  # ~1 s between attempts
                return
            self.state.emit(self.abs, self.keys)  # initial state, so a still wheel isn't blank
        changed = False
        try:
            if self.sock:
                data = self.sock.recv(65536)
                if not data:
                    return self._drop()
                self.buf += data
                *lines, self.buf = self.buf.split(b"\n")
                if lines:
                    m = json.loads(lines[-1])
                    self.abs = {int(k): v for k, v in m["abs"].items()}
                    self.keys = set(m["keys"])
                    changed = True
            else:
                for ev in self.dev.read():
                    if ev.type == e.EV_ABS:
                        self.abs[ev.code] = ev.value
                    elif ev.type == e.EV_KEY and ev.value in (0, 1):
                        (self.keys.add if ev.value else self.keys.discard)(ev.code)
                    changed = True
        except BlockingIOError:
            pass
        except (OSError, ValueError):
            return self._drop()
        if changed:
            self.state.emit(self.abs, self.keys)
