"""Serve kaboo-workflows agents via AG-UI SSE.

Usage::

    kaboo-serve config.yaml
    kaboo-serve config.yaml --port 9000
    kaboo-serve config.yaml --host 0.0.0.0 --port 8080
    python -m kaboo_workflows.serve config.yaml
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .adapters.agui import create_agui_app


def main() -> None:
    """CLI entry point for serving AG-UI."""
    parser = argparse.ArgumentParser(
        prog="kaboo-serve",
        description="Serve YAML-defined agents via AG-UI SSE",
    )
    parser.add_argument("config", help="Path to YAML config file")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")  # noqa: S104 # nosec B104
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    parser.add_argument("--log-level", default="INFO", help="Log level (default: INFO)")
    parser.add_argument(
        "--endpoint", default="/invocations", help="AG-UI agent endpoint path"
    )
    parser.add_argument(
        "--ping-path", default="/ping", help="Health check path ('' to disable)"
    )
    parser.add_argument(
        "--activity-path", default="/activity-stream", help="Activity SSE stream path"
    )
    parser.add_argument(
        "--cors-origin",
        action="append",
        dest="cors_origins",
        metavar="ORIGIN",
        help="Allowed CORS origin (repeatable). Defaults to '*'.",
    )

    args = parser.parse_args()

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    for name in ("kaboo_workflows",):
        pkg_logger = logging.getLogger(name)
        pkg_logger.setLevel(log_level)
        pkg_logger.addHandler(handler)

    app = create_agui_app(
        args.config,
        endpoint=args.endpoint,
        ping_path=args.ping_path or None,
        activity_path=args.activity_path,
        cors_origins=args.cors_origins,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
