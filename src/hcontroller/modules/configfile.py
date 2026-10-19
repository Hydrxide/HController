"""Reading and saving a module's own config file from the dashboard.

YAML and JSON are parsed before saving, so a typo is reported instead of written. The previous
file is kept as ``<name>.bak``; revert puts it back (for a config that parses but makes the app
crash-loop).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from .manifest import ModuleSpec


class ConfigFileError(ValueError):
    pass


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def file_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return "yaml"
    if suffix == ".json":
        return "json"
    return "text"


def _path(spec: ModuleSpec) -> Path:
    path = spec.config_path()
    if path is None:
        raise ConfigFileError("this module has no config_file")
    return path


def read(spec: ModuleSpec) -> dict[str, Any]:
    path = _path(spec)
    example = spec.example_path()
    exists = path.exists()
    from_example = not exists and example is not None and example.exists()
    if exists:
        text = path.read_text(encoding="utf-8")
    elif from_example:
        text = example.read_text(encoding="utf-8")  # type: ignore[union-attr]
    else:
        text = ""
    return {
        "path": str(path),
        "name": path.name,
        "exists": exists,
        "from_example": from_example,
        "example": example.name if from_example and example else None,
        "text": text,
        "backup": backup_path(path).exists(),
        "format": file_format(path),
    }


def check(path: Path, text: str) -> None:
    kind = file_format(path)
    if kind == "yaml":
        try:
            yaml.safe_load(text)
        except yaml.MarkedYAMLError as exc:
            mark = exc.problem_mark or exc.context_mark
            where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
            raise ConfigFileError(f"{where}{exc.problem or exc.context}") from exc
        except yaml.YAMLError as exc:
            raise ConfigFileError(str(exc)) from exc
    elif kind == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigFileError(f"line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc


def _replace(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save(spec: ModuleSpec, text: str) -> None:
    path = _path(spec)
    check(path, text)
    if not path.parent.is_dir():
        raise ConfigFileError(f"folder {path.parent} doesn't exist")
    if path.exists():
        shutil.copy2(path, backup_path(path))
    _replace(path, text.encode("utf-8"))


def revert(spec: ModuleSpec) -> None:
    path = _path(spec)
    backup = backup_path(path)
    if not backup.exists():
        raise ConfigFileError("there's no backup to go back to")
    _replace(path, backup.read_bytes())
