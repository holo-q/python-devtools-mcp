"""
TCP inspection server — pure stdlib, zero dependencies.

Speaks JSON-lines over TCP. Each line is a JSON object:
    Request:  {"id": 1, "method": "eval", "params": {"code": "len(app.hobos)"}}
    Response: {"id": 1, "result": {"value": "2", "type": "int"}}
    Error:    {"id": 1, "error": "NameError: name 'foo' is not defined"}

Methods:
    eval(code)               — Evaluate/execute Python code in the app namespace
    inspect(path)            — Structured inspection of object at dotted path
    source(path)             — Source code of function/class at path
    state()                  — Overview of all registered namespaces
    list(path)               — Enumerate attrs/keys/items at path
    repr(path)               — Quick type + repr at path
    call(path, args, kwargs) — Call a callable at path
    set(path, value_expr)    — Set attribute/item at path
    ping()                   — Liveness check, returns 'pong'
    version()                — Returns server version string

Protocol robustness:
    - Loopback-only: non-loopback peers are rejected immediately
    - Bounded recv buffer: clients exceeding 1MB are disconnected
    - Readonly mode: eval/call/set methods can be disabled

Threading safety:
    - invoke_fn callback routes resolve/eval onto the app's main thread
    - Without invoke_fn, calls run inline on the TCP handler thread
      (one-time warning emitted on first inline call)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger('python-devtools')

VERSION = 'python-devtools 0.2.0'

# Methods that mutate app state — blocked in readonly mode
_MUTATION_METHODS = frozenset({'eval', 'call', 'set'})

# Maximum recv buffer before force-disconnect (1MB)
_MAX_BUF = 1_000_000


class _Server:
    """TCP JSON-lines server for runtime inspection."""

    def __init__(
        self,
        namespaces: dict[str, object],
        host: str,
        port: int,
        *,
        invoke_fn: Callable | None = None,
        readonly: bool = False,
    ):
        self._namespaces = namespaces
        self._host = host
        self._port = port
        self._invoke_fn = invoke_fn
        self._readonly = readonly
        self._screenshot_fn: Callable[[], bytes] | None = None
        self._sock: socket.socket | None = None
        self._running = False

        # One-time warning for inline (no invoker) calls
        self._warned_inline = False

        # Observable state for GUI indicators
        self.n_clients: int = 0
        self.n_commands: int = 0
        self.last_command_time: float = 0.0  # time.time() of last command

    def start(self) -> None:
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self._host, self._port))
        self._sock.listen(4)
        self._sock.settimeout(1.0)  # So shutdown can break the accept loop

        thread = threading.Thread(target=self._accept_loop, daemon=True, name='devtools-server')
        thread.start()

    def shutdown(self) -> None:
        self._running = False
        if self._sock:
            self._sock.close()
            self._sock = None

    # ────────────────────────────────────────────────────────────────
    # Threading safety — route calls through app's main thread
    # ────────────────────────────────────────────────────────────────

    def _run_in_app_context(self, fn: Callable) -> Any:
        """
        Execute fn() in the app's main thread context if an invoker is set.
        Falls back to inline execution with a one-time warning.
        """
        if self._invoke_fn is not None:
            return self._invoke_fn(fn)

        # Inline fallback — warn once so devs know threading isn't safe
        if not self._warned_inline:
            self._warned_inline = True
            log.warning(
                'devtools: no main-thread invoker set — running resolve/eval inline on TCP thread. '
                'Call set_main_thread_invoker() for thread-safe access to your app objects.'
            )
        return fn()

    # ────────────────────────────────────────────────────────────────
    # Accept / Handle
    # ────────────────────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client, addr = self._sock.accept()  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                break  # Socket closed

            # Loopback guard — reject non-local peers
            if not _is_loopback(addr[0]):
                log.warning(f'devtools: rejected non-loopback connection from {addr[0]}')
                client.close()
                continue

            log.debug(f'devtools: client connected from {addr}')
            threading.Thread(
                target=self._handle_client,
                args=(client,),
                daemon=True,
                name='devtools-client',
            ).start()

    def _handle_client(self, client: socket.socket) -> None:
        self.n_clients += 1
        buf = b''
        try:
            while self._running:
                data = client.recv(8192)
                if not data:
                    break
                buf += data

                # Bounded buffer — disconnect runaway clients
                if len(buf) > _MAX_BUF:
                    log.error('devtools: client exceeded 1MB recv buffer, disconnecting')
                    break

                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    if not line.strip():
                        continue
                    self.n_commands += 1
                    self.last_command_time = time.time()
                    response = self._dispatch(line)
                    client.sendall(response.encode() + b'\n')
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            self.n_clients -= 1
            client.close()
            log.debug('devtools: client disconnected')

    # ────────────────────────────────────────────────────────────────
    # Dispatch
    # ────────────────────────────────────────────────────────────────

    def _dispatch(self, raw: bytes) -> str:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            return json.dumps({'id': None, 'error': f'Invalid JSON: {e}'})

        req_id = req.get('id')
        method = req.get('method', '')
        params = req.get('params', {})

        try:
            result = self._call(method, params)
            return json.dumps({'id': req_id, 'result': result})
        except Exception as e:
            return json.dumps({'id': req_id, 'error': f'{type(e).__name__}: {e}'})

    def _call(self, method: str, params: dict[str, Any]) -> str | dict:
        # Readonly guard — block mutation methods
        if self._readonly and method in _MUTATION_METHODS:
            raise PermissionError('readonly mode — mutation disabled')

        # Built-in protocol methods (no app context needed)
        if method == 'ping':
            return 'pong'
        if method == 'version':
            return VERSION

        # Screenshot — requires app-registered callback, runs on main thread
        if method == 'screenshot':
            if self._screenshot_fn is None:
                raise RuntimeError(
                    'Screenshot not available — app has not registered a screenshot callback. '
                    'Call devtools.set_screenshot_fn(callback) in the app.'
                )
            import base64
            png_bytes = self._run_in_app_context(self._screenshot_fn)
            return {
                'format': 'png',
                'encoding': 'base64',
                'size': len(png_bytes),
                'data': base64.b64encode(png_bytes).decode('ascii'),
            }

        # Resolve methods — run through app context for thread safety
        from python_devtools._resolve import (
            call_path,
            get_source,
            inspect_object,
            list_path,
            list_state,
            repr_path,
            run_code,
            set_value,
        )

        ns = self._namespaces
        match method:
            case 'eval':
                return self._run_in_app_context(lambda: run_code(params['code'], ns))
            case 'inspect':
                return self._run_in_app_context(lambda: inspect_object(
                    params['path'], ns,
                    max_depth=params.get('max_depth', 2),
                    max_items=params.get('max_items', 50),
                    max_repr_len=params.get('max_repr_len', 200),
                ))
            case 'source':
                return self._run_in_app_context(lambda: get_source(params['path'], ns))
            case 'state':
                return self._run_in_app_context(lambda: list_state(ns))
            case 'list':
                return self._run_in_app_context(lambda: list_path(
                    params['path'], ns,
                    max_items=params.get('max_items', 50),
                    max_repr_len=params.get('max_repr_len', 200),
                ))
            case 'repr':
                return self._run_in_app_context(lambda: repr_path(params['path'], ns))
            case 'call':
                return self._run_in_app_context(lambda: call_path(
                    params['path'], ns,
                    args=params.get('args'),
                    kwargs=params.get('kwargs'),
                ))
            case 'set':
                return self._run_in_app_context(lambda: set_value(
                    params['path'], ns, params['value_expr'],
                ))
            case _:
                raise ValueError(f'Unknown method: {method!r}')


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

def _is_loopback(addr: str) -> bool:
    """Check if an address is loopback (127.0.0.0/8 or ::1)."""
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def start_server(
    namespaces: dict[str, object],
    host: str = 'localhost',
    port: int = 9229,
    *,
    invoke_fn: Callable | None = None,
    readonly: bool = False,
) -> _Server:
    """Create and start an inspection server. Returns the server instance."""
    srv = _Server(namespaces, host, port, invoke_fn=invoke_fn, readonly=readonly)
    srv.start()
    return srv
