# tests/test_endpoints.py
"""Task 6: recent.json ring buffer + /recent + /stats + /me endpoints.

Tests are the contract for the admin aggregation logic (the brief's qk_stats
sketch has a known-wrong per-user `models` line; these tests pin the fix).

Task 7: pricing-loop robustness tests appended at the bottom.
"""
import asyncio
import sys
import types
from datetime import date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import _stub_webui_auth


def _app(load_admin):
    adm = load_admin()
    app = FastAPI()
    app.include_router(adm.qk_router, prefix="/api/v1/quota-keeper")
    return TestClient(app), adm


def _stub_self_user(monkeypatch, uid="u1", role="user"):
    """Stub open_webui.utils.auth with the real dependency-style signatures
    (current OWUI: get_current_user(request, response=, background_tasks=,
    auth_token=) -> user; get_verified_user(user) -> user, sync). Mirrors
    conftest._stub_webui_auth, but the self-service /me route must work for
    non-admins too."""
    ow = types.ModuleType("open_webui")
    utils = types.ModuleType("open_webui.utils")
    auth = types.ModuleType("open_webui.utils.auth")

    # type() avoids the class-body scoping trap: a name assigned in a class
    # body does not see the enclosing function's local of the same name
    U = type("U", (), {"id": uid, "name": "U", "email": f"{uid}@x.com",
                       "role": role, "group_ids": []})

    async def get_current_user(request, **kw):
        return U()

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


# ---- recent.json ring buffer -------------------------------------------------


def test_recent_ring_buffer(qk):
    # 205 records -> file keeps exactly the newest 200 (newest-last order)
    for i in range(205):
        qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, f"m/{i}",
                           {"cached": 1, "input": 2, "output": 3, "cache_write": 0})
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {})
    items = rec["items"]
    assert len(items) == 200
    assert items[0]["model"] == "m/5"      # oldest 5 dropped
    assert items[-1]["model"] == "m/204"   # newest kept
    it = items[-1]
    assert set(("ts", "user_id", "name", "email", "model", "tokens",
                "cost_usd", "tou_tier", "priced")) <= set(it)
    assert it["user_id"] == "u1"
    assert it["tokens"] == {"cached": 1.0, "input": 2.0, "output": 3.0}


def test_recent_endpoint_newest_first(qk, load_admin, monkeypatch):
    _stub_webui_auth(monkeypatch)
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, "m/a",
                       {"cached": 1, "input": 2, "output": 3, "cache_write": 0})
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/b",
                       {"cached": 0, "input": 5, "output": 5, "cache_write": 0})
    c, _ = _app(load_admin)
    r = c.get("/api/v1/quota-keeper/recent")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert items[0]["model"] == "m/b"      # newest first
    assert items[0]["user_id"] == "u2"
    assert items[1]["model"] == "m/a"
    it = items[0]
    assert it["tokens"] == {"cached": 0.0, "input": 5.0, "output": 5.0}
    assert it["priced"] is False           # no pricing table configured
    assert it["tou_tier"] == "off"


# ---- /me ----------------------------------------------------------------------


def test_me_returns_own_data(qk, load_admin, monkeypatch):
    _stub_self_user(monkeypatch, uid="u1")
    c, adm = _app(load_admin)
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x.com"}, "m/x",
                       {"cached": 10, "input": 90, "output": 50, "cache_write": 0},
                       now=datetime(2026, 8, 17, 12, 0))
    # deterministic "now" inside the admin module (also feeds the time
    # multiplier and the trend window)
    monkeypatch.setattr(adm, "qk_local_now", lambda cfg: datetime(2026, 8, 17, 12, 0))

    r = c.get("/api/v1/quota-keeper/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["id"] == "u1"
    assert "quota" in body and "used_credits" in body
    # default config: no quota anywhere -> none, unlimited
    assert body["quota"] is None
    assert body["quota_source"] == "none"
    assert body["effective_quota"] is None
    assert body["multiplier"] == 1.0
    # one metered request: cost (10*1.0 + 90*1.0 + 50*2.0)/1e6 USD
    assert abs(body["today"]["cost_usd"] - 1.9e-4) < 1e-9
    assert body["today"]["requests"] == 1
    # daily period -> day cost * credits_per_usd (1000)
    assert abs(body["used_credits"] - 0.19) < 1e-9
    assert len(body["trend"]) == 7
    assert body["trend"][-1]["day"] == "2026-08-17"
    assert body["trend"][-1]["requests"] == 1
    # UI contract field; None until the page wires a per-user model list
    assert body["tou"]["current_tier"] is None


def test_me_requires_auth(load_admin):
    c, _ = _app(load_admin)
    assert c.get("/api/v1/quota-keeper/me").status_code == 401


# ---- /stats -------------------------------------------------------------------


def test_stats_aggregates(qk, load_admin, monkeypatch):
    # Pin the recording clock (same style as tests/test_metering.py) so the
    # day-bucket assertion cannot flake when the suite runs across midnight.
    fixed = datetime(2026, 8, 17, 12, 0)
    monkeypatch.setattr(qk, "qk_local_now", lambda cfg: fixed)
    monkeypatch.setattr(qk, "qk_tou_local_now", lambda cfg: fixed)
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, "m/x",
                       {"cached": 10, "input": 90, "output": 50, "cache_write": 0})
    adm = load_admin()
    out = adm.qk_stats(from_=None, to=None, user=None, model=None, granularity="day")
    assert out["kpi"]["requests"] == 1
    assert abs(out["kpi"]["cache_rate"] - 0.1) < 1e-9
    assert out["kpi"]["tokens"] == {"cached": 10.0, "input": 90.0, "output": 50.0}
    assert abs(out["kpi"]["cost_usd"] - 1.9e-4) < 1e-9
    assert out["models"][0]["model"] == "m/x"
    assert out["models"][0]["users"] == 1
    assert out["users"][0]["user_id"] == "u1"
    assert out["users"][0]["quota_source"] in ("user", "group", "default", "none")
    assert out["users"][0]["multiplier"] == 1.0
    # recorded under the pinned clock, so the day bucket is deterministic
    assert out["series"] == [{"bucket": "2026-08-17", "by_model": {"m/x": 1.9e-4}}]


def test_stats_per_user_models_count_only_own_models(qk, load_admin):
    # Regression for the brief's known-wrong sketch line: per-user `models`
    # must count models seen for THAT user, not the global model set.
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0},
                                                      "m/y": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U1", "email": "u1@x"}, "m/x",
                       {"cached": 10, "input": 90, "output": 50, "cache_write": 0})
    qk.qk_record_usage({"id": "u2", "name": "U2", "email": "u2@x"}, "m/y",
                       {"cached": 0, "input": 10, "output": 10, "cache_write": 0})
    adm = load_admin()
    out = adm.qk_stats(from_=None, to=None, user=None, model=None, granularity="day")
    rows = {r["user_id"]: r for r in out["users"]}
    assert rows["u1"]["models"] == 1
    assert rows["u2"]["models"] == 1
    assert len(out["models"]) == 2
    assert out["kpi"]["requests"] == 2
    assert {m["model"] for m in out["models"]} == {"m/x", "m/y"}


def test_stats_hour_granularity_buckets(qk, load_admin):
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    tok = {"cached": 10, "input": 90, "output": 50, "cache_write": 0}
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, "m/x", tok,
                       now=datetime(2026, 8, 17, 9, 30))
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, "m/x", tok,
                       now=datetime(2026, 8, 17, 14, 0))
    adm = load_admin()
    out = adm.qk_stats(from_=None, to=None, user=None, model=None, granularity="hour")
    assert out["series"] == [
        {"bucket": "2026-08-17T09", "by_model": {"_": 1.9e-4}},
        {"bucket": "2026-08-17T14", "by_model": {"_": 1.9e-4}},
    ]
    # day granularity: per-model cost under the day bucket
    out = adm.qk_stats(from_=None, to=None, user=None, model=None, granularity="day")
    assert out["series"] == [
        {"bucket": "2026-08-17", "by_model": {"m/x": 3.8e-4}},
    ]


def test_stats_filters(qk, load_admin):
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "Alice", "email": "a@x"}, "m/x",
                       {"cached": 0, "input": 10, "output": 10, "cache_write": 0},
                       now=datetime(2026, 8, 15, 12, 0))
    qk.qk_record_usage({"id": "u1", "name": "Alice", "email": "a@x"}, "m/other",
                       {"cached": 0, "input": 10, "output": 10, "cache_write": 0},
                       now=datetime(2026, 8, 16, 12, 0))
    adm = load_admin()
    out = adm.qk_stats(model="m/x")
    assert [m["model"] for m in out["models"]] == ["m/x"]
    assert out["kpi"]["requests"] == 1
    # regression: model-filtered day series must match kpi cost (was 2x before)
    s = out["series"][0]["by_model"]["m/x"]
    assert abs(s - out["kpi"]["cost_usd"]) < 1e-12
    out = adm.qk_stats(from_="2026-08-15", to="2026-08-15")
    assert out["kpi"]["requests"] == 1
    out = adm.qk_stats(user="Alice")       # matches by name
    assert out["kpi"]["requests"] == 2
    out = adm.qk_stats(user="nobody")
    assert out["users"] == [] and out["kpi"]["requests"] == 0


def test_stats_endpoint_via_http(qk, load_admin, monkeypatch):
    _stub_webui_auth(monkeypatch)
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, "m/x",
                       {"cached": 10, "input": 90, "output": 50, "cache_write": 0})
    c, _ = _app(load_admin)
    r = c.get("/api/v1/quota-keeper/stats?granularity=hour")
    assert r.status_code == 200
    body = r.json()
    assert body["kpi"]["requests"] == 1
    assert isinstance(body["series"], list)
    assert isinstance(body["users"], list) and isinstance(body["models"], list)


def test_admin_read_endpoints_require_admin(load_admin, monkeypatch):
    c, _ = _app(load_admin)
    assert c.get("/api/v1/quota-keeper/recent").status_code == 401
    assert c.get("/api/v1/quota-keeper/stats").status_code == 401
    _stub_webui_auth(monkeypatch)
    assert c.get("/api/v1/quota-keeper/recent").status_code == 200
    assert c.get("/api/v1/quota-keeper/stats").status_code == 200


def test_stats_forbidden_for_plain_user(load_admin, monkeypatch):
    _stub_self_user(monkeypatch, role="user")
    c, _ = _app(load_admin)
    assert c.get("/api/v1/quota-keeper/stats").status_code == 403
    assert c.get("/api/v1/quota-keeper/recent").status_code == 403


# ---- /pricing summary ----------------------------------------------------------


def test_pricing_summary_vs_full(admin_client):
    c, adm = admin_client()
    adm.qk_atomic_write(adm.QK_PRICING_PATH, {
        "url": "http://x", "fetched_at": 123, "fetched_at_iso": "2026-08-17T00:00:00+00:00",
        "models": 1, "table": {"m/x": {"input": 1.0}},
    })
    r = c.get("/api/v1/quota-keeper/pricing")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"url", "fetched_at_iso", "models"}
    assert "table" not in body and "fetched_at" not in body
    r = c.get("/api/v1/quota-keeper/pricing?full=1")
    assert r.status_code == 200
    body = r.json()
    assert body["table"] == {"m/x": {"input": 1.0}}
    assert body["fetched_at"] == 123


# ---- Task 7: pricing loop robustness -----------------------------------------


def _fake_app():
    """Minimal stand-in for Open WebUI's FastAPI app: a route list behind
    .router.routes, the include_router call event() makes, and .state for
    the pricing task."""
    class App:
        def __init__(self):
            self.router = types.SimpleNamespace(routes=[])
            self.state = types.SimpleNamespace()

        def include_router(self, router, **k):
            self.router.routes.extend(getattr(router, "routes", []))

    return App()


def test_event_mounts_cleanly_with_background_refresh_off(load_admin):
    # Task 7 refactor guard: with background refresh disabled, mounting must
    # stay independent of the pricing-loop changes (Task 2 baseline behavior).
    adm = load_admin()
    ev = adm.Event()
    ev.valves.enable_background_pricing_refresh = False
    asyncio.run(ev.event(
        {}, __event_name__="system.startup.completed", __id__="f1",
        __app__=_fake_app()))
    assert ev._installed is True


def test_pricing_loop_task_created_with_strong_ref(load_admin, monkeypatch):
    # The pricing loop must be launched via asyncio.create_task and the task
    # kept on the Event instance (strong reference) -- otherwise a
    # garbage-collected task never runs. The real _pricing_loop is swapped for
    # a flag-setting fake; the assertion that the fake ran proves the task was
    # actually scheduled.
    adm = load_admin()
    ran = []

    async def fake_loop():
        ran.append(True)

    monkeypatch.setattr(adm, "_pricing_loop", fake_loop)
    ev = adm.Event()
    ev.valves.enable_background_pricing_refresh = True
    asyncio.run(ev.event(
        {}, __event_name__="system.startup.completed", __id__="f1",
        __app__=_fake_app()))
    assert ev._pricing_task is not None
    assert ran == [True]
    assert ev._pricing_task.done()  # fake loop completed inside asyncio.run


# ---- SPA catch-all shadowing (v0.2.1 root-cause fix) -------------------------


def _spa_shell_app():
    """FastAPI app emulating OWUI's route table: a catch-all SPA mount named
    'spa-static-files' at '/', registered at 'import time' -- i.e. BEFORE any
    plugin route. Anything reaching it gets the SPA HTML shell, which is what
    rendered the client-side 404 for /quota before the splice fix."""
    from starlette.responses import HTMLResponse

    async def spa_asgi(scope, receive, send):
        assert scope["type"] == "http"
        await HTMLResponse("<!doctype html>spa-shell")(scope, receive, send)

    app = FastAPI()
    app.mount("/", spa_asgi, name="spa-static-files")
    return app


def _mount_via_event(adm, app, name="system.startup.completed"):
    ev = adm.Event()
    ev.valves.enable_background_pricing_refresh = False
    asyncio.run(ev.event({}, __event_name__=name, __id__="f1", __app__=app))
    return ev


def test_routes_spliced_ahead_of_spa_catchall(load_admin):
    """Regression: with OWUI's SPA mount present, /quota and the API must hit
    OUR handlers (401 without auth -- open_webui is absent in tests), never
    the SPA's 200 HTML shell. An unknown path must still fall through."""
    adm = load_admin()
    client = TestClient(_spa_shell_app())
    ev = _mount_via_event(adm, client.app)

    assert ev._installed is True
    assert client.get("/quota").status_code == 401
    assert client.get("/api/v1/quota-keeper/me").status_code == 401
    assert client.get("/api/v1/quota-keeper/config").status_code == 401
    # POST must reach our router too (the SPA mount only allows GET/HEAD and
    # would answer 405 itself).
    assert client.post("/api/v1/quota-keeper/config", json={}).status_code != 405
    r = client.get("/definitely-not-a-plugin-path")
    assert r.status_code == 200 and "spa-shell" in r.text


def test_late_init_remount_drops_stale_routes(load_admin):
    """Hot code update: a fresh Event instance remounts via the late-init net
    (any event name); stale routes are dropped, so the route table does not
    grow and ordering ahead of the SPA mount is preserved."""
    adm = load_admin()
    client = TestClient(_spa_shell_app())
    ev1 = _mount_via_event(adm, client.app)
    n1 = len(client.app.router.routes)

    ev2 = _mount_via_event(adm, client.app, name="chat.created")  # any event
    n2 = len(client.app.router.routes)

    assert ev2._installed is True
    assert n2 == n1  # stale dropped before re-add: no duplicates
    assert client.get("/quota").status_code == 401

    # An installed instance ignores unrelated events (no re-mount churn).
    ev2.event  # bound method exists
    n_before = len(client.app.router.routes)
    asyncio.run(ev2.event({}, __event_name__="chat.created", __id__="f1",
                          __app__=client.app))
    assert len(client.app.router.routes) == n_before


# ---- auth chain compatibility (v0.2.2 root-cause fix) ------------------------


def test_auth_chain_uses_dependency_style_signatures(load_admin, monkeypatch):
    """Regression: current OWUI's get_verified_user takes the resolved *user*
    (dependency-style); calling it with the request 401'd everything. The
    stubs in conftest/_stub_self_user now mirror the real signatures and
    assert the contract, so /me working here proves the chain is driven
    correctly end to end."""
    _stub_self_user(monkeypatch, uid="u1", role="user")
    c, adm = _app(load_admin)
    r = c.get("/api/v1/quota-keeper/me")
    assert r.status_code == 200
    assert r.json()["user"]["id"] == "u1"


def test_unauthenticated_requests_401(load_admin, monkeypatch):
    """get_current_user raising HTTPException(401) (no/invalid token) must
    surface as 401 -- not be masked into a different error."""
    from fastapi import HTTPException

    ow = types.ModuleType("open_webui")
    utils = types.ModuleType("open_webui.utils")
    auth = types.ModuleType("open_webui.utils.auth")

    async def get_current_user(request, **kw):
        raise HTTPException(status_code=401, detail="Invalid token")

    def get_verified_user(user):
        return user

    auth.get_current_user = get_current_user
    auth.get_verified_user = get_verified_user
    utils.auth = auth
    ow.utils = utils
    monkeypatch.setitem(sys.modules, "open_webui", ow)
    monkeypatch.setitem(sys.modules, "open_webui.utils", utils)
    monkeypatch.setitem(sys.modules, "open_webui.utils.auth", auth)

    c, adm = _app(load_admin)
    assert c.get("/api/v1/quota-keeper/me").status_code == 401
    assert c.get("/api/v1/quota-keeper/me").json()["detail"] == "Invalid token"
    assert c.get("/api/v1/quota-keeper/config").status_code == 401


# ---- OWUI async-models drift (v0.2.3 root-cause fixes) -----------------------


def _stub_webui_models(monkeypatch, *, users=None, groups=None, memberships=None):
    """Stub open_webui.models.{users,groups} with the CURRENT async shapes:
    Users.get_users() -> coroutine of {"users": [...], "total": n};
    Groups.get_groups(filter) -> coroutine of responses WITHOUT user_ids;
    Groups.get_group_user_ids_by_ids(ids) -> coroutine of {gid: [uid]}.
    """
    models = types.ModuleType("open_webui.models")
    musers = types.ModuleType("open_webui.models.users")
    mgroups = types.ModuleType("open_webui.models.groups")

    if users is not None:
        class Users:
            @staticmethod
            async def get_users(*a, **kw):
                return {"users": users, "total": len(users)}
        musers.Users = Users

    if groups is not None:
        class Groups:
            @staticmethod
            async def get_groups(filter, **kw):  # filter is REQUIRED
                return groups

            @staticmethod
            async def get_group_user_ids_by_ids(ids, **kw):
                return {g: list((memberships or {}).get(g, [])) for g in ids}

            @staticmethod
            async def get_groups_by_member_id(uid, **kw):
                return [g for g in groups if uid in (memberships or {}).get(g.id, [])]
        mgroups.Groups = Groups

    ow = types.ModuleType("open_webui")
    ow.models = models
    models.users = musers
    models.groups = mgroups
    monkeypatch.setitem(sys.modules, "open_webui", ow)
    monkeypatch.setitem(sys.modules, "open_webui.models", models)
    monkeypatch.setitem(sys.modules, "open_webui.models.users", musers)
    monkeypatch.setitem(sys.modules, "open_webui.models.groups", mgroups)


def test_users_table_async_paginated_shape(load_admin, monkeypatch):
    # Current OWUI: get_users is async and returns a paginated dict, not a
    # list (v0.2.2 iterated the coroutine -> warning + empty table).
    _stub_webui_models(monkeypatch, users=[
        types.SimpleNamespace(id="u1", name="A", email="a@x", role="user")
    ])
    adm = load_admin()
    rows = asyncio.run(adm._users_table())
    assert rows == [{"id": "u1", "name": "A", "email": "a@x", "role": "user"}]


def test_groups_table_async_bulk_member_fill(load_admin, monkeypatch):
    # Current GroupResponse has member_count but no user_ids; member ids come
    # from the bulk get_group_user_ids_by_ids query.
    g1 = types.SimpleNamespace(id="g1", name="Team", member_count=2)
    _stub_webui_models(monkeypatch, groups=[g1], memberships={"g1": ["u1", "u2"]})
    adm = load_admin()
    rows = asyncio.run(adm._groups_table())
    assert rows == [{"id": "g1", "name": "Team", "members": ["u1", "u2"]}]


def test_group_ids_async_resolves_and_caches(qk, monkeypatch):
    calls = []
    g1 = types.SimpleNamespace(id="g1", name="Team")

    models = types.ModuleType("open_webui.models")
    mgroups = types.ModuleType("open_webui.models.groups")

    class Groups:
        @staticmethod
        async def get_groups_by_member_id(uid, **kw):
            calls.append(uid)
            return [g1] if uid == "u1" else []
    mgroups.Groups = Groups
    ow = types.ModuleType("open_webui")
    ow.models = models
    models.groups = mgroups
    monkeypatch.setitem(sys.modules, "open_webui", ow)
    monkeypatch.setitem(sys.modules, "open_webui.models", models)
    monkeypatch.setitem(sys.modules, "open_webui.models.groups", mgroups)

    qk._GROUP_IDS_CACHE.clear()
    ids1 = asyncio.run(qk.qk_user_group_ids_async({"id": "u1"}))
    ids2 = asyncio.run(qk.qk_user_group_ids_async({"id": "u1"}))
    assert ids1 == ["g1"] and ids2 == ["g1"]
    assert len(calls) == 1  # second resolution served from the 5-min cache


def test_resolve_quota_with_preresolved_group_ids(qk):
    cfg = {"group_quotas": {"g1": 500, "g2": 900},
           "user_quotas": {}, "default_quota_credits": 100}
    assert qk.qk_resolve_quota(cfg, {"id": "u1"}, ["g1", "g2"]) == (900.0, "group")
    assert qk.qk_resolve_quota(cfg, {"id": "u1"}, []) == (100.0, "default")
    # group_ids=None keeps the legacy sync path (no OWUI here -> no groups)
    assert qk.qk_resolve_quota(cfg, {"id": "u1"}, None) == (100.0, "default")


def test_stats_quota_uses_group_ids_map(load_admin):
    from pathlib import Path
    from tests.conftest import write_json

    adm = load_admin()
    write_json(Path(adm.QK_CONFIG_PATH), {"group_quotas": {"g1": 777}})
    write_json(Path(adm.QK_LEDGER_PATH),
               {"users": {"u1": {"name": "A", "email": "a@x", "days": {}}}})
    out = adm.qk_stats(group_ids_map={"u1": ["g1"]})
    row = next(r for r in out["users"] if r["user_id"] == "u1")
    assert row["quota"] == 777.0 and row["quota_source"] == "group"


# ---- rolling 24h window (v0.3.1) ----------------------------------------------


def test_stats_rolling_window(load_admin, monkeypatch):
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path
    from tests.conftest import write_json

    adm = load_admin()
    write_json(Path(adm.QK_LEDGER_PATH), {"users": {"u1": {"name": "A", "email": "a@x", "days": {
        "2026-08-18": {
            "requests": 3, "cost_usd": 3.0,
            "tokens": {"cached": 0.0, "input": 30.0, "output": 0.0},
            "hours": {
                "8": {"requests": 1, "cost_usd": 1.0, "tokens": {"cached": 0.0, "input": 10.0, "output": 0.0}},
                "15": {"requests": 2, "cost_usd": 2.0, "tokens": {"cached": 0.0, "input": 20.0, "output": 0.0}},
            },
            "models": {"m/x": {"requests": 3, "cost_usd": 3.0, "unpriced_requests": 0,
                                "tokens": {"cached": 0.0, "input": 30.0, "output": 0.0}}},
        },
        "2026-08-19": {
            "requests": 5, "cost_usd": 5.0,
            "tokens": {"cached": 0.0, "input": 50.0, "output": 0.0},
            "hours": {
                "9": {"requests": 5, "cost_usd": 5.0, "tokens": {"cached": 0.0, "input": 50.0, "output": 0.0}},
            },
            "models": {"m/x": {"requests": 5, "cost_usd": 5.0, "unpriced_requests": 0,
                                "tokens": {"cached": 0.0, "input": 50.0, "output": 0.0}}},
        },
    }}}})
    # pin "now" to 2026-08-19 12:00 UTC; window starts 2026-08-18 12:00 -> the
    # 8:00 bucket of yesterday (1 req / $1) must be excluded, everything else kept
    monkeypatch.setattr(adm, "qk_local_now", lambda cfg: _dt(2026, 8, 19, 12, 0, tzinfo=_tz.utc))
    ws = _dt(2026, 8, 18, 12, 0, tzinfo=_tz.utc).timestamp()

    out = adm.qk_stats(from_="2026-08-18", to="2026-08-19",
                       granularity="hour", window_start_ts=ws)
    assert out["kpi"]["requests"] == 7  # 2 (yesterday 15:00) + 5 (today 9:00)
    assert abs(out["kpi"]["cost_usd"] - 7.0) < 1e-9
    buckets = [s["bucket"] for s in out["series"]]
    assert buckets == ["2026-08-18T15", "2026-08-19T09"]
    row = out["users"][0]
    assert row["requests"] == 7

    # without the window, the same range reports the full two days
    out2 = adm.qk_stats(from_="2026-08-18", to="2026-08-19", granularity="hour")
    assert out2["kpi"]["requests"] == 8
