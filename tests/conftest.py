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


@pytest.fixture
def qk(tmp_path, load_filter):
    return load_filter()
