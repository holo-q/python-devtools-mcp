# python-devtools — agent usage

This repo ships an MCP server (`python-devtools`) that lets you reach into an already-running Python process to eval expressions, inspect objects, read source, follow logs, and capture screenshots.

The bridge **never launches target apps**. The user's app must already be running with `devtools.start()` (embedded) or `python-devtools -- <cmd>` (wrapper mode).

## Discovery — always start here

```
running_apps()             # list reachable apps, returns [{app_id, host, port, pid, readonly}, ...]
```

If `app_id` is unknown or stale, the bridge will return the running list automatically. Pick the right `app_id` and pass it to subsequent tool calls.

## Trace-driven debugging (preferred)

Logs are indexed and paginated — use them as the timeline of truth before brute-force state probing:

```
logs(limit=200, app_id="my-app")                              # tail snapshot
logs(after_id=<last_id>, wait_seconds=5, app_id="my-app")     # follow while reproducing
logs(before_id=<first_id>, limit=200, app_id="my-app")        # page older context
```

Returns `entries` with stable `id`s plus `next_before_id` / `next_after_id` cursors. Filter by `level=` or `logger=`.

## Inspection (read-only, cheap)

```
state(app_id=...)                          # registered namespaces overview
repr_obj("app.config", app_id=...)         # fastest type+repr probe
list_path("app.users", app_id=...)         # shallow attrs/keys/items
inspect("app.config", max_depth=2, ...)    # recursive structured dump
source("type(app).validate", app_id=...)   # source code of a function/class/method
```

## Mutation (eval/exec — full process access)

```
run("len(app.users)", app_id=...)
run("app.users[0].verify()", app_id=...)
call("app.reload", args=[], kwargs={}, app_id=...)
set_value("app.debug", "True", app_id=...)
```

`run` accepts the same Jupyter-style semantics: multi-statement code where the last expression's value is returned. Set `max_result_chars` / `max_result_lines > 0` to compact huge outputs (returns head/tail preview + `top_patterns` summary).

If a mutation tool's response carries `devtools_warning`, the target app's pid/port shifted right after your call — likely a crash. Re-run `running_apps()` before continuing.

## Visual capture (when the app registered callbacks)

```
screenshot(app_id=...)         # captures the live app's full GUI as PNG
winshot("imgui.text('hi')", app_id=...)   # renders snippet in isolated offscreen window
```

`winshot` is the focused complement to `screenshot`: where screenshot captures the *whole live app*, winshot captures *only the code you pass* — useful for verifying a single panel/widget without touching app state. Both error cleanly if the app didn't register the corresponding callback.

## Safety posture

- **LOCAL_TRUSTED** — loopback only, no auth, eval/exec unrestricted. This is a dev tool, not production.
- Readonly mode (server- or bridge-side) disables `run` / `call` / `set_value` / `winshot`.
- Prefer `inspect` / `list_path` / `repr_obj` / `logs` over `run` when you only need state.
