# tests/test_images_ingest.py
"""Image endpoint metering: per-image pricing for dall-e-style models whose
responses carry no usage object, plus token-based metering for gpt-image-1
(which reports usage). Covers pricing-table parsing, record/reprice, and the
passthrough middleware on the image ingest paths."""
import json
from pathlib import Path

from tests.conftest import write_json
from tests.test_passthrough_ingest import _led, _mk_app, _rec, _stub_owui_auth


DALLE_PRICE = {"input": None, "output": None, "cached": None, "cache_write": None,
               "per_image": 0.04}
GPT_IMAGE_PRICE = {"input": 5.0, "output": 40.0, "cached": 1.25, "cache_write": None,
                   "per_image": 0.02}


def _seed_pricing(adm, table):
    write_json(Path(adm.QK_PRICING_PATH), {"fetched_at": 1, "table": table})


def _mk_image_app(adm, path, resp_body=b""):
    """Like _mk_app but the route returns resp_body verbatim (image responses
    have no usage to wrap)."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware
    from types import SimpleNamespace

    app = FastAPI()

    async def route(request: Request):
        data = json.loads(resp_body) if resp_body else {"ok": 1}
        return JSONResponse(data)

    app.add_api_route(path, route, methods=["POST"])

    async def auth_mw(request: Request, call_next):
        request.state.user = SimpleNamespace(id="u1", name="U", email="u@x.com", role="user")
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=auth_mw)
    app.add_middleware(BaseHTTPMiddleware, dispatch=adm.qk_passthrough_middleware)
    return TestClient(app)


# ---- pricing fetch: per_image preserved, image-only rows kept -----------------


def test_fetch_pricing_keeps_per_image_absolute(load_admin, monkeypatch):
    import types as _t

    adm = load_admin()
    payload = {
        # dall-e-3: no token prices at all, per-image only
        "dall-e-3": {"input_cost_per_image": 0.04, "mode": "image"},
        # gpt-image-1: token prices AND a per-image price
        "gpt-image-1": {"input_cost_per_token": 5e-6, "output_cost_per_token": 4e-5,
                        "input_cost_per_image": 0.02},
        # genuinely free row: nothing billable -> still skipped
        "free-model": {"input_cost_per_token": 0, "output_cost_per_token": 0},
    }

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setitem(__import__("sys").modules, "requests",
                        _t.SimpleNamespace(get=lambda url, timeout=30: FakeResp()))
    table = adm.qk_fetch_pricing("u1")
    # per_image is absolute USD per image, NOT scaled to per-1M
    assert table["dall-e-3"]["per_image"] == 0.04
    # image-only row survives the zero-priced-row skip
    assert table["dall-e-3"]["input"] is None and table["dall-e-3"]["output"] is None
    assert table["gpt-image-1"]["input"] == 5.0 and table["gpt-image-1"]["output"] == 40.0
    assert table["gpt-image-1"]["per_image"] == 0.02
    assert "free-model" not in table


def test_find_pricing_per_image_only_counts_as_priced(load_admin):
    adm = load_admin()
    table = {"dall-e-3": {"input": None, "output": None, "cached": None,
                          "cache_write": None, "per_image": 0.04}}
    price, how = adm.qk_find_pricing("dall-e-3", table)
    assert price is not None and price["per_image"] == 0.04
    # zero-token price WITHOUT per_image is still "unpriced"
    price, _ = adm.qk_find_pricing(
        "x", {"x": {"input": 0.0, "output": 0.0, "cached": None, "cache_write": None}})
    assert price is None


def test_find_pricing_multiplier_scales_per_image(load_admin):
    adm = load_admin()
    table = {"dall-e-3": {"input": None, "output": None, "per_image": 0.04}}
    price, _ = adm.qk_find_pricing("dall-e-3", table, {"dall-e-3": {"multiplier": 2}})
    assert price["per_image"] == 0.08


def test_image_cost_usd(load_admin):
    adm = load_admin()
    assert adm.qk_image_cost_usd(2, {"per_image": 0.04}) == 0.08
    assert adm.qk_image_cost_usd(0, {"per_image": 0.04}) == 0.0
    assert adm.qk_image_cost_usd(2, {"per_image": None}) == 0.0
    assert adm.qk_image_cost_usd(2, None) == 0.0
    assert adm.qk_image_cost_usd(2, {"per_image": True}) == 0.0  # bool is not a price


# ---- record_usage: per-image fallback ------------------------------------------


def test_record_usage_bills_per_image_when_no_tokens(load_admin, tmp_path):
    adm = load_admin()
    _seed_pricing(adm, {"dall-e-3": DALLE_PRICE})
    adm.qk_record_usage({"id": "u1", "name": "U", "email": "u@x.com"}, "dall-e-3",
                        {"cached": 0.0, "input": 0.0, "output": 0.0}, images=2)
    led = _led(adm)
    mm = list(list(led["users"]["u1"]["days"].values())[0]["models"].values())[0]
    assert mm["requests"] == 1
    assert mm["cost_usd"] == 0.08
    assert mm["images"] == 2
    assert mm["priced"] is True
    rec = _rec(adm)
    assert rec["items"][0]["images"] == 2
    assert rec["items"][0]["cost_usd"] == 0.08


def test_record_usage_token_cost_wins_over_per_image(load_admin):
    adm = load_admin()
    _seed_pricing(adm, {"gpt-image-1": GPT_IMAGE_PRICE})
    adm.qk_record_usage({"id": "u1", "name": "U", "email": "u@x.com"}, "gpt-image-1",
                        {"cached": 0.0, "input": 1000.0, "output": 200.0}, images=1)
    led = _led(adm)
    mm = list(list(led["users"]["u1"]["days"].values())[0]["models"].values())[0]
    # 1000*5/1e6 + 200*40/1e6 = 0.005 + 0.008 = 0.013; per_image must NOT add
    assert mm["cost_usd"] == 0.013
    assert mm["images"] == 1  # count still stored for auditing/reprice


def test_record_usage_unpriced_image_model_flags_unpriced(load_admin):
    adm = load_admin()  # no pricing table at all
    adm.qk_record_usage({"id": "u1", "name": "U", "email": "u@x.com"}, "dall-e-3",
                        {"cached": 0.0, "input": 0.0, "output": 0.0}, images=3)
    led = _led(adm)
    mm = list(list(led["users"]["u1"]["days"].values())[0]["models"].values())[0]
    assert mm["cost_usd"] == 0.0
    assert mm["unpriced_requests"] == 1
    assert mm["priced"] is False
    assert mm["images"] == 3  # stored so reprice can backfill later


def test_record_usage_images_default_zero_no_images_key(load_admin):
    adm = load_admin()
    _seed_pricing(adm, {"m/x": {"input": 1.0, "output": 1.0}})
    adm.qk_record_usage({"id": "u1", "name": "U", "email": "u@x.com"}, "m/x",
                        {"cached": 0.0, "input": 100.0, "output": 0.0})
    led = _led(adm)
    mm = list(list(led["users"]["u1"]["days"].values())[0]["models"].values())[0]
    assert "images" not in mm
    assert "images" not in _rec(adm)["items"][0]


# ---- reprice: per-image backfill -------------------------------------------------


def test_reprice_backfills_unpriced_image_bucket(load_admin):
    adm = load_admin()
    # recorded while no price existed
    adm.qk_record_usage({"id": "u1", "name": "U", "email": "u@x.com"}, "dall-e-3",
                        {"cached": 0.0, "input": 0.0, "output": 0.0}, images=2)
    mm = list(list(_led(adm)["users"]["u1"]["days"].values())[0]["models"].values())[0]
    assert mm["unpriced_requests"] == 1 and mm["cost_usd"] == 0.0
    # price arrives
    _seed_pricing(adm, {"dall-e-3": DALLE_PRICE})
    rep = adm.qk_reprice_ledger(days=30)
    assert rep["buckets_repriced"] == 1
    mm = list(list(_led(adm)["users"]["u1"]["days"].values())[0]["models"].values())[0]
    assert mm["cost_usd"] == 0.08
    assert mm["priced"] is True and mm["unpriced_requests"] == 0
    item = _rec(adm)["items"][0]
    assert item["cost_usd"] == 0.08 and item["priced"] is True


def test_reprice_dry_run_does_not_write(load_admin):
    adm = load_admin()
    adm.qk_record_usage({"id": "u1", "name": "U", "email": "u@x.com"}, "dall-e-3",
                        {"cached": 0.0, "input": 0.0, "output": 0.0}, images=2)
    _seed_pricing(adm, {"dall-e-3": DALLE_PRICE})
    rep = adm.qk_reprice_ledger(days=30, dry_run=True)
    assert rep["buckets_repriced"] == 1
    mm = list(list(_led(adm)["users"]["u1"]["days"].values())[0]["models"].values())[0]
    assert mm["cost_usd"] == 0.0 and mm["unpriced_requests"] == 1


# ---- request body extraction -----------------------------------------------------


def test_ingest_image_request_info_json(load_admin):
    adm = load_admin()
    body = json.dumps({"model": "dall-e-3", "prompt": "a cat", "n": 2}).encode()
    info = adm.qk_ingest_image_request_info(body, "application/json")
    assert info == {"model": "dall-e-3", "n": 2}
    # n omitted -> 1
    info = adm.qk_ingest_image_request_info(
        json.dumps({"model": "dall-e-3"}).encode(), "application/json")
    assert info == {"model": "dall-e-3", "n": 1}
    # n clamped to a sane bound
    info = adm.qk_ingest_image_request_info(
        json.dumps({"model": "m", "n": 9999}).encode(), "application/json")
    assert info["n"] == 64


def test_ingest_image_request_info_multipart(load_admin):
    adm = load_admin()
    boundary = "----qkboundary"
    parts = []
    for name, value in (("model", "dall-e-2"), ("prompt", "make it blue"),
                        ("n", "3"), ("size", "1024x1024")):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n')
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
                 f'filename="in.png"\r\nContent-Type: image/png\r\n\r\n\x89PNG\r\n')
    parts.append(f"--{boundary}--\r\n")
    body = "".join(parts).encode("utf-8", "ignore")
    info = adm.qk_ingest_image_request_info(body, f"multipart/form-data; boundary={boundary}")
    assert info == {"model": "dall-e-2", "n": 3}


def test_ingest_image_request_info_garbage(load_admin):
    adm = load_admin()
    assert adm.qk_ingest_image_request_info(b"", "application/json") == {"model": "", "n": 1}
    assert adm.qk_ingest_image_request_info(b"not json", "application/json") == {"model": "", "n": 1}


# ---- middleware end-to-end -------------------------------------------------------


def test_middleware_records_image_generation_per_image(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    _seed_pricing(adm, {"dall-e-3": DALLE_PRICE})
    # dall-e style response: no usage, no model echo
    resp_body = json.dumps({"created": 1, "data": [{"b64_json": "AAAA"}]}).encode()
    c = _mk_image_app(adm, "/openai/images/generations", resp_body=resp_body)
    resp = c.post("/openai/images/generations",
                  json={"model": "dall-e-3", "prompt": "a cat", "n": 2})
    assert resp.status_code == 200
    led = _led(adm)
    d = list(led["users"]["u1"]["days"].values())[0]
    mm = d["models"]["dall-e-3"]
    assert mm["requests"] == 1
    assert mm["cost_usd"] == 0.08
    assert mm["images"] == 2
    assert d["channels"] == {"webui": 0, "api": 1}


def test_middleware_records_image_generation_token_usage(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    _seed_pricing(adm, {"gpt-image-1": GPT_IMAGE_PRICE})
    # gpt-image-1 style response: token usage present
    resp_body = json.dumps({
        "created": 1, "data": [{"b64_json": "AAAA"}],
        "usage": {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
    }).encode()
    c = _mk_image_app(adm, "/openai/images/generations", resp_body=resp_body)
    resp = c.post("/openai/images/generations",
                  json={"model": "gpt-image-1", "prompt": "a cat"})
    assert resp.status_code == 200
    mm = list(list(_led(adm)["users"]["u1"]["days"].values())[0]["models"].values())[0]
    # billed per token (0.005 + 0.008); per_image must not double-bill
    assert mm["cost_usd"] == 0.013
    assert mm["tokens"]["input"] == 1000.0 and mm["tokens"]["output"] == 200.0
    assert mm["images"] == 1


def test_middleware_records_image_edit_multipart(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    _seed_pricing(adm, {"dall-e-2": {"input": None, "output": None, "cached": None,
                                     "cache_write": None, "per_image": 0.02}})
    resp_body = json.dumps({"created": 1, "data": [{"b64_json": "AAAA"}]}).encode()
    c = _mk_image_app(adm, "/openai/images/edits", resp_body=resp_body)
    resp = c.post("/openai/images/edits",
                  data={"model": "dall-e-2", "prompt": "make it blue", "n": "3"},
                  files={"image": ("in.png", b"\x89PNG\r\n", "image/png")})
    assert resp.status_code == 200
    mm = list(list(_led(adm)["users"]["u1"]["days"].values())[0]["models"].values())[0]
    assert mm["requests"] == 1
    assert mm["cost_usd"] == 0.06  # 3 x $0.02
    assert mm["images"] == 3


def test_middleware_records_webui_images_path(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    _seed_pricing(adm, {"dall-e-3": DALLE_PRICE})
    resp_body = json.dumps({"created": 1, "data": [{"url": "https://x/i.png"}]}).encode()
    c = _mk_image_app(adm, "/api/v1/images/generations", resp_body=resp_body)
    resp = c.post("/api/v1/images/generations",
                  json={"model": "dall-e-3", "prompt": "a cat"})
    assert resp.status_code == 200
    mm = list(list(_led(adm)["users"]["u1"]["days"].values())[0]["models"].values())[0]
    assert mm["cost_usd"] == 0.04 and mm["images"] == 1


def test_middleware_response_body_preserved(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    resp_body = json.dumps({"created": 1, "data": [{"b64_json": "AAAA"}]}).encode()
    c = _mk_image_app(adm, "/openai/images/generations", resp_body=resp_body)
    resp = c.post("/openai/images/generations", json={"model": "dall-e-3"})
    assert resp.status_code == 200
    # test-double route re-serializes the body; compare content, not bytes
    assert resp.json() == json.loads(resp_body)
