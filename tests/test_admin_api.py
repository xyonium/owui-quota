# tests/test_admin_api.py
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(load_admin):
    adm = load_admin()
    app = FastAPI()
    app.include_router(adm.qk_router, prefix="/api/v1/quota-keeper")
    return app, adm


def test_routes_single_prefix(load_admin):
    # Router paths are relative ("/config", ...); the prefix is applied once
    # at mount, so requests land on /api/v1/quota-keeper/* -- not the
    # double-mounted /api/v1/quota-keeper/quota-keeper/*.
    app, _ = _app(load_admin)
    paths = {r.path for r in app.routes}
    assert "/api/v1/quota-keeper/config" in paths
    assert "/api/v1/quota-keeper/quota-keeper/config" not in paths


def test_admin_endpoints_unauthenticated_401(load_admin):
    # Auth failure must surface as an HTTPException (-> FastAPI 401), not as a
    # JSONResponse dependency value that silently lets the handler run.
    app, _ = _app(load_admin)
    c = TestClient(app)
    assert c.get("/api/v1/quota-keeper/ledger").status_code == 401
    assert c.post("/api/v1/quota-keeper/config", json={}).status_code == 401


def test_page_placeholder_substituted_at_mount(load_admin):
    # The placeholder lives in the raw page and is replaced by qk_build_page
    # at mount time, so the built HTML points at the real API base.
    adm = load_admin()
    assert "__QK_API_PREFIX__" in adm.QK_PAGE
    html = adm.qk_build_page("/api/v1/quota-keeper")
    assert "__QK_API_PREFIX__" not in html
    assert "'/api/v1/quota-keeper'+path" in html


def _mount(app, adm):
    ev = adm.Event()
    ev.valves.enable_background_pricing_refresh = False
    asyncio.run(ev.event({}, __event_name__="system.startup.completed", __app__=app))


def test_event_mount_idempotent(load_admin):
    # Hot-reload creates a second Event instance; it must not re-mount the
    # router or the page route on the same app.
    adm = load_admin()
    app = FastAPI()
    _mount(app, adm)
    _mount(app, adm)
    paths = [getattr(r, "path", None) for r in app.routes]
    # /config and /pricing are GET+POST pairs; /ledger is a unique path, so a
    # single occurrence proves the router was not double-mounted.
    assert paths.count("/api/v1/quota-keeper/ledger") == 1
    assert paths.count("/quota") == 1


def test_page_route_requires_admin(load_admin):
    # The page route itself is guarded by Depends(_require_admin).
    adm = load_admin()
    app = FastAPI()
    _mount(app, adm)
    c = TestClient(app)
    assert c.get("/quota").status_code == 401
