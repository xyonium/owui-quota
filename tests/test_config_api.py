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
    r = c.post("/api/v1/quota-keeper/config", json={"credits_per_usd": 0})
    assert r.status_code == 400
    # legacy schedule multiplier keys are ignored (removed feature), not 400
    r = c.post("/api/v1/quota-keeper/config", json={"schedule": {"night_start_hour": 99}})
    assert r.status_code == 200


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


# ---- whole-map replace-on-save (issue #2: aliases could not be deleted) ----


def test_model_aliases_replace_semantics(admin_client):
    # body 含 model_aliases 时整体替换：缺席的旧 key 被删除，不再是深合并残留。
    c, adm = _app(admin_client)
    adm.qk_atomic_write(adm.QK_CONFIG_PATH, {"model_aliases": {"a": "real-a", "b": "real-b"}})
    r = c.post("/api/v1/quota-keeper/config", json={"model_aliases": {"b": "real-b", "c": "real-c"}})
    assert r.status_code == 200
    cfg = adm.qk_load_json(adm.QK_CONFIG_PATH, {})
    assert cfg["model_aliases"] == {"b": "real-b", "c": "real-c"}  # "a" 被删


def test_model_aliases_clear_all(admin_client):
    # 清空 textarea → 前端发 {} → 现在应清空全部映射（此前是深合并 no-op，旧映射全残留）。
    c, adm = _app(admin_client)
    adm.qk_atomic_write(adm.QK_CONFIG_PATH, {"model_aliases": {"a": "real-a"}})
    r = c.post("/api/v1/quota-keeper/config", json={"model_aliases": {}})
    assert r.status_code == 200
    assert adm.qk_load_json(adm.QK_CONFIG_PATH, {}).get("model_aliases") == {}


def test_model_aliases_absent_untouched(admin_client):
    # 省略该 key = 不动（保留既有映射），保证普通保存不清空。
    c, adm = _app(admin_client)
    adm.qk_atomic_write(adm.QK_CONFIG_PATH, {"model_aliases": {"a": "real-a"}})
    r = c.post("/api/v1/quota-keeper/config", json={"credits_per_usd": 2000})
    assert r.status_code == 200
    assert adm.qk_load_json(adm.QK_CONFIG_PATH, {}).get("model_aliases") == {"a": "real-a"}


def test_overrides_empty_spec_stripped(admin_client):
    # replace 语义下 null 墓碑与 {} 空 spec 都在落盘前剥离，避免 /models 把 {} 误判成 manual。
    c, adm = _app(admin_client)
    adm.qk_atomic_write(
        adm.QK_CONFIG_PATH,
        {"pricing": {"overrides": {"m1": {"alias": "x"}, "m2": {"input": 1}}}},
    )
    r = c.post(
        "/api/v1/quota-keeper/config",
        json={"pricing": {"overrides": {"m1": None, "m2": {}}}},
    )
    assert r.status_code == 200
    ov = adm.qk_load_json(adm.QK_CONFIG_PATH, {})["pricing"]["overrides"]
    assert ov == {}


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


# ---- multiplier independent of alias (v0.5.32 / 0.4.17) ---------------------

def test_multiplier_scales_wrapped_prices(admin_client):
    # {prices:{...}, multiplier:m}: manual override price discounted by m
    c, adm = _app(admin_client)
    ov = {"m": {"prices": {"input": 1.0, "output": 2.0}, "multiplier": 0.5}}
    price, how = _find(adm, "m", {}, ov)
    assert price == {"input": 0.5, "output": 1.0}
    assert how == "override:m*0.5"


def test_multiplier_scales_legacy_direct_prices(admin_client):
    # legacy bare price dict that ALSO carries a multiplier: scale the prices,
    # and do NOT treat the multiplier key as a price field
    c, adm = _app(admin_client)
    ov = {"m": {"input": 4.0, "output": 8.0, "multiplier": 0.25}}
    price, how = _find(adm, "m", {}, ov)
    assert price == {"input": 1.0, "output": 2.0}
    assert how == "override:m*0.25"


def test_multiplier_only_scales_upstream_table_match(admin_client):
    # {multiplier:m} alone: scale whatever the upstream pricing table matches;
    # no longer mis-parsed as a price dict (which used to yield empty prices)
    c, adm = _app(admin_client)
    table = {"deepseek-chat": {"input": 0.27, "output": 1.10}}
    ov = {"deepseek-chat": {"multiplier": 0.8}}
    price, how = _find(adm, "deepseek-chat", table, ov)
    assert abs(price["input"] - 0.216) < 1e-9 and abs(price["output"] - 0.88) < 1e-9
    assert how.startswith("exact:deepseek-chat*0.8")
    # no table match -> unpriced (NOT a bogus empty-price override)
    assert _find(adm, "deepseek-chat", {}, ov) == (None, None)


def test_multiplier_validation_all_shapes(admin_client):
    c, adm = _app(admin_client)
    # multiplier-only with a valid multiplier passes
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"overrides": {"k": {"multiplier": 0.8}}}}).status_code == 200
    # multiplier on wrapped prices validated
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"overrides": {"k": {"prices": {"input": 1.0}, "multiplier": 0}}}}).status_code == 400
    # multiplier-only with a bad multiplier rejected
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"overrides": {"k": {"multiplier": -1}}}}).status_code == 400
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


def test_empty_override_spec_is_not_a_manual_override(admin_client, monkeypatch):
    # An empty/null override spec is not a real override: /models must not
    # report it as one (it resolves to no price). Pin the read side so a
    # pre-existing {} in config.json can't fake a "matched" editor row.
    import sys
    import types as _t
    from pathlib import Path
    from tests.conftest import write_json

    c, adm = _app(admin_client)
    write_json(Path(adm.QK_LEDGER_PATH), {"users": {"u1": {"days": {
        "2026-08-18": {"models": {"m-bare": {"requests": 3, "unpriced_requests": 3,
                                             "cost_usd": 0.0}}}
    }}}})
    write_json(Path(adm.QK_PRICING_PATH), {"table": {"other": {"input": 1.0, "output": 2.0}}})
    write_json(Path(adm.QK_CONFIG_PATH),
               {"pricing": {"overrides": {"m-bare": {}, "m-none": None}}})

    ow = _t.ModuleType("open_webui")
    models_mod = _t.ModuleType("open_webui.models")
    mmm = _t.ModuleType("open_webui.models.models")

    class Models:
        @staticmethod
        async def get_all_models(*a, **kw):
            return []

    mmm.Models = Models
    ow.models = models_mod
    models_mod.models = mmm
    monkeypatch.setitem(sys.modules, "open_webui", ow)
    monkeypatch.setitem(sys.modules, "open_webui.models", models_mod)
    monkeypatch.setitem(sys.modules, "open_webui.models.models", mmm)

    r = c.get("/api/v1/quota-keeper/models")
    assert r.status_code == 200
    items = {it["model"]: it for it in r.json()["items"]}
    assert items["m-bare"]["override"] is None
    assert items["m-bare"]["matched"] is False


# ---- multi-source pricing (v0.3.4) --------------------------------------------


def test_fetch_pricing_merges_sources_first_wins(admin_client, monkeypatch):
    import types as _t

    c, adm = _app(admin_client)
    calls = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    # source 1: LiteLLM flat (per-token); source 2: models.dev nested (per-1M)
    payloads = {
        "u1": {"gpt-x": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}},
        "u2": {"moonshotai": {"models": {"kimi-k3": {"cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3}}}},
               "zai": {"models": {"glm-5.2": {"cost": {"input": 1.4, "output": 4.4, "cache_read": 0.26}}}},
        }}

    def fake_get(url, timeout=30):
        calls.append(url)
        return FakeResp(payloads[url])

    monkeypatch.setitem(__import__("sys").modules, "requests", _t.SimpleNamespace(get=fake_get))
    table = {}
    table = adm.qk_fetch_pricing("u1", table=table)
    table = adm.qk_fetch_pricing("u2", table=table)
    assert table["gpt-x"] == {"input": 1.0, "output": 2.0, "cached": None, "cache_write": None}
    assert table["moonshotai/kimi-k3"]["input"] == 3.0
    assert table["kimi-k3"]["output"] == 15.0  # bare key also registered
    assert table["glm-5.2"]["cached"] == 0.26

    # conflict: first source wins
    payloads["u3"] = {"other": {"models": {"kimi-k3": {"cost": {"input": 99.0, "output": 99.0}}}}}
    table = adm.qk_fetch_pricing("u3", table=table)
    assert table["kimi-k3"]["input"] == 3.0  # NOT overwritten by u3


def test_validate_pricing_url_list(admin_client):
    c, adm = _app(admin_client)
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"url": ["https://a", "https://b"]}}).status_code == 200
    assert c.post("/api/v1/quota-keeper/config",
                  json={"pricing": {"url": 123}}).status_code == 400


# ---- zero-price rows are not a match (v0.4.1) ---------------------------------


def test_zero_price_table_row_is_unpriced(admin_client, monkeypatch):
    import types as _t
    import sys

    c, adm = _app(admin_client)

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            # models.dev plan-tier provider with $0 prices + a real one
            return {
                "kimi-for-coding": {"models": {"k3-256k": {"cost": {"input": 0, "output": 0}}}},
                "moonshotai": {"models": {"kimi-k3": {"cost": {"input": 3.0, "output": 15.0}}}},
            }

    monkeypatch.setitem(sys.modules, "requests", _t.SimpleNamespace(get=lambda url, timeout=30: FakeResp()))
    table = adm.qk_fetch_pricing("u")
    # the $0 row is skipped at fetch time...
    assert "k3-256k" not in table
    assert table["kimi-k3"]["input"] == 3.0
    # ...and even if a $0 row slips in, find_pricing treats it as unpriced
    price, how = adm.qk_find_pricing("k3-256k", {"k3-256k": {"input": 0, "output": 0}}, None)
    assert price is None and how is None
    # a real price still matches normally
    price, how = adm.qk_find_pricing("k3-256k", {"k3-256k": {"input": 1.5, "output": 8.0}}, None)
    assert price == {"input": 1.5, "output": 8.0} and how == "exact:k3-256k"
