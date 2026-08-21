# tests/conftest.py
import importlib.util, json, sys, types
from pathlib import Path
import pytest

# The admin module imports fastapi at module level, and fastapi imports the
# real pydantic at its own import time. Import fastapi first so the real
# pydantic lands in sys.modules and the stub below is only used when pydantic
# is genuinely absent (otherwise the stub would break fastapi's own imports).
try:
    import fastapi  # noqa: F401  (also loads real pydantic)
except ImportError:
    pass

# fastapi (any) forwards on_startup/on_shutdown to starlette.routing.Router.
# Newer starlette dropped those kwargs; OWUI's pinned stack accepts them. Patch
# the local Router to tolerate them so the admin module imports in the sandbox.
import inspect

try:
    from starlette.routing import Router as _Router

    if "on_startup" not in inspect.signature(_Router.__init__).parameters:

        def _patched_init(self, *args, on_startup=None, on_shutdown=None, **kwargs):
            _Router.__init__.__wrapped__ if False else None
            orig = _patched_init._orig
            return orig(self, *args, **kwargs)

        _patched_init._orig = _Router.__init__
        _Router.__init__ = _patched_init
except Exception:
    pass

REPO = Path(__file__).resolve().parent.parent
FILTER = REPO / "quota_keeper_filter.py"
ADMIN = REPO / "quota_keeper_admin.py"


def _stub_pydantic():
    m = types.ModuleType("pydantic")

    class Field:
        def __init__(self, default=None, description=""):
            self.default, self.description = default, description

    class BaseModel:
        def __init__(self, **kw):
            ann = {}
            for klass in type(self).__mro__:
                ann.update(getattr(klass, "__annotations__", {}))
            for k in ann:
                d = getattr(type(self), k, None)
                setattr(self, k, kw.pop(k, d.default if isinstance(d, Field) else d))

    m.BaseModel, m.Field = BaseModel, Field
    sys.modules["pydantic"] = m


def _load(path, name):
    if "pydantic" not in sys.modules:
        _stub_pydantic()
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


@pytest.fixture
def load_filter(tmp_path, monkeypatch):
    def _():
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        return _load(FILTER, "qk_filter")
    return _


@pytest.fixture
def load_admin(tmp_path, monkeypatch):
    def _():
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        return _load(ADMIN, "qk_admin")
    return _


def _stub_webui_auth(monkeypatch):
    """Stub `open_webui.utils.auth` with the real dependency-style
    signatures (current OWUI: `get_current_user(request, response=,
    background_tasks=, auth_token=)` resolves the session user;
    `get_verified_user(user)` is a sync role gate taking the *user*).
    The admin module imports auth lazily at request time, so stubbing the
    modules in sys.modules is enough (real open_webui is not installed in
    the test env; see CLAUDE.md "no dependency manifest")."""
    ow = types.ModuleType("open_webui")
    utils = types.ModuleType("open_webui.utils")
    auth = types.ModuleType("open_webui.utils.auth")

    class AdminUser:
        id = "stub-admin"
        name = "Stub Admin"
        email = "admin@stub"
        role = "admin"
        group_ids = []

    async def get_current_user(request, **kw):
        return AdminUser()

    def get_verified_user(user):
        # dependency-style contract: receives the resolved user, never the
        # request (v0.2.1 regression was calling it with the request)
        assert hasattr(user, "role")
        return user

    auth.get_current_user = get_current_user
    auth.get_verified_user = get_verified_user
    utils.auth = auth
    ow.utils = utils
    monkeypatch.setitem(sys.modules, "open_webui", ow)
    monkeypatch.setitem(sys.modules, "open_webui.utils", utils)
    monkeypatch.setitem(sys.modules, "open_webui.utils.auth", auth)


@pytest.fixture
def admin_client(load_admin, monkeypatch):
    """TestClient for the qk_router with auth stubbed to a (role=admin) user.
    Task 2 made auth a hard 401 dependency; without this stub every request
    would fail auth before reaching the handler."""
    _stub_webui_auth(monkeypatch)

    def _():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        adm = load_admin()
        app = FastAPI()
        app.include_router(adm.qk_router, prefix="/api/v1/quota-keeper")
        return TestClient(app), adm

    return _


@pytest.fixture
def qk(tmp_path, load_filter):
    return load_filter()
