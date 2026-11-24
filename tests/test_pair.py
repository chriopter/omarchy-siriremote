"""Offline regression tests. No system bus, kernel pairing, or controller access."""
import io
import subprocess
import sys
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

import dbus

from src import pair

ADDRESS = "42:11:22:33:44:55"
IDENTITY = "C2:AA:BB:CC:DD:EE"
PATH = "/org/bluez/hci2/dev_42_11_22_33_44_55"
OWNER = ":1.12"


def advert(extended=False, address_type="Random (0x01)", payload=None):
    if payload is None:
        payload = bytes.fromhex("02 01 00 07 ff 4c 00 07 0d 02 15")
    report = "Extended Advertising Report (0x0d)" if extended else "Advertising Report (0x02)"
    entry = "Entry 0" if extended else "Event type: Connectable undirected - ADV_IND (0x00)"
    rssi = "        RSSI: -50 dBm (0xce)\n"
    dump = "".join(f"        {payload[i:i+16].hex(' '):48} ................\n"
                   for i in range(0, len(payload), 16))
    return (f"> HCI Event: LE Meta Event (0x3e) plen 27\n      LE {report}\n"
            f"        {entry}\n        Address type: {address_type}\n        Address: {ADDRESS}\n"
            + (rssi if extended else "")
            + f"        Advertising Data[{len(payload)}]:\n{dump}"
            + ("" if extended else rssi))


class AdvertTests(unittest.TestCase):
    def test_legacy_and_extended_reports(self):
        payload = bytes.fromhex("0a 09 31 32 33 34 35 36 37 38 39 07 ff 4c 00 07 0d 02 15")
        self.assertEqual(list(pair.remote_adverts(io.StringIO(advert() + advert(True, payload=payload)))),
                         [(ADDRESS, -50), (ADDRESS, -50)])

    def test_ignores_nonrandom_and_nonmanufacturer_fields(self):
        nonmanufacturer = bytes.fromhex("07 09 4c 00 07 0d 02 15")
        self.assertEqual(list(pair.remote_adverts(io.StringIO(
            advert(address_type="Public (0x00)") + advert(payload=nonmanufacturer)))), [])

    def test_fields_do_not_carry_into_next_report(self):
        data_without_rssi = advert().split("        RSSI:")[0]
        next_report = f"        Event type: ADV_IND\n        Address: {ADDRESS}\n        RSSI: -40 dBm\n"
        self.assertEqual(list(pair.remote_adverts(io.StringIO(data_without_rssi + next_report))), [])


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.bus = Mock()
        self.bus.get_name_owner.return_value = OWNER
        self.agent = pair.PairingAgent(self.bus, OWNER)

    def test_only_exact_bluez_sender_device_and_active_attempt(self):
        with self.assertRaises(pair.Rejected):
            self.agent.RequestAuthorization(PATH, sender=OWNER)
        self.agent.arm(PATH)
        for path, sender in ((PATH, ":1.13"), (PATH.replace("hci2", "hci1"), OWNER),
                             (PATH + "_00", OWNER), (PATH.replace("55", "56"), OWNER)):
            with self.subTest(path=path, sender=sender), self.assertRaises(pair.Rejected):
                self.agent.RequestAuthorization(path, sender=sender)
        self.agent.RequestAuthorization(PATH, sender=OWNER)
        with self.assertRaises(pair.Rejected):
            self.agent.RequestConfirmation(PATH, 0, sender=OWNER)

    def test_confirmation_is_supported_but_credentials_and_services_are_not(self):
        self.agent.arm(PATH)
        for method, args in (("AuthorizeService", (PATH, "1812")), ("RequestPinCode", (PATH,)),
                             ("RequestPasskey", (PATH,)), ("DisplayPinCode", (PATH, "0000")),
                             ("DisplayPasskey", (PATH, 0, 0))):
            with self.subTest(method=method), self.assertRaises(pair.Rejected):
                getattr(self.agent, method)(*args)
        self.agent.RequestConfirmation(PATH, 0, sender=OWNER)

    def test_owner_change_expiration_cancel_and_release(self):
        self.agent.arm(PATH)
        self.bus.get_name_owner.return_value = ":1.13"
        for sender in (OWNER, ":1.13"):
            with self.assertRaises(pair.Rejected):
                self.agent.RequestAuthorization(PATH, sender=sender)
        self.bus.get_name_owner.return_value = OWNER
        with patch.object(pair.time, "monotonic", return_value=self.agent.deadline):
            with self.assertRaises(pair.Rejected):
                self.agent.RequestAuthorization(PATH, sender=OWNER)
        for method in (self.agent.Cancel, self.agent.Release):
            self.agent.arm(PATH)
            with self.assertRaises(pair.Rejected):
                method(sender=":1.13")
            method(sender=OWNER)
            with self.assertRaises(pair.Rejected):
                self.agent.RequestAuthorization(PATH, sender=OWNER)


class PairAttemptTests(unittest.TestCase):
    def run_fake_child(self, code, timeout=3):
        agent = Mock()
        spawned = []
        make_proc = pair.Gio.Subprocess.new

        def create(args, flags):
            spawned.append(args)
            return make_proc([sys.executable, "-c", code], flags)

        with patch.object(pair, "attempt_agent", return_value=nullcontext(agent)), \
                patch.object(pair.Gio.Subprocess, "new", side_effect=create), \
                patch.object(pair, "PAIR_TIMEOUT", timeout):
            result = pair.pair_device(OWNER, 2, PATH, ADDRESS)
        self.assertEqual(spawned, [["btmgmt", "--index", "2", "pair", "-c", "3", "-t", "2", ADDRESS]])
        agent.arm.assert_called_once_with(PATH)
        self.assertEqual(agent.mock_calls[-1], unittest.mock.call.disarm())
        return result

    def test_success_requires_result_and_no_terminal_input(self):
        code = f"import sys; assert not sys.stdin.read(); print('Paired with {ADDRESS} (LE Random)')"
        self.assertEqual(self.run_fake_child(code), ADDRESS)
        self.assertIsNone(self.run_fake_child(code + "; sys.exit(1)"))
        self.assertIsNone(self.run_fake_child(f"print('Pairing with {ADDRESS} (LE Random) failed')"))

    def test_timeout_clears_attempt(self):
        self.assertIsNone(self.run_fake_child("import time; time.sleep(30)", timeout=1))

    def test_agent_disarmed_and_connection_closed_on_registration_failure(self):
        bus, manager = Mock(), Mock()
        manager.RequestDefaultAgent.side_effect = dbus.DBusException("denied")
        with patch.object(pair.dbus, "SystemBus", return_value=bus), patch.object(pair, "interface", return_value=manager):
            with self.assertRaises(dbus.DBusException), pair.attempt_agent(OWNER):
                self.fail("registration failure must not start a pairing attempt")
        bus._unregister_object_path.assert_called_once_with(pair.AGENT_PATH)
        bus.close.assert_called_once()


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.bus, self.props = Mock(), Mock()
        self.bus.get_name_owner.return_value = OWNER
        self.props.GetAll.return_value = {"Paired": True, "Address": IDENTITY}
        self.proxy = self.enterContext(patch.object(pair, "interface", return_value=self.props))
        self.enterContext(patch.object(pair.time, "sleep"))

    def test_resolved_identity_is_trusted_on_same_device_path(self):
        self.assertEqual(pair.trust_device(self.bus, OWNER, PATH, ADDRESS, IDENTITY), IDENTITY)
        self.proxy.assert_called_once_with(self.bus, OWNER, PATH, "org.freedesktop.DBus.Properties")
        self.props.Set.assert_called_once_with(pair.DEVICE_IFACE, "Trusted", dbus.Boolean(True), timeout=3)

    def test_no_trust_on_mismatched_result_unpaired_device_or_new_owner(self):
        with self.assertRaises(pair.PairingError):
            pair.trust_device(self.bus, OWNER, PATH, ADDRESS, "C2:00:00:00:00:00")
        self.props.GetAll.return_value["Paired"] = False
        with self.assertRaises(pair.PairingError):
            pair.trust_device(self.bus, OWNER, PATH, ADDRESS, ADDRESS)
        self.props.GetAll.reset_mock()
        self.bus.get_name_owner.return_value = ":1.13"
        with self.assertRaises(pair.PairingError):
            pair.trust_device(self.bus, OWNER, PATH, ADDRESS, ADDRESS)
        self.props.GetAll.assert_not_called()
        self.props.Set.assert_not_called()