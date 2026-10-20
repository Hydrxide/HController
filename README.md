# HController

The hub for a set of small apps ("modules") on your local network. It starts them, keeps them running, shows their health and logs on one dashboard, edits their config files, and owns the **shared LED panel**: a schedule decides which app's frames the panel shows at any time.

The first module is [nhlscreen](https://github.com/Hydrxide/nhlscreen), the NHL scoreboard. Any app that serves frames the same way can share the panel with it.

```
                         ┌──────────── HController :1630 ─────────────┐
 nhlscreen :9134 ◄─runs──┤ registry ─ health ─ restarts ─ logs        │◄── dashboard (any browser on the LAN)
 weather   :9200 ◄─runs──┤                                            │
 (other device) ◄─watch──┤ scheduler ─► frame source ─► frame hub ────┼──► WS /display/stream
                         └────────────────────────────────────────────┘          │
                                                                hcontroller-panel (root) ─► LED matrix
```

This README is a setup guide. It works on a **Raspberry Pi** (the real board), and on a **Mac, Linux PC or Windows PC** for trying things out. Only the Pi can drive the LED panel itself; everywhere else, the dashboard's live preview shows exactly what the panel would.

**Contents:** [What you need](#what-you-need) · [1. Install](#1-install) · [2. Add your modules](#2-add-your-modules) · [3. Start it](#3-start-it) · [4. Start it at boot (Pi / Linux)](#4-start-it-at-boot-pi--linux) · [5. Connect the LED panel (Pi)](#5-connect-the-led-panel-pi) · [Using the dashboard](#using-the-dashboard) · [The LED panel and its schedule](#the-led-panel-and-its-schedule) · [Modules](#modules) · [Troubleshooting](#troubleshooting) · [For developers](#for-developers)

## What you need

- **Python 3.11 or newer** and **git**.
- For a real LED panel: a Raspberry Pi that [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) supports, running **Raspberry Pi OS**, and HUB75 RGB LED panels (see [step 5](#5-connect-the-led-panel-pi)).

| | Install Python and git | Check |
|---|---|---|
| **Raspberry Pi OS, Debian, Ubuntu** | `sudo apt update && sudo apt install -y git python3 python3-venv` | `python3 --version` |
| **macOS** | `xcode-select --install` for git. Python from [python.org](https://www.python.org/downloads/macos/) or `brew install python@3.13` | `python3 --version` |
| **Windows** | `winget install Git.Git` and `winget install Python.Python.3.13` in PowerShell, or the installers from [git-scm.com](https://git-scm.com/download/win) and [python.org](https://www.python.org/downloads/windows/). Then open a new PowerShell window. | `py --version` |

If `python3 --version` shows something older than 3.11, call the newer Python by its full name in the commands below, for example `python3.13 -m venv .venv`. The `python3` that comes with macOS is 3.9, and Ubuntu 22.04's is 3.10; both are too old. On Windows, `py` runs the newest Python installed.

**On a Pi:** in Raspberry Pi Imager, set the hostname (this guide uses `raspberrypi`), your username, Wi-Fi and SSH before writing the card. Then do everything below over `ssh <you>@raspberrypi.local`.

## 1. Install

Put HController and its modules side by side in your home folder: `~/HController`, `~/nhlscreen`, and so on. The example config expects that layout.

**macOS, Linux and the Pi:**

```bash
cd ~
git clone https://github.com/Hydrxide/HController.git
cd HController
python3 -m venv .venv
.venv/bin/pip install -e .                 # on the Pi with a panel: -e '.[panel]'
cp hcontroller.example.yaml hcontroller.yaml
```

**Windows (PowerShell):**

```powershell
cd ~
git clone https://github.com/Hydrxide/HController.git
cd HController
py -m venv .venv
.venv\Scripts\pip install -e .
copy hcontroller.example.yaml hcontroller.yaml
```

Use PowerShell rather than the old Command Prompt for every Windows command in this guide. There's no need to "activate" the venv: the commands call its Python directly.

## 2. Add your modules

Each module is installed on its own (clone it, make its venv) and then registered in `hcontroller.yaml`. HController doesn't install modules for you.

**nhlscreen** is already registered in the example config. Install it with the steps in its guide, **[Running nhlscreen under HController](https://github.com/Hydrxide/nhlscreen/blob/main/docs/hcontroller.md)**, which also explains every setting. If you don't want it, delete its entry under `modules:` and change `display.default`.

**Other modules:** each module's README has the block to paste under `modules:`. To write your own, see [docs/modules.md](docs/modules.md).

**The same entries work on Windows.** A Windows venv keeps Python at `.venv\Scripts\python.exe` rather than `.venv/bin/python`, and HController uses that one when the `.venv/bin` path doesn't exist. `cwd: ~/nhlscreen` means `C:\Users\<you>\nhlscreen` there.

Also set `timezone` at the top of `hcontroller.yaml` to yours (an IANA name such as `America/Vancouver` or `Europe/London`): the schedule's times are in that zone.

## 3. Start it

**macOS, Linux and the Pi:**

```bash
cd ~/HController
.venv/bin/python -m hcontroller
```

**Windows:**

```powershell
cd ~\HController
.venv\Scripts\python.exe -m hcontroller
```

It reads `hcontroller.yaml` from the folder you start it in, and keeps running until you press **Ctrl+C**, which also stops its modules. It only reads `hcontroller.yaml` at startup, so stop and start it again after editing the file.

Open the dashboard at **http://127.0.0.1:1630/** on the same machine, or **http://raspberrypi.local:1630/** (or the machine's IP address) from any device on your network. There's no login, so anyone on your Wi-Fi can use it.

**Check it's working:** each module's card goes from **starting** to **healthy** within a few seconds, and the LED panel section's preview shows the default app.

**Firewall prompts.** HController listens on your network (`server.host: 0.0.0.0`) so phones can reach the dashboard. The first time it starts, Windows asks whether to allow Python through the firewall: allow it on **private** networks. macOS asks the same only if its firewall is turned on.

## 4. Start it at boot (Pi / Linux)

On a Pi, or any Linux machine with systemd, install HController as a service so it starts at boot and restarts if it crashes. It then starts every module itself.

```bash
cd ~/HController
sed "s#/home/pi#$HOME#g; s#^User=pi#User=$USER#" deploy/hcontroller.service | sudo tee /etc/systemd/system/hcontroller.service > /dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now hcontroller
journalctl -u hcontroller -f        # HController's and every module's output; Ctrl+C stops watching
```

The `sed` fills in your username and home folder, since the unit file is written for a user called `pi`. Stop any copy you started by hand first, or the service can't get port 1630.

- **Restart after editing `hcontroller.yaml`:** `sudo systemctl restart hcontroller`.
- **Modules with their own service** (such as nhlscreen's `nhlscreen.service`): turn it off with `sudo systemctl disable --now nhlscreen`. HController starts nhlscreen itself, and reports a **conflict** if another copy holds its port.

On macOS and Windows there's no service: run HController in a terminal (step 3) while you use it.

## 5. Connect the LED panel (Pi)

The panel is driven by `hcontroller-panel`, a separate process that runs as root, because [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) needs the Pi's GPIO pins. HController and its modules stay unprivileged. The driver reads HController's frame stream (`ws://127.0.0.1:1630/display/stream`), draws each frame, applies the brightness, and blanks the panel if frames stop for 5 s.

**Hardware.** Any HUB75 panels and wiring that rpi-rgb-led-matrix supports will do: wired straight to the GPIO pins, or through an adapter board. Its README covers the panels, the adapter boards and the wiring. Power the panels from their own 5 V supply, not from the Pi. The panels together must make up HController's `display.width` × `display.height` (128 × 64 by default).

**1. Install HController with the panel extra** (if you didn't in step 1): `cd ~/HController && .venv/bin/pip install -e '.[panel]'`.

**2. Build the library's Python bindings into HController's venv.**

```bash
sudo apt install -y python3-dev cython3
git clone https://github.com/hzeller/rpi-rgb-led-matrix.git ~/rpi-rgb-led-matrix
```

Then follow `bindings/python/README.md` in `~/rpi-rgb-led-matrix`, using `~/HController/.venv/bin/python` wherever it says Python. Check it worked:

```bash
~/HController/.venv/bin/python -c "import rgbmatrix; print('ok')"
```

**3. Turn off onboard audio,** which the library refuses to share the hardware with, then reboot:

```bash
echo "blacklist snd_bcm2835" | sudo tee /etc/modprobe.d/blacklist-rgb-matrix.conf
sudo reboot
```

Optionally, add `isolcpus=3` to the end of the single line in `/boot/firmware/cmdline.txt` before rebooting, to give the panel's refresh thread a core of its own.

**4. Install the driver's service:**

```bash
cd ~/HController
sed "s#/home/pi#$HOME#g" deploy/hcontroller-panel.service | sudo tee /etc/systemd/system/hcontroller-panel.service > /dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now hcontroller-panel
journalctl -u hcontroller-panel -f
```

The panel should now show what the dashboard preview shows.

**Matching the driver to your panels.** The driver's defaults are `--rows 64 --cols 64 --chain 2 --gpio-mapping adafruit-hat`: two 64 × 64 panels in a chain, on an Adafruit HAT or Bonnet. For anything else, add flags to `ExecStart` in `/etc/systemd/system/hcontroller-panel.service`, then `sudo systemctl daemon-reload && sudo systemctl restart hcontroller-panel`. `.venv/bin/hcontroller-panel --help` lists them: `--rows` and `--cols` per panel, `--chain` and `--parallel` for how many, `--gpio-mapping` for the wiring (`regular` for direct wiring), and `--gpio-slowdown` for faster Pis.

**No hardware yet?** The driver can draw to an emulator instead, on any OS. Install the extra, start HController, then run the driver in a second terminal:

```bash
.venv/bin/pip install -e '.[emulator]'
.venv/bin/hcontroller-panel --emulate          # Windows: .venv\Scripts\hcontroller-panel --emulate
```

It draws the panel in your browser at **http://127.0.0.1:8888/**, through [RGBMatrixEmulator](https://github.com/ty-porter/RGBMatrixEmulator). The emulator writes its settings to `emulator_config.json` in the current folder the first time it runs.

## Using the dashboard

The dashboard has a section for each kind of device HController drives (just the LED panel so far), then one for the apps. Click a section's header to collapse it, and it shows a one-line summary instead. Your browser remembers which sections you've collapsed.

- **LED panel:**
  - A live LED-dot preview of exactly what the panel shows, with what's on now and why, and what comes next. The preview only streams while the section is open.
  - The **Show now** buttons.
  - **Settings:** the brightness slider, and the frame rate (how many frames a second HController fetches from the app on the panel).
  - The **Schedule:** a 24-hour strip of today's plan, and the list of time windows. You can add, reorder, edit and remove windows here.
- **Apps:** each module's status, uptime and restarts, plus Start / Stop / Restart, an **Open** link to the app's own page, its live **Logs**, and its **Config file** editor.

**Opening a module's page from another device.** The **Open** link uses the hostname you reached HController by, plus the module's port. So the module itself must listen on the network, not only on `127.0.0.1`. For nhlscreen, set `server.host: 0.0.0.0` in its config, which you can do from its **Config file** editor.

## The LED panel and its schedule

The panel shows exactly one app at a time. What it shows is decided in this order:

1. **Show now.** A button on the dashboard puts an app (or **Panel off**) on the panel until the schedule's next change, or until **Resume schedule**.
2. **Windows,** checked top to bottom. The first window that's on now and whose app is **healthy** gets the panel. If its app is down, the next window gets it. An `off` window darkens the panel.
3. **The default** (`display.default`): an app, or `off`.

```yaml
timezone: America/Toronto
display:
  default: nhlscreen
  schedule:
    - {module: off, days: daily, start: "01:00", end: "07:00"}                 # dark overnight
    - {module: weather, days: weekdays, start: "07:00", end: "08:30", brightness: 40}
```

Window fields:
- `days`: `daily`, `weekdays`, `weekends`, or a list like `[mon, wed, fri]`.
- Times: local to `timezone`. An end time earlier than the start runs past midnight, and `days` refers to the day the window starts. Start equal to end is a full 24 hours.
- `brightness` (optional): overrides the slider while the window is on.
- Quote the times. Unquoted `18:30` is read by YAML 1.1 as a number, which HController understands, but quotes are clearer.

Schedule edits on the dashboard are saved in `hcontroller-state.json` and take priority over `hcontroller.yaml` until **Reset to config**. The same file remembers Show now, the brightness, the frame rate, and your Start/Stop choices, so they all survive a restart. A brightness or frame rate set on the dashboard takes priority over the one in `hcontroller.yaml`.

**Apps that only run around their windows.** Set `process: scheduled` on a module and it is started `warmup_seconds` (default 60) before each of its windows, then stopped when the window ends. This saves memory and CPU on the Pi. Such an app also runs:
- while it's the default and no window applies;
- while you've picked it with Show now.

It doesn't run while an `off` window higher in the list covers the time. Pressing Start or Stop on a scheduled app keeps it that way until its next scheduled start or stop. `process: always` apps (the default) run all the time; the schedule only decides whether they're on the panel.

## Modules

A module is any program; everything below is optional, and each piece unlocks a feature. **Writing a new module?** [docs/modules.md](docs/modules.md) is the full contract: what HController sends, what it expects back, the timings, and a template to start from.

| In `hcontroller.yaml` | What HController does with it |
|---|---|
| `command` (+ `cwd`, `env`) | runs it as a child process, restarts it if it crashes, keeps its output |
| `url` + `health` | checks it every 5 s; any 2xx is healthy (no `health`: an open port counts) |
| `display: {frame: /render/frame}` | it can be scheduled on the LED panel |
| `config_file` (+ `config_example`) | edit it from the dashboard |
| `ui` | an **Open** link to the app's own page |

```yaml
modules:
  nhlscreen:
    name: NHL Scoreboard
    cwd: ~/nhlscreen
    command: [.venv/bin/python, -m, nhlscreen]     # on Windows, this finds .venv\Scripts\python.exe
    url: http://127.0.0.1:9134
    health: /health
    ui: /
    display: {frame: /render/frame}
    config_file: config.yaml
    config_example: config.example.yaml
    process: always          # or: scheduled
    restart: on-failure      # always | on-failure | never
    log_ignore: ['"GET /(render/frame|health) HTTP/[\d.]+" 200']
```

**Frames.** `display.frame` returns the panel's pixels as raw RGB bytes (width × height × 3), with the size in `X-Width` / `X-Height` headers, exactly like nhlscreen's `GET /render/frame`. HController fetches it `display.fps` times a second (default 10, from 1 to 60; the dashboard can change it) from whichever app is on the panel. The app renders everything itself, including anything that animates. The frame size must match `display.width` × `display.height` (default 128 × 64).

**Running modules.**
- Each module runs in the foreground in its own process group, with stdout and stderr captured; modules that daemonize themselves aren't supported.
- Stop sends SIGTERM to the group, then SIGKILL after `stop_timeout` (10 s). On Windows it ends the module's main process instead.
- Crashes restart with a backoff of 1 s, 2 s, 4 s … up to 60 s. The backoff resets once the app has stayed up for a minute.
- If something is already listening on a module's port (a copy started by hand, or nhlscreen's own systemd unit), HController reports a **conflict** instead of starting a second copy.
- Output lines matching a `log_ignore` regex are dropped. Use this for the access-log line nhlscreen prints for every frame HController fetches: 10 a second would bury its real output, and on the Pi wear out the SD card through journald. Failed requests still show, because the pattern only matches `200`.

**Modules on other devices.** Give a module a `url` and no `command`. HController watches its health and can put its frames on the panel, but doesn't start or stop it.

**Config files.**
- YAML and JSON are parsed before saving, so a typo is shown with its line number instead of being written.
- The previous file is kept as `<name>.bak`. **Revert last save** puts it back and restarts the app, for a config that parses but makes the app crash.
- **Save & restart** applies a change straight away.

## Troubleshooting

| What you see | What to do |
|---|---|
| `hcontroller: ...` and it exits at startup | A mistake in `hcontroller.yaml`. The message names the module and the setting. |
| `no hcontroller.yaml here, so there are no modules` | Start it from the `HController` folder, or copy `hcontroller.example.yaml` to `hcontroller.yaml`. |
| A card shows `cwd ... doesn't exist` | The module isn't installed where `cwd` says. Install it, or fix `cwd`. |
| A card shows `.../.venv/bin/python doesn't exist` | The module's venv is missing. Run its install steps. |
| **conflict** | Another copy of that module holds its port: one you started by hand, or its own systemd service. Stop it, and HController starts its own within 5 s. |
| **crashed** or **unhealthy** | Open the card's **Logs** for the module's own error. |
| A frame size error under the preview | The module draws a different size than `display.width` × `display.height`. |
| The dashboard doesn't load from a phone | Check the address (`http://<machine>:1630/`), that both are on the same network, and the firewall (see [step 3](#3-start-it)). On Windows, try the machine's IP address instead of its name. |
| `ERROR: Package 'hcontroller' requires a different Python` | The venv was made with Python older than 3.11. Delete `.venv` and make it again with a newer Python (see [What you need](#what-you-need)). |
| The panel stays dark (Pi) | `journalctl -u hcontroller-panel -f` shows the driver's errors. Check that the dashboard preview shows something, and that the panel supply is on. |
| The panel flickers or shows noise (Pi) | Try a higher `--gpio-slowdown` (3 or 4) in the driver's service, then `sudo systemctl daemon-reload && sudo systemctl restart hcontroller-panel`. |

## For developers

### Tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                                   # ~90 tests, offline, ~20 s (they start real child processes)
.venv/bin/python tools/testpattern.py --port 9200     # a stand-in module with an animated test pattern
```

On Windows, use `.venv\Scripts\` in place of `.venv/bin/`.

`tools/testpattern.py` serves `/health` and `/render/frame`, and can misbehave on purpose (`--crash-after`, `--ignore-sigterm`, `--unhealthy`, `--slow-start`). Register it as a second module to try out the schedule without writing a real app.

### API

| | |
|---|---|
| `GET /api/modules`, `GET /api/modules/{id}` | status of every module / one module |
| `POST /api/modules/{id}/start` · `/stop` · `/restart` | lifecycle (409 for modules HController only watches) |
| `GET /api/modules/{id}/logs?lines=200&after=N` | recent output; `after` returns only lines newer than sequence number N |
| `GET/PUT /api/modules/{id}/config?restart=true` | the module's config file: `{"text": "..."}` |
| `POST /api/modules/{id}/config/revert?restart=true` | put back `<name>.bak` |
| `GET /api/display`, `PUT /api/display` | what's on the panel, why, and what's next; `{"brightness": 60}`, `{"fps": 15}`, or both |
| `POST /api/display/show`, `DELETE /api/display/show` | Show now: `{"module": "nhlscreen"}` or `{"module": "off"}`; DELETE resumes the schedule |
| `GET/PUT /api/display/schedule` | `{"schedule": {"windows": [...], "default": "nhlscreen"}}`; `{"schedule": null}` resets to the config |
| `GET /api/display/timeline?date=2026-10-19` | the day's plan, assuming every app is healthy |
| `GET /api/display/frame` | the panel's current frame (raw RGB, `X-Width`/`X-Height`/`X-Module` headers) |
| `WS /display/stream` | every panel frame: 5-byte header (uint16 width, uint16 height, brightness byte) + RGB |
| `WS /ws` | live dashboard updates: module status, panel changes, schedule edits |
| `GET /health` | HController's own health |

### Layout

```
src/hcontroller/
  config.py              hcontroller.yaml models + validation
  state.py               hcontroller-state.json (dashboard choices)
  controller.py          wires everything; the actions the API calls
  modules/manifest.py    one module's entry (ModuleSpec)
  modules/local.py       LocalRunner: child process, restarts, logs, health
  modules/external.py    ExternalRunner: watch-only modules
  modules/registry.py    which modules should be running, and making it so
  modules/configfile.py  module config editing (check, .bak, revert)
  display/schedule.py    pure schedule rules (windows, fall-through, Show now, DST)
  display/scheduler.py   applies the schedule every second
  display/source.py      pulls the current app's frames
  display/hub.py         fans frames out to previews and the LED driver
  panel/driver.py        hcontroller-panel (rpi-rgb-led-matrix)
  api/app.py             REST + WebSockets; api/dashboard.html
tools/testpattern.py     stand-in module for tests and demos
deploy/                  systemd units
docs/modules.md          the module contract, for writing new modules
```
