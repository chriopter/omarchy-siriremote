# omarchy-siriremote

Use the Apple Siri Remote (3rd generation, USB-C) as a couch controller for [Omarchy](https://omarchy.org): touch surface as mouse, Siri button for [Voxtype](https://github.com/peteonrails/voxtype) dictation through the remote's own microphone, media keys, and [herdr](https://herdr.dev) / Hyprland navigation. Live page with buttons, battery, microphone and touch tuning at <http://127.0.0.1:8315>.

## Buttons

| Control | Action |
| --- | --- |
| Touch surface / ring centre | Mouse / left click |
| Ring ← → / ↑ ↓ | Previous/next herdr tab / workspace |
| TV | Next pane in the current herdr tab |
| Power | Next Omarchy workspace |
| Back | Escape |
| Siri tap / hold | Enter / Voxtype dictation with the remote's microphone |
| Play/Pause 1× / 2× / 3× | Play/pause / next track / previous track |
| Volume + − / Mute | Media keys |

## Development

```sh
bin/setup   # udev rules, pairing (hold Back + Volume Up), systemd user service
bin/dev     # run in the foreground instead of the service
bin/update  # pull and restart the service
```

Needs BlueZ, PipeWire (`pactl`), libopus, Python 3.14, and Voxtype, herdr and Hyprland as shipped with Omarchy. Pairing, input, touch and voice details: [docs/how-it-works.md](docs/how-it-works.md).

Not affiliated with Apple. Siri and Apple TV are trademarks of Apple Inc.
