# tests/test_tou.py
from datetime import datetime


def _cfg(qk, **over):
    cfg = qk.qk_get_config()
    cfg["tou"] = {
        "enabled": True, "timezone": "UTC",
        "tiers": {
            "peak": {"rate": 2.0, "windows": [{"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "12:00"}]},
            "offpeak": {"rate": 0.5, "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:30", "end": "08:30"}]},
            "normal": {"rate": 1.0},
        },
        "holidays": [], "default_policy": "off",
        "providers": {"deepseek": {"enabled": True}},
        "models": {},
    }
    cfg["tou"].update(over)
    return cfg


def test_peak_window_weekday(qk):
    cfg = _cfg(qk)
    # 2026-08-17 is a Monday 10:00 UTC
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 10, 0))
    assert (rate, tier) == (2.0, "peak")


def test_offpeak_window(qk):
    cfg = _cfg(qk)
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 5, 0))
    assert (rate, tier) == (0.5, "offpeak")


def test_normal_outside_windows(qk):
    cfg = _cfg(qk)
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 13, 0))
    assert (rate, tier) == (1.0, "normal")


def test_weekend_no_peak(qk):
    cfg = _cfg(qk)
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 15, 10, 0))  # Saturday
    assert tier == "normal"


def test_provider_unmatched_model_off(qk):
    cfg = _cfg(qk)
    rate, tier = qk.qk_tou_rate(cfg, "openai/gpt-4o", datetime(2026, 8, 17, 10, 0))
    assert tier == "off"


def test_model_override_wins(qk):
    cfg = _cfg(qk, models={"deepseek/deepseek-v4": {"enabled": True, "tiers": {"peak": {"rate": 1.5}}}})
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 10, 0))
    assert (rate, tier) == (1.5, "peak")


def test_provider_tier_rate_override(qk):
    cfg = _cfg(qk, providers={"deepseek": {"enabled": True, "tiers": {"offpeak": {"rate": 0.6}}}})
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 5, 0))
    assert (rate, tier) == (0.6, "offpeak")


def test_holiday_forces_offpeak(qk):
    cfg = _cfg(qk, holidays=["2026-08-17"])
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 10, 0))
    assert tier == "offpeak"


def test_holiday_without_offpeak_falls_back_to_lowest_tier(qk):
    cfg = _cfg(qk, holidays=["2026-08-17"])
    del cfg["tou"]["tiers"]["offpeak"]
    # 10:00 Monday would be peak (2.0) via window; holiday must fall back to
    # the lowest-rate tier among peak/normal -> normal (1.0)
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 10, 0))
    assert (rate, tier) == (1.0, "normal")


def test_midnight_spanning_window(qk):
    cfg = _cfg(qk)
    cfg["tou"]["tiers"]["offpeak"]["windows"] = [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "06:00"}]
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 23, 30))
    assert tier == "offpeak"
    rate, tier = qk.qk_tou_rate(cfg, "deepseek/deepseek-v4", datetime(2026, 8, 17, 5, 0))
    assert tier == "offpeak"


def _tou_post_body(**win_over):
    return {"tou": {"enabled": True, "tiers": {
        "peak": {"rate": 2.0, "windows": [
            dict({"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "12:00"}, **win_over)
        ]},
    }}}


def test_tou_window_hour_25_rejected(admin_client):
    # Fix: HH:MM shape alone accepted "25:00"; the hour range must be 0-23.
    c, _ = admin_client()
    r = c.post("/api/v1/quota-keeper/config", json=_tou_post_body(start="25:00"))
    assert r.status_code == 400
    assert "windows[0].start" in r.text and "00:00-23:59" in r.text


def test_tou_window_minute_60_rejected(admin_client):
    c, _ = admin_client()
    r = c.post("/api/v1/quota-keeper/config", json=_tou_post_body(end="12:60"))
    assert r.status_code == 400
    assert "windows[0].end" in r.text


def test_tou_window_boundaries_accepted(admin_client):
    # 00:00 and 23:59 are legal; empty days list (all days) stays legal.
    c, _ = admin_client()
    r = c.post("/api/v1/quota-keeper/config", json=_tou_post_body(
        start="00:00", end="23:59", days=[]))
    assert r.status_code == 200


def test_record_applies_tou_and_saves(qk, monkeypatch):
    cfg = _cfg(qk)
    qk.qk_atomic_write(qk.QK_CONFIG_PATH, cfg)
    qk.qk_atomic_write(qk.QK_PRICING_PATH, {"table": {"deepseek/deepseek-v4": {"input": 1.0, "output": 2.0}}})
    qk.qk_record_usage({"id": "u1", "name": "U", "email": "e"}, "deepseek/deepseek-v4",
                       {"cached": 0, "input": 1_000_000, "output": 0, "cache_write": 0},
                       now=datetime(2026, 8, 17, 10, 0))
    led = qk.qk_load_json(qk.QK_LEDGER_PATH, {})
    day = list(led["users"]["u1"]["days"])[0]
    mm = led["users"]["u1"]["days"][day]["models"]["deepseek/deepseek-v4"]
    assert abs(mm["cost_usd"] - 2.0) < 1e-9        # 1M input x $1 x peak 2.0
    assert mm["tou"]["peak"] == 1
    assert abs(mm.get("cost_saved_usd", 0) + 1.0) < 1e-9  # saved = -(extra paid)


# ---- TOU model globs (v0.3.0) ------------------------------------------------


def _tou_cfg(models):
    return {
        "tou": {
            "enabled": True,
            "tiers": {"peak": {"rate": 2.0}, "offpeak": {"rate": 0.5}, "normal": {"rate": 1.0}},
            "holidays": [],
            "default_policy": "off",
            "providers": {},
            "models": models,
        }
    }


def test_tou_models_glob_match(qk):
    cfg = _tou_cfg({"*deepseek*": {"enabled": True, "tiers": {"peak": {"rate": 3.0}}}})
    pol = qk.qk_tou_resolve_policy(cfg, "prx/deepseek-v4-pro")
    assert pol is not None and pol["tiers"]["peak"]["rate"] == 3.0
    # non-matching model stays off (default_policy off)
    assert qk.qk_tou_resolve_policy(cfg, "prx/gpt-4o") is None


def test_tou_exact_key_beats_glob(qk):
    cfg = _tou_cfg({
        "*deepseek*": {"enabled": True, "tiers": {"peak": {"rate": 3.0}}},
        "prx/deepseek-v4-pro": {"enabled": True, "tiers": {"peak": {"rate": 9.0}}},
    })
    pol = qk.qk_tou_resolve_policy(cfg, "prx/deepseek-v4-pro")
    assert pol["tiers"]["peak"]["rate"] == 9.0


def test_tou_longer_glob_wins(qk):
    cfg = _tou_cfg({
        "*deepseek*": {"enabled": True, "tiers": {"peak": {"rate": 3.0}}},
        "*deepseek-v4-pro*": {"enabled": True, "tiers": {"peak": {"rate": 7.0}}},
    })
    pol = qk.qk_tou_resolve_policy(cfg, "prx/deepseek-v4-pro-x")
    assert pol["tiers"]["peak"]["rate"] == 7.0


def test_tou_glob_disabled_means_off(qk):
    cfg = _tou_cfg({"*deepseek*": {"enabled": False}})
    assert qk.qk_tou_resolve_policy(cfg, "prx/deepseek-v4-pro") is None
