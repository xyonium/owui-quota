"""
title: Quota Keeper - Admin UI
author: quota-keeper
version: 0.2.0
required_open_webui_version: 0.10.0
description: Registers the /quota admin page to configure user/group quotas, pricing sources and time schedules, and refreshes model pricing from an upstream URL on a schedule. Pair with "Quota Keeper - Filter" which meters usage and enforces the quotas.
"""

import os
import re
import json
import time
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone as _dt_timezone
from contextlib import contextmanager
from typing import Optional

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger(__name__)

# ==== shared helpers: keep in sync with quota_keeper_filter.py (same code) ====

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
QK_RECENT_PATH = os.path.join(QK_DIR, "recent.json")

DEFAULT_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/"
    "main/model_prices_and_context_window.json"
)

DEFAULT_CONFIG = {
    "credits_per_usd": 1000.0,
    "quota_period": "daily",
    "default_quota_credits": None,
    "user_quotas": {},
    "group_quotas": {},
    "ledger_retention_days": 400,
    "pricing": {
        "url": DEFAULT_PRICING_URL,
        "refresh_hours": 24,
        "default_pricing": None,
        "overrides": {},
    },
    "schedule": {
        "timezone": None,
        "night_start_hour": 22,
        "night_end_hour": 8,
        "night_multiplier": 1.0,
        "weekend_multiplier": 1.0,
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
except Exception:
    _tlock = threading.Lock()

    @contextmanager
    def qk_lock():
        with _tlock:
            yield


def qk_merge_config(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        cfg = {}
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
                        if v is None:
                            continue
                        if not isinstance(v, str) or not re.match(r"^\d{2}:\d{2}$", v):
                            errs.append(f"tou.tiers.{tname}.windows[{wi}].{hh} must be HH:MM")
                        else:
                            h_, m_ = int(v[:2]), int(v[3:])
                            if not (0 <= h_ <= 23 and 0 <= m_ <= 59):
                                errs.append(f"tou.tiers.{tname}.windows[{wi}].{hh} must be HH:MM 00:00-23:59")
    return errs


def qk_get_config() -> dict:
    return qk_merge_config(qk_load_json(QK_CONFIG_PATH, {}))


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
    default offpeak `days: [0..6]` covers every day of the week.
    `days: []` (or missing) means every day."""
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


# ==== pricing fetch (same logic as filter; filter reads the cached table) ====

QK_DATE_RE = re.compile(r"[-:_.](20\d{2}[-_.]?\d{2}[-_.]?\d{2}|\d{6})$")


def _qk_variants(m: str):
    stripped = QK_DATE_RE.sub("", m).strip("-:_. ")
    out = []
    for v in (m, m.replace(".", "-"), stripped, stripped.replace(".", "-")):
        if v and v not in out:
            out.append(v)
    return out


def qk_fetch_pricing(url: str, timeout: int = 30) -> dict:
    import requests

    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    raw = r.json()
    table = {}

    def is_litellm(d) -> bool:
        if not isinstance(d, dict) or not d:
            return False
        for v in d.values():
            if isinstance(v, dict) and (
                "input_cost_per_token" in v or "output_cost_per_token" in v
            ):
                return True
        return False

    if is_litellm(raw):
        for name, info in raw.items():
            if not isinstance(info, dict):
                continue

            def tok(k):
                v = info.get(k)
                return float(v) * 1e6 if isinstance(v, (int, float)) else None

            entry = {
                "input": tok("input_cost_per_token"),
                "output": tok("output_cost_per_token"),
                "cached": tok("cache_read_input_token_cost"),
                "cache_write": tok("cache_creation_input_token_cost"),
            }
            if entry["input"] is not None or entry["output"] is not None:
                table[str(name).strip().lower()] = entry
    else:
        # models.dev style: {provider: {"models": {name: {"cost": per-1M}}}}
        for prov, pdata in (raw or {}).items():
            if not isinstance(pdata, dict):
                continue
            for name, minfo in (pdata.get("models") or {}).items():
                c = (minfo or {}).get("cost") or {}

                def pm(k):
                    v = c.get(k)
                    return float(v) if isinstance(v, (int, float)) else None

                entry = {
                    "input": pm("input"),
                    "output": pm("output"),
                    "cached": pm("cache_read"),
                    "cache_write": pm("cache_write"),
                }
                if entry["input"] is not None or entry["output"] is not None:
                    table[f"{prov}/{name}".strip().lower()] = entry
                    table.setdefault(str(name).strip().lower(), entry)
    if not table:
        raise ValueError("unrecognized pricing format")
    return table


def qk_find_pricing(model_id: str, table: dict, overrides: Optional[dict] = None):
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


def qk_refresh_pricing(force: bool = False) -> dict:
    cfg = qk_get_config()
    pconf = cfg.get("pricing") or {}
    cache = qk_load_json(QK_PRICING_PATH, {}) or {}
    interval = float(pconf.get("refresh_hours") or 24)
    if not force:
        age = time.time() - float(cache.get("fetched_at") or 0)
        if age < interval * 3600:
            return {"status": "cached", "models": len(cache.get("table") or {})}
    url = pconf.get("url") or DEFAULT_PRICING_URL
    table = qk_fetch_pricing(url)
    payload = {
        "url": url,
        "fetched_at": time.time(),
        "fetched_at_iso": datetime.now(_dt_timezone.utc).isoformat(),
        "models": len(table),
        "table": table,
    }
    with qk_lock():
        qk_atomic_write(QK_PRICING_PATH, payload)
    return {"status": "refreshed", "models": len(table), "url": url}


def qk_time_multiplier(cfg: dict) -> float:
    now = qk_local_now(cfg)
    sch = cfg.get("schedule") or {}
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


# ==== stats aggregation (reads the ledger for serving; filter never calls it) ====


def qk_stats(from_=None, to=None, user=None, model=None, granularity="day"):
    """Aggregate ledger usage into KPI/series/users/models views.

    Filters: `from_`/`to` are inclusive "YYYY-MM-DD" day bounds, `user` matches
    user id/name/email, `model` matches the exact model id. granularity "hour"
    buckets the series by "YYYY-MM-DDTHH" with cost summed under model key "_";
    anything else buckets per day with cost under the model id. A `model`
    filter restricts KPI, per-user and series day buckets to that model's
    contribution (hour buckets are per-day-per-hour across models and are
    skipped under a model filter).
    """
    led = qk_load_json(QK_LEDGER_PATH, {"users": {}})
    users = led.get("users") or {}
    kpi = {
        "requests": 0,
        "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
        "cost_usd": 0.0,
        "unpriced_requests": 0,
    }
    series, users_rows, models_rows = {}, [], {}
    cfg = qk_get_config()
    from_s = from_ or "0000-00-00"
    to_s = to or "9999-99-99"
    for uid, u in users.items():
        if user and user not in (uid, (u.get("name") or ""), (u.get("email") or "")):
            continue
        row = {
            "user_id": uid,
            "name": u.get("name", ""),
            "email": u.get("email", ""),
            "requests": 0,
            "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
            "cost_usd": 0.0,
            "models": set(),
            "unpriced_requests": 0,
        }
        quota, source = qk_resolve_quota(cfg, {"id": uid})
        row["quota"], row["quota_source"] = quota, source
        row["multiplier"] = qk_time_multiplier(cfg)
        for day, drec in sorted((u.get("days") or {}).items()):
            if not (from_s <= day <= to_s):
                continue
            drec = drec or {}
            day_ms = drec.get("models") or {}
            if model:
                # filtered: only the matching model's contribution counts for
                # this day (KPI and per-user rows); hours buckets aggregate
                # across models so they are skipped under a model filter
                mm = day_ms.get(model)
                if not mm:
                    continue
                row["requests"] += mm.get("requests", 0)
                row["cost_usd"] += mm.get("cost_usd", 0) or 0
                row["unpriced_requests"] += mm.get("unpriced_requests", 0) or 0
                for k in ("cached", "input", "output"):
                    tk = (mm.get("tokens") or {}).get(k, 0) or 0
                    row["tokens"][k] += tk
                    kpi["tokens"][k] += tk
                day_ms = {model: mm}
            else:
                row["requests"] += drec.get("requests", 0)
                row["cost_usd"] += drec.get("cost_usd", 0) or 0
                row["unpriced_requests"] += sum(
                    (m2.get("unpriced_requests") or 0) for m2 in day_ms.values()
                )
                for k in ("cached", "input", "output"):
                    tk = (drec.get("tokens") or {}).get(k, 0) or 0
                    row["tokens"][k] += tk
                    kpi["tokens"][k] += tk
            for m, mm in day_ms.items():
                row["models"].add(m)
                mk = models_rows.setdefault(
                    m,
                    {
                        "model": m,
                        "requests": 0,
                        "cost_usd": 0.0,
                        "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
                        "users": set(),
                        "unpriced_requests": 0,
                        "tou": {"peak": 0, "offpeak": 0, "normal": 0},
                        "cost_saved_usd": 0.0,
                    },
                )
                mk["requests"] += mm.get("requests", 0)
                mk["cost_usd"] += mm.get("cost_usd", 0) or 0
                mk["users"].add(uid)
                mk["unpriced_requests"] += mm.get("unpriced_requests", 0) or 0
                mk["cost_saved_usd"] += mm.get("cost_saved_usd", 0) or 0
                for k in ("cached", "input", "output"):
                    mk["tokens"][k] += (mm.get("tokens") or {}).get(k, 0) or 0
                for tname, tv in ((mm.get("tou") or {})).items():
                    mk["tou"][tname] = mk["tou"].get(tname, 0) + (tv or 0)
                if granularity != "hour":
                    sb = series.setdefault(day, {})
                    sb[m] = sb.get(m, 0) + (mm.get("cost_usd", 0) or 0)
            for h, hrec in ((drec.get("hours") or {}).items()):
                if granularity == "hour" and not model:
                    try:
                        bkey = f"{day}T{int(h):02d}"
                    except Exception:
                        continue
                    series.setdefault(bkey, {})
                    series[bkey]["_"] = series[bkey].get("_", 0) + (
                        (hrec.get("cost_usd") or 0) if isinstance(hrec, dict) else 0
                    )
        kpi["requests"] += row["requests"]
        kpi["cost_usd"] += row["cost_usd"]
        kpi["unpriced_requests"] += row["unpriced_requests"]
        row["models"] = len(row["models"])
        users_rows.append(row)
    ci = kpi["tokens"]["cached"] + kpi["tokens"]["input"]
    kpi["cache_rate"] = (kpi["tokens"]["cached"] / ci) if ci else 0.0
    for mk in models_rows.values():
        mk["users"] = len(mk["users"])
        tot = sum(mk["tokens"].values())
        mk["blended_per_m"] = (mk["cost_usd"] * 1e6 / tot) if tot else 0.0
    return {
        "kpi": kpi,
        "series": [{"bucket": b, "by_model": v} for b, v in sorted(series.items())],
        "users": users_rows,
        "models": sorted(models_rows.values(), key=lambda x: -x["cost_usd"]),
    }


# ==== Open WebUI integration ====


async def _require_user(request: Request):
    try:
        from open_webui.utils.auth import get_verified_user

        user = await get_verified_user(request)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"auth failed: {e}")
    return user


async def _require_admin(request: Request):
    user = await _require_user(request)
    if (getattr(user, "role", "") or "") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


async def _users_table():
    try:
        from open_webui.models.users import Users

        return [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role,
            }
            for u in Users.get_users()
        ]
    except Exception as e:
        log.warning("quota-keeper users fetch failed: %s", e)
        return []


async def _groups_table():
    try:
        from open_webui.models.groups import Groups

        return [
            {
                "id": g.id,
                "name": g.name,
                "members": list(getattr(g, "user_ids", None) or []),
            }
            for g in Groups.get_groups()
        ]
    except Exception as e:
        log.warning("quota-keeper groups fetch failed: %s", e)
        return []


# ==== UI HTML (self-contained, no build step) ====

QK_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Quota Keeper</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--txt:#e2e8f0;--mut:#94a3b8;--acc:#38bdf8;--ok:#34d399;--warn:#fbbf24;--bad:#f87171}
*{box-sizing:border-box}
body{margin:0;font:14px/1.55 -apple-system,BlinkMacSystemFont,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;background:var(--bg);color:var(--txt)}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;position:sticky;top:0;background:rgba(15,23,42,.96);backdrop-filter:blur(6px);z-index:9;flex-wrap:wrap}
header h1{margin:0;font-size:18px}
header .spacer{flex:1}
.badge{font-size:11px;padding:2px 8px;border-radius:99px;background:var(--card);border:1px solid var(--line);color:var(--mut)}
main{max-width:1200px;margin:0 auto;padding:24px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
details>summary{cursor:pointer;font-size:15px;color:var(--acc);font-weight:600}
h2{margin:0 0 4px;font-size:15px;color:var(--acc)}
h3{margin:16px 0 4px;font-size:13px;color:var(--txt)}
p.hint{margin:0 0 14px;color:var(--mut);font-size:12px}
label{display:block;margin:10px 0 4px;font-size:12px;color:var(--mut)}
label.inline{display:inline-flex;align-items:center;gap:5px;margin:0 6px 0 0;width:auto;font-size:12px;color:var(--mut)}
input,select{width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:#0b1220;color:var(--txt);font:inherit}
input:focus,select:focus{outline:1px solid var(--acc)}
.row{display:grid;gap:12px}
@media(min-width:760px){.row.c2{grid-template-columns:1fr 1fr}.row.c3{grid-template-columns:1fr 1fr 1fr}}
button{padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:#0b1220;color:var(--txt);cursor:pointer;font:inherit}
button.primary{background:var(--acc);border-color:var(--acc);color:#082f49;font-weight:600}
button:hover{filter:brightness(1.1)}
button.small{padding:3px 8px;font-size:12px}
button:disabled{opacity:.4;cursor:default}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:12px}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--txt)}
th.num{text-align:right}
th.sortable.asc::after{content:" ▲"}
th.sortable.desc::after{content:" ▼"}
td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.clickable{cursor:pointer}
tr.detail td{background:rgba(11,18,32,.5);padding:10px 10px 10px 28px}
.bar{height:6px;border-radius:4px;background:#0b1220;overflow:hidden;min-width:120px}
.bar i{display:block;height:100%;background:var(--ok)}
.bar i.warn{background:var(--warn)}.bar i.bad{background:var(--bad)}
.pct{font-size:11px;color:var(--mut)}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);color:var(--mut);margin:1px 2px 1px 0}
.tag.src-user{color:var(--acc);border-color:var(--acc)}
.tag.src-group{color:var(--ok);border-color:var(--ok)}
.tag.unpriced{color:var(--warn);border-color:var(--warn)}
.tag.t-peak{color:var(--warn);border-color:var(--warn)}
.tag.t-offpeak{color:var(--acc);border-color:var(--acc)}
.tag.t-normal{color:var(--ok);border-color:var(--ok)}
.tag.manual{color:var(--acc);border-color:var(--acc)}
.toast{position:fixed;right:18px;bottom:18px;padding:10px 16px;border-radius:10px;background:var(--card);border:1px solid var(--line);box-shadow:0 8px 30px rgba(0,0,0,.4);opacity:0;transition:.25s;z-index:99}
.toast.show{opacity:1}
.admin-only{display:none}
.muted{color:var(--mut)}
.small{font-size:12px}
.empty{padding:18px;text-align:center;color:var(--mut)}
#matchResult{margin-top:8px;font-size:12px;color:var(--mut);word-break:break-all}
.match-out{margin-left:6px;font-size:11px;color:var(--mut);word-break:break-all}
/* KPI cards */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:#0b1220;border:1px solid var(--line);border-radius:10px;padding:12px}
.kpi .lbl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.kpi .val{font-size:20px;font-weight:650;margin:4px 0 2px;font-variant-numeric:tabular-nums}
.kpi svg{width:100%;height:34px;display:block}
/* span selector + filters */
.spans{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.spans button.active{background:var(--acc);border-color:var(--acc);color:#082f49;font-weight:600}
.spans input[type=date]{width:auto;padding:6px 8px}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin-bottom:14px}
.filters .f{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--mut)}
.filters input,.filters select{width:230px;padding:6px 8px}
.filters button{padding:6px 14px}
/* trend chart */
.legend{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0;font-size:12px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
#trend svg{width:100%;height:auto}
/* pricing editor */
.pe-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0}
.pe-tools input[type=search]{width:280px;padding:6px 8px}
.pe-tools button{padding:6px 12px}
.pe-num{width:120px;padding:6px 8px}
.pageinfo{font-size:12px;color:var(--mut)}
.scroll{overflow:auto;max-height:440px}
/* TOU editor */
.chips{display:flex;gap:4px}
.chip{width:32px;height:26px;border-radius:6px;border:1px solid var(--line);background:#0b1220;color:var(--mut);font-size:11px;padding:0;cursor:pointer}
.chip.on{background:var(--acc);border-color:var(--acc);color:#082f49}
.win{display:flex;gap:8px;align-items:center;margin:6px 0;flex-wrap:wrap}
.win input[type=time]{width:110px;padding:5px 8px}
.win input[type=date]{width:150px;padding:5px 8px}
.win .chip{width:30px}
.prow{display:flex;gap:8px;align-items:center;margin:6px 0;flex-wrap:wrap}
.prow input[type=text]{width:230px;padding:6px 8px}
.prow input[type=number]{width:90px;padding:6px 8px}
.prow input[type=checkbox]{width:auto}
</style>
</head>
<body>
<header>
 <h1>Quota Keeper</h1>
 <span class="badge" id="meta"></span>
 <span class="spacer"></span>
 <button id="btnSave" class="admin-only" onclick="saveConfig()">Save config</button>
 <button id="btnRefresh" class="primary admin-only" onclick="refreshPricing(true)">Refresh pricing</button>
</header>
<main>
 <section id="secPersonal" hidden></section>

 <section id="secDash" hidden>
  <h2>Dashboard</h2>
  <div class="spans">
   <button data-span="24h" onclick="setSpan('24h')">24h</button>
   <button data-span="7d" onclick="setSpan('7d')">7d</button>
   <button data-span="30d" onclick="setSpan('30d')">30d</button>
   <button data-span="90d" onclick="setSpan('90d')">90d</button>
   <button data-span="custom" onclick="setSpan('custom')">custom</button>
   <span id="customDates" style="display:none;gap:8px;align-items:center">
    <input type="date" id="spanFrom" onchange="setCustom('from')"/>
    <span class="muted">to</span>
    <input type="date" id="spanTo" onchange="setCustom('to')"/>
   </span>
  </div>
  <div class="filters">
   <div class="f"><span>User (name / email / id)</span><input id="fUser" placeholder="all users"/></div>
   <div class="f"><span>Model</span><select id="fModel"><option value="">all</option></select></div>
   <button onclick="loadStats()">Apply</button>
  </div>
  <div class="kpis" id="kpis"></div>
  <div class="legend" id="trendLegend"></div>
  <div id="trend"><p class="hint">No data yet for the selected span.</p></div>
 </section>

 <section id="secUsers" hidden>
  <h2>Users ranking</h2>
  <p class="hint">Span-scope usage; Quota% compares span credits against the period quota × multiplier. Click a row to drill into that user's per-model usage.</p>
  <div class="filters">
   <div class="f"><span>Search</span><input id="qUser" oninput="renderUsers()" placeholder="name / email"/></div>
   <button onclick="exportCsv()">Export CSV</button>
  </div>
  <div class="scroll">
  <table id="uRank">
   <thead><tr>
    <th>User</th>
    <th class="num sortable" data-sort="requests" onclick="toggleSort('requests')">Requests</th>
    <th class="num sortable" data-sort="tokens" onclick="toggleSort('tokens')">Tokens</th>
    <th class="num sortable" data-sort="cost_usd" onclick="toggleSort('cost_usd')">Cost $</th>
    <th class="num sortable" data-sort="credits" onclick="toggleSort('credits')">Credits</th>
    <th class="num sortable" data-sort="quota" onclick="toggleSort('quota')">Quota%</th>
    <th>Source</th>
   </tr></thead>
   <tbody></tbody>
  </table></div>
 </section>

 <section id="secModels" hidden>
  <h2>Models</h2>
  <p class="hint">Blended $/M = cost per 1M tokens across all usage. The match button resolves the fuzzy pricing target via /pricing/match (the /stats payload does not carry the matched key per model) and shows it inline.</p>
  <div class="scroll">
  <table id="modelsT">
   <thead><tr>
    <th>Model</th><th class="num">Requests</th><th class="num">Users</th>
    <th class="num">Cached</th><th class="num">Input</th><th class="num">Output</th>
    <th class="num">Cost $</th><th class="num">Blended $/M</th><th class="num">Saved $</th><th>TOU</th>
   </tr></thead>
   <tbody></tbody>
  </table></div>
 </section>

 <section id="secRecent" hidden>
  <h2>Recent activity</h2>
  <p class="hint">Newest first, max 200 rows. Manual refresh only — nothing polls.</p>
  <button onclick="loadRecent()">Refresh</button>
  <div class="scroll">
  <table id="recentT">
   <thead><tr>
    <th>Time</th><th>User</th><th>Model</th>
    <th class="num">Cached</th><th class="num">Input</th><th class="num">Output</th>
    <th class="num">Cache%</th><th class="num">Cost $</th><th>Tier</th>
   </tr></thead>
   <tbody><tr><td colspan="9" class="empty">Press Refresh to load.</td></tr></tbody>
  </table></div>
 </section>

 <section id="secGeneral" hidden>
  <h2>General</h2>
  <p class="hint">Credits are derived from real cost (USD); 1000 credits = $1 by default. Effective quota = resolved quota (user &gt; max group &gt; default) × time multiplier.</p>
  <div class="row c3">
   <div><label>Credits per USD</label><input id="credits_per_usd" type="number" step="0.01"/></div>
   <div><label>Quota period</label><select id="quota_period"><option value="daily">daily</option><option value="monthly">monthly</option></select></div>
   <div><label>Default quota (credits, empty = unlimited)</label><input id="default_quota_credits" type="number" step="0.01" placeholder="unlimited"/></div>
  </div>
 </section>

 <section id="secSchedule" hidden>
  <h2>Time schedule (multipliers)</h2>
  <p class="hint">Applied at request time. Example: night ×0.5, weekend ×0.5.</p>
  <div class="row c3">
   <div><label>Timezone (empty = server TZ)</label><input id="schedule_timezone" placeholder="Asia/Shanghai"/></div>
   <div><label>Night start hour</label><input id="night_start_hour" type="number" min="0" max="23"/></div>
   <div><label>Night end hour</label><input id="night_end_hour" type="number" min="0" max="23"/></div>
  </div>
  <div class="row c2">
   <div><label>Night multiplier (1 = off)</label><input id="night_multiplier" type="number" step="0.05"/></div>
   <div><label>Weekend multiplier (1 = off)</label><input id="weekend_multiplier" type="number" step="0.05"/></div>
  </div>
  <p class="hint" id="mult_now"></p>
 </section>

 <section id="secPricing" hidden>
  <h2>Pricing source</h2>
  <p class="hint">Supports LiteLLM model_prices_and_context_window.json (per-token converted to per-1M) and models.dev format. Match order: override → exact → date-stripped → path suffix → segment → contains.</p>
  <div class="row c2">
   <div><label>Pricing URL</label><input id="pricing_url"/></div>
   <div><label>Refresh interval (hours)</label><input id="refresh_hours" type="number" min="0" step="1"/></div>
  </div>
  <label>Fallback pricing per 1M tokens when no match (JSON, optional)</label>
  <input id="default_pricing" placeholder='{"input":1,"cached":0.1,"output":2}'/>
  <div class="row" style="margin-top:10px">
   <input id="matchTest" placeholder="Type a model id to test matching, e.g. openai/gpt-4o-mini"/>
   <button onclick="testMatch()">Test match</button>
  </div>
  <div id="matchResult"></div>
 </section>

 <section id="secGroups" hidden>
  <h2>Group quotas (highest wins)</h2>
  <p class="hint">A user in several groups gets the max of those group quotas. A user-level quota overrides groups entirely.</p>
  <div class="scroll">
  <table id="groups"><thead><tr><th>Group</th><th style="width:180px">Quota (credits)</th><th class="num">Members</th></tr></thead><tbody></tbody></table>
  </div>
 </section>

 <section id="secUserq" hidden>
  <h2>User quotas (highest priority)</h2>
  <p class="hint">Leave empty to inherit from groups / default. Used column is the current stats span.</p>
  <div class="scroll">
  <table id="userq"><thead><tr><th>User</th><th style="width:180px">Quota (credits)</th><th>Source</th><th class="num">Used (span)</th><th class="num">Quota%</th></tr></thead><tbody></tbody></table>
  </div>
 </section>

 <details id="secPricingEditor" hidden>
  <summary>Pricing editor (upstream table → per-1M overrides)</summary>
  <p class="hint">Rows are prefilled from the cached upstream table; override rows carry a manual badge. Save collects edited rows into pricing.overrides and POSTs /config (deep merge preserves the rest of the config). Overrides win over the next upstream refresh.</p>
  <div class="pe-tools">
   <input type="search" id="peSearch" placeholder="search model key" oninput="peSearch()"/>
   <button onclick="loadPricingFull()">Refresh table</button>
   <button class="primary" onclick="saveConfig()">Save overrides</button>
   <span class="pageinfo" id="pePage"></span>
   <button onclick="pePage(-1)" id="pePrev">‹ prev</button>
   <button onclick="pePage(1)" id="peNext">next ›</button>
  </div>
  <div class="scroll">
  <table id="peRows">
   <thead><tr><th>Model key</th><th class="num">input</th><th class="num">cached</th><th class="num">cache_write</th><th class="num">output</th></tr></thead>
   <tbody></tbody>
  </table></div>
 </details>

 <details id="secTou" hidden>
  <summary>TOU editor (time-of-use tiered pricing)</summary>
  <p class="hint">Windows use JS-style day numbers (0 = Sunday). Holidays force the whole day to the cheapest tier. Providers key on the first path segment of a model id; model overrides key on the exact id.</p>
  <div class="row c3">
   <div><label>Enabled</label><input type="checkbox" id="touEnabled" style="width:auto"/></div>
   <div><label>Timezone (empty = schedule TZ)</label><input id="touTz" placeholder="Asia/Shanghai"/></div>
   <div><label>Default policy</label><select id="touPolicy"><option value="off">off</option><option value="normal">normal</option></select></div>
  </div>
  <h3>Global tier rates (multipliers of the base price)</h3>
  <div class="row c3">
   <div><label>Peak rate</label><input id="trate_peak" type="number" step="0.05" min="0"/></div>
   <div><label>Off-peak rate</label><input id="trate_offpeak" type="number" step="0.05" min="0"/></div>
   <div><label>Normal rate</label><input id="trate_normal" type="number" step="0.05" min="0"/></div>
  </div>
  <div class="row c3">
   <div><label>Peak windows</label><div id="wins_peak"></div><button class="small" onclick="addWin('peak')">+ window</button></div>
   <div><label>Off-peak windows</label><div id="wins_offpeak"></div><button class="small" onclick="addWin('offpeak')">+ window</button></div>
   <div><label>Normal windows</label><div id="wins_normal"></div><button class="small" onclick="addWin('normal')">+ window</button></div>
  </div>
  <h3>Providers (first path segment)</h3>
  <div id="provs"></div>
  <button class="small" onclick="addProv()">+ provider</button>
  <h3>Model overrides (exact model id)</h3>
  <div id="toumodels"></div>
  <button class="small" onclick="addTouModel()">+ model override</button>
  <h3>Holidays (whole day cheapest tier)</h3>
  <div id="holidays"></div>
  <button class="small" onclick="addHoliday()">+ holiday</button>
  <div class="row" style="margin-top:10px;max-width:520px">
   <input id="holYear" placeholder="year, e.g. 2026"/>
   <input id="holCountry" placeholder="country code, e.g. CN"/>
   <button onclick="fetchHolidays()">Fetch holidays (date.nager.at)</button>
  </div>
 </details>
</main>
<div class="toast" id="toast"></div>
<script>
// ===== Quota Keeper admin page =====
// Fetch policy (zero polling; every stale-able section has an explicit
// Refresh button; nothing fires on a timer except the toast fade-out):
//   /me on load; admins additionally fetch /stats (current span + filters),
//   /recent (manual Refresh button only), /pricing summary (header meta),
//   /config + /users + /groups (config sections); /pricing?full=1 is fetched
//   ONLY when the Pricing editor is first expanded or its Refresh is pressed.
// =====
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(msg,ms=2200){const t=$('toast');t.textContent=msg;t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),ms)}
async function api(path,opts){const r=await fetch('__QK_API_PREFIX__'+path,opts);if(!r.ok){throw new Error(await r.text()||r.status)}return r.json()}
function fmt(n,d=2){if(n===null||n===undefined||isNaN(n))return '–';return Number(n).toLocaleString(undefined,{maximumFractionDigits:d})}
// num: zero-safe number parse — 0 survives, empty/NaN falls back to def
const num=(id,def)=>{const v=parseFloat($(id).value);return isNaN(v)?def:v};

// Categorical palette (dark, 8 slots in fixed order) — validated with the
// dataviz palette checker against the card surface #1e293b: all slots clear
// 3:1 contrast (slot 4 lifted to #1f9d4d for that); CVD separation sits in
// the 8-12 floor band, made legal by the legend + per-segment titles + the
// models table view as secondary encoding. Color follows the entity: a model
// keeps its slot across buckets, ranks are computed once per span.
const SERIES_COLORS=['#3987e5','#199e70','#c98500','#1f9d4d','#9085e9','#e66767','#d55181','#d95926'];
const OTHER_COLOR='#64748b';

const STATE={
  me:null,cfg:null,users:[],groups:[],stats:null,recent:null,pricing:null,
  allModels:[],
  span:{key:localStorage.getItem('qk_span')||'7d',from:localStorage.getItem('qk_span_from')||'',to:localStorage.getItem('qk_span_to')||''},
  sort:(()=>{const p=localStorage.getItem('qk_sort')||'cost_usd:desc';const i=p.indexOf(':');return {col:i<0?'cost_usd':p.slice(0,i),dir:p.slice(i+1)==='asc'?'asc':'desc'}})(),
  filter:{user:'',model:''},
  drill:{},            // user_id -> /stats drilldown payload cache
  pe:{loaded:false,page:0,search:'',full:null,orig:{}}, // pricing editor state
  tou:null,            // live editable copy of cfg.tou
};

// ---------- entry ----------
async function init(){
  try{
    STATE.me=await api('/me');
    if((STATE.me.user||{}).role!=='admin'){renderPersonal();return}
    document.querySelectorAll('.admin-only').forEach(el=>el.classList.remove('admin-only'));
    await loadAdmin();
  }catch(e){toast('Load failed: '+e.message)}
}
async function loadAdmin(){
  [STATE.cfg,STATE.users,STATE.groups,STATE.pricing]=await Promise.all([api('/config'),api('/users'),api('/groups'),api('/pricing')]);
  STATE.tou=JSON.parse(JSON.stringify(STATE.cfg.tou||{}));
  ['secDash','secUsers','secModels','secRecent','secGeneral','secSchedule','secPricing','secGroups','secUserq','secPricingEditor','secTou'].forEach(id=>$(id).hidden=false);
  renderMeta();renderConfig();renderGroups();renderUsersQ();renderTou();initSpanUI();
  await loadStats();
}
function renderMeta(){
  const p=STATE.pricing||{};
  $('meta').textContent=(p.models?p.models+' models · ':'')+(p.fetched_at_iso?('updated '+new Date(p.fetched_at_iso).toLocaleString()):'no pricing yet');
}

// ---------- non-admin view ----------
function renderPersonal(){
  const me=STATE.me,u=me.user||{};
  const eff=me.effective_quota;
  const pct=(eff>0)?Math.min(100,(me.used_credits||0)/eff*100):null;
  const cls=pct===null?'':(pct>=100?'bad':pct>=80?'warn':'');
  const tier=(me.tou&&me.tou.current_tier)?me.tou.current_tier:'off';
  const trend=(me.trend||[]);
  const cost7=trend.reduce((s,d)=>s+((d.cost_usd||0)),0);
  const req7=trend.reduce((s,d)=>s+((d.requests||0)),0);
  // /me exposes period credits (used_credits) and today's cost; a month cost
  // in USD is not part of the /me payload, so the period figure is shown in
  // credits (that IS the month figure when quota_period=monthly).
  $('secPersonal').innerHTML=`
   <h2>${esc(u.name||u.id)}</h2>
   <p class="hint">${esc(u.email||'')} · role ${esc(u.role||'')}</p>
   <div class="kpis">
    <div class="kpi"><div class="lbl">Quota used</div>
     <div class="val">${pct===null?'∞':fmt(pct,1)+'%'}</div>
     ${pct===null?'<div class="small muted">no quota set</div>':`<div class="bar"><i class="${cls}" style="width:${pct.toFixed(1)}%"></i></div><div class="small muted">${fmt(me.used_credits,1)} / ${fmt(eff,0)} credits</div>`}</div>
    <div class="kpi"><div class="lbl">Multiplier</div>
     <div class="val">×${fmt(me.multiplier,2)}</div>
     <div class="small muted">current TOU tier: ${esc(tier)}</div></div>
    <div class="kpi"><div class="lbl">Today</div>
     <div class="val">$${fmt(me.today.cost_usd,2)}</div>
     <div class="small muted">${fmt(me.today.requests,0)} requests</div></div>
    <div class="kpi"><div class="lbl">Period credits</div>
     <div class="val">${fmt(me.used_credits,0)}</div>
     <div class="small muted">used this period (month if monthly)</div></div>
   </div>
   <p class="hint">7-day cost trend · $${fmt(cost7,2)} total, ${fmt(req7,0)} requests</p>
   ${sparkSvg(trend.map(d=>d.cost_usd||0),560,56,'#38bdf8')}`;
  $('secPersonal').hidden=false;
  $('meta').textContent='self-service view';
}

// ---------- SVG helpers ----------
function sparkSvg(values,w,h,color){
  if(!values||values.length<2)return '<div class="small muted">no trend</div>';
  const max=Math.max(...values),min=Math.min(...values),range=(max-min)||1;
  const pts=values.map((v,i)=>((i/(values.length-1)*w).toFixed(1))+','+((h-3-((v-min)/range)*(h-8)).toFixed(1))).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2"/></svg>`;
}

// ---------- span + stats ----------
function isoDay(d){const p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())}
function spanDates(){
  const k=STATE.span.key;
  if(k==='custom'){
    if(STATE.span.from&&STATE.span.to)return {from:STATE.span.from,to:STATE.span.to,gran:'day'};
    return null;
  }
  const days={'24h':0,'7d':6,'30d':29,'90d':89}[k]??6;
  const now=new Date(),from=new Date(now);
  from.setDate(now.getDate()-days);
  return {from:isoDay(from),to:isoDay(now),gran:k==='24h'?'hour':'day'};
}
function initSpanUI(){
  document.querySelectorAll('.spans button').forEach(b=>b.classList.toggle('active',b.dataset.span===STATE.span.key));
  if(STATE.span.key==='custom')$('customDates').style.display='flex';
  $('spanFrom').value=STATE.span.from||'';
  $('spanTo').value=STATE.span.to||'';
}
function setSpan(k){
  STATE.span.key=k;
  if(k==='custom'){
    $('customDates').style.display='flex';
    $('spanFrom').value=STATE.span.from||'';
    $('spanTo').value=STATE.span.to||'';
    if(!(STATE.span.from&&STATE.span.to))return;
  }else{
    $('customDates').style.display='none';
    STATE.span.from=STATE.span.to='';
  }
  localStorage.setItem('qk_span',k);
  document.querySelectorAll('.spans button').forEach(b=>b.classList.toggle('active',b.dataset.span===k));
  loadStats();
}
function setCustom(which){
  if(which==='from'){STATE.span.from=$('spanFrom').value;localStorage.setItem('qk_span_from',STATE.span.from)}
  else{STATE.span.to=$('spanTo').value;localStorage.setItem('qk_span_to',STATE.span.to)}
  if(STATE.span.from&&STATE.span.to)loadStats();
}
async function loadStats(){
  const sp=spanDates();
  if(!sp){toast('Set both custom dates');return}
  STATE.filter.user=$('fUser').value.trim();
  STATE.filter.model=$('fModel').value;
  const qs=new URLSearchParams({from:sp.from,to:sp.to,granularity:sp.gran});
  if(STATE.filter.user)qs.set('user',STATE.filter.user);
  if(STATE.filter.model)qs.set('model',STATE.filter.model);
  STATE.drill={}; // drill-down cache is span/filter-scoped: invalidate on reload
  try{
    STATE.stats=await api('/stats?'+qs.toString());
  }catch(e){toast('Stats load failed: '+e.message);return}
  if(!STATE.filter.model){STATE.allModels=(STATE.stats.models||[]).map(m=>m.model);fillModelSelect()}
  renderKpis();renderTrend();renderUsers();renderModels();renderUsersQ();
}
function fillModelSelect(){
  const sel=$('fModel');
  sel.innerHTML='<option value="">all</option>'+STATE.allModels.map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('');
  sel.value=STATE.filter.model||'';
}

// ---------- KPI cards ----------
function renderKpis(){
  const k=STATE.stats.kpi||{},cpu=Number(STATE.cfg.credits_per_usd)||1000;
  const tot=(k.tokens||{}).cached+(k.tokens||{}).input+(k.tokens||{}).output;
  // NOTE: the /stats `series` carries ONLY cost per bucket (see qk_stats), so
  // per-bucket trends exist for Cost/Credits; Requests/Tokens/Cache rate and
  // Unpriced show the span total instead of a fabricated sparkline.
  const ser=STATE.stats.series||[];
  const costPer=ser.map(b=>Object.values(b.by_model||{}).reduce((a,c)=>a+c,0));
  const cards=[
    {lbl:'Requests',val:fmt(k.requests||0,0)},
    {lbl:'Tokens',val:fmt(tot||0,0)},
    {lbl:'Cost $',val:'$'+fmt(k.cost_usd||0,2),sp:sparkSvg(costPer,140,34,'#38bdf8')},
    {lbl:'Credits',val:fmt((k.cost_usd||0)*cpu,0),sp:sparkSvg(costPer.map(v=>v*cpu),140,34,'#34d399')},
    {lbl:'Cache rate',val:fmt((k.cache_rate||0)*100,1)+'%'},
    {lbl:'Unpriced',val:fmt(k.unpriced_requests||0,0)},
  ];
  $('kpis').innerHTML=cards.map(c=>`<div class="kpi"><div class="lbl">${c.lbl}</div><div class="val">${c.val}</div>${c.sp||''}</div>`).join('');
}

// ---------- stacked trend chart ----------
function bucketLabel(b,hourMode){return hourMode?b.slice(11,13)+':00':b.slice(5)}
function renderTrend(){
  const box=$('trend');
  const ser=STATE.stats.series||[];
  const hourMode=spanDates()&&spanDates().gran==='hour';
  const totals={};
  ser.forEach(b=>{Object.entries(b.by_model||{}).forEach(([m,c])=>{totals[m]=(totals[m]||0)+c})});
  const ranked=Object.entries(totals).sort((a,b)=>b[1]-a[1]);
  if(!ranked.length){box.innerHTML='<p class="hint">No data for the selected span.</p>';$('trendLegend').innerHTML='';return}
  const top=ranked.slice(0,8).map(e=>e[0]);
  const othersCost=ranked.slice(8).reduce((s,e)=>s+e[1],0);
  const names=top.concat(othersCost>0?['Others']:[]);
  const colorOf={};top.forEach((m,i)=>colorOf[m]=SERIES_COLORS[i]);colorOf['Others']=OTHER_COLOR;
  const W=1040,H=250,pl=48,pr=12,pt=10,pb=26;
  const iw=W-pl-pr,ih=H-pt-pb;
  let ymax=0;
  ser.forEach(b=>{const s=Object.values(b.by_model||{}).reduce((a,c)=>a+c,0);if(s>ymax)ymax=s});
  const yt=v=>pt+ih-(v/ymax*ih);
  const rows=ser.map(b=>{const bm=b.by_model||{};return {label:b.bucket,parts:names.map(m=>bm[m]||0)}});
  let g=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Cost per bucket by model">`;
  [0,.25,.5,.75,1].forEach(f=>{
    const y=yt(ymax*f);
    g+=`<line x1="${pl}" y1="${y}" x2="${W-pr}" y2="${y}" stroke="rgba(148,163,184,.18)" stroke-width="1"/>`;
    g+=`<text x="${pl-6}" y="${y+4}" text-anchor="end" font-size="10" fill="#94a3b8">$${fmt(ymax*f,0)}</text>`;
  });
  const n=rows.length,bw=iw/n;
  rows.forEach((r,i)=>{
    let y0=yt(0);
    r.parts.forEach((v,pi)=>{
      if(v<=0)return;
      const h=yt(0)-yt(v),y=y0-h;
      g+=`<rect x="${(pl+i*bw+1).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(bw-2,1).toFixed(1)}" height="${h.toFixed(1)}" fill="${colorOf[names[pi]]}" stroke="rgba(15,23,42,.55)" stroke-width="1"><title>${esc(names[pi])}: $${fmt(v,2)}</title></rect>`;
      y0=y;
    });
    if(n<=16||i%Math.ceil(n/12)===0||i===n-1){
      g+=`<text x="${(pl+i*bw+bw/2).toFixed(1)}" y="${H-7}" text-anchor="middle" font-size="10" fill="#94a3b8">${esc(bucketLabel(r.label,hourMode))}</text>`;
    }
  });
  g+='</svg>';
  box.innerHTML=g;
  const legTot=m=>totals[m]||0;
  $('trendLegend').innerHTML=names.map(m=>`<span class="lg"><i style="background:${colorOf[m]}"></i>${esc(m)} <span class="muted">$${fmt(m==='Others'?othersCost:legTot(m),1)}</span></span>`).join('');
}

// ---------- users ranking ----------
function toggleSort(col){
  const s=STATE.sort;
  if(s.col===col){s.dir=s.dir==='asc'?'desc':'asc'}else{s.col=col;s.dir='desc'}
  localStorage.setItem('qk_sort',s.col+':'+s.dir);
  renderUsers();
}
function renderUsers(){
  renderSortInd();
  const cpu=Number(STATE.cfg.credits_per_usd)||1000;
  const rows=(STATE.stats.users||[]).slice();
  const q=$('qUser').value.toLowerCase();
  const f=rows.filter(r=>!q||((r.name||'')+' '+(r.email||'')+' '+r.user_id).toLowerCase().includes(q));
  const {col,dir}=STATE.sort;
  const tok=r=>Object.values(r.tokens||{}).reduce((a,b)=>a+b,0);
  const pct=r=>{const eff=(r.quota||0)*(r.multiplier||1);return eff>0?((r.cost_usd||0)*cpu/eff*100):null};
  const get=r=>col==='requests'?r.requests:col==='tokens'?tok(r):col==='credits'?(r.cost_usd||0)*cpu:col==='quota'?(pct(r)??-1):r.cost_usd;
  f.sort((a,b)=>{const va=get(a),vb=get(b);return (dir==='asc'?1:-1)*(va<vb?-1:va>vb?1:0)});
  const tb=$('uRank').querySelector('tbody');
  tb.innerHTML=f.map(r=>{
    const pp=pct(r);
    const cls=pp===null?'':(pp>=100?'bad':pp>=80?'warn':'');
    return `<tr class="clickable" data-uid="${esc(r.user_id)}" onclick="toggleDrill(this)">
     <td>${esc(r.name||r.user_id)}<br/><span class="small muted">${esc(r.email||'')}</span></td>
     <td class="num">${fmt(r.requests,0)}</td>
     <td class="num">${fmt(tok(r),0)}</td>
     <td class="num">$${fmt(r.cost_usd,2)}</td>
     <td class="num">${fmt((r.cost_usd||0)*cpu,0)}</td>
     <td>${pp===null?'<span class="muted">∞</span>':`<div class="bar"><i class="${cls}" style="width:${Math.min(pp,100).toFixed(1)}%"></i></div><span class="pct">${pp.toFixed(1)}%</span>`}</td>
     <td><span class="tag src-${r.quota_source||'none'}">${esc(r.quota_source||'none')}</span></td>
    </tr>`;}).join('')||'<tr><td colspan="7" class="empty">No users in span</td></tr>';
}
function renderSortInd(){
  document.querySelectorAll('#uRank th.sortable').forEach(th=>{
    th.classList.remove('asc','desc');
    if(th.dataset.sort===STATE.sort.col)th.classList.add(STATE.sort.dir);
  });
}
async function toggleDrill(tr){
  const uid=tr.dataset.uid;
  const next=tr.nextElementSibling;
  if(next&&next.classList.contains('drill')){next.remove();return}
  tr.parentElement.querySelectorAll('tr.drill').forEach(r=>r.remove());
  let data=STATE.drill[uid];
  if(data===undefined){
    const sp=spanDates();
    const dr=document.createElement('tr');dr.className='drill';
    dr.innerHTML='<td colspan="7" class="empty">loading…</td>';
    tr.after(dr);
    try{
      data=await api(`/stats?user=${encodeURIComponent(uid)}&from=${sp.from}&to=${sp.to}&granularity=day`);
    }catch(e){dr.innerHTML=`<td colspan="7" class="empty">${esc(e.message)}</td>`;return}
    STATE.drill[uid]=data;
    dr.remove();
  }
  const rows=(data.models||[]).map(m=>{
    const t=m.tokens||{};
    return `<tr><td>${esc(m.model)}</td><td class="num">${fmt(m.requests,0)}</td><td class="num">${fmt(t.cached,0)}</td><td class="num">${fmt(t.input,0)}</td><td class="num">${fmt(t.output,0)}</td><td class="num">$${fmt(m.cost_usd,2)}</td></tr>`;
  }).join('')||'<tr><td colspan="6" class="empty">No usage in span</td></tr>';
  const dr=document.createElement('tr');dr.className='drill';
  dr.innerHTML=`<td colspan="7"><table style="max-width:760px"><thead><tr><th>Model</th><th class="num">Requests</th><th class="num">Cached</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cost $</th></tr></thead><tbody>${rows}</tbody></table></td>`;
  tr.after(dr);
}
function exportCsv(){
  const cpu=Number(STATE.cfg.credits_per_usd)||1000;
  const q=$('qUser').value.toLowerCase();
  const f=(STATE.stats.users||[]).filter(r=>!q||((r.name||'')+' '+(r.email||'')+' '+r.user_id).toLowerCase().includes(q));
  const escC=v=>{const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s};
  const lines=[['user','email','requests','tokens','cost_usd','credits','quota','quota_source'].join(',')];
  f.forEach(r=>lines.push([escC(r.name||r.user_id),escC(r.email||''),r.requests||0,Object.values(r.tokens||{}).reduce((a,b)=>a+b,0),(r.cost_usd||0).toFixed(6),((r.cost_usd||0)*cpu).toFixed(2),r.quota??'',escC(r.quota_source||'')].join(',')));
  const blob=new Blob([lines.join('\n')],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);a.download='quota-keeper-users.csv';a.click();
  URL.revokeObjectURL(a.href);
}

// ---------- models table ----------
function renderModels(){
  const rows=STATE.stats.models||[];
  const tb=$('modelsT').querySelector('tbody');
  tb.innerHTML=rows.map((m,i)=>{
    const t=m.tokens||{},tou=m.tou||{};
    const ttags=['peak','offpeak','normal'].filter(n=>(tou[n]||0)>0).map(n=>`<span class="tag t-${n}">${n} ${fmt(tou[n],0)}</span>`).join('');
    const sv=m.cost_saved_usd||0;
    return `<tr>
     <td>${esc(m.model)}
       ${m.unpriced_requests>0?'<span class="tag unpriced">unpriced</span>':''}
       <button class="small" data-mi="${i}" onclick="matchModel(this)">match</button>
       <span class="match-out"></span></td>
     <td class="num">${fmt(m.requests,0)}</td>
     <td class="num">${fmt(m.users,0)}</td>
     <td class="num">${fmt(t.cached,0)}</td>
     <td class="num">${fmt(t.input,0)}</td>
     <td class="num">${fmt(t.output,0)}</td>
     <td class="num">$${fmt(m.cost_usd,2)}</td>
     <td class="num">${fmt(m.blended_per_m,2)}</td>
     <td class="num">${sv>0?'+':''}$${fmt(sv,2)}</td>
     <td>${ttags}</td>
    </tr>`;}).join('')||'<tr><td colspan="10" class="empty">No models in span</td></tr>';
}
// The /stats payload does not carry the fuzzy-matched pricing key per model
// row, so the target is resolved on demand via /pricing/match and shown
// inline as "→ target via how" (how: override|exact|suffix|segment|contains).
async function matchModel(btn){
  const m=STATE.stats.models[+btn.dataset.mi].model;
  const out=btn.parentElement.querySelector('.match-out');
  out.textContent='…';
  try{
    const r=await api('/pricing/match?model='+encodeURIComponent(m));
    if(r.matched){
      const i=r.how.indexOf(':');
      const how=i<0?r.how:r.how.slice(0,i),target=i<0?'':r.how.slice(i+1);
      out.textContent='→ '+target+' via '+how;
    }else{out.textContent='→ no match'}
  }catch(e){out.textContent='match error'}
}

// ---------- recent activity (manual refresh only) ----------
async function loadRecent(){
  try{
    STATE.recent=await api('/recent');
    renderRecent();
  }catch(e){toast('Recent load failed: '+e.message)}
}
function renderRecent(){
  const items=(STATE.recent?STATE.recent.items:[]).slice(0,200);
  const tb=$('recentT').querySelector('tbody');
  tb.innerHTML=items.map(it=>{
    const t=it.tokens||{};
    const ci=(t.cached||0)+(t.input||0);
    const cp=ci?((t.cached||0)/ci*100):0;
    const dt=new Date((it.ts||0)*1000);
    const p=n=>String(n).padStart(2,'0');
    const time=p(dt.getHours())+':'+p(dt.getMinutes())+':'+p(dt.getSeconds());
    const tier=(it.tou_tier&&it.tou_tier!=='off')?`<span class="tag t-${esc(it.tou_tier)}">${esc(it.tou_tier)}</span>`:'';
    return `<tr>
     <td class="small muted">${time}</td>
     <td>${esc(it.name||it.user_id)}<br/><span class="small muted">${esc(it.email||'')}</span></td>
     <td>${esc(it.model)}${it.priced===false?' <span class="tag unpriced">unpriced</span>':''}</td>
     <td class="num">${fmt(t.cached,0)}</td>
     <td class="num">${fmt(t.input,0)}</td>
     <td class="num">${fmt(t.output,0)}</td>
     <td class="num">${fmt(cp,0)}%</td>
     <td class="num">$${fmt(it.cost_usd,4)}</td>
     <td>${tier}</td></tr>`;}).join('')||'<tr><td colspan="9" class="empty">No activity recorded yet — send a chat through the filter first.</td></tr>';
}

// ---------- config sections ----------
function renderConfig(){
  $('credits_per_usd').value=STATE.cfg.credits_per_usd;
  $('quota_period').value=STATE.cfg.quota_period||'daily';
  $('default_quota_credits').value=STATE.cfg.default_quota_credits??'';
  const s=STATE.cfg.schedule||{};
  $('schedule_timezone').value=s.timezone??'';
  $('night_start_hour').value=s.night_start_hour??22;
  $('night_end_hour').value=s.night_end_hour??8;
  $('night_multiplier').value=s.night_multiplier??1;
  $('weekend_multiplier').value=s.weekend_multiplier??1;
  const p=STATE.cfg.pricing||{};
  $('pricing_url').value=p.url||'';
  $('refresh_hours').value=p.refresh_hours??24;
  $('default_pricing').value=p.default_pricing?JSON.stringify(p.default_pricing):'';
  $('mult_now').textContent='Current time multiplier: ×'+(STATE.cfg._time_multiplier??STATE.me.multiplier??1);
}
function userGroups(u){
  return STATE.groups.filter(g=>(g.members||[]).includes(u.id)).map(g=>g.id);
}
function resolveQuota(u){
  const uq=(STATE.cfg.user_quotas||{})[u.id];
  if(typeof uq==='number'&&uq>0)return {q:uq,src:'user'};
  const gq=STATE.cfg.group_quotas||{};
  const vals=userGroups(u).map(g=>gq[g]).filter(v=>typeof v==='number'&&v>0);
  if(vals.length)return {q:Math.max(...vals),src:'group'};
  if(typeof STATE.cfg.default_quota_credits==='number'&&STATE.cfg.default_quota_credits>0)return {q:STATE.cfg.default_quota_credits,src:'default'};
  return {q:null,src:'none'};
}
function renderGroups(){
  const tb=$('groups').querySelector('tbody');tb.innerHTML='';
  STATE.groups.forEach(g=>{
    const v=(STATE.cfg.group_quotas||{})[g.id];
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${esc(g.name)}<br/><span class="small muted">${esc(g.id)}</span></td>
    <td><input data-gq="${esc(g.id)}" type="number" step="0.01" value="${v??''}" placeholder="inherit"/></td>
    <td class="num">${fmt(g.members?g.members.length:0,0)}</td>`;
    tb.appendChild(tr);
  });
  if(!STATE.groups.length)tb.innerHTML='<tr><td colspan="3" class="empty">No groups</td></tr>';
}
function renderUsersQ(){
  const tb=$('userq').querySelector('tbody');tb.innerHTML='';
  const cpu=Number(STATE.cfg.credits_per_usd)||1000;
  const byId={};(STATE.stats?STATE.stats.users:[]).forEach(r=>byId[r.user_id]=r);
  STATE.users.forEach(u=>{
    const sr=byId[u.id];
    const {q,src}=resolveQuota(u);
    const quota=(sr&&sr.quota)??q;
    const used=((sr?sr.cost_usd:0)||0)*cpu;
    const eff=quota*((sr&&sr.multiplier)||STATE.me.multiplier||1);
    const pct=eff>0?Math.min(100,used/eff*100):null;
    const cls=pct===null?'':(pct>=100?'bad':pct>=80?'warn':'');
    const v=(STATE.cfg.user_quotas||{})[u.id];
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${esc(u.name||u.id)}<br/><span class="small muted">${esc(u.email||'')} ${u.role==='admin'?'<span class="tag">admin</span>':''}</span></td>
    <td><input data-uq="${esc(u.id)}" type="number" step="0.01" value="${v??''}" placeholder="inherit"/></td>
    <td><span class="tag src-${src}">${esc(src)}</span></td>
    <td class="num">${fmt(used,1)}</td>
    <td>${pct===null?'<span class="muted">∞</span>':`<div class="bar"><i class="${cls}" style="width:${pct.toFixed(1)}%"></i></div><span class="pct">${pct.toFixed(1)}%</span>`}</td>`;
    tb.appendChild(tr);
  });
  if(!STATE.users.length)tb.innerHTML='<tr><td colspan="5" class="empty">No users</td></tr>';
}

// ---------- save ----------
async function saveConfig(){
  const gq={},uq={};
  document.querySelectorAll('[data-gq]').forEach(i=>{const v=parseFloat(i.value);if(!isNaN(v)&&v>0)gq[i.dataset.gq]=v});
  document.querySelectorAll('[data-uq]').forEach(i=>{const v=parseFloat(i.value);if(!isNaN(v)&&v>0)uq[i.dataset.uq]=v});
  let dp=null;
  try{dp=$('default_pricing').value.trim()?JSON.parse($('default_pricing').value):null}catch(e){toast('Fallback pricing is not valid JSON');return}
  const body={
    credits_per_usd:num('credits_per_usd',1000),
    quota_period:$('quota_period').value,
    default_quota_credits:$('default_quota_credits').value===''?null:parseFloat($('default_quota_credits').value),
    schedule:{timezone:$('schedule_timezone').value.trim()||null,night_start_hour:num('night_start_hour',22),night_end_hour:num('night_end_hour',8),night_multiplier:num('night_multiplier',1),weekend_multiplier:num('weekend_multiplier',1)},
    // overrides: read from the editor when it has been opened; otherwise
    // keep whatever the config already holds (editor reads are authoritative
    // only after /pricing?full=1 has loaded once)
    pricing:{url:$('pricing_url').value.trim(),refresh_hours:num('refresh_hours',24),default_pricing:dp,overrides:STATE.pe.loaded?collectOverrides():((STATE.cfg.pricing||{}).overrides||{})},
    group_quotas:gq,user_quotas:uq,
    tou:buildTou(),
  };
  try{
    await api('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast('Config saved');
    STATE.cfg=await api('/config');
    STATE.tou=JSON.parse(JSON.stringify(STATE.cfg.tou||{}));
    renderConfig();renderGroups();renderUsersQ();renderTou();
    if(STATE.pe.loaded){rebuildPeOrig();renderPricingRows()}
    loadStats();
  }catch(e){toast('Save failed: '+e.message)}
}
async function refreshPricing(force){
  try{
    const r=await api('/pricing/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:!!force})});
    toast('Pricing '+r.status+' · '+r.models+' models');
    STATE.pricing=await api('/pricing');
    renderMeta();
    if(STATE.pe.loaded){rebuildPeOrig();renderPricingRows()}
  }catch(e){toast('Refresh failed: '+e.message)}
}
async function testMatch(){
  const m=$('matchTest').value.trim();if(!m)return;
  try{
    const r=await api('/pricing/match?model='+encodeURIComponent(m));
    $('matchResult').innerHTML=r.matched
     ?`✅ matched via <b>${esc(r.how)}</b> → input $${r.price.input}/1M · cached $${r.price.cached??'–'} · cache_write $${r.price.cache_write??'–'} · output $${r.price.output}/1M`
     :`❌ no match (fallback pricing applies if configured)`;
  }catch(e){$('matchResult').textContent='match error: '+e.message}
}

// ---------- pricing editor ----------
$('secPricingEditor').addEventListener('toggle',()=>{if($('secPricingEditor').open&&!STATE.pe.loaded)loadPricingFull()});
async function loadPricingFull(){
  try{
    STATE.pe.full=await api('/pricing?full=1');
    STATE.pe.loaded=true;
    STATE.pe.page=0;
    rebuildPeOrig();
    renderPricingRows();
  }catch(e){toast('Pricing table load failed: '+e.message)}
}
// baseline snapshot: table entries, with override rows marked manual (the
// override value is what the editor shows and what wins after a refresh)
function rebuildPeOrig(){
  const ov=(STATE.cfg.pricing||{}).overrides||{};
  const t=STATE.pe.full?(STATE.pe.full.table||{}):{};
  STATE.pe.orig={};
  Object.entries(t).forEach(([k,v])=>{
    const base={input:v.input??null,cached:v.cached??null,cache_write:v.cache_write??null,output:v.output??null};
    STATE.pe.orig[k]={manual:!!ov[k],cur:{},orig:base,base:base};
  });
  Object.entries(ov).forEach(([k,v])=>{
    const base={input:v.input??null,cached:v.cached??null,cache_write:v.cache_write??null,output:v.output??null};
    STATE.pe.orig[k]={manual:true,cur:{},orig:base,base:base};
  });
}
function renderPricingRows(){
  const q=STATE.pe.search;
  const keys=Object.keys(STATE.pe.orig).filter(k=>k.toLowerCase().includes(q)).sort();
  const per=50,pages=Math.max(1,Math.ceil(keys.length/per));
  if(STATE.pe.page>=pages)STATE.pe.page=pages-1;
  const pageKeys=keys.slice(STATE.pe.page*per,STATE.pe.page*per+per);
  const tb=$('peRows').querySelector('tbody');
  tb.innerHTML=pageKeys.map(k=>{
    const o=STATE.pe.orig[k];
    const base=o.base;
    return `<tr>
     <td>${esc(k)}${o.manual?' <span class="tag manual">manual</span>':''}</td>
     <td><input class="pe-num" type="number" step="0.01" min="0" data-pk="${esc(k)}" data-f="input" value="${esc(pv(o,base,'input'))}" oninput="peEdit(this)"/></td>
     <td><input class="pe-num" type="number" step="0.01" min="0" data-pk="${esc(k)}" data-f="cached" value="${esc(pv(o,base,'cached'))}" oninput="peEdit(this)"/></td>
     <td><input class="pe-num" type="number" step="0.01" min="0" data-pk="${esc(k)}" data-f="cache_write" value="${esc(pv(o,base,'cache_write'))}" oninput="peEdit(this)"/></td>
     <td><input class="pe-num" type="number" step="0.01" min="0" data-pk="${esc(k)}" data-f="output" value="${esc(pv(o,base,'output'))}" oninput="peEdit(this)"/></td>
    </tr>`;}).join('')||'<tr><td colspan="5" class="empty">No models match the search.</td></tr>';
  $('pePage').textContent=(STATE.pe.page+1)+' / '+pages+' · '+keys.length+' models';
  $('pePrev').disabled=STATE.pe.page===0;
  $('peNext').disabled=STATE.pe.page>=pages-1;
}
function peSearch(){STATE.pe.search=$('peSearch').value.trim().toLowerCase();STATE.pe.page=0;renderPricingRows()}
function pePage(d){STATE.pe.page+=d;renderPricingRows()}
// prefill helper: current edit if present, else the baseline value
function pv(o,base,f){return (o.cur[f]!==undefined)?(o.cur[f]):(base[f]??'')}
function peEdit(inp){
  const row=STATE.pe.orig[inp.dataset.pk];if(!row)return;
  const v=parseFloat(inp.value);
  row.cur[inp.dataset.f]=isNaN(v)?null:v;
}
function collectOverrides(){
  // DOM-independent: iterate every row in STATE.pe.orig (all table keys from
  // /pricing?full=1 plus existing overrides). peEdit keeps row.cur in sync on
  // input events, so rows not currently in the DOM (other pages / filtered by
  // search) keep their edits and their baseline values.
  // Emission rules:
  //  - a row with an existing override is always re-emitted (preservation)
  //  - a row is emitted only when its effective values differ from baseline
  //    (a cleared field falls back to orig: upstream baseline for table rows,
  //    the stored override for manual rows) and at least one effective value
  //    is non-empty
  //  - unedited baseline rows are never emitted
  const FIELDS=['input','cached','cache_write','output'];
  const ov={};
  Object.entries(STATE.pe.orig).forEach(([key,row])=>{
    const out={};
    FIELDS.forEach(f=>{out[f]=(row.cur[f]??row.orig[f])??null});
    const changed=FIELDS.some(f=>out[f]!==row.orig[f]);
    const hasVal=FIELDS.some(f=>out[f]!==null&&out[f]!==undefined);
    if(hasVal&&(row.manual||changed))ov[key]=out;
  });
  return ov;
}

// ---------- TOU editor ----------
function renderTou(){
  const t=STATE.tou||{};
  $('touEnabled').checked=!!t.enabled;
  $('touTz').value=t.timezone??'';
  $('touPolicy').value=t.default_policy||'off';
  ['peak','offpeak','normal'].forEach(n=>{$('trate_'+n).value=((t.tiers&&t.tiers[n]&&t.tiers[n].rate)!=null)?t.tiers[n].rate:1});
  ['peak','offpeak','normal'].forEach(n=>renderWins(n));
  renderProvs();renderTouModels();renderHolidays();
}
const DLAB=['Mo','Tu','We','Th','Fr','Sa','Su'];
const DVAL=[1,2,3,4,5,6,0]; // JS weekday: 0=Sunday
function winHtml(n,w){
  return `<div class="win" data-tier="${esc(n)}">
   ${DVAL.map((d,i)=>`<button type="button" class="chip${(w.days||[]).includes(d)?' on':''}" data-d="${d}" onclick="chip(this)">${DLAB[i]}</button>`).join('')}
   <input type="time" data-start value="${esc(w.start||'09:00')}"/>
   <input type="time" data-end value="${esc(w.end||'18:00')}"/>
   <button class="small" onclick="delWin(this)">remove</button>
  </div>`;
}
function renderWins(n){
  const box=$('wins_'+n);
  const wins=(STATE.tou.tiers&&STATE.tou.tiers[n]&&STATE.tou.tiers[n].windows)||[];
  box.innerHTML=wins.map(w=>winHtml(n,w)).join('');
}
function addWin(n){$('wins_'+n).insertAdjacentHTML('beforeend',winHtml(n,{}))}
function chip(b){b.classList.toggle('on')}
function delWin(b){b.closest('.win').remove()}
function provRowHtml(kind,name,p){
  const nk=kind==='provider'?'pname':'mname',ek=kind==='provider'?'pen':'men',rk=kind==='provider'?'prate':'mrate';
  return `<div class="prow" data-kind="${kind}">
   <input type="text" data-${nk} value="${esc(name)}" placeholder="${kind==='provider'?'provider name':'exact model id'}"/>
   <label class="inline"><input type="checkbox" data-${ek} ${p.enabled?'checked':''}/> enabled</label>
   ${['peak','offpeak','normal'].map(n=>`<input type="number" step="0.05" min="0" placeholder="${n}" data-${rk}="${n}" value="${esc((p.tiers&&p.tiers[n])?p.tiers[n].rate:'')}"/>`).join('')}
   <button class="small" onclick="this.closest('.prow').remove()">remove</button>
  </div>`;
}
function renderProvs(){
  const box=$('provs');box.innerHTML='';
  Object.entries(STATE.tou.providers||{}).forEach(([name,p])=>{box.insertAdjacentHTML('beforeend',provRowHtml('provider',name,p))});
}
function renderTouModels(){
  const box=$('toumodels');box.innerHTML='';
  Object.entries(STATE.tou.models||{}).forEach(([name,p])=>{box.insertAdjacentHTML('beforeend',provRowHtml('model',name,p))});
}
function addProv(){$('provs').insertAdjacentHTML('beforeend',provRowHtml('provider','',{enabled:true}))}
function addTouModel(){$('toumodels').insertAdjacentHTML('beforeend',provRowHtml('model','',{enabled:true}))}
function renderHolidays(){
  const box=$('holidays');box.innerHTML='';
  (STATE.tou.holidays||[]).forEach(h=>{
    box.insertAdjacentHTML('beforeend',`<div class="win"><input type="date" data-holiday value="${esc(h)}"/><button class="small" onclick="this.closest('.win').remove()">remove</button></div>`);
  });
  if(!(STATE.tou.holidays||[]).length)box.innerHTML='<p class="hint">No holidays configured.</p>';
}
function addHoliday(){
  $('holidays').insertAdjacentHTML('beforeend',`<div class="win"><input type="date" data-holiday/><button class="small" onclick="this.closest('.win').remove()">remove</button></div>`);
}
async function fetchHolidays(){
  const y=$('holYear').value.trim(),cc=$('holCountry').value.trim().toUpperCase();
  const DRE=/^\d{4}-\d{2}-\d{2}$/;
  if(!/^\d{4}$/.test(y)||!cc){toast('Enter a year and a country code');return}
  try{
    const r=await fetch('https://date.nager.at/api/v3/PublicHolidays/'+y+'/'+cc);
    if(!r.ok)throw new Error('HTTP '+r.status);
    const arr=await r.json();
    const hols=(STATE.tou.holidays||[]).slice();
    let added=0;
    (arr||[]).forEach(h=>{
      const d=String(h&&h.date?h.date:'');
      if(DRE.test(d)&&!hols.includes(d)){hols.push(d);added++}
    });
    hols.sort();
    STATE.tou.holidays=hols;
    renderHolidays();
    toast(added?('Added '+added+' holidays'):'No new holidays in the list');
  }catch(e){toast('Holiday fetch failed: '+e.message)}
}
function buildTou(){
  const t=JSON.parse(JSON.stringify(STATE.tou));
  t.enabled=$('touEnabled').checked;
  t.timezone=$('touTz').value.trim()||null;
  t.default_policy=$('touPolicy').value;
  ['peak','offpeak','normal'].forEach(n=>{
    const tier=t.tiers[n]=(t.tiers[n]||{});
    const rv=parseFloat($('trate_'+n).value);
    tier.rate=isNaN(rv)?(tier.rate??1):rv;
    const wins=[];
    document.querySelectorAll('.win[data-tier="'+n+'"]').forEach(w=>{
      const days=[];w.querySelectorAll('.chip.on').forEach(c=>days.push(+c.dataset.d));
      const start=w.querySelector('[data-start]').value,end=w.querySelector('[data-end]').value;
      if(start&&end)wins.push({days:days,start:start,end:end});
    });
    if(wins.length)tier.windows=wins;else delete tier.windows;
  });
  const provs={};
  document.querySelectorAll('.prow[data-kind="provider"]').forEach(r=>{
    const name=r.querySelector('[data-pname]').value.trim();if(!name)return;
    const pt={enabled:r.querySelector('[data-pen]').checked,tiers:{}};
    ['peak','offpeak','normal'].forEach(n=>{
      const v=parseFloat(r.querySelector('[data-prate="'+n+'"]').value);
      if(!isNaN(v))pt.tiers[n]={rate:v};
    });
    if(!Object.keys(pt.tiers).length)delete pt.tiers;
    provs[name]=pt;
  });
  t.providers=provs;
  const mods={};
  document.querySelectorAll('.prow[data-kind="model"]').forEach(r=>{
    const name=r.querySelector('[data-mname]').value.trim();if(!name)return;
    const pt={enabled:r.querySelector('[data-men]').checked,tiers:{}};
    ['peak','offpeak','normal'].forEach(n=>{
      const v=parseFloat(r.querySelector('[data-mrate="'+n+'"]').value);
      if(!isNaN(v))pt.tiers[n]={rate:v};
    });
    if(!Object.keys(pt.tiers).length)delete pt.tiers;
    mods[name]=pt;
  });
  t.models=mods;
  const hols=[];
  document.querySelectorAll('[data-holiday]').forEach(inp=>{const v=inp.value.trim();if(v&&!hols.includes(v))hols.push(v)});
  t.holidays=hols;
  return t;
}

init();
</script>
</body>
</html>
"""


def qk_build_page(api_prefix: str) -> str:
    return QK_PAGE.replace("__QK_API_PREFIX__", api_prefix)


def _mount_guard(app, prefix: str) -> bool:
    """Idempotently attach the API router; True when already mounted."""
    if any(getattr(r, "path", None) == f"{prefix}/config" for r in app.routes):
        return True
    app.include_router(qk_router, prefix=prefix)
    return False


# ==== Router ====
# Admin-only routes carry Depends(_require_admin) per route; /me is
# self-service and only requires an authenticated user (_require_user).

qk_router = APIRouter()


@qk_router.get("/config", dependencies=[Depends(_require_admin)])
async def api_config(request: Request):
    cfg = qk_get_config()
    cfg["_time_multiplier"] = qk_time_multiplier(cfg)
    return JSONResponse(cfg)


@qk_router.post("/config", dependencies=[Depends(_require_admin)])
async def api_save_config(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"errors": ["invalid JSON body"]}, status_code=400)
    errs = qk_validate_config(body)
    if errs:
        return JSONResponse({"errors": errs}, status_code=400)
    with qk_lock():
        cur = qk_load_json(QK_CONFIG_PATH, {})
        if not isinstance(cur, dict):
            log.warning("quota-keeper config.json was not an object; starting from defaults")
            cur = {}
        qk_deep_merge(cur, body)
        cfg = qk_merge_config(cur)
        qk_atomic_write(QK_CONFIG_PATH, cfg)
    return JSONResponse({"ok": True})


@qk_router.get("/users", dependencies=[Depends(_require_admin)])
async def api_users(request: Request):
    return JSONResponse(await _users_table())


@qk_router.get("/groups", dependencies=[Depends(_require_admin)])
async def api_groups(request: Request):
    return JSONResponse(await _groups_table())


@qk_router.get("/ledger", dependencies=[Depends(_require_admin)])
async def api_ledger(request: Request):
    return JSONResponse(qk_load_json(QK_LEDGER_PATH, {"users": {}}))


@qk_router.get("/pricing", dependencies=[Depends(_require_admin)])
async def api_pricing(request: Request):
    cache = qk_load_json(QK_PRICING_PATH, {}) or {}
    if request.query_params.get("full") == "1":
        return JSONResponse(cache)
    return JSONResponse({k: cache.get(k) for k in ("url", "fetched_at_iso", "models")})


@qk_router.get("/recent", dependencies=[Depends(_require_admin)])
async def api_recent(request: Request):
    rec = qk_load_json(QK_RECENT_PATH, {"items": []})
    return JSONResponse({"items": list(reversed(rec.get("items") or []))})


@qk_router.get("/stats", dependencies=[Depends(_require_admin)])
async def api_stats(request: Request):
    q = request.query_params
    return JSONResponse(
        qk_stats(q.get("from"), q.get("to"), q.get("user"), q.get("model"),
                 q.get("granularity", "day"))
    )


@qk_router.get("/me")
async def api_me(request: Request, user=Depends(_require_user)):
    cfg = qk_get_config()
    quota, source = qk_resolve_quota(cfg, {"id": user.id})
    mult = qk_time_multiplier(cfg)
    led = (qk_load_json(QK_LEDGER_PATH, {"users": {}}).get("users") or {}).get(user.id) or {}
    days = led.get("days") or {}
    now = qk_local_now(cfg)
    pref = now.strftime("%Y-%m-")
    used_month = sum((d or {}).get("cost_usd", 0) or 0 for k, d in days.items() if k.startswith(pref))
    used_day = ((days.get(now.strftime("%Y-%m-%d")) or {}).get("cost_usd", 0)) or 0
    cpu_ = float(cfg.get("credits_per_usd") or 1000.0)
    trend = []
    for i in range(6, -1, -1):
        kd = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        dd = days.get(kd) or {}
        trend.append({"day": kd, "requests": dd.get("requests", 0),
                      "cost_usd": dd.get("cost_usd", 0) or 0})
    return JSONResponse(
        {
            "user": {"id": user.id, "name": user.name, "email": user.email, "role": user.role},
            "quota": quota,
            "quota_source": source,
            "multiplier": mult,
            "effective_quota": (quota * mult) if quota is not None else None,
            "used_credits": used_month * cpu_ if (cfg.get("quota_period") == "monthly") else used_day * cpu_,
            "today": {"cost_usd": used_day,
                      "requests": (days.get(now.strftime("%Y-%m-%d")) or {}).get("requests", 0)},
            "trend": trend,
            "tou": {"current_tier": None},  # UI contract field; wired once the page has a per-user model list
        }
    )


@qk_router.post("/pricing/refresh", dependencies=[Depends(_require_admin)])
async def api_refresh(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        result = await asyncio.to_thread(
            qk_refresh_pricing, bool(body.get("force"))
        )
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@qk_router.get("/pricing/match", dependencies=[Depends(_require_admin)])
async def api_match(request: Request):
    model = request.query_params.get("model", "")
    cfg = qk_get_config()
    cache = qk_load_json(QK_PRICING_PATH, {}) or {}
    table = cache.get("table") or {}
    price, how = qk_find_pricing(
        model, table, (cfg.get("pricing") or {}).get("overrides")
    )
    if price is not None:
        return JSONResponse({"matched": True, "how": how, "price": price})
    return JSONResponse({"matched": False})


# ==== Background pricing refresh loop ====

async def _pricing_loop():
    while True:
        try:
            await asyncio.to_thread(qk_refresh_pricing, force=False)
        except Exception as e:
            log.info("quota-keeper pricing refresh failed: %s", e)
        await asyncio.sleep(600)


class Event:
    class Valves(BaseModel):
        route_prefix: str = Field(
            default="/quota", description="Path of the admin UI page"
        )
        api_prefix: str = Field(
            default="/api/v1/quota-keeper",
            description="Base path for config/pricing APIs",
        )
        enable_background_pricing_refresh: bool = Field(
            default=True, description="Periodically refresh the pricing table"
        )

    def __init__(self):
        self.valves = self.Valves()
        self._installed = False
        self._pricing_task = None

    async def event(
        self,
        event: dict,
        __event_name__: str = None,
        __id__: str = None,
        __app__=None,
        **kwargs,
    ):
        if __event_name__ not in (
            "system.startup.completed",
            "function.enable_started",
        ):
            return
        subject = (event.get("subject") or {}).get("id")
        if __event_name__ == "function.enable_started" and subject != __id__:
            return
        if __app__ is None or self._installed:
            return
        try:
            _mount_guard(__app__, self.valves.api_prefix)
            self._installed = True
            log.info("quota-keeper API mounted at %s", self.valves.api_prefix)

            page_path = self.valves.route_prefix
            if not page_path.startswith("/"):
                page_path = "/" + page_path

            async def _page(request: Request):
                return HTMLResponse(qk_build_page(self.valves.api_prefix))

            # avoid duplicate page routes across reloads
            if not any(
                getattr(r, "path", None) == page_path for r in __app__.routes
            ):
                __app__.get(
                    page_path, dependencies=[Depends(_require_admin)]
                )(_page)
            log.info("quota-keeper admin page at %s", page_path)

            if self.valves.enable_background_pricing_refresh:
                self._pricing_task = asyncio.create_task(_pricing_loop())
        except Exception as e:
            log.warning("quota-keeper setup failed: %s", e)
