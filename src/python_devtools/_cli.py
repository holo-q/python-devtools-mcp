"""
CLI entry point — two modes:

1. MCP bridge (default):
    python-devtools [--app-id APP_ID]
    Runs an MCP stdio server that routes tool calls to already-running apps by app_id.
    This mode never launches target apps.

2. Wrapper mode (with --):
    python-devtools [--app-id APP_ID] [--port PORT] [--readonly] -- <command>
    Injects devtools into a child Python process via sitecustomize.py.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from typing import Any

from python_devtools._registry import list_registered_apps, unregister_app

# 10 MB — guard against runaway responses filling memory
_MAX_BUF = 10 * 1024 * 1024


def _fmt(result) -> str:
    """Format result for MCP — compact JSON to avoid \\n noise in MCP protocol."""
    if isinstance(result, (dict, list)):
        return json.dumps(result, default=str)
    return str(result)


class _DevToolsClient:
    """
    TCP client connecting to one devtools server endpoint.

    Connection strategy — fail fast, recover transparently:
        - Connect on first tool call, fail immediately if app isn't there
        - On connection error during request: tear down, try once to reconnect
        - On reconnect: flush stale buffer, create fresh socket
        - Cooldown after failure — don't hammer a dead endpoint on every call
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
        Respects cooldown to avoid hammering a dead endpoint on every tool call.
        """
        if self._sock is not None:
            return
        if self._last_fail and (time.time() - self._last_fail) < self._COOLDOWN:
            raise ConnectionRefusedError(
                f'App not reachable at {self._host}:{self._port} (retrying in {self._COOLDOWN}s)'
            )
        try:
            self._connect_once()
        except self._CONN_ERRORS as err:
            self._fail()
            raise ConnectionRefusedError(f'App not reachable at {self._host}:{self._port}') from err

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

        Strategy: try once -> on connection error, tear down + reconnect once.
        Handles: app restarts, idle TCP drops, half-open sockets.
        Fails fast when app is down.
        """
        with self._lock:
            self._connect()
            self._id += 1
            msg = json.dumps({'id': self._id, 'method': method, 'params': params})

            try:
                resp = self._send_and_recv(msg)
            except self._CONN_ERRORS:
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


class _AppResolutionError(RuntimeError):
    """Raised when app_id routing cannot resolve a live app."""


class _AppRouter:
    """Resolve app IDs to live endpoints using the local app registry."""

    def __init__(self, *, timeout: float, host: str, port: int | None):
        self._timeout = timeout
        self._clients: dict[tuple[str, int], _DevToolsClient] = {}
        self._direct_client = _DevToolsClient(host, port, timeout=timeout) if port is not None else None

    def _get_client(self, host: str, port: int) -> _DevToolsClient:
        key = (host, port)
        client = self._clients.get(key)
        if client is None:
            client = _DevToolsClient(host, port, timeout=self._timeout)
            self._clients[key] = client
        return client

    def _is_alive(self, entry: dict[str, Any]) -> bool:
        host = entry['host']
        port = int(entry['port'])
        try:
            self._get_client(host, port).request('ping')
            return True
        except Exception:
            unregister_app(entry.get('registry_path'))
            return False

    def running_apps(self) -> list[dict[str, Any]]:
        entries = sorted(
            list_registered_apps(),
            key=lambda item: (item.get('app_id', ''), item.get('started_at', 0.0)),
            reverse=True,
        )
        seen: set[tuple[str, str, int]] = set()
        running: list[dict[str, Any]] = []
        for entry in entries:
            key = (entry['app_id'], entry['host'], int(entry['port']))
            if key in seen:
                continue
            if not self._is_alive(entry):
                continue
            seen.add(key)
            running.append(
                {
                    'app_id': entry['app_id'],
                    'host': entry['host'],
                    'port': int(entry['port']),
                    'pid': int(entry.get('pid', 0)),
                    'readonly': bool(entry.get('readonly', False)),
                }
            )
        return sorted(running, key=lambda item: (item['app_id'], item['port']))

    def _format_running(self, running: list[dict[str, Any]]) -> str:
        if not running:
            return 'none'
        return '; '.join(
            f"{item['app_id']} ({item['host']}:{item['port']}, pid={item['pid']}, "
            f"mode={'readonly' if item['readonly'] else 'read-write'})"
            for item in running
        )

    def resolve(self, app_id: str) -> dict[str, Any]:
        candidates = [
            entry
            for entry in sorted(list_registered_apps(), key=lambda item: item.get('started_at', 0.0), reverse=True)
            if entry.get('app_id') == app_id
        ]

        for entry in candidates:
            if self._is_alive(entry):
                return entry

        running = self.running_apps()
        raise _AppResolutionError(
            f"Unknown app_id '{app_id}'. Running apps: {self._format_running(running)}"
        )

    def request(self, *, app_id: str | None, method: str, **params):
        if self._direct_client is not None and app_id is None:
            return self._direct_client.request(method, **params)

        if not app_id:
            running = self.running_apps()
            raise _AppResolutionError(
                f'app_id is required. Running apps: {self._format_running(running)}'
            )

        resolved = self.resolve(app_id)
        return self._get_client(resolved['host'], int(resolved['port'])).request(method, **params)


def main():
    # Split argv at '--' to detect wrapper mode
    argv = sys.argv[1:]
    command: list[str] | None = None
    if '--' in argv:
        idx = argv.index('--')
        command = argv[idx + 1:]
        argv = argv[:idx]

    parser = argparse.ArgumentParser(
        prog='python-devtools',
        description='MCP bridge to already-running Python apps with devtools enabled (does not launch apps)',
    )
    parser.add_argument('--app-id', type=str, default=None, help='Default app id for tool calls')
    parser.add_argument(
        '--port',
        type=int,
        default=None,
        help='Direct TCP port (legacy single-app mode). If omitted, route by app_id via registry.',
    )
    parser.add_argument('--host', type=str, default='localhost', help='Direct host for --port mode (default: localhost)')
    parser.add_argument('--readonly', action='store_true', help='Disable mutation tools (run/eval)')
    parser.add_argument('--timeout', type=float, default=5.0, help='Socket timeout in seconds (default: 5)')
    args = parser.parse_args(argv)

    # Wrapper mode: inject devtools into child and exec
    if command is not None:
        from python_devtools._wrap import wrap

        wrap(command, port=args.port or 0, app_id=args.app_id, readonly=args.readonly)
        return

    # MCP bridge mode
    router = _AppRouter(timeout=args.timeout, host=args.host, port=args.port)
    if args.port is not None:
        print(f'python-devtools: bridge in direct mode -> {args.host}:{args.port}', file=sys.stderr)
    else:
        print('python-devtools: bridge in app-id mode (registry-discovered endpoints)', file=sys.stderr)
    print('python-devtools: target app must already be running externally', file=sys.stderr)
    if args.app_id:
        print(f'python-devtools: default app_id={args.app_id}', file=sys.stderr)
    if args.readonly:
        print('python-devtools: readonly mode — mutation tools not registered', file=sys.stderr)
    else:
        print('python-devtools: mutations enabled (eval/exec/set/call)', file=sys.stderr)

    # Import MCP SDK (optional dependency — only needed for the CLI)
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.utilities.types import Image
    except ImportError:
        print(
            'error: MCP SDK not installed. Reinstall with: pip install -U python-devtools',
            file=sys.stderr,
        )
        sys.exit(1)

    mcp = FastMCP('python-devtools')

    def _request(method: str, *, app_id: str | None = None, **params):
        target_app_id = app_id or args.app_id
        return router.request(app_id=target_app_id, method=method, **params)

    @mcp.tool()
    def running_apps() -> str:
        """List reachable running devtools apps discovered via registry (stale entries are pruned)."""
        return _fmt(router.running_apps())

    # Mutation tools: only registered when not readonly
    if not args.readonly:

        @mcp.tool()
        def run(code: str, app_id: str | None = None) -> str:
            """Evaluate or execute Python code in an already-running app's live namespace."""
            return _fmt(_request('eval', app_id=app_id, code=code))

        @mcp.tool()
        def call(path: str, args: list | None = None, kwargs: dict | None = None, app_id: str | None = None) -> str:
            """Call a callable at a dotted path in an already-running app."""
            return _fmt(_request('call', app_id=app_id, path=path, args=args, kwargs=kwargs))

        @mcp.tool()
        def set_value(path: str, value_expr: str, app_id: str | None = None) -> str:
            """Set an attribute or item at a dotted path in an already-running app."""
            return _fmt(_request('set', app_id=app_id, path=path, value_expr=value_expr))

        @mcp.tool()
        def winshot(code: str, app_id: str | None = None):
            """Render UI code in an isolated offscreen window in an already-running target app."""
            import base64

            result = _request('winshot', app_id=app_id, code=code)
            png_bytes = base64.b64decode(result['data'])
            return Image(data=png_bytes, format='png')

    # Read-only tools: always registered
    @mcp.tool()
    def inspect(path: str, max_depth: int = 2, max_items: int = 50, app_id: str | None = None) -> str:
        """Inspect an object at a dotted path in an already-running app."""
        return _fmt(_request('inspect', app_id=app_id, path=path, max_depth=max_depth, max_items=max_items))

    @mcp.tool()
    def list_path(path: str, max_items: int = 50, app_id: str | None = None) -> str:
        """List contents at a dotted path in an already-running app — attrs, keys, or items."""
        return _fmt(_request('list', app_id=app_id, path=path, max_items=max_items))

    @mcp.tool()
    def repr_obj(path: str, app_id: str | None = None) -> str:
        """Quick type + repr of an object at a dotted path in an already-running app."""
        return _fmt(_request('repr', app_id=app_id, path=path))

    @mcp.tool()
    def source(path: str, app_id: str | None = None) -> str:
        """Get source code of a function, class, or method from an already-running app."""
        return _fmt(_request('source', app_id=app_id, path=path))

    @mcp.tool()
    def state(app_id: str | None = None) -> str:
        """List all registered namespaces and their types for one already-running app."""
        return _fmt(_request('state', app_id=app_id))

    @mcp.tool()
    def screenshot(app_id: str | None = None):
        """Capture a screenshot of an already-running target app's GUI."""
        import base64

        result = _request('screenshot', app_id=app_id)
        png_bytes = base64.b64decode(result['data'])
        return Image(data=png_bytes, format='png')

    @mcp.tool()
    def ping(app_id: str | None = None) -> str:
        """Ping one already-running app, or list running apps when app_id is omitted."""
        if app_id is None and args.app_id is None:
            return _fmt(router.running_apps())
        return _fmt(_request('ping', app_id=app_id))

    # Run as stdio MCP server (Claude Code connects here)
    mcp.run()


if __name__ == '__main__':
    main()
