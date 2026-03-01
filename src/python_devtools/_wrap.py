"""
Wrapper mode — inject devtools into a child Python process via sitecustomize.

Usage:
    python-devtools [--port PORT] [--app-id APP_ID] [--readonly] -- <command>

Mechanism:
    Prepends a generated sitecustomize.py to PYTHONPATH. When the child
    Python interpreter starts, site.py imports sitecustomize which:
    1. Chains to any existing sitecustomize.py (removes inject dir, imports, restores)
    2. Starts the devtools TCP server on the configured port
    3. Registers __main__ for inspection (module ref — populated later with script globals)

    The python_devtools package itself is also made importable via PYTHONPATH,
    so it doesn't need to be installed in the child's environment.

Non-Python children:
    If the wrapped command isn't Python, PYTHONPATH and env vars are harmless noise.
"""

from __future__ import annotations

import os
import sys
import textwrap

# Fixed inject dir under XDG cache — no temp files to clean up
_CACHE_DIR = os.path.join(
    os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')),
    'python-devtools',
)

# sitecustomize.py template — injected into the child's PYTHONPATH.
# Prefixed underscores on all names to minimize namespace pollution.
_SITECUSTOMIZE = textwrap.dedent("""\
    # Auto-injected by `python-devtools -- <cmd>`.
    # Starts a devtools TCP server in the child process.
    import sys as _sys, os as _os

    # ── Chain to real sitecustomize ──
    # Remove injection dir so import finds the original (if any)
    _inject = _os.environ.get('_DEVTOOLS_INJECT_DIR', '')
    if _inject in _sys.path:
        _sys.path.remove(_inject)
    try:
        import sitecustomize  # noqa: F401
    except ImportError:
        pass

    # ── Start devtools server ──
    try:
        import python_devtools as _devtools
        _devtools.start(
            port=int(_os.environ.get('_DEVTOOLS_PORT', '0')),
            app_id=_os.environ.get('_DEVTOOLS_APP_ID'),
            readonly='_DEVTOOLS_READONLY' in _os.environ,
        )
        # __main__ is the module that will hold the user's script globals.
        # We register the object ref now — it gets populated later by the interpreter.
        _main = _sys.modules.get('__main__')
        if _main is not None:
            _devtools.register('main', _main)
        import atexit as _atexit
        _atexit.register(_devtools.stop)
    except Exception as _e:
        print(f'python-devtools: injection failed: {_e}', file=_sys.stderr)
""")


def _default_app_id(command: list[str]) -> str:
    entry = os.path.basename(command[0]) if command else 'python'
    stem, _ = os.path.splitext(entry)
    base = stem or 'python'
    return f'{base}-{os.getpid()}'


def wrap(
    command: list[str],
    *,
    port: int = 0,
    app_id: str | None = None,
    readonly: bool = False,
) -> None:
    """Inject devtools into the child process and exec it. Does not return."""
    if not command:
        print('error: no command specified after --', file=sys.stderr)
        sys.exit(1)

    # ── Write sitecustomize.py to cache dir ──
    inject_dir = os.path.join(_CACHE_DIR, '_inject')
    os.makedirs(inject_dir, exist_ok=True)
    with open(os.path.join(inject_dir, 'sitecustomize.py'), 'w') as f:
        f.write(_SITECUSTOMIZE)

    # ── Resolve python_devtools package parent so it's importable in the child ──
    import python_devtools
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(python_devtools.__file__)))

    # ── Build PYTHONPATH: inject dir (sitecustomize) + package parent ──
    existing = os.environ.get('PYTHONPATH', '')
    parts = [inject_dir, pkg_parent]
    if existing:
        parts.append(existing)

    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join(parts)
    env['_DEVTOOLS_PORT'] = str(port)
    env['_DEVTOOLS_APP_ID'] = app_id or _default_app_id(command)
    env['_DEVTOOLS_INJECT_DIR'] = inject_dir
    if readonly:
        env['_DEVTOOLS_READONLY'] = '1'

    mode = 'readonly' if readonly else 'read-write'
    endpoint = str(port) if port else 'auto'
    print(
        f'python-devtools: wrapping `{" ".join(command)}` — app_id {env["_DEVTOOLS_APP_ID"]}, port {endpoint}, {mode}',
        file=sys.stderr,
    )

    # Replace this process with the child — signals, stdio, exit code all pass through
    os.execvpe(command[0], command, env)
