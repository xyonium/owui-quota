"""
title: Quota Keeper - Filter
author: quota-keeper
version: 0.1.1
required_open_webui_version: 0.6.0
description: Token metering (cached/input/output) + cost quota enforcement. User quota overrides groups; among groups the highest wins. Pricing pulled from upstream (LiteLLM/models.dev formats) with suffix fuzzy matching. Pair with "Quota Keeper - Admin UI" event function for the /quota config page.
"""

import os
import re
import json
import time
import logging
import threading
from contextlib import contextmanager
from collections import OrderedDict
from datetime import datetime, timedelta, timezone as _dt_timezone
from typing import Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# ==== shared helpers: keep in sync with quota_keeper_admin.py (same code) ====

def qk_data_dir() -> str:
    base = os.environ.get("DATA_DIR") or "/app/backend/data"
    d = os.path.join(base, "quota_keeper")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(os.getcwd(), "quota_keeper")
        os.makedirs(d, exist_ok=True)
    return d


QK_DIR = qk_data_dir()
QK_CONFIG_PATH = os.path.join(QK_DIR, "config.json")
QK_LEDGER_PATH = os.path.join(QK_DIR, "ledger.json")
QK_PRICING_PATH = os.path.join(QK_DIR, "pricing_cache.json")

DEFAULT_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/"
    "main/model_prices_and_context_window.json"
)

DEFAULT_CONFIG = {
    "credits_per_usd": 1000.0,          # 1000 credits = 1 USD
    "quota_period": "daily",            # daily | monthly
    "default_quota_credits": None,      # None = unlimited
    "user_quotas": {},                  # user_id -> credits (highest priority)
    "group_quotas": {},                 # group_id -> credits (max wins among groups)
    "ledger_retention_days": 400,
    "pricing": {
        "url": DEFAULT_PRICING_URL,
        "refresh_hours": 24,
        "default_pricing": None,        # {"input":..,"cached":..,"output":..} per 1M tokens
        "overrides": {},                # model -> same shape, absolute priority
    },
    "schedule": {
        "timezone": None,               # None -> $TZ -> UTC
        "night_start_hour": 22,
        "night_end_hour": 8,
        "night_multiplier": 1.0,        # quota * multiplier at night (e.g. 0.5)
        "weekend_multiplier": 1.0,      # quota * multiplier on Sat/Sun
    },
    "tou": {
        "enabled": False,
        "timezone": None,               # None -> schedule.timezone
        "tiers": {
            "peak": {"rate": 2.0, "windows": [{"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "12:00"},
                                               {"days": [1, 2, 3, 4, 5], "start": "14:00", "end": "18:00"}]},
            "offpeak": {"rate": 0.5, "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:30", "end": "08:30"}]},
            "normal": {"rate": 1.0},
        },
        "holidays": [],                  # "YYYY-MM-DD" -> whole day offpeak
        "default_policy": "off",         # off | normal
        "providers": {},                 # "<first path segment>": {"enabled": bool, "tiers": {name: {"rate": x}}}
        "models": {},                    # exact model id: {"enabled": bool, "tiers": {...}}
    },
}


def qk_load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def qk_atomic_write(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


try:
    import fcntl

    @contextmanager
    def qk_lock():
        p = os.path.join(QK_DIR, ".lock")
        with open(p, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
except Exception:  # windows / exotic fs
    _tlock = threading.Lock()

    @contextmanager
    def qk_lock():
        with _tlock:
            yield


class _JsonCache:
    """mtime-keyed in-memory cache; refreshes automatically after any write."""

    def __init__(self):
        self._lock = threading.Lock()
        self._store = {}

    def get(self, path: str, default):
        try:
            mtime = os.stat(path).st_mtime
        except Exception:
            return default
        with self._lock:
            ent = self._store.get(path)
            if ent and ent[0] == mtime:
                return ent[1]
        data = qk_load_json(path, default)
        with self._lock:
            self._store[path] = (mtime, data)
        return data


JC = _JsonCache()


def qk_merge_config(cfg: dict) -> dict:
    base = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v
    return base


def qk_deep_merge(base, patch):
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            qk_deep_merge(base[k], v)
        else:
            base[k] = v
    return base


_QK_NUM = lambda v: not isinstance(v, bool) and isinstance(v, (int, float))


def qk_validate_config(cfg) -> list:
    errs = []
    if not isinstance(cfg, dict):
        return ["config must be an object"]
    if "credits_per_usd" in cfg and not (_QK_NUM(cfg["credits_per_usd"]) and cfg["credits_per_usd"] > 0):
        errs.append("credits_per_usd must be a positive number")
    if "quota_period" in cfg and cfg["quota_period"] not in (None, "daily", "monthly"):
        errs.append("quota_period must be daily|monthly")
    for key in ("user_quotas", "group_quotas"):
        if key in cfg and not isinstance(cfg[key], dict):
            errs.append(f"{key} must be an object")
    sch = cfg.get("schedule")
    if sch is not None and not isinstance(sch, dict):
        errs.append("schedule must be an object")
    if isinstance(sch, dict):
        for k in ("night_start_hour", "night_end_hour"):
            if k in sch and not (isinstance(sch[k], int) and not isinstance(sch[k], bool) and 0 <= sch[k] <= 23):
                errs.append(f"schedule.{k} must be int 0-23")
        for k in ("night_multiplier", "weekend_multiplier"):
            if k in sch and not (_QK_NUM(sch[k]) and sch[k] >= 0):
                errs.append(f"schedule.{k} must be number >= 0")
    pri = cfg.get("pricing")
    if pri is not None and not isinstance(pri, dict):
        errs.append("pricing must be an object")
    tou = cfg.get("tou")
    if tou is not None and not isinstance(tou, dict):
        errs.append("tou must be an object")
    if isinstance(tou, dict):
        dp = tou.get("default_policy")
        if dp is not None and dp not in ("off", "normal"):
            errs.append("tou.default_policy must be off|normal")
        hol = tou.get("holidays")
        if hol is not None:
            if not isinstance(hol, list):
                errs.append("tou.holidays must be a list of YYYY-MM-DD")
            elif any(not isinstance(h, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", h) for h in hol):
                errs.append("tou.holidays entries must be YYYY-MM-DD")
        tiers = tou.get("tiers")
        if tiers is not None and not isinstance(tiers, dict):
            errs.append("tou.tiers must be an object")
        elif isinstance(tiers, dict):
            for tname, tconf in tiers.items():
                if not isinstance(tconf, dict):
                    errs.append(f"tou.tiers.{tname} must be an object")
                    continue
                rate = tconf.get("rate")
                if rate is not None and not (_QK_NUM(rate) and rate > 0):
                    errs.append(f"tou.tiers.{tname}.rate must be a positive number")
                for wi, w in enumerate(tconf.get("windows") or []):
                    if not isinstance(w, dict):
                        errs.append(f"tou.tiers.{tname}.windows[{wi}] must be an object")
                        continue
                    days = w.get("days")
                    if days is not None and (
                        not isinstance(days, list)
                        or not all(isinstance(dx, int) and not isinstance(dx, bool) and 0 <= dx <= 6 for dx in days)
                    ):
                        errs.append(f"tou.tiers.{tname}.windows[{wi}].days must be a list of ints 0-6")
                    for hh in ("start", "end"):
                        v = w.get(hh)
                        if v is not None and (not isinstance(v, str) or not re.match(r"^\d{2}:\d{2}$", v)):
                            errs.append(f"tou.tiers.{tname}.windows[{wi}].{hh} must be HH:MM")
    return errs


def qk_get_config() -> dict:
    return qk_merge_config(JC.get(QK_CONFIG_PATH, {}))


def qk_local_now(cfg: dict) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        tz = (cfg.get("schedule") or {}).get("timezone") or os.environ.get("TZ") or "UTC"
        try:
            return datetime.now(ZoneInfo(tz))
        except Exception:
            return datetime.now(_dt_timezone.utc)
    except Exception:
        return datetime.now(_dt_timezone.utc)


def qk_tou_local_now(cfg: dict) -> datetime:
    """Now in the TOU timezone (tou.timezone > schedule.timezone > TZ > UTC)."""
    tou = cfg.get("tou") or {}
    tz = tou.get("timezone") or (cfg.get("schedule") or {}).get("timezone")
    if tz:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz))
        except Exception:
            pass
    return qk_local_now(cfg)


def qk_tou_resolve_policy(cfg: dict, model_id: str):
    """models[exact] -> providers[first segment] -> default_policy. Returns policy dict or None (off)."""
    tou = cfg.get("tou") or {}
    if not tou.get("enabled"):
        return None
    mid = str(model_id or "").strip().lower()
    mpol = (tou.get("models") or {}).get(mid)
    if isinstance(mpol, dict):
        return mpol if mpol.get("enabled", True) else None
    prov = mid.split("/")[0] if "/" in mid else "_default"
    ppol = (tou.get("providers") or {}).get(prov)
    if isinstance(ppol, dict):
        return ppol if ppol.get("enabled", True) else None
    if (tou.get("providers") or {}).get(prov) is None and tou.get("default_policy") == "normal":
        return {}
    return None


def qk_tou_rate(cfg: dict, model_id: str, now) -> tuple:
    """Returns (rate, tier). Tier 'off' means TOU does not apply (rate 1.0).
    Window days use JS-style numbering (0=Sunday .. 6=Saturday) so the
    default offpeak `days: [0..6]` covers every day of the week."""
    tou = cfg.get("tou") or {}
    pol = qk_tou_resolve_policy(cfg, model_id)
    if pol is None:
        return 1.0, "off"
    tiers = dict(tou.get("tiers") or {})

    def _merge(tname):
        base = dict(tiers.get(tname) or {})
        over = ((pol.get("tiers") or {}).get(tname)) or {}
        base.update(over)
        return base

    peak, offpeak, normal = _merge("peak"), _merge("offpeak"), _merge("normal")
    dstr = now.strftime("%Y-%m-%d")
    if dstr in (tou.get("holidays") or []):
        if offpeak:
            return float(offpeak.get("rate", 1.0)), "offpeak"
        cands = [(float(t.get("rate", 1.0)), n) for n, t in (("peak", peak), ("normal", normal)) if t]
        if cands:
            return min(cands, key=lambda x: x[0])
        return 1.0, "normal"

    def _hit(tier):
        for w in tier.get("windows") or []:
            days = w.get("days") or list(range(7))
            if (now.weekday() + 1) % 7 not in days:
                continue
            try:
                sh, sm = map(int, str(w.get("start", "00:00")).split(":"))
                eh, em = map(int, str(w.get("end", "00:00")).split(":"))
            except Exception:
                continue
            s, e, cur = sh * 60 + sm, eh * 60 + em, now.hour * 60 + now.minute
            if s <= e:
                if s <= cur < e:
                    return True
            else:  # spans midnight
                if cur >= s or cur < e:
                    return True
        return False

    if peak and _hit(peak):
        return float(peak.get("rate", 1.0)), "peak"
    if offpeak and _hit(offpeak):
        return float(offpeak.get("rate", 1.0)), "offpeak"
    return float(normal.get("rate", 1.0)), "normal"


# ---------------------------------------------------------------------------
# Pricing: fetch (LiteLLM flat format or models.dev nested format) + fuzzy match
# ---------------------------------------------------------------------------

QK_DATE_RE = re.compile(r"[-:_.](20\d{2}[-_.]?\d{2}[-_.]?\d{2}|\d{6})$")


def _qk_variants(m: str):
    """Model-id variants: raw, dash-normalized, date-stripped, both."""
    stripped = QK_DATE_RE.sub("", m).strip("-:_. ")
    out = []
    for v in (m, m.replace(".", "-"), stripped, stripped.replace(".", "-")):
        if v and v not in out:
            out.append(v)
    return out


def qk_find_pricing(model_id: str, table: dict, overrides: Optional[dict] = None):
    """override -> exact -> path-suffix -> tail-segment -> contains (all on
    raw and date-stripped variants). Returns (price|None, how|None)."""
    m = (model_id or "").strip().lower()
    if not m:
        return None, None
    vs = _qk_variants(m)
    ov = {str(k).strip().lower(): v for k, v in (overrides or {}).items()}
    for cand in vs:
        if cand in ov:
            return ov[cand], "override:" + cand
    if table:
        for cand in vs:
            if cand in table:
                return table[cand], "exact:" + cand
        best = None
        for cand in vs:
            for k in table:
                if cand.endswith("/" + k) and (best is None or len(k) > len(best[1])):
                    best = ("suffix", k)
        if best:
            return table[best[1]], best[0] + ":" + best[1]
        for cand in vs:
            segs = cand.split("/")
            for i in range(len(segs)):
                tail = "/".join(segs[i:])
                if tail in table:
                    return table[tail], "segment:" + tail
        best = None
        for cand in vs:
            for k in table:
                if len(k) >= 4 and k in cand and (best is None or len(k) > len(best[1])):
                    best = ("contains", k)
        if best:
            return table[best[1]], best[0] + ":" + best[1]
    return None, None


def qk_normalize_usage(u) -> Optional[dict]:
    """Normalize OpenAI / Anthropic / generic usage into cached/input/output(+write)."""
    if not isinstance(u, dict):
        return None

    def g(*keys):
        for k in keys:
            v = u.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return None

    pt = g("prompt_tokens")
    ct = g("completion_tokens")
    ai = g("input_tokens")
    ao = g("output_tokens")
    cr = g("cache_read_input_tokens")
    cw = g("cache_creation_input_tokens")
    cached_oai = None
    ptd = u.get("prompt_tokens_details")
    if isinstance(ptd, dict) and isinstance(ptd.get("cached_tokens"), (int, float)):
        cached_oai = float(ptd["cached_tokens"])

    if pt is not None:
        cached = (cached_oai or 0.0) + (cr or 0.0)
        inp = max(0.0, pt - cached)
        out = ct if ct is not None else (ao or 0.0)
    elif ai is not None or ao is not None:
        # input_tokens and/or output_tokens; a lone output_tokens appears in
        # Anthropic message_delta partial-usage events
        cached = cr or 0.0
        inp = ai or 0.0
        out = ao or 0.0
    else:
        return None
    if inp == 0 and out == 0 and cached == 0 and not cw:
        return None
    return {"cached": cached, "input": inp, "output": out, "cache_write": cw or 0.0}


def qk_cost_usd(tok: dict, price) -> float:
    if not price:
        return 0.0
    c = 0.0
    for field in ("input", "cached", "cache_write", "output"):
        p = price.get(field)
        if isinstance(p, (int, float)):
            c += (tok.get(field) or 0.0) * float(p) / 1e6
    return c


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def qk_prune_ledger(led: dict, cfg: dict) -> None:
    try:
        days = int(cfg.get("ledger_retention_days") or 0)
    except Exception:
        days = 0
    if days <= 0:
        return
    # cutoff in the configured timezone so "today" is the same day the
    # ledger buckets were written under (previously naive UTC)
    cutoff = (qk_local_now(cfg) - timedelta(days=days)).strftime("%Y-%m-%d")
    for uid in list((led.get("users") or {}).keys()):
        udays = (led["users"].get(uid) or {}).get("days") or {}
        for k in [k for k in udays if k < cutoff]:
            udays.pop(k, None)


def qk_record_usage(user: dict, model: str, tok: dict, count_request: bool = True,
                    now: datetime = None) -> None:
    """Record one usage event. count_request=False marks a partial-usage
    topup for an id that already recorded: tokens/cost still accumulate but
    the request counters (day/hours/models) are not incremented again.
    `now` overrides the current time (also picks the TOU tier/day key);
    default is the TOU-aware local now."""
    uid = (user or {}).get("id")
    if not uid:
        return
    cfg = qk_get_config()
    cache = JC.get(QK_PRICING_PATH, {}) or {}
    table = cache.get("table") or {}
    pconf = cfg.get("pricing") or {}
    price, _how = qk_find_pricing(model, table, pconf.get("overrides"))
    priced = price is not None
    if price is None:
        price = pconf.get("default_pricing")
        priced = price is not None
    base_cost = qk_cost_usd(tok, price)
    now_local = now or qk_tou_local_now(cfg)
    day = now_local.strftime("%Y-%m-%d")
    model = str(model or "unknown")[:200]
    rate, tier = qk_tou_rate(cfg, model, now_local)
    cost = base_cost * rate

    with qk_lock():
        led = qk_load_json(QK_LEDGER_PATH, {"users": {}})
        users = led.setdefault("users", {})
        u = users.setdefault(uid, {"name": "", "email": "", "days": {}})
        u["name"] = (user.get("name") or u.get("name") or "")[:200]
        u["email"] = (user.get("email") or u.get("email") or "")[:200]
        d = u["days"].setdefault(
            day,
            {
                "requests": 0,
                "cost_usd": 0.0,
                "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
                "tou": {"peak": 0, "offpeak": 0, "normal": 0},
                "cost_saved_usd": 0.0,
                "models": {},
            },
        )
        if count_request:
            d["requests"] = d.get("requests", 0) + 1
        d["cost_usd"] = round(d.get("cost_usd", 0.0) + cost, 8)
        dtou = d.setdefault("tou", {"peak": 0, "offpeak": 0, "normal": 0})
        if count_request and tier in dtou:
            dtou[tier] = dtou.get(tier, 0) + 1
        d["cost_saved_usd"] = round(d.get("cost_saved_usd", 0.0) + (base_cost - cost), 8)
        for k in ("cached", "input", "output"):
            d["tokens"][k] = d["tokens"].get(k, 0.0) + (tok.get(k) or 0.0)
        # hourly bucket (consumed by the dashboard at granularity=hour)
        h = d.setdefault("hours", {}).setdefault(
            str(now_local.hour),
            {
                "requests": 0,
                "cost_usd": 0.0,
                "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
            },
        )
        if count_request:
            h["requests"] = h.get("requests", 0) + 1
        h["cost_usd"] = round(h.get("cost_usd", 0.0) + cost, 8)
        for k in ("cached", "input", "output"):
            h["tokens"][k] = h["tokens"].get(k, 0.0) + (tok.get(k) or 0.0)
        mm = d["models"].setdefault(
            model,
            {
                "requests": 0,
                "cost_usd": 0.0,
                "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
                "priced": True,
                "unpriced_requests": 0,
                "tou": {"peak": 0, "offpeak": 0, "normal": 0},
                "cost_saved_usd": 0.0,
            },
        )
        # unpriced_requests is a per-request counter; topups are not requests
        if count_request:
            mm["requests"] = mm.get("requests", 0) + 1
            mm["unpriced_requests"] = mm.get("unpriced_requests", 0) + (0 if priced else 1)
        mm["cost_usd"] = round(mm.get("cost_usd", 0.0) + cost, 8)
        mtou = mm.setdefault("tou", {"peak": 0, "offpeak": 0, "normal": 0})
        if count_request and tier in mtou:
            mtou[tier] = mtou.get(tier, 0) + 1
        mm["cost_saved_usd"] = round(mm.get("cost_saved_usd", 0.0) + (base_cost - cost), 8)
        for k in ("cached", "input", "output"):
            mm["tokens"][k] = mm["tokens"].get(k, 0.0) + (tok.get(k) or 0.0)
        # derived flag kept for UI back-compat until Task 9 (same semantics as
        # the old sticky AND: once an unpriced request occurred, stays false)
        mm["priced"] = mm.get("unpriced_requests", 0) == 0
        qk_prune_ledger(led, cfg)
        qk_atomic_write(QK_LEDGER_PATH, led)


def qk_period_used_usd(uid: str, cfg: dict) -> float:
    led = JC.get(QK_LEDGER_PATH, {"users": {}}) or {}
    days = ((led.get("users") or {}).get(uid) or {}).get("days") or {}
    now = qk_local_now(cfg)
    if (cfg.get("quota_period") or "daily") == "monthly":
        pref = now.strftime("%Y-%m-")
        return sum(
            (d or {}).get("cost_usd", 0) or 0
            for k, d in days.items()
            if k.startswith(pref)
        )
    return ((days.get(now.strftime("%Y-%m-%d")) or {}).get("cost_usd", 0)) or 0


# ---------------------------------------------------------------------------
# Quota resolution & time schedule
# ---------------------------------------------------------------------------


def qk_user_group_ids(user: dict):
    gids = (user or {}).get("group_ids")
    if isinstance(gids, list) and gids:
        return [str(g) for g in gids]
    try:
        from open_webui.models.groups import Groups

        return [g.id for g in Groups.get_groups_by_member_id(user.get("id"))]
    except Exception:
        return []


def _num(v):
    """True for numbers, but not bools (bool is an int subclass and must not
    be accepted as a quota/multiplier value)."""
    return not isinstance(v, bool) and isinstance(v, (int, float))


def qk_resolve_quota(cfg: dict, user: dict):
    """user quota (if set) wins; otherwise the highest group quota; else default."""
    uq = (cfg.get("user_quotas") or {}).get((user or {}).get("id"))
    if _num(uq) and uq > 0:
        return float(uq), "user"
    gq = cfg.get("group_quotas") or {}
    vals = [
        float(gq[str(g)])
        for g in qk_user_group_ids(user or {})
        if _num(gq.get(str(g))) and gq.get(str(g)) > 0
    ]
    if vals:
        return max(vals), "group"
    dq = cfg.get("default_quota_credits")
    if _num(dq) and dq > 0:
        return float(dq), "default"
    return None, "none"


def qk_time_multiplier(cfg: dict) -> float:
    sch = cfg.get("schedule") or {}
    now = qk_local_now(cfg)
    mult = 1.0
    wm_raw = sch.get("weekend_multiplier", 1.0)
    try:
        wm = float(wm_raw) if _num(wm_raw) else 1.0
    except Exception:
        wm = 1.0
    nm_raw = sch.get("night_multiplier", 1.0)
    try:
        nm = float(nm_raw) if _num(nm_raw) else 1.0
    except Exception:
        nm = 1.0
    if now.weekday() >= 5 and wm != 1.0:
        mult *= wm
    try:
        ns = int(sch.get("night_start_hour", 22))
        ne = int(sch.get("night_end_hour", 8))
    except Exception:
        ns, ne = 22, 8
    h = now.hour
    in_night = (h >= ns or h < ne) if ns > ne else (ns <= h < ne)
    if in_night and nm != 1.0:
        mult *= nm
    return mult


# ---------------------------------------------------------------------------
# The Filter
# ---------------------------------------------------------------------------


class QuotaBlocked(Exception):
    pass


class Filter:
    class Valves(BaseModel):
        enable_enforcement: bool = Field(
            default=True, description="Enable quota enforcement (usage is always recorded)"
        )
        admins_bypass: bool = Field(default=True, description="Admins skip quota checks")
        allow_background_tasks: bool = Field(
            default=True,
            description="Never block background tasks (title/tag generation); their cost is still metered",
        )
        estimate_unreported_tokens: bool = Field(
            default=False,
            description="If provider reports no usage, estimate tokens from text length (not billing-grade)",
        )
        block_message: str = Field(
            default=(
                "Quota exceeded: used {used} / {quota} credits this period "
                "(source={source}, time multiplier={mult}). Resets with the period; contact admin for more."
            )
        )

    def __init__(self):
        self.valves = self.Valves()
        self._seen = OrderedDict()  # dedup usage by response id
        self._orphan = OrderedDict()  # usage seen in stream without user info

    # -- helpers ------------------------------------------------------------

    def _mark_seen(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = True
        while len(self._seen) > 4096:
            self._seen.popitem(last=False)
        return True

    def _record(self, user: dict, model: str, tok: dict, rid: str = "") -> None:
        # orphan (no user yet) is stashed WITHOUT marking seen, so the same
        # response id can be adopted and recorded later by outlet()/stream()
        uid = (user or {}).get("id")
        if not uid:
            rid = rid or f"{time.time_ns()}"
            self._orphan[rid] = {"model": model, "tok": tok, "ts": time.time()}
            while len(self._orphan) > 256:
                self._orphan.popitem(last=False)
            return
        rid = rid or f"{time.time_ns()}"
        if not self._mark_seen(rid):
            return
        qk_record_usage(user, model, tok)

    # -- enforcement ----------------------------------------------------------

    async def inlet(
        self, body: dict, __user__: dict = None, __metadata__: dict = None
    ) -> dict:
        if not self.valves.enable_enforcement:
            return body
        try:
            user = __user__ or {}
            uid = user.get("id")
            if not uid:
                return body
            if self.valves.admins_bypass and user.get("role") == "admin":
                return body
            if (__metadata__ or {}).get("task") and self.valves.allow_background_tasks:
                return body
            cfg = qk_get_config()
            quota, source = qk_resolve_quota(cfg, user)
            if quota is None:
                return body  # unlimited
            mult = qk_time_multiplier(cfg)
            eff = quota * mult
            if eff <= 0:
                # multiplier zeroed the quota (e.g. night_multiplier=0): a
                # hard block, not unlimited
                raise QuotaBlocked(
                    "Quota temporarily disabled: effective quota is 0 "
                    "(time multiplier = {}). Contact admin to adjust the "
                    "schedule multipliers.".format(round(mult, 3))
                )
            try:
                cpu_ = float(cfg.get("credits_per_usd") or 1000.0)
            except Exception:
                cpu_ = 1000.0
            used = qk_period_used_usd(uid, cfg) * cpu_
            if used >= eff:
                try:
                    msg = self.valves.block_message.format(
                        used=round(used, 1),
                        quota=round(eff, 1),
                        source=source,
                        mult=round(mult, 3),
                    )
                except Exception:
                    log.warning("quota-keeper block_message template invalid; using default")
                    msg = Filter.Valves().block_message.format(
                        used=round(used, 1),
                        quota=round(eff, 1),
                        source=source,
                        mult=round(mult, 3),
                    )
                raise QuotaBlocked(msg)
            return body
        except QuotaBlocked:
            raise
        except Exception as e:  # fail-open on unexpected errors
            log.warning("quota-keeper inlet error: %s", e)
            return body

    # -- metering: streaming terminal chunk -----------------------------------

    async def stream(
        self, event, __user__: dict = None, __metadata__: dict = None
    ):
        try:
            ev = event
            if isinstance(ev, str):
                if '"usage"' not in ev:
                    return event  # cheap pre-filter: no usage field, skip json.loads
                s = ev.strip()
                if s.startswith("data:"):
                    s = s[5:].strip()
                if not s or s == "[DONE]":
                    return event
                try:
                    ev = json.loads(s)
                except Exception:
                    return event
            if not isinstance(ev, dict):
                return event
            u = ev.get("usage")
            if u is None and isinstance(ev.get("message"), dict):
                # Anthropic message_start nests usage under "message"
                u = ev["message"].get("usage")
            tok = qk_normalize_usage(u)
            if tok is None:
                return event
            rid = str(ev.get("id") or f"stream-{time.time_ns()}")
            model = str(ev.get("model") or (__metadata__ or {}).get("model_name") or "unknown")
            if rid in self._seen:
                # Later partial usage for an already-recorded id: contribute
                # its own fields additively (no new request). Anthropic sends
                # input in message_start and cumulative output in
                # message_delta, so plain addition matches the real totals;
                # input is counted once from the first event.
                qk_record_usage(__user__ or {}, model, tok, count_request=False)
                return event
            self._record(__user__ or {}, model, tok, rid)
        except Exception as e:
            log.warning("quota-keeper stream error: %s", e)
        return event

    # -- metering: non-streaming (and orphan adoption) --------------------------

    async def outlet(
        self, body: dict, __user__: dict = None, __metadata__: dict = None
    ) -> dict:
        try:
            if not isinstance(body, dict):
                return body
            tok = qk_normalize_usage(body.get("usage"))
            choices = body.get("choices") or []
            if tok is None and isinstance(choices, list) and choices:
                tok = qk_normalize_usage((choices[0] or {}).get("usage"))
            rid = str(body.get("id") or "")
            model = str(body.get("model") or (__metadata__ or {}).get("model_name") or "unknown")
            if tok is not None:
                self._record(__user__ or {}, model, tok, rid)
            elif rid and (__user__ or {}).get("id") and rid in self._orphan:
                # adopt usage stashed by stream() when user info was missing
                # there (independent of estimate_unreported_tokens)
                ent = self._orphan.pop(rid)
                qk_record_usage(__user__, ent["model"], ent["tok"])
            elif self.valves.estimate_unreported_tokens and (__user__ or {}).get("id"):
                ch = (body.get("choices") or [{}])[0]
                content = str((ch.get("message") or {}).get("content") or "")
                if content:
                    est = {"cached": 0.0, "input": 0.0, "output": len(content) / 4.0, "cache_write": 0.0}
                    self._record(__user__, model, est, rid or f"est-{time.time_ns()}")
        except Exception as e:
            log.warning("quota-keeper outlet error: %s", e)
        return body
