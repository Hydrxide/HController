"""Choices made from the dashboard, saved next to the config in ``hcontroller-state.json``.

One key per choice (``running``, ``holds``, ``schedule``, ``override``, ``brightness``, ``fps``), so each
can be reset on its own without touching the others; same idea as nhlscreen's state file.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._data = self._read()

    def _read(self) -> dict[str, Any]:
        if not (self.path and self.path.exists()):
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self._data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        """Save ``value`` under ``key``; None removes the key."""
        if value is None:
            if self._data.pop(key, None) is None:
                return
        else:
            self._data[key] = copy.deepcopy(value)
        self._write()

    def _write(self) -> None:
        if not self.path:
            return
        try:
            if self._data:
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, self.path)
            else:
                self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not save state to %s: %s", self.path, exc)
