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


# ---- model_aliases (v0.5.6) ---------------------------------------------------


def test_model_alias_maps_name_before_pricing(qk, tmp_path):
    """prx.gemini-flash -> gemini-3.7-flash: the alias maps to the real name
    BEFORE price matching, so the record prices under the real name and the
    ledger has no alias row."""
    from pathlib import Path
    from tests.conftest import write_json
    from pathlib import Path
    write_json(Path(qk.QK_PRICING_PATH), {"table": {
        "gemini-3.7-flash": {"input": 0.5, "output": 1.5, "cached": 0.1, "cache_write": 0.1}}})
    cfg = qk.qk_get_config()
    cfg["model_aliases"] = {"prx.gemini-flash": "gemini-3.7-flash"}
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u@x.com"},
                       "prx.gemini-flash",
                       {"cached": 0, "input": 10, "output": 5, "cache_write": 0})
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert set(d["models"]) == {"gemini-3.7-flash"}   # no prx.* row
    mm = d["models"]["gemini-3.7-flash"]
    assert mm["unpriced_requests"] == 0               # priced under real name
    assert mm["cost_usd"] > 0


def test_model_alias_unmapped_passes_through(qk, tmp_path):
    """glm-5.3 (priced via override to glm-5.2) is NOT in model_aliases: it
    must stay its own ledger row, not merge into the alias target."""
    from pathlib import Path
    from tests.conftest import write_json
    cfg = qk.qk_get_config()
    cfg["model_aliases"] = {"prx.gemini-flash": "gemini-3.7-flash"}
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u@x.com"},
                       "glm-5.3",
                       {"cached": 0, "input": 10, "output": 5, "cache_write": 0})
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert set(d["models"]) == {"glm-5.3"}            # untouched


def test_model_aliases_validated(load_admin):
    adm = load_admin()
    assert not adm.qk_validate_config({"model_aliases": {"prx.x": "gemini-3.7-flash"}})
    assert adm.qk_validate_config({"model_aliases": {"prx.x": 42}})         # non-str value
    assert adm.qk_validate_config({"model_aliases": "oops"})                # non-dict


def test_quota_period_weekly_accepted(qk, load_admin):
    # v0.4.16: weekly joins daily|monthly as a valid quota_period on BOTH files
    # (shared validation block must stay in sync).
    for mod in (qk, load_admin()):
        assert not mod.qk_validate_config({"quota_period": "weekly"})
        assert mod.qk_validate_config({"quota_period": "fortnightly"})  # still rejected


def test_period_used_weekly_sums_iso_week(qk, monkeypatch, tmp_path):
    # weekly period sums Mon..today of the ISO week only -- last Sunday (prev
    # week) and the same-weekday-last-week are excluded.
    # "now" is pinned to a fixed Wednesday: building the fixture from the real
    # today makes the monday/today dict keys collide whenever today IS a
    # Monday ({monday: 2.0} silently overwritten by {today: 3.0}), so the test
    # used to fail one day per week.
    from datetime import datetime, timedelta
    from tests.conftest import write_json

    now = datetime(2026, 8, 19, 12, 0)  # a Wednesday
    monkeypatch.setattr(qk, "qk_local_now", lambda cfg=None: now)
    cfg = qk.qk_get_config()
    cfg["quota_period"] = "weekly"
    monday = now - timedelta(days=now.weekday())
    last_sunday = monday - timedelta(days=1)
    days = {
        monday.strftime("%Y-%m-%d"): {"cost_usd": 2.0},        # in-week
        now.strftime("%Y-%m-%d"): {"cost_usd": 3.0},           # today, in-week
        last_sunday.strftime("%Y-%m-%d"): {"cost_usd": 99.0},  # prev week, excluded
    }
    write_json(tmp_path / "quota_keeper" / "ledger.json", {"users": {"u1": {"days": days}}})
    qk.JC._store.clear()  # mtime cache: force re-read of the fresh ledger
    assert qk.qk_period_used_usd("u1", cfg) == pytest.approx(5.0)


# --- Shared-helper sync guards (run against BOTH modules) -------------------
# Regression: the filter shipped without defining QK_PRICE_FIELDS (it was only
# defined in the admin module), so every qk_find_pricing call that consulted an
# override raised NameError -- silently swallowed by the filter's fail-open
# handler, so usage was never metered. These exercise the override paths on the
# filter module too, which the rest of the suite only ran against admin.


def test_price_fields_constant_defined_in_both(qk, load_admin):
    for mod in (qk, load_admin()):
        assert mod.QK_PRICE_FIELDS == ("input", "cached", "cache_write", "output")


def test_find_pricing_override_shapes_both(qk, load_admin):
    table = {"m/x": {"input": 1.0, "output": 2.0, "cached": None, "cache_write": None}}
    for mod in (qk, load_admin()):
        # legacy direct prices + multiplier
        price, how = mod.qk_find_pricing("m/x", table, {"m/x": {"input": 5, "output": 10, "multiplier": 2}})
        assert price["input"] == 10.0 and price["output"] == 20.0
        assert how == "override:m/x*2.0"
        # multiplier-only scales the upstream table match
        price, how = mod.qk_find_pricing("m/x", table, {"m/x": {"multiplier": 3}})
        assert price["input"] == 3.0 and price["output"] == 6.0
        assert how == "exact:m/x*3.0"
        # wrapped prices
        price, how = mod.qk_find_pricing("m/x", table, {"m/x": {"prices": {"input": 7, "output": 9}}})
        assert price["input"] == 7.0 and price["output"] == 9.0
        # alias to a table key
        price, how = mod.qk_find_pricing("other", table, {"other": {"alias": "m/x"}})
        assert price["input"] == 1.0 and how.startswith("alias:m/x")


def test_validate_config_override_paths_both(qk, load_admin):
    for mod in (qk, load_admin()):
        # valid override shapes -> no errors
        assert not mod.qk_validate_config({"pricing": {"overrides": {"m/x": {"input": 1, "multiplier": 2}}}})
        assert not mod.qk_validate_config({"pricing": {"overrides": {"m/x": {"multiplier": 3}}}})  # multiplier-only
        assert not mod.qk_validate_config({"pricing": {"overrides": {"m/x": {"alias": "y"}}}})
        # invalid multiplier (any shape) -> error
        assert mod.qk_validate_config({"pricing": {"overrides": {"m/x": {"input": 1, "multiplier": -1}}}})
        assert mod.qk_validate_config({"pricing": {"overrides": {"m/x": {"alias": "y", "multiplier": 0}}}})
        # negative price field -> error
        assert mod.qk_validate_config({"pricing": {"overrides": {"m/x": {"prices": {"input": -2}}}}})


def test_record_usage_with_override_meters_cost(qk, tmp_path):
    # The exact call that NameError'd before the fix: qk_record_usage always
    # consults pricing.overrides via qk_find_pricing. With an override price
    # configured, the model must meter a non-zero cost (priced, not unpriced).
    from tests.conftest import write_json
    cfg = qk.qk_get_config()
    cfg["pricing"]["overrides"] = {"m/x": {"input": 10.0, "output": 20.0}}
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    qk.JC._store.clear()
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "u@x.com"},
                       "m/x", {"cached": 0, "input": 1_000_000, "output": 1_000_000, "cache_write": 0})
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    mm = list(led["users"]["u1"]["days"].values())[0]["models"]["m/x"]
    assert mm["unpriced_requests"] == 0
    # input 1M @ $10 + output 1M @ $20 = $30 (TOU disabled -> rate 1)
    assert mm["cost_usd"] == pytest.approx(30.0)


# ---- topup delta semantics: full-usage repeats must not double tokens --------
#
# The topup paths (stream() with an already-seen response id; the OWUI 0.11
# stream-end outlet whose body id is the message id) were designed for PARTIAL
# usage deltas (Anthropic message_delta). When the second event carries the
# FULL usage of an already-recorded response (stream-end outlet echoes
# messages[-1].usage, or a duplicate usage chunk in the stream), the tokens
# were added a second time into the day/hour/day-model buckets -- while the
# hour per-model bucket skipped topups entirely. Day-granularity stats summed
# two equally-doubled buckets (looked consistent); the 24h window view sums
# hour buckets for KPI/users but hour per-model buckets for the model table,
# exposing the doubling as KPI == 2x sum(models) for API-only users.


def _tok_sum(t):
    return sum((t or {}).get(k, 0.0) or 0.0 for k in ("cached", "input", "output"))


def test_stream_end_outlet_full_usage_topup_not_double_counted(qk):
    """stream() records the terminal chunk's full usage; the 0.11 stream-end
    outlet repeats the SAME full usage on messages[-1].usage. Tokens/cost must
    be recorded once at every bucket level (day, hour, day-model, hour-model)."""
    f = qk.Filter()
    import asyncio
    md = {"session_id": "s", "message_id": "msg-1", "user_id": "u1",
          "model_name": "gpt-x"}  # no chat_id -> api channel
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    asyncio.run(f.stream({"id": "chatcmpl-1", "model": "gpt-x", "usage": dict(usage)},
                         __user__=_user(), __metadata__=md))
    body = {"model": "gpt-x", "id": "msg-1",
            "messages": [{"role": "user", "content": "hi"},
                         {"id": "msg-1", "role": "assistant", "content": "ok",
                          "usage": dict(usage)}]}
    asyncio.run(f.outlet(body, __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1
    assert d["tokens"]["input"] == 1000 and d["tokens"]["output"] == 500  # not 2x
    mm = d["models"]["gpt-x"]
    assert mm["requests"] == 1
    assert mm["tokens"]["input"] == 1000 and mm["tokens"]["output"] == 500
    h = list(d["hours"].values())[0]
    assert h["tokens"]["input"] == 1000 and h["tokens"]["output"] == 500
    hm = h["models"]["gpt-x"]
    assert hm["tokens"]["input"] == 1000 and hm["tokens"]["output"] == 500
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {})
    assert len(rec["items"]) == 1  # the zero-delta topup is not a new response


def test_api_stream_end_outlet_without_message_id_not_double_recorded(qk):
    """Regression (2026-09-04 live incident): API clients send no chat_id and no
    message id, so __metadata__.message_id is None and stream() can never mark
    _seen_msgids. OWUI 0.11.1 still runs outlet_filter_handler inline at stream
    end ("For temp/API chats, messages are built from form_data plus
    ctx['assistant_message']"), with a FRESHLY generated body id
    (output_id('msg')) and body.model = the OWUI model id (prx.deepseek-flash).
    The v0.4.9 msgid dedup can never fire here: the echo must be matched to the
    stream() record by content (same user, token totals not exceeding what was
    recorded, within a short window) and merged as a zero-delta topup instead
    of double-recorded under the prx-stripped alias."""
    f = qk.Filter()
    import asyncio
    # API shape: no chat_id, NO message_id in metadata (main.py pops form_data
    # 'id' into message_ids; API clients don't send one -> None)
    md = {"session_id": None, "user_id": "u1"}
    usage = {"prompt_tokens": 356, "completion_tokens": 1,
             "prompt_tokens_details": {"cached_tokens": 0}}
    # stream(): the OpenAI chunk echoes the REAL upstream model name
    asyncio.run(f.stream({"id": "30ec0a31-f97e", "model": "deepseek-v4-flash",
                          "usage": dict(usage)},
                         __user__=_user(), __metadata__=md))
    # stream-end outlet echo: fresh msg id, OWUI model id as body.model
    body = {"model": "prx.deepseek-flash", "id": "msg-9f8e7d6c5b4a",
            "messages": [{"role": "user", "content": "hi"},
                         {"id": "msg-9f8e7d6c5b4a", "role": "assistant", "content": "1",
                          "usage": dict(usage)}],
            "chat_id": "", "session_id": None}
    asyncio.run(f.outlet(body, __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1                        # not 2
    assert d["tokens"]["input"] == 356 and d["tokens"]["output"] == 1
    assert set(d["models"]) == {"deepseek-v4-flash"}  # no deepseek-flash phantom
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {})
    assert len(rec["items"]) == 1
    assert rec["items"][0]["model"] == "deepseek-v4-flash"


def test_nonstream_api_outlet_still_records_without_stream_match(qk):
    """Guard the other side of the content-match dedup: a NON-streaming API
    request never runs stream(), so the outlet echo-match finds nothing and
    the request must still be recorded exactly once (under the prx-stripped
    model id fallback)."""
    f = qk.Filter()
    import asyncio
    md = {"session_id": None, "user_id": "u1"}
    usage = {"prompt_tokens": 100, "completion_tokens": 5}
    body = {"model": "prx.deepseek-flash", "id": "msg-nonstream",
            "messages": [{"role": "user", "content": "hi"},
                         {"id": "msg-nonstream", "role": "assistant", "content": "ok",
                          "usage": dict(usage)}],
            "chat_id": "", "session_id": None}
    asyncio.run(f.outlet(body, __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1
    assert set(d["models"]) == {"deepseek-flash"}
    rec = qk.qk_load_json(qk.QK_RECENT_PATH, {})
    assert len(rec["items"]) == 1


def test_stream_duplicate_usage_chunk_same_rid_not_double_counted(qk):
    """Two usage-bearing stream events sharing the response id (e.g. upstream
    usage chunk + a second forwarded/synthesized one): the repeat is a
    zero-delta topup, tokens must not be added twice."""
    f = qk.Filter()
    import asyncio
    md = {"session_id": "s", "model_name": "gpt-x"}
    ev = {"id": "chatcmpl-2", "model": "gpt-x",
          "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
    asyncio.run(f.stream(dict(ev), __user__=_user(), __metadata__=md))
    asyncio.run(f.stream(dict(ev), __user__=_user(), __metadata__=md))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1
    assert d["tokens"]["input"] == 1000 and d["tokens"]["output"] == 500
    h = list(d["hours"].values())[0]
    assert h["models"]["gpt-x"]["tokens"]["input"] == 1000


def test_anthropic_partials_land_in_hour_model_bucket(qk):
    """Partial-usage topups (Anthropic message_delta) must ALSO reach the hour
    per-model bucket, otherwise the 24h model table undercounts vs the KPI."""
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream({"id": "r7", "message": {"usage": {"input_tokens": 40}},
                          "model": "claude-x"}, __user__=_user(), __metadata__={}))
    asyncio.run(f.stream({"id": "r7", "usage": {"output_tokens": 7},
                          "model": "claude-x"}, __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1
    assert d["tokens"]["input"] == 40 and d["tokens"]["output"] == 7
    h = list(d["hours"].values())[0]
    assert h["tokens"]["input"] == 40 and h["tokens"]["output"] == 7
    hm = h["models"]["claude-x"]
    assert hm["tokens"]["input"] == 40 and hm["tokens"]["output"] == 7


def test_stream_topup_cumulative_output_not_over_added(qk):
    """Anthropic message_start usage includes output_tokens=1 and message_delta
    reports CUMULATIVE output: the topup must record the delta (7-1), not add
    the cumulative value on top (which would yield 8)."""
    f = qk.Filter()
    import asyncio
    asyncio.run(f.stream({"id": "r8", "message": {"usage": {"input_tokens": 40, "output_tokens": 1}},
                          "model": "claude-x"}, __user__=_user(), __metadata__={}))
    asyncio.run(f.stream({"id": "r8", "usage": {"output_tokens": 7},
                          "model": "claude-x"}, __user__=_user(), __metadata__={}))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    d = list(led["users"]["u1"]["days"].values())[0]
    assert d["requests"] == 1
    assert d["tokens"]["input"] == 40 and d["tokens"]["output"] == 7  # not 8


def test_stats_views_consistent_after_full_usage_topup(qk, load_admin):
    """KPI/user tokens must equal the per-model sum in BOTH stats views after
    a full-usage topup (day view sums day buckets, 24h view sums hour buckets)."""
    import asyncio, time
    f = qk.Filter()
    md = {"session_id": "s", "message_id": "msg-1", "user_id": "u1",
          "model_name": "gpt-x"}
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    asyncio.run(f.stream({"id": "chatcmpl-1", "model": "gpt-x", "usage": dict(usage)},
                         __user__=_user(), __metadata__=md))
    asyncio.run(f.outlet({"model": "gpt-x", "id": "msg-1",
                          "messages": [{"role": "assistant", "content": "ok",
                                        "usage": dict(usage)}]},
                         __user__=_user(), __metadata__=md))
    adm = load_admin()
    day = adm.qk_stats()
    win = adm.qk_stats_window(time.time() - 86400)
    for name, out in (("day", day), ("24h", win)):
        kpi = _tok_sum(out["kpi"]["tokens"])
        usr = sum(_tok_sum(r["tokens"]) for r in out["users"])
        mod = sum(_tok_sum(m["tokens"]) for m in out["models"])
        assert kpi == 1500 and usr == 1500 and mod == 1500, \
            f"{name} view: kpi={kpi} users={usr} models={mod}"
