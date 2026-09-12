"""Starting the dashboard: pick a port, bring up uvicorn, open a browser.

Separate from `server.py` so the app can be built and exercised without a socket
-- the route tests run the ASGI app in-process and never bind anything.
"""

from __future__ import annotations

import socket
import threading
import webbrowser
from pathlib import Path
from typing import Mapping

from qaas.config import AgentSpec
from qaas.store import DEFAULT_ROOT
from qaas.ui import require_extra

DEFAULT_PORT = 7777
#: How many ports past the one asked for to try. A second dashboard on the same
#: machine is an ordinary thing to want, and "address already in use" is a worse
#: answer than "I took 7778".
PORT_ATTEMPTS = 10


def free_port(host: str, port: int, attempts: int = PORT_ATTEMPTS) -> int:
    """The first port from `port` that binds, or raise naming what was tried."""
    for candidate in range(port, port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise OSError(f"no free port in {port}..{port + attempts - 1} on {host}")


def build(
    root: Path | str = DEFAULT_ROOT,
    *,
    specs: Mapping[str, AgentSpec] | None = None,
    min_confidence: float = 0.6,
    ledger_path: Path | None = None,
    cfg=None,
):
    """The ASGI app, with the `[ui]` extra checked first so the error is a sentence."""
    require_extra()
    from qaas.ui.server import Dashboard, build_app

    return build_app(
        Dashboard(
            root,
            specs=specs,
            min_confidence=min_confidence,
            ledger_path=ledger_path,
            cfg=cfg,
        )
    )


def serve(app, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *, log_level: str = "warning"):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=log_level))
    server.run()


def serve_in_background(app, host: str, port: int) -> threading.Thread:
    """For `qaas run --dashboard`: the run owns the foreground, the UI does not."""
    thread = threading.Thread(target=serve, args=(app, host, port), daemon=True)
    thread.start()
    return thread


def open_browser(url: str, delay: float = 0.6) -> None:
    # On a timer because the browser opens faster than uvicorn binds, and a tab
    # that lands on a refused connection is a worse first impression than one
    # that lands half a second late.
    threading.Timer(delay, lambda: webbrowser.open(url)).start()
