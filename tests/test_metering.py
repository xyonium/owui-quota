# tests/test_metering.py
import pytest


def _user(uid="u1"):
    return {"id": uid, "name": "U", "email": "u@x.com", "role": "user"}


def test_orphan_adopted_when_outlet_has_usage(qk):
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream({"id": "r1", "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                          "model": "gpt-4o"}, __user__=None, __metadata__={}))
    asyncio.run(f.outlet({"id": "r1", "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                          "model": "gpt-4o"}, __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {"users": {}})
    d = led["users"]["u1"]["days"]
    day = list(d)[0]
    assert d[day]["requests"] == 1          # exactly once
    assert d[day]["tokens"]["input"] == 100


def test_orphan_adopted_when_outlet_no_usage(qk):
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream({"id": "r2", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                          "model": "gpt-4o"}, __user__=None, __metadata__={}))
    asyncio.run(f.outlet({"id": "r2", "model": "gpt-4o"}, __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {"users": {}})
    day = list(led["users"]["u1"]["days"])[0]
    assert led["users"]["u1"]["days"][day]["requests"] == 1


def test_block_message_bad_template_still_blocks(qk):
    f = qk.Filter()
    f.valves.block_message = "quota {used} of {quota} — contact {admin}"
    # The brief's literal never set a quota: default_quota_credits=None means
    # unlimited, so inlet returns before the block path and the test is
    # vacuous. Set a quota so the used>=eff branch is actually reached.
    cfg = qk.qk_get_config()
    cfg["default_quota_credits"] = 1000.0
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    qk.qk_atomic_write(qk.QK_LEDGER_PATH, {"users": {"u1": {"days": {qk.qk_local_now(qk.qk_get_config()).strftime("%Y-%m-%d"): {"cost_usd": 999.0}}}}})
    with pytest.raises(qk.QuotaBlocked):
        import asyncio
        asyncio.run(f.inlet({"model": "gpt-4o"}, __user__=_user(), __metadata__={}))


def test_schedule_multipliers_removed_always_one(qk):
    # v0.4.0 removed the night/weekend quota multipliers: legacy config keys
    # are ignored and the effective quota never varies by time of day.
    cfg = qk.qk_get_config()
    cfg["schedule"] = {"night_multiplier": 0.0, "weekend_multiplier": 0.0,
                       "night_start_hour": 0, "night_end_hour": 23}
    assert qk.qk_time_multiplier(cfg) == 1.0


def test_bool_quota_rejected(qk):
    cfg = qk.qk_get_config()
    cfg["user_quotas"]["u1"] = True
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    quota, src = qk.qk_resolve_quota(cfg, _user())
    assert quota is None and src == "none"


def test_unpriced_requests_counter_selfheals(qk, monkeypatch):
    # no pricing cache -> unpriced_requests=1
    qk.qk_record_usage(_user(), "totally-unknown", {"cached": 0, "input": 10, "output": 5, "cache_write": 0})
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    day = list(led["users"]["u1"]["days"])[0]
    mm = led["users"]["u1"]["days"][day]["models"]["totally-unknown"]
    assert mm["unpriced_requests"] == 1
    # now price appears -> counter stays but new request priced
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"totally-unknown": {"input": 1, "output": 2}}})
    qk.qk_record_usage(_user(), "totally-unknown", {"cached": 0, "input": 10, "output": 5, "cache_write": 0})
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    mm = led["users"]["u1"]["days"][day]["models"]["totally-unknown"]
    assert mm["unpriced_requests"] == 1 and mm["requests"] == 2


def test_anthropic_partial_usage_merged(qk):
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream({"id": "r3", "message": {"usage": {"input_tokens": 40}},
                          "model": "claude-x"}, __user__=_user(), __metadata__={}))
    asyncio.run(f.stream({"id": "r3", "usage": {"output_tokens": 7},  # brief's literal had an extra "}" here
                          "model": "claude-x"}, __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    day = list(led["users"]["u1"]["days"])[0]
    d = led["users"]["u1"]["days"][day]
    assert d["requests"] == 1
    assert d["tokens"]["input"] == 40 and d["tokens"]["output"] == 7
    # the topup (count_request=False) is a merge, not a response: recent.json
    # must contain exactly the first partial's single entry
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {"items": []})
    assert len(rec["items"]) == 1
    assert rec["items"][0]["model"] == "claude-x"


def test_stream_sse_string_with_usage_prefix(qk):
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream('data: {"id":"r4","usage":{"prompt_tokens":8,"completion_tokens":3},"model":"gpt-4o"}',
                         __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    day = list(led["users"]["u1"]["days"])[0]
    assert led["users"]["u1"]["days"][day]["requests"] == 1


# ---- webui/api channel split (v0.3.1) ------------------------------------------


def test_channels_recorded_per_day_and_model(qk, tmp_path):
    from pathlib import Path
    from tests.conftest import write_json

    u = {"id": "u1", "name": "A", "email": "a@x"}
    qk.qk_record_usage(u, "m/x", {"cached": 1, "input": 2, "output": 3, "cache_write": 0}, channel="webui")
    qk.qk_record_usage(u, "m/x", {"cached": 1, "input": 2, "output": 3, "cache_write": 0}, channel="webui")
    qk.qk_record_usage(u, "m/x", {"cached": 1, "input": 2, "output": 3, "cache_write": 0})  # default api
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    day = next(iter(led["users"]["u1"]["days"].values()))
    assert day["channels"] == {"webui": 2, "api": 1}
    assert day["models"]["m/x"]["channels"] == {"webui": 2, "api": 1}
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {})
    assert [i["channel"] for i in rec["items"]] == ["webui", "webui", "api"]


def test_stats_channels_aggregated(load_admin):
    from pathlib import Path
    from tests.conftest import write_json

    adm = load_admin()
    write_json(Path(adm.QK_LEDGER_PATH), {"users": {"u1": {"name": "A", "email": "a@x", "days": {
        "2026-08-19": {
            "requests": 3, "cost_usd": 0.0,
            "channels": {"webui": 2, "api": 1},
            "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
            "models": {"m/x": {"requests": 3, "channels": {"webui": 2, "api": 1},
                                "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0}}},
        },
    }}}})
    out = adm.qk_stats()
    assert out["users"][0]["channels"] == {"webui": 2, "api": 1}
    # legacy ledger rows without the channels field aggregate to zero, not a crash
    write_json(Path(adm.QK_LEDGER_PATH), {"users": {"u1": {"name": "A", "email": "a@x", "days": {
        "2026-08-19": {"requests": 1, "cost_usd": 0.0, "models": {}},
    }}}})
    out = adm.qk_stats()
    assert out["users"][0]["channels"] == {"webui": 0, "api": 0}


# ---- OWUI 0.11 outlet body shape (rebuilt message list) -------------------------
#
# 0.11 rebuilds the outlet body: usage lives ONLY on the last assistant message
# (messages[-1].usage); there is no top-level body["usage"] and no "choices".
# The non-streaming API path (/api/v1/messages, /openai/responses) reaches outlet
# exclusively through this shape, so a filter that only reads the top level /
# choices[0] silently records nothing for every non-streaming API request.


def _011_body(usage):
    return {
        "model": "glm-5.3",
        "messages": [
            {"id": "u1", "role": "user", "content": "hi", "info": None, "timestamp": 1},
            {"id": "msg-1", "role": "assistant", "content": "ok", "usage": usage},
        ],
        "filter_ids": [],
        "chat_id": "",
        "session_id": "sess-1",
        "id": "msg-1",
    }


def test_outlet_records_usage_from_last_message(qk):
    """0.11.0 non-streaming API body: usage on messages[-1] must be recorded."""
    f = qk.Filter()
    import asyncio
    body = _011_body({"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140})
    md = {"chat_id": "", "session_id": "sess-1", "message_id": "msg-1", "user_id": "u1"}
    asyncio.run(f.outlet(body, __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1
    assert d["tokens"]["input"] == 100
    assert d["tokens"]["output"] == 40
    # empty chat_id -> api channel
    assert d["channels"] == {"webui": 0, "api": 1}
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {})
    assert rec["items"][0]["channel"] == "api"


def test_outlet_anthropic_usage_shape_in_message(qk):
    """0.11.0 Anthropic-normalized usage (input/output_tokens) on the message."""
    f = qk.Filter()
    import asyncio
    body = _011_body({"input_tokens": 11, "output_tokens": 7,
                      "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2})
    md = {"chat_id": "", "session_id": "s", "message_id": "msg-1", "user_id": "u1"}
    asyncio.run(f.outlet(body, __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1
    assert d["tokens"]["input"] == 11
    assert d["tokens"]["cached"] == 3


def test_outlet_toplevel_usage_still_wins(qk):
    """Regression guard: the pre-0.11 top-level usage shape must keep working."""
    f = qk.Filter()
    import asyncio
    body = {"id": "r9", "model": "gpt-4o",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
    asyncio.run(f.outlet(body, __user__=_user(), __metadata__={"chat_id": "chat-9"}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1
    assert d["tokens"]["input"] == 5
    assert d["channels"] == {"webui": 1, "api": 0}


def test_stream_then_outlet_does_not_double_record(qk):
    """0.11.0 calls outlet_filter_handler at the END of a streaming chat too.
    The upstream stream chunk echoes the ALIAS (prx.gemini-flash); the rebuilt
    outlet body carries the message id (not the response id) and usage on the
    last message. Without the message-id mark + model_name preference, the
    request is recorded twice (real name by stream, alias by outlet). OWUI
    injects the same __metadata__ (with model_name) into both calls."""
    f = qk.Filter()
    import asyncio
    md = {"chat_id": "chat-9", "session_id": "s", "message_id": "msg-1",
          "user_id": "u1", "model_name": "gemini-3.7-flash"}
    asyncio.run(f.stream({"id": "chatcmpl-ABC", "model": "prx.gemini-flash",
                          "usage": {"prompt_tokens": 4226, "completion_tokens": 756}},
                         __user__=_user(), __metadata__=md))
    outlet_body = {"model": "prx.gemini-flash", "id": "msg-1",
                   "messages": [{"role": "user", "content": "hi"},
                                {"id": "msg-1", "role": "assistant", "content": "ok",
                                 "usage": {"prompt_tokens": 4226, "completion_tokens": 756}}],
                   "chat_id": "chat-9", "session_id": "s"}
    asyncio.run(f.outlet(outlet_body, __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1                      # not 2
    assert set(d["models"]) == {"gemini-3.7-flash"}  # real name only, no alias row
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {})
    assert len(rec["items"]) == 1
    assert rec["items"][0]["model"] == "gemini-3.7-flash"


def test_stream_then_outlet_topup_reuses_real_name_without_model_name(qk, tmp_path):
    """Regression (v0.5.2): stream() records the real name; the stream-end
    outlet topup gets metadata WITHOUT model_name and an outlet body whose
    model echoes the alias (prx.gemini-flash). The topup must merge into the
    already-recorded real-name row, not create a phantom alias row with
    req=0 + unpriced."""
    from tests.conftest import write_json
    from pathlib import Path
    write_json(Path(qk.QK_PRICING_PATH), {"table": {
        "gemini-flash": {"input": 0.5, "output": 1.5, "cached": 0.1, "cache_write": 0.1}}})
    f = qk.Filter()
    import asyncio
    md = {"chat_id": "chat-9", "session_id": "s", "message_id": "msg-1",
          "user_id": "u1"}  # NO model_name (production metadata)
    asyncio.run(f.stream({"id": "chatcmpl-ABC", "model": "prx.gemini-flash",
                          "usage": {"prompt_tokens": 4226, "completion_tokens": 756}},
                         __user__=_user(), __metadata__=md))
    outlet_body = {"model": "prx.gemini-flash", "id": "msg-1",
                   "messages": [{"role": "user", "content": "hi"},
                                {"id": "msg-1", "role": "assistant", "content": "ok",
                                 "usage": {"prompt_tokens": 4226, "completion_tokens": 756}}],
                   "chat_id": "chat-9", "session_id": "s"}
    asyncio.run(f.outlet(outlet_body, __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1                      # not 2
    # stream() had no model_name so it recorded the prx-stripped fallback
    # (gemini-flash); the topup must reuse that same name, not prx.gemini-flash
    assert set(d["models"]) == {"gemini-flash"}     # no phantom alias row
    mm = d["models"]["gemini-flash"]
    assert mm["unpriced_requests"] == 0            # no phantom unpriced
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {})
    assert len(rec["items"]) == 1
    assert rec["items"][0]["model"] == "gemini-flash"


def test_outlet_orphan_alias_model_stripped(qk, tmp_path):
    """Regression (v0.5.2): outlet with an unmatched message id (metadata
    model_name missing, stream() mark never saw this id) records the request
    directly. The body.model echoes the alias prx.*; it must be stripped to
    the underlying name so the row is priced, not a phantom unpriced alias."""
    # price the stripped name so unpriced_requests must be 0 after the fix
    from tests.conftest import write_json
    from pathlib import Path
    write_json(Path(qk.QK_PRICING_PATH), {"table": {
        "gemini-flash": {"input": 0.5, "output": 1.5, "cached": 0.1, "cache_write": 0.1}}})
    f = qk.Filter()
    import asyncio
    md = {"chat_id": "", "session_id": "s", "message_id": "msg-9", "user_id": "u1"}
    body = {"model": "prx.gemini-flash", "id": "msg-9",
            "messages": [{"role": "user", "content": "hi"},
                         {"id": "msg-9", "role": "assistant", "content": "ok",
                          "usage": {"prompt_tokens": 11, "completion_tokens": 7}}],
            "chat_id": "", "session_id": "s"}
    asyncio.run(f.outlet(body, __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert set(d["models"]) == {"gemini-flash"}
    assert d["models"]["gemini-flash"]["unpriced_requests"] == 0
    assert d["channels"] == {"webui": 0, "api": 1}


def test_outlet_message_usage_falls_back_to_body_model(qk):
    """Non-streaming 0.11 body still records via messages[-1].usage. OWUI's
    metadata has no model_name field, so the model name comes from body.model
    (whatever name the response carries). If a deployment injects model_name,
    it wins (kept as the preferred source for naming consistency)."""
    f = qk.Filter()
    import asyncio
    # production metadata: no model_name key at all
    md = {"chat_id": "", "session_id": "s", "message_id": "msg-1", "user_id": "u1"}
    body = {"model": "prx.gemini-flash", "id": "msg-1",
            "messages": [{"role": "user", "content": "hi"},
                         {"id": "msg-1", "role": "assistant", "content": "ok",
                          "usage": {"prompt_tokens": 11, "completion_tokens": 7}}],
            "chat_id": "", "session_id": "s"}
    asyncio.run(f.outlet(body, __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1
    # v0.5.2: the body.model fallback strips the provider prefix (prx.*) so
    # the alias never pollutes the ledger as a phantom unpriced model
    assert set(d["models"]) == {"gemini-flash"}   # body.model fallback, prx stripped
    assert d["channels"] == {"webui": 0, "api": 1}
    # model_name (when present) takes precedence
    f2 = qk.Filter()
    md2 = dict(md, model_name="gemini-3.7-flash")
    asyncio.run(f2.outlet(dict(body, id="msg-2"), __user__=_user(), __metadata__=md2))
    led2 = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d2 = list(led2["users"]["u1"]["days"].values())[0]
    assert set(d2["models"]) == {"gemini-flash", "gemini-3.7-flash"}
