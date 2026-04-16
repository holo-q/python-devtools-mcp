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
    screenshot()             — Capture current GUI state as PNG (app-dependent)
    winshot(code)            — Render code in offscreen window, return PNG (app-dependent)
    ping()                   — Liveness check, returns 'pong'
    version()                — Returns server version string
    logs(...)                — Indexed log tail/pagination/follow for debugging

Protocol robustness:
    - Loopback-only: non-loopback peers are rejected immediately
    - Bounded recv buffer: clients exceeding 1MB are disconnected
    - Readonly mode: eval/call/set/winshot methods can be disabled

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
import traceback
from collections import deque
from collections.abc import Callable
from typing import Any

log = logging.getLogger('python-devtools')

VERSION = 'python-devtools 0.2.0'

# Methods that mutate app state — blocked in readonly mode
_MUTATION_METHODS = frozenset({'eval', 'call', 'set', 'winshot'})

# Maximum recv buffer before force-disconnect (1MB)
_MAX_BUF = 1_000_000

# Log history retained for MCP log queries
_MAX_LOG_ENTRIES = 5_000

# Upper bound for blocking log follow calls
_MAX_LOG_WAIT_SECONDS = 30.0


def _parse_level(level: str | None) -> int:
    """Parse a logging level string; defaults to NOTSET for unknown input."""
    if level is None:
        return logging.NOTSET
    if isinstance(level, str):
        parsed = logging.getLevelName(level.upper())
        if isinstance(parsed, int):
            return parsed
    return logging.NOTSET


class _LogBuffer:
    """Thread-safe indexed log buffer with tail/pagination semantics."""

    def __init__(self, max_entries: int = _MAX_LOG_ENTRIES):
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._next_id = 1
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def append(self, record: logging.LogRecord) -> None:
        """Append one log record with a monotonic id."""
        message = record.getMessage()
        if record.exc_info:
            message += '\n' + ''.join(traceback.format_exception(*record.exc_info)).rstrip()
        if record.stack_info:
            message += '\n' + str(record.stack_info)

        with self._cv:
            entry_id = self._next_id
            self._next_id += 1
            self._entries.append(
                {
                    'id': entry_id,
                    'ts': float(record.created),
                    'level': record.levelname,
                    'logger': record.name,
                    'message': message,
                }
            )
            self._cv.notify_all()

    def wait_for_new(self, after_id: int, wait_seconds: float) -> None:
        """Block until there is any entry with id > after_id, or timeout."""
        timeout = max(0.0, min(float(wait_seconds), _MAX_LOG_WAIT_SECONDS))
        if timeout <= 0.0:
            return

        with self._cv:
            deadline = time.monotonic() + timeout
            while self._next_id - 1 <= after_id:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._cv.wait(remaining)

    def query(
        self,
        *,
        after_id: int = 0,
        before_id: int | None = None,
        limit: int = 200,
        level: str | None = None,
        logger_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Query logs with indexed pagination.

        Defaults to tail mode (latest `limit` entries). Use:
          - `after_id` for forward reads/follow
          - `before_id` for older/backward reads
        """
        min_level = _parse_level(level)
        max_items = max(1, min(int(limit), 500))
        logger_filter = (logger_name or '').strip()

        with self._lock:
            entries = list(self._entries)

        filtered = [
            e for e in entries
            if (min_level <= _parse_level(str(e['level'])))
            and (not logger_filter or logger_filter in str(e['logger']))
        ]

        window = filtered
        if before_id is not None:
            window = [e for e in window if int(e['id']) < int(before_id)]
            selected = window[-max_items:]
        elif after_id > 0:
            window = [e for e in window if int(e['id']) > int(after_id)]
            selected = window[:max_items]
        else:
            selected = window[-max_items:]

        if selected:
            first_id = int(selected[0]['id'])
            last_id = int(selected[-1]['id'])
            has_older = bool(window and int(window[0]['id']) < first_id)
            has_newer = bool(window and int(window[-1]['id']) > last_id)
            next_before_id = first_id
            next_after_id = last_id
        else:
            has_older = False
            has_newer = False
            next_before_id = before_id
            next_after_id = max(0, int(after_id))

        return {
            'entries': selected,
            'count': len(selected),
            'first_id': next_before_id if selected else None,
            'last_id': next_after_id if selected else None,
            'has_older': has_older,
            'has_newer': has_newer,
            'next_before_id': next_before_id,
            'next_after_id': next_after_id,
            'tip': (
                'Use logs(before_id=next_before_id) for older context, '
                'or logs(after_id=next_after_id, wait_seconds=5) to follow while reproducing.'
            ),
        }


class _LogCaptureHandler(logging.Handler):
    """Forwards all Python logging records into the devtools log buffer."""

    def __init__(self, sink: _LogBuffer):
        super().__init__(level=logging.NOTSET)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.append(record)
        except Exception:
            # Never let logging failures crash user apps.
            pass


class _Server:
    """TCP JSON-lines server for runtime inspection."""

    def __init__(
        self,
        namespaces: dict[str, object],
        host: str,
        port: int,
        app_id: str,
        *,
        invoke_fn: Callable | None = None,
        readonly: bool = False,
    ):
        self._namespaces = namespaces
        self._host = host
        self._port = port
        self._app_id = app_id
        self._invoke_fn = invoke_fn
        self._readonly = readonly
        self._screenshot_fn: Callable[[], bytes] | None = None
        self._winshot_fn: Callable[[str], bytes] | None = None
        self._sock: socket.socket | None = None
        self._running = False
        self._registry_path: str | None = None
        self._log_buffer = _LogBuffer()
        self._log_handler: _LogCaptureHandler | None = None

        # One-time warning for inline (no invoker) calls
        self._warned_inline = False

        # Observable state for GUI indicators
        self.n_clients: int = 0
        self.n_commands: int = 0
        self.last_command_time: float = 0.0  # time.time() of last command

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        from python_devtools._registry import register_app

        self._running = True
        root_logger = logging.getLogger()
        self._log_handler = _LogCaptureHandler(self._log_buffer)
        root_logger.addHandler(self._log_handler)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self._host, self._port))
        self._port = int(self._sock.getsockname()[1])
        self._sock.listen(4)
        self._sock.settimeout(1.0)  # So shutdown can break the accept loop
        self._registry_path = register_app(
            app_id=self._app_id,
            host=self._host,
            port=self._port,
            readonly=self._readonly,
        )

        thread = threading.Thread(target=self._accept_loop, daemon=True, name='devtools-server')
        thread.start()

    def shutdown(self) -> None:
        from python_devtools._registry import unregister_app

        self._running = False
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        if self._sock:
            self._sock.close()
            self._sock = None
        unregister_app(self._registry_path)
        self._registry_path = None

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
        if method == 'logs':
            after_id = int(params.get('after_id', 0) or 0)
            before_raw = params.get('before_id')
            before_id = int(before_raw) if before_raw is not None else None
            limit = int(params.get('limit', 200) or 200)
            level = params.get('level')
            logger_name = params.get('logger')
            wait_seconds = float(params.get('wait_seconds', 0.0) or 0.0)

            # Follow mode: wait for new logs after the cursor (primarily for user test runs).
            if before_id is None and wait_seconds > 0:
                self._log_buffer.wait_for_new(after_id=after_id, wait_seconds=wait_seconds)

            return self._log_buffer.query(
                after_id=after_id,
                before_id=before_id,
                limit=limit,
                level=level,
                logger_name=logger_name,
            )

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

        # Winshot — renders code in an offscreen window, returns PNG
        if method == 'winshot':
            if self._winshot_fn is None:
                raise RuntimeError(
                    'Winshot not available — app has not registered a winshot callback. '
                    'Call devtools.set_winshot_fn(callback) in the app.'
                )
            import base64
            code = params.get('code', '')
            fn = self._winshot_fn
            png_bytes = self._run_in_app_context(lambda: fn(code))
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
                return self._run_in_app_context(lambda: run_code(
                    params['code'],
                    ns,
                    max_result_chars=int(params.get('max_result_chars', 0) or 0),
                    max_result_lines=int(params.get('max_result_lines', 0) or 0),
                ))
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
    port: int = 0,
    app_id: str = 'app',
    *,
    invoke_fn: Callable | None = None,
    readonly: bool = False,
) -> _Server:
    """Create and start an inspection server. Returns the server instance."""
    srv = _Server(namespaces, host, port, app_id, invoke_fn=invoke_fn, readonly=readonly)
    srv.start()
    return srv
