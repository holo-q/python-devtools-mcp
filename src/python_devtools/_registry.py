"""Local app registry used by the MCP bridge for app-id routing."""

from __future__ import annotations

import json
import os
import time
from glob import glob
from typing import Any

_CACHE_DIR = os.path.join(
    os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')),
    'python-devtools',
)
_REGISTRY_DIR = os.path.join(_CACHE_DIR, 'registry')


def register_app(
    *,
    app_id: str,
    host: str,
    port: int,
    readonly: bool,
    pid: int | None = None,
) -> str:
    """Write one registry entry and return the file path."""
    os.makedirs(_REGISTRY_DIR, exist_ok=True)
    now = time.time()
    process_id = os.getpid() if pid is None else pid
    entry = {
        'app_id': str(app_id),
        'host': str(host),
        'port': int(port),
        'readonly': bool(readonly),
        'pid': int(process_id),
        'started_at': now,
        'instance_id': f'{process_id}-{int(now * 1000)}-{int(port)}',
    }
    path = os.path.join(_REGISTRY_DIR, f"{entry['instance_id']}.json")
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(entry, f, separators=(',', ':'))
    os.replace(tmp, path)
    return path


def unregister_app(path: str | None) -> None:
    """Remove one registry entry path if present."""
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def list_registered_apps() -> list[dict[str, Any]]:
    """Return all syntactically valid registry entries."""
    entries: list[dict[str, Any]] = []
    if not os.path.isdir(_REGISTRY_DIR):
        return entries

    for path in glob(os.path.join(_REGISTRY_DIR, '*.json')):
        try:
            with open(path, encoding='utf-8') as f:
                raw = json.load(f)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue

        app_id = raw.get('app_id')
        host = raw.get('host')
        port = raw.get('port')
        if not isinstance(app_id, str) or not app_id:
            continue
        if not isinstance(host, str) or not host:
            continue
        if not isinstance(port, int):
            continue

        entries.append(
            {
                'app_id': app_id,
                'host': host,
                'port': port,
                'readonly': bool(raw.get('readonly', False)),
                'pid': int(raw.get('pid', 0)),
                'started_at': float(raw.get('started_at', 0.0)),
                'instance_id': str(raw.get('instance_id', os.path.basename(path))),
                'registry_path': path,
            }
        )
    return entries
