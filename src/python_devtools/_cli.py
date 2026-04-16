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
_RUN_DEFAULT_MAX_RESULT_CHARS = 0
_RUN_DEFAULT_MAX_RESULT_LINES = 0


def _fmt(result):
    """Normalize tool result for MCP transport.

    Return structured objects directly so MCP clients can render them without
    JSON-string escaping noise. Scalars are stringified for consistency.
    """
    if isinstance(result, (dict, list)):
        return result
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
        description=(
            'MCP bridge to already-running Python apps with devtools enabled '
            '(does not launch apps). Prefer logs() for indexed, timeline-first debugging.'
        ),
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

    def _tool_error(exc: Exception) -> str:
        """Return the direct tool error payload without extra wrapper prose."""
        detail = str(exc).strip()
        if not detail:
            return type(exc).__name__
        prefix = f'{type(exc).__name__}:'
        if detail.startswith(prefix):
            return detail
        return f'{type(exc).__name__}: {detail}'

    def _resolve_target(app_id: str | None) -> dict[str, Any] | None:
        target_app_id = app_id or args.app_id
        if not target_app_id:
            return None
        try:
            return router.resolve(target_app_id)
        except _AppResolutionError:
            return None

    def _post_mutation_warning(
        *,
        app_id: str | None,
        target_before: dict[str, Any] | None,
    ) -> str | None:
        target_app_id = app_id or args.app_id
        if not target_app_id:
            return None

        try:
            target_after = router.resolve(target_app_id)
        except _AppResolutionError:
            return (
                'Target app became unreachable immediately after this command. '
                'It may have crashed; call running_apps() and retry.'
            )

        if target_before is None:
            return None

        if (
            target_before.get('pid') != target_after.get('pid')
            or target_before.get('host') != target_after.get('host')
            or int(target_before.get('port', 0)) != int(target_after.get('port', 0))
        ):
            return (
                'Target app restarted immediately after this command '
                f"(pid {target_before.get('pid')}->{target_after.get('pid')}, "
                f"{target_before.get('host')}:{target_before.get('port')}"
                f"->{target_after.get('host')}:{target_after.get('port')})."
            )
        return None

    def _attach_warning(payload: Any, warning: str | None) -> Any:
        if warning is None:
            return payload
        if isinstance(payload, dict):
            out = dict(payload)
            out['devtools_warning'] = warning
            return out
        return {'result': payload, 'devtools_warning': warning}

    def _normalize_logs_payload(payload: Any, *, after_id: int, before_id: int | None) -> dict[str, Any]:
        """Normalize old/new server log response shapes into one indexed schema."""
        if not isinstance(payload, dict):
            return {
                'entries': [],
                'count': 0,
                'first_id': None,
                'last_id': max(0, after_id),
                'has_older': False,
                'has_newer': False,
                'next_before_id': before_id,
                'next_after_id': max(0, after_id),
                'tip': (
                    'Use logs(before_id=next_before_id) for older context, '
                    'or logs(after_id=next_after_id, wait_seconds=5) to follow while reproducing.'
                ),
                'raw': payload,
            }

        entries = payload.get('entries', [])
        if not isinstance(entries, list):
            entries = []

        first_id = int(entries[0].get('id')) if entries and isinstance(entries[0], dict) and 'id' in entries[0] else None
        last_entry_id = int(entries[-1].get('id')) if entries and isinstance(entries[-1], dict) and 'id' in entries[-1] else None

        next_after_id = int(payload.get('next_after_id') or payload.get('last_id') or last_entry_id or max(0, after_id))
        next_before_id = payload.get('next_before_id')
        if next_before_id is None and first_id is not None:
            next_before_id = first_id

        return {
            'entries': entries,
            'count': int(payload.get('count', len(entries))),
            'first_id': payload.get('first_id', first_id),
            'last_id': payload.get('last_id', last_entry_id),
            'has_older': bool(payload.get('has_older', False)),
            'has_newer': bool(payload.get('has_newer', False)),
            'next_before_id': next_before_id,
            'next_after_id': next_after_id,
            'tip': payload.get(
                'tip',
                'Use logs(before_id=next_before_id) for older context, '
                'or logs(after_id=next_after_id, wait_seconds=5) to follow while reproducing.',
            ),
        }

    @mcp.tool()
    def running_apps() -> Any:
        """List reachable running devtools apps discovered via registry (stale entries are pruned)."""
        return _fmt(router.running_apps())

    # Mutation tools: only registered when not readonly
    if not args.readonly:

        @mcp.tool()
        def run(
            code: str,
            app_id: str | None = None,
            max_result_chars: int = _RUN_DEFAULT_MAX_RESULT_CHARS,
            max_result_lines: int = _RUN_DEFAULT_MAX_RESULT_LINES,
        ) -> Any:
            """
            Evaluate or execute Python code in an already-running app.

            Lossless by default: no truncation unless limits are explicitly set.
            Set max_result_chars/max_result_lines > 0 to compact large text output.
            """
            try:
                target_before = _resolve_target(app_id)
                result = _fmt(
                    _request(
                        'eval',
                        app_id=app_id,
                        code=code,
                        max_result_chars=max(0, int(max_result_chars)),
                        max_result_lines=max(0, int(max_result_lines)),
                    )
                )
                return _attach_warning(result, _post_mutation_warning(app_id=app_id, target_before=target_before))
            except Exception as exc:
                return _tool_error(exc)

        @mcp.tool()
        def call(path: str, args: list | None = None, kwargs: dict | None = None, app_id: str | None = None) -> Any:
            """Call a callable at a dotted path in an already-running app."""
            try:
                target_before = _resolve_target(app_id)
                result = _fmt(_request('call', app_id=app_id, path=path, args=args, kwargs=kwargs))
                return _attach_warning(result, _post_mutation_warning(app_id=app_id, target_before=target_before))
            except Exception as exc:
                return _tool_error(exc)

        @mcp.tool()
        def set_value(path: str, value_expr: str, app_id: str | None = None) -> Any:
            """Set an attribute or item at a dotted path in an already-running app."""
            try:
                target_before = _resolve_target(app_id)
                result = _fmt(_request('set', app_id=app_id, path=path, value_expr=value_expr))
                return _attach_warning(result, _post_mutation_warning(app_id=app_id, target_before=target_before))
            except Exception as exc:
                return _tool_error(exc)

        @mcp.tool()
        def winshot(code: str, app_id: str | None = None):
            """Render UI code in an isolated offscreen window in an already-running target app."""
            import base64

            try:
                result = _request('winshot', app_id=app_id, code=code)
                png_bytes = base64.b64decode(result['data'])
                return Image(data=png_bytes, format='png')
            except Exception as exc:
                return _tool_error(exc)

    # Read-only tools: always registered
    @mcp.tool()
    def inspect(path: str, max_depth: int = 2, max_items: int = 50, app_id: str | None = None) -> Any:
        """Inspect an object at a dotted path (pair with logs() to correlate state with events)."""
        try:
            return _fmt(_request('inspect', app_id=app_id, path=path, max_depth=max_depth, max_items=max_items))
        except Exception as exc:
            return _tool_error(exc)

    @mcp.tool()
    def list_path(path: str, max_items: int = 50, app_id: str | None = None) -> Any:
        """List contents at a dotted path in an already-running app — attrs, keys, or items."""
        try:
            return _fmt(_request('list', app_id=app_id, path=path, max_items=max_items))
        except Exception as exc:
            return _tool_error(exc)

    @mcp.tool()
    def repr_obj(path: str, app_id: str | None = None) -> Any:
        """Quick type + repr of an object at a dotted path in an already-running app."""
        try:
            return _fmt(_request('repr', app_id=app_id, path=path))
        except Exception as exc:
            return _tool_error(exc)

    @mcp.tool()
    def source(path: str, app_id: str | None = None) -> Any:
        """Get source code of a function, class, or method from an already-running app."""
        try:
            return _fmt(_request('source', app_id=app_id, path=path))
        except Exception as exc:
            return _tool_error(exc)

    @mcp.tool()
    def state(app_id: str | None = None) -> Any:
        """List all registered namespaces and their types for one already-running app."""
        try:
            return _fmt(_request('state', app_id=app_id))
        except Exception as exc:
            return _tool_error(exc)

    @mcp.tool()
    def logs(
        after_id: int = 0,
        before_id: int | None = None,
        limit: int = 200,
        level: str | None = None,
        logger: str | None = None,
        wait_seconds: float = 0.0,
        app_id: str | None = None,
    ) -> Any:
        """
        Read indexed app logs (tail by default).

        Pattern for debugging:
          - Snapshot recent logs: logs(limit=200)
          - Reproduce user action and follow: logs(after_id=<last>, wait_seconds=5)
          - Page older context: logs(before_id=<first>, limit=200)
        """
        cursor = max(0, int(after_id))
        max_wait = max(0.0, float(wait_seconds))

        # Backward-compatible follow loop:
        # - New servers can honor wait_seconds directly.
        # - Older servers ignore wait_seconds; we poll until timeout.
        try:
            deadline = time.monotonic() + max_wait
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                payload = _request(
                    'logs',
                    app_id=app_id,
                    after_id=cursor,
                    # Compatibility with older app servers that use since_id only.
                    since_id=cursor,
                    before_id=before_id,
                    limit=limit,
                    level=level,
                    logger=logger,
                    wait_seconds=remaining if (before_id is None and max_wait > 0.0) else 0.0,
                )
                normalized = _normalize_logs_payload(payload, after_id=cursor, before_id=before_id)

                if before_id is not None or max_wait <= 0.0:
                    return _fmt(normalized)
                if normalized.get('entries'):
                    return _fmt(normalized)
                if remaining <= 0.0:
                    return _fmt(normalized)

                cursor = int(normalized.get('next_after_id', cursor))
                time.sleep(min(0.25, remaining))
        except Exception as exc:
            return _tool_error(exc)

    @mcp.tool()
    def screenshot(app_id: str | None = None):
        """Capture a screenshot of an already-running target app's GUI."""
        import base64

        try:
            result = _request('screenshot', app_id=app_id)
            png_bytes = base64.b64decode(result['data'])
            return Image(data=png_bytes, format='png')
        except Exception as exc:
            return _tool_error(exc)

    @mcp.tool()
    def ping(app_id: str | None = None) -> Any:
        """Ping one already-running app, or list running apps when app_id is omitted."""
        try:
            if app_id is None and args.app_id is None:
                return _fmt(router.running_apps())
            return _fmt(_request('ping', app_id=app_id))
        except Exception as exc:
            return _tool_error(exc)

    # Run as stdio MCP server (Claude Code connects here)
    mcp.run()


if __name__ == '__main__':
    main()
