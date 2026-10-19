"""One module's entry in hcontroller.yaml: how to run it and what it offers.

Every capability is optional: a ``command`` makes it a local process HController supervises;
a ``url`` alone makes it an external app HController only watches (it may run on another
device). ``health``, ``display.frame``, ``ui`` and ``config_file`` each unlock a feature.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def is_loopback(host: str) -> bool:
    return host.lower() in _LOOPBACK or host.startswith("127.")


def _http_path(value: str | None) -> str | None:
    if value is not None and not value.startswith("/"):
        raise ValueError(f"{value!r} must be a path starting with /")
    return value


class DisplaySpec(BaseModel):
    """The app can go on the LED panel: ``frame`` returns raw RGB with X-Width/X-Height headers."""

    model_config = ConfigDict(extra="forbid")

    frame: str = "/render/frame"

    @field_validator("frame")
    @classmethod
    def _frame(cls, value: str) -> str:
        return _http_path(value)


class ModuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    cwd: Path | None = None  # relative paths are relative to hcontroller.yaml
    command: list[str] | None = None  # runs in the foreground; a relative program path is relative to cwd
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None  # where HController reaches the app, e.g. http://127.0.0.1:9134
    health: str | None = None  # any 2xx = healthy
    ui: str | None = None  # the app's own web page
    display: DisplaySpec | None = None
    config_file: str | None = None  # editable from the dashboard (YAML/JSON are checked before saving)
    config_example: str | None = None  # seeds the editor while config_file doesn't exist
    autostart: bool = True  # process: always apps only
    process: Literal["always", "scheduled"] = "always"
    warmup_seconds: float = Field(60, ge=0)  # scheduled apps start this long before their window
    restart: Literal["always", "on-failure", "never"] = "on-failure"
    stop_timeout: float = Field(10, gt=0)  # SIGTERM, then SIGKILL after this long
    # Output lines matching any of these regexes are dropped, e.g. the access-log line for every
    # frame HController fetches (10 a second would bury everything else and wear the Pi's SD card).
    log_ignore: list[str] = Field(default_factory=list)

    _log_ignore: list[re.Pattern[str]] = PrivateAttr(default_factory=list)

    @field_validator("log_ignore")
    @classmethod
    def _patterns(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"log_ignore pattern {pattern!r}: {exc}") from exc
        return patterns

    def model_post_init(self, __context: Any) -> None:
        self._log_ignore = [re.compile(pattern) for pattern in self.log_ignore]

    def ignores(self, line: str) -> bool:
        return any(pattern.search(line) for pattern in self._log_ignore)

    @field_validator("cwd", mode="before")
    @classmethod
    def _cwd(cls, value: Any) -> Any:
        return Path(value).expanduser() if value is not None else None

    @field_validator("command")
    @classmethod
    def _command(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not value:
            raise ValueError("command can't be empty")
        return value

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"url {value!r} should look like http://host:port")
        return value.rstrip("/")

    @field_validator("health", "ui")
    @classmethod
    def _paths(cls, value: str | None) -> str | None:
        return _http_path(value)

    @model_validator(mode="after")
    def _consistent(self) -> ModuleSpec:
        if not self.command and not self.url:
            raise ValueError("needs a command (to run it) or a url (to watch it)")
        if (self.health or self.ui or self.display) and not self.url:
            raise ValueError("health, ui and display need a url")
        if self.process == "scheduled" and not self.command:
            raise ValueError("process: scheduled needs a command")
        for key in ("config_file", "config_example"):
            value = getattr(self, key)
            if value and not Path(value).expanduser().is_absolute() and self.cwd is None:
                raise ValueError(f"{key} needs a cwd (or an absolute path)")
        return self

    def resolve_paths(self, base: Path) -> None:
        """Make a relative cwd relative to the config file's folder."""
        if self.cwd is not None and not self.cwd.is_absolute():
            self.cwd = (base / self.cwd).resolve()

    @property
    def local(self) -> bool:
        return self.command is not None

    def endpoint(self, path: str) -> str:
        assert self.url, "module has no url"
        return f"{self.url}{path}"

    def address(self) -> tuple[str, int] | None:
        """(host, port) of ``url``, with localhost spelled 127.0.0.1 so duplicates are caught."""
        if not self.url:
            return None
        parts = urlsplit(self.url)
        host = (parts.hostname or "").lower()
        if host == "localhost":
            host = "127.0.0.1"
        return host, parts.port or (443 if parts.scheme == "https" else 80)

    def _path(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() or self.cwd is None else self.cwd / path

    def config_path(self) -> Path | None:
        return self._path(self.config_file)

    def example_path(self) -> Path | None:
        return self._path(self.config_example)
