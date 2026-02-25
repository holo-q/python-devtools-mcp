"""DevTools: main orchestrator — register objects, start inspection server."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from python_devtools._server import _Server

log = logging.getLogger('python-devtools')


class DevTools:
    """
    Runtime inspection server for Python apps.

    Register named objects, start the TCP server, and LLM agents can
    connect via the MCP CLI bridge to query live app state.

    Threading safety:
        By default, resolve/eval calls run inline on the TCP handler thread.
        For apps with a main-thread constraint (GUI frameworks, game loops),
        call set_main_thread_invoker() with a callback that schedules work
        back onto the main thread and returns the result.

    LOCAL_TRUSTED mode:
        The server binds to loopback only and rejects non-loopback peers.
        eval/exec is intentionally unrestricted — this is a dev tool, not
        a production service. The warning banner on start makes this explicit.
    """

    def __init__(self):
        self._namespaces: dict[str, object] = {}
        self._server: _Server | None = None
        self._invoke_fn: Callable | None = None
        self._screenshot_fn: Callable[[], bytes] | None = None
        self._winshot_fn: Callable[[str], bytes] | None = None

    # ────────────────────────────────────────────────────────────────────
    # Registration
    # ────────────────────────────────────────────────────────────────────

    def register(self, name: str, obj: object) -> None:
        """Register an object under a name for inspection."""
        self._namespaces[name] = obj
        log.debug(f'devtools: registered {name!r} ({type(obj).__name__})')

    def unregister(self, name: str) -> None:
        """Remove a registered object."""
        self._namespaces.pop(name, None)

    # ────────────────────────────────────────────────────────────────────
    # Threading safety
    # ────────────────────────────────────────────────────────────────────

    def set_main_thread_invoker(self, callback: Callable | None) -> None:
        """
        Set a callback that runs resolve/eval on the app's main thread.

        The callback signature: callback(fn) -> result
        It receives a zero-arg callable, must execute it on the main thread,
        and return the result. If None, calls run inline on the TCP thread.

        Example (imgui app with frame-synced queue):
            def invoke_on_main(fn):
                future = concurrent.futures.Future()
                main_queue.put((fn, future))
                return future.result(timeout=10)
            devtools.set_main_thread_invoker(invoke_on_main)
        """
        self._invoke_fn = callback
        # Propagate to live server if already running
        if self._server is not None:
            self._server._invoke_fn = callback

    def set_screenshot_fn(self, callback: Callable[[], bytes] | None) -> None:
        """
        Register a callback that captures the app's current visual state as PNG bytes.

        The callback must return PNG-encoded bytes. It will be invoked on the main
        thread (via invoke_fn) so it has access to the framebuffer/GL context.

        Without this, the screenshot MCP tool returns an error explaining the
        capability isn't available.

        Example (OpenGL app):
            def capture():
                w, h = glfw.get_framebuffer_size(window)
                pixels = gl.glReadPixels(0, 0, w, h, gl.GL_RGB, gl.GL_UNSIGNED_BYTE)
                # ... flip, encode PNG, return bytes
            devtools.set_screenshot_fn(capture)
        """
        self._screenshot_fn = callback
        if self._server is not None:
            self._server._screenshot_fn = callback

    def set_winshot_fn(self, callback: Callable[[str], bytes] | None) -> None:
        """
        Register a callback that renders code in an offscreen window and returns PNG bytes.

        The callback signature: (code: str) -> bytes
        It receives a code string, sets up an offscreen rendering context, executes the
        code within it (e.g., imgui widget calls), captures the result, and returns
        PNG-encoded bytes.

        Without this, the winshot MCP tool returns an error explaining the
        capability isn't available.

        Example (imgui app):
            def winshot(code: str) -> bytes:
                # Set up offscreen GL context + imgui frame
                # exec(code) within the frame
                # Read framebuffer, encode as PNG, return bytes
                ...
            devtools.set_winshot_fn(winshot)
        """
        self._winshot_fn = callback
        if self._server is not None:
            self._server._winshot_fn = callback

    # ────────────────────────────────────────────────────────────────────
    # Server lifecycle
    # ────────────────────────────────────────────────────────────────────

    def start(self, *, port: int = 9229, host: str = 'localhost', readonly: bool = False) -> None:
        """Start the inspection server in a background thread."""
        if self._server is not None:
            log.warning('devtools: server already running')
            return

        from python_devtools._server import start_server
        self._server = start_server(
            self._namespaces,
            host=host,
            port=port,
            invoke_fn=self._invoke_fn,
            readonly=readonly,
        )
        # Propagate callbacks if already set before start()
        if self._screenshot_fn is not None:
            self._server._screenshot_fn = self._screenshot_fn
        if self._winshot_fn is not None:
            self._server._winshot_fn = self._winshot_fn

        # LOCAL_TRUSTED banner — make the security posture explicit
        log.warning('python-devtools: LOCAL_TRUSTED mode — eval/exec enabled, loopback-only, no auth')
        if readonly:
            log.warning('python-devtools: readonly mode — mutation tools disabled')
        log.info(f'devtools: listening on {host}:{port}')

    def stop(self) -> None:
        """Stop the inspection server."""
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    # ────────────────────────────────────────────────────────────────────
    # Observable state (for GUI indicators)
    # ────────────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def readonly(self) -> bool:
        return self._server._readonly if self._server else False

    @property
    def n_clients(self) -> int:
        return self._server.n_clients if self._server else 0

    @property
    def n_commands(self) -> int:
        return self._server.n_commands if self._server else 0

    @property
    def last_command_time(self) -> float:
        return self._server.last_command_time if self._server else 0.0

    # ────────────────────────────────────────────────────────────────────
    # Argparse integration
    # ────────────────────────────────────────────────────────────────────

    def add_arguments(self, parser) -> None:
        """Add --devtools, --devtools-port, --devtools-readonly to an argparse parser."""
        group = parser.add_argument_group('DevTools')
        group.add_argument('--devtools', action='store_true', help='Enable runtime devtools server')
        group.add_argument('--devtools-port', type=int, default=9229, help='DevTools port (default: 9229)')
        group.add_argument('--devtools-readonly', action='store_true', help='Disable eval/call/set (read-only mode)')

    def from_args(self, args, **namespaces) -> None:
        """Register namespaces and conditionally start from parsed args."""
        for name, obj in namespaces.items():
            self.register(name, obj)

        if getattr(args, 'devtools', False):
            port = getattr(args, 'devtools_port', 9229)
            readonly = getattr(args, 'devtools_readonly', False)
            self.start(port=port, readonly=readonly)
