"""Starting the dashboard: pick a port, bring up uvicorn, open a browser.

Separate from `server.py` so the app can be built and exercised without a socket
-- the route tests run the ASGI app in-process and never bind anything.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import webbrowser
from pathlib import Path
from typing import Mapping

import click

from qaas.config import AgentSpec
from qaas.store import DEFAULT_ROOT
from qaas.ui import require_extra

DEFAULT_PORT = 7777
#: How many ports past the one asked for to try. A second dashboard on the same
#: machine is an ordinary thing to want, and "address already in use" is a worse
#: answer than "I took 7778".
PORT_ATTEMPTS = 10


class NonLoopbackHost(click.UsageError, ValueError):
    """A bind address the dashboard refuses, with the sentence saying why.

    A `click.UsageError` so that raised from inside `qaas dashboard --host X`
    it prints as `Error: <why>` and exits 2, rather than as a traceback with the
    reason at the bottom of it; a `ValueError` for everything that is not the
    CLI. click is already here as typer's own dependency.
    """


def bind_host(host: str) -> str:
    """`host` as a socket wants it, or `NonLoopbackHost` if it is not loopback.

    The page's protection against another site reading the ledger is a check
    that the `Host` header is a loopback literal (`server.LocalOnly`). That is a
    defence against a *browser*, and only a loopback bind makes it one: bound to
    `0.0.0.0` or a LAN address, any peer on the network can send `Host:
    127.0.0.1` and pass it, and the one write route with it. `--host` used to
    accept anything, so the flag that exposed the ledger also switched off the
    only thing guarding it, silently.
    """
    name = host.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name.lower() == "localhost":
        return name
    try:
        loopback = ipaddress.ip_address(name).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise NonLoopbackHost(
            f"refusing to serve the dashboard on {host!r}: it binds loopback only "
            "(127.0.0.1, ::1 or localhost). Its Host check guards against other "
            "pages in your browser, not against other machines, and a "
            "non-loopback bind would hand the run ledger and the overrides route "
            "to anything that can reach the port. Use SSH port forwarding to "
            "view it remotely."
        )
    return name


def free_port(host: str, port: int, attempts: int = PORT_ATTEMPTS) -> int:
    """The first port from `port` that binds, or raise naming what was tried.

    The address family comes from the host. This probed with `AF_INET` only, so
    `--host ::1` could never bind and `qaas dashboard` died with a traceback on
    an address it was perfectly able to serve.
    """
    name = bind_host(host)
    for candidate in range(port, port + attempts):
        try:
            family, kind, proto, _, address = socket.getaddrinfo(
                name, candidate, type=socket.SOCK_STREAM
            )[0]
        except (socket.gaierror, IndexError) as exc:
            raise OSError(f"cannot resolve {host}: {exc}") from None
        with socket.socket(family, kind, proto) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(address)
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
    config_dirs=None,
    state_root: Path | str | None = None,
    target_root: Path | str | None = None,
):
    """The ASGI app, with the `[ui]` extra checked first so the error is a sentence.

    `config_dirs` is used as given -- for the overrides route and for
    `/api/config` -- so a caller honouring `--config` is honoured everywhere.
    """
    require_extra()
    from qaas.ui.server import Dashboard, build_app

    return build_app(
        Dashboard(
            root,
            specs=specs,
            min_confidence=min_confidence,
            ledger_path=ledger_path,
            cfg=cfg,
            config_dirs=config_dirs,
            state_root=state_root,
            target_root=target_root,
        )
    )


def serve(app, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *, log_level: str = "warning"):
    import uvicorn

    # Checked here as well as in `free_port`: this is the call that binds.
    name = bind_host(host)
    server = uvicorn.Server(uvicorn.Config(app, host=name, port=port, log_level=log_level))
    server.run()


def serve_in_background(app, host: str, port: int) -> threading.Thread:
    """For `qaas run --dashboard`: the run owns the foreground, the UI does not."""
    bind_host(host)  # refused on the caller's thread, where it can be reported
    thread = threading.Thread(target=serve, args=(app, host, port), daemon=True)
    thread.start()
    return thread


def open_browser(url: str, delay: float = 0.6) -> None:
    # On a timer because the browser opens faster than uvicorn binds, and a tab
    # that lands on a refused connection is a worse first impression than one
    # that lands half a second late.
    threading.Timer(delay, lambda: webbrowser.open(url)).start()
