"""Object introspection utilities — resolve paths, inspect, mutate, call."""

from __future__ import annotations

import inspect
import re
import types
from collections.abc import Mapping, Sequence, Set


def resolve(path: str, namespaces: dict[str, object]) -> object:
    """
    Resolve a dotted path with optional indexing against registered namespaces.

    Uses eval() — intentionally unrestricted for dev use. Handles:
        'app'                    → namespaces['app']
        'app.hobos[0].session'   → attribute + index traversal
        'len(app.hobos)'         → arbitrary expressions
    """
    return eval(path, {'__builtins__': __builtins__}, namespaces)


# ────────────────────────────────────────────────────────────────────────
# Public resolvers — all return structured dicts
# ────────────────────────────────────────────────────────────────────────

def run_code(code: str, namespaces: dict[str, object]) -> dict:
    """Evaluate expression or execute statement. Returns structured result."""
    try:
        result = eval(code, {'__builtins__': __builtins__}, namespaces)
        return {
            'result': repr(result),
            'type': type(result).__qualname__,
            'mode': 'eval',
        }
    except SyntaxError:
        exec(code, {'__builtins__': __builtins__}, namespaces)
        return {
            'result': 'OK',
            'type': 'NoneType',
            'mode': 'exec',
        }


def inspect_object(
    path: str,
    namespaces: dict[str, object],
    *,
    max_depth: int = 2,
    max_items: int = 50,
    max_repr_len: int = 200,
) -> dict:
    """Structured inspection — type, repr, attrs. Returns serialized dict directly."""
    obj = resolve(path, namespaces)
    tree = _serialize_obj(
        obj,
        max_depth=max_depth,
        max_items=max_items,
        max_repr_len=max_repr_len,
    )
    tree['path'] = path
    return tree


def get_source(path: str, namespaces: dict[str, object]) -> dict:
    """Get source code of a function, class, or method at the given path."""
    obj = resolve(path, namespaces)

    # Unwrap properties, classmethods, staticmethods
    if isinstance(obj, property):
        obj = obj.fget  # type: ignore[assignment]
    elif isinstance(obj, (classmethod, staticmethod)):
        obj = obj.__func__  # type: ignore[union-attr]

    try:
        src = inspect.getsource(obj)
        fname = inspect.getfile(obj)
        lineno = inspect.getsourcelines(obj)[1]
        return {
            'path': path,
            'file': fname,
            'line': lineno,
            'source': src,
        }
    except (TypeError, OSError) as e:
        return {'path': path, 'error': str(e)}


def list_state(namespaces: dict[str, object]) -> dict:
    """Overview of all registered namespaces."""
    entries = []
    for name, obj in namespaces.items():
        entries.append({
            'name': name,
            'type': type(obj).__qualname__,
            'repr': _safe_repr(obj, maxlen=60),
        })
    return {'namespaces': entries}


def list_path(
    path: str,
    namespaces: dict[str, object],
    *,
    max_items: int = 50,
    max_repr_len: int = 200,
) -> dict:
    """Shallow listing — table of contents for an object's contents."""
    obj = resolve(path, namespaces)
    tname = type(obj).__qualname__
    node: dict = {'path': path, 'type': tname}

    # ── Mappings ──
    if isinstance(obj, Mapping) and not isinstance(obj, (str, bytes)):
        node['kind'] = 'mapping'
        node['length'] = len(obj)  # type: ignore[arg-type]
        keys = []
        for i, k in enumerate(obj):
            if i >= max_items:
                node['truncated'] = True
                break
            keys.append(repr(k))
        node['keys'] = keys
        return node

    # ── Sequences & sets ──
    if isinstance(obj, (Sequence, Set)) and not isinstance(obj, (str, bytes)):
        node['kind'] = 'sequence'
        node['length'] = len(obj)  # type: ignore[arg-type]
        items = []
        for i, item in enumerate(obj):
            if i >= max_items:
                node['truncated'] = True
                break
            items.append({
                'type': type(item).__qualname__,
                'repr': _safe_repr(item, maxlen=max_repr_len),
            })
        node['items'] = items
        return node

    # ── General object ──
    node['kind'] = 'object'
    attrs = _get_public_attrs(obj, max_items=max_items)
    node['attrs'] = [
        {
            'name': n,
            'type': type(v).__qualname__,
            'repr': _safe_repr(v, maxlen=max_repr_len),
        }
        for n, v in attrs
    ]
    node['methods'] = _get_public_methods(obj, max_items=max_items)

    # Truncation check for attrs + methods combined
    all_public = [n for n in dir(obj) if not n.startswith('_')]
    if len(all_public) > max_items:
        node['truncated'] = True

    return node


def repr_path(
    path: str,
    namespaces: dict[str, object],
    *,
    max_repr_len: int = 200,
) -> dict:
    """Quick type + repr — fastest tool, minimal overhead."""
    obj = resolve(path, namespaces)
    return {
        'path': path,
        'type': type(obj).__qualname__,
        'repr': _safe_repr(obj, maxlen=max_repr_len),
    }


def call_path(
    path: str,
    namespaces: dict[str, object],
    args: list | None = None,
    kwargs: dict | None = None,
    *,
    max_repr_len: int = 200,
) -> dict:
    """Resolve callable at path, call with args/kwargs, return result."""
    a = args or []
    kw = kwargs or {}
    try:
        fn = resolve(path, namespaces)
        result = fn(*a, **kw)  # type: ignore[operator]
        return {
            'path': path,
            'result_type': type(result).__qualname__,
            'result_repr': _safe_repr(result, maxlen=max_repr_len),
            'ok': True,
        }
    except Exception as e:
        return {
            'path': path,
            'error': str(e),
            'error_type': type(e).__qualname__,
            'ok': False,
        }


# Regex for bracket indexing at end of path: foo.bar[0] or foo['key']
_BRACKET_TAIL_RE = re.compile(r'^(.+)\[(.+)\]$')


def set_value(
    path: str,
    namespaces: dict[str, object],
    value_expr: str,
) -> dict:
    """Set a value on an object — supports dot attrs and bracket indexing."""
    val = eval(value_expr, {'__builtins__': __builtins__}, namespaces)

    try:
        # Try bracket indexing first: path like 'obj.data[0]' or 'obj.data["key"]'
        m = _BRACKET_TAIL_RE.match(path)
        if m:
            parent_path, key_expr = m.group(1), m.group(2)
            parent = resolve(parent_path, namespaces)
            key = eval(key_expr, {'__builtins__': __builtins__}, namespaces)
            parent[key] = val  # type: ignore[index]
        else:
            # Dot-separated: split into parent + attr
            dot = path.rfind('.')
            if dot == -1:
                # Top-level name — set directly in namespaces
                namespaces[path] = val
            else:
                parent_path, attr = path[:dot], path[dot + 1:]
                parent = resolve(parent_path, namespaces)
                setattr(parent, attr, val)

        return {
            'path': path,
            'ok': True,
            'new_value_repr': _safe_repr(val),
        }
    except Exception as e:
        return {
            'path': path,
            'ok': False,
            'error': str(e),
            'error_type': type(e).__qualname__,
        }


# ────────────────────────────────────────────────────────────────────────
# Serialization
# ────────────────────────────────────────────────────────────────────────

def _serialize_obj(
    obj: object,
    *,
    max_depth: int = 2,
    max_items: int = 50,
    max_repr_len: int = 200,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> dict:
    """
    Recursive bounded serializer — turns an object into a JSON-safe dict.

    Returns dict with keys:
        type  — qualname of the object's class
        repr  — truncated repr string
    And optionally:
        attrs     — list of {name, type, repr} for public attributes
        items     — list of serialized children (sequences/sets)
        entries   — list of {key, value} for mappings
        length    — element count for sized containers
        truncated — true when items/attrs/entries were capped at max_items

    Cycle detection via id() prevents infinite loops on self-referential
    structures. Depth gating prevents runaway recursion on deep graphs.
    """
    if _seen is None:
        _seen = set()

    tname = type(obj).__qualname__
    rstr = _safe_repr(obj, maxlen=max_repr_len)

    # Cycle detection — bail with marker
    oid = id(obj)
    if oid in _seen:
        return {'type': tname, 'repr': '<circular ref>'}
    _seen.add(oid)

    # Base node — always present
    node: dict = {'type': tname, 'repr': rstr}

    # Length for sized containers
    if isinstance(obj, (Mapping, Sequence, Set)) and not isinstance(obj, (str, bytes)):
        try:
            node['length'] = len(obj)  # type: ignore[arg-type]
        except Exception:
            pass

    # At max depth, return just type+repr — no recursion into children
    if _depth >= max_depth:
        _seen.discard(oid)
        return node

    # Recurse kwargs for children
    rkw = dict(
        max_depth=max_depth,
        max_items=max_items,
        max_repr_len=max_repr_len,
        _depth=_depth + 1,
        _seen=_seen,
    )

    # ── Mappings (dict-like) ──
    if isinstance(obj, Mapping) and not isinstance(obj, (str, bytes)):
        entries = []
        items_iter = iter(obj.items())
        for i, (k, v) in enumerate(items_iter):
            if i >= max_items:
                node['truncated'] = True
                break
            entries.append({
                'key': _safe_repr(k, maxlen=max_repr_len),
                'value': _serialize_obj(v, **rkw),
            })
        if entries:
            node['entries'] = entries

    # ── Sequences & sets (list, tuple, set, frozenset, ...) ──
    elif isinstance(obj, (Sequence, Set)) and not isinstance(obj, (str, bytes)):
        items = []
        items_iter = iter(obj)
        for i, item in enumerate(items_iter):
            if i >= max_items:
                node['truncated'] = True
                break
            items.append(_serialize_obj(item, **rkw))
        if items:
            node['items'] = items

    # ── General objects — serialize public attrs ──
    else:
        attrs_list = _get_public_attrs(obj, max_items=max_items)
        if attrs_list:
            serialized = []
            for name, val in attrs_list:
                serialized.append({
                    'name': name,
                    'type': type(val).__name__,
                    'repr': _safe_repr(val, maxlen=max_repr_len),
                })
            node['attrs'] = serialized
            # Flag truncation if _get_public_attrs hit its cap
            all_public = [n for n in dir(obj) if not n.startswith('_')]
            if len(all_public) > max_items:
                node['truncated'] = True

    _seen.discard(oid)
    return node


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def _safe_repr(obj: object, maxlen: int = 200) -> str:
    """Repr with truncation and error safety."""
    try:
        r = repr(obj)
    except Exception as e:
        return f'<repr error: {e}>'
    if len(r) > maxlen:
        return r[:maxlen - 3] + '...'
    return r


def _get_public_attrs(obj: object, *, max_items: int = 50) -> list[tuple[str, object]]:
    """Get non-dunder, non-callable attributes with their values, sorted."""
    attrs = []
    for name in sorted(dir(obj)):
        if name.startswith('_'):
            continue
        try:
            val = getattr(obj, name)
        except Exception:
            continue
        # Skip methods/functions — we want state, not API surface
        if callable(val) and not isinstance(val, (type, types.ModuleType)):
            continue
        attrs.append((name, val))
        if len(attrs) >= max_items:
            break
    return attrs


def _get_public_methods(obj: object, *, max_items: int = 50) -> list[str]:
    """Get non-dunder callable names — complement to _get_public_attrs."""
    methods = []
    for name in sorted(dir(obj)):
        if name.startswith('_'):
            continue
        try:
            val = getattr(obj, name)
        except Exception:
            continue
        if callable(val) and not isinstance(val, (type, types.ModuleType)):
            methods.append(name)
            if len(methods) >= max_items:
                break
    return methods
