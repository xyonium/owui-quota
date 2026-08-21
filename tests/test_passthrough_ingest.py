# tests/test_passthrough_ingest.py
"""Passthrough ingestion middleware: meters direct /api/v1/messages and
/openai/responses requests (pure upstream proxy, no filter invocation)."""
import asyncio
import json
from pathlib import Path
from tests.conftest import write_json


def _user(u="u1", email="u@x.com"):
    return {"id": u, "name": "U", "email": email, "role": "user"}


def _led(adm):
    return json.loads(Path(adm.QK_LEDGER_PATH).read_text())


def _rec(adm):
    return json.loads(Path(adm.QK_RECENT_PATH).read_text())


# ---- normalize: Responses API shapes -----------------------------------------


def test_normalize_responses_usage_details(load_admin):
    adm = load_admin()
    u = {"input_tokens": 12, "output_tokens": 4,
         "input_tokens_details": {"cached_tokens": 5}}
    out = adm.qk_normalize_usage(u)
    assert out == {"cached": 5.0, "input": 7.0, "output": 4.0, "cache_write": 0.0}


def test_normalize_anthropic_input_excludes_cache(load_admin):
    adm = load_admin()
    u = {"input_tokens": 12, "output_tokens": 4, "cache_read_input_tokens": 5}
    out = adm.qk_normalize_usage(u)
    # anthropic: input_tokens excludes cache -> no subtraction
    assert out == {"cached": 5.0, "input": 12.0, "output": 4.0, "cache_write": 0.0}


def test_normalize_openai_style_unchanged(load_admin):
    adm = load_admin()
    u = {"prompt_tokens": 20, "completion_tokens": 8,
         "prompt_tokens_details": {"cached_tokens": 6}}
    out = adm.qk_normalize_usage(u)
    assert out == {"cached": 6.0, "input": 14.0, "output": 8.0, "cache_write": 0.0}


# ---- extraction: message/body shapes ------------------------------------------


def test_extract_nonstream_anthropic_messages(load_admin):
    adm = load_admin()
    data = {"type": "message", "model": "claude-x",
            "usage": {"input_tokens": 11, "output_tokens": 7,
                      "cache_read_input_tokens": 3}}
    out = adm.qk_ingest_extract_usage(data)
    assert out["input"] == 11 and out["cached"] == 3 and out["output"] == 7


def test_extract_responses_completed(load_admin):
    adm = load_admin()
    data = {"type": "response.completed", "response": {
        "id": "resp-1", "usage": {"input_tokens": 12, "output_tokens": 4,
                                  "input_tokens_details": {"cached_tokens": 5}}}}
    out = adm.qk_ingest_extract_usage(data)
    assert out["input"] == 7 and out["cached"] == 5


def test_extract_openai_completion_choices(load_admin):
    adm = load_admin()
    data = {"choices": [{"message": {"role": "assistant"},
                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}}]}
    out = adm.qk_ingest_extract_usage(data)
    assert out["input"] == 5 and out["output"] == 2


def test_extract_none_when_no_usage(load_admin):
    adm = load_admin()
    assert adm.qk_ingest_extract_usage({"type": "ping"}) is None
    assert adm.qk_ingest_extract_usage(None) is None


# ---- SSE scanning -------------------------------------------------------------


ANTHROPIC_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":10,'
    '"cache_read_input_tokens":2}}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"usage":{"output_tokens":7}}}\n\n'
    'data: [DONE]\n\n'
).encode()


def test_scan_anthropic_sse_merges_start_and_delta(load_admin):
    adm = load_admin()
    _m, out = adm.qk_ingest_scan_sse(iter([ANTHROPIC_SSE]))
    assert out == {"cached": 2.0, "input": 10.0, "output": 7.0, "cache_write": 0.0}
    assert _m == "claude-x"


def test_scan_sse_split_chunks(load_admin):
    adm = load_admin()
    chunks = [ANTHROPIC_SSE[i:i + 7] for i in range(0, len(ANTHROPIC_SSE), 7)]
    _m, out = adm.qk_ingest_scan_sse(iter(chunks))
    assert out["output"] == 7.0 and out["input"] == 10.0


def test_scan_responses_sse_completed(load_admin):
    adm = load_admin()
    sse = (
        'data: {"type":"response.created"}\n\n'
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":12,'
        '"output_tokens":4,"input_tokens_details":{"cached_tokens":5}}}}\n\n'
        'data: [DONE]\n\n'
    ).encode()
    _m, out = adm.qk_ingest_scan_sse(iter([sse]))
    assert out["input"] == 7 and out["cached"] == 5


# ---- middleware integration ---------------------------------------------------


def _mk_app(adm, path, resp_body=b"", stream=False, user=None):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware
    from types import SimpleNamespace

    app = FastAPI()
    # user=None -> default authenticated user; user=False -> no user at all
    auth_user = SimpleNamespace(id="u1", name="U", email="u@x.com", role="user") \
        if user is None else user

    async def route(request: Request):
        if stream:
            return StreamingResponse(iter([resp_body]), media_type="text/event-stream")
        data = json.loads(resp_body) if resp_body else {"ok": 1}
        if "usage" in data:
            return JSONResponse(data)
        # wrap so the middleware sees a usage-bearing body (like a real
        # /messages or /responses passthrough response)
        return JSONResponse({"type": "message", "model": data.get("model", "claude-x"),
                             "usage": data.get("usage") or {"input_tokens": 1, "output_tokens": 1}})

    app.add_api_route(path, route, methods=["POST"])

    # outermost: simulate OWUI's AuthTokenMiddleware populating state.user
    if auth_user is not False:
        async def auth_mw(request: Request, call_next):
            request.state.user = auth_user
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=auth_mw)
    app.add_middleware(BaseHTTPMiddleware, dispatch=adm.qk_passthrough_middleware)
    return TestClient(app)


def _stub_owui_auth(monkeypatch):
    """Provide the open_webui.auth stub for admin import (qk_passthrough
    middleware references request.state.user only, but the admin module's
    auth dependency import needs the module to exist). get_current_user
    mimics OWUI's signature: resolves the user from request.state.token."""
    import sys, types
    from types import SimpleNamespace

    ow = types.ModuleType("open_webui")
    utils = types.ModuleType("open_webui.utils")
    auth = types.ModuleType("open_webui.utils.auth")

    async def get_current_user(request, response=None, background_tasks=None, auth_token=None):
        token = getattr(request.state, "token", None)
        if auth_token is not None:
            token = auth_token
        if token is None:
            raise RuntimeError("no token")
        return SimpleNamespace(id="u1", name="U", email="u@x.com", role="user")

    def get_verified_user(user):
        return user

    auth.get_current_user = get_current_user
    auth.get_verified_user = get_verified_user
    utils.auth = auth
    ow.utils = utils
    monkeypatch.setitem(sys.modules, "open_webui", ow)
    monkeypatch.setitem(sys.modules, "open_webui.utils", utils)
    monkeypatch.setitem(sys.modules, "open_webui.utils.auth", auth)


def test_middleware_records_nonstream_messages(load_admin, monkeypatch, tmp_path):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    body = json.dumps({"type": "message", "model": "claude-x",
                       "usage": {"input_tokens": 11, "output_tokens": 7,
                                 "cache_read_input_tokens": 3}}).encode()
    c = _mk_app(adm, "/api/v1/messages", resp_body=body)
    # simulate OWUI AuthTokenMiddleware populating request.state.user
    from fastapi.testclient import TestClient
    resp = c.post("/api/v1/messages", json={"model": "claude-x"},
                  headers={"authorization": "Bearer x"})
    assert resp.status_code == 200
    led = _led(adm)
    d = list((led["users"].get("u1") or {}).get("days", {}).values())
    assert d and d[0]["requests"] == 1
    assert d[0]["channels"] == {"webui": 0, "api": 1}


def test_middleware_records_streaming_messages(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    c = _mk_app(adm, "/api/v1/messages", resp_body=ANTHROPIC_SSE, stream=True)
    resp = c.post("/api/v1/messages", json={})
    assert resp.status_code == 200
    led = _led(adm)
    d = list((led["users"].get("u1") or {}).get("days", {}).values())
    assert d and d[0]["requests"] == 1
    assert d[0]["tokens"]["input"] == 10 and d[0]["tokens"]["output"] == 7


def test_middleware_ignores_non_ingest_path(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    body = json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
    c = _mk_app(adm, "/api/chat/completions", resp_body=body)
    resp = c.post("/api/chat/completions", json={})
    assert resp.status_code == 200
    # path not in QK_INGEST_PATHS -> nothing recorded
    assert not Path(adm.QK_LEDGER_PATH).exists()


def test_middleware_skips_when_no_user(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    body = json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
    c = _mk_app(adm, "/openai/responses", resp_body=body, user=False)
    resp = c.post("/openai/responses", json={})
    assert resp.status_code == 200
    # no authenticated user -> nothing recorded (can't attribute)
    assert not Path(adm.QK_LEDGER_PATH).exists()


def test_middleware_stream_body_preserved(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    payload = b'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\n\n'
    c = _mk_app(adm, "/api/v1/messages", resp_body=payload, stream=True)
    resp = c.post("/api/v1/messages", json={})
    assert resp.status_code == 200
    assert resp.content == payload


def test_middleware_reads_user_set_by_route_depends(load_admin, monkeypatch):
    """OWUI's routes resolve the user via Depends(get_verified_user), which
    sets request.state.user (auth.py:360/411). Our middleware runs after the
    route, so the response-side ingest record must see that user without
    re-resolving the token itself."""
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    body = json.dumps({"type": "message", "model": "claude-x",
                       "usage": {"input_tokens": 11, "output_tokens": 7}}).encode()

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware
    from types import SimpleNamespace

    app = FastAPI()

    async def route(request: Request):
        # OWUI's Depends(get_verified_user) would have set this already
        request.state.user = SimpleNamespace(id="u1", name="U", email="u@x.com", role="user")
        return JSONResponse(json.loads(body))

    app.add_api_route("/api/v1/messages", route, methods=["POST"])
    app.add_middleware(BaseHTTPMiddleware, dispatch=adm.qk_passthrough_middleware)

    with TestClient(app) as c:
        resp = c.post("/api/v1/messages", json={"model": "claude-x"})
        assert resp.status_code == 200

    led = _led(adm)
    d = list((led["users"].get("u1") or {}).get("days", {}).values())
    assert d and d[0]["requests"] == 1
    assert d[0]["channels"] == {"webui": 0, "api": 1}


def test_middleware_records_nonstream_messages(load_admin, monkeypatch, tmp_path):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    body = json.dumps({"type": "message", "model": "claude-x",
                       "usage": {"input_tokens": 11, "output_tokens": 7,
                                 "cache_read_input_tokens": 3}}).encode()
    c = _mk_app(adm, "/api/v1/messages", resp_body=body)
    # simulate OWUI AuthTokenMiddleware populating request.state.user
    from fastapi.testclient import TestClient
    resp = c.post("/api/v1/messages", json={"model": "claude-x"},
                  headers={"authorization": "Bearer x"})
    assert resp.status_code == 200
    led = _led(adm)
    d = list((led["users"].get("u1") or {}).get("days", {}).values())
    assert d and d[0]["requests"] == 1
    assert d[0]["channels"] == {"webui": 0, "api": 1}


def test_middleware_records_streaming_messages(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    c = _mk_app(adm, "/api/v1/messages", resp_body=ANTHROPIC_SSE, stream=True)
    resp = c.post("/api/v1/messages", json={})
    assert resp.status_code == 200
    led = _led(adm)
    d = list((led["users"].get("u1") or {}).get("days", {}).values())
    assert d and d[0]["requests"] == 1
    assert d[0]["tokens"]["input"] == 10 and d[0]["tokens"]["output"] == 7


def test_middleware_ignores_non_ingest_path(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    body = json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
    c = _mk_app(adm, "/api/chat/completions", resp_body=body)
    resp = c.post("/api/chat/completions", json={})
    assert resp.status_code == 200
    # path not in QK_INGEST_PATHS -> nothing recorded
    assert not Path(adm.QK_LEDGER_PATH).exists()


def test_middleware_skips_when_no_user(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    body = json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
    c = _mk_app(adm, "/openai/responses", resp_body=body, user=False)
    resp = c.post("/openai/responses", json={})
    assert resp.status_code == 200
    # no authenticated user -> nothing recorded (can't attribute)
    assert not Path(adm.QK_LEDGER_PATH).exists()


def test_middleware_stream_body_preserved(load_admin, monkeypatch):
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    payload = b'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\n\n'
    c = _mk_app(adm, "/api/v1/messages", resp_body=payload, stream=True)
    resp = c.post("/api/v1/messages", json={})
    assert resp.status_code == 200
    assert resp.content == payload




def test_middleware_does_not_consume_request_body(load_admin, monkeypatch):
    """Regression (v0.5.3): reading request.body() in the middleware emptied
    it for the passthrough route, breaking the forwarded model. The body must
    be re-injected (request._body) so the route still sees the payload."""
    _stub_owui_auth(monkeypatch)
    adm = load_admin()
    body = json.dumps({"type": "message", "model": "prx.gemini-flash",
                       "usage": {"input_tokens": 11, "output_tokens": 7}}).encode()

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware
    import json as j

    app = FastAPI()
    seen = {}

    async def route(request: Request):
        req_body = await request.body()
        seen["model"] = j.loads(req_body).get("model")
        return JSONResponse(j.loads(body))

    app.add_api_route("/api/v1/messages", route, methods=["POST"])
    app.add_middleware(BaseHTTPMiddleware, dispatch=adm.qk_passthrough_middleware)

    with TestClient(app) as c:
        resp = c.post("/api/v1/messages", json={"model": "prx.gemini-flash"})
        assert resp.status_code == 200
    assert seen["model"] == "prx.gemini-flash"


def test_extract_anthropic_full_usage_with_cache_write(load_admin):
    """/api/v1/messages (Anthropic protocol) response carries cache details:
    cache_read_input_tokens -> cached, cache_creation_input_tokens ->
    cache_write. Both must be extracted for cost accounting."""
    adm = load_admin()
    resp = {"id": "msg_x", "type": "message", "model": "prx.gemini-flash",
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "cache_read_input_tokens": 30,
                      "cache_creation_input_tokens": 20}}
    tok = adm.qk_ingest_extract_usage(resp)
    assert tok == {"cached": 30.0, "input": 100.0, "output": 50.0, "cache_write": 20.0}
