# HController: context for Claude sessions

Central controller for the user's LAN apps ("modules"), running on the same Pi 4 as the LED board. It supervises modules as child processes, shows their health and logs on a dashboard, edits their config files, and **owns the shared 128×64 LED panel**: a time-window scheduler decides which app's frames reach the panel. `README.md` is the user-facing guide; this file holds the decisions and conventions.

The first module is **nhlscreen** (`~/nhlscreen`, its own repo and CLAUDE.md). It runs unmodified: HController uses its `/health`, `GET /render/frame` (raw RGB + `X-Width`/`X-Height`), `/` page and `config.yaml`.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # setup
.venv/bin/pytest -q                                          # ~90 tests, offline, ~20s; spawn real child processes
.venv/bin/python -m hcontroller                              # uses ./hcontroller.yaml; dashboard on :1630
.venv/bin/python tools/testpattern.py --port 9200            # stand-in module (health + animated frames)
.venv/bin/hcontroller-panel --emulate                        # LED driver without hardware (needs '.[emulator]')
```

For a demo with nhlscreen, run it in replay mode as a module: `command: [.venv/bin/python, -m, nhlscreen, --replay, replays/synthetic, --speed, "10"]` with `cwd: ~/nhlscreen` (nhlscreen crashes if `--config` names a missing file; it picks up `./config.yaml` itself). macOS has no `timeout`; run servers as background tasks.

## Decisions (made by the user; keep them unless asked)

- **Topology:** everything on one device for now, but keep the door open for modules on other LAN devices. A module with `url` and no `command` is *external* (watched, schedulable, not launched). Future plan: an "agent" is just HController running on another device; the main one would add a `RemoteRunner` that calls the agent's `/api/modules/{id}/...` (same API). The `Runner` shape in `modules/runner.py` is that seam.
- **Child processes** (not systemd units or Docker): HController is the only systemd unit for apps; nhlscreen's own unit gets disabled on the Pi.
- **No login**, LAN only, **port 1630**.
- **The panel is HController's**, and nhlscreen is only one of several apps sharing it. The LED driver is a separate root process (`hcontroller-panel`) fed by `WS /display/stream`, so neither HController nor its modules run as root. This replaces nhlscreen's old "Pi matrix output" next step.
- **Scheduler semantics:**
  - Windows are an ordered list; the first active window whose app is healthy wins, and a down app falls through to the next window, then the default.
  - `off` windows and a default of `off` darken the panel.
  - Show now lasts until the schedule's next change (or Resume).
  - Each app picks `process: always` (runs all the time; the schedule only routes the panel) or `process: scheduled` (started `warmup_seconds` before its windows, stopped after).
  - No app "takeovers" of the panel yet (e.g. nhlscreen claiming it for a live game; its `/health` `tracked_games` would be the trigger). Not built, by choice. The resolver would take a "claims" input ahead of windows.
- Not built yet (route left open): RemoteRunner/agent, a reverse proxy for module UIs (nhlscreen's page uses absolute URLs, so the dashboard links to the module's own port), "update module" (git pull + reinstall), rotation between overlapping windows.

## Architecture

```
hcontroller.yaml -> config.py (pydantic) ──► controller.py wires:
  modules/registry.py ── LocalRunner (local.py: process group, restart backoff, logs, health)
                     └── ExternalRunner (external.py: health only)
  display/scheduler.py ── schedule.py (pure rules) ──► display/source.py (pulls frames) ──► display/hub.py
                                                                      WS /display/stream ◄──┘ (dashboard + hcontroller-panel)
  state.py (hcontroller-state.json) · events.py (bus -> WS /ws) · api/app.py + api/dashboard.html
```

| Module | Role |
|---|---|
| `display/schedule.py` | **Pure** schedule logic: `resolve`, `wanted`, `next_change`, `next_wanted_change`, `timeline`. All instants are compared in UTC: two datetimes sharing a `ZoneInfo` compare wall-clock time, which breaks on DST days. Tested with fixed datetimes, including both 2026 DST changes. |
| `display/scheduler.py` | Ticks every second and on every module status change; applies the decision (source target, brightness), pushes `wanted` to the registry, persists Show now / schedule / brightness. |
| `modules/registry.py` | Desired state per module: `always` = autostart or the saved Start/Stop (`state.running`); `scheduled` = scheduler's `wanted`, unless a dashboard hold (`state.holds`) applies until the app's next scheduled change. Acts only when desired state *changes*, so a module that exits under its restart policy stays stopped. |
| `modules/local.py` | `start_new_session` so stop signals the whole group and terminal Ctrl+C doesn't hit modules; after any exit it SIGKILLs leftovers in the group; a port probe before spawning reports `conflict` instead of a duplicate. |
| `display/source.py` | Keeps the last good frame for 2s through fetch errors, then black; dedupes identical frames but resends each second (the LED driver blanks after 5s of silence). |

## Gotchas found while building

- YAML 1.1: unquoted `off` is `False` and unquoted `18:30` is the integer 1110 (base 60). `normalize_target` and `parse_clock` handle both.
- httpx logs every request at INFO (10 frame fetches a second); `__main__` turns it down to WARNING.
- The same polling floods a module's own access log (nhlscreen's uvicorn logs every `GET /render/frame`). The per-module `log_ignore` regexes drop those lines; the example config sets one for nhlscreen.
- uvicorn re-raises SIGTERM after a clean shutdown, so a gracefully stopped module exits with -15; `_run` treats that as a normal stop.
- Starlette's TestClient warns that httpx support is deprecated; it's filtered in `pyproject.toml`.

## Working conventions

- Tests use real child processes (`tools/testpattern.py`) with `FAST` runner timings from `tests/conftest.py`; helper names must not start with `test`. No network beyond localhost.
- The dashboard is re-read per request (`api/dashboard.html`), Python isn't: restart HController after backend changes.
- Update `README.md` when user-facing behavior changes. Python ≥3.11 (Raspberry Pi OS Bookworm ships 3.11).
