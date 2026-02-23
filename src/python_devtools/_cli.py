"""
CLI entry point — two modes:

1. MCP bridge (default):
    python-devtools --port 9229
    Runs an MCP stdio server that bridges to the app's devtools TCP port.

2. Wrapper mode (with --):
    python-devtools [--port PORT] [--readonly] -- <command>
    Injects devtools into a child Python process via sitecustomize.py.
    The child gets a devtools TCP server automatically — no code changes needed.

Claude Code configuration:
    {
        "mcpServers": {
            "my-app": {
                "command": "python-devtools",
                "args": ["--port", "9229"]
            }
        }
    }

Readonly mode (no eval/exec — only inspect/source/state/ping):
    {
        "mcpServers": {
            "my-app": {
                "command": "python-devtools",
                "args": ["--port", "9229", "--readonly"]
            }
        }
    }
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time

# 10 MB — guard against runaway responses filling memory
_MAX_BUF = 10 * 1024 * 1024


def _fmt(result) -> str:
    """Format result for MCP — compact JSON to avoid \\n noise in MCP protocol."""
    if isinstance(result, dict):
        return json.dumps(result, default=str)
    return str(result)


class _DevToolsClient:
    """
    TCP client connecting to the app's devtools server.

    Connection strategy — fail fast, recover transparently:
        - Connect on first tool call, fail immediately if app isn't there
        - On connection error during request: tear down, try once to reconnect
        - On reconnect: flush stale buffer, create fresh socket
        - Cooldown after failure — don't hammer a dead port on every tool call
    """

    # Errors that indicate a dead/broken connection worth retrying
    _CONN_ERRORS = (ConnectionError, ConnectionResetError, BrokenPipeError, TimeoutError, OSError)

    # After a connection failure, don't retry for this many seconds.
    # Prevents every tool call from blocking when the app is down.
    _COOLDOWN = 3.0

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._id = 0
        self._buf = b''
        self._last_fail: float = 0.0  # time.time() of last connection failure
        self._lock = threading.Lock()  # Serialize requests — socket I/O isn't thread-safe

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def _connect_once(self) -> None:
        """Open a fresh TCP connection. Raises on failure."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        sock.connect((self._host, self._port))
        self._sock = sock
        self._buf = b''  # Flush stale buffer from previous connection
        self._last_fail = 0.0  # Clear cooldown on success

    def _disconnect(self) -> None:
        """Tear down current connection, if any."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._buf = b''

    def _fail(self) -> None:
        """Record a connection failure for cooldown tracking."""
        self._disconnect()
        self._last_fail = time.time()

    def _connect(self) -> None:
        """
        Connect if not connected. Fail fast — no retry loop.

        Raises ConnectionRefusedError immediately if the app isn't listening.
        Respects cooldown to avoid hammering a dead port on every tool call.
        """
        if self._sock is not None:
            return
        # Cooldown — if we just failed, don't retry yet
        if self._last_fail and (time.time() - self._last_fail) < self._COOLDOWN:
            raise ConnectionRefusedError(
                f'App not reachable at {self._host}:{self._port} (retrying in {self._COOLDOWN}s)'
            )
        try:
            self._connect_once()
        except self._CONN_ERRORS:
            self._fail()
            raise ConnectionRefusedError(f'App not reachable at {self._host}:{self._port}')

    def _send_and_recv(self, msg: str) -> dict:
        """Send a JSON-lines message and read one response line. Raises on I/O failure."""
        self._sock.sendall(msg.encode() + b'\n')  # type: ignore[union-attr]

        while b'\n' not in self._buf:
            data = self._sock.recv(65536)  # type: ignore[union-attr]
            if not data:
                raise ConnectionError('Server closed connection')
            self._buf += data
            if len(self._buf) > _MAX_BUF:
                raise RuntimeError('response too large')

        line, self._buf = self._buf.split(b'\n', 1)
        return json.loads(line)

    def request(self, method: str, **params):
        """
        Send a request, return the result. Reconnects transparently on failure.

        Strategy: try once → on connection error, tear down + reconnect once.
        Handles: app restarts, idle TCP drops, half-open sockets.
        Fails fast when app is down — no 30-second retry loops.
        Thread-safe — serialized via lock (MCP may call tools concurrently).
        """
        with self._lock:
            self._connect()
            self._id += 1
            msg = json.dumps({'id': self._id, 'method': method, 'params': params})

            try:
                resp = self._send_and_recv(msg)
            except self._CONN_ERRORS:
                # Connection died — tear down, reconnect once, retry same message
                print('python-devtools: connection lost, reconnecting...', file=sys.stderr)
                self._disconnect()
                try:
                    self._connect_once()
                    resp = self._send_and_recv(msg)
                except self._CONN_ERRORS as e:
                    self._fail()
                    raise ConnectionError(f'Reconnect to {self._host}:{self._port} failed: {e}') from e

            if 'error' in resp:
                raise RuntimeError(resp['error'])
            return resp['result']


def main():
    # ── Split argv at '--' to detect wrapper mode ──
    argv = sys.argv[1:]
    command: list[str] | None = None
    if '--' in argv:
        idx = argv.index('--')
        command = argv[idx + 1:]
        argv = argv[:idx]

    parser = argparse.ArgumentParser(
        prog='python-devtools',
        description='MCP bridge to a running Python app with devtools enabled',
    )
    parser.add_argument('--port', type=int, default=9229, help='DevTools port (default: 9229)')
    parser.add_argument('--host', type=str, default='localhost', help='DevTools host (default: localhost)')
    parser.add_argument('--readonly', action='store_true', help='Disable mutation tools (run/eval)')
    parser.add_argument('--timeout', type=float, default=5.0, help='Socket timeout in seconds (default: 5)')
    args = parser.parse_args(argv)

    # ── Wrapper mode: inject devtools into child and exec ──
    if command is not None:
        from python_devtools._wrap import wrap
        wrap(command, port=args.port, readonly=args.readonly)
        return  # execvpe never returns — this is just for clarity

    # ── MCP bridge mode ──
    client = _DevToolsClient(args.host, args.port, timeout=args.timeout)
    print(f'python-devtools: bridge ready, will connect to {args.host}:{args.port} on first tool call', file=sys.stderr)
    if args.readonly:
        print('python-devtools: readonly mode — mutation tools not registered', file=sys.stderr)
    else:
        print('python-devtools: mutations enabled (eval/exec/set/call)', file=sys.stderr)

    # Import MCP SDK (optional dependency — only needed for the CLI)
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print('error: MCP SDK not installed. Install with: pip install python-devtools[cli]', file=sys.stderr)
        sys.exit(1)

    mcp = FastMCP('python-devtools')

    # -- Mutation tools: only registered when not readonly --
    if not args.readonly:
        @mcp.tool()
        def run(code: str) -> str:
            """Evaluate a Python expression or execute a statement in the app's live namespace.

            The app's registered namespaces are available as local variables.
            Expressions return their repr. Statements (assignments, calls) return 'OK'.

            Examples:
                run("app.hobos")
                run("len(app.hobos)")
                run("[h.session.name for h in app.hobos]")
                run("storage.application.window")
                run("app.hobos[0].renderer.is_dev = True")
            """
            return _fmt(client.request('eval', code=code))

        @mcp.tool()
        def call(path: str, args: list | None = None, kwargs: dict | None = None) -> str:
            """Call a callable at a dotted path with optional arguments.

            Examples:
                call("app.hobos[0].session.get_frame_range", args=["1-10"])
                call("storage.application.write")
            """
            return _fmt(client.request('call', path=path, args=args, kwargs=kwargs))

        @mcp.tool()
        def set_value(path: str, value_expr: str) -> str:
            """Set an attribute or item at a dotted path.

            value_expr is evaluated as Python (e.g., "True", "42", "'hello'").

            Examples:
                set_value("app.hobos[0].renderer.is_dev", "True")
                set_value("storage.application.window.width", "2500")
            """
            return _fmt(client.request('set', path=path, value_expr=value_expr))

    # -- Read-only tools: always registered --

    @mcp.tool()
    def inspect(path: str, max_depth: int = 2, max_items: int = 50) -> str:
        """Inspect an object at a dotted path — shows type, repr, and public attributes.

        Use this for structured exploration of objects. For arbitrary code, use run().
        Accepts max_depth (default 2) and max_items (default 50) to control output size.

        Examples:
            inspect("app")
            inspect("storage.application")
            inspect("app.hobos[0].session", max_depth=3)
        """
        return _fmt(client.request('inspect', path=path, max_depth=max_depth, max_items=max_items))

    @mcp.tool()
    def list_path(path: str, max_items: int = 50) -> str:
        """List contents at a dotted path — attrs, keys, or items.

        Shallow enumeration (no deep recursion). Use this to explore
        what's inside an object before diving deeper with inspect().

        Examples:
            list_path("app")
            list_path("storage.application.__dict__")
            list_path("app.hobos")
        """
        return _fmt(client.request('list', path=path, max_items=max_items))

    @mcp.tool()
    def repr_obj(path: str) -> str:
        """Quick type + repr of an object at a dotted path.

        The fastest inspection tool — minimal overhead, returns just type and repr.

        Examples:
            repr_obj("app.hobos[0].session.name")
            repr_obj("storage.application.window")
        """
        return _fmt(client.request('repr', path=path))

    @mcp.tool()
    def source(path: str) -> str:
        """Get source code of a function, class, or method.

        Examples:
            source("app.gui")
            source("type(app.hobos[0])")
        """
        return _fmt(client.request('source', path=path))

    @mcp.tool()
    def state() -> str:
        """List all registered namespaces and their types.

        This is the starting point — shows what's available for inspection.
        """
        return _fmt(client.request('state'))

    @mcp.tool()
    def screenshot() -> str:
        """Capture a screenshot of the running application's GUI.

        Returns the file path to a PNG image. Use the Read tool to view it.
        Requires the app to have registered a screenshot callback via
        devtools.set_screenshot_fn().
        """
        import base64
        import os
        import tempfile
        result = client.request('screenshot')
        png_bytes = base64.b64decode(result['data'])
        # Save to temp file — Claude Code's Read tool can view images natively
        fd, path = tempfile.mkstemp(suffix='.png', prefix='devtools-screenshot-')
        os.write(fd, png_bytes)
        os.close(fd)
        return f'{{"path": "{path}", "size": {len(png_bytes)}, "format": "png"}}'

    @mcp.tool()
    def ping() -> str:
        """Check connection health to the devtools server."""
        return _fmt(client.request('ping'))

    # Run as stdio MCP server (Claude Code connects here)
    mcp.run()


if __name__ == '__main__':
    main()
