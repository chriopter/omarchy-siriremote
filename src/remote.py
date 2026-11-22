#!/usr/bin/env python3
"""Apple Siri Remote gen 3: raw BLE input, uinput mouse, Voxtype and local SSE viewer.

Uses only the Python standard library; voice decoding loads the system libopus.
USB/Bluetooth hidraw reports remain available for diagnostics and battery fallback.
"""
import ctypes
import fcntl
import glob
import json
import os
import queue
import select
import shutil
import socket
import struct
import subprocess
import threading
import time
import wave
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# --- Paths and HID report layout ---

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser(os.environ.get("SIRI_REMOTE_DATA", "~/.local/share/omarchy-siriremote"))
os.makedirs(os.path.join(DATA, "rec"), exist_ok=True)
clients = []
lock = threading.Lock()

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

class VMouse:
    """Virtual mouse via /dev/uinput: touch surface -> relative motion, ring centre click -> left click."""
    EV_SYN, EV_KEY, EV_REL = 0, 1, 2
    BTN_LEFT, REL_X, REL_Y = 0x110, 0, 1
    # remote button -> linux key code (input-event-codes.h)
    KEYMAP = {"menu": 1, "up": 103, "down": 108, "left": 105, "right": 106,   # Esc, arrows
              "vol_up": 115, "vol_down": 114, "mute": 113}                            # media keys
    # play/pause like Apple: 1x play/pause, 2x next track, 3x previous track
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
        self.swap, self.invert_x, self.invert_y = True, False, False
        self.gain, self.k, self.cap, self.noise, self.touch_min = 0.8, 0.04, 4.0, 1, 130
        self.settle_frames, self.settle, self.jump_max, self.touch_keep = 4, 0, 300, 80
        self.tap_max, self.multi_s = 0.35, 0.4
        self.guard_until = 0.0
        self.px = self.py = 0.0; self.glide = 0.3
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
                for k, v in json.load(open(path)).items():
                    setattr(self, k, v)
        except (OSError, ValueError):
            pass

    def touch(self, b):
        """Convert packed signed 12-bit touch coordinates into filtered motion."""
        if len(b) < 10:
            return
        if self.last is None:
            self.reload()
        # hysteresis: a touch starts at touch_min but only ends below touch_keep, so contact
        # pressure dipping mid-swipe doesn't break the stroke (that caused skips)
        if b[8] < (self.touch_keep if self.last is not None else self.touch_min):
            self.last = None; return
        x = b[4] | ((b[5] & 0x0F) << 8); y = (b[5] >> 4) | (b[6] << 4)
        x = x - 4096 if x > 2047 else x; y = y - 4096 if y > 2047 else y
        now = time.time()
        if self.last is None or now - self.last[2] > 0.15 or now < self.guard_until:
            self.last = (x, y, now); self.fx = self.fy = 0.0; self.settle = self.settle_frames; return
        dx, dy = x - self.last[0], y - self.last[1]
        self.last = (x, y, now)
        if self.settle > 0:                      # finger still landing: position wanders, don't move
            self.settle -= 1; return
        if abs(dx) > self.jump_max or abs(dy) > self.jump_max:   # sensor glitch
            return
        if self.pressed:                         # click held: freeze
            return
        if abs(dx) <= self.noise: dx = 0         # sensor noise only, no accumulation
        if abs(dy) <= self.noise: dy = 0
        if not (dx or dy):
            return
        v = (dx * dx + dy * dy) ** 0.5           # raw units per report (~67 Hz)
        g = self.gain * min(self.cap, 1.0 + self.k * v)
        if self.swap: dx, dy = dy, dx
        if self.invert_x: dx = -dx
        if self.invert_y: dy = -dy
        with self.lock:
            self.px += dx * g; self.py += dy * g   # emitted smoothly by _pump

    def _pump(self):
        """BLE delivers touch reports in bursts per connection event; spread the motion over
        ~4 ms ticks so the pointer glides instead of jumping."""
        while True:
            time.sleep(0.004)
            with self.lock:
                if abs(self.px) < 0.5 and abs(self.py) < 0.5:
                    continue
                k = self.glide
                mx, my = self.px * k, self.py * k
                self.fx += mx; self.fy += my; self.px -= mx; self.py -= my
                ix, iy = int(self.fx), int(self.fy)
                self.fx -= ix; self.fy -= iy
            if ix or iy:
                self._ev(self.EV_REL, self.REL_X, ix); self._ev(self.EV_REL, self.REL_Y, iy); self._ev(self.EV_SYN, 0, 0)

    def keys(self, pressed):
        """Press/release mapped keys on button state changes."""
        # the ring never sends plain arrows: it is herdr navigation (see below)
        now = {self.KEYMAP[b] for b in pressed if b in self.KEYMAP and b not in self.ARROWS}
        held = getattr(self, "_held", set())
        for code in held - now:
            self._ev(self.EV_KEY, code, 0)
        for code in now - held:
            self._ev(self.EV_KEY, code, 1)
        if held != now:
            self._ev(self.EV_SYN, 0, 0)
        self._held = now
        prev = getattr(self, "_prev", set()); cur = set(pressed); now_t = time.time()
        # Siri short tap = Enter (holding it dictates; the voice path drops taps)
        if "siri" in cur and "siri" not in prev:
            self._siri_t = now_t
            if VOICE and MIC and Voice.mode() == "dictate":
                MIC.start()
        if "siri" in prev and "siri" not in cur:
            if now_t - getattr(self, "_siri_t", 0) < self.tap_max:
                if MIC:
                    MIC.cancel()
                self.combo([28])
            elif MIC:
                MIC.release()
        if "tv" in cur and "tv" not in prev:
            threading.Thread(target=self.herdr_step, args=("pane", 1), daemon=True).start()
        if "play_pause" in cur and "play_pause" not in prev:
            # like Apple: 1x acts at once (play/pause); 2x undoes it and skips to the next track
            # (playback continues); 3x goes to the track before the original one
            taps = getattr(self, "_taps", 0) + 1 if now_t - getattr(self, "_tap_t", 0) < self.multi_s else 1
            self._taps, self._tap_t = taps, now_t
            if taps == 1:
                self.combo([164])
            elif taps == 2:
                self.combo([164]); self.combo([163])
            elif taps == 3:
                self.combo([165]); self.combo([165])
        # one button = one action, no modes:
        # ring ←/→ herdr tab, ↑/↓ herdr workspace, TV = next pane in tab, Power = next Omarchy workspace
        RING = {"left": ("tab", -1), "right": ("tab", 1), "up": ("workspace", -1), "down": ("workspace", 1)}
        for b, (kind, step) in RING.items():
            if b in cur and b not in prev:
                threading.Thread(target=self.herdr_step, args=(kind, step), daemon=True).start()
        if "power" in cur and "power" not in prev:   # Omarchy "Next workspace" (its SUPER+TAB), via hyprctl
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

    def herdr_step(self, kind, step):
        """Ring: ←/→ previous/next tab, ↑/↓ previous/next workspace; TV: next pane in the tab.
        All wrap around, always the same action."""
        try:
            wss = self.herdr("workspace.list")["workspaces"]
            ws = next(w for w in wss if w["focused"])
            if kind == "workspace":
                ids, cur = [w["workspace_id"] for w in wss], ws["workspace_id"]
                key = "workspace_id"
            elif kind == "tab":
                tabs = [t for t in self.herdr("tab.list")["tabs"] if t["workspace_id"] == ws["workspace_id"]]
                ids, cur, key = [t["tab_id"] for t in tabs], ws["active_tab_id"], "tab_id"
            else:
                panes = [p for p in self.herdr("pane.list")["panes"] if p["tab_id"] == ws["active_tab_id"]]
                ids = [p["pane_id"] for p in panes]
                if not ids:
                    return
                cur = next((p["pane_id"] for p in panes if p["focused"]), ids[0]); key = "pane_id"
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
    """Decode [4B header][1B len][Opus CELT-WB 20 ms] at 16 kHz, conceal gaps and save audio."""
    RATE = 16000; FRAME = 320   # native rate of the remote's CELT-WB stream: no resampling

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
        self.frames, self.raw, self.last, self.n = [], [], 0, 0
        self.prev_seq, self.lost = None, 0
        self.tx, self.tx_start, self.siri_up = False, 0, None

    def _decode(self, pkt):
        # for PLC (pkt None) frame_size must be exactly one 20 ms frame
        n = self.lib.opus_decode_float(self.dec, pkt, len(pkt) if pkt else 0, self.buf,
                                       self.FRAME * 6 if pkt else self.FRAME, 0)
        return list(self.buf[:n]) if n > 0 else []

    def feed(self, v):
        if len(v) < 6:
            return
        seq = v[2] | (v[3] << 8); L = v[4]; pkt = bytes(v[5:5 + L])   # bytes 2-3: 16-bit frame counter
        if not self.frames:
            broadcast({"type": "voice", "state": "start"}); self.prev_seq = None; self.lost = 0
        if self.prev_seq is not None:
            missing = ((seq - self.prev_seq) & 0xFFFF) - 1
            if 0 < missing <= 10:          # conceal short real gaps only; restart/wrap is not a gap
                for _ in range(missing):
                    self.frames.extend(self._decode(None)); self.lost += 1
        self.prev_seq = seq
        self.raw.append(bytes(v))
        if not self.tx:
            self.tx = True; self.tx_start = time.time()
            broadcast({"type": "voice", "state": "tx_on", "t": self.tx_start})
        pcm = self._decode(pkt)
        self.frames.extend(pcm); self.last = time.time()
        if MIC:
            MIC.push(pcm)
        if len(self.raw) % 10 == 0 and pcm:
            rms = (sum(x * x for x in pcm) / len(pcm)) ** 0.5
            broadcast({"type": "voice", "state": "level", "rms": int(rms * 32768), "sec": round(len(self.frames) / self.RATE, 1)})

    def flush_loop(self):
        while True:
            time.sleep(0.03)
            if self.tx and time.time() - self.last > 0.12:
                self.tx = False
                tail = round(self.last - self.siri_up, 2) if self.siri_up and self.siri_up > self.tx_start else None
                broadcast({"type": "voice", "state": "tx_off", "dur": round(self.last - self.tx_start, 2), "tail": tail})
                if MIC:
                    MIC.tx_ended()
            if not (self.frames and time.time() - self.last > 0.6):
                continue
            frames, raw, lost = self.frames, self.raw, self.lost
            self.frames, self.raw = [], []
            tapped = self.siri_up and getattr(self, "siri_t0", 0) and self.siri_up - self.siri_t0 < 0.35
            if tapped or len(raw) < 10:          # a Siri tap (= Enter), not speech
                broadcast({"type": "voice", "state": "done_skip"}); continue
            self.n += 1
            base = os.path.join(DATA, "rec", time.strftime("%H%M%S") + f"-{self.n}")
            peak = max(1e-6, max(abs(x) for x in frames))
            # float -> 24-bit-ish safe int16 with headroom (no clipping): scale only if needed
            g = min(1.0, 0.98 / peak)
            with wave.open(base + "-raw.wav", "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.RATE)
                w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x * g)) * 32767)) for x in frames))
            with open(base + ".pkts", "wb") as f:
                for r in raw:
                    f.write(bytes([len(r)]) + r)
            out = base + "-raw.wav"
            name = os.path.basename(out)
            broadcast({"type": "voice", "state": "done", "url": "/rec/" + name, "raw": "/rec/" + os.path.basename(base) + "-raw.wav",
                       "sec": round(len(frames) / self.RATE, 1), "lost": lost, "clip": peak > 1.0})
            if self.mode() == "echo":
                player = shutil.which("pw-play") or shutil.which("paplay") or shutil.which("aplay")
                if player:
                    subprocess.Popen([player, out], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif not MIC:                        # fallback without PipeWire: transcribe the file
                threading.Thread(target=self.dictate, args=(out,), daemon=True).start()

    @staticmethod
    def mode():
        try:
            return json.load(open(os.path.join(DATA, "settings.json"))).get("siri", "dictate")
        except (OSError, ValueError):
            return "dictate"

    def dictate(self, wav):
        """Transcribe with the user's Voxtype setup (same engine/prompt as the pedal's PTT) and paste
        the text into the focused window the way Voxtype does (clipboard + Shift+Insert)."""
        env = dict(os.environ)
        try:
            for line in open(os.path.expanduser("~/.config/voxtype/env")):
                k, _, v = line.strip().partition("=")
                if k and not k.startswith("#"):
                    env[k.removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
        broadcast({"type": "voice", "state": "stt", "msg": "transkribiere …"})
        p = subprocess.run(["voxtype", "-q", "transcribe", wav], capture_output=True, text=True, env=env, timeout=90)
        lines = [l for l in p.stdout.splitlines() if l.strip() and not l.startswith(("Loading audio", "Audio format", "Processing "))]
        text = " ".join(l.strip() for l in lines).strip()
        if p.returncode != 0 or not text:
            err = (p.stderr.strip().splitlines() or [""])[-1][:160]
            print("voxtype failed:", p.returncode, p.stderr[-400:], flush=True)
            broadcast({"type": "voice", "state": "stt", "msg": ("Transkription fehlgeschlagen: " + err) if p.returncode else "(nichts erkannt)"})
            return
        subprocess.run(["wl-copy", "--", text], check=False)
        subprocess.run(["wtype", "-M", "shift", "-k", "Insert", "-m", "shift"], check=False)
        broadcast({"type": "voice", "state": "stt", "msg": "✓ " + text})


VOICE = None


class Mic:
    """The remote's voice as a PipeWire source ("Siri Remote Mic"), so Voxtype records it natively:
    Siri down -> default source = remote mic + `voxtype record start`; transmission over after
    release -> `voxtype record stop` and the previous default source comes back. Voxtype then
    shows its own indicator, transcribes and pastes exactly as with the pedal."""
    NAME = "siri_remote_mic"
    FRAME = 320 * 2                                    # 20 ms of s16le mono at 16 kHz

    def __init__(self):
        self.fifo = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "siri-remote-mic.fifo")
        for line in subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True).stdout.splitlines():
            if self.NAME in line:
                subprocess.run(["pactl", "unload-module", line.split()[0]], capture_output=True)
        if os.path.exists(self.fifo):
            os.unlink(self.fifo)
        r = subprocess.run(["pactl", "load-module", "module-pipe-source", f"source_name={self.NAME}",
                            f"file={self.fifo}", "format=s16le", "rate=16000", "channels=1",
                            "source_properties=device.description=Siri-Remote-Mic"], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        self.q, self.lock = bytearray(), threading.Lock()
        self.active, self.released, self.prev_source = False, False, None
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._guard_pedal, daemon=True).start()

    def _guard_pedal(self):
        """Moving Voxtype's stream makes PipeWire remember the remote mic for it. When Voxtype records
        without the remote (pedal, keybinding) and lands on the remote mic, move it back to the
        laptop's input, which also resets that memory."""
        p = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True)
        for line in p.stdout:
            if "'new' on source-output" not in line or self.active:
                continue
            time.sleep(0.05)
            me = next((l.split()[0] for l in subprocess.run(["pactl", "list", "short", "sources"], capture_output=True,
                       text=True).stdout.splitlines() if self.NAME in l), None)
            out = subprocess.run(["pactl", "list", "source-outputs"], capture_output=True, text=True).stdout
            for block in out.split("Source Output #")[1:]:
                if "voxtype" in block.lower() and me and f"Source: {me}\n" in block:
                    target = self.prev_source or subprocess.run(["pactl", "get-default-source"], capture_output=True,
                                                                text=True).stdout.strip()
                    if target and target != self.NAME:
                        self._run("pactl", "move-source-output", block.split()[0], target)

    def push(self, pcm):
        if self.active:
            data = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767)) for x in pcm)
            with self.lock:
                self.q += data

    def _writer(self):
        """Feed the pipe in real time: queued voice (bursts drained quickly), else silence."""
        fd = os.open(self.fifo, os.O_WRONLY)
        nxt = time.monotonic()
        while True:
            with self.lock:
                n = self.FRAME * (3 if len(self.q) > self.FRAME * 6 else 1)
                chunk = bytes(self.q[:n]); del self.q[:n]
            os.write(fd, chunk + b"\0" * (self.FRAME - len(chunk)) if len(chunk) < self.FRAME else chunk)
            nxt += 0.02
            time.sleep(max(0, nxt - time.monotonic()))

    def _run(self, *cmd):
        subprocess.run(list(cmd), capture_output=True, timeout=5)

    def start(self):
        if self.active:
            return
        out = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True).stdout.strip()
        self.prev_source = out if out and out != self.NAME else self.prev_source
        with self.lock:
            self.q.clear()
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
        """Siri released: stop right away (the remote's audio tail after release is only noise)."""
        if not self.active:
            return
        self.released = True
        self.tx_ended()

    def tx_ended(self):
        if self.active and self.released and not getattr(self, "finishing", False):
            self.finishing = True
            threading.Thread(target=self._finish, args=("stop",), daemon=True).start()

    def cancel(self):
        if self.active and not getattr(self, "finishing", False):
            self.finishing = True
            threading.Thread(target=self._finish, args=("cancel",), daemon=True).start()

    def _finish(self, how):
        time.sleep(0.12)                              # last buffered frames through the pipe
        self._run("voxtype", "record", how)
        self.active = self.finishing = False
        time.sleep(0.5)
        if self.prev_source:
            self._run("pactl", "set-default-source", self.prev_source)


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
BT_LIVE = {"connected": False, "percent": None, "power": None}
POWER = {0xAB: "lädt", 0xAF: "Akku", 0xBB: "eingesteckt (voll)"}


def bt_input_loop():
    """Own the remote's LE link via a raw ATT socket: enable reports (0xAF -> F0/F1 feature
    reports) and subscribe to buttons (0x3a), touch (0x3e), battery (0x2f) and power state (0x32)."""
    H_BTN, H_TOUCH, H_BATT, H_PWR = 0x3a, 0x3e, 0x2f, 0x32
    global BT_MAC
    scanner = None
    while True:
        s = None
        try:
            while not BT_MAC:
                BT_MAC = find_remote() or time.sleep(5)
            # wait until bluez auto-connects the woken remote (needs discovery running), then take the link over
            if not scanner or scanner.poll() is not None:
                scanner = subprocess.Popen(["bluetoothctl", "--timeout", "86400", "scan", "on"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # an explicit bluez Disconnect disables auto-connect; an explicit Connect re-arms it and
            # waits (kernel accept list resolves the remote's RPA) until the woken remote shows up
            while "Connected: yes" not in btctl("info", BT_MAC, t=3):
                subprocess.run(["bluetoothctl", "--timeout", "10", "connect", BT_MAC], capture_output=True, timeout=15)
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
                raise TimeoutError("takeover failed")
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
            for fh in (0x4e, 0x51):
                req(b"\x12" + struct.pack("<H", fh) + b"\xaf")
            for cccd in (0x37, 0x3b, 0x3f, 0x43, 0x47, 0x4b, 0x30, 0x33):
                req(b"\x12" + struct.pack("<H", cccd) + b"\x01\x00")
            for h, key in ((H_BATT, "percent"), (H_PWR, "power")):
                r = req(b"\x0a" + struct.pack("<H", h))
                if len(r) >= 2 and r[0] == 0x0b: BT_LIVE[key] = r[1]
            BT_LIVE["connected"] = True
            broadcast({"type": "status", "msg": f"Bluetooth verbunden: {BT_MAC}", "bus": "bluetooth"})
            broadcast({"type": "battery", **battery()})
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
                    if MOUSE:
                        MOUSE.click("select" in m["pressed"])
                        MOUSE.keys(m["pressed"])
                    if VOICE:
                        down = "siri" in m["pressed"]
                        if down and not getattr(VOICE, "siri_down", False): VOICE.siri_up = None; VOICE.siri_t0 = time.time()
                        if not down and getattr(VOICE, "siri_down", False): VOICE.siri_up = time.time()
                        VOICE.siri_down = down
                    m.update(dev="ble", bus="bluetooth", hex=v.hex(" "), id=0xFB, len=len(v), t=time.time())
                    broadcast(m)
                elif h == 0x36 and VOICE:
                    VOICE.feed(v)
                elif h == H_TOUCH:
                    if MOUSE:
                        MOUSE.touch(v)
                    broadcast({"type": "touch", "bus": "bluetooth", "hex": v.hex(" "), "b": list(v), "t": time.time()})
                elif h in (H_BATT, H_PWR) and v:
                    BT_LIVE["percent" if h == H_BATT else "power"] = v[0]
                    broadcast({"type": "battery", **battery()})
                else:
                    broadcast({"type": "raw", "bus": "bluetooth", "dev": "ble", "id": h, "len": len(v), "hex": v.hex(" ")})
        except Exception as e:
            with open(os.path.join(DATA, "bt.log"), "a") as lf:
                lf.write(f"{time.strftime('%H:%M:%S')} {type(e).__name__}: {e}\n")
            if BT_LIVE["connected"]:
                broadcast({"type": "status", "msg": f"Bluetooth getrennt ({e}) – Taste drücken zum Aufwecken", "bus": "bluetooth"})
            BT_LIVE["connected"] = False
        finally:
            if s:
                s.close()
        time.sleep(1)


# --- Battery state and page updates ---

def battery():
    """Charge state: USB link = charging; Bluetooth battery from kernel power_supply or BlueZ."""
    info = {"usb": any(b == "usb" for _, b in remotes()), "percent": None, "source": None,
            "bt": BT_LIVE["connected"], "status": None}
    if BT_LIVE["percent"] is not None:
        info["percent"] = BT_LIVE["percent"]; info["source"] = "bluetooth"
        info["status"] = {0xAB: "Charging", 0xBB: "Full"}.get(BT_LIVE["power"], "Discharging")
        info["power"] = POWER.get(BT_LIVE["power"], None)
        return info
    for ps in glob.glob("/sys/class/power_supply/hid-*"):
        try:
            if "0315" in open(ps + "/device/uevent").read() or "Remote" in open(ps + "/model_name").read():
                info["percent"] = int(open(ps + "/capacity").read()); info["source"] = "hid"
                st = open(ps + "/status").read().strip(); info["status"] = st
        except OSError:
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
                info["percent"] = buf[1]; info["source"] = "hid 0x47 (" + bus + ")"; break
            except OSError:
                pass
    if info["percent"] is None:
        try:
            out = subprocess.run(["bluetoothctl", "devices", "Connected"], capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                mac = line.split()[1]
                i = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=3).stdout
                if "0315" in i or "Remote" in i:
                    for l in i.splitlines():
                        if "Battery Percentage" in l:
                            info["percent"] = int(l.split("(")[1].rstrip(")")); info["source"] = "bluez"
        except Exception:
            pass
    return info


def page_version():
    """Reload open pages after source or HTML changes."""
    return str(max(os.path.getmtime(os.path.join(HERE, f)) for f in ("index.html", "remote.py")))


def battery_loop():
    """Publish page versions and changed battery state every five seconds."""
    last = None
    while True:
        broadcast({"type": "version", "v": page_version()})
        b = battery()
        if b != last:
            broadcast({"type": "battery", **b}); last = b
        time.sleep(5)


def btctl(*a, t=8):
    """Run a bounded BlueZ query, returning diagnostic text on failure."""
    try:
        return subprocess.run(["bluetoothctl", *a], capture_output=True, text=True, timeout=t).stdout
    except Exception as e:
        return str(e)


# --- HID diagnostics and SSE broadcasting ---

def broadcast(msg):
    """Queue an unchanged JSON event for every connected browser."""
    data = json.dumps(msg)
    with lock:
        for q in clients:
            q.put(data)


def decode(r):
    """Decode the thirteen button bits in HID report 0xFB."""
    if len(r) >= 3 and r[0] == 0xFB:
        bits = r[1] | (r[2] << 8)
        return {"type": "buttons", "pressed": [n for i, n in enumerate(BTN_BITS) if bits >> i & 1]}
    return {"type": "raw"}


def reader():
    """Stream hidraw reports for the viewer; BLE actions use the ATT path."""
    fds = {}
    denied = set()
    while True:
        for path, bus in remotes():
            if path not in fds:
                try:
                    fds[path] = (os.open(path, os.O_RDONLY | os.O_NONBLOCK), bus)
                    denied.discard(path)
                    broadcast({"type": "status", "msg": f"verbunden: {path} ({bus})", "dev": path, "bus": bus})
                except PermissionError:
                    if path in denied:
                        continue
                    denied.add(path)
                    broadcast({"type": "status", "msg": f"keine Rechte auf {path} – udev-Regel fehlt", "dev": path, "bus": bus})
        if not fds:
            time.sleep(1)
            continue
        r, _, _ = select.select([f for f, _ in fds.values()], [], [], 1)
        for path, (fd, bus) in list(fds.items()):
            if fd in r:
                try:
                    data = os.read(fd, 512)
                    if not data:
                        raise ConnectionError("hidraw device closed")
                except OSError:
                    os.close(fd); del fds[path]
                    broadcast({"type": "status", "msg": f"getrennt: {path}", "dev": path, "bus": bus})
                    continue
                m = decode(data)
                m.update(dev=path, bus=bus, hex=data.hex(" "), id=data[0], len=len(data), t=time.time())
                broadcast(m)


# --- Local HTTP server ---

class H(BaseHTTPRequestHandler):
    """Serve the German viewer, settings, recordings and SSE events on loopback."""
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        """Merge posted settings into settings.json (hot-reloaded by the mouse on next touch)."""
        if self.path != "/settings":
            self.send_response(404); self.end_headers(); return
        path = os.path.join(DATA, "settings.json")
        try:
            new = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            cur = json.load(open(path)) if os.path.exists(path) else {}
            cur.update({k: v for k, v in new.items() if isinstance(v, (int, float, bool, str))})
            tmp = path + ".tmp"
            json.dump(cur, open(tmp, "w"), indent=1); os.replace(tmp, path)
            if MOUSE:
                MOUSE.reload()
            self._json(cur)
        except (ValueError, OSError) as e:
            self._json({"error": str(e)}, 400)

    def do_GET(self):
        if self.path == "/settings":
            try:
                return self._json(json.load(open(os.path.join(DATA, "settings.json"))))
            except (OSError, ValueError):
                return self._json({})
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = queue.Queue()
            with lock:
                clients.append(q)
            q.put(json.dumps({"type": "version", "v": page_version()}))
            q.put(json.dumps({"type": "battery", **battery()}))
            q.put(json.dumps({"type": "status", "msg": "Geräte: " + (", ".join(f"{p} ({b})" for p, b in remotes()) or "keine")}))
            try:
                while True:
                    try:
                        self.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    clients.remove(q)
        else:
            if self.path.startswith("/rec/") and self.path.endswith(".wav") and "/" not in self.path[5:]:
                name, ctype = os.path.join(DATA, "rec", self.path[5:]), "audio/wav"
            else:
                name, ctype = os.path.join(HERE, "index.html"), "text/html; charset=utf-8"
            try:
                body = open(name, "rb").read()
            except OSError:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=battery_loop, daemon=True).start()
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
    print("http://127.0.0.1:8315", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8315), H).serve_forever()
