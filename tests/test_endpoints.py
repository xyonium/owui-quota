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


def test_recent_mine_filters_and_strips(qk, load_admin, monkeypatch):
    _stub_self_user(monkeypatch, uid="u1", role="user")
    for i in range(3):
        qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, f"m/{i}",
                           {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/other",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    c, _ = _app(load_admin)
    r = c.get("/api/v1/quota-keeper/recent?mine=1")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    assert all(it["user_id"] == "u1" for it in items)
    assert all("email" not in it for it in items)


def test_recent_mine_caps_at_50(qk, load_admin, monkeypatch):
    _stub_self_user(monkeypatch, uid="u1", role="user")
    for i in range(60):
        qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, f"m/{i}",
                           {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    c, _ = _app(load_admin)
    items = c.get("/api/v1/quota-keeper/recent?mine=1").json()["items"]
    assert len(items) == 50
    assert items[0]["model"] == "m/59"   # newest first preserved


def test_models_mine_own_models_with_prices(qk, load_admin, monkeypatch):
    _stub_self_user(monkeypatch, uid="u1", role="user")
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/x",
                       {"cached": 0, "input": 10, "output": 5, "cache_write": 0})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/unpriced",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/theirs",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    c, _ = _app(load_admin)
    r = c.get("/api/v1/quota-keeper/models?mine=1")
    assert r.status_code == 200
    items = {it["model"]: it for it in r.json()["items"]}
    # v0.5.23: mine=1 filters to the caller's OWN used models
    assert set(items) == {"m/x", "m/unpriced"}       # u2's m/theirs absent
    assert items["m/x"]["matched"] is True
    assert items["m/x"]["price"]["input"] == 1.0
    assert items["m/unpriced"]["matched"] is False
    assert items["m/unpriced"]["price"] is None
    assert items["m/x"]["requests"] == 1             # own aggregate only


def test_models_admin_unchanged(qk, load_admin, monkeypatch):
    # admin without ?mine sees all users' models (existing behavior)
    _stub_webui_auth(monkeypatch)
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/x",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/y",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    c, _ = _app(load_admin)
    items = c.get("/api/v1/quota-keeper/models").json()["items"]
    assert {it["model"] for it in items} == {"m/x", "m/y"}


def test_models_nonadmin_without_mine_sees_pool(qk, load_admin, monkeypatch):
    # v0.5.23: a plain user WITHOUT ?mine sees the local model pool (all
    # users' used models) — the price list has reference value; mine=1 still
    # narrows to their own
    _stub_self_user(monkeypatch, uid="u1")
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/x",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/y",
                       {"cached": 0, "input": 1, "output": 1, "cache_write": 0})
    c, _ = _app(load_admin)
    items = c.get("/api/v1/quota-keeper/models").json()["items"]
    assert {it["model"] for it in items} == {"m/x", "m/y"}
    # v0.5.31: browsing the shared pool must NOT leak other users' aggregate
    # usage/cost or the admin's override config -- a non-admin gets only the
    # price-reference fields (model/used/matched/how/price).
    for it in items:
        assert "cost_usd" not in it and "requests" not in it and "override" not in it
        assert {"model", "used", "matched", "how", "price"} <= set(it)


def test_me_usage_own_data_only(qk, load_admin, monkeypatch):
    _stub_self_user(monkeypatch, uid="u1")
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"m/x": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/x",
                       {"cached": 10, "input": 90, "output": 50, "cache_write": 0},
                       now=datetime(2026, 8, 20, 12, 0), channel="webui")
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u1@x"}, "m/y",
                       {"cached": 0, "input": 10, "output": 10, "cache_write": 0},
                       now=datetime(2026, 8, 21, 9, 0), channel="api")
    qk.qk_record_usage({"id": "u2", "name": "V", "email": "v@x"}, "m/z",
                       {"cached": 0, "input": 999, "output": 999, "cache_write": 0},
                       now=datetime(2026, 8, 21, 9, 0), channel="api")
    c, adm = _app(load_admin)
    monkeypatch.setattr(adm, "qk_local_now", lambda cfg: datetime(2026, 8, 21, 12, 0))

    r = c.get("/api/v1/quota-keeper/me/usage?span=7d")
    assert r.status_code == 200
    body = r.json()
    assert body["span"] == "7d"
    assert len(body["trend"]) == 7
    assert body["trend"][-1]["day"] == "2026-08-21"
    assert body["trend"][-1]["requests"] == 1          # only u1, not u2
    assert body["channels"] == {"webui": 1, "api": 1}  # u2's api not counted
    models = {m["model"]: m for m in body["models"]}
    assert set(models) == {"m/x", "m/y"}               # u2's m/z absent
    assert models["m/x"]["tokens"]["cached"] == 10.0
    # cost desc: m/x (0.00028) before m/y (0.00004)
    assert [m["model"] for m in body["models"]] == ["m/x", "m/y"]

    r = c.get("/api/v1/quota-keeper/me/usage?span=30d")
    assert len(r.json()["trend"]) == 30


def test_me_usage_requires_auth(load_admin):
    c, _ = _app(load_admin)
    assert c.get("/api/v1/quota-keeper/me/usage").status_code == 401


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
    assert out["series"] == [{"bucket": "2026-08-17", "cost": {"m/x": 1.9e-4},
                              "requests": 1, "tokens": 150.0}]
    assert out["kpi"]["channels"] == {"webui": 0, "api": 1}  # record_usage default channel=api


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
        {"bucket": "2026-08-17T09", "cost": {"_": 1.9e-4}, "requests": 1, "tokens": 150.0},
        {"bucket": "2026-08-17T14", "cost": {"_": 1.9e-4}, "requests": 1, "tokens": 150.0},
    ]
    # day granularity: per-model cost under the day bucket
    out = adm.qk_stats(from_=None, to=None, user=None, model=None, granularity="day")
    assert out["series"] == [
        {"bucket": "2026-08-17", "cost": {"m/x": 3.8e-4}, "requests": 2, "tokens": 300.0},
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
    s = out["series"][0]["cost"]["m/x"]
    assert abs(s - out["kpi"]["cost_usd"]) < 1e-12
    out = adm.qk_stats(from_="2026-08-15", to="2026-08-15")
    assert out["kpi"]["requests"] == 1
    out = adm.qk_stats(user="Alice")       # matches by name
    assert out["kpi"]["requests"] == 2
    out = adm.qk_stats(user="nobody")
    assert out["users"] == [] and out["kpi"]["requests"] == 0


def test_stats_kpi_channels_follow_model_filter(qk, load_admin):
    # model filter scopes kpi.channels to that model's contribution; unfiltered
    # aggregates the whole span (both record_usage calls default channel=api)
    qk.qk_record_usage({"id": "u1", "name": "A", "email": "a@x"}, "m/x",
                       {"cached": 0, "input": 10, "output": 10, "cache_write": 0})
    qk.qk_record_usage({"id": "u1", "name": "A", "email": "a@x"}, "m/other",
                       {"cached": 0, "input": 10, "output": 10, "cache_write": 0})
    adm = load_admin()
    out = adm.qk_stats(model="m/x")
    assert out["kpi"]["channels"] == {"webui": 0, "api": 1}
    out = adm.qk_stats()
    assert out["kpi"]["channels"] == {"webui": 0, "api": 2}


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


def test_stats_self_service_for_plain_user(load_admin, monkeypatch):
    # v0.5.24: /stats is self-service — a plain user gets 200 with their OWN
    # usage only (server-enforced), never another user's
    _stub_self_user(monkeypatch, role="user", uid="u1")
    from pathlib import Path
    from tests.conftest import write_json
    write_json(Path(load_admin().QK_LEDGER_PATH), {"users": {
        "u1": {"name": "U", "email": "u1@x", "days": {
            "2026-08-21": {"requests": 2, "cost_usd": 0.01, "tokens": {"cached": 0, "input": 2, "output": 1}, "models": {"m/x": {"requests": 2}}},
        }},
        "u2": {"name": "V", "email": "v@x", "days": {
            "2026-08-21": {"requests": 99, "cost_usd": 9.9, "tokens": {"cached": 0, "input": 99, "output": 99}, "models": {"m/y": {"requests": 99}}},
        }},
    }})
    c, _ = _app(load_admin)
    r = c.get("/api/v1/quota-keeper/stats?from=2026-08-21&to=2026-08-21")
    assert r.status_code == 200
    assert r.json()["kpi"]["requests"] == 2          # own only
    assert [u["user_id"] for u in r.json()["users"]] == ["u1"]
    # /recent is self-service too
    assert c.get("/api/v1/quota-keeper/recent").status_code == 200


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


def test_page_is_login_gated_not_admin_gated(load_admin, monkeypatch):
    """Role split: a non-admin must REACH the /quota page (it renders the
    personal card from /me), while every admin API stays 403 for them.

    Regression: the page used to hang Depends(_require_admin), so non-admins
    got 403 on the HTML itself and the personal-card code never ran."""
    _stub_self_user(monkeypatch, uid="u1", role="user")
    adm = load_admin()
    client = TestClient(_spa_shell_app())
    _mount_via_event(adm, client.app)

    page = client.get("/quota")
    assert page.status_code == 200, "non-admin must be able to load the page"
    assert "renderPersonalHeader" in page.text  # the role-split branch ships in the SPA
    assert client.get("/api/v1/quota-keeper/me").status_code == 200
    # v0.5.24: the shared dashboard needs /config and /stats — they are now
    # self-service (user-scoped server-side); the rest stay admin-only.
    for path in ("/users", "/groups", "/ledger", "/pricing"):
        r = client.get(f"/api/v1/quota-keeper{path}")
        assert r.status_code == 403, f"{path} must stay admin-only (got {r.status_code})"
    # self-service endpoints reachable but scoped to the caller
    assert client.get("/api/v1/quota-keeper/recent?mine=1").status_code == 200
    assert client.get("/api/v1/quota-keeper/config").status_code == 200
    assert client.get("/api/v1/quota-keeper/stats").status_code == 200
    assert client.get("/api/v1/quota-keeper/models?mine=1").status_code == 200
    assert client.get("/api/v1/quota-keeper/me/usage").status_code == 200


def test_reprice_bar_hidden_from_nonadmin(load_admin, monkeypatch):
    """v0.5.28 regression: the reprice/backfill bar must actually be hidden for
    non-admins. It carries .admin-only, but .admin-only{display:none} was defined
    BEFORE .filters{display:flex} in the stylesheet, so at equal specificity the
    later flex rule won and the bar stayed visible. Pin both the markup contract
    (the bar has .admin-only) and the CSS ordering (a reinforcing high-specificity
    !important rule appears AFTER the .filters rule)."""
    _stub_self_user(monkeypatch, uid="u1", role="user")
    adm = load_admin()
    client = TestClient(_spa_shell_app())
    _mount_via_event(adm, client.app)
    page = client.get("/quota")
    assert page.status_code == 200
    text = page.text
    # the bar is marked admin-only (init removes the class only for admins)
    assert 'id="repriceBar"' in text and "filters admin-only" in text
    # the reinforcing rule exists and is placed AFTER .filters{display:flex}
    flex = text.index(".filters{display:flex")
    reinforce = text.index(".filters.admin-only")
    assert reinforce > flex, "admin-only reinforcement must come after .filters{flex}"
    assert "!important" in text[reinforce:reinforce + 200]


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


# ---- rolling 24h window (v0.3.1 ledger trim; replaced in v0.4.4) --------------
#
# The ledger bucket-trimming approach to the rolling window was removed: it
# needed hour buckets, timezone conversions and day-boundary special cases to
# all line up, and any miss read as a silent 0. The 24h span is now served
# from recent.json's per-request epoch timestamps by qk_stats_window (routed
# in api_stats); qk_stats keeps window_start_ts only as an ignored parameter.


def test_stats_window_24h(load_admin, monkeypatch):
    """24h span = pure timestamp filter over recent.json. No day/hour buckets,
    no timezone math: items with ts >= now-86400 are in, older ones are out,
    even when they were recorded on a different calendar day."""
    import time as _time
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path
    from tests.conftest import write_json

    CST = _tz(_td(hours=8))
    adm = load_admin()
    write_json(Path(adm.QK_CONFIG_PATH), {"schedule": {"timezone": "Asia/Shanghai"}})
    monkeypatch.setattr(adm, "qk_local_now",
                        lambda cfg: _dt(2026, 8, 20, 0, 16, tzinfo=CST))
    # "now" for the window is 2026-08-20 00:16 CST; items recorded across the
    # UTC day boundary (2026-08-19 evening local ... wait, CST 00:16 = UTC 16:16
    # previous day) must all still count -- this is the exact scenario that
    # showed 0 with the bucket approach.
    now_ts = _dt(2026, 8, 20, 0, 16, tzinfo=CST).timestamp()
    items = [
        {"ts": now_ts - 3600, "user_id": "u1", "name": "A", "email": "a@x",
         "model": "m/x", "tokens": {"cached": 0.0, "input": 100.0, "output": 50.0},
         "cost_usd": 0.01, "tou_tier": "normal", "priced": True, "channel": "webui"},
        {"ts": now_ts - 23 * 3600, "user_id": "u1", "name": "A", "email": "a@x",
         "model": "m/y", "tokens": {"cached": 10.0, "input": 90.0, "output": 40.0},
         "cost_usd": 0.02, "tou_tier": "peak", "priced": False, "channel": "api"},
        # 25h ago: outside the window, must be excluded
        {"ts": now_ts - 25 * 3600, "user_id": "u1", "name": "A", "email": "a@x",
         "model": "m/x", "tokens": {"cached": 0.0, "input": 5.0, "output": 5.0},
         "cost_usd": 99.0, "tou_tier": "normal", "priced": True, "channel": "webui"},
    ]
    write_json(Path(adm.QK_RECENT_PATH), {"items": items})
    # v0.5.12: KPI totals come from the ledger's hourly buckets (exact, no
    # 200-ring cap). recent.json still drives the per-user/per-model tables.
    # item1 (1h ago) = 2026-08-19 23:16 CST -> day 2026-08-19 hour 23;
    # item2 (23h ago) = 2026-08-19 01:16 CST -> day 2026-08-19 hour 1;
    # item3 (25h ago) = 2026-08-18 23:16 CST -> outside the window.
    write_json(Path(adm.QK_LEDGER_PATH),
               {"users": {"u1": {"name": "A", "email": "a@x", "days": {
                   "2026-08-19": {
                       "models": {"m/y": {"unpriced_requests": 1}},
                       "hours": {
                           "23": {"requests": 1, "cost_usd": 0.01,
                                  "tokens": {"cached": 0.0, "input": 100.0, "output": 50.0},
                                  "channels": {"webui": 1, "api": 0},
                                  "models": {"m/x": {"requests": 1, "cost_usd": 0.01,
                                                      "tokens": {"cached": 0.0, "input": 100.0, "output": 50.0},
                                                      "channels": {"webui": 1, "api": 0}}}},
                           "1": {"requests": 1, "cost_usd": 0.02,
                                 "tokens": {"cached": 10.0, "input": 90.0, "output": 40.0},
                                 "channels": {"webui": 0, "api": 1},
                                 "models": {"m/y": {"requests": 1, "cost_usd": 0.02,
                                                     "tokens": {"cached": 10.0, "input": 90.0, "output": 40.0},
                                                     "channels": {"webui": 0, "api": 1}}}},
                       }},
                   "2026-08-18": {"hours": {
                       "23": {"requests": 1, "cost_usd": 99.0,
                              "tokens": {"cached": 0.0, "input": 5.0, "output": 5.0},
                              "channels": {"webui": 1, "api": 0},
                              "models": {"m/x": {"requests": 1, "cost_usd": 99.0,
                                                  "tokens": {"cached": 0.0, "input": 5.0, "output": 5.0},
                                                  "channels": {"webui": 1, "api": 0}}}},
                   }},
               }}}})

    out = adm.qk_stats_window(now_ts - 86400)
    assert out["kpi"]["requests"] == 2
    assert abs(out["kpi"]["cost_usd"] - 0.03) < 1e-9
    assert out["kpi"]["unpriced_requests"] == 1
    assert out["kpi"]["tokens"]["input"] == 190.0
    row = out["users"][0]
    assert row["requests"] == 2
    assert row["channels"] == {"webui": 1, "api": 1}
    assert row["quota_source"] == "none"  # resolved from config, no quota set
    models = {m["model"]: m for m in out["models"]}
    assert models["m/x"]["requests"] == 1
    # v0.5.17: hour buckets carry no per-model tou/unpriced breakdown
    # (unpriced is aggregated at the KPI level from day-level models)
    # series bucketed by local hour: item2 at 01:16 local yesterday
    assert any(b["bucket"].endswith("T01") for b in out["series"])
    # window series also carries per-bucket requests/tokens; kpi aggregates channels
    assert out["kpi"]["channels"] == {"webui": 1, "api": 1}
    b0 = out["series"][0]
    assert set(("bucket", "cost", "requests", "tokens")) <= set(b0)
    # v0.5.27: window series cost is keyed BY MODEL (not a single "_" aggregate)
    # so the 24h trend chart can stack per-model bars + show a per-model legend.
    assert all("_" not in b["cost"] for b in out["series"])
    # m/x at hour 23 ($0.01), m/y at hour 01 ($0.02)
    bycost = {}
    for b in out["series"]:
        for m, c in b["cost"].items():
            bycost[m] = bycost.get(m, 0) + c
    assert abs(bycost.get("m/x", 0) - 0.01) < 1e-9
    assert abs(bycost.get("m/y", 0) - 0.02) < 1e-9
    assert sum(b["requests"] for b in out["series"]) == 2  # 25h-old item trimmed out
    # item1 tokens 0+100+50=150, item2 10+90+40=140
    assert abs(sum(b["tokens"] for b in out["series"]) - (150.0 + 140.0)) < 1e-9
    # filters still apply
    assert adm.qk_stats_window(now_ts - 86400, model="m/y")["kpi"]["requests"] == 1
    assert adm.qk_stats_window(now_ts - 86400, user="nobody")["kpi"]["requests"] == 0


def test_stats_window_partial_flag(load_admin, monkeypatch):
    """v0.5.17: KPI comes from the ledger hour buckets (exact), so
    window_partial is always False — the flag no longer applies."""
    from pathlib import Path
    from tests.conftest import write_json

    adm = load_admin()
    write_json(Path(adm.QK_RECENT_PATH), {"items": [
        {"ts": 1_000_000.0, "user_id": "u1", "name": "A", "email": "a@x",
         "model": "m/x", "tokens": {}, "cost_usd": 0.0, "channel": "api"}
        for _ in range(200)
    ]})
    out = adm.qk_stats_window(500_000.0)
    assert out["kpi"]["window_partial"] is False
    # buffer not full: nothing could have been evicted, never partial
    write_json(Path(adm.QK_RECENT_PATH), {"items": [
        {"ts": 1_000_000.0, "user_id": "u1", "name": "A", "email": "a@x",
         "model": "m/x", "tokens": {}, "cost_usd": 0.0, "channel": "api"}
    ]})
    assert adm.qk_stats_window(500_000.0)["kpi"]["window_partial"] is False


# ---- reprice (backfill unpriced ledger days, v0.4.6) -------------------------


def _unpriced_ledger(day, model="glm-5.3", cost=0.0, unpriced=2, inp=100000.0, out=5000.0):
    toks = {"cached": 0.0, "input": inp, "output": out}
    tou = {"peak": 0, "offpeak": 0, "normal": 2}
    mm = {"requests": 2, "cost_usd": cost, "tokens": dict(toks),
          "priced": False, "unpriced_requests": unpriced,
          "tou": dict(tou), "cost_saved_usd": 0.0}
    d = {"requests": 2, "cost_usd": cost, "tokens": dict(toks),
         "tou": dict(tou), "cost_saved_usd": 0.0, "models": {model: mm}}
    return {"users": {"u1": {"name": "A", "email": "a@x", "days": {day: d}}}}


def test_reprice_backfills_and_clears_tag(load_admin, monkeypatch):
    """The user's glm-5.3 case: recorded while unpriced (cost 0, tag set), a
    price is configured later -> reprice backfills cost and clears the tag."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path
    from tests.conftest import write_json

    CST = _tz(_td(hours=8))
    adm = load_admin()
    monkeypatch.setattr(adm, "qk_tou_local_now",
                        lambda cfg: _dt(2026, 8, 20, 12, 0, tzinfo=CST))
    monkeypatch.setattr(adm, "qk_local_now",
                        lambda cfg: _dt(2026, 8, 20, 12, 0, tzinfo=CST))
    write_json(Path(adm.QK_LEDGER_PATH), _unpriced_ledger("2026-08-19"))
    # price configured AFTER the fact: $1/M input, $2/M output (per-1M table)
    write_json(Path(adm.QK_PRICING_PATH), {"table": {
        "glm-5.3": {"input": 1.0, "output": 2.0}}})
    write_json(Path(adm.QK_CONFIG_PATH), {"tou": {"enabled": False}})

    # bucket tokens are the AGGREGATE of both requests: 100k in + 5k out.
    # full re-cost = 100000*$1/1M + 5000*$2/1M = $0.11; both requests unpriced
    # (share 2/2 = 1) -> the whole $0.11 is backfilled.
    rep = adm.qk_reprice_ledger(days=30)
    assert rep["buckets_repriced"] == 1
    assert abs(rep["cost_added_usd"] - 0.11) < 1e-6

    import json
    led = json.load(open(adm.QK_LEDGER_PATH))
    mm = led["users"]["u1"]["days"]["2026-08-19"]["models"]["glm-5.3"]
    assert abs(mm["cost_usd"] - 0.11) < 1e-6
    assert mm["unpriced_requests"] == 0 and mm["priced"] is True
    d = led["users"]["u1"]["days"]["2026-08-19"]
    assert abs(d["cost_usd"] - 0.11) < 1e-6  # day total backfilled too

    # idempotent: a second run finds no unpriced buckets
    rep2 = adm.qk_reprice_ledger(days=30)
    assert rep2["buckets_repriced"] == 0


def test_reprice_mixed_and_still_unpriced(load_admin, monkeypatch):
    """Mixed buckets are topped up by exactly their unpriced share (not the
    full re-cost); a model with STILL no price keeps its tag."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path
    from tests.conftest import write_json
    import json

    CST = _tz(_td(hours=8))
    adm = load_admin()
    for fn in ("qk_tou_local_now", "qk_local_now"):
        monkeypatch.setattr(adm, fn, lambda cfg: _dt(2026, 8, 20, 12, 0, tzinfo=CST))
    led = _unpriced_ledger("2026-08-19", model="glm-5.3")  # fully unpriced
    models = led["users"]["u1"]["days"]["2026-08-19"]["models"]
    # glm-5.3 case: 2 requests, 1 already priced ($0.11 stored), 1 unpriced.
    # bucket tokens are the 2-request aggregate (100k in/5k out) -> full
    # re-cost $0.11; unpriced share 1/2 -> backfill $0.055 -> total $0.165.
    models["m/half"] = _unpriced_ledger("2026-08-19", model="m/half", cost=0.11, unpriced=1)["users"]["u1"]["days"]["2026-08-19"]["models"]["m/half"]
    # still-unpriced model (no price anywhere) -> untouched
    models["m/noprice"] = _unpriced_ledger("2026-08-19", model="m/noprice")["users"]["u1"]["days"]["2026-08-19"]["models"]["m/noprice"]
    write_json(Path(adm.QK_LEDGER_PATH), led)
    price = {"input": 1.0, "output": 2.0}
    write_json(Path(adm.QK_PRICING_PATH), {"table": {
        "glm-5.3": price, "m/half": price}})
    write_json(Path(adm.QK_CONFIG_PATH), {"tou": {"enabled": False}})

    rep = adm.qk_reprice_ledger(days=30)
    assert rep["buckets_repriced"] == 2  # glm-5.3 (full) + m/half (share)
    out = json.load(open(adm.QK_LEDGER_PATH))
    models = out["users"]["u1"]["days"]["2026-08-19"]["models"]
    # glm-5.3 fully unpriced: 0 + full share (2/2) of $0.11 = $0.11
    assert abs(models["glm-5.3"]["cost_usd"] - 0.11) < 1e-6
    assert models["glm-5.3"]["unpriced_requests"] == 0
    # m/half: $0.11 stored + half share (1/2) of $0.11 = $0.055 -> $0.165 total
    assert abs(models["m/half"]["cost_usd"] - 0.165) < 1e-6
    assert models["m/half"]["unpriced_requests"] == 0        # tag cleared
    assert models["m/noprice"]["unpriced_requests"] == 2     # tag kept (no price)
    assert models["m/noprice"]["cost_usd"] == 0.0


def test_reprice_dry_run_writes_nothing(load_admin, monkeypatch):
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path
    from tests.conftest import write_json
    import json

    CST = _tz(_td(hours=8))
    adm = load_admin()
    for fn in ("qk_tou_local_now", "qk_local_now"):
        monkeypatch.setattr(adm, fn, lambda cfg: _dt(2026, 8, 20, 12, 0, tzinfo=CST))
    write_json(Path(adm.QK_LEDGER_PATH), _unpriced_ledger("2026-08-19"))
    write_json(Path(adm.QK_PRICING_PATH), {"table": {"glm-5.3": {"input": 1.0, "output": 2.0}}})
    write_json(Path(adm.QK_CONFIG_PATH), {"tou": {"enabled": False}})

    rep = adm.qk_reprice_ledger(days=30, dry_run=True)
    assert rep["buckets_repriced"] == 1 and abs(rep["cost_added_usd"] - 0.11) < 1e-6
    led = json.load(open(adm.QK_LEDGER_PATH))
    mm = led["users"]["u1"]["days"]["2026-08-19"]["models"]["glm-5.3"]
    assert mm["cost_usd"] == 0.0 and mm["unpriced_requests"] == 2  # unchanged


def test_reprice_also_backfills_recent(load_admin, monkeypatch):
    """reprice rewrites recent.json unpriced entries in the SAME pass, at the
    SAME price, so the Recent feed agrees with the Models/ledger numbers."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path
    from tests.conftest import write_json
    import json

    CST = _tz(_td(hours=8))
    adm = load_admin()
    for fn in ("qk_tou_local_now", "qk_local_now"):
        monkeypatch.setattr(adm, fn, lambda cfg: _dt(2026, 8, 20, 12, 0, tzinfo=CST))
    write_json(Path(adm.QK_LEDGER_PATH), _unpriced_ledger("2026-08-19"))
    write_json(Path(adm.QK_PRICING_PATH), {"table": {"glm-5.3": {"input": 1.0, "output": 2.0}}})
    write_json(Path(adm.QK_CONFIG_PATH), {"tou": {"enabled": False}})

    inside = _dt(2026, 8, 19, 10, 0, tzinfo=CST).timestamp()   # within 30d window
    outside = _dt(2026, 6, 1, 10, 0, tzinfo=CST).timestamp()   # before cutoff
    write_json(Path(adm.QK_RECENT_PATH), {"items": [
        {"ts": inside, "user_id": "u1", "name": "A", "email": "a@x", "model": "glm-5.3",
         "tokens": {"cached": 0.0, "input": 100000.0, "output": 5000.0},
         "cost_usd": 0.0, "tou_tier": "normal", "priced": False, "channel": "webui"},
        {"ts": inside, "user_id": "u1", "name": "A", "email": "a@x", "model": "glm-5.3",
         "tokens": {"cached": 0.0, "input": 50000.0, "output": 1000.0},
         "cost_usd": 0.052, "tou_tier": "normal", "priced": True, "channel": "webui"},
        {"ts": outside, "user_id": "u1", "name": "A", "email": "a@x", "model": "glm-5.3",
         "tokens": {"cached": 0.0, "input": 9000.0, "output": 1000.0},
         "cost_usd": 0.0, "tou_tier": "normal", "priced": False, "channel": "api"},
    ]})

    rep = adm.qk_reprice_ledger(days=30)
    items = json.load(open(adm.QK_RECENT_PATH))["items"]
    # entry 0: unpriced, in-window -> repriced to $0.11, priced flag set
    assert abs(items[0]["cost_usd"] - 0.11) < 1e-6 and items[0]["priced"] is True
    # entry 1: already priced -> untouched
    assert abs(items[1]["cost_usd"] - 0.052) < 1e-9 and items[1]["priced"] is True
    # entry 2: unpriced but OUTSIDE the window -> untouched
    assert items[2]["cost_usd"] == 0.0 and items[2]["priced"] is False
    assert rep["recent_items_repriced"] == 1
    assert abs(rep["recent_cost_added_usd"] - 0.11) < 1e-6


def test_reprice_resolves_model_aliases(load_admin, monkeypatch):
    """2026-08-24 bug (deepseek-flash -> deepseek-v4-flash): buckets recorded
    under an upstream ALIAS name could never reprice — the ledger/recent passes
    looked up the RAW bucket name in the price table and never resolved config
    model_aliases, so unpriced_requests stayed set, and the display-side alias
    merge (/models, /stats) folded the stale tag into the TARGET model's row
    (the target showed 'unpriced' right after the alias was added). Reprice
    must price aliased buckets/entries via the resolved real name."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path
    from tests.conftest import write_json
    import json

    CST = _tz(_td(hours=8))
    adm = load_admin()
    for fn in ("qk_tou_local_now", "qk_local_now"):
        monkeypatch.setattr(adm, fn, lambda cfg: _dt(2026, 8, 20, 12, 0, tzinfo=CST))
    write_json(Path(adm.QK_LEDGER_PATH),
               _unpriced_ledger("2026-08-19", model="deepseek-flash"))
    write_json(Path(adm.QK_PRICING_PATH), {"table": {
        "deepseek-v4-flash": {"input": 1.0, "output": 2.0}}})
    write_json(Path(adm.QK_CONFIG_PATH), {
        "tou": {"enabled": False},
        "model_aliases": {"deepseek-flash": "deepseek-v4-flash"}})
    inside = _dt(2026, 8, 19, 10, 0, tzinfo=CST).timestamp()
    write_json(Path(adm.QK_RECENT_PATH), {"items": [
        {"ts": inside, "user_id": "u1", "name": "A", "email": "a@x",
         "model": "deepseek-flash",
         "tokens": {"cached": 0.0, "input": 100000.0, "output": 5000.0},
         "cost_usd": 0.0, "tou_tier": "normal", "priced": False, "channel": "api"},
    ]})

    rep = adm.qk_reprice_ledger(days=30)
    assert rep["buckets_repriced"] == 1
    assert abs(rep["cost_added_usd"] - 0.11) < 1e-6
    led = json.load(open(adm.QK_LEDGER_PATH))
    mm = led["users"]["u1"]["days"]["2026-08-19"]["models"]["deepseek-flash"]
    assert abs(mm["cost_usd"] - 0.11) < 1e-6
    assert mm["unpriced_requests"] == 0 and mm["priced"] is True
    items = json.load(open(adm.QK_RECENT_PATH))["items"]
    assert abs(items[0]["cost_usd"] - 0.11) < 1e-6 and items[0]["priced"] is True
    assert rep["recent_items_repriced"] == 1


def test_reprice_alias_without_target_price_keeps_flag(load_admin, monkeypatch):
    """An alias whose TARGET still has no price anywhere must leave the bucket
    untouched (no indiscriminate tag clearing)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path
    from tests.conftest import write_json
    import json

    CST = _tz(_td(hours=8))
    adm = load_admin()
    for fn in ("qk_tou_local_now", "qk_local_now"):
        monkeypatch.setattr(adm, fn, lambda cfg: _dt(2026, 8, 20, 12, 0, tzinfo=CST))
    write_json(Path(adm.QK_LEDGER_PATH),
               _unpriced_ledger("2026-08-19", model="deepseek-flash"))
    write_json(Path(adm.QK_PRICING_PATH), {"table": {"other": {"input": 1.0, "output": 2.0}}})
    write_json(Path(adm.QK_CONFIG_PATH), {
        "tou": {"enabled": False},
        "model_aliases": {"deepseek-flash": "deepseek-v4-flash"}})

    rep = adm.qk_reprice_ledger(days=30)
    assert rep["buckets_repriced"] == 0
    led = json.load(open(adm.QK_LEDGER_PATH))
    mm = led["users"]["u1"]["days"]["2026-08-19"]["models"]["deepseek-flash"]
    assert mm["unpriced_requests"] == 2 and mm["cost_usd"] == 0.0


def test_reprice_model_filter_matches_resolved_alias(load_admin, monkeypatch):
    """The per-model reprice button passes the DISPLAY name (alias-resolved,
    e.g. deepseek-v4-flash); the filter must also catch buckets still recorded
    under the alias (deepseek-flash), not just exact-name matches."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path
    from tests.conftest import write_json
    import json

    CST = _tz(_td(hours=8))
    adm = load_admin()
    for fn in ("qk_tou_local_now", "qk_local_now"):
        monkeypatch.setattr(adm, fn, lambda cfg: _dt(2026, 8, 20, 12, 0, tzinfo=CST))
    led = _unpriced_ledger("2026-08-19", model="deepseek-flash")
    models = led["users"]["u1"]["days"]["2026-08-19"]["models"]
    models["glm-5.3"] = _unpriced_ledger("2026-08-19", model="glm-5.3")[
        "users"]["u1"]["days"]["2026-08-19"]["models"]["glm-5.3"]
    write_json(Path(adm.QK_LEDGER_PATH), led)
    price = {"input": 1.0, "output": 2.0}
    write_json(Path(adm.QK_PRICING_PATH), {"table": {
        "deepseek-v4-flash": price, "glm-5.3": price}})
    write_json(Path(adm.QK_CONFIG_PATH), {
        "tou": {"enabled": False},
        "model_aliases": {"deepseek-flash": "deepseek-v4-flash"}})

    # filtering by the RESOLVED name reprices the alias-named bucket only
    rep = adm.qk_reprice_ledger(days=30, model="deepseek-v4-flash")
    assert rep["buckets_repriced"] == 1
    out = json.load(open(adm.QK_LEDGER_PATH))
    models = out["users"]["u1"]["days"]["2026-08-19"]["models"]
    assert models["deepseek-flash"]["unpriced_requests"] == 0
    assert models["glm-5.3"]["unpriced_requests"] == 2  # different model: skipped
    # filtering by the RAW alias name also works
    rep = adm.qk_reprice_ledger(days=30, model="deepseek-flash")
    out = json.load(open(adm.QK_LEDGER_PATH))
    assert out["users"]["u1"]["days"]["2026-08-19"]["models"][
        "glm-5.3"]["unpriced_requests"] == 2  # still untouched
