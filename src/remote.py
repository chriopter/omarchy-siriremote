#!/usr/bin/env python3
"""Apple Siri Remote gen 3: raw BLE input, uinput mouse and keys, Voxtype dictation.

Uses only the Python standard library; voice decoding loads the system libopus.
The Omarchy plugin reads the live state from $XDG_RUNTIME_DIR/omarchy-siriremote/state.json
and writes touch settings to $SIRI_REMOTE_DATA/settings.json.
"""
import ctypes
import fcntl
import glob
import math
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

# --- Paths, live state and HID report layout ---

DATA = os.path.expanduser(os.environ.get("SIRI_REMOTE_DATA", "~/.local/share/omarchy-siriremote"))
RUN = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "omarchy-siriremote")
os.makedirs(DATA, exist_ok=True)
os.makedirs(RUN, exist_ok=True)
STATE = {"t": 0, "menu": None, "searching": False, "connected": False, "percent": None, "charging": False, "full": False, "usb": False, "listening": False}
state_lock = threading.Lock()
DEBUG = os.environ.get("SIRI_REMOTE_DEBUG", "")   # "1": touch report statistics in the journal, "raw": every report


def publish(**changes):
    """Write the live state for the plugin, atomically and only when something changed."""
    with state_lock:
        if all(STATE.get(k) == v for k, v in changes.items()):
            return
        STATE.update(changes)
        tmp = os.path.join(RUN, "state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(STATE, f)
        os.replace(tmp, os.path.join(RUN, "state.json"))


def stop(*_):
    """No driver, no state: the plugin then shows the driver as stopped."""
    if MIC:
        MIC.cleanup()
    try:
        os.unlink(os.path.join(RUN, "state.json"))
    except OSError:
        pass
    sys.exit(0)

# Report 0xFB (from the HID descriptor): bits in order
BTN_BITS = ["tv", "vol_up", "vol_down", "select", "power", "siri",     # byte1 bits0-5 (consumer 0x40 = TV/home)
            "menu",                                                      # 0x86 generic desktop = Zurück
            "mute", "play_pause", "up", "right", "down", "left"]        # consumer 0xE2,0xCD,0x42 up,0x45 right,0x43 down,0x44 left


def remotes():
    """List matching hidraw devices and their transport."""
    out = []
    for d in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            ue = open(d + "/device/uevent").read()
        except OSError:
            continue
        if ":00000315" in ue and ("000005AC" in ue or "0000004C" in ue):
            name = [l.split("=", 1)[1] for l in ue.splitlines() if l.startswith("HID_ID=")][0]
            bus = "usb" if name.startswith("0003") else "bluetooth" if name.startswith("0005") else name
            out.append(("/dev/" + os.path.basename(d), bus))
    return out


# --- Virtual mouse, keyboard and desktop navigation ---

def mpris(*args, t=2):
    """Run busctl against the user bus, returning its output ('' on failure)."""
    try:
        return subprocess.run(["busctl", "--user", *args], capture_output=True, text=True, timeout=t).stdout
    except Exception:
        return ""


PLAYER = None   # the MPRIS player the play button last acted on


def media(taps):
    """Play button through MPRIS: 1 = play/pause, 2 = next track and play. The player that is
    playing gets it, else the first one; without any player, the media keys instead."""
    names = [l.split()[0] for l in mpris("list", "--no-pager").splitlines()
             if l.startswith("org.mpris.MediaPlayer2.")]
    path, iface = "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player"

    def status(n):
        return mpris("get-property", n, path, iface, "PlaybackStatus").strip()   # s "Playing"

    if not names:
        if MOUSE:
            MOUSE.combo([164] if taps == 1 else [163])
        return
    global PLAYER
    if taps == 2 and PLAYER in names:        # the one the first press just paused
        player = PLAYER
    else:
        player = PLAYER = next((n for n in names if "Playing" in status(n)),
                               PLAYER if PLAYER in names else names[0])
    if taps == 1:
        mpris("call", player, path, iface, "PlayPause")
        return
    mpris("call", player, path, iface, "Next")
    time.sleep(0.3)
    if "Playing" not in status(player):
        mpris("call", player, path, iface, "Play")


class Menu:
    """Settings menu, opened by holding Power and shown by the bar plugin in the middle of the
    screen: ring ↑/↓ choose, ←/→ change, Back, TV or Power held again close it (and it closes
    itself after a while).
    The driver owns the logic and publishes the rows in state.json as menu."""
    KEYS = ("scale", "output", "input")
    IDLE_S = 15

    def __init__(self):
        self.open, self.sel, self.t = False, 0, 0.0
        threading.Thread(target=self._idle, daemon=True).start()

    def _idle(self):
        while True:
            time.sleep(1)
            if self.open and time.time() - self.t > self.IDLE_S:
                self.close()

    @staticmethod
    def _sh(*cmd):
        try:
            return subprocess.run(list(cmd), capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return ""

    def _devices(self, kind):
        """(name, description) of the sinks or sources, and the default one's name."""
        try:
            devs = json.loads(self._sh("pactl", "-f", "json", "list", kind) or "[]")
        except ValueError:
            devs = []
        devs = [(d["name"], d.get("description") or d["name"]) for d in devs
                if not d["name"].endswith(".monitor") and d["name"] != Mic.NAME]
        default = self._sh("pactl", "get-default-sink" if kind == "sinks" else "get-default-source").strip()
        return devs, default

    def value(self, key):
        if key == "scale":
            try:
                mon = next(m for m in json.loads(self._sh("hyprctl", "monitors", "-j") or "[]") if m["focused"])
                return f"{mon['scale']:g}×"
            except (StopIteration, ValueError, KeyError):
                return "?"
        devs, default = self._devices("sinks" if key == "output" else "sources")
        return next((d for n, d in devs if n == default), default or "?")

    def change(self, key, step):
        if key == "scale":
            self._sh("omarchy-hyprland-monitor-scaling", "up" if step > 0 else "down")
            return
        kind = "sinks" if key == "output" else "sources"
        devs, default = self._devices(kind)
        if not devs:
            return
        names = [n for n, _ in devs]
        nxt = names[(names.index(default) + step) % len(names)] if default in names else names[0]
        if kind == "sinks":
            self._sh("pactl", "set-default-sink", nxt)
            for line in self._sh("pactl", "list", "short", "sink-inputs").splitlines():
                self._sh("pactl", "move-sink-input", line.split()[0], nxt)
        else:
            self._sh("pactl", "set-default-source", nxt)

    def show(self):
        rows = [{"key": k, "value": self.value(k)} for k in self.KEYS]
        publish(menu={"open": self.open, "sel": self.sel, "rows": rows} if self.open else None)

    def toggle(self):
        if self.open:
            self.close()
        else:
            self.open, self.sel, self.t = True, 0, time.time()
            self.show()

    def close(self):
        self.open = False
        publish(menu=None)

    def key(self, button):
        """A ring direction while the menu is open."""
        self.t = time.time()
        if button in ("up", "down"):
            self.sel = (self.sel + (1 if button == "down" else -1)) % len(self.KEYS)
        else:
            self.change(self.KEYS[self.sel], 1 if button == "right" else -1)
        self.show()


MENU = Menu()


class VMouse:
    """Virtual mouse via /dev/uinput: touch surface -> relative motion, ring centre click -> left click."""
    EV_SYN, EV_KEY, EV_REL = 0, 1, 2
    BTN_LEFT, REL_X, REL_Y = 0x110, 0, 1
    # remote button -> linux key code (input-event-codes.h)
    KEYMAP = {"menu": 1, "up": 103, "down": 108, "left": 105, "right": 106,   # Esc, arrows
              "vol_up": 115, "vol_down": 114, "mute": 113}                            # media keys
    # play/pause: 1x play/pause, 2x next track
    ARROWS = ("up", "down", "left", "right")

    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, 0x40045564, self.EV_KEY); fcntl.ioctl(self.fd, 0x40045565, self.BTN_LEFT)
        for code in set(self.KEYMAP.values()) | {125, 15, 56, 28, 29, 164, 163, 165}:   # + Super, Tab, Alt, Enter (arrows already in)
            fcntl.ioctl(self.fd, 0x40045565, code)
        fcntl.ioctl(self.fd, 0x40045564, 0x14)        # EV_REP: kernel auto-repeat while a key is held
        fcntl.ioctl(self.fd, 0x40045564, self.EV_REL)
        fcntl.ioctl(self.fd, 0x40045566, self.REL_X); fcntl.ioctl(self.fd, 0x40045566, self.REL_Y)
        setup = struct.pack("<HHHH80sI", 0x06, 0x004C, 0x0315, 1, b"Siri Remote Touch Mouse", 0)
        fcntl.ioctl(self.fd, 0x405c5503, setup); fcntl.ioctl(self.fd, 0x5501)
        self.last, self.fx, self.fy, self.pressed = None, 0.0, 0.0, False
        self.swap, self.invert_x, self.invert_y = False, False, True
        self.gain, self.k, self.cap, self.noise, self.touch_min = 1.9, 0.005, 1.6, 1, 150
        self.settle_frames, self.settle, self.jump_max, self.touch_keep = 2, 0, 300, 125
        self.tap_max, self.multi_s, self.hold_s = 0.35, 0.4, 0.5
        self.pend, self.a9, self.pend_max = None, [], 0.08
        self.land_s, self.lift_drop, self.land_t, self.cmax = 0.045, 15, 0.0, 0
        self.precision, self.slow_v = 0.45, 25.0
        self.roll, self.size_now, self.size_prev = 6.0, 0, 0
        # see touch(): ignoring the remote's own trailing creep after the finger stops
        self.coast_ratio, self.coast_exit, self.coast_max = 0.3, 1.5, 0.4
        self.peak = self.vprev = self.coast_v = self.coast_t = 0.0; self.dev_t = 0
        self.guard_until = 0.0
        self.burst_t, self.burst_dt = 0.0, 0.045   # arrival of the last BLE burst, EMA of their spacing (stats)
        # playback buffer, see _pump(): each sample's motion is replayed on the remote's own clock
        self.smooth = 0.005                        # seconds the pointer runs behind the finger, see touch()
        self.seg_end, self.segs = 0.0, []
        self.glide, self.qx, self.qy = 0.004, 0.0, 0.0 # low-pass time constant (s) after playback
        self.lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def _ev(self, t, c, v):
        os.write(self.fd, struct.pack("<qqHHi", 0, 0, t, c, v))

    def reload(self):
        """Apply settings when the data file changes; preserve all existing keys."""
        path = os.path.join(DATA, "settings.json")
        try:
            mt = os.path.getmtime(path)
            if mt != getattr(self, "_mt", None):
                self._mt = mt
                data = json.load(open(path))
                for k, v in data.items():
                    setattr(self, k, v)
                if "smooth" in data and "glide" not in data:   # the card's one smoothing slider
                    self.glide = 0.75 * self.smooth
        except (OSError, ValueError):
            pass

    def touch(self, b):
        """Convert packed signed 12-bit touch coordinates into filtered motion."""
        if len(b) < 10:
            return
        if self.last is None:
            self.reload()
        # hysteresis: a touch starts at touch_min but only ends below touch_keep, so contact
        # pressure dipping mid-swipe does not break the stroke. A finger hovering just above the
        # surface reads about 95-135 (up to ~145 while landing), one resting on it 150-200.
        if b[8] < (self.touch_keep if self.last is not None else self.touch_min):
            if DEBUG == "raw":
                print(f"raw {time.time() % 100:8.3f} below c={b[8]:3d} {bytes(b).hex()}", flush=True)
            if self.last is not None:            # finger lifted: stop now, drop what is still queued
                with self.lock:
                    self.segs, self.qx, self.qy = [], 0.0, 0.0
            self.last = None; self.pend = None; return
        x = b[4] | ((b[5] & 0x0F) << 8); y = (b[5] >> 4) | (b[6] << 4)
        x = x - 4096 if x > 2047 else x; y = y - 4096 if y > 2047 else y
        now = time.time()
        if DEBUG == "raw":
            print(f"raw {now % 100:8.3f} x={x:5d} y={y:5d} c={b[8]:3d} {bytes(b).hex()}", flush=True)
        if DEBUG:
            st = self.__dict__.setdefault("_st", {"t0": now, "n": 0, "gap": 0.0, "prev": now, "raw": 0, "kept": 0})
            st["n"] += 1; st["gap"] = max(st["gap"], now - st["prev"]); st["prev"] = now
            if self.last is not None:
                st["raw"] += abs(x - self.last[0]) + abs(y - self.last[1])
            if now - st["t0"] >= 2:
                print(f"touch {st['n'] / (now - st['t0']):.0f}/s, bursts every {self.burst_dt * 1000:.0f} ms, "
                      f"max gap {st['gap'] * 1000:.0f} ms, "
                      f"moved {st['raw']}, passed filters {st['kept']}", flush=True)
                st.update(t0=now, n=0, gap=0.0, raw=0, kept=0)
        if now - self.burst_t > 0.008:           # a new BLE connection event: learn their spacing
            if now - self.burst_t < 0.2:
                self.burst_dt += 0.2 * (now - self.burst_t - self.burst_dt)
            self.burst_t = now
        dev_t = b[1] | (b[2] << 8)               # the remote's own sample clock (~0.5 ms units)
        dev_dt, self.dev_t = (dev_t - self.dev_t) & 0xFFFF, dev_t
        step = min(0.15, max(0.005, dev_dt / 2000))   # time since the sample before, remote clock
        if self.last is None or now < self.guard_until:      # finger lands (or just clicked)
            self.last = (x, y, now); self.fx = self.fy = 0.0; self.settle = self.settle_frames
            self.peak = self.vprev = self.coast_t = 0.0; self.land_t = now; self.cmax = b[8]; self.a9 = [b[9]]; self.pend = None
            self.size_now = self.size_prev = b[9]; return
        if now - self.last[2] > 0.35:
            # BLE dropped reports mid-stroke (gaps of 200-450 ms happen): the finger is still
            # down, so take the new position as the baseline without settling again
            self.last = (x, y, now); return
        dx, dy = x - self.last[0], y - self.last[1]
        self.last = (x, y, now)
        # Lifting: byte 9 (contact size) collapses a sample or two before the remote reports the
        # finger gone, while the position slides. Hold the motion back from then; if the finger
        # is not gone within pend_max (it sometimes keeps moving with a small contact), play it.
        flush = None
        self.a9 = (self.a9 + [b[9]])[-5:]
        self.size_prev, self.size_now = getattr(self, "size_now", b[9]), b[9]
        ref = max(self.a9[:-1] or [0])
        if self.pend is None and ref >= 12 and b[9] < 0.6 * ref:
            self.pend, self.pend_t, self.pend_ref = [], 0.0, ref
        elif self.pend is not None:
            self.pend_t += step
            if b[9] >= 0.8 * self.pend_ref or self.pend_t > self.pend_max:
                flush, self.pend = self.pend, None
        if self.settle > 0:                      # finger still landing: position wanders, don't move
            self.settle -= 1; return
        if now - self.land_t < self.land_s:      # the first moments of a touch only settle the finger
            return
        # A finger leaving the surface loses contact before the remote says it is gone, and the
        # position slides meanwhile: once contact falls clearly below this touch's best, hold
        # still (it resumes if contact comes back, as in a light swipe).
        self.cmax = max(b[8], self.cmax - 0.5)
        if b[8] < self.cmax - self.lift_drop:
            with self.lock:
                self.segs, self.qx, self.qy = [], 0.0, 0.0
            return
        if abs(dx) > self.jump_max or abs(dy) > self.jump_max:   # sensor glitch
            return
        if self.pressed:                         # click held: freeze
            return
        if abs(dx) <= self.noise: dx = 0         # sensor noise only, no accumulation
        if abs(dy) <= self.noise: dy = 0
        if not (dx or dy):
            return
        if DEBUG:
            self._st["kept"] += abs(dx) + abs(dy)
        # Speed per the remote's clock, scaled to its usual 34-unit step: samples come 20 and
        # 34 units apart in turn, so distance per report alone jitters and fakes acceleration.
        v = (dx * dx + dy * dy) ** 0.5 * 34 / max(16, min(dev_dt, 120))
        # The remote smooths the position itself: after the finger stops it still reports a
        # creep decaying over 0.3-0.5 s, which dragged the pointer on. Once the speed falls
        # below half its recent peak, coast: emit nothing until the finger clearly speeds up
        # again or coast_max has passed (so slow fine positioning afterwards still moves).
        self.peak = max(v, self.peak * 0.95)
        if self.coast_t:
            if v > self.coast_exit * max(self.coast_v, 2) or now - self.coast_t > self.coast_max:
                self.coast_t, self.peak = 0.0, v
        elif v < self.coast_ratio * self.peak and v < self.vprev:
            self.coast_t, self.coast_v = now, v
        self.vprev = v
        if self.coast_t:
            return
        # A single sample far faster than the one before is a sensor or radio glitch (the
        # big jumps): let speed at most triple from one sample to the next.
        limit = 3 * self.peak + 40
        if v > limit:
            dx, dy, v = dx * limit / v, dy * limit / v, limit
        g = self.gain * min(self.cap, 1.0 + self.k * v)
        # the opposite for slow moves: below slow_v the pointer slows down, by up to `precision`
        # at a standstill, so small finger moves place it finely
        g *= 1.0 - self.precision * (1.0 - min(1.0, v / self.slow_v)) ** 2
        # A rolling thumb grows (or shrinks) its contact patch, and the reported centre slides
        # with it although the fingertip stays: take off the part of this step that the change
        # in contact size (byte 9) explains, about `roll` units of travel per unit of size.
        dsize = abs(b[9] - self.size_prev)
        dist = (dx * dx + dy * dy) ** 0.5
        if dist > 0:
            g *= max(0.0, dist - self.roll * dsize) / dist
        if self.swap: dx, dy = dy, dx
        if self.invert_x: dx = -dx
        if self.invert_y: dy = -dy
        if self.pend is not None:
            self.pend.append((dx * g, dy * g, step)); return
        for mx, my, st in (flush or []) + [(dx * g, dy * g, step)]:
            self._queue(mx, my, st, now)

    def _queue(self, mx, my, step, now):
        with self.lock:                          # emitted evenly by _pump between due times
            # Play the sample out over its own spacing, after the one before, about `smooth`
            # behind now: nudging toward that keeps the buffer filled through the radio's
            # bursts and follows the remote's clock, which runs a few percent off ours.
            if self.seg_end < now - 0.3:         # a new stroke
                start, due = now, now + self.smooth
            else:
                start = max(self.seg_end, now)
                due = self.seg_end + step
                due = max(due + 0.05 * (now + self.smooth - due), now + 0.01)
            self.segs.append([start, max(due, start + 0.001), mx, my, 0.0])
            self.seg_end = due

    def _pump(self):
        """BLE delivers touch reports in bursts, one per connection event (45-70 ms apart, not
        evenly). Each sample's motion is due at its own time on the remote's clock plus a short
        buffer; it is played out linearly from the sample before up to then, in 4 ms steps, so
        the pointer moves at the remote's ~58 Hz pace however the radio bunches the reports."""
        while True:
            time.sleep(0.004)
            now = time.time()
            with self.lock:
                if not self.segs and abs(self.qx) < 0.3 and abs(self.qy) < 0.3:
                    continue
                for seg in self.segs:
                    t0, t1, mx, my, done = seg
                    f = min(1.0, max(0.0, (now - t0) / (t1 - t0)))
                    if f > done:
                        self.qx += mx * (f - done); self.qy += my * (f - done); seg[4] = f
                self.segs = [seg for seg in self.segs if seg[4] < 1.0]
                # then a gentle low-pass, so single jumps in the remote's data (skipped samples,
                # late bursts) come out as a quick glide instead of a leap
                # (at least 0.5 px a step, so the end comes promptly instead of fading for long)
                a = 1.0 if self.glide <= 0 else 1 - math.exp(-0.004 / self.glide)
                q = math.hypot(self.qx, self.qy)
                if q > 0:
                    a = min(1.0, max(a, 0.5 / q))
                self.fx += self.qx * a; self.fy += self.qy * a; self.qx -= self.qx * a; self.qy -= self.qy * a
                ix, iy = int(self.fx), int(self.fy)
                self.fx -= ix; self.fy -= iy
            if ix or iy:
                self._ev(self.EV_REL, self.REL_X, ix); self._ev(self.EV_REL, self.REL_Y, iy); self._ev(self.EV_SYN, 0, 0)

    def keys(self, pressed):
        """Press/release mapped keys on button state changes."""
        # The ring: in herdr it moves between panes (and at the edge to the next tab or
        # workspace, see herdr_move); everywhere else it is plain arrow keys, held like keys.
        prev = getattr(self, "_prev", set())
        route = self.__dict__.setdefault("_route", {})
        new_arrows = [b for b in self.ARROWS if b in pressed and b not in prev]
        if MENU.open:                            # the open settings menu takes the ring, Back and TV
            for b in new_arrows:
                route[b] = "menu"
                threading.Thread(target=MENU.key, args=(b,), daemon=True).start()
            new_arrows = []
            closing = {b for b in ("menu", "tv") if b in pressed and b not in prev}
            if closing:
                MENU.close()
                self._swallow = getattr(self, "_swallow", set()) | closing
        # Back/TV that closed the menu do nothing else until they are let go
        self._swallow = {b for b in getattr(self, "_swallow", set()) if b in pressed}
        pressed = [b for b in pressed if b not in self._swallow]
        if new_arrows:
            herdr = self.herdr_focused()
            for b in new_arrows:
                route[b] = "herdr" if herdr else "key"
                if herdr:
                    threading.Thread(target=self.herdr_move, args=(b,), daemon=True).start()
        now = {self.KEYMAP[b] for b in pressed if b in self.KEYMAP
               and (b not in self.ARROWS or route.get(b) == "key")}
        held = getattr(self, "_held", set())
        for code in held - now:
            self._ev(self.EV_KEY, code, 0)
        for code in now - held:
            self._ev(self.EV_KEY, code, 1)
        if held != now:
            self._ev(self.EV_SYN, 0, 0)
        self._held = now
        cur = set(pressed); now_t = time.time()
        # Siri: holding it dictates (a tap sends nothing)
        if "siri" in cur and "siri" not in prev:
            self._siri_t = now_t
            if MIC:
                MIC.start()
        if "siri" in prev and "siri" not in cur:
            if now_t - getattr(self, "_siri_t", 0) < self.tap_max:   # a tap, no dictation
                if MIC:
                    MIC.cancel()
            elif MIC:
                MIC.release()
        if "tv" in cur and "tv" not in prev:     # TV = Enter
            self.combo([28])
        if "play_pause" in cur and "play_pause" not in prev:
            # 1x play/pause at once; a second press right after skips to the next track and
            # makes sure it plays (the first press had just paused it)
            taps = getattr(self, "_taps", 0) + 1 if now_t - getattr(self, "_tap_t", 0) < self.multi_s else 1
            self._taps, self._tap_t = taps, now_t
            if taps <= 2:
                threading.Thread(target=media, args=(taps,), daemon=True).start()
        # Power: held for hold_s opens/closes the settings menu; a short press goes to the next
        # Omarchy workspace (its SUPER+TAB, via hyprctl) when let go
        if "power" in cur and "power" not in prev:
            self._power_t, self._power_long = now_t, False
            threading.Timer(self.hold_s, self._power_hold, args=(now_t,)).start()
        if "power" in prev and "power" not in cur:
            self._power_t = 0
            if not getattr(self, "_power_long", False):
                subprocess.Popen(["hyprctl", "dispatch", 'hl.dsp.focus({ workspace = "m+1" })'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._prev = cur

    @staticmethod
    def herdr(method, **params):
        """One request on herdr's socket API (newline-delimited JSON)."""
        with socket.socket(socket.AF_UNIX) as sk:
            sk.settimeout(2)
            sk.connect(os.path.expanduser("~/.config/herdr/herdr.sock"))
            sk.sendall((json.dumps({"id": "remote", "method": method, "params": params}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sk.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf).get("result", {})

    def _power_hold(self, t):
        if getattr(self, "_power_t", 0) == t:   # still the same press, still held
            self._power_long = True
            MENU.toggle()

    @staticmethod
    def herdr_focused():
        """Whether the focused window is the terminal a herdr client runs in."""
        try:
            win = json.loads(subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True,
                                            text=True, timeout=1).stdout or "{}").get("pid")
            for pid in subprocess.run(["pgrep", "-x", "herdr"], capture_output=True, text=True).stdout.split():
                p = int(pid)
                while p > 1:
                    if p == win:
                        return True
                    p = int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
        except Exception:
            pass
        return False

    def herdr_move(self, direction):
        """Ring in herdr: focus the neighbouring pane that way; at the edge, ←/→ go to the
        previous/next tab and ↑/↓ to the previous/next workspace."""
        try:
            pane = next(p["pane_id"] for p in self.herdr("pane.list")["panes"] if p["focused"]
                        and p["tab_id"] == next(w for w in self.herdr("workspace.list")["workspaces"]
                                                if w["focused"])["active_tab_id"])
            n = self.herdr("pane.neighbor", pane_id=pane, direction=direction).get("neighbor", {})
            if n.get("neighbor_pane_id"):
                self.herdr("pane.focus", pane_id=n["neighbor_pane_id"])
                return
        except Exception as e:
            print("herdr pane", direction, e, flush=True)
        kind = "tab" if direction in ("left", "right") else "workspace"
        self.herdr_step(kind, -1 if direction in ("left", "up") else 1)

    def herdr_step(self, kind, step):
        """Previous/next tab or workspace in herdr, wrapping around."""
        try:
            wss = self.herdr("workspace.list")["workspaces"]
            ws = next(w for w in wss if w["focused"])
            if kind == "workspace":
                ids, cur = [w["workspace_id"] for w in wss], ws["workspace_id"]
                key = "workspace_id"
            else:
                tabs = [t for t in self.herdr("tab.list")["tabs"] if t["workspace_id"] == ws["workspace_id"]]
                ids, cur, key = [t["tab_id"] for t in tabs], ws["active_tab_id"], "tab_id"
            if len(ids) > 1:
                self.herdr(kind + ".focus", **{key: ids[(ids.index(cur) + step) % len(ids)]})
        except Exception as e:
            print("herdr", kind, e, flush=True)

    def combo(self, codes):
        """Tap a key combination (modifiers first, released in reverse)."""
        for c in codes:
            self._ev(self.EV_KEY, c, 1)
        self._ev(self.EV_SYN, 0, 0)
        for c in reversed(codes):
            self._ev(self.EV_KEY, c, 0)
        self._ev(self.EV_SYN, 0, 0)

    def click(self, down):
        if not down and self.pressed:
            self.guard_until = time.time() + 0.2   # no jump right after a click (as SiriRemoteForge)
        if down != self.pressed:
            self.pressed = down
            self._ev(self.EV_KEY, self.BTN_LEFT, 1 if down else 0); self._ev(self.EV_SYN, 0, 0)


MOUSE = None


# --- Opus voice capture and Voxtype dictation ---

class Voice:
    """Decode [4B header][1B len][Opus CELT-WB 20 ms] at 48 kHz, conceal gaps and feed the mic."""
    # Decoded at 48 kHz, Opus's highest rate (it resamples internally); the mic doubles it, see Mic.
    RATE = 48000; FRAME = 960

    def __init__(self):
        self.lib = ctypes.CDLL("libopus.so.0")
        self.lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.lib.opus_decoder_create.restype = ctypes.c_void_p
        self.lib.opus_decode_float.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                                               ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_int]
        err = ctypes.c_int()
        self.dec = self.lib.opus_decoder_create(self.RATE, 1, ctypes.byref(err))
        if not self.dec or err.value:
            raise RuntimeError(f"Opus decoder creation failed: {err.value}")
        self.buf = (ctypes.c_float * (self.FRAME * 6))()
        self.frames, self.packets, self.last = [], 0, 0
        self.prev_seq, self.tx = None, False
        self.siri_down, self.siri_t0, self.siri_up = False, 0, None

    def _decode(self, pkt):
        # for PLC (pkt None) frame_size must be exactly one 20 ms frame
        n = self.lib.opus_decode_float(self.dec, pkt, len(pkt) if pkt else 0, self.buf,
                                       self.FRAME * 6 if pkt else self.FRAME, 0)
        return list(self.buf[:n]) if n > 0 else []

    def feed(self, v):
        if len(v) < 6:
            return
        seq = v[2] | (v[3] << 8); L = v[4]; pkt = bytes(v[5:5 + L])   # bytes 2-3: 16-bit frame counter
        if not self.packets:
            self.prev_seq = None
        pcm = []
        if self.prev_seq is not None:
            missing = ((seq - self.prev_seq) & 0xFFFF) - 1
            self.lost = getattr(self, "lost", 0) + (missing if 0 < missing <= 50 else 0)   # a jump = new stream
            if 0 < missing <= 10:          # conceal short real gaps only; restart/wrap is not a gap
                for _ in range(missing):
                    pcm.extend(self._decode(None))
        self.prev_seq = seq
        self.packets += 1
        if not self.tx:
            self.tx = True
            publish(listening=True)
        pcm.extend(self._decode(pkt))
        self.last = time.time()
        if MIC:
            MIC.push(pcm)
        else:
            self.frames.extend(pcm)

    def flush_loop(self):
        while True:
            time.sleep(0.03)
            if self.tx and time.time() - self.last > 0.12:
                self.tx = False
                publish(listening=False)
                if DEBUG:
                    print(f"voice: {self.packets} packets, {getattr(self, 'lost', 0)} lost, "
                          f"mic active {bool(MIC and MIC.active)}", flush=True)
                    self.lost = 0
            if not (self.packets and time.time() - self.last > 0.6):
                continue
            frames, packets = self.frames, self.packets
            self.frames, self.packets = [], 0
            tapped = self.siri_up and self.siri_t0 and self.siri_up - self.siri_t0 < 0.35
            if MIC or tapped or packets < 10:    # PipeWire path, or a Siri tap (= Enter), not speech
                continue
            threading.Thread(target=self.dictate, args=(frames,), daemon=True).start()

    def dictate(self, frames):
        """Fallback without PipeWire: transcribe with the user's Voxtype setup and paste the text
        the way Voxtype does (clipboard + Shift+Insert)."""
        fd, wav = tempfile.mkstemp(suffix=".wav", dir=RUN)
        os.close(fd)
        try:
            g = min(1.0, 0.98 / max(1e-6, max(abs(x) for x in frames)))   # headroom, no clipping
            with wave.open(wav, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.RATE)
                w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x * g)) * 32767)) for x in frames))
            env = dict(os.environ)
            try:
                for line in open(os.path.expanduser("~/.config/voxtype/env")):
                    k, _, v = line.strip().partition("=")
                    if k and not k.startswith("#"):
                        env[k.removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
            except OSError:
                pass
            p = subprocess.run(["voxtype", "-q", "transcribe", wav], capture_output=True, text=True, env=env, timeout=90)
        finally:
            os.unlink(wav)
        lines = [l for l in p.stdout.splitlines() if l.strip() and not l.startswith(("Loading audio", "Audio format", "Processing "))]
        text = " ".join(l.strip() for l in lines).strip()
        if p.returncode != 0 or not text:
            print("voxtype failed:", p.returncode, p.stderr[-400:], flush=True)
            return
        subprocess.run(["wl-copy", "--", text], check=False)
        subprocess.run(["wtype", "-M", "shift", "-k", "Insert", "-m", "shift"], check=False)


VOICE = None


class Mic:
    """The remote's voice as a PipeWire source ("Siri Remote Mic"), so Voxtype records it natively.
    The source exists only while dictating with the remote, so nothing else (the pedal, a
    keybinding, other apps) can ever pick it: Siri down -> load it, make it the default source,
    `voxtype record start`; transmission over after release -> `voxtype record stop`, the previous
    default source comes back and the source is removed again. Voxtype shows its own indicator,
    transcribes and pastes exactly as with the pedal."""
    NAME = "siri_remote_mic"
    # PipeWire's pipe source buffers a fixed 8192 samples: 512 ms at the stream's native 16 kHz,
    # 170 ms at 48 kHz, 85 ms at 96 kHz (measured end to end: 540, 190, 110 ms).
    RATE = 96000
    FRAME = RATE // 50 * 2                             # 20 ms of s16le mono

    def __init__(self):
        self.fifo = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "siri-remote-mic.fifo")
        self.module = None
        self._unload_all()                             # left over from a driver that died
        if subprocess.run(["pactl", "info"], capture_output=True).returncode:
            raise RuntimeError("no PipeWire/PulseAudio server")
        self.q, self.lock = bytearray(), threading.Lock()
        self.active, self.released, self.prev_source, self.prev_x = False, False, None, 0.0

    def _unload_all(self):
        for line in subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True).stdout.splitlines():
            if self.NAME in line:
                subprocess.run(["pactl", "unload-module", line.split()[0]], capture_output=True)
        self.module = None

    def _load(self):
        if os.path.exists(self.fifo):
            os.unlink(self.fifo)
        r = subprocess.run(["pactl", "load-module", "module-pipe-source", f"source_name={self.NAME}",
                            f"file={self.fifo}", "format=s16le", f"rate={self.RATE}", "channels=1",
                            "source_properties=device.description=Siri-Remote-Mic"], capture_output=True, text=True)
        if r.returncode:
            print("remote mic unavailable:", r.stderr.strip(), flush=True)
            return False
        self.module = r.stdout.strip()
        threading.Thread(target=self._writer, args=(self.module,), daemon=True).start()
        return True

    def cleanup(self):
        """Driver exits: never leave the remote mic behind as the default source."""
        if self.active and self.prev_source:
            self._run("pactl", "set-default-source", self.prev_source)
        self._unload_all()

    def push(self, pcm):
        if self.active and not self.released and pcm:
            up, prev = [], self.prev_x                 # 48 -> 96 kHz: linear midpoints
            for x in pcm:
                up += ((prev + x) / 2, x); prev = x
            self.prev_x = prev
            data = struct.pack(f"<{len(up)}h", *(int(max(-1.0, min(1.0, x)) * 32767) for x in up))
            with self.lock:
                self.q += data

    def _writer(self, module):
        """Feed the pipe in real time while this source exists: queued voice (bursts drained
        quickly), else silence. PipeWire stops reading while nobody records; silence that would
        then pile up in front of the voice is dropped instead, voice waits for the reader."""
        try:
            fd = os.open(self.fifo, os.O_WRONLY)
        except OSError:
            return
        try:
            fcntl.fcntl(fd, 1031, 4096)                # F_SETPIPE_SZ: one page, ~40 ms
        except OSError:
            pass
        os.set_blocking(fd, False)
        nxt = time.monotonic()
        try:
            while self.module == module:
                with self.lock:
                    n = self.FRAME * (3 if len(self.q) > self.FRAME * 6 else 1)
                    chunk = bytes(self.q[:n]); del self.q[:n]
                data = chunk + b"\0" * (self.FRAME - len(chunk)) if len(chunk) < self.FRAME else chunk
                try:
                    done = os.write(fd, data)
                except BlockingIOError:
                    done = 0
                if done < len(chunk):                  # voice not taken yet: keep it, in order
                    with self.lock:
                        self.q[:0] = chunk[done:]
                nxt += 0.02
                time.sleep(max(0, nxt - time.monotonic()))
        except OSError:
            pass
        finally:
            os.close(fd)

    def _run(self, *cmd):
        subprocess.run(list(cmd), capture_output=True, timeout=5)

    def start(self):
        if self.active:
            return
        out = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True).stdout.strip()
        self.prev_source = out if out and out != self.NAME else self.prev_source
        with self.lock:
            self.q.clear()
        if not self._load():
            return
        self.active, self.released = True, False
        self._run("pactl", "set-default-source", self.NAME)
        self._run("voxtype", "record", "start")
        threading.Thread(target=self._grab_stream, daemon=True).start()

    def _grab_stream(self):
        """PipeWire restores Voxtype's remembered input, so move its capture stream explicitly."""
        me = None
        for line in subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True).stdout.splitlines():
            if self.NAME in line:
                me = line.split()[0]
        for _ in range(30):
            out = subprocess.run(["pactl", "list", "source-outputs"], capture_output=True, text=True).stdout
            for block in out.split("Source Output #")[1:]:
                sid = block.split()[0]
                if "voxtype" in block.lower():
                    if me and f"Source: {me}" not in block:
                        self._run("pactl", "move-source-output", sid, self.NAME)
                    return
            time.sleep(0.05)

    def release(self):
        """Siri released: stop like any push-to-talk key, once the voice already received has
        gone through the pipe (~0.1 s). What the remote sends after release is not used."""
        if self.active and not getattr(self, "finishing", False):
            self.released = self.finishing = True
            threading.Thread(target=self._finish, args=("stop",), daemon=True).start()

    def cancel(self):
        if self.active and not getattr(self, "finishing", False):
            self.finishing = True
            threading.Thread(target=self._finish, args=("cancel",), daemon=True).start()

    def _finish(self, how):
        t = time.time()
        while self.q and time.time() - t < 0.5:      # play the queued voice into the pipe first
            time.sleep(0.02)
        time.sleep(0.1)                               # through the pipe source's 85 ms buffer
        self._run("voxtype", "record", how)
        self.active = self.finishing = False
        time.sleep(0.5)
        if self.prev_source:
            self._run("pactl", "set-default-source", self.prev_source)
        self._unload_all()


MIC = None


# --- Bluetooth LE link and raw ATT reports ---

def find_remote():
    """First paired Bluetooth device whose modalias is Apple's gen-3 Siri Remote (v004Cp0315)."""
    for line in btctl("devices", "Paired", t=4).splitlines():
        parts = line.split()
        if len(parts) > 1 and "p0315" in btctl("info", parts[1], t=3):
            return parts[1]
    return None


BT_MAC = None
SEARCH_S = 120                   # how long to look for a remote that went away
WAKE = threading.Event()         # SIGUSR1 (the card's "Search"): look again
BT_LIVE = {"connected": False, "percent": None, "power": None}   # power: 0xAB charging, 0xAF battery, 0xBB full


def bt_input_loop():
    """Own the remote's LE link via a raw ATT socket: enable reports (0xAF -> F0/F1 feature
    reports) and subscribe to buttons (0x3a), touch (0x3e), battery (0x2f) and power state (0x32)."""
    H_BTN, H_TOUCH, H_BATT, H_PWR = 0x3a, 0x3e, 0x2f, 0x32
    global BT_MAC
    scanner = None
    failed_t = 0.0                         # when the last takeover failed
    while True:
        s = None
        try:
            while not BT_MAC:
                BT_MAC = find_remote() or time.sleep(5)
            # wait until bluez auto-connects the woken remote (needs discovery running), then take the link over
            # an explicit bluez Disconnect disables auto-connect; an explicit Connect re-arms it and
            # waits (kernel accept list resolves the remote's RPA) until the woken remote shows up
            # Search only for SEARCH_S after the remote went away (or the driver started): a remote
            # that is out of the room must not keep the radio busy. After that the driver sleeps
            # until the card's "Search" (SIGUSR1) wakes it.
            search_t = time.time()
            publish(searching=True)
            while "Connected: yes" not in btctl("info", BT_MAC, t=3):
                if time.time() - search_t > SEARCH_S:
                    if scanner and scanner.poll() is None:
                        scanner.kill()
                    scanner = None
                    publish(searching=False)
                    print("remote not found, stopped searching", flush=True)
                    WAKE.clear(); WAKE.wait()
                    search_t = time.time()
                    publish(searching=True)
                # a running scan process is not enough: it outlives a bluetooth restart, discovery not
                if not scanner or scanner.poll() is not None or "Discovering: yes" not in btctl("show", t=3):
                    if scanner and scanner.poll() is None:
                        scanner.kill()
                    scanner = subprocess.Popen(["bluetoothctl", "--timeout", "86400", "scan", "on"],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["bluetoothctl", "--timeout", "10", "connect", BT_MAC], capture_output=True, timeout=15)
                # After a failed takeover (typically right after bluetooth or the adapter
                # restarted) BlueZ can keep a dead connection attempt that blocks every new one:
                # the remote never comes back, buttons or not. Only switching the adapter off and
                # on clears it, so do that when the remote is still away 15 s after such a failure.
                if failed_t and time.time() - failed_t > 15:
                    print("bluetooth stuck, switching the adapter off and on", flush=True)
                    btctl("power", "off", t=5); time.sleep(2); btctl("power", "on", t=5)
                    failed_t = 0.0
            # hand the woken link from bluez to our ATT socket; the remote re-advertises right after
            # the disconnect, so retry the direct connect quickly instead of waiting long
            subprocess.run(["bluetoothctl", "disconnect", BT_MAC], capture_output=True, timeout=5)
            for _ in range(6):
                s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
                s.setsockopt(socket.SOL_BLUETOOTH, socket.BT_SECURITY, struct.pack("BB", socket.BT_SECURITY_MEDIUM, 0))
                s.bind(("00:00:00:00:00:00", 0, 4, socket.BDADDR_LE_PUBLIC))
                s.settimeout(2.5)
                try:
                    s.connect((BT_MAC, 0, 4, socket.BDADDR_LE_PUBLIC)); break
                except OSError:
                    s.close(); s = None; time.sleep(0.1)
            if s is None:
                failed_t = failed_t or time.time()
                raise TimeoutError("takeover failed")
            failed_t = 0.0
            # Discovery is only for finding the remote: left running it scans continuously and
            # takes airtime from the link (touch gaps, lost voice), so stop it once ours.
            if scanner and scanner.poll() is None:
                scanner.kill()
            scanner = None
            publish(searching=False)
            s.settimeout(5)

            def req(pdu):
                s.send(pdu)
                while True:
                    r = s.recv(512)
                    if not r:
                        raise ConnectionError("ATT socket closed")
                    if r[0] == 0x1d: s.send(b"\x1e")
                    if r[0] in (0x1b, 0x1d): continue
                    return r
            req(b"\x02" + struct.pack("<H", 185))
            # setup writes "handle:hex,…"; SIRI_REMOTE_INIT overrides them for experiments
            for item in os.environ.get("SIRI_REMOTE_INIT", "4e:af,51:af").split(","):
                fh, _, val = item.partition(":")
                r = req(b"\x12" + struct.pack("<H", int(fh, 16)) + bytes.fromhex(val))
                if DEBUG:
                    print(f"init write {fh} {val} -> {r.hex()}", flush=True)
            for cccd in (0x37, 0x3b, 0x3f, 0x43, 0x47, 0x4b, 0x30, 0x33):
                req(b"\x12" + struct.pack("<H", cccd) + b"\x01\x00")
            for h, key in ((H_BATT, "percent"), (H_PWR, "power")):
                r = req(b"\x0a" + struct.pack("<H", h))
                if len(r) >= 2 and r[0] == 0x0b: BT_LIVE[key] = r[1]
            BT_LIVE["connected"] = True
            print("connected", BT_MAC, flush=True)
            publish(**battery())
            s.settimeout(None)
            while True:
                r = s.recv(512)
                if not r:
                    raise ConnectionError("ATT socket closed")
                if r[0] == 0x1d: s.send(b"\x1e")
                if r[0] not in (0x1b, 0x1d): continue
                if len(r) < 3:
                    continue
                h = struct.unpack("<H", r[1:3])[0]; v = r[3:]
                if h == H_BTN and len(v) >= 2:
                    m = decode(bytes([0xFB]) + v)
                    if DEBUG:
                        print("buttons", sorted(m["pressed"]), flush=True)
                    if MOUSE:
                        MOUSE.click("select" in m["pressed"])
                        MOUSE.keys(m["pressed"])
                    if VOICE:
                        down = "siri" in m["pressed"]
                        if down and not VOICE.siri_down: VOICE.siri_up = None; VOICE.siri_t0 = time.time()
                        if not down and VOICE.siri_down: VOICE.siri_up = time.time()
                        VOICE.siri_down = down
                elif h == 0x36 and VOICE:
                    VOICE.feed(v)
                elif h == H_TOUCH:
                    if MOUSE:
                        MOUSE.touch(v)
                elif h in (H_BATT, H_PWR) and v:
                    BT_LIVE["percent" if h == H_BATT else "power"] = v[0]
                    publish(**battery())
        except Exception as e:
            if BT_LIVE["connected"] or isinstance(e, TimeoutError):
                print("disconnected:" if BT_LIVE["connected"] else "not taken over:", type(e).__name__, e, flush=True)
            BT_LIVE["connected"] = False
            publish(**battery())
        finally:
            if s:
                s.close()
        time.sleep(1)


# --- Battery state ---

def battery():
    """Link and charge state: USB link = charging; Bluetooth battery from the remote, the kernel
    power_supply, HID feature report 0x47 or BlueZ."""
    info = {"connected": BT_LIVE["connected"], "usb": any(b == "usb" for _, b in remotes()),
            "percent": None, "charging": False, "full": False}
    if BT_LIVE["percent"] is not None:
        info.update(percent=BT_LIVE["percent"], charging=BT_LIVE["power"] == 0xAB, full=BT_LIVE["power"] == 0xBB)
        return info
    for ps in glob.glob("/sys/class/power_supply/hid-*"):
        try:
            if "0315" in open(ps + "/device/uevent").read() or "Remote" in open(ps + "/model_name").read():
                st = open(ps + "/status").read().strip()
                info.update(percent=int(open(ps + "/capacity").read()), charging=st == "Charging", full=st == "Full")
        except (OSError, ValueError):
            pass
    if info["percent"] is None:
        # Apple battery feature report 0x47 (same as Magic Keyboard): byte1 = percent
        for path, bus in remotes():
            try:
                fd = os.open(path, os.O_RDWR)
                try:
                    buf = bytearray(8); buf[0] = 0x47
                    fcntl.ioctl(fd, (3 << 30) | (len(buf) << 16) | (ord("H") << 8) | 0x07, buf)
                finally:
                    os.close(fd)
                info["percent"] = buf[1]; break
            except OSError:
                pass
    if info["percent"] is None and BT_MAC:
        for l in btctl("info", BT_MAC, t=3).splitlines():
            if "Battery Percentage" in l and "(" in l:
                info["percent"] = int(l.split("(")[1].rstrip(")"))
    info["charging"] = info["charging"] or info["usb"]
    return info


def battery_loop():
    """Every five seconds: the link and battery state, and a fresh timestamp so the plugin
    can tell a running driver from a file a crashed one left behind."""
    while True:
        # the measured spacing of the radio's bursts, for the card (known once touch reports came)
        radio = round(MOUSE.burst_dt * 1000) if MOUSE and MOUSE.burst_t and BT_LIVE["connected"] else None
        publish(t=int(time.time()), radio_ms=radio, **battery())
        time.sleep(5)


def btctl(*a, t=8):
    """Run a bounded BlueZ query, returning diagnostic text on failure."""
    try:
        return subprocess.run(["bluetoothctl", *a], capture_output=True, text=True, timeout=t).stdout
    except Exception as e:
        return str(e)


def decode(r):
    """Decode the thirteen button bits in HID report 0xFB."""
    if len(r) >= 3 and r[0] == 0xFB:
        bits = r[1] | (r[2] << 8)
        return {"pressed": [n for i, n in enumerate(BTN_BITS) if bits >> i & 1]}
    return {"pressed": []}


if __name__ == "__main__":
    signal.signal(signal.SIGUSR1, lambda *_: WAKE.set())
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    publish(**battery())
    try:
        MOUSE = VMouse()
    except OSError as e:
        print("uinput:", e, flush=True)
    VOICE = Voice()
    try:
        MIC = Mic()
    except Exception as e:
        print("pipewire mic unavailable, falling back to file transcription:", e, flush=True)
    threading.Thread(target=VOICE.flush_loop, daemon=True).start()
    threading.Thread(target=bt_input_loop, daemon=True).start()
    threading.Thread(target=battery_loop, daemon=True).start()
    signal.pause()
