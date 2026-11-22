# How it works

## Pairing

The remote's pairing advert has no LE discoverable flags, so BlueZ drops it
and `bluetoothctl scan` never lists the remote. `src/pair.py` watches raw adverts with `btmon`
for Apple manufacturer data `4c00 070d 0215` (PnP 0x0315), pairs with `btmgmt`
(LE random, NoInputNoOutput) and confirms Just Works through a bluetoothctl agent, so
bluetoothd stores the bond like any other device.

## Input

BlueZ's HID-over-GATT only exposes a vendor report, so buttons never reach the
kernel. When BlueZ reconnects the woken remote, the service takes the link over with a raw
L2CAP ATT socket, writes `0xAF` to the feature reports F0/F1 (handles `0x4e`/`0x51`) and
subscribes to the reports: buttons `0x3a` (the 13 bits of HID report `0xFB`), touch
`0x3e`, voice `0x36`, battery `0x2f`, charging state `0x32`.

## Touch

Signed 12-bit coordinates `x = b4 | (b5 & 0x0f) << 8`, `y = b5 >> 4 | b6 << 4`;
byte 8 is the contact value, so hovering fingers are ignored. Motion uses touch-down
settling, noise and jump filters, `gain * min(cap, 1 + k * speed)` acceleration and a 4 ms
smoothing pump. All values are sliders on the page and live in `settings.json`.

## Voice

While Siri is held the remote streams `[4-byte header][length][Opus CELT-WB 20 ms]`
(16-bit frame counter in header bytes 2–3). The service decodes it with libopus at 16 kHz,
conceals short gaps and feeds a PipeWire pipe source "Siri-Remote-Mic". On Siri down it
points Voxtype's capture stream at that source and runs `voxtype record start`; on release
`voxtype record stop`. Voxtype used without the remote (pedal, keybinding) is moved back to
the regular input.

Settings, recordings and logs live in `$SIRI_REMOTE_DATA` (default
`~/.local/share/omarchy-siriremote`).
