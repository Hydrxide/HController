"""Entry point: ``python -m hcontroller [--config hcontroller.yaml]``."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .api.app import create_app
from .config import ConfigError, load_config
from .controller import Controller


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hcontroller", description="Runs LAN module apps and the shared LED panel")
    parser.add_argument("--config", "-c", help="YAML config file (default: ./hcontroller.yaml if present)")
    parser.add_argument("--host", help="override server.host")
    parser.add_argument("--port", type=int, help="override server.port")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # httpx logs every request at INFO: 10 frame fetches a second plus health checks.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("hcontroller")

    config_path = args.config or ("hcontroller.yaml" if Path("hcontroller.yaml").exists() else None)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        parser.exit(2, f"hcontroller: {exc}\n")
    if config_path is None:
        log.warning("no hcontroller.yaml here, so there are no modules (copy hcontroller.example.yaml)")

    # Dashboard choices are saved next to the config so they survive restarts.
    state_dir = Path(config_path).resolve().parent if config_path else Path.cwd()
    controller = Controller(config, state_path=state_dir / "hcontroller-state.json")
    uvicorn.run(
        create_app(controller),
        host=args.host or config.server.host,
        port=args.port or config.server.port,
        log_level=args.log_level.lower(),
        access_log=False,  # the dashboard polls logs; access lines would bury the modules' output
    )


if __name__ == "__main__":
    main()
