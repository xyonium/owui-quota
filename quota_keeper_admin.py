"""
title: Quota Keeper - Admin UI
author: quota-keeper
version: 0.1.1
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
from datetime import datetime, timezone as _dt_timezone
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
main{max-width:1100px;margin:0 auto;padding:24px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
h2{margin:0 0 4px;font-size:15px;color:var(--acc)}
p.hint{margin:0 0 14px;color:var(--mut);font-size:12px}
label{display:block;margin:10px 0 4px;font-size:12px;color:var(--mut)}
input,select{width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:#0b1220;color:var(--txt);font:inherit}
input:focus,select:focus{outline:1px solid var(--acc)}
.row{display:grid;gap:12px}
@media(min-width:760px){.row.c2{grid-template-columns:1fr 1fr}.row.c3{grid-template-columns:1fr 1fr 1fr}}
button{padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:#0b1220;color:var(--txt);cursor:pointer;font:inherit}
button.primary{background:var(--acc);border-color:var(--acc);color:#082f49;font-weight:600}
button:hover{filter:brightness(1.1)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:12px}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:6px;border-radius:4px;background:#0b1220;overflow:hidden;min-width:120px}
.bar i{display:block;height:100%;background:var(--ok)}
.bar i.warn{background:var(--warn)}.bar i.bad{background:var(--bad)}
.pct{font-size:11px;color:var(--mut)}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);color:var(--mut)}
.tag.src-user{color:var(--acc);border-color:var(--acc)}
.tag.src-group{color:var(--ok);border-color:var(--ok)}
.tag.unpriced{color:var(--warn);border-color:var(--warn)}
.toast{position:fixed;right:18px;bottom:18px;padding:10px 16px;border-radius:10px;background:var(--card);border:1px solid var(--line);box-shadow:0 8px 30px rgba(0,0,0,.4);opacity:0;transition:.25s;z-index:99}
.toast.show{opacity:1}
.muted{color:var(--mut)}
.small{font-size:12px}
#matchResult{margin-top:8px;font-size:12px;color:var(--mut);word-break:break-all}
</style>
</head>
<body>
<header>
 <h1>📊 Quota Keeper</h1>
 <span class="badge" id="meta"></span>
 <span class="spacer"></span>
 <button onclick="saveConfig()">💾 Save config</button>
 <button class="primary" onclick="refreshPricing(true)">↻ Refresh pricing</button>
</header>
<main>
 <section>
  <h2>General</h2>
  <p class="hint">Credits are derived from real cost (USD); 1000 credits = $1 by default. Effective quota = resolved quota (user &gt; max group &gt; default) × time multiplier.</p>
  <div class="row c3">
   <div><label>Credits per USD</label><input id="credits_per_usd" type="number" step="0.01"/></div>
   <div><label>Quota period</label><select id="quota_period"><option value="daily">daily</option><option value="monthly">monthly</option></select></div>
   <div><label>Default quota (credits, empty = unlimited)</label><input id="default_quota_credits" type="number" step="0.01" placeholder="unlimited"/></div>
  </div>
 </section>

 <section>
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

 <section>
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

 <section>
  <h2>Group quotas (highest wins)</h2>
  <p class="hint">A user in several groups gets the max of those group quotas. A user-level quota overrides groups entirely.</p>
  <div style="overflow:auto;max-height:320px">
  <table id="groups"><thead><tr><th>Group</th><th style="width:180px">Quota (credits)</th><th class="num">Members</th></tr></thead><tbody></tbody></table>
  </div>
 </section>

 <section>
  <h2>User quotas (highest priority)</h2>
  <p class="hint">Leave empty to inherit from groups / default.</p>
  <div style="overflow:auto;max-height:420px">
  <table id="users"><thead><tr><th>User</th><th style="width:180px">Quota (credits)</th><th>Source</th><th>Used this period</th></tr></thead><tbody></tbody></table>
  </div>
 </section>

 <section>
  <h2>Usage this month (per model)</h2>
  <p class="hint">Metered from chat completion responses: cached / input / output tokens (cache-write included in cost). Unpriced rows had no pricing match and no fallback.</p>
  <div style="overflow:auto;max-height:460px">
  <table id="usage"><thead><tr><th>User</th><th>Model</th><th class="num">Requests</th><th class="num">Cached</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cost</th><th class="num">Credits</th></tr></thead><tbody></tbody></table>
  </div>
 </section>
</main>
<div class="toast" id="toast"></div>
<script>
let CFG=null, USERS=[], GROUPS=[], LEDGER=null, PRICING=null;
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(msg,ms=2200){const t=$('toast');t.textContent=msg;t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),ms)}
async function api(path,opts){const r=await fetch('__QK_API_PREFIX__'+path,opts);if(!r.ok){throw new Error(await r.text()||r.status)}return r.json()}
function fmt(n,d=0){if(n===null||n===undefined||isNaN(n))return '–';return Number(n).toLocaleString(undefined,{maximumFractionDigits:d})}
async function loadAll(){
 [CFG,USERS,GROUPS,LEDGER,PRICING]=await Promise.all([api('/config'),api('/users'),api('/groups'),api('/ledger'),api('/pricing')]);
 $('credits_per_usd').value=CFG.credits_per_usd;
 $('quota_period').value=CFG.quota_period||'daily';
 $('default_quota_credits').value=CFG.default_quota_credits??'';
 const s=CFG.schedule||{};
 $('schedule_timezone').value=s.timezone??'';
 $('night_start_hour').value=s.night_start_hour??22;
 $('night_end_hour').value=s.night_end_hour??8;
 $('night_multiplier').value=s.night_multiplier??1;
 $('weekend_multiplier').value=s.weekend_multiplier??1;
 const p=CFG.pricing||{};
 $('pricing_url').value=p.url||'';
 $('refresh_hours').value=p.refresh_hours??24;
 $('default_pricing').value=p.default_pricing?JSON.stringify(p.default_pricing):'';
 $('meta').textContent=(PRICING.models?PRICING.models+' models · ':'')+(PRICING.fetched_at_iso?('updated '+new Date(PRICING.fetched_at_iso).toLocaleString()):'no pricing yet');
 $('mult_now').textContent='Current time multiplier: ×'+(CFG._time_multiplier??1);
 renderGroups();renderUsers();renderUsage();
}
function userGroups(u){
 const byMembership=GROUPS.filter(g=>(g.members||[]).includes(u.id)).map(g=>g.id);
 return byMembership;
}
function resolveQuota(u){
 const uq=(CFG.user_quotas||{})[u.id];
 if(typeof uq==='number'&&uq>0)return {q:uq,src:'user'};
 const gq=CFG.group_quotas||{};
 const vals=userGroups(u).map(g=>gq[g]).filter(v=>typeof v==='number'&&v>0);
 if(vals.length)return {q:Math.max(...vals),src:'group'};
 if(typeof CFG.default_quota_credits==='number'&&CFG.default_quota_credits>0)return {q:CFG.default_quota_credits,src:'default'};
 return {q:null,src:'none'};
}
function periodKey(){
 const d=new Date();const pad=n=>String(n).padStart(2,'0');
 if((CFG.quota_period||'daily')==='monthly')return {prefix:d.getFullYear()+'-'+pad(d.getMonth()+1)+'-',day:null};
 return {prefix:null,day:d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())};
}
function usedCredits(u){
 const lu=(LEDGER?.users||{})[u.id];if(!lu)return 0;
 const cpu=Number(CFG.credits_per_usd)||1000;
 const {prefix,day}=periodKey();
 let usd=0;
 if(day){usd=(lu.days?.[day]?.cost_usd)||0}
 else{Object.entries(lu.days||{}).forEach(([k,d])=>{if(k.startsWith(prefix))usd+=(d.cost_usd||0)})}
 return usd*cpu;
}
function renderGroups(){
 const tb=$('groups').querySelector('tbody');tb.innerHTML='';
 GROUPS.forEach(g=>{
  const v=(CFG.group_quotas||{})[g.id];
  const tr=document.createElement('tr');
  tr.innerHTML=`<td>${esc(g.name)}<br/><span class="small muted">${esc(g.id)}</span></td>
  <td><input data-gq="${esc(g.id)}" type="number" step="0.01" value="${v??''}" placeholder="inherit"/></td>
  <td class="num">${fmt(g.members?.length||0)}</td>`;
  tb.appendChild(tr);
 });
 if(!GROUPS.length)tb.innerHTML='<tr><td colspan="3" class="muted" style="text-align:center;padding:18px">No groups</td></tr>';
}
function renderUsers(){
 const tb=$('users').querySelector('tbody');tb.innerHTML='';
 USERS.forEach(u=>{
  const {q,src}=resolveQuota(u);
  const used=usedCredits(u);
  const v=(CFG.user_quotas||{})[u.id];
  const pct=q?Math.min(100,used/q*100):null;
  const cls=pct===null?'':(pct>=100?'bad':pct>=80?'warn':'');
  const tr=document.createElement('tr');
  tr.innerHTML=`<td>${esc(u.name||u.id)}<br/><span class="small muted">${esc(u.email||'')} ${u.role==='admin'?'<span class="tag">admin</span>':''}</span></td>
  <td><input data-uq="${esc(u.id)}" type="number" step="0.01" value="${v??''}" placeholder="inherit"/></td>
  <td><span class="tag src-${src}">${src}</span>${q?`<div class="small muted">${fmt(used,1)} / ${fmt(q,0)} credits</div>`:''}</td>
  <td>${pct===null?'<span class="muted">∞</span>':`<div class="bar"><i class="${cls}" style="width:${pct}%"></i></div><span class="pct">${pct.toFixed(1)}%</span>`}</td>`;
  tb.appendChild(tr);
 });
 if(!USERS.length)tb.innerHTML='<tr><td colspan="4" class="muted" style="text-align:center;padding:18px">No users</td></tr>';
}
function renderUsage(){
 const tb=$('usage').querySelector('tbody');tb.innerHTML='';
 const rows=[];
 const d=new Date();const pad=n=>String(n).padStart(2,'0');
 const pref=d.getFullYear()+'-'+pad(d.getMonth()+1)+'-';
 Object.entries(LEDGER?.users||{}).forEach(([uid,u])=>{
  Object.entries(u.days||{}).forEach(([day,drec])=>{
   if(!day.startsWith(pref))return;
   Object.entries(drec.models||{}).forEach(([m,mm])=>{
    rows.push({name:u.name||uid,email:u.email||'',m,mm});
   });
  });
 });
 rows.sort((a,b)=>(b.mm.cost_usd||0)-(a.mm.cost_usd||0));
 const cpu=Number(CFG.credits_per_usd)||1000;
 rows.slice(0,200).forEach(r=>{
  const t=r.mm.tokens||{};
  const tr=document.createElement('tr');
  tr.innerHTML=`<td>${esc(r.name)}<br/><span class="small muted">${esc(r.email)}</span></td>
  <td>${esc(r.m)} ${r.mm.priced===false?'<span class="tag unpriced">unpriced</span>':''}</td>
  <td class="num">${fmt(r.mm.requests)}</td>
  <td class="num">${fmt(t.cached)}</td>
  <td class="num">${fmt(t.input)}</td>
  <td class="num">${fmt(t.output)}</td>
  <td class="num">$${fmt(r.mm.cost_usd,4)}</td>
  <td class="num">${fmt((r.mm.cost_usd||0)*cpu,1)}</td>`;
  tb.appendChild(tr);
 });
 if(!rows.length)tb.innerHTML='<tr><td colspan="8" class="muted" style="text-align:center;padding:24px">No usage this month yet — send a chat through the filter first.</td></tr>';
}
async function saveConfig(){
 // num: parse a numeric field; 0 survives, empty/NaN falls back to def
 const num=(id,def)=>{const v=parseFloat($(id).value);return isNaN(v)?def:v};
 const gq={},uq={};
 document.querySelectorAll('[data-gq]').forEach(i=>{const v=parseFloat(i.value);if(!isNaN(v)&&v>0)gq[i.dataset.gq]=v});
 document.querySelectorAll('[data-uq]').forEach(i=>{const v=parseFloat(i.value);if(!isNaN(v)&&v>0)uq[i.dataset.uq]=v});
 let dp=null;try{dp=$('default_pricing').value.trim()?JSON.parse($('default_pricing').value):null}catch(e){toast('Fallback pricing is not valid JSON');return}
 const body={
  credits_per_usd:num('credits_per_usd',1000),
  quota_period:$('quota_period').value,
  default_quota_credits:$('default_quota_credits').value===''?null:parseFloat($('default_quota_credits').value),
  schedule:{timezone:$('schedule_timezone').value.trim()||null,night_start_hour:num('night_start_hour',22),night_end_hour:num('night_end_hour',8),night_multiplier:num('night_multiplier',1),weekend_multiplier:num('weekend_multiplier',1)},
  pricing:{url:$('pricing_url').value.trim(),refresh_hours:num('refresh_hours',24),default_pricing:dp,overrides:(CFG.pricing||{}).overrides||{}},
  group_quotas:gq,user_quotas:uq,
 };
 try{await api('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});toast('Config saved');loadAll()}catch(e){toast('Save failed: '+e.message)}
}
async function refreshPricing(force){
 try{const r=await api('/pricing/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:!!force})});toast('Pricing '+r.status+' · '+r.models+' models');loadAll()}catch(e){toast('Refresh failed: '+e.message)}
}
async function testMatch(){
 const m=$('matchTest').value.trim();if(!m)return;
 try{const r=await api('/pricing/match?model='+encodeURIComponent(m));
  $('matchResult').innerHTML=r.matched
   ?`✅ matched via <b>${esc(r.how)}</b> → input $${r.price.input}/1M · cached $${r.price.cached??'–'} · cache_write $${r.price.cache_write??'–'} · output $${r.price.output}/1M`
   :`❌ no match (fallback pricing applies if configured)`;
 }catch(e){$('matchResult').textContent='match error: '+e.message}
}
loadAll().catch(e=>toast('Load failed: '+e.message));
</script>
</body>
</html>"""


def qk_build_page(api_prefix: str) -> str:
    return QK_PAGE.replace("__QK_API_PREFIX__", api_prefix)


def _mount_guard(app, prefix: str) -> bool:
    """Idempotently attach the API router; True when already mounted."""
    if any(getattr(r, "path", None) == f"{prefix}/config" for r in app.routes):
        return True
    app.include_router(qk_router, prefix=prefix)
    return False


# ==== Router ====

qk_router = APIRouter(dependencies=[Depends(_require_admin)])


@qk_router.get("/config")
async def api_config(request: Request):
    cfg = qk_get_config()
    cfg["_time_multiplier"] = qk_time_multiplier(cfg)
    return JSONResponse(cfg)


@qk_router.post("/config")
async def api_save_config(request: Request):
    body = await request.json()
    errs = qk_validate_config(body)
    if errs:
        return JSONResponse({"errors": errs}, status_code=400)
    with qk_lock():
        cur = qk_load_json(QK_CONFIG_PATH, {})
        qk_deep_merge(cur, body)
        cfg = qk_merge_config(cur)
        qk_atomic_write(QK_CONFIG_PATH, cfg)
    return JSONResponse({"ok": True})


@qk_router.get("/users")
async def api_users(request: Request):
    return JSONResponse(await _users_table())


@qk_router.get("/groups")
async def api_groups(request: Request):
    return JSONResponse(await _groups_table())


@qk_router.get("/ledger")
async def api_ledger(request: Request):
    return JSONResponse(qk_load_json(QK_LEDGER_PATH, {"users": {}}))


@qk_router.get("/pricing")
async def api_pricing(request: Request):
    return JSONResponse(qk_load_json(QK_PRICING_PATH, {}))


@qk_router.post("/pricing/refresh")
async def api_refresh(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        return JSONResponse(qk_refresh_pricing(bool(body.get("force"))))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@qk_router.get("/pricing/match")
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
            qk_refresh_pricing(force=False)
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
                asyncio.get_event_loop().create_task(_pricing_loop())
        except Exception as e:
            log.warning("quota-keeper setup failed: %s", e)
