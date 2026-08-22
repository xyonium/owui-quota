"""
title: Quota Keeper - Admin UI
author: quota-keeper
version: 0.5.33
required_open_webui_version: 0.10.0
description: Registers the /quota admin page to configure user/group quotas, pricing sources and time schedules, and refreshes model pricing from an upstream URL on a schedule. Pair with "Quota Keeper - Filter" which meters usage and enforces the quotas.
"""

import os
import re
import json
import time
import asyncio
import fnmatch
import logging
import threading
from datetime import datetime, timedelta, timezone as _dt_timezone
from contextlib import contextmanager
from typing import Optional

from pydantic import BaseModel, Field
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

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
    "model_aliases": {},                # upstream alias -> real model name (naming map,
                                        # applied before pricing/bucketing; NOT a price ref)
    "pricing": {
        # one URL string, or a list merged in order (first source wins on
        # conflicts); LiteLLM flat and models.dev nested formats both work
        "url": DEFAULT_PRICING_URL,
        "refresh_hours": 24,
        "default_pricing": None,
        "overrides": {},
    },
    "schedule": {
        "timezone": "Asia/Shanghai",               # None -> $TZ -> UTC (ledger day keys & TOU fallback TZ)
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
        # keys: exact model id, or a * glob (fnmatch, case-insensitive), e.g.
        # "*deepseek*" matches any id containing it; exact keys beat globs,
        # longer glob patterns beat shorter ones
        "models": {},
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

QK_PRICE_FIELDS = ("input", "cached", "cache_write", "output")


def qk_validate_config(cfg) -> list:
    errs = []
    if not isinstance(cfg, dict):
        return ["config must be an object"]
    if "credits_per_usd" in cfg and not (_QK_NUM(cfg["credits_per_usd"]) and cfg["credits_per_usd"] > 0):
        errs.append("credits_per_usd must be a positive number")
    if "quota_period" in cfg and cfg["quota_period"] not in (None, "daily", "weekly", "monthly"):
        errs.append("quota_period must be daily|weekly|monthly")
    for key in ("user_quotas", "group_quotas"):
        if key in cfg and not isinstance(cfg[key], dict):
            errs.append(f"{key} must be an object")
    ma = cfg.get("model_aliases")
    if ma is not None:
        if not isinstance(ma, dict):
            errs.append("model_aliases must be an object")
        else:
            for k, v in ma.items():
                if not isinstance(v, str) or not v.strip():
                    errs.append(f"model_aliases.{k} must map to a non-empty model name")
    sch = cfg.get("schedule")
    if sch is not None and not isinstance(sch, dict):
        errs.append("schedule must be an object")
    # night/weekend multiplier keys were removed in v0.4.0; legacy keys left
    # in config.json are ignored (not an error).
    pri = cfg.get("pricing")
    if pri is not None and not isinstance(pri, dict):
        errs.append("pricing must be an object")
    if isinstance(pri, dict):
        u = pri.get("url")
        if u is not None and not (
            isinstance(u, str)
            or (isinstance(u, list) and all(isinstance(x, str) for x in u))
        ):
            errs.append("pricing.url must be a string or a list of strings")
        ov = pri.get("overrides")
        if ov is not None and not isinstance(ov, dict):
            errs.append("pricing.overrides must be an object")
        elif isinstance(ov, dict):
            for k, v in ov.items():
                if v is None:
                    continue  # tombstone: cleared by a later POST's deep merge
                if not isinstance(v, dict):
                    errs.append(f"pricing.overrides.{k} must be an object or null")
                    continue
                # multiplier is validated for ALL shapes (alias / prices /
                # legacy-direct / multiplier-only), not just alias (v0.5.32)
                mult = v.get("multiplier")
                if mult is not None and not (_QK_NUM(mult) and mult > 0):
                    errs.append(f"pricing.overrides.{k}.multiplier must be a positive number")
                if "alias" in v:
                    if not isinstance(v.get("alias"), str) or not v["alias"].strip():
                        errs.append(f"pricing.overrides.{k}.alias must be a non-empty string")
                elif "multiplier" in v and not any(f in v for f in QK_PRICE_FIELDS) and "prices" not in v:
                    pass  # multiplier-only: scales the upstream table match; no price fields to check
                else:
                    pr = v.get("prices") if "prices" in v else v
                    if not isinstance(pr, dict):
                        errs.append(f"pricing.overrides.{k}.prices must be an object")
                        continue
                    for f in QK_PRICE_FIELDS:
                        fv = pr.get(f)
                        if fv is not None and not (_QK_NUM(fv) and fv >= 0):
                            errs.append(f"pricing.overrides.{k}.{f} must be a number >= 0")
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
    """models[exact] -> models[glob] (keys containing '*', fnmatch, longest
    pattern first) -> providers[first segment] -> default_policy.
    Returns a policy dict or None (off)."""
    tou = cfg.get("tou") or {}
    if not tou.get("enabled"):
        return None
    mid = str(model_id or "").strip().lower()
    models = tou.get("models") or {}
    mpol = models.get(mid)
    if isinstance(mpol, dict):
        return mpol if mpol.get("enabled", True) else None
    globs = [
        (str(k).strip().lower(), v)
        for k, v in models.items()
        if isinstance(v, dict) and "*" in str(k)
    ]
    # longest pattern first so *deepseek-chat* beats *deepseek*
    for pat, v in sorted(globs, key=lambda kv: -len(kv[0])):
        if fnmatch.fnmatchcase(mid, pat):
            return v if v.get("enabled", True) else None
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


def qk_fetch_pricing(url: str, timeout: int = 30, table: dict = None) -> dict:
    """Fetch one pricing source and merge it into `table` (a fresh dict when
    omitted). Existing keys win: when several sources are configured the
    FIRST listed source's entry for a model id is kept (source priority).

    Auto-detected formats: LiteLLM flat (key -> per-token costs) and
    models.dev nested (provider -> models -> per-1M costs). Prices are
    normalized to per-1M tokens. Zero-priced rows (models.dev free/
    plan-tier providers like kimi-for-coding / zai-coding-plan) are
    skipped: a $0 entry would shadow a real price from another source and
    meter 0 forever."""
    import requests

    if table is None:
        table = {}
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    raw = r.json()

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
            if (entry["input"] or 0) + (entry["output"] or 0) <= 0:
                continue  # zero-priced row
            table.setdefault(str(name).strip().lower(), entry)
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
                if (entry["input"] or 0) + (entry["output"] or 0) <= 0:
                    continue  # zero-priced row (free/plan tier)
                table.setdefault(f"{prov}/{name}".strip().lower(), entry)
                table.setdefault(str(name).strip().lower(), entry)
    if not table:
        raise ValueError("unrecognized pricing format")
    return table


def _qk_scale_price(price, mult):
    return {
        f: (float(v) * mult if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
        for f, v in (price or {}).items()
        if f in QK_PRICE_FIELDS
    }


def qk_find_pricing(model_id: str, table: dict, overrides: Optional[dict] = None):
    """override -> exact -> path-suffix -> tail-segment -> contains (all on
    raw and date-stripped variants). Returns (price|None, how|None).
    A matched entry whose input+output are both 0 counts as NOT priced
    (returns None): plan-tier/free $0 rows would otherwise meter 0 forever.

    Override value shapes (a None value means "cleared" and is skipped):
      legacy direct:  {"input": x, ..., "multiplier": m?}
      wrapped direct: {"prices": {...same...}, "multiplier": m?}
      alias:          {"alias": "<model key>", "multiplier": m?}
      multiplier-only:{"multiplier": m}   (scales the upstream table match)
    `multiplier` is INDEPENDENT of `alias` (v0.5.32): it scales a manual
    override (legacy or wrapped prices), an alias target, OR -- alone -- the
    price the upstream table matches. Alias targets resolve through the same
    matching chain (table lookup AND nested overrides, up to 8 hops,
    cycle-safe). Default multiplier is 1."""
    m = (model_id or "").strip().lower()
    if not m:
        return None, None
    ov = {str(k).strip().lower(): v for k, v in (overrides or {}).items()}

    def _mult(spec):
        v = spec.get("multiplier")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 1.0

    def _table_match(vs):
        """exact -> path-suffix -> tail-segment -> contains over the pricing
        table only (no overrides). Returns (price|None, how|None)."""
        if not table:
            return None, None
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

    def resolve(mid, depth):
        if depth > 8:
            return None, None
        vs = _qk_variants(mid)
        for cand in vs:
            spec = ov.get(cand)
            if not isinstance(spec, dict):
                continue
            mult = _mult(spec)
            if "alias" in spec:
                target = str(spec.get("alias") or "").strip().lower()
                if target and target != cand:
                    base, how = resolve(target, depth + 1)
                    if base is not None:
                        return _qk_scale_price(base, mult), "alias:" + target + ("*" + str(mult) if mult != 1.0 else "")
            elif "prices" in spec:
                p = spec.get("prices")
                if isinstance(p, dict):
                    return _qk_scale_price(p, mult), "override:" + cand + ("*" + str(mult) if mult != 1.0 else "")
            elif "multiplier" in spec and any(f in spec for f in QK_PRICE_FIELDS):
                # legacy direct price dict that ALSO carries a multiplier:
                # scale the manual prices, don't treat the multiplier key as a price
                return _qk_scale_price(spec, mult), "override:" + cand + ("*" + str(mult) if mult != 1.0 else "")
            elif "multiplier" in spec:
                # multiplier-only: scale whatever the upstream table matches
                base, how = _table_match(vs)
                if base is not None:
                    return _qk_scale_price(base, mult), how + "*" + str(mult)
                return None, None
            else:  # legacy direct price dict (price fields only, no multiplier)
                return spec, "override:" + cand
        return _table_match(vs)

    price, how = resolve(m, 0)
    if price is not None and ((price.get("input") or 0) + (price.get("output") or 0)) <= 0:
        return None, None  # zero-priced match = unpriced (do not meter 0 silently)
    return price, how


def qk_refresh_pricing(force: bool = False) -> dict:
    cfg = qk_get_config()
    pconf = cfg.get("pricing") or {}
    cache = qk_load_json(QK_PRICING_PATH, {}) or {}
    interval = float(pconf.get("refresh_hours") or 24)
    if not force:
        age = time.time() - float(cache.get("fetched_at") or 0)
        if age < interval * 3600:
            return {"status": "cached", "models": len(cache.get("table") or {})}
    # pricing.url accepts one URL or a list (first source wins on conflicts)
    urls = pconf.get("url") or DEFAULT_PRICING_URL
    if isinstance(urls, str):
        urls = [urls]
    urls = [u for u in urls if isinstance(u, str) and u.strip()] or [DEFAULT_PRICING_URL]
    table = {}
    errors = []
    for u in urls:
        try:
            table = qk_fetch_pricing(u.strip(), table=table)
        except Exception as e:
            # a single unreachable source (DNS/proxy blip) must not kill the
            # whole refresh -- the rest still merge and the error is reported
            errors.append(f"{u.strip()}: {e}")
            log.warning("quota-keeper pricing source failed: %s", errors[-1])
    if not table:
        raise ValueError("all pricing sources failed: " + " | ".join(errors))
    payload = {
        "url": urls[0] if len(urls) == 1 else urls,
        "sources": urls,
        "fetched_at": time.time(),
        "fetched_at_iso": datetime.now(_dt_timezone.utc).isoformat(),
        "models": len(table),
        "table": table,
    }
    if errors:
        payload["errors"] = errors
    with qk_lock():
        qk_atomic_write(QK_PRICING_PATH, payload)
    out = {"status": "refreshed", "models": len(table), "url": urls[0] if len(urls) == 1 else urls}
    if errors:
        out["status"] = "partial"
        out["errors"] = errors
    return out


def qk_time_multiplier(cfg: dict) -> float:
    """Deprecated night/weekend quota multipliers: removed in v0.4.0 (a
    time-varying quota ceiling hard-blocks users mid-period when the
    multiplier drops below their already-spent amount -- confusing and
    near-useless next to TOU pricing). Always 1; the schedule config key
    remains only for `timezone`."""
    return 1.0


def qk_user_group_ids(user: dict):
    gids = (user or {}).get("group_ids")
    if isinstance(gids, list) and gids:
        return [str(g) for g in gids]
    try:
        from open_webui.models.groups import Groups

        return [g.id for g in Groups.get_groups_by_member_id(user.get("id"))]
    except Exception:
        return []


_GROUP_IDS_CACHE = {}  # uid -> (expiry_epoch, [group_ids]); 5-min TTL


async def qk_user_group_ids_async(user: dict):
    """Async group-membership resolver for async handlers.

    OWUI >= 0.10 dropped group_ids from the injected UserModel and made
    Groups.* async; the sync fallback above then silently returns [] and
    group quotas never apply there. This resolves through the (possibly
    async) model and caches briefly, so membership changes take up to
    5 min to apply.
    """
    gids = (user or {}).get("group_ids")
    if isinstance(gids, list) and gids:
        return [str(g) for g in gids]
    uid = (user or {}).get("id")
    if not uid:
        return []
    ent = _GROUP_IDS_CACHE.get(uid)
    if ent and ent[0] > time.time():
        return list(ent[1])
    ids = []
    try:
        from open_webui.models.groups import Groups

        res = Groups.get_groups_by_member_id(uid)
        if asyncio.iscoroutine(res):  # OWUI >= 0.10 async models
            res = await res
        ids = [str(g.id) for g in res]
    except Exception:
        ids = []
    if len(_GROUP_IDS_CACHE) > 4096:
        _GROUP_IDS_CACHE.clear()
    _GROUP_IDS_CACHE[uid] = (time.time() + 300, ids)
    return ids


def _num(v):
    """True for numbers, but not bools (bool is an int subclass and must not
    be accepted as a quota/multiplier value)."""
    return not isinstance(v, bool) and isinstance(v, (int, float))


def qk_resolve_quota(cfg: dict, user: dict, group_ids=None):
    """user quota (if set) wins; otherwise the highest group quota; else default.

    group_ids: memberships pre-resolved via qk_user_group_ids_async; when
    None, falls back to the legacy sync qk_user_group_ids path."""
    uq = (cfg.get("user_quotas") or {}).get((user or {}).get("id"))
    if _num(uq) and uq > 0:
        return float(uq), "user"
    gq = cfg.get("group_quotas") or {}
    gids = group_ids if group_ids is not None else qk_user_group_ids(user or {})
    vals = [
        float(gq[str(g)])
        for g in gids
        if _num(gq.get(str(g))) and gq.get(str(g)) > 0
    ]
    if vals:
        return max(vals), "group"
    dq = cfg.get("default_quota_credits")
    if _num(dq) and dq > 0:
        return float(dq), "default"
    return None, "none"


def qk_cost_usd(tok: dict, price) -> float:
    # verbatim copy of the filter's cost model -- reprice must reproduce the
    # exact same numbers the filter would have written with this price
    if not price:
        return 0.0
    c = 0.0
    for field in ("input", "cached", "cache_write", "output"):
        p = price.get(field)
        if isinstance(p, (int, float)):
            c += (tok.get(field) or 0.0) * float(p) / 1e6
    return c


def qk_normalize_usage(u) -> Optional[dict]:
    """Normalize OpenAI / Anthropic / Responses API / generic usage into
    cached/input/output(+write). The OpenAI Responses API reports cache inside
    `input_tokens_details.cached_tokens` and its `input_tokens` is the TOTAL
    input (cache included); Anthropic's `input_tokens` excludes cache."""
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
    itd = u.get("input_tokens_details")
    if cached_oai is None and isinstance(itd, dict) and isinstance(itd.get("cached_tokens"), (int, float)):
        cached_oai = float(itd["cached_tokens"])

    if pt is not None:
        cached = (cached_oai or 0.0) + (cr or 0.0)
        inp = max(0.0, pt - cached)
        out = ct if ct is not None else (ao or 0.0)
    elif ai is not None or ao is not None:
        # input_tokens and/or output_tokens; a lone output_tokens appears in
        # Anthropic message_delta partial-usage events. For the Responses API
        # input_tokens includes the cached part (only when cache came via
        # input_tokens_details and Anthropic-style cache fields are absent).
        cached = (cached_oai or 0.0) + (cr or 0.0)
        inp = ai or 0.0
        if cached_oai is not None and cr is None:
            inp = max(0.0, inp - cached)
        out = ao or 0.0
    else:
        return None
    if inp == 0 and out == 0 and cached == 0 and not cw:
        return None
    return {"cached": cached, "input": inp, "output": out, "cache_write": cw or 0.0}


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


def qk_resolve_model_alias(cfg: dict, model: str) -> str:
    """Map an upstream-echoed alias (e.g. cli-proxy-api's prx.gemini-flash)
    to the real model name via config `model_aliases`. Applied BEFORE price
    matching and ledger bucketing, so an aliased request prices and
    aggregates under the real name. Unmapped models pass through unchanged
    (a price override that aliases glm-5.3 to glm-5.2 must NOT merge them —
    that's a pricing-only reference, this is a naming map)."""
    if not model:
        return str(model or "unknown")
    aliases = (cfg.get("model_aliases") or {})
    if isinstance(aliases, dict):
        real = aliases.get(model) or aliases.get(str(model).lower())
        if real:
            return str(real)
    return str(model)


def qk_record_usage(user: dict, model: str, tok: dict, count_request: bool = True,
                    now: datetime = None, channel: str = "api") -> None:
    """Record one usage event (verbatim copy of the filter's writer — the
    passthrough middleware meters direct API requests here). count_request=False
    marks a partial-usage topup for an id that already recorded: tokens/cost
    still accumulate but the request counters are not incremented again.
    `channel` is "webui" when the request came through the web UI, else "api"."""
    uid = (user or {}).get("id")
    if not uid:
        return
    cfg = qk_get_config()
    cache = JC.get(QK_PRICING_PATH, {}) or {}
    table = cache.get("table") or {}
    pconf = cfg.get("pricing") or {}
    model = qk_resolve_model_alias(cfg, model)
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
            dch = d.setdefault("channels", {"webui": 0, "api": 0})
            dch[channel if channel in dch else "api"] = dch.get(channel if channel in dch else "api", 0) + 1
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
                "channels": {"webui": 0, "api": 0},
                "models": {},
            },
        )
        if count_request:
            h["requests"] = h.get("requests", 0) + 1
            hch = h.setdefault("channels", {"webui": 0, "api": 0})
            hch[channel if channel in hch else "api"] = hch.get(channel if channel in hch else "api", 0) + 1
        h["cost_usd"] = round(h.get("cost_usd", 0.0) + cost, 8)
        for k in ("cached", "input", "output"):
            h["tokens"][k] = h["tokens"].get(k, 0.0) + (tok.get(k) or 0.0)
        if count_request:
            hm = h.setdefault("models", {}).setdefault(
                model,
                {"requests": 0, "cost_usd": 0.0,
                 "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
                 "channels": {"webui": 0, "api": 0}},
            )
            hm["requests"] = hm.get("requests", 0) + 1
            hm["cost_usd"] = round(hm.get("cost_usd", 0.0) + cost, 8)
            for k in ("cached", "input", "output"):
                hm["tokens"][k] = hm["tokens"].get(k, 0.0) + (tok.get(k) or 0.0)
            hchm = hm.setdefault("channels", {"webui": 0, "api": 0})
            hchm[channel if channel in hchm else "api"] = hchm.get(channel if channel in hchm else "api", 0) + 1
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
            mch = mm.setdefault("channels", {"webui": 0, "api": 0})
            mch[channel if channel in mch else "api"] = mch.get(channel if channel in mch else "api", 0) + 1
        mm["cost_usd"] = round(mm.get("cost_usd", 0.0) + cost, 8)
        mtou = mm.setdefault("tou", {"peak": 0, "offpeak": 0, "normal": 0})
        if count_request and tier in mtou:
            mtou[tier] = mtou.get(tier, 0) + 1
        mm["cost_saved_usd"] = round(mm.get("cost_saved_usd", 0.0) + (base_cost - cost), 8)
        for k in ("cached", "input", "output"):
            mm["tokens"][k] = mm["tokens"].get(k, 0.0) + (tok.get(k) or 0.0)
        # derived flag kept for UI back-compat (same semantics as the old
        # sticky AND: once an unpriced request occurred, stays false)
        mm["priced"] = mm.get("unpriced_requests", 0) == 0
        qk_prune_ledger(led, cfg)
        qk_atomic_write(QK_LEDGER_PATH, led)
        # recent.json ring buffer (dashboard "recent activity" feed), capped
        # at 200 newest-last; same lock so it stays consistent with the ledger.
        # Topups (count_request=False) are partial-usage merges for an already
        # recorded response, not new responses: they do not enter the feed.
        if count_request:
            rec = qk_load_json(QK_RECENT_PATH, {"items": []})
            items = rec.setdefault("items", [])
            items.append(
                {
                    "ts": time.time(),
                    "user_id": uid,
                    "name": u.get("name", ""),
                    "email": u.get("email", ""),
                    "model": model,
                    "tokens": {k: tok.get(k, 0.0) for k in ("cached", "input", "output")},
                    "cost_usd": cost,
                    "tou_tier": tier,
                    "priced": priced,
                    "channel": channel,
                }
            )
            del items[:-200]
            qk_atomic_write(QK_RECENT_PATH, rec)


# ==== passthrough API ingestion middleware (direct /messages & /responses) ====
#
# Open WebUI routes these as a pure upstream proxy (no filter inlet/stream/
# outlet), so the Filter never sees them. We hook a middleware at Event mount
# time that (a) captures the authenticated user from the request, (b) watches
# the response — non-streaming body or SSE chunks — for the usage object, and
# (c) records it into the same ledger/recent.json the Filter writes, with
# channel="api". Starlette builds the middleware stack lazily on the first
# request; Event functions mount during lifespan, before any request, so
# add_middleware here is legal on the pinned starlette (OWUI 0.11).

QK_INGEST_PATHS = frozenset(
    {
        "/api/v1/messages",   # Anthropic Messages passthrough (litellm mode)
        "/openai/responses",  # OpenAI Responses API passthrough
    }
)
QK_INGEST_MARK = "_quota_keeper_ingested"
QK_SSE_EVENT_MAX = 1 << 22  # 4 MiB guard against pathological chunks


def qk_ingest_parse_body_user(req) -> dict:
    """Extract the authenticated user from a Starlette Request. OWUI routes
    resolve the user via Depends(get_verified_user), which runs before the
    passthrough forward, so request.state.user (a UserModel) is populated by
    the time our response-side hook runs. Falls back to the forwarded
    X-OpenWebUI-User-* headers for resilience."""
    u = None
    try:
        u = getattr(req.state, "user", None)
    except Exception:
        pass
    if u is not None:
        uid = getattr(u, "id", None) or getattr(u, "id_str", None)
        if uid:
            return {
                "id": str(uid),
                "name": str(getattr(u, "name", "") or ""),
                "email": str(getattr(u, "email", "") or ""),
                "role": str(getattr(u, "role", "") or ""),
            }
    h = req.headers
    uid = h.get("x-openwebui-user-id") or h.get("x-open-webui-user-id")
    if not uid:
        return {}
    return {
        "id": uid,
        "name": h.get("x-openwebui-user-name") or h.get("x-open-webui-user-name", ""),
        "email": h.get("x-openwebui-user-email") or h.get("x-open-webui-user-email", ""),
        "role": h.get("x-openwebui-user-role") or h.get("x-open-webui-user-role", ""),
    }


def qk_ingest_body_model(body: bytes) -> str:
    try:
        obj = json.loads(body)
    except Exception:
        return ""
    m = obj.get("model") if isinstance(obj, dict) else None
    return str(m) if m else ""


def qk_ingest_extract_usage(data: dict) -> Optional[dict]:
    """Pull the usage object from a parsed JSON event/body. Handles the
    Anthropic streaming shapes (message_start carries cumulative usage,
    message_delta carries deltas), the OpenAI Responses API
    (response.completed carries response.usage), and the plain OpenAI
    completion shape (top-level or choices[0].usage)."""
    if not isinstance(data, dict):
        return None
    if data.get("type") == "message_start":
        msg = data.get("message")
        if isinstance(msg, dict):
            return qk_normalize_usage(msg.get("usage"))
        return None
    if data.get("type") == "message_delta":
        # Anthropic streams put output_tokens in the TOP-LEVEL usage of the
        # message_delta event (delta only carries stop_reason); some proxies
        # nest it under delta.usage instead — accept both.
        d = data.get("delta")
        u = data.get("usage")
        if u is None and isinstance(d, dict):
            u = d.get("usage")
        if isinstance(u, dict):
            return qk_normalize_usage(u)
        return None
    if data.get("type") == "response.completed":
        resp = data.get("response")
        if isinstance(resp, dict):
            return qk_normalize_usage(resp.get("usage"))
        return None
    u = data.get("usage")
    if isinstance(u, dict):
        return qk_normalize_usage(u)
    ch = data.get("choices")
    if isinstance(ch, list) and ch and isinstance(ch[0], dict):
        return qk_normalize_usage(ch[0].get("usage"))
    return None


def qk_ingest_merge_usage(acc: dict, part: dict) -> dict:
    """Merge a partial (delta) usage into an accumulator. Both are normalized
    {cached,input,output,cache_write}; cumulative messages (message_start /
    response.completed) overwrite, deltas add."""
    out = dict(acc)
    for k in ("cached", "input", "output", "cache_write"):
        v = part.get(k) or 0.0
        out[k] = (out.get(k) or 0.0) + v
    return out


def qk_ingest_scan_sse(chunks):
    """Scan SSE bytes for a usage object, merging message_start + message_delta
    (Anthropic) or accepting response.completed / top-level usage. Keeps a
    small rolling tail buffer so data: lines split across chunks still parse.

    Returns (model, tok) — the model echoed by the first event that carries
    one (anthropic message_start.message.model / responses response.model)."""
    acc = None
    model = ""
    buf = b""
    for chunk in chunks:
        buf += chunk
        if len(buf) > QK_SSE_EVENT_MAX:
            # pathological stream: drop from the head to bound memory
            buf = buf[-QK_SSE_EVENT_MAX:]
        while b"\n\n" in buf:
            raw, _, buf = buf.partition(b"\n\n")
            # an SSE event block may carry event:/id: lines before the
            # data: line; scan the block line by line
            for line in raw.split(b"\n"):
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == b"[DONE]":
                    continue
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                if not model:
                    if isinstance(ev.get("message"), dict) and ev["message"].get("model"):
                        model = str(ev["message"]["model"])
                    elif isinstance(ev.get("response"), dict) and ev["response"].get("model"):
                        model = str(ev["response"]["model"])
                    elif ev.get("model"):
                        model = str(ev["model"])
                part = qk_ingest_extract_usage(ev)
                if part is None:
                    continue
                # Anthropic message_start / message_delta carry CUMULATIVE
                # usage (delta's top-level usage is the running total, not an
                # increment) — each replaces the accumulator. response.completed
                # likewise holds the final totals.
                if ev.get("type") in ("message_start", "message_delta", "response.completed"):
                    acc = part
                elif acc is None:
                    acc = part
    return model, acc


def qk_ingest_record(req, body: bytes = b"", chunks=None) -> None:
    """Best-effort: resolve user+model, extract usage, write to the ledger.
    Never raises (fail-open — the API response must not be impacted).

    IMPORTANT: the middleware MUST NOT read the request body (reading it in
    the middleware broke passthrough — the route saw an empty payload and the
    upstream request fell back to a default model / 499'd). The model name is
    taken from the RESPONSE instead: anthropic messages and openai
    chat/responses bodies all echo the requested model."""
    try:
        if getattr(req.state, QK_INGEST_MARK, False):
            return  # already ingested (hot-reload re-mount double registration)
        setattr(req.state, QK_INGEST_MARK, True)
        user = qk_ingest_parse_body_user(req)
        if not user.get("id"):
            return
        tok = None
        model = ""
        if chunks is not None:
            model, tok = qk_ingest_scan_sse(chunks)
        elif body:
            try:
                data = json.loads(body)
                tok = qk_ingest_extract_usage(data)
                model = str(data.get("model") or "") if isinstance(data, dict) else ""
            except Exception:
                tok = None
        if not tok:
            return
        model = str(model or getattr(req.state, "qk_ingest_model", "") or "unknown")
        # Request ID dedup: the Filter's _seen is per-instance and may not see
        # this request at all; the passthrough has no stable response id, so
        # dedup is keyed on (user, model, ts, tokens) with a short TTL window.
        cfg = qk_get_config()
        now_local = qk_tou_local_now(cfg)
        qk_record_usage(user, model, tok, count_request=True, now=now_local, channel="api")
    except Exception as e:
        log.warning("quota-keeper passthrough ingest failed: %s", e)


async def qk_passthrough_middleware(request: Request, call_next):
    """Async middleware wrapping passthrough endpoints: forwards the request
    untouched, and records usage from the response (streaming or buffered)."""
    if request.method != "POST" or request.url.path not in QK_INGEST_PATHS:
        return await call_next(request)
    # NOTE: do NOT read request.body() here. Reading it in the middleware
    # consumed the payload for the passthrough route (the forwarded model
    # fell back to a default and requests 499'd). The model is extracted from
    # the response instead (qk_ingest_record / scan_sse).
    response = await call_next(request)
    try:
        is_sse = "text/event-stream" in (response.headers.get("content-type") or "")
        if getattr(response, "body_iterator", None) is not None and is_sse:
            chunks = []

            async def _tee():
                async for c in response.body_iterator:
                    chunks.append(c)
                    yield c
                # stream fully consumed: now safe to scan the collected bytes
                try:
                    qk_ingest_record(request, b"", chunks=chunks)
                except Exception:
                    pass

            teed = _tee()
            hdrs = dict(response.headers)
            # a streamed body has no meaningful content-length; keeping the
            # original one would truncate/mangle the forwarded stream
            hdrs.pop("content-length", None)
            return StreamingResponse(teed, status_code=response.status_code,
                                     headers=hdrs,
                                     media_type=response.media_type)
        # non-streaming (JSON etc.): buffer the body and scan it as JSON
        resp_body = b""
        async for c in response.body_iterator:
            resp_body += c
        qk_ingest_record(request, resp_body)
        return Response(content=resp_body, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)
    except Exception as e:
        log.warning("quota-keeper passthrough tee failed: %s", e)
        return response


# ==== stats aggregation (reads the ledger for serving; filter never calls it) ====


def qk_stats(from_=None, to=None, user=None, model=None, granularity="day",
             group_ids_map=None, window_start_ts=None):
    """Aggregate ledger usage into KPI/series/users/models views.

    Filters: `from_`/`to` are inclusive "YYYY-MM-DD" day bounds, `user` matches
    user id/name/email, `model` matches the exact model id. granularity "hour"
    buckets the series by "YYYY-MM-DDTHH" with cost summed under model key "_";
    anything else buckets per day with cost under the model id. A `model`
    filter restricts KPI, per-user and series day buckets to that model's
    contribution (hour buckets are per-day-per-hour across models and are
    skipped under a model filter).

    `window_start_ts` is retained only for signature compatibility -- rolling
    windows (the "24h" span) are served from recent.json by qk_stats_window,
    which api_stats routes to; this function ignores the parameter. The old
    bucket-trimming approach was dropped: it depended on hour buckets,
    timezone conversions and day-boundary special cases all lining up, and a
    miss read as a silent 0 (v0.4.3 lesson).
    """
    led = qk_load_json(QK_LEDGER_PATH, {"users": {}})
    users = led.get("users") or {}
    kpi = {
        "requests": 0,
        "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
        "cost_usd": 0.0,
        "unpriced_requests": 0,
        "channels": {"webui": 0, "api": 0},
    }
    series, users_rows, models_rows = {}, [], {}
    series_rt = {}  # bucket -> {"requests": n, "tokens": t} (per-bucket KPI trends)
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
            "channels": {"webui": 0, "api": 0},
        }
        quota, source = qk_resolve_quota(
            cfg, {"id": uid},
            None if group_ids_map is None else group_ids_map.get(uid, []),
        )
        row["quota"], row["quota_source"] = quota, source
        row["multiplier"] = qk_time_multiplier(cfg)
        for day, drec in sorted((u.get("days") or {}).items()):
            if not (from_s <= day <= to_s):
                continue
            drec = drec or {}
            hours = drec.get("hours") or {}
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
                for cname, cnt in ((mm.get("channels") or {}).items()):
                    if cname in row["channels"]:
                        row["channels"][cname] += cnt or 0
                        kpi["channels"][cname] += cnt or 0
                for k in ("cached", "input", "output"):
                    tk = (mm.get("tokens") or {}).get(k, 0) or 0
                    row["tokens"][k] += tk
                    kpi["tokens"][k] += tk
                rt = series_rt.setdefault(day, {"requests": 0, "tokens": 0.0})
                rt["requests"] += mm.get("requests", 0)
                rt["tokens"] += sum((mm.get("tokens") or {}).get(k, 0) or 0 for k in ("cached", "input", "output"))
                day_ms = {model: mm}
            else:
                row["requests"] += drec.get("requests", 0)
                row["cost_usd"] += drec.get("cost_usd", 0) or 0
                row["unpriced_requests"] += sum(
                    (m2.get("unpriced_requests") or 0) for m2 in day_ms.values()
                )
                for cname, cnt in ((drec.get("channels") or {}).items()):
                    if cname in row["channels"]:
                        row["channels"][cname] += cnt or 0
                        kpi["channels"][cname] += cnt or 0
                for k in ("cached", "input", "output"):
                    tk = (drec.get("tokens") or {}).get(k, 0) or 0
                    row["tokens"][k] += tk
                    kpi["tokens"][k] += tk
                rt = series_rt.setdefault(day, {"requests": 0, "tokens": 0.0})
                rt["requests"] += drec.get("requests", 0)
                rt["tokens"] += sum((drec.get("tokens") or {}).get(k, 0) or 0 for k in ("cached", "input", "output"))
            for m, mm in day_ms.items():
                # history recorded the upstream alias (prx.*) before
                # model_aliases existed — merge into the real name
                m = qk_resolve_model_alias(cfg, m)
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
            for h, hrec in hours.items():
                if granularity == "hour" and not model:
                    try:
                        bkey = f"{day}T{int(h):02d}"
                    except Exception:
                        continue
                    series.setdefault(bkey, {})
                    series[bkey]["_"] = series[bkey].get("_", 0) + (
                        (hrec.get("cost_usd") or 0) if isinstance(hrec, dict) else 0
                    )
                    rt = series_rt.setdefault(bkey, {"requests": 0, "tokens": 0.0})
                    if isinstance(hrec, dict):
                        rt["requests"] += hrec.get("requests", 0) or 0
                        rt["tokens"] += sum(
                            (hrec.get("tokens") or {}).get(k, 0) or 0
                            for k in ("cached", "input", "output")
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
        "series": [
            {"bucket": b, "cost": v,
             "requests": (series_rt.get(b) or {}).get("requests", 0),
             "tokens": (series_rt.get(b) or {}).get("tokens", 0.0)}
            for b, v in sorted(series.items())
        ],
        "users": users_rows,
        "models": sorted(models_rows.values(), key=lambda x: -x["cost_usd"]),
    }


def qk_stats_window(window_start_ts, user=None, model=None, group_ids_map=None):
    """Rolling-window stats ("24h" span) — v0.5.17 rewrite.

    EVERYTHING (KPI, per-user, per-model) is summed from the ledger's HOUR
    buckets, which carry per-model breakdowns (v0.5.17+; older hours are
    backfilled from day×models). KPI == sum(users) == sum(models) by
    construction. No recent.json, no share scaling.
    """
    cfg = qk_get_config()
    try:
        wstart = float(window_start_ts)
    except Exception:
        wstart = 0.0
    led_all = qk_load_json(QK_LEDGER_PATH, {"users": {}}).get("users") or {}
    now_dt = qk_local_now(cfg)
    now_ts = now_dt.timestamp()
    kpi = {"requests": 0, "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
           "cost_usd": 0.0, "unpriced_requests": 0, "channels": {"webui": 0, "api": 0}}
    series, urows, mrows = {}, {}, {}
    series_rt = {}
    _unpriced_days_seen = set()
    for uid, u in led_all.items():
        if user and user not in (uid, (u or {}).get("name", ""), (u or {}).get("email", "")):
            continue
        for day, drec in ((u or {}).get("days") or {}).items():
            drec = drec or {}
            for hour_str, hrec in ((drec.get("hours") or {}).items()):
                if not isinstance(hrec, dict):
                    continue
                try:
                    bts = datetime.strptime(f"{day}T{int(hour_str):02d}", "%Y-%m-%dT%H").replace(
                        tzinfo=now_dt.tzinfo).timestamp()
                except Exception:
                    continue
                if not (wstart <= bts <= now_ts):
                    continue
                hreq = hrec.get("requests", 0) or 0
                hcost = hrec.get("cost_usd", 0) or 0
                if not model:
                    kpi["requests"] += hreq
                    kpi["cost_usd"] += hcost
                    for k in ("cached", "input", "output"):
                        kpi["tokens"][k] += (hrec.get("tokens") or {}).get(k, 0) or 0
                    hch = hrec.get("channels") or {}
                    for cname, cnt in hch.items():
                        if cname in kpi["channels"]:
                            kpi["channels"][cname] += cnt or 0
                # per-user row
                row = urows.get(uid)
                if row is None:
                    row = urows[uid] = {
                        "user_id": uid,
                        "name": (u or {}).get("name", ""),
                        "email": (u or {}).get("email", ""),
                        "requests": 0, "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
                        "cost_usd": 0.0, "models": set(), "unpriced_requests": 0,
                        "channels": {"webui": 0, "api": 0},
                    }
                    quota, source = qk_resolve_quota(
                        cfg, {"id": uid},
                        None if group_ids_map is None else group_ids_map.get(uid, []))
                    row["quota"], row["quota_source"] = quota, source
                    row["multiplier"] = qk_time_multiplier(cfg)
                if not model:
                    row["requests"] += hreq
                    row["cost_usd"] += hcost
                    for k in ("cached", "input", "output"):
                        row["tokens"][k] += (hrec.get("tokens") or {}).get(k, 0) or 0
                    for cname, cnt in hch.items():
                        if cname in row["channels"]:
                            row["channels"][cname] += cnt or 0
                # per-model rows inside the hour
                for m, hm in ((hrec.get("models") or {}).items()):
                    # history recorded the upstream alias (prx.*) before
                    # model_aliases existed — merge into the real name so
                    # stats agree with the alias config
                    m = qk_resolve_model_alias(cfg, m)
                    if model and m != model:
                        continue
                    hm = hm or {}
                    if model:
                        # model filter: KPI/user accumulate only this model's
                        # hour share
                        kpi["requests"] += hm.get("requests", 0) or 0
                        kpi["cost_usd"] += hm.get("cost_usd", 0) or 0
                        for k in ("cached", "input", "output"):
                            kpi["tokens"][k] += (hm.get("tokens") or {}).get(k, 0) or 0
                        hmch = hm.get("channels") or {}
                        for cname, cnt in hmch.items():
                            if cname in kpi["channels"]:
                                kpi["channels"][cname] += cnt or 0
                        row["requests"] += hm.get("requests", 0) or 0
                        row["cost_usd"] += hm.get("cost_usd", 0) or 0
                        for k in ("cached", "input", "output"):
                            row["tokens"][k] += (hm.get("tokens") or {}).get(k, 0) or 0
                        for cname, cnt in hmch.items():
                            if cname in row["channels"]:
                                row["channels"][cname] += cnt or 0
                    mk = mrows.get(m)
                    if mk is None:
                        mk = mrows[m] = {
                            "model": m, "requests": 0,
                            "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
                            "cost_usd": 0.0, "users": set(), "unpriced_requests": 0,
                            "tou": {"peak": 0, "offpeak": 0, "normal": 0}, "cost_saved_usd": 0.0,
                        }
                    mk["requests"] += hm.get("requests", 0) or 0
                    mk["cost_usd"] += hm.get("cost_usd", 0) or 0
                    mk["users"].add(uid)
                    for k in ("cached", "input", "output"):
                        mk["tokens"][k] += (hm.get("tokens") or {}).get(k, 0) or 0
                    row["models"].add(m)
                    # series cost keyed BY MODEL (v0.5.27): the trend chart
                    # stacks per-model cost per bucket, so the 24h view needs
                    # the same per-model split qk_stats already produces for
                    # day granularity -- not a single "_" aggregate.
                    mcost = hm.get("cost_usd", 0) or 0
                    if mcost:
                        bkey = f"{day}T{int(hour_str):02d}"
                        sb = series.setdefault(bkey, {})
                        sb[m] = sb.get(m, 0) + mcost
                # requests/tokens series stay model-agnostic (per-hour totals)
                bkey = f"{day}T{int(hour_str):02d}"
                series.setdefault(bkey, {})
                rt = series_rt.setdefault(bkey, {"requests": 0, "tokens": 0.0})
                rt["requests"] += hreq
                rt["tokens"] += sum((hrec.get("tokens") or {}).get(k, 0) or 0 for k in ("cached", "input", "output"))
                # unpriced: day-level models (hours have no unpriced field).
                # Count once per day that contributed a window hour.
                if day not in _unpriced_days_seen:
                    _unpriced_days_seen.add(day)
                    for mm in ((drec.get("models") or {}).values()):
                        kpi["unpriced_requests"] += (mm or {}).get("unpriced_requests", 0) or 0
    ci = kpi["tokens"]["cached"] + kpi["tokens"]["input"]
    kpi["cache_rate"] = (kpi["tokens"]["cached"] / ci) if ci else 0.0
    kpi["window_partial"] = False
    for row in urows.values():
        row["models"] = len(row["models"])
    for mk in mrows.values():
        mk["users"] = len(mk["users"])
        tot = sum(mk["tokens"].values())
        mk["blended_per_m"] = (mk["cost_usd"] * 1e6 / tot) if tot else 0.0
    return {
        "kpi": kpi,
        "series": [
            {"bucket": b, "cost": v,
             "requests": (series_rt.get(b) or {}).get("requests", 0),
             "tokens": (series_rt.get(b) or {}).get("tokens", 0.0)}
            for b, v in sorted(series.items())
        ],
        "users": list(urows.values()),
        "models": sorted(mrows.values(), key=lambda x: -x["cost_usd"]),
    }


def qk_reprice_ledger(days=30, model=None, dry_run=False):
    """Reprice ledger buckets that were recorded while a model had no price.

    For each (user, day, model) bucket with unpriced_requests > 0 whose model
    NOW resolves to a price (same override/match chain as the filter, then
    default_pricing), backfill ONLY the unpriced share of the cost: the full
    re-cost (qk_cost_usd on the bucket's tokens) is scaled by
    unpriced_requests/requests and added to the stored cost. unpriced_requests
    is zeroed, which also clears the UI "unpriced" tag.

    Why the share scaling: any priced request already contributes to the
    stored cost, so a cost-proportion skip guard would suppress every mixed
    bucket (e.g. glm-5.3 with one priced + one unpriced request) and the tag
    would never clear. Scaling by the unpriced request share tops a mixed
    bucket up by exactly its missing share instead of double-counting.

    Approximations (the ledger is day×model aggregates, not per-request):
    - TOU: charged at the CURRENT policy rate for "now" (exact when TOU is
      disabled, rate 1.0); historical tier mix is not reconstructable.
    - the unpriced share is assumed to hold the same token mix as the bucket
      average.

    recent.json is repriced in the same pass, under the same lock and the
    same price table, so the Recent feed always agrees with the Models/ledger
    numbers; each recent entry is an independent per-request record (own
    ts/tokens), so that side is exact (no share scaling needed).

    Returns a report dict (ledger + recent counts); dry_run=True computes
    without writing.
    """
    cfg = qk_get_config()
    cache = qk_load_json(QK_PRICING_PATH, {}) or {}
    table = cache.get("table") or {}
    pconf = cfg.get("pricing") or {}
    try:
        days = int(days)
    except Exception:
        days = 30
    days = max(1, min(days, 3650))
    now = qk_tou_local_now(cfg)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    # current TOU rate per model is time-dependent; repricing historical days
    # at "now" rate is the documented approximation (exact when TOU disabled)
    report = {"days_scanned": days, "cutoff": cutoff, "models": {},
              "cost_added_usd": 0.0, "buckets_repriced": 0, "dry_run": dry_run}
    with qk_lock():
        led = qk_load_json(QK_LEDGER_PATH, {"users": {}})
        users = led.get("users") or {}
        for uid, u in users.items():
            udays = (u or {}).get("days") or {}
            for day, drec in udays.items():
                if day < cutoff:
                    continue
                drec = drec or {}
                models = drec.get("models") or {}
                day_delta = 0.0
                day_saved = 0.0
                for m, mm in list(models.items()):
                    if model and m != model:
                        continue
                    mm = mm or {}
                    un = mm.get("unpriced_requests") or 0
                    if un <= 0:
                        continue
                    tok = mm.get("tokens") or {}
                    tot_tok = sum(float(tok.get(k) or 0.0) for k in ("cached", "input", "output"))
                    if tot_tok <= 0:
                        # nothing to reprice; just clear the stale flag
                        if not dry_run:
                            mm["unpriced_requests"] = 0
                            mm["priced"] = True
                        continue
                    price, _how = qk_find_pricing(m, table, pconf.get("overrides"))
                    if price is None:
                        price = pconf.get("default_pricing")
                    if price is None:
                        continue  # still no price: leave the flag alone
                    new_base = qk_cost_usd(tok, price)
                    if new_base <= 0:
                        if not dry_run:
                            mm["unpriced_requests"] = 0
                            mm["priced"] = True
                        continue
                    stored = float(mm.get("cost_usd") or 0.0)
                    # Only the UNPRICED share is backfilled: scale the full
                    # re-cost by unpriced_requests/requests so a mixed bucket
                    # (some requests already priced, e.g. glm-5.3 with one of
                    # each) is topped up by exactly its missing share, not
                    # double-counted to the full amount. A cost-proportion
                    # guard can't work here -- any priced request makes the
                    # stored cost a large fraction of the full re-cost.
                    reqs = mm.get("requests") or 0
                    share = (un / reqs) if reqs > 0 else 1.0
                    share = max(0.0, min(1.0, share))
                    rate, _tier = qk_tou_rate(cfg, m, now)
                    add_cost = new_base * share * rate
                    if add_cost <= 0:
                        if not dry_run:
                            mm["unpriced_requests"] = 0
                            mm["priced"] = True
                        continue
                    new_cost = stored + add_cost
                    delta = add_cost
                    mrep = report["models"].setdefault(
                        m, {"buckets": 0, "cost_added_usd": 0.0})
                    mrep["buckets"] += 1
                    mrep["cost_added_usd"] = round(mrep["cost_added_usd"] + delta, 8)
                    report["cost_added_usd"] = round(report["cost_added_usd"] + delta, 8)
                    report["buckets_repriced"] += 1
                    if dry_run:
                        continue
                    mm["cost_usd"] = round(new_cost, 8)
                    # cost_saved_usd tracks (base - charged); the backfilled
                    # share contributes (base_share - charged_share)
                    add_saved = (new_base * share) - add_cost
                    mm["cost_saved_usd"] = round(
                        float(mm.get("cost_saved_usd") or 0.0) + add_saved, 8)
                    mm["unpriced_requests"] = 0
                    mm["priced"] = True
                    day_delta += delta
                    day_saved += add_saved
                if day_delta and not dry_run:
                    drec["cost_usd"] = round(float(drec.get("cost_usd") or 0.0) + day_delta, 8)
                    drec["cost_saved_usd"] = round(
                        float(drec.get("cost_saved_usd") or 0.0) + day_saved, 8)
        # recent.json: each entry is an independent per-request record (own
        # ts/tokens/priced), so repricing is exact -- no aggregation, no mixed
        # buckets. Done under the SAME lock and the SAME price table as the
        # ledger so the Recent feed and the Models/Ledger numbers can never
        # disagree. Only entries inside the window (ts >= cutoff day start)
        # whose model now has a price are touched.
        rec_items_repriced = 0
        rec_cost_added = 0.0
        try:
            cutoff_ts = datetime.strptime(cutoff, "%Y-%m-%d").replace(
                tzinfo=now.tzinfo).timestamp()
        except Exception:
            cutoff_ts = 0.0
        rec = qk_load_json(QK_RECENT_PATH, {"items": []})
        ritems = rec.get("items") or []
        for it in ritems:
            it = it if isinstance(it, dict) else {}
            if it.get("priced", True):
                continue  # already priced (or pre-channel entries): skip
            try:
                ts = float(it.get("ts") or 0)
            except Exception:
                continue
            if ts < cutoff_ts:
                continue
            m = str(it.get("model") or "")
            if model and m != model:
                continue
            price, _how = qk_find_pricing(m, table, pconf.get("overrides"))
            if price is None:
                price = pconf.get("default_pricing")
            if price is None:
                continue  # still no price: leave the entry alone
            tok = it.get("tokens") or {}
            base = qk_cost_usd(tok, price)
            if base <= 0:
                it["priced"] = True
                continue
            rate, _tier = qk_tou_rate(cfg, m, now)
            new_cost = base * rate
            delta = new_cost - float(it.get("cost_usd") or 0.0)
            if not dry_run:
                it["cost_usd"] = round(new_cost, 8)
                it["priced"] = True
            rec_items_repriced += 1
            rec_cost_added = round(rec_cost_added + delta, 8)
        if not dry_run:
            qk_atomic_write(QK_LEDGER_PATH, led)
            if rec_items_repriced:
                qk_atomic_write(QK_RECENT_PATH, rec)
    report["recent_items_repriced"] = rec_items_repriced
    report["recent_cost_added_usd"] = rec_cost_added
    return report


# ==== Open WebUI integration ====


async def _qk_resolve_user(request: Request, response: Response, background_tasks: BackgroundTasks):
    """Resolve the OWUI session user, tolerating auth-signature drift across
    OWUI versions.

    Current builds: get_current_user(request, response, background_tasks,
    auth_token=Depends(bearer_security)); older ones took (request,
    auth_token=...). And get_verified_user is dependency-style everywhere
    supported -- it takes the resolved *user*, NOT the request (v0.2.1
    called it with the request, it touched request.role, and every call
    401'd).
    """
    try:
        from open_webui.utils import auth as _auth
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"auth unavailable: {e}")

    auth_token = None
    bearer = getattr(_auth, "bearer_security", None)
    if bearer is not None:
        try:
            auth_token = await bearer(request)  # HTTPBearer(auto_error=False)
        except Exception:
            auth_token = None

    user = None
    attempts = (
        {"response": response, "background_tasks": background_tasks, "auth_token": auth_token},
        {"auth_token": auth_token},
        {},
    )
    last_type_err = None
    for kw in attempts:
        try:
            user = await _auth.get_current_user(request, **kw)
            break
        except TypeError as e:
            last_type_err = e  # unexpected-kwarg mismatch -> retry narrower
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"auth failed: {e}")
    else:
        raise HTTPException(status_code=401, detail=f"auth failed: {last_type_err}")

    gv = getattr(_auth, "get_verified_user", None)
    if gv is not None:
        try:
            res = gv(user)
            user = await res if asyncio.iscoroutine(res) else res
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"auth failed: {e}")
    if user is None:
        raise HTTPException(status_code=401, detail="auth failed: no user")
    return user


async def _require_user(
    request: Request, response: Response, background_tasks: BackgroundTasks
):
    return await _qk_resolve_user(request, response, background_tasks)


async def _require_admin(
    request: Request, response: Response, background_tasks: BackgroundTasks
):
    user = await _require_user(request, response, background_tasks)
    if (getattr(user, "role", "") or "") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


async def _users_table():
    try:
        from open_webui.models.users import Users

        res = Users.get_users()
        if asyncio.iscoroutine(res):  # OWUI >= 0.10: async models
            res = await res
        if isinstance(res, dict):  # and paginated: {"users": [...], "total": n}
            res = res.get("users") or []
        return [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role,
            }
            for u in res
        ]
    except Exception as e:
        log.warning("quota-keeper users fetch failed: %s", e)
        return []


async def _groups_table():
    try:
        from open_webui.models.groups import Groups

        try:
            res = Groups.get_groups({})  # current builds require `filter`
        except TypeError:
            res = Groups.get_groups()  # older builds take no args
        if asyncio.iscoroutine(res):
            res = await res
        out = [
            {
                "id": g.id,
                "name": g.name,
                "members": list(getattr(g, "user_ids", None) or []),
            }
            for g in res
        ]
        # Current GroupResponse dropped user_ids (member_count only): fill
        # member ids via the bulk query so the users table can tag each
        # user's groups and the groups table shows real members.
        if out and all(not g["members"] for g in out):
            bulk = getattr(Groups, "get_group_user_ids_by_ids", None)
            if bulk is not None:
                try:
                    m = bulk([g["id"] for g in out])
                    if asyncio.iscoroutine(m):
                        m = await m
                    for g in out:
                        g["members"] = [str(u) for u in (m.get(g["id"]) or [])]
                except Exception as e:
                    log.info("quota-keeper group member fill failed: %s", e)
        return out
    except Exception as e:
        log.warning("quota-keeper groups fetch failed: %s", e)
        return []


async def qk_group_ids_map(uids):
    """uid -> [group_id] via OWUI's bulk membership query; None when the
    method is absent/fails (callers then use the per-user legacy path)."""
    try:
        from open_webui.models.groups import Groups

        bulk = getattr(Groups, "get_groups_by_member_ids", None)
        if bulk is None:
            return None
        res = bulk([str(u) for u in uids])
        if asyncio.iscoroutine(res):
            res = await res
        return {
            str(uid): [str(g.id) for g in groups]
            for uid, groups in (res or {}).items()
        }
    except Exception as e:
        log.info("quota-keeper group map fetch failed: %s", e)
        return None


# ==== UI HTML (self-contained, no build step) ====

QK_PAGE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Quota Keeper</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230f172a'/%3E%3Cpath d='M8 20a8 8 0 0 1 16 0' fill='none' stroke='%2338bdf8' stroke-width='2.4' stroke-linecap='round'/%3E%3Cline x1='16' y1='20' x2='21' y2='13.5' stroke='%2338bdf8' stroke-width='2.4' stroke-linecap='round'/%3E%3Ccircle cx='16' cy='20' r='2' fill='%2338bdf8'/%3E%3C/svg%3E"/>
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
input,select,textarea{width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:#0b1220;color:var(--txt);font:inherit}
input:focus,select:focus,textarea:focus{outline:1px solid var(--acc)}
textarea{resize:vertical;min-height:36px}
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
.tag.matched{color:var(--ok);border-color:var(--ok)}
.tag.unpriced{color:var(--warn);border-color:var(--warn)}
.tag.t-peak{color:var(--warn);border-color:var(--warn)}
.tag.t-offpeak{color:var(--acc);border-color:var(--acc)}
.tag.t-normal{color:var(--ok);border-color:var(--ok)}
.tag.manual{color:var(--acc);border-color:var(--acc)}
.tag.ch-webui{color:var(--ok);border-color:var(--ok)}
.tag.ch-api{color:var(--acc);border-color:var(--acc)}
.toast{position:fixed;right:18px;bottom:18px;padding:10px 16px;border-radius:10px;background:var(--card);border:1px solid var(--line);box-shadow:0 8px 30px rgba(0,0,0,.4);opacity:0;transition:.25s;z-index:99}
/* scrollbar matches the dark theme */
*{scrollbar-width:thin;scrollbar-color:var(--line) transparent}
*::-webkit-scrollbar{width:9px;height:9px}
*::-webkit-scrollbar-track{background:transparent}
*::-webkit-scrollbar-thumb{background:var(--line);border-radius:5px;border:2px solid transparent;background-clip:content-box}
*::-webkit-scrollbar-thumb:hover{background:var(--acc);border:2px solid transparent;background-clip:content-box}
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
.pe-num{width:110px;padding:6px 8px}
.pe-alias{width:150px;padding:6px 8px}
.pe-mult{width:64px;padding:6px 8px}
tr.pe-cleared td{opacity:.45}
.pageinfo{font-size:12px;color:var(--mut)}
.scroll{overflow:auto;max-height:440px}
/* sticky column headers inside every scrollable table. Opaque --card background
   covers rows scrolling underneath; box-shadow re-creates the header separator
   (under border-collapse a th border-bottom does NOT stick with the cell). */
.scroll thead th{position:sticky;top:0;z-index:2;background:var(--card);box-shadow:0 1px 0 var(--line)}
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
.fatal{margin:0 0 18px;padding:12px 16px;border:1px solid var(--bad);border-radius:10px;color:var(--bad);background:rgba(248,113,113,.08);white-space:pre-wrap;word-break:break-all}
/* .admin-only must beat the display rules of layout classes it is combined
   with (.filters{display:flex}, .kpis{display:grid}, ...). Those are defined
   ABOVE the original .admin-only rule, so at equal specificity the later
   layout rule won and admin-only elements (e.g. the reprice bar) stayed
   visible to non-admins. Reinforce it here, last, with higher specificity. */
.admin-only, .filters.admin-only, .kpis.admin-only, div.admin-only{display:none !important}
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
 <div class="fatal" id="fatal" hidden></div>
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
    <th class="num" title="requests with a web-UI chat_id (vs direct API) -- compare against Open WebUI analytics">WebUI</th>
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
  <p class="hint">Blended $/M = cost per 1M tokens across all usage. The match button resolves the fuzzy pricing target via /pricing/match (the /stats payload does not carry the matched key per model) and shows it inline. <b>unpriced</b> means some recorded requests had no price at write time — after configuring a price, run reprice to backfill the last N days at the current price and clear the tag.</p>
  <div class="filters admin-only" id="repriceBar">
   <div class="f"><span>Backfill days</span><input id="repriceDays" type="number" value="30" min="1" max="3650" style="width:70px"/></div>
   <button onclick="reprice(null)">Reprice all unpriced</button>
   <span class="hint" id="repriceOut"></span>
  </div>
  <div class="scroll">
  <table id="modelsT">
   <thead><tr>
    <th>Model</th><th class="num">Requests</th><th class="num">Users</th>
    <th class="num">Cached</th><th class="num">Cache%</th><th class="num" title="Total input tokens (cached + cache-miss); billing still splits cached/miss/output by price">Input</th><th class="num">Output</th>
    <th class="num">Cost $</th><th class="num">Blended $/M</th><th class="num">Saved $</th><th>TOU</th>
   </tr></thead>
   <tbody></tbody>
  </table></div>
  <div id="pricePool" style="margin-top:18px">
   <h3 style="margin:0 0 4px">Available models &amp; prices</h3>
   <p class="hint">Every model used on this server (the local pool), with its resolved per-1M-token price — use this to pick a model, independent of the time-span stats above. <b>unpriced</b> means no price matched; it meters $0 until the admin configures one.</p>
   <div class="scroll">
   <table id="pricePoolT">
    <thead><tr>
     <th>Model</th><th class="num">Input $/M</th><th class="num">Cached $/M</th>
     <th class="num">Output $/M</th><th>Match</th><th>Status</th>
    </tr></thead>
    <tbody><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
   </table></div>
  </div>
 </section>

 <section id="secRecent" hidden>
  <h2>Recent activity</h2>
  <p class="hint">Newest first, max 200 rows. Manual refresh only — nothing polls.</p>
  <button onclick="loadRecent()">Refresh</button>
  <div class="scroll">
  <table id="recentT">
   <thead><tr>
    <th>Time</th><th>User</th><th>Model</th><th title="webui = sent from the Open WebUI page (chat_id present); api = direct API call">Via</th>
    <th class="num">Cached</th><th class="num" title="Total input tokens (cached + cache-miss)">Input</th><th class="num">Output</th>
    <th class="num">Cache%</th><th class="num">Cost $</th><th>Tier</th>
   </tr></thead>
   <tbody><tr><td colspan="10" class="empty">Press Refresh to load.</td></tr></tbody>
  </table></div>
 </section>

 <section id="secGeneral" hidden>
  <h2>General</h2>
  <p class="hint">Credits are derived from real cost (USD); 1000 credits = $1 by default. Effective quota = resolved quota (user &gt; max group &gt; default) × time multiplier.</p>
  <div class="row c3">
   <div><label>Credits per USD</label><input id="credits_per_usd" type="number" step="0.01"/></div>
   <div><label>Quota period</label><select id="quota_period"><option value="daily">daily</option><option value="weekly">weekly</option><option value="monthly">monthly</option></select></div>
   <div><label>Default quota (credits, empty = unlimited)</label><input id="default_quota_credits" type="number" step="0.01" placeholder="unlimited"/></div>
  </div>
 </section>

 <section id="secSchedule" hidden>
  <h2>Timezone</h2>
  <p class="hint">The timezone used for ledger day boundaries (daily/monthly quota periods) and as the fallback for TOU windows (TOU has its own optional timezone below). Defaults to Asia/Shanghai; set to empty to use the server TZ. The old night/weekend quota multipliers were removed in v0.4.0 — use TOU pricing for time-of-day economics.</p>
  <div class="row c3">
   <div><label>Timezone</label><input id="schedule_timezone" placeholder="Asia/Shanghai"/></div>
  </div>
 </section>

 <section id="secPricing" hidden>
  <h2>Pricing source</h2>
  <p class="hint">Supports LiteLLM model_prices_and_context_window.json (per-token converted to per-1M) and models.dev format. <b>Multiple sources</b>: one URL per line (or a JSON list) — they merge in order and the FIRST source wins when the same model id appears in several. Match order: override → exact → date-stripped → path suffix → segment → contains.</p>
  <div class="row c2">
   <div><label>Pricing URL(s), one per line</label><textarea id="pricing_url" rows="2"></textarea></div>
   <div><label>Refresh interval (hours)</label><input id="refresh_hours" type="number" min="0" step="1"/></div>
  </div>
  <label>Fallback pricing per 1M tokens when no match (JSON, optional)</label>
  <input id="default_pricing" placeholder='{"input":1,"cached":0.1,"output":2}'/>
  <label>Model aliases (upstream alias → real model name, JSON; e.g. {"prx.gemini-flash":"gemini-3.7-flash"} — merges the alias into the real model's stats/pricing)</label>
  <textarea id="model_aliases" rows="2" placeholder='{"prx.gemini-flash":"gemini-3.7-flash"}'></textarea>
  <div class="row" style="margin-top:10px">
   <input id="matchTest" placeholder="Type a model id to test matching, e.g. openai/gpt-4o-mini"/>
   <button onclick="testMatch()">Test match</button>
  </div>
  <div id="matchResult"></div>
 </section>

 <details id="secGroups" hidden>
  <summary>Group quotas (highest wins)</summary>
  <p class="hint">A user in several groups gets the max of those group quotas. A user-level quota overrides groups entirely.</p>
  <div class="scroll">
  <table id="groups"><thead><tr><th>Group</th><th style="width:180px">Quota (credits)</th><th class="num">Members</th></tr></thead><tbody></tbody></table>
  </div>
 </details>

 <details id="secUserq" hidden>
  <summary>User quotas (highest priority)</summary>
  <p class="hint">Leave empty to inherit from groups / default. Used column is the current stats span.</p>
  <div class="scroll">
  <table id="userq"><thead><tr><th>User</th><th style="width:180px">Quota (credits)</th><th>Source</th><th class="num">Used (span)</th><th class="num">Quota%</th></tr></thead><tbody></tbody></table>
  </div>
 </details>

 <details id="secPricingEditor" hidden>
  <summary>Pricing editor (your models → match &amp; overrides)</summary>
  <p class="hint">Lists models actually seen in usage (the real upstream model ids) — not the upstream price table, not Open WebUI's model registry (aliases/stale entries nobody calls). Each row shows whether the model currently resolves to a price and how (exact / suffix / segment / contains / override / alias). Two ways to fix a <b>no match</b> row: <b>direct prices</b> (per 1M), or an <b>alias</b> to an existing upstream key with a <b>multiplier</b> (e.g. <code>k3-256k → alias azure_ai/fw-kimi-k3 × 0.5</code>; the alias target must exist in the upstream table — use Test match to check). Overrides are saved into <code>pricing.overrides</code> and survive upstream refreshes.</p>
  <div class="pe-tools">
   <input type="search" id="peSearch" placeholder="search model" oninput="peSearch()"/>
   <label class="inline"><input type="checkbox" id="peOnlyUnpriced" onchange="peSearch()" style="width:auto"/> only unpriced</label>
   <button onclick="loadPricingFull()">Refresh</button>
   <button class="primary" onclick="saveConfig()">Save overrides</button>
   <span class="pageinfo" id="pePage"></span>
  </div>
  <div class="scroll">
  <table id="peRows">
   <thead><tr>
    <th>Model</th><th>Match</th>
    <th class="num">input $/1M</th><th class="num">cached $/1M</th><th class="num">cache_write $/1M</th><th class="num">output $/1M</th>
    <th>Alias to key</th><th class="num">× mult</th><th></th>
   </tr></thead>
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
  <h3>Model overrides (exact id or * glob, e.g. *deepseek*)</h3>
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
// boot marker: if the header badge never shows even this, the inline script
// did not execute at all (browser/extension blocked it) -- no banner can
// appear either, since the banner itself is rendered by this script
{const b=document.getElementById('meta');if(b)b.textContent='booting…';}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(msg,ms=2200){const t=$('toast');t.textContent=msg;t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),ms)}
// persistent, unmissable load-failure banner (the 2.2s toast was too easy to
// miss and a blank page with no visible cause is undebuggable)
function showFatal(msg){const b=$('fatal');if(!b)return;b.textContent=msg;b.hidden=false}
window.addEventListener('error',e=>{try{showFatal('JS error: '+(e.message||e.type)+' @line '+(e.lineno||'?'))}catch(_){}});
window.addEventListener('unhandledrejection',e=>{try{showFatal('Async error: '+((e.reason&&e.reason.message)||e.reason||'unknown'))}catch(_){}});
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
  pe:{loaded:false,search:'',data:null,orig:{}}, // pricing editor state
  tou:null,            // live editable copy of cfg.tou
  personal:{span:localStorage.getItem('qk_myspan')||'7d',usage:null,recent:null,models:null},
};

// ---------- entry ----------
async function init(){
  try{
    STATE.me=await api('/me');
    const isAdmin=(STATE.me.user||{}).role==='admin';
    if(isAdmin)document.querySelectorAll('.admin-only').forEach(el=>el.classList.remove('admin-only'));
    // Everyone gets the shared dashboard (KPI + trend + models + recent);
    // non-admins see their OWN stats (server-enforced via /stats) and the
    // admin-only sections stay hidden. /users /groups /pricing are admin-only
    // so non-admins skip them.
    const get=async p=>{try{return await api(p)}catch(e){toast(p+' failed: '+e.message);return null}};
    // /models is self-service: without ?mine=1 it returns the LOCAL POOL (every
    // model anyone used, with resolved prices) for both roles -- the reference
    // table for picking a model by price.
    [STATE.cfg,STATE.users,STATE.groups,STATE.pricing,STATE.pricePool]=await Promise.all(
      isAdmin?[get('/config'),get('/users'),get('/groups'),get('/pricing'),get('/models')]
             :[get('/config'),Promise.resolve([]),Promise.resolve([]),Promise.resolve({}),get('/models')]);
    if(!STATE.cfg)throw new Error('config unavailable');
    STATE.users=STATE.users||[];STATE.groups=STATE.groups||[];STATE.pricing=STATE.pricing||{};
    STATE.tou=JSON.parse(JSON.stringify(STATE.cfg.tou||{}));
    const secs=['secDash','secModels','secRecent'];
    if(isAdmin)secs.push('secUsers','secGeneral','secSchedule','secPricing','secGroups','secUserq','secPricingEditor','secTou');
    secs.forEach(id=>{const el=$(id);if(el)el.hidden=false});
    STATE.isAdmin=isAdmin;
    if(!isAdmin){
      // non-admin: no quota editors; show the personal header with a
      // remaining-credits progress bar
      renderPersonalHeader();
      lockNonAdminUi();
    }
    renderMeta();renderConfig();renderGroups();renderUsersQ();renderTou();initSpanUI();
    renderPricePool();
    await loadStats();
    loadRecent();
  }catch(e){showFatal('Load failed: '+e.message)}
}
function renderMeta(){
  const p=STATE.pricing||{};
  $('meta').textContent=(p.models?p.models+' models · ':'')+(p.fetched_at_iso?('updated '+new Date(p.fetched_at_iso).toLocaleString()):'no pricing yet');
}

// ---------- non-admin view ----------
function renderPersonalHeader(){
  // Non-admin header: a prominent REMAINING-credits progress bar keyed to the
  // configured quota period (daily | monthly), then the today summary. The
  // bar fills with what's LEFT (drains as you spend); it flips warn/bad as the
  // remainder runs out. The dashboard below is shared with the admin view,
  // filtered to this user server-side via /stats.
  const me=STATE.me,u=me.user||{};
  const eff=me.effective_quota;         // resolved quota x time multiplier, in credits
  const used=me.used_credits||0;        // credits already spent this period
  const periodKey=me.quota_period||STATE.cfg.quota_period||'daily';
  const period={daily:'Daily',weekly:'Weekly',monthly:'Monthly'}[periodKey]||'Daily';
  const periodNoun={daily:'day',weekly:'week',monthly:'month'}[periodKey]||'day';
  const unlimited=!(eff>0);
  const remain=unlimited?null:Math.max(0,eff-used);
  // fill = fraction of quota REMAINING (1 -> full bar, 0 -> empty)
  const remPct=unlimited?null:Math.min(100,(remain/eff)*100);
  const cls=remPct===null?'':(remPct<=0?'bad':remPct<=20?'bad':remPct<=40?'warn':'');
  // Today card: headline = today's credits spent, then three clearly-separated
  // mini-stats (requests / tokens / cache rate) each with its own label.
  const tk=me.today.tokens||{};
  const todayTok=(tk.cached||0)+(tk.input||0)+(tk.output||0);
  const mini=(l,v)=>`<div style="min-width:76px"><div class="lbl" style="font-size:10px">${l}</div><div style="font-size:16px;font-weight:600;margin-top:2px">${v}</div></div>`;
  $('secPersonal').innerHTML=`
   <h2>${esc(u.name||u.id)} <span class="small muted">${esc(u.email||'')}</span></h2>
   <div class="kpi" style="width:100%;max-width:none">
    <div class="lbl">${period} quota — credits remaining</div>
    ${unlimited
      ?'<div class="val">∞</div><div class="small muted">no quota set — unlimited</div>'
      :`<div class="val">${fmt(remain,0)} <span class="small muted">/ ${fmt(eff,0)} credits</span></div>
         <div class="bar"><i class="${cls}" style="width:${remPct.toFixed(1)}%"></i></div>
         <div class="small muted">${fmt(remain,0)} left · ${fmt(used,0)} used this ${periodNoun} (${fmt(100-remPct,1)}% spent)</div>`}
   </div>
   <div class="kpis" style="margin-top:12px">
    <div class="kpi"><div class="lbl">Today</div>
     <div class="val">${fmt(me.today.credits||0,0)} <span class="small muted" style="font-size:13px">credits</span></div>
     <div class="small muted" style="margin-bottom:8px">$${fmt(me.today.cost_usd,2)} today</div>
     <div style="display:flex;gap:22px;flex-wrap:wrap">
      ${mini('Requests',fmt(me.today.requests,0))}
      ${mini('Tokens',fmt(todayTok,0))}
      ${mini('Cache rate',fmt((me.today.cache_rate||0)*100,2)+'%')}
     </div></div>
    ${renderTouCard(me.tou||{})}
   </div>`;
  $('secPersonal').hidden=false;
  $('meta').textContent='self-service view';
}

// TOU summary card for the personal header: current tier + the full three-tier
// rate table with active windows, plus which policy level resolved. The day
// numbering matches qk_tou_rate (JS style: 0=Sunday .. 6=Saturday; empty=all).
function renderTouCard(tou){
  if(!tou||!tou.enabled){
    return `<div class="kpi"><div class="lbl">TOU pricing</div>
     <div class="val">off</div>
     <div class="small muted">time-of-use pricing not enabled</div></div>`;
  }
  const cur=tou.current_tier||'off';
  const curRate=tou.current_rate!=null?tou.current_rate:1.0;
  const applies=cur!=='off';  // does a TOU policy actually match the current model?
  // Policy-source label: which level matched + the verbatim configured pattern
  // (glob like *deepseek* or an exact model key), so the user can see WHY the
  // current model is or isn't TOU-priced. "当前模型" is just the model being
  // evaluated, not a claim that TOU applies to it.
  let srcLbl;
  if(!applies)srcLbl='不匹配任何 TOU 策略';
  else if(tou.policy_source==='model')srcLbl='模型匹配: '+(tou.matched_pattern||tou.model||'');
  else if(tou.policy_source&&tou.policy_source.startsWith('provider:'))srcLbl='提供商: '+tou.policy_source.slice(9);
  else srcLbl='default policy';
  const DOW=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const dayStr=ds=>{if(!ds||!ds.length||ds.length>=7)return '每天';return ds.map(d=>DOW[d]||d).join('/')};
  const rows=(tou.tiers||[]).map(t=>{
    const wins=(t.windows||[]).map(w=>`${esc(w.start)}-${esc(w.end)} ${dayStr(w.days)}`).join(', ')||'其余时段';
    const active=applies&&t.name===cur?';font-weight:600':'';
    const dot=applies&&t.name===cur?'● ':'';
    return `<div class="small" style="display:flex;justify-content:space-between;gap:8px${active}">
      <span>${dot}${t.name} ×${fmt(t.rate,2)}</span><span class="muted" style="text-align:right">${esc(wins)}</span></div>`;
  }).join('');
  const hol=(tou.holidays||[]).length?`<div class="small muted">节假日按 offpeak/最低档计费 (${(tou.holidays||[]).length} 天)</div>`:'';
  const headline=applies?`当前 ${esc(cur)} ×${fmt(curRate,2)}`:'当前模型不生效';
  return `<div class="kpi" style="min-width:300px"><div class="lbl">TOU pricing — ${headline}</div>
   <div class="small muted" style="margin-bottom:4px">当前模型: ${esc(tou.model||'(无记录)')} · ${esc(srcLbl)}</div>
   ${rows}${hol}</div>`;
}

// Non-admin UI lock-down: the dashboard is shared with the admin view, but a
// plain user must not (a) retarget the User filter at someone else, or (b)
// touch the reprice/backfill controls (the /pricing/reprice route is
// admin-only; exposing the button would just 403). The stats endpoints already
// enforce own-data-only server-side -- this is presentation hardening.
function lockNonAdminUi(){
  const u=STATE.me.user||{};
  // 1) pin the User filter to self and make it read-only
  const fu=$('fUser');
  if(fu){
    fu.value=u.name||u.email||u.id||'';
    fu.disabled=true;
    fu.title='non-admin: your own usage only';
  }
  // 2) the models-section reprice/backfill bar carries .admin-only so it is
  // display:none by default and only re-shown for admins in init(); belt-and-
  // braces hide it here too in case of a stale cached page.
  const rb=$('repriceBar');
  if(rb)rb.hidden=true;
}

// ---------- personal page: by-model / recent / prices ----------
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
  const out={from:isoDay(from),to:isoDay(now),gran:k==='24h'?'hour':'day'};
  if(k==='24h')out.window_start=Math.floor(Date.now()/1000)-86400; // rolling 24h, not "since 00:00"
  return out;
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
  // non-admins are pinned to their own usage regardless of the (disabled)
  // fUser input; the server enforces this too, this just keeps the client
  // consistent. Admins read the input freely.
  STATE.filter.user=STATE.isAdmin===false
    ?(STATE.me.user||{}).name||(STATE.me.user||{}).id||''
    :$('fUser').value.trim();
  STATE.filter.model=$('fModel').value;
  const qs=new URLSearchParams({from:sp.from,to:sp.to,granularity:sp.gran});
  if(sp.window_start)qs.set('window_start',sp.window_start);
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
  const tk=k.tokens||{};
  const tot=(tk.cached||0)+(tk.input||0)+(tk.output||0);
  // series buckets carry {cost:{m:v}|{_:v}, requests, tokens} (see qk_stats /
  // qk_stats_window): Cost/Credits trend from cost, Requests/Tokens from the
  // per-bucket request/token series. Cache rate and Unpriced are span ratios
  // (a sparkline of a ratio is noise), so they get a totals subline instead.
  const ser=STATE.stats.series||[];
  const costPer=ser.map(b=>Object.values(b.cost||{}).reduce((a,c)=>a+c,0));
  const reqPer=ser.map(b=>b.requests||0);
  const tokPer=ser.map(b=>b.tokens||0);
  const ch=k.channels||{};
  const ci=(tk.cached||0)+(tk.input||0);
  const cards=[
    {lbl:'Requests',val:fmt(k.requests||0,0),sp:sparkSvg(reqPer,140,34,'#c98500'),
     sub:`webui ${fmt(ch.webui||0,0)} · api ${fmt(ch.api||0,0)}`},
    {lbl:'Tokens',val:fmt(tot||0,0),sp:sparkSvg(tokPer,140,34,'#9085e9')},
    {lbl:'Cost $',val:'$'+fmt(k.cost_usd||0,2),sp:sparkSvg(costPer,140,34,'#38bdf8')},
    {lbl:'Credits',val:fmt((k.cost_usd||0)*cpu,0),sp:sparkSvg(costPer.map(v=>v*cpu),140,34,'#34d399')},
    {lbl:'Cache rate',val:fmt((k.cache_rate||0)*100,2)+'%',
     sub:`cached ${fmt(tk.cached||0,0)} / in ${fmt(ci,0)}`},
    {lbl:'Unpriced',val:fmt(k.unpriced_requests||0,0),
     sub:`of ${fmt(k.requests||0,0)} req`},
  ];
  $('kpis').innerHTML=cards.map(c=>`<div class="kpi"><div class="lbl">${c.lbl}</div><div class="val">${c.val}</div>${c.sp||''}${c.sub?`<div class="small muted">${c.sub}</div>`:''}</div>`).join('');
}

// ---------- stacked trend chart ----------
function bucketLabel(b,hourMode){return hourMode?b.slice(11,13)+':00':b.slice(5)}
function renderTrend(){
  const box=$('trend');
  const ser=STATE.stats.series||[];
  const hourMode=spanDates()&&spanDates().gran==='hour';
  const totals={};
  ser.forEach(b=>{Object.entries(b.cost||{}).forEach(([m,c])=>{totals[m]=(totals[m]||0)+c})});
  const ranked=Object.entries(totals).sort((a,b)=>b[1]-a[1]);
  if(!ranked.length){box.innerHTML='<p class="hint">No data for the selected span.</p>';$('trendLegend').innerHTML='';return}
  const top=ranked.slice(0,8).map(e=>e[0]);
  const othersCost=ranked.slice(8).reduce((s,e)=>s+e[1],0);
  const names=top.concat(othersCost>0?['Others']:[]);
  const colorOf={};top.forEach((m,i)=>colorOf[m]=SERIES_COLORS[i]);colorOf['Others']=OTHER_COLOR;
  const W=1040,H=250,pl=48,pr=12,pt=10,pb=26;
  const iw=W-pl-pr,ih=H-pt-pb;
  let ymax=0;
  ser.forEach(b=>{const s=Object.values(b.cost||{}).reduce((a,c)=>a+c,0);if(s>ymax)ymax=s});
  const yt=v=>pt+ih-(v/ymax*ih);
  const rows=ser.map(b=>{const bm=b.cost||{};return {label:b.bucket,parts:names.map(m=>bm[m]||0)}});
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
     <td class="num" title="api: ${fmt((r.channels||{}).api||0,0)}">${fmt((r.channels||{}).webui||0,0)}</td>
     <td class="num">${fmt(tok(r),0)}</td>
     <td class="num">$${fmt(r.cost_usd,2)}</td>
     <td class="num">${fmt((r.cost_usd||0)*cpu,0)}</td>
     <td>${pp===null?'<span class="muted">∞</span>':`<div class="bar"><i class="${cls}" style="width:${Math.min(pp,100).toFixed(1)}%"></i></div><span class="pct">${pp.toFixed(1)}%</span>`}</td>
     <td><span class="tag src-${r.quota_source||'none'}">${esc(r.quota_source||'none')}</span></td>
    </tr>`;}).join('')||'<tr><td colspan="8" class="empty">No users in span</td></tr>';
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
    dr.innerHTML='<td colspan="8" class="empty">loading…</td>';
    tr.after(dr);
    try{
      // 24h: match the main stats (rolling window via hours buckets), not a
      // day-aggregated from/to query — otherwise the drill shows nothing while
      // the KPI has data
      if(sp.window_start){
        data=await api(`/stats?user=${encodeURIComponent(uid)}&window_start=${sp.window_start}&granularity=hour`);
      }else{
        data=await api(`/stats?user=${encodeURIComponent(uid)}&from=${sp.from}&to=${sp.to}&granularity=day`);
      }
    }catch(e){dr.innerHTML=`<td colspan="8" class="empty">${esc(e.message)}</td>`;return}
    STATE.drill[uid]=data;
    dr.remove();
  }
  const rows=(data.models||[]).map(m=>{
    const t=m.tokens||{};
    const tin=(t.cached||0)+(t.input||0);  // total input (cached + cache-miss)
    return `<tr><td>${esc(m.model)}</td><td class="num">${fmt(m.requests,0)}</td><td class="num">${fmt(t.cached,0)}</td><td class="num">${fmt(tin,0)}</td><td class="num">${fmt(t.output,0)}</td><td class="num">$${fmt(m.cost_usd,2)}</td></tr>`;
  }).join('')||'<tr><td colspan="6" class="empty">No usage in span</td></tr>';
  const dr=document.createElement('tr');dr.className='drill';
  dr.innerHTML=`<td colspan="8"><table style="max-width:760px"><thead><tr><th>Model</th><th class="num">Requests</th><th class="num">Cached</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cost $</th></tr></thead><tbody>${rows}</tbody></table></td>`;
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
    const ci=(t.cached||0)+(t.input||0),cp=ci?((t.cached||0)/ci*100):0;
    return `<tr>
     <td>${esc(m.model)}
       ${m.unpriced_requests>0?`<span class="tag unpriced">unpriced</span>${STATE.isAdmin!==false?`<button class="small" onclick="reprice('${esc(m.model)}')">reprice</button>`:''}`:''}
       ${STATE.isAdmin!==false?`<button class="small" data-mi="${i}" onclick="matchModel(this)">match</button><span class="match-out"></span>`:''}</td>
     <td class="num">${fmt(m.requests,0)}</td>
     <td class="num">${fmt(m.users,0)}</td>
     <td class="num">${fmt(t.cached,0)}</td>
     <td class="num">${fmt(cp,2)}%</td>
     <td class="num">${fmt(ci,0)}</td>
     <td class="num">${fmt(t.output,0)}</td>
     <td class="num">$${fmt(m.cost_usd,2)}</td>
     <td class="num">${fmt(m.blended_per_m,2)}</td>
     <td class="num">${sv>0?'+':''}$${fmt(sv,2)}</td>
     <td>${ttags}</td>
    </tr>`;}).join('')||'<tr><td colspan="10" class="empty">No models in span</td></tr>';
}
// ---------- available-models price reference (local pool) ----------
// Renders /models (the local pool: every model anyone used, with resolved
// per-1M prices) as a model-picking reference. Independent of the time-span
// stats table above it; shown to both roles (the endpoint is self-service).
function renderPricePool(){
  const tb=$('pricePoolT');if(!tb)return;
  const items=(STATE.pricePool&&STATE.pricePool.items)||[];
  const fmtP=v=>(typeof v==='number')?fmt(v,2):'<span class="muted">–</span>';
  const rows=items.map(it=>{
    const p=it.price||{};
    const how=it.how?String(it.how):'';
    const howShort=how.indexOf(':')<0?how:how.slice(0,how.indexOf(':'));
    const match=it.matched?`<span class="tag matched" title="${esc(how)}">${esc(howShort||'matched')}</span>`
                          :'<span class="tag">none</span>';
    const status=it.unpriced_requests>0?'<span class="tag unpriced">unpriced</span>'
                :(it.matched?'<span class="tag matched">priced</span>':'<span class="tag unpriced">no price</span>');
    return `<tr>
     <td>${esc(it.model)}</td>
     <td class="num">${fmtP(p.input)}</td>
     <td class="num">${fmtP(p.cached)}</td>
     <td class="num">${fmtP(p.output)}</td>
     <td>${match}</td>
     <td>${status}</td>
    </tr>`;}).join('');
  tb.querySelector('tbody').innerHTML=rows||'<tr><td colspan="6" class="empty">No models recorded yet.</td></tr>';
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

// ---------- reprice (backfill unpriced ledger days at current price) ----------
async function reprice(model){
  const days=Math.max(1,parseInt($('repriceDays').value)||30);
  const out=$('repriceOut');
  const scope=model?('model '+model):('ALL unpriced models');
  if(!confirm(`Reprice ${scope} for the last ${days} days at the CURRENT price?\n\nThis rewrites ledger cost_usd AND recent-activity entries recorded while the model had no price, and clears the unpriced tag in both. TOU is applied at the current rate (exact when TOU is off).`))return;
  out.textContent=' repricing…';
  try{
    const qs='days='+days+(model?'&model='+encodeURIComponent(model):'');
    const r=await api('/pricing/reprice?'+qs,{method:'POST'});
    if(r.error){out.textContent=' error: '+r.error;return}
    const per=Object.entries(r.models||{}).map(([m,v])=>`${m}: +$${fmt(v.cost_added_usd,4)} (${v.buckets}d)`).join(', ');
    const rec=r.recent_items_repriced?` · recent: ${r.recent_items_repriced} entries +$${fmt(r.recent_cost_added_usd,4)}`:'';
    out.textContent=` +$${fmt(r.cost_added_usd,4)} across ${r.buckets_repriced} buckets`+(per?' · '+per:'')+rec;
    toast('Reprice done: +$'+fmt(r.cost_added_usd,4));
    await loadStats();  // refresh tables so the unpriced tag disappears
    if(STATE.recent)await loadRecent();  // re-fetch the feed: reprice rewrote recent.json server-side
  }catch(e){out.textContent=' failed: '+e.message;toast('Reprice failed: '+e.message)}
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
    const chan=it.channel==='webui'?'webui':'api';
    return `<tr>
     <td class="small muted">${time}</td>
     <td>${esc(it.name||it.user_id)}<br/><span class="small muted">${esc(it.email||'')}</span></td>
     <td>${esc(it.model)}${it.priced===false?' <span class="tag unpriced">unpriced</span>':''}</td>
     <td><span class="tag ch-${chan}">${chan}</span></td>
     <td class="num">${fmt(t.cached,0)}</td>
     <td class="num">${fmt(ci,0)}</td>
     <td class="num">${fmt(t.output,0)}</td>
     <td class="num">${fmt(cp,2)}%</td>
     <td class="num">$${fmt(it.cost_usd,4)}</td>
     <td>${tier}</td></tr>`;}).join('')||'<tr><td colspan="10" class="empty">No activity recorded yet — send a chat through the filter first.</td></tr>';
}

// ---------- config sections ----------
function renderConfig(){
  $('credits_per_usd').value=STATE.cfg.credits_per_usd;
  $('quota_period').value=STATE.cfg.quota_period||'daily';
  $('default_quota_credits').value=STATE.cfg.default_quota_credits??'';
  const s=STATE.cfg.schedule||{};
  $('schedule_timezone').value=s.timezone??'';
  const p=STATE.cfg.pricing||{};
  $('pricing_url').value=Array.isArray(p.url)?p.url.join('\n'):(p.url||'');
  $('refresh_hours').value=p.refresh_hours??24;
  $('default_pricing').value=p.default_pricing?JSON.stringify(p.default_pricing):'';
  const ma=STATE.cfg.model_aliases||{};
  const maEl=$('model_aliases');
  if(maEl)maEl.value=Object.keys(ma).length?JSON.stringify(ma,null,1):'';
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
    schedule:{timezone:$('schedule_timezone').value.trim()||null},
    // overrides: read from the editor when it has been opened; otherwise
    // keep whatever the config already holds (editor reads are authoritative
    // only after /pricing?full=1 has loaded once)
    pricing:{url:(()=>{const lines=$('pricing_url').value.split('\n').map(s=>s.trim()).filter(Boolean);return lines.length<=1?(lines[0]||''):lines})(),refresh_hours:num('refresh_hours',24),default_pricing:dp,overrides:STATE.pe.loaded?collectOverrides():((STATE.cfg.pricing||{}).overrides||{})},
    group_quotas:gq,user_quotas:uq,
    model_aliases:(()=>{const el=$('model_aliases');if(!el)return (STATE.cfg.model_aliases||{});const v=el.value.trim();if(!v)return {};try{const o=JSON.parse(v);if(typeof o!=='object'||Array.isArray(o))throw 0;return o}catch(e){toast('Model aliases is not a JSON object');throw e}})(),
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
    STATE.pe.data=await api('/models');
    STATE.pe.loaded=true;
    rebuildPeOrig();
    renderPricingRows();
    if(STATE.pe.data&&!STATE.pe.data.pricing_fetched)toast('Upstream pricing table not fetched yet - matches may be empty');
  }catch(e){toast('Models load failed: '+e.message)}
}
// STATE.pe.orig[model] = {manual, cleared, cur:{prices, alias, mult},
//   base:{prices, alias, mult}, how, price, used, available, requests,
//   unpriced_requests}
// cur is only populated on edit (or when an override exists, so clear can
// restore); collectOverrides is DOM-independent.
function rebuildPeOrig(){
  const items=(STATE.pe.data&&STATE.pe.data.items)||[];
  STATE.pe.orig={};
  items.forEach(it=>{
    let base={prices:{input:null,cached:null,cache_write:null,output:null},alias:'',mult:''};
    const o=it.override;
    if(o){
      // multiplier is read for EVERY shape (alias / prices / legacy-direct /
      // multiplier-only), not just alias -- v0.5.32
      base.mult=(o.multiplier!==undefined&&o.multiplier!==null)?o.multiplier:'';
      if(o.alias!==undefined&&o.alias!==null){base.alias=o.alias;}
      else{const p=(o.prices&&typeof o.prices==='object')?o.prices:o;base.prices={input:p.input??null,cached:p.cached??null,cache_write:p.cache_write??null,output:p.output??null};}
    }
    STATE.pe.orig[it.model]={
      manual:!!it.override,cleared:false,cur:null,base:base,
      how:it.how||'',price:it.price||null,used:!!it.used,
      requests:it.requests||0,unpriced_requests:it.unpriced_requests||0,
    };
  });
}
function peEff(o){
  if(o.cleared)return null;
  if(!o.cur)return o.manual?o.base:null;
  // merge edits over the base so untouched fields keep their values (an
  // alias edit must not wipe stored direct prices, and one price edit must
  // not null out the other three)
  const b=o.base,c=o.cur;
  // fall back per field: stored override first, then the resolved upstream
  // price (sparse edits must not null out fields the user left untouched)
  const fb=f=>c.prices[f]??b.prices[f]??((o.price||{})[f]??null);
  return {prices:{input:fb('input'),cached:fb('cached'),cache_write:fb('cache_write'),output:fb('output')},
          alias:c.alias!==''?c.alias:b.alias,mult:(c.mult!==''&&c.mult!==null)?c.mult:b.mult};
}
function peRowHtml(k,o){
  const eff=peEff(o);
  const cur=o.cur||o.base;
  // display falls back to the resolved (upstream) price per field, so the
  // inputs always show the full effective row instead of the sparse edit
  const dp={};
  ['input','cached','cache_write','output'].forEach(f=>{
    dp[f]=(eff&&!eff.alias&&eff.prices[f]!==null&&eff.prices[f]!==undefined)?eff.prices[f]:((o.price||{})[f]);
  });
  const showPrice=eff&&eff.alias?null:dp;
  // match display: "method: actual-matched-key" for fuzzy strategies
  // (suffix/segment/contains show the upstream key they landed on; exact
  // shows just "exact"), alias shows the target chain
  const howFmt=h=>{
    const i=h.indexOf(':');
    const m=i<0?h:h.slice(0,i),t=i<0?'':h.slice(i+1);
    if(m==='exact')return 'exact';
    if(m==='override')return 'override';
    return t?m+': '+t:m;
  };
  const howTxt=o.cleared?'<span class="muted">cleared</span>'
    :eff&&eff.alias?('alias → '+esc(eff.alias)+(eff.mult!==''&&eff.mult!==null?' ×'+esc(eff.mult):''))
    :(o.how?esc(howFmt(o.how)):'<span class="tag unpriced">no match</span>');
  const numVal=f=>{const v=(showPrice||{})[f];return (v===null||v===undefined)?'':v};
  const dis=o.cleared?'disabled':'';
  const adis=o.cleared?'disabled':'';
  return `<tr data-mrow="${esc(k)}"${o.cleared?' class="pe-cleared"':''}>
   <td>${esc(k)}
     ${o.manual?'<span class="tag manual">manual</span>':''}
     ${(o.price||o.manual)?'<span class="tag matched">matched ✓</span>':'<span class="tag unpriced">no match</span>'}
     <br/><span class="small muted">used · ${fmt(o.requests,0)} reqs</span></td>
   <td class="small">${howTxt}</td>
   ${['input','cached','cache_write','output'].map(f=>`<td><input class="pe-num" type="number" step="0.01" min="0" data-pk="${esc(k)}" data-f="${f}" value="${esc(numVal(f))}" ${dis} oninput="peEdit(this)"/></td>`).join('')}
   <td><input class="pe-alias" type="text" placeholder="e.g. kimi-k3" data-pk="${esc(k)}" data-f="alias" value="${esc(cur.alias||'')}" ${adis} oninput="peEdit(this)"/></td>
   <td><input class="pe-mult" type="number" step="0.05" min="0" placeholder="1" data-pk="${esc(k)}" data-f="mult" value="${esc(cur.mult===''||cur.mult===null?'':cur.mult)}" ${adis} oninput="peEdit(this)"/></td>
   <td>${o.manual&&!o.cleared?`<button class="small" onclick="peClear('${esc(k)}')">clear</button>`:''}${o.cleared?`<button class="small" onclick="peUndo('${esc(k)}')">undo</button>`:''}</td>
  </tr>`;
}
function renderPricingRows(){
  const q=STATE.pe.search,onlyU=$('peOnlyUnpriced').checked;
  let keys=Object.keys(STATE.pe.orig).filter(k=>{
    if(q&&!k.toLowerCase().includes(q))return false;
    const o=STATE.pe.orig[k];
    if(onlyU&&!(o.unpriced_requests>0||(!o.price&&!o.manual&&!o.cleared)))return false;
    return true;
  }).sort((a,b)=>{
    const oa=STATE.pe.orig[a],ob=STATE.pe.orig[b];
    const ua=(oa.unpriced_requests>0||(!oa.price&&!oa.manual))?0:1;
    const ub=(ob.unpriced_requests>0||(!ob.price&&!ob.manual))?0:1;
    if(ua!==ub)return ua-ub;
    if((oa.used?0:1)!==(ob.used?0:1))return (oa.used?0:1)-(ob.used?0:1);
    return a.toLowerCase()<b.toLowerCase()?-1:1;
  });
  const tb=$('peRows').querySelector('tbody');
  tb.innerHTML=keys.map(k=>peRowHtml(k,STATE.pe.orig[k])).join('')||'<tr><td colspan="9" class="empty">No models match the filter.</td></tr>';
  $('pePage').textContent=keys.length+' models';
}
function peSearch(){STATE.pe.search=$('peSearch').value.trim().toLowerCase();renderPricingRows()}
function peEdit(inp){
  const o=STATE.pe.orig[inp.dataset.pk];if(!o)return;
  if(!o.cur)o.cur=JSON.parse(JSON.stringify(o.base));
  const f=inp.dataset.f;
  if(f==='alias'){o.cur.alias=inp.value.trim();}
  else if(f==='mult'){o.cur.mult=inp.value===''?'':parseFloat(inp.value);}
  else{const v=parseFloat(inp.value);o.cur.prices[f]=isNaN(v)?null:v;}
  o.cleared=false;
}
function peClear(k){const o=STATE.pe.orig[k];if(!o)return;o.cleared=true;renderPricingRows()}
function peUndo(k){const o=STATE.pe.orig[k];if(!o)return;o.cleared=false;renderPricingRows()}
function collectOverrides(){
  // DOM-independent: iterate STATE.pe.orig (every used/configured model).
  // Emission rules:
  //  - cleared manual row          -> emit null (deep-merge tombstone)
  //  - alias set                   -> {alias, multiplier?} (multiplier omitted
  //    when blank -> backend treats it as 1)
  //  - edited rows with any price  -> {prices:{...}} (cur is only populated
  //    by peEdit, so an untouched upstream row never emits -- the previous
  //    "changed vs upstream" float-equality check re-emitted every
  //    upstream-priced row as an override and wiped the row's alias on any
  //    later save)
  //  - unedited rows               -> never emitted (existing overrides are
  //    preserved because the POST deep-merges into the stored config)
  const ov={};
  Object.entries(STATE.pe.orig).forEach(([k,o])=>{
    if(o.cleared){ov[k]=null;return}
    const eff=peEff(o);
    if(!eff)return;
    const hasMult=eff.mult!==''&&eff.mult!==null&&!isNaN(eff.mult);
    if(eff.alias){
      const out={alias:eff.alias};
      if(hasMult)out.multiplier=Number(eff.mult);
      ov[k]=out;
      return;
    }
    if(!o.cur)return; // untouched rows never emit (deep-merge preserves stored overrides)
    // judge by the user's ACTUAL edits (o.cur), not peEff's upstream-backfilled
    // prices: a multiplier-only row has all cur.prices null, and backfilling the
    // upstream price into eff.prices would wrongly freeze it into a manual override
    const cp=(o.cur&&o.cur.prices)||{};
    const userSetPrice=Object.values(cp).some(v=>v!==null&&v!==undefined);
    if(userSetPrice){
      // manual prices (optionally discounted by a multiplier for quick sales)
      const out={prices:eff.prices};
      if(hasMult)out.multiplier=Number(eff.mult);
      ov[k]=out;
      return;
    }
    if(hasMult){
      // multiplier-only: scale the upstream table match (no manual price, no alias)
      ov[k]={multiplier:Number(eff.mult)};
    }
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


def _mount_guard(app, page_path: str, api_prefix: str) -> int:
    """(Re)attach the page + API routes ahead of OWUI's SPA catch-all mount.

    OWUI mounts SPAStaticFiles at "/" (name "spa-static-files") at import
    time; routes merely appended later land after it and are shadowed -- the
    page then serves the OWUI SPA shell (which renders its client-side 404)
    and the APIs return HTML instead of JSON. Splice ours in just before
    that mount instead (same workaround as the prune plugin this web mode is
    modeled on).

    Starlette cannot swap a route's handler in place, so after a hot code
    update the previous module's routes would keep serving until restart.
    Drop our stale routes by path first so the fresh handlers take effect at
    once. Routes under a *changed* prefix cannot be found this way and still
    linger until restart (see valve descriptions).

    Returns the number of stale routes dropped.
    """
    routes = app.router.routes
    stale = [
        r
        for r in routes
        if getattr(r, "path", None) in (page_path, api_prefix)
        or str(getattr(r, "path", "")).startswith(api_prefix + "/")
    ]
    if stale:
        stale_ids = {id(r) for r in stale}
        routes[:] = [r for r in routes if id(r) not in stale_ids]

    router = APIRouter()

    # Page is login-gated, not admin-gated: any signed-in user may load it.
    # The SPA then calls /me and renders the admin console or the personal
    # card by role; every admin API still enforces _require_admin per route.
    @router.get(
        page_path, include_in_schema=False, dependencies=[Depends(_require_user)]
    )
    async def _qk_page(request: Request):
        # no-store: a cached page keeps the OLD JS after a plugin update
        # (stale all-or-nothing loader = blank page again)
        return HTMLResponse(qk_build_page(api_prefix), headers={"Cache-Control": "no-store"})

    router.include_router(qk_router, prefix=api_prefix)

    before = len(routes)
    app.include_router(router)
    added = routes[before:]
    # The SPA mount is added at OWUI import time, long before any plugin
    # route, so spa_i < before holds and the index stays valid after del.
    spa_i = next(
        (
            i
            for i, r in enumerate(routes)
            if getattr(r, "name", None) == "spa-static-files"
        ),
        None,
    )
    if spa_i is not None and added:
        del routes[before:]
        routes[spa_i:spa_i] = added
    return len(stale)


# ==== Router ====
# Admin-only routes carry Depends(_require_admin) per route; /me is
# self-service and only requires an authenticated user (_require_user).

qk_router = APIRouter()


@qk_router.get("/config", dependencies=[Depends(_require_user)])
async def api_config(request: Request, user=Depends(_require_user)):
    cfg = qk_get_config()
    cfg["_time_multiplier"] = qk_time_multiplier(cfg)
    # non-admins: strip the admin-only quota tables (they can see their own
    # effective quota via /me); keep pricing/aliases/schedule for rendering
    if getattr(user, "role", "") != "admin":
        cfg = {k: v for k, v in cfg.items()
               if k not in ("user_quotas", "group_quotas")}
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


@qk_router.get("/recent")
async def api_recent(request: Request, user=Depends(_require_user)):
    rec = qk_load_json(QK_RECENT_PATH, {"items": []})
    items = list(reversed(rec.get("items") or []))
    # self-service: non-admins (or an explicit ?mine=1) only ever see their own
    # rows, capped at 50, with the email field stripped. Server-enforced: the
    # caller's role/id decide, never a client-supplied filter.
    if request.query_params.get("mine") == "1" or getattr(user, "role", "") != "admin":
        items = [it for it in items if it.get("user_id") == user.id][:50]
        items = [{k: v for k, v in it.items() if k != "email"} for it in items]
    return JSONResponse({"items": items})


@qk_router.get("/stats", dependencies=[Depends(_require_user)])
async def api_stats(request: Request, user=Depends(_require_user)):
    q = request.query_params
    led_users = (qk_load_json(QK_LEDGER_PATH, {"users": {}}).get("users") or {})
    gmap = await qk_group_ids_map(led_users.keys())
    # non-admins can only ever query their own usage (server-enforced)
    uid = q.get("user") if getattr(user, "role", "") == "admin" else user.id
    wstart = q.get("window_start")
    try:
        wstart = float(wstart) if wstart is not None else None
    except Exception:
        wstart = None
    if wstart is not None:
        # rolling window (the "24h" span): per-request epoch timestamps from
        # recent.json -- no calendar-day/hour-bucket trimming, no timezone math
        return JSONResponse(
            qk_stats_window(wstart, uid, q.get("model"), group_ids_map=gmap)
        )
    return JSONResponse(
        qk_stats(q.get("from"), q.get("to"), uid, q.get("model"),
                 q.get("granularity", "day"), group_ids_map=gmap)
    )


@qk_router.get("/me")
async def api_me(request: Request, user=Depends(_require_user)):
    cfg = qk_get_config()
    gids = await qk_user_group_ids_async({"id": user.id})
    quota, source = qk_resolve_quota(cfg, {"id": user.id}, gids)
    mult = qk_time_multiplier(cfg)
    led = (qk_load_json(QK_LEDGER_PATH, {"users": {}}).get("users") or {}).get(user.id) or {}
    days = led.get("days") or {}
    now = qk_local_now(cfg)
    period = cfg.get("quota_period") or "daily"
    pref = now.strftime("%Y-%m-")
    used_month = sum((d or {}).get("cost_usd", 0) or 0 for k, d in days.items() if k.startswith(pref))
    today_d = days.get(now.strftime("%Y-%m-%d")) or {}
    used_day = today_d.get("cost_usd", 0) or 0
    monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    today_k = now.strftime("%Y-%m-%d")
    used_week = sum((d or {}).get("cost_usd", 0) or 0 for k, d in days.items() if monday <= k <= today_k)
    used_period = used_month if period == "monthly" else (used_week if period == "weekly" else used_day)
    cpu_ = float(cfg.get("credits_per_usd") or 1000.0)
    today_tok = today_d.get("tokens") or {}
    trend = []
    for i in range(6, -1, -1):
        kd = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        dd = days.get(kd) or {}
        trend.append({"day": kd, "requests": dd.get("requests", 0),
                      "cost_usd": dd.get("cost_usd", 0) or 0})
    # TOU: resolve the current tier against the user's most-recently-used model
    # (policy is model/provider-scoped). Fall back to the default policy when
    # the user has no recorded model yet.
    last_model = None
    for k in sorted(days.keys(), reverse=True):
        ms = ((days.get(k) or {}).get("models") or {})
        if ms:
            # most-used model that day, alias-resolved for display consistency
            last_model = max(ms.items(), key=lambda kv: (kv[1] or {}).get("cost_usd", 0) or 0)[0]
            break
    tou = cfg.get("tou") or {}
    tou_summary = {"enabled": bool(tou.get("enabled")), "current_tier": None,
                   "current_rate": 1.0, "model": last_model, "policy_source": None,
                   "matched_pattern": None, "tiers": []}
    if tou.get("enabled"):
        probe = last_model or ""
        rate, tier = qk_tou_rate(cfg, probe, now)
        # Which policy level resolved, and the verbatim pattern the admin
        # configured (e.g. the "*deepseek*" glob) so the UI can show exactly
        # why the current model is or isn't TOU-priced.
        src = "off"
        matched_pattern = None
        if qk_tou_resolve_policy(cfg, probe) is not None:
            models_cfg = tou.get("models") or {}
            p_low = probe.lower()
            prov = probe.split("/")[0] if "/" in probe else "_default"
            if probe and p_low in models_cfg:
                src, matched_pattern = "model", p_low
            else:
                glob_hit = next(
                    (k for k in models_cfg
                     if "*" in str(k) and fnmatch.fnmatchcase(p_low, str(k).lower())),
                    None)
                if probe and glob_hit is not None:
                    src, matched_pattern = "model", str(glob_hit)
                elif prov in (tou.get("providers") or {}):
                    src, matched_pattern = "provider:" + prov, prov
                else:
                    src = "default"
        else:
            # no policy matched -> TOU does not apply to the current model
            tier, rate = "off", 1.0
        tiers_cfg = tou.get("tiers") or {}
        tier_rows = []
        for tname in ("peak", "normal", "offpeak"):
            t = tiers_cfg.get(tname) or {}
            wins = t.get("windows") or []
            tier_rows.append({
                "name": tname,
                "rate": float(t.get("rate", 1.0)) if isinstance(t.get("rate"), (int, float)) else 1.0,
                "windows": [{"start": w.get("start", "00:00"), "end": w.get("end", "00:00"),
                             "days": w.get("days")} for w in wins],
            })
        tou_summary.update({"current_tier": tier, "current_rate": rate,
                            "policy_source": src, "matched_pattern": matched_pattern,
                            "tiers": tier_rows,
                            "holidays": tou.get("holidays") or []})
    return JSONResponse(
        {
            "user": {"id": user.id, "name": user.name, "email": user.email, "role": user.role},
            "quota": quota,
            "quota_source": source,
            "quota_period": period,
            "multiplier": mult,
            "effective_quota": (quota * mult) if quota is not None else None,
            "used_credits": used_period * cpu_,
            "today": {"cost_usd": used_day,
                      "requests": today_d.get("requests", 0),
                      "credits": used_day * cpu_,
                      "tokens": today_tok,
                      "cache_rate": ((today_tok.get("cached") or 0) /
                                     ((today_tok.get("cached") or 0) + (today_tok.get("input") or 0)))
                                     if ((today_tok.get("cached") or 0) + (today_tok.get("input") or 0)) else 0.0},
            "trend": trend,
            "tou": tou_summary,
        }
    )


@qk_router.get("/me/usage")
async def api_me_usage(request: Request, user=Depends(_require_user)):
    """Self-service per-model/usage view: the caller's own ledger days only,
    never another user's. span=7d (default) or 30d."""
    cfg = qk_get_config()
    span = "30d" if request.query_params.get("span") == "30d" else "7d"
    days_n = 30 if span == "30d" else 7
    led = (qk_load_json(QK_LEDGER_PATH, {"users": {}}).get("users") or {}).get(user.id) or {}
    days = led.get("days") or {}
    now = qk_local_now(cfg)
    trend, channels, models = [], {"webui": 0, "api": 0}, {}
    for i in range(days_n - 1, -1, -1):
        kd = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        dd = days.get(kd) or {}
        trend.append({"day": kd, "requests": dd.get("requests", 0),
                      "cost_usd": dd.get("cost_usd", 0) or 0})
        for cname, cnt in ((dd.get("channels") or {}).items()):
            if cname in channels:
                channels[cname] += cnt or 0
        for m, mm in ((dd.get("models") or {}).items()):
            mm = mm or {}
            row = models.setdefault(m, {"model": m, "requests": 0,
                                        "tokens": {"cached": 0.0, "input": 0.0, "output": 0.0},
                                        "cost_usd": 0.0})
            row["requests"] += mm.get("requests", 0) or 0
            row["cost_usd"] += mm.get("cost_usd", 0) or 0
            for k in ("cached", "input", "output"):
                row["tokens"][k] += (mm.get("tokens") or {}).get(k, 0) or 0
    return JSONResponse({
        "span": span,
        "channels": channels,
        "trend": trend,
        "models": sorted(models.values(), key=lambda x: -x["cost_usd"]),
    })


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


@qk_router.post("/pricing/reprice", dependencies=[Depends(_require_admin)])
async def api_reprice(request: Request):
    q = request.query_params
    try:
        days = int(q.get("days") or 30)
    except Exception:
        days = 30
    model = q.get("model") or None
    dry = q.get("dry") == "1"
    try:
        result = await asyncio.to_thread(qk_reprice_ledger, days, model, dry)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@qk_router.get("/models")
async def api_models(request: Request, user=Depends(_require_user)):
    """Models actually used (from the ledger's usage records -- these are
    the real upstream model ids), each with the resolved price, the
    matching strategy, and the configured override (if any). Backs the
    pricing editor; OWUI's /api/models is intentionally NOT included (it
    mixes aliases, duplicates and stale entries nobody calls).

    Self-service: non-admins (or ?mine=1) only see models THEY used, with
    requests/cost aggregated from their own days. The raw upstream pricing
    table stays admin-only (/pricing); this only exposes the resolved
    per-1M price for models the caller already used."""
    cfg = qk_get_config()
    ov = ((cfg.get("pricing") or {}).get("overrides")) or {}
    table = (qk_load_json(QK_PRICING_PATH, {}) or {}).get("table") or {}

    # mine=1 (explicit) filters to the caller's OWN used models; without it,
    # everyone (admin or not) sees the LOCAL model pool — every model anyone
    # has used in the ledger, with resolved prices.
    mine = request.query_params.get("mine") == "1"
    used = {}  # model -> {"requests": n, "unpriced_requests": n, "cost_usd": x}
    led = (qk_load_json(QK_LEDGER_PATH, {"users": {}}).get("users") or {})
    for uid, u in led.items():
        if mine and uid != user.id:
            continue
        for d in (u.get("days") or {}).values():
            for m, mm in ((d or {}).get("models") or {}).items():
                # history recorded the upstream alias (prx.*) before
                # model_aliases existed — merge into the real name so the
                # list shows what users actually used, not stale aliases
                m = qk_resolve_model_alias(cfg, m)
                row = used.setdefault(m, {"requests": 0, "unpriced_requests": 0, "cost_usd": 0.0})
                mm = mm or {}
                row["requests"] += mm.get("requests", 0) or 0
                row["unpriced_requests"] += mm.get("unpriced_requests", 0) or 0
                row["cost_usd"] += mm.get("cost_usd", 0) or 0

    is_admin = getattr(user, "role", "") == "admin"
    ids = sorted(used, key=str.lower)
    items = []
    for m in ids:
        price, how = qk_find_pricing(m, table, ov)
        spec = None
        ml = m.strip().lower()
        for k, v in ov.items():
            if str(k).strip().lower() == ml and v is not None:
                spec = v
                break
        u = used.get(m) or {}
        row = {
            "model": m,
            "used": True,
            "matched": price is not None,
            "how": how,
            "price": price,
        }
        if is_admin or mine:
            # usage aggregates + the configured override are admin-facing (or
            # own-data for ?mine=1). For a non-admin browsing the shared local
            # pool these would leak OTHER users' aggregate usage/cost, so they
            # are stripped -- the price-reference table only needs the price.
            row["requests"] = u.get("requests", 0)
            row["unpriced_requests"] = u.get("unpriced_requests", 0)
            row["cost_usd"] = u.get("cost_usd", 0.0)
            row["override"] = spec
        items.append(row)
    return JSONResponse({"items": items, "pricing_fetched": bool(table)})


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
            default="/quota",
            description="Path of the admin UI page (after changing, the old path stays registered until restart)",
        )
        api_prefix: str = Field(
            default="/api/v1/quota-keeper",
            description="Base path for config/pricing APIs (after changing, the old path stays registered until restart)",
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
        name = __event_name__ or ""
        if not name or __app__ is None:
            return
        subject = (event or {}).get("subject") or {}
        subject_id = subject.get("id") if isinstance(subject, dict) else None
        own_lifecycle = subject_id == __id__ and name in (
            "function.enable_started",
            "function.updated",
            "function.valves_updated",
        )
        # Late-init safety net (prune pattern): a hot code update swaps in a
        # fresh instance that has never mounted, while the previous module's
        # routes may still be in the table. Remount on the first event that
        # arrives so the new handlers take effect without a server restart.
        if name != "system.startup.completed" and not own_lifecycle and self._installed:
            return
        try:
            page_path = (self.valves.route_prefix or "/quota").strip()
            if not page_path.startswith("/"):
                page_path = "/" + page_path
            if page_path == "/":
                page_path = "/quota"  # an empty valve would shadow the SPA at "/"
            stale = _mount_guard(__app__, page_path, self.valves.api_prefix)
            if stale:
                log.info("quota-keeper refreshed %d stale route(s)", stale)
            if not self._installed:
                log.info("quota-keeper API mounted at %s", self.valves.api_prefix)
                log.info("quota-keeper admin page at %s", page_path)
                # Passthrough ingestion middleware — mounted ONCE per module
                # instance, inside the _installed guard (a repeat mount would
                # tee the same response stream twice and 499 the client).
                # Starlette builds the middleware stack lazily on the first
                # request, so mounting during lifespan is legal on the pinned
                # starlette. NOTE (v0.5.3): newer starlette (1.x, OWUI 0.11's
                # fastapi 0.136) REBUILDS middleware_stack after startup, so
                # add_middleware raises "Cannot add middleware after an
                # application has started" — bypass: append to user_middleware
                # and rebuild the stack.
                # Hot reload: a NEW module instance (self._installed=False)
                # remounts and must first remove the OLD instance's middleware
                # — hot reload creates a NEW function object, so identity (is)
                # can't match; match by dispatch __name__.
                try:
                    from starlette.middleware import Middleware
                    from starlette.middleware.base import BaseHTTPMiddleware

                    umw = getattr(__app__, "user_middleware", None)
                    if umw is not None:
                        umw[:] = [
                            m for m in umw
                            if not (m.cls is BaseHTTPMiddleware
                                    and getattr(m.kwargs.get("dispatch"), "__name__", "")
                                    == "qk_passthrough_middleware")
                        ]
                    umw.append(
                        Middleware(BaseHTTPMiddleware, dispatch=qk_passthrough_middleware)
                    )
                    try:
                        __app__.middleware_stack = __app__.build_middleware_stack()
                    except Exception as _e:
                        # build may fail if the stack is mid-request; keep the
                        # middleware registered, it will build on next access
                        log.info("quota-keeper ingest stack rebuild skipped: %s", _e)
                    log.info("quota-keeper passthrough ingest middleware mounted (v2)")
                except Exception as e:
                    log.warning("quota-keeper ingest middleware mount failed: %s", e)
            self._installed = True

            # The pricing loop is tracked on app.state so a hot code update
            # cancels the previous module instance's loop instead of leaking
            # one background task per save (HANDOFF §8.14).
            task = getattr(__app__.state, "quota_keeper_pricing_task", None)
            if task is not None and not task.done():
                task.cancel()
            if self.valves.enable_background_pricing_refresh:
                self._pricing_task = asyncio.create_task(_pricing_loop())
                __app__.state.quota_keeper_pricing_task = self._pricing_task
            else:
                __app__.state.quota_keeper_pricing_task = None
        except Exception as e:
            log.warning("quota-keeper setup failed: %s", e)
