"""``hcontroller.yaml``: server, timezone, the LED panel and its schedule, and the module registry."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .display.schedule import OFF, Window, check_schedule, normalize_target
from .modules.manifest import ModuleSpec, is_loopback

MODULE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(ValueError):
    """hcontroller.yaml can't be read or isn't valid."""


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"  # the dashboard is meant for the whole LAN
    port: int = Field(1630, ge=1, le=65535)


class DisplayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(128, gt=0, le=1024)
    height: int = Field(64, gt=0, le=1024)
    fps: float = Field(10, gt=0, le=60)
    brightness: int = Field(70, ge=1, le=100)
    default: str | None = None  # app shown when no window applies, or "off"; unset = first app with a display
    schedule: list[Window] = Field(default_factory=list)

    @field_validator("default", mode="before")
    @classmethod
    def _default(cls, value: Any) -> str | None:
        return None if value is None else normalize_target(value)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: ServerConfig = Field(default_factory=ServerConfig)
    timezone: str = "America/Toronto"
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    modules: dict[str, ModuleSpec] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, tz: str) -> str:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {tz!r} (use an IANA name like America/Toronto)") from exc
        return tz

    @field_validator("modules")
    @classmethod
    def _module_ids(cls, modules: dict[str, ModuleSpec]) -> dict[str, ModuleSpec]:
        for module_id in modules:
            if module_id == OFF or not MODULE_ID.match(module_id):
                raise ValueError(f"module id {module_id!r}: use lowercase letters, digits, - and _ (and not 'off')")
        return modules

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        display = self.display
        if display.default is None:
            display.default = next((mid for mid, spec in self.modules.items() if spec.display), OFF)
        check_schedule(display.schedule, display.default, self.modules)

        seen: dict[tuple[str, int], str] = {}
        for module_id, spec in self.modules.items():
            address = spec.address()
            if address is None:
                continue
            if address in seen:
                raise ValueError(f"modules {seen[address]!r} and {module_id!r} both use {address[0]}:{address[1]}")
            seen[address] = module_id
            if address[1] == self.server.port and is_loopback(address[0]):
                raise ValueError(f"module {module_id!r} uses port {address[1]}, which is HController's own")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def default(self) -> str:
        return self.display.default or OFF


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    base = path.resolve().parent
    for spec in config.modules.values():
        spec.resolve_paths(base)
    return config
