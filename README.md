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

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp hcontroller.example.yaml hcontroller.yaml   # register your modules
.venv/bin/python -m hcontroller                # dashboard at http://<this machine>:1630/
```

On Windows, use `.venv\Scripts\` in place of `.venv/bin/`. Python 3.11 or newer is required.

Open **http://raspberrypi.local:1630/** (or the machine's IP) from any device on your network. There's no login, so anyone on your Wi-Fi can use it. The dashboard has four parts:

- **Panel:** a live LED-dot preview of exactly what the panel shows. It also shows what's on now and why, what comes next, the **Show now** buttons and the brightness slider.
- **Schedule:** a 24-hour strip of today's plan, and the list of time windows. You can add, reorder, edit and remove windows here.
- **Apps:** each module's status, uptime and restarts, plus Start / Stop / Restart, an **Open** link to the app's own page, its live **Logs**, and its **Config file** editor.

## Modules

A module is any program; everything below is optional, and each piece unlocks a feature:

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
    command: [.venv/bin/python, -m, nhlscreen]
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

**Frames.** `display.frame` returns the panel's pixels as raw RGB bytes (width × height × 3), with the size in `X-Width` / `X-Height` headers, exactly like nhlscreen's `GET /render/frame`. HController fetches it `display.fps` times a second (default 10) from whichever app is on the panel. The app renders everything itself, including anything that animates. The frame size must match `display.width` × `display.height` (default 128 × 64).

**Running modules.**
- Each module runs in the foreground in its own process group, with stdout and stderr captured; modules that daemonize themselves aren't supported.
- Stop sends SIGTERM to the group, then SIGKILL after `stop_timeout` (10 s).
- Crashes restart with a backoff of 1 s, 2 s, 4 s … up to 60 s. The backoff resets once the app has stayed up for a minute.
- If something is already listening on a module's port (a copy started by hand, or nhlscreen's own systemd unit), HController reports a **conflict** instead of starting a second copy.
- Output lines matching a `log_ignore` regex are dropped. Use this for the access-log line nhlscreen prints for every frame HController fetches: 10 a second would bury its real output, and on the Pi wear out the SD card through journald. Failed requests still show, because the pattern only matches `200`.

**Modules on other devices.** Give a module a `url` and no `command`. HController watches its health and can put its frames on the panel, but doesn't start or stop it.

**Config files.**
- YAML and JSON are parsed before saving, so a typo is shown with its line number instead of being written.
- The previous file is kept as `<name>.bak`. **Revert last save** puts it back and restarts the app, for a config that parses but makes the app crash.
- **Save & restart** applies a change straight away.

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

Schedule edits on the dashboard are saved in `hcontroller-state.json` and take priority over `hcontroller.yaml` until **Reset to config**. The same file remembers Show now, brightness, and your Start/Stop choices, so they all survive a restart.

**Apps that only run around their windows.** Set `process: scheduled` on a module and it is started `warmup_seconds` (default 60) before each of its windows, then stopped when the window ends. This saves memory and CPU on the Pi. Such an app also runs:
- while it's the default and no window applies;
- while you've picked it with Show now.

It doesn't run while an `off` window higher in the list covers the time. Pressing Start or Stop on a scheduled app keeps it that way until its next scheduled start or stop. `process: always` apps (the default) run all the time; the schedule only decides whether they're on the panel.

## API

| | |
|---|---|
| `GET /api/modules`, `GET /api/modules/{id}` | status of every module / one module |
| `POST /api/modules/{id}/start` · `/stop` · `/restart` | lifecycle (409 for modules HController only watches) |
| `GET /api/modules/{id}/logs?lines=200&after=N` | recent output; `after` returns only lines newer than sequence number N |
| `GET/PUT /api/modules/{id}/config?restart=true` | the module's config file: `{"text": "..."}` |
| `POST /api/modules/{id}/config/revert?restart=true` | put back `<name>.bak` |
| `GET /api/display`, `PUT /api/display` | what's on the panel, why, and what's next; `{"brightness": 60}` |
| `POST /api/display/show`, `DELETE /api/display/show` | Show now: `{"module": "nhlscreen"}` or `{"module": "off"}`; DELETE resumes the schedule |
| `GET/PUT /api/display/schedule` | `{"schedule": {"windows": [...], "default": "nhlscreen"}}`; `{"schedule": null}` resets to the config |
| `GET /api/display/timeline?date=2026-10-19` | the day's plan, assuming every app is healthy |
| `GET /api/display/frame` | the panel's current frame (raw RGB, `X-Width`/`X-Height`/`X-Module` headers) |
| `WS /display/stream` | every panel frame: 5-byte header (uint16 width, uint16 height, brightness byte) + RGB |
| `WS /ws` | live dashboard updates: module status, panel changes, schedule edits |
| `GET /health` | HController's own health |

## Raspberry Pi

```bash
git clone <this repo> ~/HController && cd ~/HController
python3 -m venv .venv && .venv/bin/pip install -e '.[panel]'
cp hcontroller.example.yaml hcontroller.yaml          # adjust module paths
sudo systemctl disable --now nhlscreen                 # HController runs nhlscreen now
sudo cp deploy/hcontroller.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hcontroller
journalctl -u hcontroller -f                           # HController and every module's output
```

**The LED panel.** `hcontroller-panel` is a separate process that runs as root, because [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) needs GPIO; HController and its modules stay unprivileged. It reads `ws://127.0.0.1:1630/display/stream`, draws each frame, applies the brightness, and blanks the panel if frames stop for 5 s. Setup:

1. Build the library's Python bindings into HController's venv. Follow `bindings/python/README.md` in that repository; it needs `python3-dev` and `cython3`.
2. Turn off onboard audio, which the library refuses to share the hardware with: `echo "blacklist snd_bcm2835" | sudo tee /etc/modprobe.d/blacklist-rgb-matrix.conf`, then reboot. Optionally add `isolcpus=3` to `/boot/firmware/cmdline.txt` to give the refresh thread its own core.
3. Install the unit: `sudo cp deploy/hcontroller-panel.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now hcontroller-panel`.

The defaults (`--rows 64 --cols 64 --chain 2 --gpio-mapping adafruit-hat`) match two 64 × 64 panels on an Adafruit RGB Matrix Bonnet; see `hcontroller-panel --help`.

Without hardware, `pip install -e '.[emulator]'` and run `.venv/bin/hcontroller-panel --emulate`. This draws the same stream with [RGBMatrixEmulator](https://github.com/ty-porter/RGBMatrixEmulator).

**Opening a module's page from another device.** The **Open** link uses the hostname you reached HController by, plus the module's port. So the module itself must listen on the LAN; for nhlscreen, set `server.host: 0.0.0.0` in its config, which you can do from the dashboard.

## Testing

```bash
.venv/bin/pytest -q                                   # ~90 tests, offline, ~20 s (they start real child processes)
.venv/bin/python tools/testpattern.py --port 9200     # a stand-in module with an animated test pattern
```

`tools/testpattern.py` serves `/health` and `/render/frame`, and can misbehave on purpose (`--crash-after`, `--ignore-sigterm`, `--unhealthy`, `--slow-start`). Register it as a second module to try out the schedule without writing a real app.

## Layout

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
```
