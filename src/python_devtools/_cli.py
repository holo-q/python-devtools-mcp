"""
CLI entry point — MCP stdio server that bridges to the app's devtools TCP port.

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

Or run directly: python-devtools --port 9229
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time

# 10 MB — guard against runaway responses filling memory
_MAX_BUF = 10 * 1024 * 1024


def _fmt(result) -> str:
    """Format result for MCP — compact JSON to avoid \\n noise in MCP protocol."""
    if isinstance(result, dict):
        return json.dumps(result, default=str)
    return str(result)


class _DevToolsClient:
    """TCP client connecting to the app's devtools server."""

    def __init__(self, host: str, port: int, timeout: float = 30.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._id = 0
        self._buf = b''

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self._host, self._port))
        self._sock.settimeout(self._timeout)

    def ensure_connected(self) -> None:
        """Connect if not already connected. Retries with backoff."""
        if self._sock is not None:
            return
        for attempt in range(30):  # ~30s total
            try:
                self.connect()
                return
            except (ConnectionRefusedError, OSError):
                if attempt < 29:
                    time.sleep(1)
        raise ConnectionRefusedError(f'Cannot connect to {self._host}:{self._port} after 30 attempts')

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def request(self, method: str, **params):
        """Send a request, return the result. Lazy-connects on first call."""
        self.ensure_connected()
        self._id += 1
        msg = json.dumps({'id': self._id, 'method': method, 'params': params})
        self._sock.sendall(msg.encode() + b'\n')

        # Read response line
        while b'\n' not in self._buf:
            data = self._sock.recv(65536)
            if not data:
                raise ConnectionError('Server closed connection')
            self._buf += data
            if len(self._buf) > _MAX_BUF:
                raise RuntimeError('response too large')

        line, self._buf = self._buf.split(b'\n', 1)
        resp = json.loads(line)
        if 'error' in resp:
            raise RuntimeError(resp['error'])
        return resp['result']


def main():
    parser = argparse.ArgumentParser(
        prog='python-devtools',
        description='MCP bridge to a running Python app with devtools enabled',
    )
    parser.add_argument('--port', type=int, default=9229, help='DevTools port (default: 9229)')
    parser.add_argument('--host', type=str, default='localhost', help='DevTools host (default: localhost)')
    parser.add_argument('--readonly', action='store_true', help='Disable mutation tools (run/eval)')
    parser.add_argument('--timeout', type=float, default=30.0, help='Socket timeout in seconds (default: 30)')
    args = parser.parse_args()

    # Lazy connect — app may not be running yet, bridge waits on first tool call
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
    def ping() -> str:
        """Check connection health to the devtools server."""
        return _fmt(client.request('ping'))

    # Run as stdio MCP server (Claude Code connects here)
    mcp.run()


if __name__ == '__main__':
    main()
