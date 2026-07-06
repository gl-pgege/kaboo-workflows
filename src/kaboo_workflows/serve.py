"""Serve kaboo-workflows agents via AG-UI SSE.

Usage::

    kaboo-serve config.yaml
    kaboo-serve config.yaml --port 9000
    kaboo-serve config.yaml --host 0.0.0.0 --port 8080
    python -m kaboo_workflows.serve config.yaml
"""

from __future__ import annotations

import argparse
import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", "all")

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

    args = parser.parse_args()

    app = create_agui_app(args.config)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
