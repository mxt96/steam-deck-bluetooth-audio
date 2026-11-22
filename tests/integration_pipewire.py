"""Run inside dbus-run-session with pipewire, pipewire-pulse, WirePlumber and pactl."""

from array import array
from contextlib import ExitStack
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from deck_audio import AudioError, Pulse, SINK


def eventually(description, condition, timeout=12):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = condition()
            if last:
                return last
        except AudioError as exc:
            last = exc
        time.sleep(0.1)
    raise AssertionError(f"{description}: {last!r}")


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise


def spawn(stack, log_dir, label, *command):
    log = stack.enter_context((log_dir / f"{label}.log").open("wb"))
    process = subprocess.Popen(command, stdout=log, stderr=log, close_fds=True)
    stack.callback(stop, process)
    return process


def rms(path):
    samples = array("h")
    data = path.read_bytes()
    samples.frombytes(data[:len(data) - len(data) % samples.itemsize])
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples)) if samples else 0


def verify_audio(stack, location, speaker, *headphones, during=None):
    captures = {}
    for output in (speaker, *headphones):
        path = location / f"{output}.pcm"
        captures[output] = path
        stream = stack.enter_context(path.open("wb"))
        proc = subprocess.Popen(
            ["pacat", "--record", "--raw", "--rate=48000", "--channels=2",
             "--format=s16le", f"--device={output}.monitor"],
            stdout=stream, stderr=subprocess.DEVNULL,
        )
        stack.callback(stop, proc)
    time.sleep(0.3)
    signal = array("h")
    for frame in range(48000 * 2):
        sample = round(12000 * math.sin(2 * math.pi * 440 * frame / 48000))
        signal.extend((sample, sample))
    with tempfile.TemporaryFile() as tone:
        tone.write(signal.tobytes())
        tone.seek(0)
        playback = subprocess.Popen(["pacat", "--playback", "--raw", "--rate=48000",
                                     "--channels=2", "--format=s16le"], stdin=tone)
        try:
            if during is not None:
                time.sleep(0.8)
                assert playback.poll() is None
                during()
            assert playback.wait(timeout=10) == 0
        finally:
            stop(playback)
    time.sleep(0.3)
    assert all(path.stat().st_size > 48000 for path in captures.values())
    levels = {name: rms(path) for name, path in captures.items()}
    assert rms(captures[speaker]) == 0, levels
    assert all(levels[output] > 500 for output in headphones), levels
    print(f"PCM RMS, speakers and Bluetooth test outputs: {levels}", flush=True)


def integration(directory):
    with ExitStack() as stack:
        spawn(stack, directory, "pipewire", "pipewire")
        eventually("PipeWire socket ready", lambda: (directory / "pipewire-0").is_socket())
        spawn(stack, directory, "wireplumber", "wireplumber")
        pulse_process = spawn(stack, directory, "pulse", "pipewire-pulse")
        pulse = Pulse()
        eventually("PipeWire Pulse and default sink available",
                   lambda: pulse.info() and pulse.default())

        def add(name, properties=""):
            module = pulse.load("module-null-sink", f"sink_name={name} {properties}")
            eventually(name, lambda: any(sink["name"] == name for sink in pulse.graph().sinks))
            return module

        speaker = "bluez_output.fake_speakers"
        add(speaker, "sink_properties='device.api=alsa priority.session=1000'")
        eventually("WirePlumber default metadata ready", lambda: pulse.set_default(speaker) is None)
        eventually("speakers selected", lambda: pulse.default() == speaker)
        daemon = spawn(stack, directory, "daemon", sys.executable, str(ROOT / "src/deck_audio.py"), "run")
        virtual_index = eventually("empty virtual sink", lambda: pulse.graph().sink)["index"]
        assert pulse.default() == speaker

        first = add("receiver_one", "sink_properties='device.api=bluez5'")
        initial = eventually("one headphone", lambda: pulse.graph().linked.get("receiver_one"))
        serial = pulse.graph().devices["receiver_one"].serial
        eventually("multicast is default", lambda: pulse.default() == SINK)
        second = add("receiver_two", "sink_properties='device.bus=bluetooth'")
        eventually("two headphones", lambda: len(pulse.graph().linked) == 2)
        third = add("receiver_three", "sink_properties='device.api=bluez5'")
        eventually("three headphones", lambda: len(pulse.graph().linked) == 3)
        assert initial == pulse.graph().linked["receiver_one"]
        assert pulse.graph().sink["index"] == virtual_index
        with ExitStack() as captures:
            verify_audio(captures, directory, speaker, "receiver_one", "receiver_two", "receiver_three")

        with ExitStack() as captures:
            verify_audio(captures, directory, speaker, "receiver_one", "receiver_three",
                         during=lambda: pulse.unload(second))
        eventually("second headphone was removed", lambda: len(pulse.graph().loops) == 2)
        assert initial == pulse.graph().linked["receiver_one"]
        assert pulse.graph().sink["index"] == virtual_index
        assert pulse.default() == SINK

        daemon.kill()
        daemon.wait(timeout=3)
        daemon = spawn(stack, directory, "recovered", sys.executable, str(ROOT / "src/deck_audio.py"), "run")
        time.sleep(0.8)
        assert daemon.poll() is None
        assert initial == pulse.graph().linked["receiver_one"]
        assert len(pulse.graph().null_modules) == 1

        pulse.unload(first)
        pulse.unload(third)
        eventually("all routes removed", lambda: not pulse.graph().loops)
        eventually("speakers restored", lambda: pulse.default() == speaker)
        assert pulse.graph().sink["index"] == virtual_index
        add("receiver_one", "sink_properties='device.api=bluez5'")
        eventually("headphone reconnected", lambda: pulse.graph().linked.get("receiver_one"))
        assert pulse.graph().devices["receiver_one"].serial != serial
        eventually("multicast is default again", lambda: pulse.default() == SINK)

        stop(pulse_process)
        assert daemon.wait(timeout=10) != 0
        spawn(stack, directory, "pulse-restarted", "pipewire-pulse")
        eventually("new Pulse server is online", lambda: pulse.info() and pulse.default())
        assert not pulse.graph().null_modules
        speaker_module = add(speaker, "sink_properties='device.api=alsa priority.session=1000'")
        eventually("selecting speakers on new Pulse server", lambda: pulse.set_default(speaker) is None)
        add("receiver_one", "sink_properties='device.api=bluez5'")
        daemon = spawn(stack, directory, "new-server", sys.executable, str(ROOT / "src/deck_audio.py"), "run")
        eventually("route recovered after Pulse restart", lambda: pulse.graph().linked.get("receiver_one"))
        eventually("multicast restored after Pulse restart", lambda: pulse.default() == SINK)

        stop(daemon)
        assert daemon.returncode == 0
        eventually("owned virtual sink and routes removed",
                   lambda: not pulse.graph().null_modules and not pulse.graph().loops and not pulse.graph().sink)
        eventually("speakers restored on stop", lambda: pulse.default() == speaker)
        assert any(module["index"] == speaker_module for module in pulse.graph().modules)
        print("PipeWire integration passed", flush=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="deck-pipewire-") as temporary:
        directory = Path(temporary)
        with patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(directory), "XDG_CONFIG_HOME": str(directory / "config"),
            "XDG_STATE_HOME": str(directory / "state"), "PULSE_SERVER": f"unix:{directory}/pulse/native",
            "PIPEWIRE_REMOTE": "pipewire-0",
        }):
            try:
                integration(directory)
            except Exception:
                for path in sorted(directory.glob("*.log")):
                    print(f"\n--- {path.name} ---\n{path.read_text(errors='replace')[-3000:]}", file=sys.stderr)
                raise
