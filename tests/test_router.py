from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from deck_audio import (
    AudioError, Graph, MARKER, MONITOR, SINK, Router, RoutingJournal, bluetooth_outputs,
    connected_audio, loop_args, parse_args,
)


def sink(name, index, **props):
    return {
        "name": name, "index": index, "description": name,
        "properties": {"object.serial": str(index), **props},
    }


def headset(name, index):
    return sink(name, index, **{"device.api": "bluez5"})


class FakePulse:
    cookie = "server-1"

    def __init__(self):
        self.sinks = {"speakers": sink("speakers", 4, **{"device.api": "alsa"})}
        self.modules = {}
        self.inputs = {}
        self.selected = "speakers"
        self.next_id = 30
        self.fail = set()
        self.unlinked = set()
        self.default_changes = []

    def check(self):
        pass

    def graph(self):
        return Graph(list(self.sinks.values()), list(self.modules.values()), list(self.inputs.values()))

    def load(self, module, args):
        config = parse_args(args)
        if config.get("sink") in self.fail:
            raise AudioError("отказ транспорта")
        index = self.next_id
        self.next_id += 1
        self.modules[index] = dict(name=module, index=index, argument=args)
        if module == "module-null-sink":
            self.sinks[SINK] = {**sink(SINK, index), "owner_module": index}
        else:
            output = config["sink"]
            if output not in self.unlinked:
                self.inputs[index] = {"owner_module": index, "sink": self.sinks[output]["index"]}
        return index

    def unload(self, index):
        module = self.modules.pop(index)
        self.inputs.pop(index, None)
        if module["name"] == "module-null-sink":
            self.sinks.pop(SINK)

    def unload_owned(self, index):
        graph = self.graph()
        if index in graph.null_modules or index in graph.loops:
            self.unload(index)

    def default(self):
        return self.selected

    def set_default(self, name):
        self.selected = name
        self.default_changes.append(name)

    def clear_default(self):
        self.set_default(next(name for name in self.sinks if name != SINK))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = RoutingJournal(Path(self.temp.name) / "previous-sink")
        self.pulse = FakePulse()
        self.router = Router(self.pulse, self.journal)

    def add(self, name, index):
        self.pulse.sinks[name] = headset(name, index)

    def reconcile(self, now):
        self.router.reconcile(now)
        return self.router.reconcile(now + 0.1)

    def test_device_detection_uses_properties(self):
        outputs = bluetooth_outputs([
            headset("receiver_a", 10),
            sink("receiver_b", 11, **{"device.bus": "bluetooth"}),
            sink("receiver_c", 12, **{"factory.name": "api.bluez5.a2dp.sink"}),
            sink("bluez_output.fake", 13, **{"device.api": "alsa"}),
            sink("receiver_d", 14, **{"device.api": "bluez5", "object.serial": ""}),
            headset("name with unsafe characters", 15),
        ])
        self.assertEqual(set(outputs), {"receiver_a", "receiver_b", "receiver_c"})
        options = parse_args(loop_args(outputs["receiver_a"]))
        self.assertEqual(options["source"], MONITOR)
        self.assertEqual(options["sink_dont_move"], "true")
        self.assertEqual(options["source_dont_move"], "true")
        for property_name in ("source_output_properties", "sink_input_properties"):
            props = parse_args(options[property_name])
            self.assertEqual(props["node.dont-fallback"], "true")
            self.assertEqual(props["node.dont-move"], "true")
            self.assertEqual(props["application.id"], MARKER)
        self.assertEqual(parse_args(options["sink_input_properties"])["target.object"], "10")

    def test_one_two_three_disconnect_and_radio_off(self):
        self.add("one", 10)
        self.router.reconcile(0)
        self.reconcile(1)
        self.assertEqual(self.pulse.selected, SINK)
        first = self.pulse.graph().linked["one"]
        virtual = self.pulse.graph().sink
        self.add("two", 11)
        self.reconcile(2)
        self.assertEqual(set(self.pulse.graph().linked), {"one", "two"})
        self.add("three", 12)
        self.reconcile(3)
        self.assertEqual(set(self.pulse.graph().linked), {"one", "two", "three"})
        del self.pulse.sinks["two"]
        self.reconcile(4)
        self.assertEqual(self.pulse.graph().linked["one"], first)
        self.assertEqual(set(self.pulse.graph().linked), {"one", "three"})
        del self.pulse.sinks["one"], self.pulse.sinks["three"]
        self.reconcile(5)
        self.assertEqual(self.pulse.selected, "speakers")
        self.assertEqual(self.pulse.graph().loops, {})
        self.assertEqual(self.pulse.graph().sink, virtual)
        self.add("one", 16)
        self.reconcile(6)
        self.assertEqual(self.pulse.selected, SINK)
        self.assertNotEqual(self.pulse.graph().linked["one"], first)

    def test_reconnect_same_name_uses_new_serial(self):
        self.add("one", 10)
        self.router.reconcile(0)
        self.reconcile(1)
        old = self.pulse.graph().linked["one"]
        self.add("one", 20)
        self.reconcile(2)
        self.assertNotIn(old, self.pulse.modules)
        self.assertEqual(list(self.pulse.graph().loops.values()), [("one", "20")])

    def test_rejected_third_keeps_two_and_retries_with_backoff(self):
        for i, name in enumerate(("one", "two", "three"), 10):
            self.add(name, i)
        self.pulse.fail.add("three")
        self.router.reconcile(0)
        with self.assertLogs(level="WARNING") as logs:
            deadline = self.reconcile(1)
        self.assertIn("отказ транспорта", logs.output[0])
        self.assertEqual(set(self.pulse.graph().linked), {"one", "two"})
        self.assertEqual(deadline, 7)
        self.pulse.fail.clear()
        self.reconcile(7)
        self.assertEqual(set(self.pulse.graph().linked), {"one", "two", "three"})

    def test_unbound_output_never_selected_as_default(self):
        self.add("one", 10)
        self.pulse.unlinked.add("one")
        self.router.reconcile(0)
        self.reconcile(1)
        with self.assertLogs(level="WARNING") as logs:
            self.reconcile(4)
        self.assertIn("не подтвердил привязку", logs.output[0])
        self.assertEqual(self.pulse.selected, "speakers")
        self.assertEqual(self.pulse.graph().loops, {})

    def test_crash_recovery_adopts_modules_and_restores_default(self):
        self.add("one", 10)
        self.router.reconcile(0)
        self.reconcile(1)
        modules = self.pulse.modules.copy()
        new = Router(self.pulse, self.journal)
        new.reconcile(2)
        self.assertEqual(self.pulse.modules, modules)
        new.cleanup()
        self.assertEqual(self.pulse.selected, "speakers")
        self.assertIsNone(self.journal.read())
        self.assertEqual(self.pulse.modules, {})

    def test_new_audio_server_reselects_multicast(self):
        self.add("one", 10)
        self.router.reconcile(0)
        self.reconcile(1)
        self.assertEqual(self.pulse.selected, SINK)
        self.pulse = FakePulse()
        self.pulse.cookie = "server-2"
        self.router = Router(self.pulse, self.journal)
        self.add("one", 10)
        self.router.reconcile(2)
        self.reconcile(3)
        self.assertEqual(self.pulse.selected, SINK)

    def test_remembered_default_without_headphones_after_reboot(self):
        self.router.reconcile(0)
        self.pulse.selected = SINK
        self.router.reconcile(1)
        self.assertEqual(self.pulse.selected, "speakers")

    def test_manual_default_is_respected_while_active_and_on_cleanup(self):
        self.pulse.sinks["usb"] = sink("usb", 5)
        self.add("one", 10)
        self.router.reconcile(0)
        self.reconcile(1)
        self.pulse.set_default("usb")
        self.reconcile(2)
        self.router.cleanup()
        self.assertEqual(self.pulse.default_changes, [SINK, "usb"])

    def test_cleanup_falls_back_when_original_device_is_gone(self):
        self.pulse.sinks["usb"] = sink("usb", 5)
        self.pulse.selected = "usb"
        self.add("one", 10)
        self.router.reconcile(0)
        self.reconcile(1)
        del self.pulse.sinks["usb"]
        self.router.cleanup()
        self.assertEqual(self.pulse.selected, "speakers")

    def test_foreign_sink_name_collision_is_safe(self):
        self.pulse.sinks[SINK] = {**sink(SINK, 22), "owner_module": 9}
        self.add("one", 10)
        with self.assertRaisesRegex(AudioError, "уже используется"):
            self.router.reconcile(0)
        self.router.cleanup()
        self.assertIn(SINK, self.pulse.sinks)
        self.assertEqual(self.pulse.modules, {})

    def test_bluez_status_uses_connected_audio_uuids(self):
        service = "0000110b-0000-1000-8000-00805f9b34fb"
        objects = {"data": [{
            "/headset": {"org.bluez.Device1": {
                "Address": {"data": "00:11:22:33:44:55"},
                "Alias": {"data": "Headset"},
                "Connected": {"data": True},
                "UUIDs": {"data": [service]},
            }},
            "/controller": {"org.bluez.Device1": {
                "Address": {"data": "00:11:22:33:44:56"},
                "Connected": {"data": True},
                "UUIDs": {"data": ["00001124-0000-1000-8000-00805f9b34fb"]},
            }},
        }]}
        with patch("deck_audio.parse_json", return_value=objects), patch("deck_audio.command"):
            self.assertEqual(connected_audio(), {"00:11:22:33:44:55": "Headset"})


if __name__ == "__main__":
    unittest.main()
