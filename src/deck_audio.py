#!/usr/bin/env python3
"""Route a PipeWire Pulse sink exclusively to currently usable BlueZ outputs."""

import fcntl
import json
import logging
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

NAME = "steam-deck-bluetooth-audio"
SINK = "steam_deck_bluetooth_multicast"
MONITOR = SINK + ".monitor"
MARKER = "io.github.MTX96.steam-deck-bluetooth-audio"
SINK_ARGS = (
    f"sink_name={SINK} channels=2 channel_map=front-left,front-right "
    f"sink_properties='device.description=\"Bluetooth Audio Multicast\" "
    f"application.id={MARKER} node.virtual=true priority.session=0'"
)
SINK_NAME = re.compile(r"[A-Za-z0-9_.:-]+")
SERIAL = re.compile(r"[0-9]+")
ADDRESS = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")
EVENT = re.compile(rb"^Event '(?:new|change|remove)' on (?:sink|sink-input|card|server|module)(?:[ #]|$)")
COMMAND_TIMEOUT = 4
SETTLE = 3
RETRY_LIMIT = 30
LOG = logging.getLogger(NAME)


class AudioError(RuntimeError):
    pass


def command(*args):
    try:
        return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True,
                              timeout=COMMAND_TIMEOUT,
                              env=dict(os.environ, LC_ALL="C")).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise AudioError(f"{' '.join(args[:3])}: {exc.stderr.strip() or exc.returncode}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(str(exc)) from exc


def parse_json(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise AudioError("Некорректный ответ JSON от аудиосервера или BlueZ") from exc


@dataclass(frozen=True)
class Device:
    name: str
    index: int
    serial: str
    description: str
    address: str = ""


def bluetooth_outputs(sinks):
    outputs = {}
    for sink in sinks:
        props = sink.get("properties") or {}
        if not (props.get("device.api") == "bluez5"
                or props.get("device.bus") == "bluetooth"
                or str(props.get("factory.name", "")).startswith("api.bluez5.")):
            continue
        name = sink["name"]
        serial = str(props.get("object.serial", ""))
        if not SINK_NAME.fullmatch(name) or not SERIAL.fullmatch(serial):
            continue
        address = str(props.get("api.bluez5.address") or props.get("device.string") or "").upper()
        outputs[name] = Device(name, sink["index"], serial,
                               sink.get("description") or name,
                               address if ADDRESS.fullmatch(address) else "")
    return outputs


def parse_args(argument):
    try:
        return dict(token.split("=", 1) for token in shlex.split(argument or "") if "=" in token)
    except ValueError:
        return {}


def loop_args(device):
    # WirePlumber must not pick another sink if the BlueZ node disappears.
    pin = "node.dont-fallback=true node.dont-move=true node.passive=true"
    return (f"source={MONITOR} sink={device.name} "
            "source_dont_move=true sink_dont_move=true "
            f"source_output_properties='{pin} application.id={MARKER}' "
            f"sink_input_properties='{pin} application.id={MARKER} "
            f"target.object={device.serial}'")


@dataclass
class Graph:
    sinks: list
    modules: list
    inputs: list

    @property
    def devices(self):
        return bluetooth_outputs(self.sinks)

    @property
    def null_modules(self):
        return [module["index"] for module in self.modules
                if module["name"] == "module-null-sink"
                and (args := parse_args(module.get("argument"))).get("sink_name") == SINK
                and parse_args(args.get("sink_properties")).get("application.id") == MARKER]

    @property
    def loops(self):
        loops = {}
        for module in self.modules:
            if module["name"] != "module-loopback":
                continue
            args = parse_args(module.get("argument"))
            props = parse_args(args.get("sink_input_properties"))
            if args.get("source") == MONITOR and props.get("application.id") == MARKER:
                loops[module["index"]] = (args.get("sink"), props.get("target.object"))
        return loops

    @property
    def sink(self):
        return next((sink for sink in self.sinks if sink["name"] == SINK), None)

    @property
    def linked(self):
        devices = self.devices
        loops = self.loops
        linked = {}
        for stream in self.inputs:
            index = stream.get("owner_module")
            if index not in loops:
                # PipeWire also exports the module ID as a node property.
                module_id = str((stream.get("properties") or {}).get("pulse.module.id", ""))
                index = int(module_id) if SERIAL.fullmatch(module_id) else None
            if index not in loops:
                continue
            name, serial = loops[index]
            device = devices.get(name)
            if device and serial == device.serial and stream.get("sink") == device.index:
                linked[name] = index
        return linked


class Pulse:
    @staticmethod
    def info():
        return parse_json(command("pactl", "--format=json", "info"))

    def check(self):
        info = self.info()
        server = info.get("server_name", "")
        if "PipeWire" not in server:
            raise AudioError(f"Нужен PipeWire с pipewire-pulse. Сервер: {server or 'неизвестен'}")
        command("wpctl", "status")
        self.cookie = info.get("cookie", "")

    @staticmethod
    def modules():
        # pactl 17 omits the module index in JSON output.
        modules = []
        for line in command("pactl", "list", "short", "modules").splitlines():
            fields = line.split("\t", 3)
            if len(fields) >= 3 and SERIAL.fullmatch(fields[0]):
                index, name, argument = fields[:3]
                modules.append(dict(index=int(index), name=name, argument=argument))
        return modules

    def graph(self):
        modules = self.modules()
        sinks, inputs = (parse_json(command("pactl", "--format=json", "list", kind))
                         for kind in ("sinks", "sink-inputs"))
        return Graph(sinks, modules, inputs)

    @staticmethod
    def load(module, args):
        return int(command("pactl", "load-module", module, args))

    def unload(self, index):
        try:
            command("pactl", "unload-module", str(index))
        except AudioError:
            if any(module["index"] == index for module in self.modules()):
                raise

    def unload_owned(self, index):
        # Module indices can be recycled after a stream removes itself.
        graph = Graph([], self.modules(), [])
        if index in graph.null_modules or index in graph.loops:
            self.unload(index)

    @staticmethod
    def default():
        return command("pactl", "get-default-sink")

    @staticmethod
    def set_default(name):
        command("pactl", "set-default-sink", name)

    @staticmethod
    def clear_default():
        command("wpctl", "clear-default", "0")


class RoutingJournal:
    """Remember the previous default over a daemon crash."""

    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            return parse_json(self.path.read_text())
        except FileNotFoundError:
            return None

    def save(self, name, identity):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"previous": name, "identity": identity}) + "\n")
        temporary.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)


@dataclass
class Attempt:
    deadline: float
    module: int | None = None
    delay: int = SETTLE


@dataclass
class Router:
    pulse: Pulse
    journal: RoutingJournal
    attempts: dict = field(default_factory=dict)
    null_deadline: float | None = None

    def restore(self, graph):
        entry = self.journal.read() or {}
        previous = entry.get("previous")
        if self.pulse.default() == SINK:
            names = {sink["name"] for sink in graph.sinks if sink["name"] != SINK}
            if previous in names:
                self.pulse.set_default(previous)
            else:
                self.pulse.clear_default()
        self.journal.clear()

    def activate(self, graph):
        current = self.pulse.default()
        entry = self.journal.read()
        serial = graph.sink["properties"]["object.serial"]
        identity = f"{self.pulse.cookie}/{serial}"
        if entry is None or entry.get("identity") != identity:
            previous = current if current != SINK else (entry or {}).get("previous")
            self.journal.save(previous, identity)
        elif current != SINK:
            # A manual change during this listening session takes precedence.
            return
        if current != SINK:
            try:
                self.pulse.set_default(SINK)
            except AudioError:
                # Keep the previous default on partial success or a lost reply.
                if self.pulse.default() != SINK:
                    self.journal.clear()
                raise

    def reconcile(self, now):
        graph = self.pulse.graph()
        null_modules = graph.null_modules
        if graph.sink is not None and graph.sink.get("owner_module") not in null_modules:
            raise AudioError(f"Имя {SINK} уже используется другим аудиовыходом")
        if graph.sink is None:
            if not null_modules:
                self.pulse.load("module-null-sink", SINK_ARGS)
                self.null_deadline = now + SETTLE
            elif self.null_deadline is None:
                self.null_deadline = now + SETTLE
            elif now >= self.null_deadline:
                for stale in null_modules:
                    self.pulse.unload_owned(stale)
                self.null_deadline = None
            return now + 1
        self.null_deadline = None
        for duplicate in set(null_modules) - {graph.sink["owner_module"]}:
            self.pulse.unload_owned(duplicate)

        devices = graph.devices
        linked = graph.linked
        active = {}
        for module, (name, serial) in graph.loops.items():
            device = devices.get(name)
            if (device is None or serial != device.serial or name in active
                    or (name in linked and linked[name] != module)):
                self.pulse.unload_owned(module)
            else:
                active[name] = module

        keys = {(device.name, device.serial) for device in devices.values()}
        self.attempts = {key: value for key, value in self.attempts.items() if key in keys}
        for device in devices.values():
            if device.name in linked:
                self.attempts.pop((device.name, device.serial), None)
            else:
                self.connect(device, active.get(device.name), now)

        if linked:
            self.activate(graph)
        elif not devices or not any(attempt.module is not None for attempt in self.attempts.values()):
            # WirePlumber can remember this sink across reboots, when our journal is gone.
            if self.journal.read() is not None or len(graph.sinks) > 1:
                self.restore(graph)

        return min((attempt.deadline for attempt in self.attempts.values()), default=None)

    def connect(self, device, module, now):
        key = (device.name, device.serial)
        attempt = self.attempts.get(key)
        if attempt and now < attempt.deadline:
            return
        if module is not None:
            if attempt is None or attempt.module != module:
                self.attempts[key] = Attempt(now + SETTLE, module)
                return
            self.pulse.unload_owned(module)
        if attempt and attempt.module is not None:
            error = "PipeWire не подтвердил привязку потока"
        else:
            try:
                module = self.pulse.load("module-loopback", loop_args(device))
            except AudioError as exc:
                error = str(exc)
            else:
                delay = attempt.delay if attempt else SETTLE
                self.attempts[key] = Attempt(now + SETTLE, module, delay)
                return
        delay = min(attempt.delay * 2 if attempt else SETTLE * 2, RETRY_LIMIT)
        self.attempts[key] = Attempt(now + delay, delay=delay)
        LOG.warning("%s: %s. Повтор через %s с", device.description, error, delay)

    def cleanup(self):
        self.pulse.check()
        graph = self.pulse.graph()
        for module in graph.loops:
            self.pulse.unload_owned(module)
        if self.journal.read() is not None or (
                graph.sink and graph.sink.get("owner_module") in graph.null_modules):
            self.restore(graph)
        for module in graph.null_modules:
            self.pulse.unload_owned(module)


@contextmanager
def runtime_lock(directory):
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AudioError("Другой экземпляр сервиса уже управляет аудиовыходом") from exc
        yield


def watch(router):
    router.pulse.check()
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with selectors.DefaultSelector() as selector, subprocess.Popen(
        ["pactl", "subscribe"], stdout=subprocess.PIPE,
        env=dict(os.environ, LC_ALL="C"),
    ) as subscriber:
        try:
            selector.register(subscriber.stdout, selectors.EVENT_READ)
            wake_read, wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
            previous_wakeup = signal.set_wakeup_fd(wake_write)
            selector.register(wake_read, selectors.EVENT_READ)
            try:
                watch_events(router, selector, subscriber.stdout, lambda: stopping)
            finally:
                signal.set_wakeup_fd(previous_wakeup)
                os.close(wake_read)
                os.close(wake_write)
        finally:
            if subscriber.poll() is None:
                subscriber.terminate()
            if stopping:
                router.cleanup()
            # On unexpected audio-server loss, keep the journal for systemd's restart.


def watch_events(router, selector, stream, stopping):
    pending = b""
    event_due = time.monotonic() + 0.2
    retry_due = None
    blocked_until = 0.0
    while not stopping():
        now = time.monotonic()
        if ((event_due is not None and event_due <= now)
                or (retry_due is not None and retry_due <= now)):
            event_due = None
            try:
                retry_due = router.reconcile(now)
            except AudioError as exc:
                LOG.warning("%s. Повтор через 5 с", exc)
                blocked_until = time.monotonic() + 5
                retry_due = blocked_until
            continue
        deadlines = [due for due in (event_due, retry_due) if due is not None]
        timeout = max(0, min(deadlines) - now) if deadlines else None
        for key, _ in selector.select(timeout):
            if key.fileobj is not stream:
                os.read(key.fd, 4096)
                continue
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                if stopping():
                    return
                raise AudioError("Подписка pactl завершилась. systemd перезапустит сервис")
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            if event_due is None and any(EVENT.match(line) for line in lines):
                event_due = max(time.monotonic() + 0.2, blocked_until)


AUDIO_SERVICES = {"00001108", "0000110b", "0000111e", "0000184e", "00001850"}


def connected_audio():
    managed = parse_json(command("busctl", "--system", "--json=short", "call",
                                 "org.bluez", "/", "org.freedesktop.DBus.ObjectManager",
                                 "GetManagedObjects"))
    objects, = managed["data"]
    connected = {}
    for interfaces in objects.values():
        device = interfaces.get("org.bluez.Device1", {})

        def value(key, default=None):
            return device.get(key, {}).get("data", default)

        address = value("Address")
        if address and value("Connected") and any(
            uuid.lower().split("-")[0] in AUDIO_SERVICES for uuid in value("UUIDs", [])
        ):
            connected[address.upper()] = value("Alias") or address
    return connected


def status(pulse):
    pulse.check()
    graph = pulse.graph()
    default = pulse.default()
    linked = graph.linked
    devices = graph.devices
    print(f"Выход по умолчанию: {default or 'нет'}")
    print(f"Bluetooth-выходов в PipeWire: {len(devices)}. Подключено к объединению: {len(linked)}.")
    for device in sorted(devices.values(), key=lambda item: item.description.casefold()):
        state = "маршрут подключён" if device.name in linked else "маршрут отсутствует"
        print(f"  {device.description} — {state}")
    try:
        connected = connected_audio()
    except (AudioError, KeyError, TypeError, ValueError):
        print("BlueZ: список дополнительно подключённых устройств недоступен.")
    else:
        addresses = {device.address for device in devices.values()}
        unnamed = sum(not device.address for device in devices.values())
        without_output = {address: label for address, label in connected.items() if address not in addresses}
        if len(without_output) > unnamed:
            labels = ", ".join(sorted(without_output.values()))
            print(f"BlueZ: для части устройств нет подтверждённого аудиовыхода PipeWire ({labels}).")
    if len(linked) < len(devices):
        print("Проверьте профиль A2DP/LE Audio и журнал. Возможен предел контроллера, профиля или кодека.")
    if len(linked) >= 3:
        print("Активность маршрутов подтверждена. Одновременный звук и задержку проверьте на наушниках.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    pulse = Pulse()
    try:
        if action == "check":
            pulse.check()
        elif action == "status":
            status(pulse)
        elif action in {"run", "cleanup"}:
            runtime = os.environ.get("XDG_RUNTIME_DIR")
            if not runtime:
                raise AudioError("XDG_RUNTIME_DIR недоступен. Запустите в пользовательской сессии.")
            directory = Path(runtime) / NAME
            with runtime_lock(directory):
                router = Router(pulse, RoutingJournal(directory / "previous-sink"))
                if action == "run":
                    watch(router)
                else:
                    router.cleanup()
        else:
            raise AudioError("Использование: deck_audio.py {run|cleanup|check|status}")
    except (AudioError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
