"""Test setup.

Run with the Python that has Hermes Agent installed, from the plugin root:

    /path/to/hermes-agent/.venv/bin/python -m pytest -q

Everything runs offline against an in-memory fake of the Chatwork API; no
token and no network are needed.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep Hermes' runtime files (status, locks, plugin-data) out of the real ~/.hermes.
_TMP_HOME = tempfile.mkdtemp(prefix="hermes-chatwork-test-")
os.environ["HERMES_HOME"] = _TMP_HOME
os.environ["HERMES_GATEWAY_LOCK_DIR"] = os.path.join(_TMP_HOME, "locks")
for _var in [v for v in os.environ if v.startswith("CHATWORK_")]:
    os.environ.pop(_var)

try:
    import gateway.platforms.base  # noqa: F401  (fail early with a clear message)
except Exception as exc:  # pragma: no cover
    pytest.exit(f"Hermes Agent is not importable from this Python ({exc}). "
                "Run pytest with the interpreter of your Hermes install.", returncode=4)


@pytest.fixture(autouse=True)
def _clean_chatwork_env(monkeypatch):
    for var in [v for v in os.environ if v.startswith("CHATWORK_")]:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture(scope="session", autouse=True)
def _chatwork_platform_registered():
    """Register the platform the way Hermes does (real PlatformEntry), so
    Platform("chatwork") resolves and our register() kwargs are checked."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    import adapter

    class _Ctx:
        def register_platform(self, **kw):
            platform_registry.register(PlatformEntry(**kw), scope=None)

    adapter.register(_Ctx())
    yield
    platform_registry.unregister("chatwork")
