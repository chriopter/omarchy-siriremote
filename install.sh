#!/usr/bin/env bash
# Run as your desktop user. Only device rules and pairing need root.
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if (( EUID == 0 )); then
    echo 'Run ./install.sh as your desktop user, not as root.' >&2
    exit 1
fi
for cmd in python3 bluetoothctl btmon btmgmt stdbuf sudo systemctl udevadm modprobe pactl; do
    command -v "$cmd" >/dev/null || { echo "Missing command: $cmd" >&2; exit 1; }
done

# A 70-* rule runs before systemd's seat-late rule applies uaccess ACLs.
sudo tee /etc/udev/rules.d/70-siri-remote.rules >/dev/null <<'RULES'
# Apple Siri Remote gen 3: USB 05AC:0315, Bluetooth 004C:0315
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", KERNELS=="*:05AC:0315.*", TAG+="uaccess"
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", KERNELS=="*:004C:0315.*", TAG+="uaccess"
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess"
RULES
# Ensure the virtual input device also exists after reboot.
sudo tee /etc/modules-load.d/siri-remote.conf >/dev/null <<'MODULES'
uinput
MODULES
sudo modprobe uinput
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
sudo udevadm trigger --subsystem-match=misc --sysname-match=uinput
sudo udevadm settle

# Match the same paired-device modalias as remote.py; do not pair again on reinstall.
paired=false
while read -r kind mac rest; do
    [[ "$kind" == Device ]] || continue
    if [[ $(bluetoothctl info "$mac") == *p0315* ]]; then
        paired=true
        break
    fi
done <<< "$(bluetoothctl devices Paired)"
if [[ "$paired" == false ]]; then
    sudo "$(command -v python3)" "$project_dir/pair.py"
fi

unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p -- "$unit_dir"
# Quote paths for systemd directives (including literal specifier/variable characters).
unit_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    printf '"%s"' "$value"
}
exec_path=$project_dir/remote.py
exec_path=${exec_path//\$/\$\$}
cat > "$unit_dir/siri-remote.service" <<UNIT
[Unit]
Description=Apple Siri Remote gen 3 Bluetooth LE driver
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$(unit_quote "$project_dir")
ExecStart=$(unit_quote "$(command -v python3)") $(unit_quote "$exec_path")
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
UNIT
systemctl --user daemon-reload
systemctl --user enable siri-remote.service
printf '%s\n' 'Installed and enabled siri-remote.service; no service was started or restarted.' \
    'Start when ready: systemctl --user start siri-remote.service' \
    'Viewer: http://127.0.0.1:8315'
