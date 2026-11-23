#!/usr/bin/env python3
"""Pair a gen-3 Siri Remote (USB-C, 2022) with BlueZ. Run as root; hold BACK + VOLUME UP on the remote.

BlueZ drops the remote's pairing advert (no LE discoverable flags), so bluetoothctl never lists it.
This watches raw adverts via btmon for Apple manufacturer data 4c00 070d 0215 (PnP 0x0315), pairs
with btmgmt (LE random, NoInputNoOutput) and answers the just-works confirmation through a
bluetoothctl agent, so bluetoothd stores the bond (keys, IRK) like any other device.
"""
import re, subprocess, sys, threading, time

SIG = "4c 00 07 0d 02 15"   # btmon wraps the hex line right after this


def agent():
    """Default agent that says yes to the just-works confirmation."""
    p = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    p.stdin.write("agent NoInputNoOutput\ndefault-agent\nscan on\n"); p.stdin.flush()

    def answer():
        last = 0
        for line in p.stdout:
            if re.search(r"Accept pairing|Authorize service|Confirm passkey|\(yes/no\)", line) and time.time() > last:
                last = time.time() + 3
                p.stdin.write("yes\n"); p.stdin.flush()
    threading.Thread(target=answer, daemon=True).start()
    return p


def main():
    print("Remote close to the laptop, hold BACK + VOLUME UP for a few seconds …", flush=True)
    ag = agent()
    mon = subprocess.Popen(["stdbuf", "-oL", "btmon"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    addr = found = None
    tried, said = {}, 0
    try:
        for raw in mon.stdout:
            line = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            # btmon prints a report's address, then its data, then its RSSI: judge the
            # signal of the report that carried the remote's advert, at its RSSI line.
            if m := re.search(r"Address: (([0-9A-F]{2}:){5}[0-9A-F]{2})", line):
                addr, found = m.group(1), None; continue
            if addr and SIG in line:
                found = addr; continue
            r = re.search(r"RSSI: (-\d+) dBm", line)
            if not (r and found) or time.time() < tried.get(found, 0):
                continue
            rssi, addr, found = int(r.group(1)), found, None
            if rssi < -72:
                if time.time() - said > 1:       # every advert counts; say it once a second
                    print(f"Remote seen ({rssi} dBm), bring it closer", flush=True); said = time.time()
                continue
            tried[addr] = time.time() + 10
            print(f"Remote found: {addr} ({rssi} dBm), pairing …", flush=True)
            try:
                p = subprocess.run(["btmgmt", "pair", "-c", "3", "-t", "2", addr], input="yes\n" * 5,
                                   capture_output=True, text=True, timeout=25)
                out = p.stdout + p.stderr
            except subprocess.TimeoutExpired:
                out = "timeout"
            if m := re.search(r"Paired with (([0-9A-F]{2}:){5}[0-9A-F]{2})", out):
                mac = m.group(1)
                subprocess.run(["bluetoothctl", "trust", mac], capture_output=True)
                print(f"Paired: {mac}", flush=True)
                return 0
            print("Pairing failed, hold BACK + VOLUME UP again", flush=True)
    finally:
        mon.kill(); ag.kill()
    return 1


if __name__ == "__main__":
    sys.exit(main())
