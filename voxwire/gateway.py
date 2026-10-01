#!/usr/bin/env python3
"""Voxwire gateway — the generic transcript → integration router.

A (cleaned) transcript comes in; the gateway wraps it in a `Command`, asks the
plugin registry which `Integration` should handle it, runs that integration, and
returns a structured result. Nothing here names a specific service, webhook, or
wake word — all of that lives in the integrations, which are drop-in plugins
under `voxwire/integrations/`, or outside Voxwire entirely (see
`load_integrations`). That is what makes the public core generic: adding or
removing an integration never touches this file.

Security (see docs/DESIGN.md §3): the gateway NEVER approves a gated action.
When an integration returns `requires_confirmation`, the gateway surfaces the
preview and stops — the native confirmation gate and the actual execution are
owned by the executor host (the menubar app), not by this router and never by
the agent. `dispatch()` cannot run a gated action even if it wanted to: a
destructive `handle()` returns the gate request *without* acting.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import logging
import os
import pkgutil
import sys
from pathlib import Path

from integrations import base
from integrations.base import Command, Integration, route

#: Entry-point group an installed package declares its integrations in.
PLUGIN_GROUP = "voxwire.integrations"
#: Extra plugin folders (os.pathsep-separated) for an overlay that isn't packaged.
PLUGIN_PATH_ENV = "VOXWIRE_PLUGIN_PATH"

_loaded = False
_errors: dict[str, str] = {}          # where (full path / entry point) -> "Type: message"
_error_summary: dict[str, str] = {}   # short label -> exception type; safe to expose
log = logging.getLogger(__name__)


def load_integrations(force: bool = False) -> list[str]:
    """Import every plugin so its `register(...)` call runs; return the
    registered names. Plugins come from three places:

    1. modules under `voxwire/integrations/` (drop a `*.py` in there and it is live);
    2. installed packages that declare a module, an `Integration` instance or an
       `Integration` subclass in the `voxwire.integrations` entry-point group;
    3. `*.py` modules in the folders listed in VOXWIRE_PLUGIN_PATH.

    2 and 3 are how a private overlay adds integrations to an unmodified Voxwire
    (docs/DESIGN.md, BYT-123). A plugin runs in-process with your privileges, so
    only point these at code you trust. An outside plugin that fails to load is
    recorded in `load_errors()` (and, without detail, in `load_error_summary()`)
    and skipped; it can't take the gateway down.

    Idempotent and cheap: safe to call on every dispatch (module imports are
    cached and `register` de-duplicates by name).
    """
    global _loaded
    if _loaded and not force:
        return [i.name for i in base.registered()]
    _errors.clear()
    _error_summary.clear()
    pkg = base.__package__  # "integrations"
    pkg_dir = Path(base.__file__).resolve().parent
    for info in pkgutil.iter_modules([str(pkg_dir)]):
        name = info.name
        if name == "base" or name.startswith("_"):
            continue
        importlib.import_module(f"{pkg}.{name}")
    for ep in importlib.metadata.entry_points(group=PLUGIN_GROUP):
        _try(f"entry point {ep.name} = {ep.value}", f"entry point {ep.name}",
             lambda ep=ep: _register_object(ep.load()))
    for folder in filter(None, os.environ.get(PLUGIN_PATH_ENV, "").split(os.pathsep)):
        for path in sorted(Path(folder).glob("*.py")):
            if not path.name.startswith("_"):
                _try(str(path), path.name, lambda path=path: _import_file(path))
    _loaded = True
    return [i.name for i in base.registered()]


def load_errors() -> dict[str, str]:
    """Outside plugins that failed in the last load, and why — in full. For local
    use only: the message can carry whatever the plugin put in it (a token, an
    internal host name). Anything served over HTTP uses `load_error_summary()`."""
    return dict(_errors)


def load_error_summary() -> dict[str, str]:
    """The same failures with the detail stripped: plugin file name or entry-point
    name -> exception type. Safe to expose; the full message is in the log."""
    return dict(_error_summary)


def _try(where: str, label: str, load) -> None:
    """Run one outside plugin's load; on failure record it and undo it.

    A plugin can `register(...)` and then raise further down its module. It is
    reported as skipped, so it must not stay routable either: the registry is
    put back exactly as it was before the attempt (which also restores any
    integration the failed plugin replaced by name).

    Anything a plugin raises counts as a failed load, including `SystemExit`
    (a plugin calling `sys.exit()` when its config is missing must not stop
    Voxwire). The one exception is `KeyboardInterrupt`: that is the user
    stopping Voxwire, not the plugin failing, so it is rolled back and re-raised.
    """
    before = base.registered()
    try:
        load()
    except BaseException as e:  # one broken outside plugin must not stop the rest
        base._REGISTRY[:] = before
        if isinstance(e, KeyboardInterrupt):
            raise
        _errors[where] = f"{type(e).__name__}: {e}"
        _error_summary[label] = type(e).__name__
        log.warning("voxwire: skipped plugin %s (%s)", where, _errors[where])


def _register_object(obj) -> None:
    """An entry point names a module (importing it ran its `register` call), an
    `Integration` instance, or an `Integration` subclass."""
    if isinstance(obj, Integration):
        base.register(obj)
    elif isinstance(obj, type) and issubclass(obj, Integration):
        base.register(obj())


def _import_file(path: Path) -> None:
    """Import a plugin file once, under a name unique to its location."""
    resolved = path.resolve()
    name = f"voxwire_plugin_{path.stem}_{hashlib.sha1(str(resolved).encode()).hexdigest()[:8]}"
    if name in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(name, resolved)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise


def integrations() -> list[dict]:
    """A light description of the registered plugins (for UIs / debugging)."""
    load_integrations()
    return [{"name": i.name, "wake_words": list(i.wake_words)}
            for i in base.registered()]


def dispatch(transcript: str, source: str = "unknown",
             confidence: float = 1.0, meta: dict | None = None) -> dict:
    """Route one transcript through the integration plugins and return a result.

    The returned dict is the same shape whether or not anything matched:
      matched / handled_by ....... which integration (if any) took it
      ok ......................... did the integration report success
      reply ...................... short natural-language result to speak/show
      requires_confirmation ...... True → a gated action awaiting the executor;
                                   the gateway has NOT executed it
      preview .................... the exact action to show at the gate
    """
    load_integrations()
    text = (transcript or "").strip()
    out = {
        "transcript": text,
        "source": source,
        "matched": False,
        "handled_by": None,
        "ok": False,
        "reply": "",
        "requires_confirmation": False,
        "preview": None,
    }
    if not text:
        out["reply"] = "(empty transcript)"
        return out

    command = Command(text=text, source=source, confidence=confidence,
                      meta=meta or {})
    integ = route(command)
    if integ is None:
        out["reply"] = "No integration handled that."
        return out

    out["matched"] = True
    out["handled_by"] = integ.name
    try:
        result = integ.handle(command)
    except Exception as e:  # a plugin bug must not take down the gateway
        out["reply"] = f"[error] {integ.name}: {type(e).__name__}: {e}"
        return out

    out["ok"] = bool(result.ok)
    out["reply"] = result.reply
    out["requires_confirmation"] = bool(result.requires_confirmation)
    out["preview"] = result.preview
    return out
