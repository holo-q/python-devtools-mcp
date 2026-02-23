"""
python-devtools: Live runtime inspection for any Python app.

Embed a lightweight inspection server in your app, connect via MCP from
Claude Code or any LLM agent to query state, eval code, and inspect objects
while the app is running.

Quick start:
    import python_devtools as devtools

    devtools.register('app', my_app)
    devtools.register('storage', storage)
    devtools.start()  # Listens on port 9229

Argparse integration:
    devtools.add_arguments(parser)   # Adds --devtools, --devtools-port, --devtools-readonly
    args = parser.parse_args()
    devtools.from_args(args, app=my_app, storage=storage)

Threading safety (GUI apps):
    devtools.set_main_thread_invoker(my_invoke_callback)
    devtools.start()

Claude Code connects via the CLI bridge:
    python-devtools --port 9229
"""

from __future__ import annotations

from collections.abc import Callable

from python_devtools._core import DevTools

# Module-level singleton — most apps just need one
_default = DevTools()


def register(name: str, obj: object) -> None:
    """Register an object under a name in the default devtools instance."""
    _default.register(name, obj)


def set_main_thread_invoker(callback: Callable | None) -> None:
    """Set a callback that routes resolve/eval onto the app's main thread."""
    _default.set_main_thread_invoker(callback)


def set_screenshot_fn(callback: Callable[[], bytes] | None) -> None:
    """Register a callback that captures the app's GUI as PNG bytes."""
    _default.set_screenshot_fn(callback)


def start(*, port: int = 9229, host: str = 'localhost', readonly: bool = False) -> None:
    """Start the devtools inspection server on the default instance."""
    _default.start(port=port, host=host, readonly=readonly)


def add_arguments(parser) -> None:
    """Add --devtools, --devtools-port, --devtools-readonly to an argparse parser."""
    _default.add_arguments(parser)


def from_args(args, **namespaces) -> None:
    """Register namespaces and start if --devtools was passed."""
    _default.from_args(args, **namespaces)


def stop() -> None:
    """Stop the devtools inspection server."""
    _default.stop()


__all__ = [
    'DevTools',
    'add_arguments',
    'from_args',
    'register',
    'set_main_thread_invoker',
    'set_screenshot_fn',
    'start',
    'stop',
]
