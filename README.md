# omarchy-siriremote

Use the Apple Siri Remote (3rd generation, USB-C, 2022) as a couch controller for
[Omarchy](https://omarchy.org): touch surface as mouse, Siri button for
[Voxtype](https://github.com/peteonrails/voxtype) dictation through the remote's own
microphone, media keys, and [herdr](https://herdr.dev) / Hyprland navigation.

One dependency-free Python file (`remote.py`) running as a systemd user service, plus a
live page at <http://127.0.0.1:8315> showing buttons, touch, battery, the microphone and
touch tuning sliders.

## Buttons

| Control | Action |
| --- | --- |
| Touch surface | Mouse |
| Ring centre press | Left click |
| Ring ← / → | Previous / next herdr tab |
| Ring ↑ / ↓ | Previous / next herdr workspace |
| TV | Next pane in the current herdr tab |
| Power | Next Omarchy workspace (what Super+Tab does) |
| Back | Escape |
| Siri tap | Enter |
| Siri hold | Voxtype dictation with the remote's microphone |
| Play/Pause 1× / 2× / 3× | Play/pause at once / next track / previous track |
| Volume + / −, Mute | Media keys (Omarchy shows its OSD) |

herdr is driven through its socket API, the workspace switch through `hyprctl`, dictation
through Voxtype itself (its indicator, transcription and paste), media through the media
keys Omarchy already binds. Enter, Escape and the mouse come from a uinput device.

## Install

```sh
git clone https://github.com/chriopter/omarchy-siriremote
cd omarchy-siriremote
./install.sh
```

`install.sh` asks for sudo to add udev access rules (hidraw `05AC:0315` / `004C:0315`,
`/dev/uinput`), pairs the remote if none is paired yet (keep it close and hold
**Back + Volume Up**), and installs the user unit `siri-remote.service`.
Start it with `systemctl --user start siri-remote.service`; logs via
`journalctl --user -u siri-remote.service`.

Needs BlueZ (`bluetoothctl`, `btmon`, `btmgmt`), PipeWire with `pactl`, `libopus`,
Python 3.14, Voxtype, herdr and Hyprland as shipped with Omarchy.

## How it works

**Pairing.** The remote's pairing advert has no LE discoverable flags, so BlueZ drops it
and `bluetoothctl scan` never lists the remote. `pair.py` watches raw adverts with `btmon`
for Apple manufacturer data `4c00 070d 0215` (PnP 0x0315), pairs with `btmgmt`
(LE random, NoInputNoOutput) and confirms Just Works through a bluetoothctl agent, so
bluetoothd stores the bond like any other device.

**Input.** BlueZ's HID-over-GATT only exposes a vendor report, so buttons never reach the
kernel. When BlueZ reconnects the woken remote, the service takes the link over with a raw
L2CAP ATT socket, writes `0xAF` to the feature reports F0/F1 (handles `0x4e`/`0x51`) and
subscribes to the reports: buttons `0x3a` (the 13 bits of HID report `0xFB`), touch
`0x3e`, voice `0x36`, battery `0x2f`, charging state `0x32`.

**Touch.** Signed 12-bit coordinates `x = b4 | (b5 & 0x0f) << 8`, `y = b5 >> 4 | b6 << 4`;
byte 8 is the contact value, so hovering fingers are ignored. Motion uses touch-down
settling, noise and jump filters, `gain * min(cap, 1 + k * speed)` acceleration and a 4 ms
smoothing pump. All values are sliders on the page and live in `settings.json`.

**Voice.** While Siri is held the remote streams `[4-byte header][length][Opus CELT-WB 20 ms]`
(16-bit frame counter in header bytes 2–3). The service decodes it with libopus at 16 kHz,
conceals short gaps and feeds a PipeWire pipe source "Siri-Remote-Mic". On Siri down it
points Voxtype's capture stream at that source and runs `voxtype record start`; on release
`voxtype record stop`. Voxtype used without the remote (pedal, keybinding) is moved back to
the regular input.

Settings, recordings and logs live in `$SIRI_REMOTE_DATA` (default
`~/.local/share/omarchy-siriremote`).

Not affiliated with Apple. Siri and Apple TV are trademarks of Apple Inc.
