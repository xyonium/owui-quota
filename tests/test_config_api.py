# tests/test_config_api.py
# Task 4: config POST = validate (400 on bad schema) + deep-merge into the
# on-disk config (partial saves preserve siblings), instead of replacing the
# whole config with defaults+patch.
#
# Deviation from task-4-brief.md: the brief's `_app(load_admin)` mounts the
# router without auth stubs, but Task 2 made every admin route require
# `open_webui.utils.auth.get_verified_user` (401 otherwise). We use the
# `admin_client` fixture (tests/conftest.py) which stubs that import to a
# role=admin user before building the client. Controller-authorized.
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(admin_client):
    return admin_client()


def test_partial_post_preserves_siblings(admin_client):
    c, adm = _app(admin_client)
    adm.qk_atomic_write(
        adm.QK_CONFIG_PATH,
        {"pricing": {"url": "u", "refresh_hours": 12, "overrides": {"m": {"input": 1}}}},
    )
    r = c.post("/api/v1/quota-keeper/config", json={"pricing": {"url": "u2"}})
    assert r.status_code == 200
    cfg = adm.qk_load_json(adm.QK_CONFIG_PATH, {})
    assert cfg["pricing"]["refresh_hours"] == 12 and cfg["pricing"]["overrides"] == {"m": {"input": 1}}
    assert cfg["pricing"]["url"] == "u2"


def test_bad_schema_400(admin_client):
    c, adm = _app(admin_client)
    r = c.post("/api/v1/quota-keeper/config", json={"schedule": "x"})
    assert r.status_code == 400 and "schedule" in r.text


def test_numeric_bounds(admin_client):
    c, adm = _app(admin_client)
    r = c.post("/api/v1/quota-keeper/config", json={"schedule": {"night_start_hour": 99}})
    assert r.status_code == 400


def test_non_json_body_400(admin_client):
    # Fix: malformed body must be a clean 400, not an unhandled 500 from
    # `await request.json()` raising.
    c, adm = _app(admin_client)
    r = c.post("/api/v1/quota-keeper/config", content=b"{not json",
               headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "invalid JSON body" in r.text


def test_list_config_on_disk_recovers(admin_client):
    # Fix: a config.json that is a JSON list (e.g. manual corruption) used to
    # crash the POST path (500) inside qk_deep_merge; it must be treated as an
    # empty object so a save succeeds and GET /config returns an object.
    c, adm = _app(admin_client)
    adm.qk_atomic_write(adm.QK_CONFIG_PATH, ["not", "an", "object"])
    r = c.post("/api/v1/quota-keeper/config", json={"schedule": {"timezone": "UTC"}})
    assert r.status_code == 200
    r = c.get("/api/v1/quota-keeper/config")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)
    assert r.json()["schedule"]["timezone"] == "UTC"


def test_get_config_recovers_from_list_config(admin_client):
    # Fix: GET /config used to 500 when the on-disk config.json parses to a
    # non-dict (qk_merge_config called .items() on it). It must merge from an
    # empty object and return the full defaults-shaped config.
    c, adm = _app(admin_client)
    adm.qk_atomic_write(adm.QK_CONFIG_PATH, ["not", "an", "object"])
    r = c.get("/api/v1/quota-keeper/config")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert body["credits_per_usd"] == 1000.0
    assert body["pricing"]["refresh_hours"] == 24


# ---- pricing override schema: alias + multiplier (v0.3.0) --------------------


def _find(adm, model, table, ov):
    return adm.qk_find_pricing(model, table, ov)


def test_alias_override_resolves_through_table(admin_client):
    c, adm = _app(admin_client)
    table = {"kimi-k3": {"input": 2.0, "output": 8.0, "cached": 0.5}}
    ov = {"kimi-k3-256k": {"alias": "kimi-k3", "multiplier": 0.5}}
    price, how = _find(adm, "kimi-k3-256k", table, ov)
    assert price == {"input": 1.0, "output": 4.0, "cached": 0.25}
    assert how.startswith("alias:kimi-k3")


def test_alias_without_multiplier_and_nested_alias(admin_client):
    c, adm = _app(admin_client)
    table = {"a/base": {"input": 4.0, "output": 4.0}}
    ov = {"b": {"alias": "a/base"}, "c": {"alias": "b", "multiplier": 2}}
    assert _find(adm, "b", table, ov)[0] == {"input": 4.0, "output": 4.0}
    assert _find(adm, "c", table, ov)[0] == {"input": 8.0, "output": 8.0}


def test_alias_cycle_safe_and_missing_target(admin_client):
    c, adm = _app(admin_client)
    ov = {"x": {"alias": "y"}, "y": {"alias": "x"}}
    assert _find(adm, "x", {}, ov) == (None, None)
    assert _find(adm, "x", {}, {"x": {"alias": "ghost"}}) == (None, None)


def test_wrapped_prices_and_legacy_direct_and_null(admin_client):
    c, adm = _app(admin_client)
    table = {}
    ov = {
        "w": {"prices": {"input": 1.0, "output": 2.0}},
        "l": {"input": 3.0, "output": 6.0},
        "n": None,
    }
    assert _find(adm, "w", table, ov)[0] == {"input": 1.0, "output": 2.0}
    assert _find(adm, "l", table, ov)[0] == {"input": 3.0, "output": 6.0}
    price, how = _find(adm, "n", table, ov)
    assert price is None  # null override = cleared, does not match


def test_override_validation(admin_client):
    c, adm = _app(admin_client)
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"overrides": {"k": {"alias": "a/base", "multiplier": 0.5}}}}).status_code == 200
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"overrides": {"k": {"alias": "", "multiplier": 1}}}}).status_code == 400
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"overrides": {"k": {"alias": "x", "multiplier": 0}}}}).status_code == 400
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"overrides": {"k": "junk"}}}).status_code == 400
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"overrides": {"k": {"prices": {"input": -1}}}}}).status_code == 400
    # null tombstone is legal (clears an override on the deep-merge)
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"overrides": {"k": None}}}).status_code == 200


def test_models_endpoint_aggregates_used_and_available(admin_client, monkeypatch):
    import sys
    import types as _t
    from pathlib import Path
    from tests.conftest import write_json

    c, adm = _app(admin_client)
    write_json(Path(adm.QK_LEDGER_PATH), {"users": {"u1": {"days": {
        "2026-08-18": {"models": {
            "kimi-k3-256k": {"requests": 16, "unpriced_requests": 16, "cost_usd": 0.0},
            "gpt-4o": {"requests": 2, "unpriced_requests": 0, "cost_usd": 0.1},
        }}
    }}}})
    write_json(Path(adm.QK_PRICING_PATH),
               {"table": {"kimi-k3": {"input": 2.0, "output": 8.0}, "gpt-4o": {"input": 5.0, "output": 15.0}}})
    write_json(Path(adm.QK_CONFIG_PATH),
               {"pricing": {"overrides": {"kimi-k3-256k": {"alias": "kimi-k3", "multiplier": 0.5}}}})

    # stub open_webui.models.models.Models.get_all_models (async in current OWUI)
    ow = _t.ModuleType("open_webui")
    models_mod = _t.ModuleType("open_webui.models")
    mmm = _t.ModuleType("open_webui.models.models")

    class _M:
        def __init__(self, id):
            self.id = id

    class Models:
        @staticmethod
        async def get_all_models(*a, **kw):
            return [_M("kimi-k3-256k"), _M("prx.free")]

    mmm.Models = Models
    ow.models = models_mod
    models_mod.models = mmm
    monkeypatch.setitem(sys.modules, "open_webui", ow)
    monkeypatch.setitem(sys.modules, "open_webui.models", models_mod)
    monkeypatch.setitem(sys.modules, "open_webui.models.models", mmm)

    r = c.get("/api/v1/quota-keeper/models")
    assert r.status_code == 200
    body = r.json()
    assert body["pricing_fetched"] is True
    items = {it["model"]: it for it in body["items"]}
    k = items["kimi-k3-256k"]
    # ledger history predates the override: 16 requests were recorded unpriced;
    # the /models row keeps that history while NOW resolving via the alias
    assert k["used"] and k["requests"] == 16 and k["unpriced_requests"] == 16
    assert k["matched"] and k["price"] == {"input": 1.0, "output": 4.0}
    assert k["override"] == {"alias": "kimi-k3", "multiplier": 0.5}
    assert items["gpt-4o"]["matched"] and items["gpt-4o"]["price"]["input"] == 5.0
    # /api/models entries with no usage are NOT listed (the editor only shows
    # models that actually appear in usage records)
    assert "prx.free" not in items
