# Writing a module for HController

A **module** is any app HController runs, watches, or puts on the LED panel. HController treats a module as a black box: it starts a command, talks to it over HTTP on the local network, and reads its output. There is nothing to import and no SDK, so a module can be written in any language. Each feature below is optional, and each one you add unlocks something on the dashboard.

This page is the full contract: what HController sends, what it expects back, and the timings it uses. Working examples:

- [nhlscreen](https://github.com/Hydrxide/nhlscreen) is the first real module and uses every feature.
- `tools/testpattern.py` in this repo is the smallest complete one, using only the Python standard library.
- [A template](#template-a-minimal-module) is at the end of this page.

## Contents

- [What each feature needs](#what-each-feature-needs)
- [Running under HController (`command`)](#running-under-hcontroller-command)
- [Being reachable (`url`)](#being-reachable-url)
- [Health (`health`)](#health-health)
- [Frames for the LED panel (`display`)](#frames-for-the-led-panel-display)
- [Its config file (`config_file`)](#its-config-file-config_file)
- [Its own web page (`ui`)](#its-own-web-page-ui)
- [Output and logs](#output-and-logs)
- [Running only around its schedule (`process: scheduled`)](#running-only-around-its-schedule-process-scheduled)
- [Apps on another device](#apps-on-another-device)
- [Registering it in `hcontroller.yaml`](#registering-it-in-hcontrolleryaml)
- [What HController doesn't do (yet)](#what-hcontroller-doesnt-do-yet)
- [Checking a module before registering it](#checking-a-module-before-registering-it)
- [Template: a minimal module](#template-a-minimal-module)

## What each feature needs

| The module provides | Key in `hcontroller.yaml` | HController adds |
|---|---|---|
| A command that runs in the foreground | `command`, `cwd`, `env` | Starts and stops it, restarts it after crashes, keeps its output for the dashboard and the system log |
| An HTTP port | `url` | Lets HController reach it; without `health`, an open port counts as healthy |
| A health endpoint | `health` | Health status on the dashboard; an unhealthy app is skipped for the panel |
| A frame endpoint | `display: {frame: ...}` | Can be scheduled on the LED panel and picked with **Show now** |
| A YAML or JSON config file | `config_file`, `config_example` | Config editor on the dashboard, with syntax checks, a backup and revert |
| A web page | `ui` | An **Open** link on its dashboard card |

Every module needs at least a `command` (HController runs it) or a `url` (HController only watches it). `health`, `display` and `ui` all need a `url`.

## Running under HController (`command`)

### How it's started

- **Command:** `command` is an argument list, not a shell string, so there's no shell expansion, pipes or `&&`. If the first item contains a `/` and is relative, such as `.venv/bin/python`, it's resolved against `cwd`.
- **Working directory:** the process runs in `cwd`. A relative `cwd` is relative to `hcontroller.yaml`, and `~` is expanded.
- **Environment:** the module gets HController's own environment, plus `PYTHONUNBUFFERED=1`, plus anything in `env`. On the Pi, HController runs under systemd, which sets only a minimal `PATH`. Point `command` at the module's own venv, such as `.venv/bin/python`, rather than relying on `python` being on the `PATH`.
- **Input and output:** stdin is `/dev/null`, and stdout and stderr are both captured (see [Output and logs](#output-and-logs)).
- **User:** it runs as HController's user (`pi` on the Pi), never as root.

### Requirements

- **Stay in the foreground.** HController treats the process exiting as the app stopping. A program that forks into the background and exits looks like a crash and gets restarted in a loop. Don't daemonize, and don't write a PID file.
- **Stop on SIGTERM.** On Linux and macOS, the module runs in its own process group, and stopping it sends SIGTERM to the whole group. After `stop_timeout` (10 s by default), it sends SIGKILL. When the process exits, anything left in its group is killed with SIGKILL, so its port is free for the next start.
  - Child processes are fine; they get the same signals.
  - A child that calls `setsid()`, or otherwise leaves the group, escapes both the stop signal and the cleanup.
  - Exiting with code 0, or being ended by the SIGTERM itself (as uvicorn does after a clean shutdown), counts as a clean stop.
- **Use the exit code to say what happened.** With the default `restart: on-failure`, a non-zero exit (or a signal) is a crash: HController restarts it after 1 s, 2 s, 4 s … up to 60 s, and the wait resets once the app has stayed up for a minute. Exit code 0 means "done on purpose" and the app stays stopped. `restart: always` restarts after any exit, and `restart: never` never restarts.
- **Expect to be restarted at any time.** Crashes, config saves, the dashboard's Restart button, and stops and starts around a schedule all restart it. Keep anything that must survive on disk, and start quickly.
- **Don't run a second copy yourself.** Before every start, HController checks whether something already answers on the module's `url` port. If something does, such as a copy started by hand or the module's own systemd unit, it shows **conflict** and checks again every 5 s instead of starting a duplicate. On the Pi, disable the module's own systemd unit.

On Windows, HController starts the module in a new process group and stops it with `terminate()` and then `kill()`, which only reach the main process.

## Being reachable (`url`)

- **Address:** `url` is where HController reaches the module, normally `http://127.0.0.1:<port>` for a module on the same machine. `https` also works.
- **Unique port:** each module needs its own host and port. Two modules with the same one, or a module using HController's own port (1630), is an error at startup. `localhost` counts as `127.0.0.1` for this check.
- **Interface:** binding `127.0.0.1` is enough for HController itself. Bind `0.0.0.0` if other devices should open the module's page (see [`ui`](#its-own-web-page-ui)).
- **No login:** the endpoints HController calls can't require a login, because HController sends no credentials. HController is built for a trusted LAN with no logins at all.
- **No redirects:** HController doesn't follow redirects, so `health` and `display.frame` must answer directly rather than redirecting.

## Health (`health`)

`health` is a path, such as `/health`. HController sends `GET <url><health>` and checks the answer:

| Answer | Counts as |
|---|---|
| Any 2xx status | healthy (the body is ignored, so it can carry diagnostics) |
| Any other status, including 3xx; no answer within 2 s; connection refused | a failed check |

**Timings:**
- **How often:** every 5 s while healthy, and every second otherwise.
- **After starting:** the app shows **starting** until its first healthy answer. If it hasn't answered after 60 s, it shows **unhealthy**, but keeps running.
- **Once it has answered:** two failed checks in a row make it **unhealthy**, and one healthy answer clears that.
- **No `health` path:** an open TCP port on the `url` counts as healthy. With no `url` at all, the module counts as healthy as soon as it's running.

**What healthy should mean.** Health decides who gets the LED panel: an unhealthy app is skipped, and the next schedule window (or the default) gets the panel instead. So return 2xx whenever the app can show something sensible, even if its data is a little stale. Save non-2xx for "showing my frames now would be worse than showing the next app". nhlscreen deliberately reports healthy while the NHL API is slow. If it reported unhealthy and it was the default app, the panel would go dark instead of showing slightly old scores.

**Keep the handler fast.** Don't make outbound calls from the handler; report state the app already has.

## Frames for the LED panel (`display`)

`display: {frame: /render/frame}` means the app can go on the panel. While the app is on the panel, HController sends `GET <url><frame>` repeatedly and passes each answer to the LED driver and to the dashboard preview.

### The response

| | Required |
|---|---|
| Status | exactly `200` |
| Body | `width × height × 3` bytes: 8-bit R, G, B for each pixel, row by row from the top-left, with no padding or header. 128 × 64 is 24,576 bytes. |
| `X-Width`, `X-Height` headers | the frame size in pixels; it must equal `display.width` × `display.height` in `hcontroller.yaml` (default 128 × 64) |
| `Content-Type` | ignored; `application/octet-stream` is conventional |
| `Cache-Control: no-store` | recommended |

With Pillow, an image in `RGB` mode gives exactly this body from `image.tobytes()`.

### How it's called

- **Only while the app is on the panel:** frames are requested `display.fps` times a second (default 10, adjustable from 1 to 60 on the dashboard) and not at all otherwise. Nothing tells the app when it gains or loses the panel.
- **One request at a time, with a 1 s timeout:** a slow answer lowers the frame rate. Aim for a few milliseconds per frame; nhlscreen renders in under 1 ms and the template on this page in about 1 ms.
- **Errors:** a non-200 status, a wrong size or a timeout shows an error on the dashboard. The panel keeps the last good frame for 2 s, then goes black. Frame errors don't mark the app unhealthy; only `health` does that.

### Rendering rules

- **Render from the clock and the app's state, never from the request count.** Requests aren't evenly spaced, can be skipped, and may come from other clients too, such as the app's own preview page. Each request should return what the screen looks like *now*. Anything that moves, such as a sweep, a countdown or a scrolling ticker, is computed from the current time. Don't use frame requests to drive the app's own logic either, since they stop whenever another app is on the panel.
- **Black `(0, 0, 0)` is an unlit LED.** Draw on black.
- **Render at full intensity.** HController applies brightness (the dashboard slider, or a window's `brightness`) in the LED driver. An app that dims its own colours gets dimmed twice.
- **Size:** design for 128 × 64. If the app's size is configurable, as nhlscreen's `display.matrix_width` and `matrix_height` are, keep it equal to HController's `display.width` and `display.height`. A mismatch is reported, not scaled.

## Its config file (`config_file`)

- **Location:** `config_file` is a path relative to `cwd`, or an absolute path.
- **Syntax checks:** `.yaml`, `.yml` and `.json` files are parsed before saving, and a typo is shown with its line and column instead of being written. Any other extension is saved as plain text, unchecked.
- **Saving:** the new text goes to a temporary file that then replaces the config (it isn't edited in place). The previous version is kept as `<name>.bak`, and **Revert last save** puts that back. Add `*.bak` and the real config file to the module's `.gitignore`.
- **Restarting:** **Save & restart** restarts the app, but only if it's running, and **Save** alone doesn't restart it. So reading the config once at startup is enough. If the app watches the file for changes instead, it must handle the file being replaced rather than modified.
- **No config file yet:** while the file doesn't exist, the editor shows `config_example` (for example `config.example.yaml`), and saving creates the real file. Ship an example file, and make the app start with sensible defaults when its config file is missing, as nhlscreen does.
- **Don't keep secrets there:** anyone on the LAN can read and edit this file from the dashboard, which has no login.

## Its own web page (`ui`)

`ui` is a path on the module's `url`, such as `/`, and gives the dashboard card an **Open** link that opens in a new tab.

- **The link's address:** when `url` points at this machine (`127.0.0.1` or `localhost`), the link uses whatever hostname the browser used to reach HController, plus the module's port, such as `http://raspberrypi.local:9134/`. For that to work from a phone or laptop, the module must listen on `0.0.0.0`, not only on `127.0.0.1`.
- **Its own origin:** HController doesn't proxy module pages, so the page runs at the module's own address. Absolute paths like `/static/app.js` are fine.

## Output and logs

- **Capture:** stdout and stderr are merged and split into lines. Each line goes to the dashboard's **Logs** view, which keeps the last 2,000 lines per module, and to HController's own log (journald on the Pi: `journalctl -u hcontroller`).
- **Lines:** lines longer than 4,000 characters are cut. Output is a pipe, not a terminal, so most tools turn colours off by themselves; if not, turn them off, because escape codes show up raw. HController adds no timestamps to the module's lines, so include your own if you want them.
- **Flushing:** flush output line by line. HController sets `PYTHONUNBUFFERED=1` for Python; apps in other languages should flush after each line, or their logs arrive in bursts.
- **Noise:** don't log every frame or health request. At 10 frames a second, those lines bury everything else and wear out the Pi's SD card. Either turn the access log off in the app (`uvicorn.run(..., access_log=False)`), or drop the lines in `hcontroller.yaml` with `log_ignore` regexes. This one, used for nhlscreen, drops only *successful* frame and health requests, so failures still show:

  ```yaml
  log_ignore: ['"GET /(render/frame|health) HTTP/[\d.]+" 200']
  ```

## Running only around its schedule (`process: scheduled`)

By default (`process: always`) a module runs all the time, and the schedule only decides whether it's on the panel. With `process: scheduled`, HController runs it only when it might be needed:

- **When it runs:**
  - from `warmup_seconds` (default 60) before a window in which it could get the panel, until the window ends;
  - while one of its windows is on, even if a higher window's app holds the panel, so it's ready to take over if that app goes down;
  - while it's the default and no window applies;
  - while it's picked with **Show now**.

  It doesn't run while an `off` window higher in the list covers the time.
- **Start and Stop on the dashboard:** pressing either holds that choice until the app's next scheduled start or stop.
- **Stopping:** works the same way as any other stop: SIGTERM, then SIGKILL after `stop_timeout`.

**Requirements:**
- **Be ready in time:** be healthy, with its first data fetched, within `warmup_seconds`. Raise `warmup_seconds` if it needs longer.
- **Expect frequent stops and starts:** at least once a day.
- **Keep state on disk:** anything it must remember between runs, such as caches or the user's choices, goes on disk.
- **Needs a `command`:** a module that only has a `url` can't be scheduled this way.

## Apps on another device

A module with a `url` and no `command` is **external**. HController doesn't start, stop or restart it; the dashboard shows **up** or **down** instead.

- **Health:** checked every 5 s. Two failed checks in a row mark it **down**, and so does a failure on the very first check.
- **Panel:** it can still go on the panel. Frames then cross the network: at 128 × 64 and 10 fps that's about 250 KB/s (2 Mbit/s) while it's on the panel, and each frame must arrive within the 1 s timeout. Wired Ethernet or solid Wi-Fi is enough.
- **Listening:** the app must listen on an interface the controller can reach, not on `127.0.0.1`.
- **Not available:** logs and the config editor only work for modules HController runs itself.

## Registering it in `hcontroller.yaml`

Add the module under `modules:`, then restart HController, which only reads this file at startup. A mistake stops HController at startup with a message naming the module and the problem.

```yaml
modules:
  clock:                        # the id: lowercase letters, digits, - and _ (not "off")
    name: Clock                 # shown on the dashboard (default: the id)

    # Running it (omit `command` for an app HController only watches)
    cwd: ~/clock                # relative paths here are relative to hcontroller.yaml
    command: [.venv/bin/python, clock.py, --port, "9300"]   # strings only: quote numbers
    env: {TZ: America/Toronto}  # added to HController's environment
    autostart: true             # process: always apps: start with HController (default true)
    process: always             # always | scheduled
    warmup_seconds: 60          # process: scheduled: start this long before its windows
    restart: on-failure         # always | on-failure | never
    stop_timeout: 10            # seconds from SIGTERM to SIGKILL
    log_ignore: []              # regexes; matching output lines are dropped

    # Talking to it
    url: http://127.0.0.1:9300  # needed by health, display and ui
    health: /health             # any 2xx = healthy; without it, an open port counts
    display: {frame: /render/frame}   # can go on the LED panel
    ui: /                       # an Open link to its own page
    config_file: config.yaml    # relative to cwd
    config_example: config.example.yaml   # shown in the editor until config_file exists
```

Then put it on the panel from the dashboard: **Show now** to try it straight away, or a window in **Schedule** (see [The LED panel and its schedule](../README.md#the-led-panel-and-its-schedule) in the README). A schedule window or `display.default` can only name a module that has `display`.

## What HController doesn't do (yet)

These are deliberate limits of the current build. Don't design a module that depends on them:

- **Takeovers:** a module can't ask for the panel, for example to show a goal alert while another app's window is on. The schedule alone decides. The planned way to add this is a "claim" checked ahead of the windows.
- **Being told it's on the panel:** a module isn't told when it gains or loses the panel, or what the brightness is.
- **Panel size:** HController doesn't pass the panel size to the module; each side's config has to agree.
- **Proxying pages:** module pages aren't served through HController's port.
- **Installing and updating:** HController doesn't install or update modules; set them up yourself (clone, create the venv, install).
- **Messaging between modules:** there's none. Modules can call each other's HTTP APIs directly if they need to.

## Checking a module before registering it

Start the module by hand from its `cwd`, then:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9300/health              # 200 (any 2xx)
curl -s -D - -o frame.bin http://127.0.0.1:9300/render/frame | grep -i '^HTTP\|^x-'  # 200, x-width: 128, x-height: 64
wc -c < frame.bin                                                                   # 24576
kill -TERM <pid>                                                                    # exits within 10 s
```

Then stop it, so HController doesn't find the port taken and report a **conflict**, register it, and restart HController. The app should go from **starting** to **healthy** on the dashboard, and **Show now** should put it on the preview.

## Template: a minimal module

A complete module using FastAPI and Pillow, the same stack as nhlscreen. It shows the time and answers in about a millisecond per frame.

```python
"""A minimal HController module: the time, on the LED panel."""

import argparse
import time

import uvicorn
from fastapi import FastAPI, Response
from PIL import Image, ImageDraw

WIDTH, HEIGHT = 128, 64  # must match display.width / display.height in hcontroller.yaml

app = FastAPI()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/render/frame")
def frame() -> Response:
    image = Image.new("RGB", (WIDTH, HEIGHT))  # black = LEDs off
    ImageDraw.Draw(image).text((40, 26), time.strftime("%H:%M:%S"), fill=(255, 176, 0))
    return Response(
        image.tobytes(),  # width * height * 3 bytes, row by row from the top left
        media_type="application/octet-stream",
        headers={"X-Width": str(WIDTH), "X-Height": str(HEIGHT), "Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9300)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
```

Set it up in its own folder, then register it:

```bash
mkdir ~/clock && cd ~/clock          # save the code above as clock.py
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn pillow
```

```yaml
modules:
  clock:
    name: Clock
    cwd: ~/clock
    command: [.venv/bin/python, clock.py, --port, "9300"]
    url: http://127.0.0.1:9300
    health: /health
    display: {frame: /render/frame}
```

Pillow's built-in font is small and plain. For real layouts, nhlscreen's `render/` package shows bitmap fonts and pixel-exact drawing at this size.
