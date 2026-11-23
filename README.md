# omarchy-siriremote

![Siri Remote buttons and what they do in Omarchy](preview.png)

The Apple Siri Remote (3rd generation, USB-C) as a couch controller for [Omarchy](https://omarchy.org): the touch surface moves the mouse, the Siri button dictates through the remote's own microphone with [Voxtype](https://github.com/peteonrails/voxtype), and the ring navigates [herdr](https://herdr.dev). A bar widget shows connection and battery and tunes the touch surface.

## Install

```sh
omarchy plugin add https://github.com/chriopter/omarchy-siriremote --enable
~/.config/omarchy/plugins/chriopter.siriremote/bin/setup
```

The first line adds the bar widget, the second the driver: udev rules, pairing (hold **Back + Volume Up** when asked) and the systemd user service `siri-remote.service`. The widget's *Set up* button runs the same script.

Optional, for smoother touch: `bin/fast-link` sets a 15–30 ms Bluetooth LE connection interval for all devices (edits `/etc/bluetooth/main.conf`, restarts bluetooth).

Needs BlueZ, PipeWire, libopus and Python 3; Voxtype, herdr and Hyprland as shipped with Omarchy.

## Buttons

| Button | Action |
| --- | --- |
| Touch surface, press | Mouse, left click |
| Ring ← → ↑ ↓ | Arrow keys; in herdr the next pane, at the edge the next tab (← →) or workspace (↑ ↓) |
| TV | Enter |
| Back | Escape |
| Power | Next workspace; held: settings menu |
| Siri, held | Dictation with Voxtype |
| Play/Pause | Pause or play; twice: next track |
| Volume, Mute | Media keys |

The settings menu opens in the middle of the screen: display scaling, speaker and voice input. ↑ ↓ choose, ← → change, Back closes.

## Bar widget

Click the widget for its card: connection, battery, radio interval, and sliders for speed, acceleration, fine control, thumb-roll filter, smoothing and landing. *Search* looks for the remote again after the driver stopped searching (it does after two minutes, so the remote can sleep). *Restart*, *Logs* and *Reset* are always there.

## Remove

```sh
~/.config/omarchy/plugins/chriopter.siriremote/bin/remove   # --unpair also forgets the remote
omarchy plugin remove chriopter.siriremote
```

`bin/remove` stops the driver, deletes its service, udev rules and settings, and undoes `bin/fast-link`.

## Development

```sh
ln -s "$PWD" ~/.config/omarchy/plugins/chriopter.siriremote
omarchy plugin enable chriopter.siriremote
bin/setup    # driver
bin/dev      # driver in the foreground instead of the service
bin/update   # pull and restart the driver
```

Behind a symlinked plugin folder the shell does not reload on its own: run `omarchy restart shell` after changing `plugin/`. How pairing, touch, voice and the menu work: [docs/how-it-works.md](docs/how-it-works.md).

MIT licensed. Not affiliated with Apple; Siri and Apple TV are trademarks of Apple Inc.
