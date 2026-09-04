#!/usr/bin/env python3
"""One-off repair for the 2026-08-31..09-04 alias-shadow double counting.

Background: after the 2026-08-29 OWUI 0.11.1 upgrade, every STREAMING direct
API request (no chat_id / no message id in __metadata__) was recorded twice:
once by Filter.stream() under the real upstream model name (priced), then
again ~0.2s later by the inline stream-end outlet handler under the
prx-stripped OWUI model id (e.g. "deepseek-flash", unpriced, cost $0) —
the v0.4.9 message-id dedup can never fire for API requests. Fixed in
Filter v0.4.20 (content-match echo dedup).

The shadow rows inflated: day/hour request counters, channels.api, tou tier
counters, and cached/input/output token totals (cost was NOT inflated — the
alias matched no price). recent.json got one phantom row per request.

The alias model bucket is pure shadow when  requests == unpriced_requests
and cost_usd == 0  (a bucket containing a genuine non-streaming record would
fail this check and the day is SKIPPED, never guessed at). Repair subtracts
the alias bucket from the day totals and each hour bucket (v0.5.17+ keeps
per-model hour data, so the hour-level subtraction is EXACT), then deletes
the alias key. Days where an hour bucket lacks per-model data for the alias
are SKIPPED and reported.

Usage (plugins may stay online; the script takes the same sibling `.lock`
fcntl lock the plugins take around every ledger write):

    python3 scripts/qk_shadow_repair_0904.py --model deepseek-flash
    python3 scripts/qk_shadow_repair_0904.py --model deepseek-flash --apply

IMPORTANT: update the Filter Function in Open WebUI to >= 0.4.20 FIRST --
an old Filter keeps adding shadow rows while you repair.

Back up ledger.json / recent.json before --apply.
"""

import argparse
import fcntl
import json
import os
import sys
import tempfile

TOK_FIELDS = ("cached", "input", "output")
TOU_TIERS = ("peak", "offpeak", "normal")


def _default_path(name: str) -> str:
    base = os.environ.get("DATA_DIR") or "/app/backend/data"
    return os.path.join(base, "quota_keeper", name)


def _atomic_write(path: str, obj) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def repair(ledger_path: str, recent_path: str, model: str, since: str,
           dry_run: bool = True) -> dict:
    report = {
        "dry_run": bool(dry_run), "model": model, "since": since,
        "days_repaired": 0, "days_skipped": 0, "skipped": [],
        "requests_removed": 0, "tokens_removed": {k: 0.0 for k in TOK_FIELDS},
        "recent_removed": 0,
    }

    def _sub_tokens(dst: dict, src: dict, scale=1.0):
        for k in TOK_FIELDS:
            dst[k] = round(float(dst.get(k, 0.0) or 0.0)
                           - float(src.get(k, 0.0) or 0.0) * scale, 8)

    lock_path = os.path.join(os.path.dirname(os.path.abspath(ledger_path)),
                             ".lock")
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            with open(ledger_path, "r", encoding="utf-8") as f:
                led = json.load(f)
            for uid, u in (led.get("users") or {}).items():
                for day, drec in ((u or {}).get("days") or {}).items():
                    if day < since:
                        continue
                    drec = drec or {}
                    mm = (drec.get("models") or {}).get(model)
                    if not mm:
                        continue
                    reqs = int(mm.get("requests") or 0)
                    if reqs <= 0:
                        continue
                    # purity check: a bucket with any priced/costed request
                    # may hold genuine records -- never guess
                    if int(mm.get("unpriced_requests") or 0) != reqs \
                            or abs(float(mm.get("cost_usd") or 0.0)) > 1e-9:
                        report["days_skipped"] += 1
                        if len(report["skipped"]) < 50:
                            report["skipped"].append(
                                {"user": uid, "day": day,
                                 "reason": "bucket-not-pure-shadow",
                                 "requests": reqs,
                                 "unpriced": mm.get("unpriced_requests"),
                                 "cost": mm.get("cost_usd")})
                        continue
                    # hour-level check: every hour holding the alias must have
                    # a per-model bucket to subtract from exactly
                    hours = drec.get("hours") or {}
                    alias_hours = {
                        hk: (hm or {}) for hk, hrec in hours.items()
                        for hm in [((hrec or {}).get("models") or {}).get(model)]
                        if hm is not None
                    }
                    hour_reqs = sum(int(hm.get("requests") or 0)
                                    for hm in alias_hours.values())
                    if hour_reqs != reqs:
                        report["days_skipped"] += 1
                        if len(report["skipped"]) < 50:
                            report["skipped"].append(
                                {"user": uid, "day": day,
                                 "reason": "hour-buckets-incomplete",
                                 "day_requests": reqs, "hour_requests": hour_reqs})
                        continue
                    report["days_repaired"] += 1
                    report["requests_removed"] += reqs
                    for k in TOK_FIELDS:
                        report["tokens_removed"][k] += float(
                            (mm.get("tokens") or {}).get(k, 0.0) or 0.0)
                    if dry_run:
                        continue
                    # day totals
                    drec["requests"] = int(drec.get("requests") or 0) - reqs
                    _sub_tokens(drec.setdefault("tokens", {}),
                                mm.get("tokens") or {})
                    ch = drec.get("channels") or {}
                    mch = mm.get("channels") or {}
                    for c in ("webui", "api"):
                        if c in ch and c in mch:
                            ch[c] = int(ch.get(c) or 0) - int(mch.get(c) or 0)
                    dtou = drec.get("tou") or {}
                    mtou = mm.get("tou") or {}
                    for t in TOU_TIERS:
                        if t in dtou and t in mtou:
                            dtou[t] = int(dtou.get(t) or 0) - int(mtou.get(t) or 0)
                    # hour buckets
                    for hk, hm in alias_hours.items():
                        hrec = hours[hk]
                        hrec["requests"] = int(hrec.get("requests") or 0) \
                            - int(hm.get("requests") or 0)
                        _sub_tokens(hrec.setdefault("tokens", {}),
                                    hm.get("tokens") or {})
                        hch = hrec.get("channels") or {}
                        hmch = hm.get("channels") or {}
                        for c in ("webui", "api"):
                            if c in hch and c in hmch:
                                hch[c] = int(hch.get(c) or 0) - int(hmch.get(c) or 0)
                        (hrec.get("models") or {}).pop(model, None)
                    del drec["models"][model]
            if not dry_run and report["days_repaired"]:
                _atomic_write(ledger_path, led)

            # recent.json: drop the phantom feed rows (purely cosmetic)
            if os.path.exists(recent_path):
                with open(recent_path, "r", encoding="utf-8") as f:
                    rec = json.load(f)
                items = rec.get("items") or []
                import time as _t
                since_ts = _t.mktime(_t.strptime(since, "%Y-%m-%d"))
                kept = [r for r in items
                        if not (r.get("model") == model
                                and r.get("priced") is False
                                and float(r.get("ts") or 0) >= since_ts)]
                report["recent_removed"] = len(items) - len(kept)
                if not dry_run and report["recent_removed"]:
                    rec["items"] = kept
                    _atomic_write(recent_path, rec)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True,
                    help="alias model key to remove (e.g. deepseek-flash)")
    ap.add_argument("--since", default="2026-08-31",
                    help="only touch days >= this date (default 2026-08-31)")
    ap.add_argument("--ledger", default=_default_path("ledger.json"))
    ap.add_argument("--recent", default=_default_path("recent.json"))
    ap.add_argument("--apply", action="store_true",
                    help="write the repair (default: dry run, report only)")
    args = ap.parse_args(argv)
    if not os.path.exists(args.ledger):
        print(f"ledger not found: {args.ledger}", file=sys.stderr)
        return 2
    if args.apply:
        print("WARNING: applying repair. Back up ledger.json/recent.json first.",
              file=sys.stderr)
    rep = repair(args.ledger, args.recent, args.model, args.since,
                 dry_run=not args.apply)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
