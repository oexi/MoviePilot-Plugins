# _*_ coding: utf-8 _*_
"""Lifecycle-safe bridge for the current MoviePilot V3 search API.

``ChainBase`` is a process-wide host class while plugin modules are not.  In
particular, the host may retain the class while a plugin module is reloaded,
and two V3 plugins may need the same search boundary at the same time.  The
bridge therefore stores one shared state dictionary on ``ChainBase`` and
keeps one owner record per key in that dictionary.  Module copies only add or
remove their record; they never install a second layer of wrappers.

The two per-site search boundaries are always wrapped, and the independent
sync/async refresh boundaries are wrapped when the host exposes them.  The
host's global plugin route, ordinary-site route and page-size calculation
remain host-owned.
"""

import functools
import importlib
import inspect
import logging
import re
import sys
import threading
import time
import types
import weakref
from collections.abc import Mapping
from typing import Any, Callable, Optional
from urllib.parse import urlsplit


# This attribute name is deliberately stable across module reloads.  State is
# attached to the host class rather than to this module, so independently
# loaded plugin copies can share one bridge.
_STATE_ATTR = "__jackett_extend_host_compat_state__"

# The ABI marker must be a process-stable value.  A module-local object (the
# old implementation's token) makes every module reload look incompatible and
# causes the second copy to tear down the first copy's bridge.
_BRIDGE_ABI = 5
_BRIDGE_ID = "jackett_extend_host_compat"
# Keep the old name available for callers/tests that inspected it.  It is a
# value, not an identity token, and is intentionally equal in every module
# copy.
_BRIDGE_TOKEN = _BRIDGE_ID

_METHODS = (
    "search_site_torrents",
    "async_search_site_torrents",
)

# Refresh is a separate host boundary from ordinary search.  Older V3 hosts
# may not expose one or both refresh methods, so they are discovered
# opportunistically when the bridge is installed.  Keep ``_METHODS`` as the
# required search contract for source compatibility with existing callers.
_OPTIONAL_METHODS = (
    "refresh_torrents",
    "async_refresh_torrents",
)
_ALL_METHODS = _METHODS + _OPTIONAL_METHODS

# The plugin modules expose refresh through their module map rather than a
# synchronous instance method.  Prefer a dedicated refresh implementation when
# an integration has one, then use the ordinary search implementation as the
# compatibility fallback used by JackettExtend/ProwlarrExtend.
_OWNER_METHODS = {
    "search_site_torrents": ("search_torrents",),
    "async_search_site_torrents": ("async_search_torrents",),
    "refresh_torrents": ("refresh_torrents", "search_torrents"),
    "async_refresh_torrents": (
        "async_refresh_torrents",
        "async_search_torrents",
    ),
}

# ``None`` historically meant the one plugin owner.  Keep a fixed key for
# that API so a reload replaces the old default owner rather than leaving it
# behind as a second owner.  Distinct integrations can opt into coexistence
# by passing an explicit ``owner_key``.
_DEFAULT_OWNER_KEY = "__jackett_extend_default_owner__"

# Module copies have separate globals.  Publish the short installation lock
# through ``sys.modules`` so two copies racing on a previously unpatched
# ChainBase cannot both install their own first wrapper layer.
_SHARED_LOCK_KEY = "_moviepilot_v3_host_compat_install_lock"
_lock_holder = types.ModuleType(_SHARED_LOCK_KEY)
_lock_holder.install_lock = threading.RLock()
_lock_holder = sys.modules.setdefault(_SHARED_LOCK_KEY, _lock_holder)
_STATE_INIT_LOCK = _lock_holder.install_lock
_MISSING = object()

# Refresh callers need to distinguish a real upstream failure from a valid
# empty feed without coupling a process-wide wrapper to the module copy that
# raised the exception.  The value marker is therefore part of the shared ABI:
# a wrapper installed from JackettExtend can recognize an exception created by
# ProwlarrExtend (and vice versa) without relying on ``isinstance``.
_UPSTREAM_ERROR_ATTR = "__moviepilot_virtual_site_upstream_error__"
_UPSTREAM_ERROR_MARKER = "sanitized-upstream-error-v1"
_DIAGNOSTIC_INTERVAL_SECONDS = 300.0


class SanitizedUpstreamError(RuntimeError):
    """Non-sensitive upstream failure that may cross a dedicated refresh API."""

    __moviepilot_virtual_site_upstream_error__ = _UPSTREAM_ERROR_MARKER

    def __init__(self, category: object = "upstream_error"):
        value = str(category or "upstream_error")
        allowed = {
            "timeout",
            "request_error",
            "empty_response",
            "invalid_response",
            "torznab_error",
        }
        if value not in allowed and not re.fullmatch(r"http_[1-5][0-9]{2}", value):
            value = "upstream_error"
        self.category = value
        super().__init__(f"Virtual site upstream request failed ({value})")


def _is_sanitized_upstream_error(error: BaseException) -> bool:
    """Recognize the process-stable refresh signal from any module copy."""
    try:
        return getattr(error, _UPSTREAM_ERROR_ATTR, None) == _UPSTREAM_ERROR_MARKER
    except Exception:
        return False


def _propagates_upstream_error(route: str, error: BaseException) -> bool:
    return route in _OPTIONAL_METHODS and _is_sanitized_upstream_error(error)


def _diagnostic_owner_key(owner: object) -> str:
    """Return a bounded internal owner label without reading user data."""
    try:
        value = getattr(owner, "_bridge_owner_key", None)
    except Exception:
        value = None
    if value is None:
        return "default"
    text = str(value).strip()
    if not text:
        return "default"
    return re.sub(r"[^A-Za-z0-9_.:-]", "?", text)[:64]


def _emit_bridge_warning(message: str) -> None:
    """Emit one diagnostic without making logging a bridge dependency."""
    try:
        module = importlib.import_module("app.sdk.logging")
        host_logger = getattr(module, "logger", None)
        warning = getattr(host_logger, "warning", None)
        if callable(warning):
            warning(message)
            return
    except Exception:
        pass
    try:
        logging.getLogger(__name__).warning(message)
    except Exception:
        pass


def _record_bridge_error(
    state: Mapping,
    owner: object,
    route: str,
    error: BaseException,
    *,
    phase: str = "dispatch",
) -> None:
    """Rate-limit a sanitized bridge failure while preserving fail-open routing."""
    try:
        owner_key = _diagnostic_owner_key(owner)
        error_type = type(error).__name__[:64] or "Exception"
        key = (phase, owner_key, str(route), error_type)
        now = time.monotonic()
        lock = _state_lock(state)
        emit = False
        count = 1
        with lock:
            diagnostics = state.get("diagnostics")
            if not isinstance(diagnostics, dict):
                diagnostics = {}
                try:
                    state["diagnostics"] = diagnostics
                except (TypeError, AttributeError):
                    diagnostics = {}
            record = diagnostics.get(key)
            if not isinstance(record, dict):
                record = {"last_emit": 0.0, "suppressed": 0}
                diagnostics[key] = record
            last_emit = float(record.get("last_emit", 0.0) or 0.0)
            if last_emit <= 0.0 or now - last_emit >= _DIAGNOSTIC_INTERVAL_SECONDS:
                count = int(record.get("suppressed", 0) or 0) + 1
                record["last_emit"] = now
                record["suppressed"] = 0
                emit = True
            else:
                record["suppressed"] = int(record.get("suppressed", 0) or 0) + 1
        if emit:
            _emit_bridge_warning(
                "bridge_dispatch_error "
                f"bridge={_BRIDGE_ID} route={route} owner={owner_key} "
                f"phase={phase} error_type={error_type} count={count}"
            )
    except Exception:
        # Diagnostics must never alter the host fallback path.
        pass


# Prefer the stable SDK export.  ``app.chain`` package-root symbols are served
# by MoviePilot's versioned Compat layer; ``app.chain.base`` is the canonical
# owner and remains a fallback.  All paths resolve to the same class object.
_CHAIN_BASE_MODULES = ("app.sdk.chain", "app.chain.base")


def _find_chain_base() -> Optional[type]:
    """Lazily resolve the current host boundary."""
    for module_name in _CHAIN_BASE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        chain_base = getattr(module, "ChainBase", None)
        if inspect.isclass(chain_base):
            return chain_base
    return None


def _normalise_domain(site: Mapping) -> str:
    value = str(site.get("domain") or "").strip()
    if value.lower().startswith(("http://", "https://")):
        try:
            value = urlsplit(value).hostname or value
        except ValueError:
            pass
    return value


def _normalise_owner_key(owner_key: Any) -> Any:
    """Return a dictionary-safe key while preserving normal key identity.

    Owner keys are normally strings.  Handling an accidentally unhashable
    value by identity keeps the bridge from raising during plugin shutdown;
    the same object can still be used to remove its record later.
    """
    if owner_key is None:
        return _DEFAULT_OWNER_KEY
    try:
        hash(owner_key)
    except Exception:
        return ("__unhashable_owner_key__", id(owner_key))
    return owner_key


def _generation(state: Mapping) -> int:
    try:
        return int(state.get("generation", 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _bump_generation(state: Mapping) -> int:
    value = _generation(state) + 1
    try:
        state["generation"] = value
    except (TypeError, AttributeError):
        # A read-only legacy snapshot is only used during restoration.  Its
        # wrapper cannot safely publish a generation, so the process lock is
        # the best available fallback.
        return value
    return value


def _owner_from_record(record: Mapping) -> Any:
    owner_ref = record.get("owner_ref")
    if callable(owner_ref):
        try:
            return owner_ref()
        except Exception:
            return None
    # Only deliberately non-weak-referenceable test doubles (or old legacy
    # state) use this strong fallback.  Normal plugin instances are held by a
    # weak reference and cannot be retained by a stale wrapper after reload.
    return record.get("owner")


def _owner_from_state(state: Mapping) -> Any:
    """Return one live owner for old callers and isolated test teardown.

    New state is keyed and may have several owners.  Returning the first live
    one keeps the historical helper useful without making it part of routing.
    Legacy state stores the owner at the top level.
    """
    owners = state.get("owners")
    if isinstance(owners, Mapping):
        for record in owners.values():
            if isinstance(record, Mapping):
                owner = _owner_from_record(record)
                if owner is not None:
                    return owner
        return None
    owner_ref = state.get("owner_ref")
    if callable(owner_ref):
        try:
            return owner_ref()
        except Exception:
            return None
    # Only used for deliberately non-weak-referenceable test doubles.  Normal
    # plugin instances always use a weak reference and cannot be retained by
    # a stale wrapper after reload/stop.
    return state.get("owner")


def _weak_owner(plugin: object):
    try:
        return weakref.ref(plugin)
    except TypeError:
        return None


def _predicate_from_record(record: Mapping) -> Optional[Callable]:
    predicate_ref = record.get("predicate_ref")
    if callable(predicate_ref):
        try:
            return predicate_ref()
        except Exception:
            return None
    predicate = record.get("predicate")
    return predicate if callable(predicate) else None


def _predicate_from_state(state: Mapping) -> Optional[Callable]:
    """Read a predicate from a legacy single-owner snapshot."""
    predicate_ref = state.get("predicate_ref")
    if callable(predicate_ref):
        try:
            return predicate_ref()
        except Exception:
            return None
    predicate = state.get("predicate")
    return predicate if callable(predicate) else None


def _state_lock(state: Mapping):
    """Return the state lock, adding one when migrating an older state."""
    lock = state.get("lock")
    if lock is not None and hasattr(lock, "__enter__"):
        return lock
    # Older V3 snapshots had no lock.  The short initialization section is
    # guarded by a module lock; all current wrappers use the published lock.
    with _STATE_INIT_LOCK:
        lock = state.get("lock")
        if lock is None or not hasattr(lock, "__enter__"):
            lock = threading.RLock()
            try:
                state["lock"] = lock
            except (TypeError, AttributeError):
                return _STATE_INIT_LOCK
    return lock


def _call_predicate(predicate: Callable, site: Mapping, domain: str):
    """Apply the current virtual-site predicate contract."""
    return predicate(site, domain)


def _owner_for_site(
    state: Mapping,
    site: object,
    route: str = "search_site_torrents",
) -> Any:
    """Return a generation-validated owner for one virtual-site decision.

    Predicate execution is intentionally outside the lock.  A generation
    check before and after each predicate prevents a reload/install/uninstall
    during that predicate from dispatching into the owner captured before the
    change.  A predicate failure only disables that owner; another owner can
    still claim the site.
    """
    if not isinstance(site, Mapping):
        # In particular, preserve the host's global ``site={}`` plugin route.
        return None
    try:
        if not site:
            return None
    except Exception:
        return None
    try:
        domain = _normalise_domain(site)
    except Exception:
        return None

    lock = _state_lock(state)
    with lock:
        if not state.get("active", True):
            return None
        generation = _generation(state)
        owners = state.get("owners")
        keyed_state = isinstance(owners, Mapping)
        if keyed_state:
            # Snapshot insertion order.  A later broad predicate therefore
            # cannot steal a site already claimed by an earlier owner.
            candidates = tuple(owners.items())
        else:
            # Let wrappers created by a prior single-owner bridge drain safely
            # if they are ever invoked while legacy state is being retired.
            candidates = ((_DEFAULT_OWNER_KEY, state),)

    for owner_key, record in candidates:
        if not isinstance(record, Mapping):
            continue
        owner = _owner_from_record(record) if keyed_state else _owner_from_state(record)
        if owner is None:
            continue
        predicate = _predicate_from_record(record) if keyed_state else _predicate_from_state(record)
        if predicate is None:
            # Read the current owner on each request so reloads cannot retain a
            # stopped instance through a bound method.
            try:
                candidate = getattr(owner, "_is_virtual_site", None)
            except Exception:
                candidate = None
            if callable(candidate):
                predicate = candidate
        if predicate is None:
            continue

        # If an earlier predicate changed the owner set, its snapshot is no
        # longer authoritative.  The host fallback is deterministic and the
        # next request sees the new owner set.
        with lock:
            if not state.get("active", True) or _generation(state) != generation:
                return None
        try:
            owned = bool(_call_predicate(predicate, site, domain))
        except Exception as error:
            _record_bridge_error(state, owner, route, error, phase="predicate")
            owned = False
        if not owned:
            continue

        with lock:
            if not state.get("active", True) or _generation(state) != generation:
                return None
            current_owners = state.get("owners")
            if keyed_state and isinstance(current_owners, Mapping):
                current_record = current_owners.get(owner_key, _MISSING)
                if current_record is not record:
                    return None
                if _owner_from_record(current_record) is not owner:
                    return None
            elif _owner_from_state(state) is not owner:
                return None
        return owner
    return None


def _owner_methods(method: object) -> tuple:
    """Normalize a route's preferred owner method names."""
    if isinstance(method, str):
        return (method,)
    try:
        return tuple(method)
    except TypeError:
        return ()


def _call_owner(owner: object, method: object, args: tuple, kwargs: dict):
    """Call the first available owner method without masking its errors."""
    methods = _owner_methods(method)
    for name in methods:
        try:
            candidate = getattr(owner, name)
        except AttributeError:
            continue
        if callable(candidate):
            return candidate(*args, **kwargs)
    raise AttributeError(f"owner has no callable method in {methods!r}")


async def _call_owner_async(owner: object, method: object, args: tuple, kwargs: dict):
    result = _call_owner(owner, method, args, kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _site_from_call(args: tuple, kwargs: dict):
    if "site" in kwargs:
        return kwargs.get("site")
    # The first positional argument is ChainBase; site follows it.
    return args[1] if len(args) > 1 else None


def _owner_call_arguments(signature: inspect.Signature, args: tuple, kwargs: dict):
    """Bind host arguments before relaying them to a plugin owner.

    Host and plugin method signatures use the same parameter names but not the
    same positional order: plugin search methods include ``cat`` before
    ``page``, while the host refresh method places ``mtype`` last.  Binding
    against the original host signature makes mixed and keyword calls follow
    the host contract, then relaying named arguments avoids shifting values at
    either boundary.
    """
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    parameters = signature.parameters
    receiver_name = next(iter(parameters), None)
    owner_args = ()
    owner_kwargs = {}
    for name, value in bound.arguments.items():
        if name == receiver_name:
            continue
        parameter = parameters[name]
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            owner_kwargs.update(value)
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            owner_args += tuple(value)
        else:
            owner_kwargs[name] = value
    return owner_args, owner_kwargs


def _make_sync_wrapper(original, state, route: str = "search_site_torrents"):
    host_signature = inspect.signature(original)

    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        site = _site_from_call(args, kwargs)
        owner = _owner_for_site(state, site, route)
        if owner is not None:
            try:
                owner_args, owner_kwargs = _owner_call_arguments(
                    host_signature, args, kwargs
                )
                return _call_owner(
                    owner,
                    _OWNER_METHODS.get(route, ("search_torrents",)),
                    owner_args,
                    owner_kwargs,
                )
            except Exception as error:
                # A plugin module must not break the host's ordinary search
                # chain.  BaseException (including cancellation) is allowed
                # through deliberately.
                if _propagates_upstream_error(route, error):
                    raise
                _record_bridge_error(state, owner, route, error)
        return original(*args, **kwargs)

    wrapper.__jackett_extend_wrapper__ = True
    return wrapper


def _make_async_wrapper(original, state, route: str = "async_search_site_torrents"):
    host_signature = inspect.signature(original)

    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        site = _site_from_call(args, kwargs)
        owner = _owner_for_site(state, site, route)
        if owner is not None:
            try:
                owner_args, owner_kwargs = _owner_call_arguments(
                    host_signature, args, kwargs
                )
                return await _call_owner_async(
                    owner,
                    _OWNER_METHODS.get(route, ("async_search_torrents",)),
                    owner_args,
                    owner_kwargs,
                )
            except Exception as error:
                if _propagates_upstream_error(route, error):
                    raise
                _record_bridge_error(state, owner, route, error)
        result = original(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    wrapper.__jackett_extend_wrapper__ = True
    return wrapper


def _state_method_names(state: Mapping):
    """Return every method recorded by this or an older bridge state."""
    names = []
    for key in ("originals", "wrappers", "defined_here"):
        values = state.get(key) or {}
        if not isinstance(values, Mapping):
            continue
        for name in values:
            if isinstance(name, str) and name not in names:
                names.append(name)
    return tuple(names)


def _current_descriptor(chain_base: type, name: str):
    try:
        return inspect.getattr_static(chain_base, name)
    except AttributeError:
        return _MISSING


def _restore(chain_base: type, state: Mapping) -> bool:
    """Retire a state and restore only attributes still owned by its bridge.

    The method list comes from the state itself so a new module can safely
    migrate a prior snapshot, including older page-size or refresh wrappers.
    A third-party replacement is left untouched rather than being overwritten.
    """
    lock = _state_lock(state)
    with lock:
        try:
            state["active"] = False
        except (TypeError, AttributeError):
            pass
        _bump_generation(state)
        originals = state.get("originals") or {}
        wrappers = state.get("wrappers") or {}
        defined_here = state.get("defined_here") or {}
        ok = True
        for name in _state_method_names(state):
            if not isinstance(originals, Mapping) or name not in originals:
                continue
            if not isinstance(wrappers, Mapping):
                continue
            expected = wrappers.get(name, _MISSING)
            if expected is _MISSING:
                continue
            # Never replace a method that was changed after bridge install.
            if _current_descriptor(chain_base, name) is not expected:
                continue
            try:
                if isinstance(defined_here, Mapping) and defined_here.get(
                    name, name in getattr(chain_base, "__dict__", {})
                ):
                    setattr(chain_base, name, originals[name])
                elif name in getattr(chain_base, "__dict__", {}):
                    # The original came from a base class; removing our
                    # subclass assignment restores normal lookup exactly.
                    delattr(chain_base, name)
            except Exception:
                ok = False
        if getattr(chain_base, _STATE_ATTR, None) is state:
            try:
                delattr(chain_base, _STATE_ATTR)
            except Exception:
                ok = False
        return ok


def _state_is_current(chain_base: type, state: Mapping) -> bool:
    """Whether ``state`` is the current shared ABI and still owns wrappers."""
    if (
        state.get("abi") != _BRIDGE_ABI
        or state.get("bridge_id") != _BRIDGE_ID
        or not state.get("active", True)
    ):
        return False
    methods = tuple(state.get("methods") or ())
    if (
        not methods
        or any(name not in _ALL_METHODS for name in methods)
        or not all(name in methods for name in _METHODS)
    ):
        return False
    owners = state.get("owners")
    if not isinstance(owners, Mapping):
        return False
    wrappers = state.get("wrappers")
    if not isinstance(wrappers, Mapping):
        return False
    return all(
        name in wrappers
        and _current_descriptor(chain_base, name) is wrappers[name]
        for name in methods
    )


def _set_record_predicate(record: dict, predicate: Callable, plugin: object) -> None:
    """Publish an optional predicate without retaining an old plugin."""
    before = (record.get("predicate_ref"), record.get("predicate"))
    if inspect.ismethod(predicate) and getattr(predicate, "__self__", None) is plugin:
        record["predicate_ref"] = None
        record["predicate"] = None
    else:
        # Plain functions/lambdas are intentionally retained: a caller
        # commonly passes an inline predicate and a weak reference would
        # disappear before the first host search.  A closure that captures the
        # old plugin is omitted to avoid turning that optional hook into a
        # stale-owner root.
        closure = getattr(predicate, "__closure__", None) or ()
        captures_owner = False
        for cell in closure:
            try:
                if cell.cell_contents is plugin:
                    captures_owner = True
                    break
            except ValueError:
                continue
        if captures_owner:
            record["predicate_ref"] = None
            record["predicate"] = None
        else:
            record["predicate_ref"] = None
            record["predicate"] = predicate
    after = (record.get("predicate_ref"), record.get("predicate"))
    if before != after:
        # The caller owns the state lock; generation changes invalidate any
        # predicate decision that was in flight before this update.
        return


def _set_predicate(state: dict, predicate: Callable, plugin: object) -> None:
    """Compatibility helper for code that used the old state-level function."""
    owners = state.get("owners")
    if isinstance(owners, Mapping):
        for record in owners.values():
            if isinstance(record, dict) and _owner_from_record(record) is plugin:
                before = (record.get("predicate_ref"), record.get("predicate"))
                _set_record_predicate(record, predicate, plugin)
                if before != (record.get("predicate_ref"), record.get("predicate")):
                    _bump_generation(state)
                return
    # Legacy single-owner snapshots used these top-level fields.
    before = (state.get("predicate_ref"), state.get("predicate"))
    if inspect.ismethod(predicate) and getattr(predicate, "__self__", None) is plugin:
        state["predicate_ref"] = None
        state["predicate"] = None
    else:
        closure = getattr(predicate, "__closure__", None) or ()
        captures_owner = False
        for cell in closure:
            try:
                if cell.cell_contents is plugin:
                    captures_owner = True
                    break
            except ValueError:
                continue
        state["predicate_ref"] = None
        state["predicate"] = None if captures_owner else predicate
    if before != (state.get("predicate_ref"), state.get("predicate")):
        _bump_generation(state)


def _new_owner_record(plugin: object, predicate: Optional[Callable]) -> dict:
    owner_ref = _weak_owner(plugin)
    record = {
        "owner_ref": owner_ref,
        "owner": None if owner_ref is not None else plugin,
        "predicate_ref": None,
        "predicate": None,
    }
    if predicate is not None:
        _set_record_predicate(record, predicate, plugin)
    return record


def _set_owner(state: dict, plugin: object, owner_key: Any = None) -> None:
    """Compatibility shim for the former single-owner state helper.

    Current installation goes through :func:`_register_owner`, but retaining
    this small helper avoids breaking integrations that imported the old
    private function while still giving keyed state the correct generation
    semantics.
    """
    owners = state.get("owners")
    if isinstance(owners, dict):
        _register_owner(state, plugin, None, owner_key)
        return
    owner_ref = _weak_owner(plugin)
    state["owner_ref"] = owner_ref
    state["owner"] = None if owner_ref is not None else plugin
    _bump_generation(state)


def _same_logical_owner(first: object, second: object) -> bool:
    """Best-effort identity match used only while migrating old state."""
    if first is second or type(first) is type(second):
        return True
    first_name = getattr(first, "plugin_name", None)
    second_name = getattr(second, "plugin_name", None)
    return bool(first_name and second_name and first_name == second_name)


def _legacy_owner_records(
    state: Mapping,
    plugin: object,
    owner_key: Any,
):
    """Snapshot live owners before retiring an incompatible state."""
    owners = state.get("owners")
    if isinstance(owners, Mapping):
        records = []
        for key, record in owners.items():
            if not isinstance(record, Mapping):
                continue
            owner = _owner_from_record(record)
            if owner is None:
                continue
            target_key = key
            # A legacy/default slot belonging to the same logical plugin is
            # moved to its explicit modern key so reload does not leave a
            # duplicate predicate behind.
            if (
                owner_key is not None
                and key == _DEFAULT_OWNER_KEY
                and _same_logical_owner(owner, plugin)
            ):
                target_key = owner_key
            records.append((owner, _predicate_from_record(record), target_key))
        return records

    owner = _owner_from_state(state)
    if owner is None or owner_key is None:
        return []
    old_key = owner_key if _same_logical_owner(owner, plugin) else _DEFAULT_OWNER_KEY
    return [(owner, _predicate_from_state(state), old_key)]


def _register_owner(
    state: dict,
    plugin: object,
    predicate: Optional[Callable],
    owner_key: Any,
) -> bool:
    """Add or replace one keyed owner while preserving key order."""
    owners = state.get("owners")
    if not isinstance(owners, dict):
        try:
            owners = {}
            state["owners"] = owners
        except (TypeError, AttributeError):
            return False

    key = _normalise_owner_key(owner_key)
    current = owners.get(key, _MISSING)
    if isinstance(current, Mapping):
        current_owner = _owner_from_record(current)
        if current_owner is plugin:
            if predicate is not None:
                before = (current.get("predicate_ref"), current.get("predicate"))
                _set_record_predicate(current, predicate, plugin)
                after = (current.get("predicate_ref"), current.get("predicate"))
                if before != after:
                    _bump_generation(state)
            return True

    # Assigning an existing dictionary key preserves insertion order.  This
    # makes same-key reload replacement deterministic and prevents a late
    # broad predicate from moving ahead of an established owner.
    owners[key] = _new_owner_record(plugin, predicate)
    _bump_generation(state)
    return True


def _new_state(
    chain_base: type,
    plugin: object,
    predicate: Optional[Callable],
    owner_key: Any,
    legacy_owners=(),
):
    methods = tuple(
        name
        for name in _ALL_METHODS
        if callable(getattr(chain_base, name, None))
    )
    if not all(name in methods for name in _METHODS):
        return None
    state = {
        "abi": _BRIDGE_ABI,
        "bridge_id": _BRIDGE_ID,
        # Retain a stable marker under the historical field name for
        # diagnostics/migrations; independent copies deliberately compare the
        # value, never object identity.
        "module_token": _BRIDGE_TOKEN,
        "methods": methods,
        "active": True,
        "generation": 0,
        "lock": threading.RLock(),
        "owners": {},
        # Legacy fields remain harmlessly absent from new state.  The helper
        # functions above still understand them while draining old wrappers.
        "originals": {},
        "defined_here": {},
        "wrappers": {},
        "diagnostics": {},
    }
    lock = state["lock"]
    with lock:
        for old_owner, old_predicate, old_key in legacy_owners or ():
            if not _register_owner(state, old_owner, old_predicate, old_key):
                return None
        if not _register_owner(state, plugin, predicate, owner_key):
            return None
        try:
            for name in methods:
                state["originals"][name] = inspect.getattr_static(chain_base, name)
                state["defined_here"][name] = name in getattr(chain_base, "__dict__", {})
            state["wrappers"] = {
                name: (
                    _make_async_wrapper(
                        state["originals"][name], state, name
                    )
                    if name.startswith("async_")
                    else _make_sync_wrapper(
                        state["originals"][name], state, name
                    )
                )
                for name in methods
            }
            for name, wrapper in state["wrappers"].items():
                setattr(chain_base, name, wrapper)
            setattr(chain_base, _STATE_ATTR, state)
            return state
        except Exception:
            _restore(chain_base, state)
            return None


def install(
    plugin: object,
    predicate: Optional[Callable] = None,
    owner_key: Any = None,
) -> bool:
    """Install/update one keyed plugin owner for the host search boundary.

    ``owner_key`` is optional for source compatibility.  Omitting it uses the
    historical singleton owner slot; passing different keys allows multiple
    independently loaded plugins to share the wrappers.
    """
    chain_base = _find_chain_base()
    if chain_base is None:
        return False

    # Installation/migration mutates class attributes and is serialized only
    # for that short publication phase.  No owner code runs while held.
    with _STATE_INIT_LOCK:
        state = getattr(chain_base, _STATE_ATTR, None)
        legacy_owners = ()
        if isinstance(state, Mapping):
            if not _state_is_current(chain_base, state):
                # Preserve a live old owner when a different keyed plugin is
                # the first new module to arrive.  A same-logical-owner
                # reload is assigned the incoming key and replaced below;
                # the unkeyed API intentionally keeps its historical
                # singleton replacement semantics.
                legacy_owners = _legacy_owner_records(state, plugin, owner_key)
                if not _restore(chain_base, state):
                    return False
                state = getattr(chain_base, _STATE_ATTR, None)
            if isinstance(state, dict) and _state_is_current(chain_base, state):
                lock = _state_lock(state)
                with lock:
                    return _register_owner(state, plugin, predicate, owner_key)
            # If restoring a custom mapping failed to remove the attribute,
            # do not risk layering another wrapper over an unknown state.
            if getattr(chain_base, _STATE_ATTR, None) is not None:
                return False

        return (
            _new_state(
                chain_base,
                plugin,
                predicate,
                owner_key,
                legacy_owners=legacy_owners,
            )
            is not None
        )


def uninstall(plugin: object, owner_key: Any = None) -> bool:
    """Remove one owner, restoring originals only after the final owner.

    An explicit key must identify the exact current owner.  For compatibility,
    an omitted key first checks the historical default slot and then accepts a
    unique identity match in keyed slots.  A stale owner can never uninstall a
    same-key replacement.
    """
    chain_base = _find_chain_base()
    if chain_base is None:
        return False
    with _STATE_INIT_LOCK:
        state = getattr(chain_base, _STATE_ATTR, None)
        if not isinstance(state, Mapping):
            return False
        lock = _state_lock(state)
        with lock:
            if getattr(chain_base, _STATE_ATTR, None) is not state:
                return False
            owners = state.get("owners")
            if isinstance(owners, Mapping):
                if owner_key is not None:
                    key = _normalise_owner_key(owner_key)
                    record = owners.get(key, _MISSING)
                    if not isinstance(record, Mapping) or _owner_from_record(record) is not plugin:
                        return False
                else:
                    # Preserve the old no-key behavior and make shutdown
                    # resilient if an integration did not retain its key.
                    key = _normalise_owner_key(None)
                    record = owners.get(key, _MISSING)
                    if not isinstance(record, Mapping) or _owner_from_record(record) is not plugin:
                        matches = [
                            candidate_key
                            for candidate_key, candidate in owners.items()
                            if isinstance(candidate, Mapping)
                            and _owner_from_record(candidate) is plugin
                        ]
                        if len(matches) != 1:
                            return False
                        key = matches[0]
                try:
                    del owners[key]
                except (KeyError, TypeError):
                    return False
                _bump_generation(state)
                if owners:
                    return True
                return _restore(chain_base, state)

            # A pre-keyed bridge had one owner at the state top level.
            if _owner_from_state(state) is not plugin:
                return False
            return _restore(chain_base, state)


def status() -> dict:
    """Return small, non-sensitive diagnostics for isolated tests."""
    chain_base = _find_chain_base()
    if chain_base is None:
        return {"available": False, "installed": False, "owner_alive": False}
    state = getattr(chain_base, _STATE_ATTR, None)
    if not isinstance(state, Mapping):
        return {
            "available": True,
            "installed": False,
            "owner_alive": False,
        }
    lock = _state_lock(state)
    with lock:
        active = bool(state.get("active", True))
        owners = state.get("owners")
        if isinstance(owners, Mapping):
            owner_keys = tuple(owners.keys())
            owner_alive = any(
                isinstance(record, Mapping) and _owner_from_record(record) is not None
                for record in owners.values()
            )
        else:
            owner_keys = ()
            owner_alive = _owner_from_state(state) is not None
        return {
            "available": True,
            "installed": active,
            "owner_alive": active and owner_alive,
            "owner_keys": owner_keys,
            "owners": len(owner_keys),
            "methods": tuple(state.get("methods") or ()),
        }


is_installed = lambda: bool(status().get("installed"))
