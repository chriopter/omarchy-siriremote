#!/usr/bin/env python3
"""Pair a gen-3 Siri Remote (USB-C, 2022) with BlueZ. Run as root.

BlueZ can drop the remote's pairing advert (no LE discoverable flags). Watch
raw adverts via btmon, then pair the selected LE random address with btmgmt.
"""
import re
import subprocess
import sys
import time
from contextlib import contextmanager

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import Gio, GLib

BLUEZ = "org.bluez"
AGENT_PATH = "/org/omarchy/SiriRemote/PairingAgent"
AGENT_IFACE = "org.bluez.Agent1"
DEVICE_IFACE = "org.bluez.Device1"
SIG = bytes.fromhex("4c 00 07 0d 02 15")
MAC = r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}"
PAIR_TIMEOUT = 25


class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class PairingError(Exception):
    pass


class PairingAgent(dbus.service.Object):
    def __init__(self, bus, bluez_owner):
        self.bus, self.bluez_owner = bus, bluez_owner
        self.disarm()
        super().__init__(bus, AGENT_PATH)

    def arm(self, device_path):
        self.device_path = device_path
        self.deadline = time.monotonic() + PAIR_TIMEOUT

    def disarm(self):
        self.device_path = None
        self.deadline = 0

    def check_sender(self, sender):
        if sender == self.bluez_owner:
            try:
                if self.bus.get_name_owner(BLUEZ) == sender:
                    return
            except dbus.DBusException:
                pass
        raise Rejected("Not the BlueZ daemon that registered this agent")

    def accept_pairing(self, device, sender):
        self.check_sender(sender)
        if self.device_path is None or device != self.device_path or time.monotonic() >= self.deadline:
            raise Rejected("Not the active Siri Remote pairing attempt")
        self.disarm()  # One affirmative pairing response at most per attempt.

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="", sender_keyword="sender")
    def RequestConfirmation(self, device, passkey, sender=None):
        self.accept_pairing(device, sender)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="", sender_keyword="sender")
    def RequestAuthorization(self, device, sender=None):
        # For a btmgmt pairing, BlueZ sees no D-Bus bonding request and sends
        # Just Works (confirm_hint) here, even though we initiated it locally.
        self.accept_pairing(device, sender)

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        raise Rejected("Service authorization is not part of pairing")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        raise Rejected("PIN pairing is not supported")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        raise Rejected("Passkey entry is not supported")

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        raise Rejected("PIN pairing is not supported")

    @dbus.service.method(AGENT_IFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        raise Rejected("Passkey entry is not supported")

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="", sender_keyword="sender")
    def Cancel(self, sender=None):
        self.check_sender(sender)
        self.disarm()

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="", sender_keyword="sender")
    def Release(self, sender=None):
        self.check_sender(sender)
        self.disarm()


def interface(bus, owner, path, name):
    return dbus.Interface(bus.get_object(owner, path, introspect=False), name)


@contextmanager
def system_bus():
    bus = dbus.SystemBus(private=True)
    try:
        yield bus
    finally:
        bus.close()


@contextmanager
def attempt_agent(owner):
    # btmgmt bypasses Device1.Pair, so bluetoothd uses the default agent.
    # Use a fresh connection for each attempt; closing it also unregisters
    # the agent on errors and prevents queued callbacks reaching any retry.
    with system_bus() as bus:
        agent = PairingAgent(bus, owner)
        manager = interface(bus, owner, "/org/bluez", "org.bluez.AgentManager1")
        path = dbus.ObjectPath(AGENT_PATH)
        try:
            manager.RegisterAgent(path, "NoInputNoOutput", timeout=5)
            manager.RequestDefaultAgent(path, timeout=5)
            yield agent
        finally:
            agent.disarm()
            agent.remove_from_connection()


def pair_device(owner, index, path, address):
    with attempt_agent(owner) as agent:
        # btmgmt sees confirmation events too. Supply an empty pipe (its
        # shell expects a pollable fd) so only the scoped agent can say yes.
        proc = Gio.Subprocess.new(
            ["btmgmt", "--index", str(index), "pair", "-c", "3", "-t", "2", address],
            Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE,
        )
        loop = GLib.MainLoop()
        result = None
        timed_out = False

        def finished(proc, reply):
            nonlocal result
            agent.disarm()
            try:
                _, out, _ = proc.communicate_utf8_finish(reply)
                if not timed_out and proc.get_successful():
                    match = re.search(rf"^Paired with ({MAC}) \(LE (?:Random|Public)\)$", out, re.MULTILINE)
                    if match:
                        result = match[1]
            except GLib.Error:
                pass
            finally:
                loop.quit()

        def expired(*_):
            nonlocal timed_out
            timed_out = True
            agent.disarm()
            proc.force_exit()
            return GLib.SOURCE_REMOVE

        agent.arm(path)
        timer = GLib.timeout_source_new(PAIR_TIMEOUT * 1000)
        timer.set_callback(expired)
        timer.attach(None)
        try:
            proc.communicate_utf8_async("", None, finished)
            loop.run()
        finally:
            agent.disarm()
            timer.destroy()
            proc.force_exit()
        return result


def powered_controller(bus, owner):
    objects = interface(bus, owner, "/", "org.freedesktop.DBus.ObjectManager").GetManagedObjects(timeout=5)
    indexes = [int(m[1]) for path, ifaces in objects.items()
               if (m := re.fullmatch(r"/org/bluez/hci(\d+)", path))
               and ifaces.get("org.bluez.Adapter1", {}).get("Powered")]
    if not indexes:
        raise PairingError("No powered BlueZ Bluetooth controller found")
    return min(indexes)


def siri_signature(data):
    """Check the start of an actual manufacturer-data field, not its text rendering."""
    while data and data[0]:
        size = data[0] + 1
        if size > len(data):
            return False
        if data[1] == 0xff and data[2:size].startswith(SIG):
            return True
        data = data[size:]
    return False


def remote_adverts(lines):
    """Read btmon reports on the controller already selected with --index."""
    report = None
    for raw in lines:
        line = raw.strip()
        if raw and not raw[0].isspace():  # next monitor packet
            report = None
        elif re.fullmatch(r"LE (?:Extended )?Advertising Report \(0x0[2d]\)", line):
            report = False  # advertising packet, waiting for an entry
        elif report is not None:
            if line.startswith(("Event type:", "Entry ")):
                report = {"random": False, "address": None, "rssi": None, "data": None, "size": 0}
            elif not report:
                continue
            elif line.startswith("Address type:"):
                report["random"] = line == "Address type: Random (0x01)"
            elif m := re.match(rf"Address: ({MAC})(?: |$)", line):
                report["address"] = m[1]
            elif m := re.match(r"RSSI: (-?\d+) dBm\b", line):
                report["rssi"] = int(m[1])
            elif m := re.fullmatch(r"Advertising Data\[(\d+)\]:", line):
                report["size"] = int(m[1])
                report["data"] = bytearray()
            elif report["data"] is not None and len(report["data"]) < report["size"]:
                # btmon prints 16 hex bytes then an ASCII column. Read at
                # most the remaining advertised size (not the ASCII text).
                count = min(16, report["size"] - len(report["data"]))
                if m := re.match(rf"(?:[0-9a-f]{{2}} ){{{count - 1}}}[0-9a-f]{{2}}\b", line):
                    report["data"].extend(bytes.fromhex(m[0]))
                else:
                    report["data"], report["size"] = None, 0

            if (report and report["address"] and report["random"] and report["rssi"] is not None
                    and report["data"] and len(report["data"]) == report["size"]):
                if siri_signature(report["data"]):
                    yield report["address"], report["rssi"]
                report = False


def trust_device(bus, owner, path, advertised, paired):
    props = interface(bus, owner, path, "org.freedesktop.DBus.Properties")
    # BlueZ processes the new bond separately from btmgmt. It can update
    # Device1.Address to the identity address without changing its path.
    for _ in range(20):
        if bus.get_name_owner(BLUEZ) != owner:
            break
        try:
            device = props.GetAll(DEVICE_IFACE, timeout=3)
        except dbus.DBusException as exc:
            if exc.get_dbus_name() != "org.freedesktop.DBus.Error.UnknownObject":
                raise
            device = {}
        identity = device.get("Address")
        if device.get("Paired") and identity and paired in (advertised, identity):
            props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True), timeout=3)
            return identity
        time.sleep(0.1)
    raise PairingError("Pairing completed but BlueZ did not confirm the bond for this device; it was not trusted")


def main():
    DBusGMainLoop(set_as_default=True)
    with system_bus() as bus:
        owner = bus.get_name_owner(BLUEZ)
        index = powered_controller(bus, owner)
        adapter = f"/org/bluez/hci{index}"
        discovery = interface(bus, owner, adapter, "org.bluez.Adapter1")
        with subprocess.Popen(["stdbuf", "-oL", "btmon", "--index", str(index), "--color", "never"],
                              stdout=subprocess.PIPE, text=True, errors="replace") as mon:
            try:
                discovery.StartDiscovery(timeout=5)
                print("Remote close to the laptop, hold BACK + VOLUME UP for a few seconds …", flush=True)
                tried, said = {}, float("-inf")
                for address, rssi in remote_adverts(mon.stdout):
                    now = time.monotonic()
                    if rssi < -72:
                        if now - said > 1:
                            print(f"Remote seen ({rssi} dBm), bring it closer", flush=True)
                            said = now
                        continue
                    if now < tried.get(address, 0):
                        continue
                    tried[address] = now + PAIR_TIMEOUT + 10
                    print(f"Remote found: {address} ({rssi} dBm), pairing …", flush=True)
                    path = f"{adapter}/dev_{address.replace(':', '_')}"
                    if paired := pair_device(owner, index, path, address):
                        mac = trust_device(bus, owner, path, address, paired)
                        print(f"Paired: {mac}", flush=True)
                        return 0
                    print("Pairing failed, hold BACK + VOLUME UP again", flush=True)
            finally:
                mon.terminate()
                # Closing the private discovery connection also drops its scan session.
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (dbus.DBusException, GLib.Error, OSError, PairingError) as exc:
        sys.exit(f"Pairing setup failed: {exc}")
