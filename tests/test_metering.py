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


def test_eff_zero_blocks_not_unlimited(qk, monkeypatch):
    from datetime import datetime

    cfg = qk.qk_get_config()
    cfg["user_quotas"]["u1"] = 100
    cfg["schedule"]["night_multiplier"] = 0.0
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    # Pin the clock to a fixed local time inside the night window (22:00-08:00)
    # so the eff==0 block is deterministic regardless of when the suite runs.
    # Naive datetime is fine: qk_time_multiplier only uses .hour/.weekday().
    fixed = datetime(2026, 8, 17, 23, 30)  # Monday 23:30, in night window
    monkeypatch.setattr(qk, "qk_local_now", lambda cfg: fixed)
    f = qk.Filter()
    import asyncio
    with pytest.raises(qk.QuotaBlocked):
        asyncio.run(f.inlet({"model": "gpt-4o"}, __user__=_user(), __metadata__={}))


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
