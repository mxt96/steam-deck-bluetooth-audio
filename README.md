# Steam Deck Bluetooth Audio Multicast

**Bluetooth Audio Multicast** is a virtual audio output that sends game, music, and video audio to connected Bluetooth headphones. The service discovers outputs after pairing, updates routes as headphones connect and disconnect, and starts automatically when you sign in after a reboot.

The multicast output routes audio exclusively to Bluetooth devices exposed by PipeWire. When the last pair disconnects, the service restores the previous output if it is available. Otherwise WirePlumber chooses an output, which may be the Steam Deck speakers.

## Install on Steam Deck OLED

From the power menu, switch to **Desktop Mode** and open **Konsole**. Run the installer as your regular Steam Deck user without `sudo`. It requires Python 3.10 or later, `pactl`, `wpctl`, `systemctl`, `flock`, and a working PipeWire, pipewire-pulse, and WirePlumber session. The download commands also require `curl` and `tar`. The installer checks the audio stack and installs files in your home directory. Pair your headphones in the regular Steam Deck Bluetooth settings.

### Download, review, and install

```bash
cd ~/Downloads
curl -fL https://github.com/mxt96/steam-deck-bluetooth-audio/archive/refs/heads/main.tar.gz \
  -o steam-deck-bluetooth-audio.tar.gz
tar -xzf steam-deck-bluetooth-audio.tar.gz
cd steam-deck-bluetooth-audio-main
less install.sh
less src/deck_audio.py
less src/steam-deck-bluetooth-audio
less systemd/steam-deck-bluetooth-audio.service
less uninstall.sh
bash install.sh
```

Press `q` to exit each `less` preview. To install from a specific commit, replace `refs/heads/main.tar.gz` in the URL with `<verified commit SHA>.tar.gz` and enter the extracted directory with that SHA. Running the installer again updates the project files and restarts the service. This may briefly interrupt playback.

### Install with one command

Use this option if you trust the contents of `main`. The command runs the downloaded installer with your user's permissions. To inspect the files first, use the steps above.

```bash
bash -c 'set -e; d=$(mktemp -d); trap '\''rm -rf -- "$d"'\'' EXIT; curl -fL https://github.com/mxt96/steam-deck-bluetooth-audio/archive/refs/heads/main.tar.gz -o "$d/source.tar.gz"; tar -xzf "$d/source.tar.gz" -C "$d"; bash "$d/steam-deck-bluetooth-audio-main/install.sh"'
```

Connect two pairs in Bluetooth settings, start playback, and wait a few seconds. The default output should switch to **Bluetooth Audio Multicast** and `status` should report `Linked to multicast: 2.` If an app uses a dedicated output, select **Bluetooth Audio Multicast** in the app's audio settings or in the KDE mixer. If it still uses the old output, restart playback.

## Manage the service

Use these commands even if `~/.local/bin` is absent from your `PATH`.

| Command | Action |
| --- | --- |
| `~/.local/bin/steam-deck-bluetooth-audio start` | Start the service and enable automatic startup. |
| `~/.local/bin/steam-deck-bluetooth-audio stop` | Stop the service, disable automatic startup, and restore normal audio routing. |
| `~/.local/bin/steam-deck-bluetooth-audio status` | Show service status, the current output, and headphone routes. |
| `~/.local/bin/steam-deck-bluetooth-audio restart` | Restart the service and enable automatic startup. |
| `~/.local/bin/steam-deck-bluetooth-audio uninstall` | Remove the service, command, and its audio modules. |

You can also run `bash uninstall.sh` from the project directory. Your Bluetooth pairings are preserved. If you manually select another output while the service is running, that selection takes precedence until all headphones disconnect and a pair reconnects.

### Troubleshooting

```bash
~/.local/bin/steam-deck-bluetooth-audio status
journalctl --user -u steam-deck-bluetooth-audio.service -b --no-pager -n 80
pactl info
wpctl status
bluetoothctl devices Connected
```

- **Headphones are connected but `status` shows no route.** Check for a playback profile such as A2DP in Steam Deck's settings. When BlueZ is available on the system D-Bus, `status` also checks devices advertising common audio services and reports when some have no confirmed PipeWire output.
- **Routes exist, but a game plays through a different output.** Select **Bluetooth Audio Multicast** in the game or mixer. In Gaming Mode, check the selected device in the audio settings. In Desktop Mode, check the app's output in the KDE mixer.
- **A third pair is missing or silent.** Compare the number of Bluetooth outputs and linked routes in `status`. The service logs linking errors and retries with a delay. If the third route is linked and silent, test that pair by itself, then with the other two. PipeWire can report a link even when audio fails to play over Bluetooth.
- **Audio is missing after waking the Deck.** Check whether the headphones reconnected. If necessary, reconnect them in Bluetooth settings. The service adds their routes as soon as PipeWire exposes the outputs.

## How routing works

- `pactl subscribe` reports changes to the audio server and devices. The service checks the graph after events, backs off after errors, and waits for events when the graph is stable.
- The service identifies Bluetooth outputs using `device.api`, `device.bus`, and the `api.bluez5.*` factories. This also includes Bluetooth speakers that BlueZ exposes as audio outputs.
- A persistent stereo `module-null-sink` receives audio. One `module-loopback` per available device copies audio from its monitor. The playback stream is pinned to the device's `object.serial`. Both loopback directions disable stream moves and fallback. Disconnecting a pair removes only its route.
- The virtual output keeps the same name throughout the service's lifetime. When a Bluetooth route is linked, the service selects the virtual output as the default. On shutdown it restores normal audio routing and unloads its modules. If the service crashes and the audio server stays up, the restarted process adopts the existing modules and reads the saved previous output.
- `module-combine-sink` takes a list of target output names, so changing that list would require reloading the combined output. The native `libpipewire-module-combine-stream` supports rule-based output discovery. The registry objects it matches in the inspected PipeWire version lack the required BlueZ properties. `pw-link` links ports directly and would require separate management of the virtual output and its ports. The service uses built-in pipewire-pulse modules and reads device properties from `pactl`. [Combine Sink](https://docs.pipewire.org/page_pulse_module_combine_sink.html) · [Combine Stream](https://docs.pipewire.org/page_module_combine_stream.html) · [PipeWire 1.0.5 node registry properties](https://github.com/PipeWire/pipewire/blob/1.0.5/src/pipewire/impl-node.c) · [Loopback](https://docs.pipewire.org/page_pulse_module_loopback.html)

The service starts with the user's `default.target` and restarts if its subscription to the audio server ends. When switching between Desktop Mode and Gaming Mode, it continues running if the user systemd manager stays up. If that manager starts again, it launches the service. WirePlumber's Bluetooth access depends on the active logind session. The service adds routes after a mode switch or wake as PipeWire reports the outputs. Test game output selection and mode switching on your SteamOS build. [WirePlumber Bluetooth configuration](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html) · [BlueZ device API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Device.rst) · [SteamOS mode-switch release notes](https://store.steampowered.com/news/app/1675200/view/687522235410285838)

## Limits and on-device testing

Steam Deck OLED has Bluetooth 5.3 and a dedicated Bluetooth antenna. Valve does not specify the maximum number of simultaneous headphone streams. The Bluetooth controller, firmware, profile, codec, and radio conditions affect how many streams are usable. The service copies the stereo audio signal. Headphone models can play that signal at different times because their codecs and buffers introduce different delays. [Steam Deck OLED specifications](https://www.steamdeck.com/en/tech)

The automated suite exercises simulated audio graphs and a real PipeWire session with virtual receivers. It checks that PCM audio reaches three receivers, the simulated speaker output stays silent, and playback continues on the remaining receivers when one disconnects. Tests with physical Bluetooth headphones and Steam Deck hardware are still pending.

| Scenario | Check on Steam Deck OLED |
| --- | --- |
| One pair | Connect one pair and start audio. Verify one linked route in `status`, audible headphones, and silent speakers. |
| Two different pairs | Connect the second pair during playback. Verify two linked routes and audible playback on both pairs. |
| A third pair | Connect it with the first two still connected. Verify three routes and playback on all three, or record where connection or playback failed. |
| Disconnect one pair | Disconnect one pair during continuous audio. Verify that the remaining pair keeps playing and the speakers stay silent. |
| Disconnect all pairs | Verify that the service reports zero routes and restores a regular output. Then reconnect one pair. |
| Sleep and wake | Put the Deck to sleep with two pairs connected. Wake it and verify both routes and playback after Bluetooth reconnects. |
| Reboot | Leave automatic startup enabled, reboot, and connect the headphones. Verify service operation after sign-in. |
| Desktop Mode ↔ Gaming Mode | Switch in both directions. Verify the virtual output, game audio in the headphones, and silent speakers while the game uses multicast. |
| Toggle Bluetooth | Turn Bluetooth off and on. Verify that the regular output returns, then verify that the routes reappear. |
| Service crash | With headphones connected, run the commands below. Verify the service status, the number of virtual outputs, and playback. |
| Uninstall | Run `uninstall`. Verify regular audio, removal of the virtual output, and preserved Bluetooth pairings. |

```bash
systemctl --user kill --signal=SIGKILL steam-deck-bluetooth-audio.service
sleep 6
~/.local/bin/steam-deck-bluetooth-audio status
pactl list short sinks
```

When reporting test results, include the SteamOS version, headphone models and profiles, and what happened in each scenario. Use these commands to collect versions.

```bash
cat /etc/os-release
pipewire --version
wireplumber --version
pactl --version
bluetoothctl --version
```

## Developer checks

```bash
python3 -m unittest discover -s tests -v
shellcheck install.sh uninstall.sh src/steam-deck-bluetooth-audio
dbus-run-session -- python3 tests/integration_pipewire.py
```

The integration test requires `pipewire`, `pipewire-pulse`, `wireplumber`, `dbus-run-session`, `wpctl`, `pactl`, and `pacat`. It starts a separate user audio session in a temporary directory. GitHub Actions runs these checks on pushes and pull requests.

[MIT License](LICENSE)
