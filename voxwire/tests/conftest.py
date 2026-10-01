"""Test scaffolding.

Puts the app dir (voxwire/) on sys.path, keeps the plugin registry clean between
tests, and sets VOXWIRE_NO_IDLE_WATCH before any test imports server.py, so the
idle-release thread never races a test, whichever test module imports it first.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("VOXWIRE_NO_IDLE_WATCH", "1")

VOXWIRE = Path(__file__).resolve().parent.parent
if str(VOXWIRE) not in sys.path:
    sys.path.insert(0, str(VOXWIRE))

import pytest  # noqa: E402

import gateway  # noqa: E402
from integrations import base  # noqa: E402


@pytest.fixture(autouse=True)
def _registry():
    """Ensure the discovered plugins are live, and drop any throwaway
    integration a test registers so it can't leak into the next test."""
    gateway.load_integrations()
    keep = {i.name for i in base.registered()}
    yield
    for integ in base.registered():
        if integ.name not in keep:
            base.unregister(integ.name)
