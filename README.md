<div align="center">

# python-devtools

**Live runtime inspection for any Python app — MCP-powered.**

Connect Claude Code (or any MCP client) to your running Python process.<br>
Query state, eval expressions, inspect objects, read source — all while the app runs.

<br>

```
┌─────────────────┐         TCP/JSON-lines         ┌──────────────────┐
│                  │ ◄──────────────────────────────►│                  │
│   Your App       │       localhost:auto            │   MCP Bridge     │
│   (3 lines)      │                                 │   (stdio ↔ TCP)  │
│                  │                                 │                  │
└─────────────────┘                                 └────────┬─────────┘
                                                             │ MCP stdio
                                                    ┌────────▼─────────┐
                                                    │   Claude Code    │
                                                    │   or any MCP     │
                                                    │   client         │
                                                    └──────────────────┘
```

<br>

[Install](#install) · [Quick Start](#quick-start) · [Wrapper Mode](#wrapper-mode) · [Tools](#tools) · [Threading](#threading-safety) · [Security](#security)

</div>

---

## Install

```bash
# Install once (includes MCP bridge dependency)
pip install python-devtools
```

Or with `uv`:

```bash
uv add python-devtools
```

Or run the MCP bridge through `uv`:

```bash
uv run --project /abs/project/path --with mcp python-devtools
```

## Quick Start

### 1. Embed in your app

```python
import python_devtools as devtools

devtools.register('app', my_app)
devtools.register('db', database)
devtools.start(app_id='my-app')  # localhost:<auto free port>
```

Three lines. Your app now speaks devtools.

### 2. Connect Claude Code

Add to your `.claude/settings.json`:

```json
{
  "mcpServers": {
    "python-devtools": {
      "command": "python-devtools"
    }
  }
}
```

The bridge connects to already-running apps only. It does not launch your program.

If you prefer `uv`-managed execution and want to guarantee the MCP SDK is present, use:

```json
{
  "mcpServers": {
    "python-devtools": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/abs/project/path", "--with", "mcp", "python-devtools", "--app-id", "my-app"]
    }
  }
}
```

### 3. Inspect live state

Claude can now reach into your running app:

```
> run("len(app.users)", app_id="my-app")
→ 42

> inspect("app.config", app_id="my-app")
→ {type: AppConfig, attrs: [{name: debug, type: bool, repr: True}, ...]}

> run("app.users[0].email", app_id="my-app")
→ 'alice@example.com'

> source("type(app.users[0]).validate", app_id="my-app")
→ def validate(self): ...
```

## Discovery (No Fixed Ports)

`devtools.start()` defaults to `port=0`, so each app instance binds an available free port.

- App side: each running instance writes `{app_id, host, port, pid}` to a local registry
- Bridge side: tools resolve `app_id` to the current endpoint from that registry
- Unknown `app_id`: the bridge pings candidates and returns the list of running apps
- Crash/system-crash safety: stale registry records are pruned automatically when liveness checks fail

This removes the need to reserve one static port per app.

---

## Wrapper Mode

Don't want to modify your app's source? Wrap it:

```bash
python-devtools --app-id myapp -- uv run myapp.py
python-devtools --app-id flask-dev -- flask run
python-devtools --app-id worker --port 9230 -- python worker.py
```

This injects a devtools server into the child process via `sitecustomize.py` — **no code changes needed**. The child gets a TCP server on startup, and `__main__` is auto-registered as `main`:

```
> run("dir(main)")
→ ['__builtins__', '__file__', 'app', 'config', 'db', ...]

> run("main.app.config['DEBUG']")
→ True
```

<details>
<summary><b>How it works</b></summary>
<br>

The wrapper prepends a generated `sitecustomize.py` to `PYTHONPATH`. When the child Python interpreter starts, `site.py` imports it, which:

1. **Chains** to any existing `sitecustomize.py` (removes inject dir from path, imports original, restores)
2. **Starts** the devtools TCP server on a free port (or your configured port)
3. **Registers** `__main__` — the module ref is captured early but populated later with the script's globals

The `python_devtools` package is also added to `PYTHONPATH`, so it doesn't need to be installed in the child's environment.

Non-Python children (e.g., `python-devtools -- node app.js`) are harmless — the env vars are set but nothing reads them.

</details>

<br>

Pair with the MCP bridge for Claude Code access:

```bash
# Terminal 1: run your app with devtools injected
python-devtools --app-id myapp -- uv run myapp.py

# Claude Code config: MCP bridge routes by app_id
# .claude/settings.json
{
  "mcpServers": {
    "python-devtools": {
      "command": "python-devtools"
    }
  }
}
```

---

## Tools

<table>
<tr>
<th width="160">Tool</th>
<th>Description</th>
<th width="80">Mutates</th>
</tr>
<tr>
<td><code>running_apps</code></td>
<td>List reachable app IDs discovered from the local registry (stale entries are auto-pruned)</td>
<td align="center">—</td>
</tr>
<tr>
<td><code>run</code></td>
<td>Eval an expression or exec a statement in the app's live namespace</td>
<td align="center">yes</td>
</tr>
<tr>
<td><code>call</code></td>
<td>Call a callable at a dotted path with args/kwargs</td>
<td align="center">yes</td>
</tr>
<tr>
<td><code>set_value</code></td>
<td>Set an attribute or item at a dotted path</td>
<td align="center">yes</td>
</tr>
<tr>
<td><code>inspect</code></td>
<td>Structured inspection — type, repr, public attrs, recursive</td>
<td align="center">—</td>
</tr>
<tr>
<td><code>list_path</code></td>
<td>Shallow enumeration — attrs, keys, or items at a path</td>
<td align="center">—</td>
</tr>
<tr>
<td><code>repr_obj</code></td>
<td>Quick type + repr — fastest tool, minimal overhead</td>
<td align="center">—</td>
</tr>
<tr>
<td><code>source</code></td>
<td>Get source code of a function, class, or method</td>
<td align="center">—</td>
</tr>
<tr>
<td><code>state</code></td>
<td>List all registered namespaces and their types</td>
<td align="center">—</td>
</tr>
<tr>
<td><code>ping</code></td>
<td>Connection health check</td>
<td align="center">—</td>
</tr>
</table>

Every tool accepts an optional <code>app_id</code> argument. If no default app ID is set on the bridge and the supplied app ID is not found, the bridge pings known endpoints, prunes stale records, and returns the running apps list.

## Argparse Integration

For apps that already use argparse:

```python
import argparse
import python_devtools as devtools

parser = argparse.ArgumentParser()
devtools.add_arguments(parser)  # adds --devtools, --devtools-port, --devtools-app-id, --devtools-readonly

args = parser.parse_args()
devtools.from_args(args, app=my_app, db=database)
```

```bash
python myapp.py --devtools --devtools-app-id myapp
python myapp.py --devtools --devtools-app-id myapp --devtools-port 9230
python myapp.py --devtools --devtools-app-id myapp --devtools-readonly
```

## Threading Safety

GUI apps, game loops, and anything with a main-thread constraint need an invoker:

```python
import concurrent.futures
import queue

main_queue = queue.Queue()

def invoke_on_main(fn):
    """Route devtools calls onto the main thread."""
    future = concurrent.futures.Future()
    main_queue.put((fn, future))
    return future.result(timeout=10)

devtools.set_main_thread_invoker(invoke_on_main)
devtools.start()

# In your main loop:
while running:
    while not main_queue.empty():
        fn, future = main_queue.get()
        future.set_result(fn())
    # ... rest of frame
```

Without an invoker, calls run inline on the TCP handler thread (a one-time warning is emitted).

## Readonly Mode

Lock down mutation tools for safer inspection:

```python
devtools.start(readonly=True)
```

```json
{
  "mcpServers": {
    "python-devtools": {
      "command": "python-devtools",
      "args": ["--readonly"]
    }
  }
}
```

In readonly mode, `run`, `call`, and `set_value` are not registered — only inspection tools are available.

## Security

<div align="center">

> **LOCAL_TRUSTED** — loopback only, no auth, eval enabled.

</div>

This is a **development tool**, not a production service.

- Binds to **localhost only** — non-loopback connections are rejected
- **No authentication** — anyone on localhost can connect
- **eval/exec is unrestricted** — full access to your Python process
- The `readonly` flag disables mutation tools but does not add auth

Do not expose to networks. Do not run in production.

## Observable State

For GUI status indicators, the server exposes:

```python
devtools.running          # bool — is the server listening?
devtools.n_clients        # int  — currently connected clients
devtools.n_commands       # int  — total commands processed
devtools.last_command_time  # float — time.time() of last command
```

## Architecture

```
python-devtools/
├── __init__.py      # Module API — register, start, stop
├── _core.py         # DevTools orchestrator — lifecycle, argparse
├── _registry.py     # Local app registry for app-id routing
├── _server.py       # TCP JSON-lines server — accept, dispatch, threading
├── _resolve.py      # Object resolution — inspect, eval, serialize
├── _cli.py          # MCP stdio bridge + wrapper dispatch
└── _wrap.py         # Wrapper mode — sitecustomize.py injection
```

The app runtime server (`__init__`, `_core`, `_server`, `_resolve`) is implemented with stdlib modules.
The MCP bridge (`_cli`) uses the bundled `mcp` dependency from the base package install.

---

<div align="center">
<sub>MIT License</sub>
</div>
