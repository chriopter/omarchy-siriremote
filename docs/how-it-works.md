# How it works

## Pairing

The remote's pairing advert has no LE discoverable flags, so BlueZ can drop it.
`src/pair.py` watches raw adverts with `btmon` for Apple manufacturer data
`4c00 070d 0215` (PnP 0x0315), then pairs with `btmgmt` (LE random, NoInputNoOutput)
on the lowest-numbered powered BlueZ controller. The temporary D-Bus agent accepts
at most one pairing confirmation from BlueZ for that controller and the exact
selected device, only during that pairing attempt. It never authorizes services.
The advert and signal strength select the candidate; Just Works does not authenticate it.

## Input

BlueZ's HID-over-GATT only exposes a vendor report, so buttons never reach the
kernel. When BlueZ reconnects the woken remote, the service takes the link over with a raw
L2CAP ATT socket, writes `0xAF` to the feature reports F0/F1 (handles `0x4e`/`0x51`) and
subscribes to the reports: buttons `0x3a` (the 13 bits of HID report `0xFB`), touch
`0x3e`, voice `0x36`, battery `0x2f`, charging state `0x32`.

## Link

The service scans (`bluetoothctl scan on`) only while it is looking for the remote and stops
the scan once the takeover succeeds: a running scan takes radio time from every Bluetooth
device. After the remote goes away it looks for two minutes, then stops scanning. It still checks
for an existing BlueZ connection every five seconds. *Search* in the plugin card (SIGUSR1) starts a new scan. If the takeover keeps failing
for 15 s, BlueZ is stuck and the adapter is power-cycled.

`bin/fast-link` sets a 15 ms LE connection interval in `/etc/bluetooth/main.conf` (Apple
allows no shorter one). The remote sends one report per connection event; voice needs 50 a
second (20 ms Opus frames), so at 30 ms it falls behind while you speak (3.9 s of speech took
5.1 s to arrive) and at 15 ms it keeps up. Touch reports also arrive sooner.

## Touch

Signed 12-bit coordinates `x = b4 | (b5 & 0x0f) << 8`, `y = b5 >> 4 | b6 << 4`; bytes 1–2 are
the remote's clock (~2 kHz), byte 8 the contact value (hovering fingers stay below 150), byte 9
the contact size. The driver

- ignores the first `land_s` of a touch while the finger lands, and stops as soon as the
  contact value drops or the size collapses (the finger lifting slides the position);
- subtracts motion that only comes from the contact size changing (`roll`: a rolling thumb);
- scales speed, measured on the remote's clock, by `gain`, accelerates fast strokes (`k`) and
  slows small ones (`precision`);
- plays each radio burst out evenly over its own time, `smooth` behind, through a 4 ms pump.

The sliders in the plugin card write these values to `settings.json`; they are read again when
the next touch begins.

## Buttons and menu

Ring arrows go to herdr's socket API when a herdr window has focus (neighbouring pane, at the
edge tab or workspace), otherwise they are arrow keys. Play/Pause talks MPRIS to the player
that is playing. Power held opens a menu (display scaling, speaker, voice input) that the
plugin draws in the middle of the screen; the ring drives it.

## Voice

While Siri is held the remote streams `[4-byte header][length][Opus CELT-WB 20 ms]`
(16-bit frame counter in header bytes 2–3). The service decodes it with libopus at 48 kHz and
conceals short gaps. On Siri down it loads a PipeWire pipe source "Siri-Remote-Mic", makes it
the default input and runs `voxtype record start`; on release it lets the voice already
received through the pipe (0.3 s), runs `voxtype record stop`, restores the input and unloads the source. PipeWire's
pipe source buffers a fixed 8192 samples, 512 ms at the stream's native 16 kHz; the source runs
at 96 kHz (Opus decodes at 48 kHz, doubled), which keeps it at 85 ms. The pipe is kept at 4 KiB, and silence is dropped while nobody records, so
speech is not held back behind buffered audio. Dictating without the
remote never sees the remote's microphone.

## Plugin

The driver writes its live state (link, searching, battery, charging, listening, menu, radio interval) to
`$XDG_RUNTIME_DIR/omarchy-siriremote/state.json` and removes it when it stops. The bar
plugin (`manifest.json`, `plugin/`) watches that file and writes the touch settings to
`$SIRI_REMOTE_DATA/settings.json` (default `~/.local/share/omarchy-siriremote`). Logs go to
the journal.
