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
    """Stub open_webui.utils.auth.get_verified_user to a plain (non-admin)
    user. Mirrors conftest._stub_webui_auth, but the self-service /me route
    must work for non-admins too."""
    ow = types.ModuleType("open_webui")
    utils = types.ModuleType("open_webui.utils")
    auth = types.ModuleType("open_webui.utils.auth")

    # type() avoids the class-body scoping trap: a name assigned in a class
    # body does not see the enclosing function's local of the same name
    U = type("U", (), {"id": uid, "name": "U", "email": f"{uid}@x.com",
                       "role": role, "group_ids": []})

    async def get_verified_user(request):
        return U()

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
    """Minimal stand-in for Open WebUI's FastAPI app: records nothing, but
    accepts the include_router/get calls event() makes."""
    class App:
        routes = []
        def include_router(self, *a, **k):
            pass
        def get(self, *a, **k):
            def deco(fn):
                return fn
            return deco
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
