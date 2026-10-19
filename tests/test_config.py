from pathlib import Path

import pytest

from hcontroller.config import ConfigError, load_config
from hcontroller.display.schedule import OFF
from hcontroller.state import State

ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path, text: str) -> Path:
    path = tmp_path / "hcontroller.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_config_loads():
    config = load_config(ROOT / "hcontroller.example.yaml")
    assert config.server.port == 1630
    assert config.default == "nhlscreen"
    nhl = config.modules["nhlscreen"]
    assert nhl.cwd == Path.home() / "nhlscreen"
    assert nhl.config_path() == Path.home() / "nhlscreen" / "config.yaml"
    assert config.display.schedule[0].module == OFF  # unquoted "off" in YAML is False


def test_no_config_means_no_modules_and_panel_off():
    config = load_config(None)
    assert config.modules == {} and config.default == OFF


def test_yaml_quirks_and_relative_paths(tmp_path):
    (tmp_path / "app").mkdir()
    path = write(
        tmp_path,
        """
display:
  default: off
  schedule:
    - {module: clock, start: 18:30, end: 22:00, days: weekends}
modules:
  clock: {cwd: app, command: [./run], url: "http://localhost:9300/", display: {}}
""",
    )
    config = load_config(path)
    window = config.display.schedule[0]
    assert (window.start, window.end, window.days) == ("18:30", "22:00", "weekends")
    assert config.default == OFF
    clock = config.modules["clock"]
    assert clock.cwd == (tmp_path / "app").resolve()
    assert clock.url == "http://localhost:9300"
    assert clock.endpoint("/render/frame") == "http://localhost:9300/render/frame"


def test_default_is_the_first_app_with_a_display(tmp_path):
    path = write(
        tmp_path,
        """
modules:
  logger: {command: [logger]}
  clock: {url: "http://127.0.0.1:9300", display: {}}
""",
    )
    assert load_config(path).default == "clock"


@pytest.mark.parametrize(
    "text, message",
    [
        ("display: {schedule: [{module: nope, start: '1:00', end: '2:00'}]}", "no app called 'nope'"),
        ("modules: {logger: {command: [x]}}\ndisplay: {default: logger}", "no display.frame"),
        ("timezone: Mars/Base", "unknown timezone"),
        ("modules: {Bad: {command: [x]}}", "module id 'Bad'"),
        ("modules: {'off': {command: [x]}}", "module id 'off'"),
        ("modules: {x: {name: lonely}}", "needs a command"),
        ("modules: {x: {command: [x], health: /h}}", "need a url"),
        ("modules: {x: {url: 'ftp://host'}}", "should look like http"),
        ("modules: {x: {url: 'http://h:1', process: scheduled}}", "needs a command"),
        ("modules: {x: {command: [x], config_file: c.yaml}}", "needs a cwd"),
        ("modules: {x: {command: [x], comand: [y]}}", "comand"),
        (
            "modules: {a: {url: 'http://localhost:9000'}, b: {url: 'http://127.0.0.1:9000'}}",
            "both use 127.0.0.1:9000",
        ),
        ("modules: {a: {url: 'http://127.0.0.1:1630'}}", "HController's own"),
        ("display: {schedule: [{module: x, start: '25:00', end: '2:00'}]}", "not a time"),
        ("display: {fps: 0.5}", "fps"),
        ("[1, 2", "while parsing"),
    ],
)
def test_bad_configs_say_what_is_wrong(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message.replace("(", r"\(")):
        load_config(write(tmp_path, text))


def test_state_round_trip_and_removal(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    state.set("brightness", 40)
    state.set("running", {"a": True})
    assert State(path).get("brightness") == 40
    copy = State(path).get("running")
    copy["a"] = False  # get() hands out copies
    assert State(path).get("running") == {"a": True}
    state.set("brightness", None)
    state.set("running", None)
    assert not path.exists()
    path.write_text("{not json", encoding="utf-8")
    assert State(path).get("brightness") is None
