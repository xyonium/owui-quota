#!/usr/bin/env python3
"""One-off repair for ledger buckets inflated by the pre-0.4.18 topup bug.

Background: for webui streaming chats, OWUI 0.11 fires outlet() a second time
at stream end with a rebuilt message body whose messages[-1].usage carries the
FULL usage; the filter recorded it again as a "topup" (count_request=False),
adding the full tokens/cost a second time into the day/hour/day-model buckets.
Request counters were guarded and stayed correct. Direct API requests never
take that path and were never doubled (verified on the production ledger:
every api-only hour sums to exactly 1.0x, webui hours to ~2.0x).

The hour per-model buckets (hm, v0.5.17+) only ever received count_request=True
records, so where they are REAL they hold the TRUE usage. Two day shapes:

  ANCHORED days (post-backfill era): sum(hm) < day/model totals -- the excess
      is exactly the double-count. Repair rebuilds hour totals, day per-model
      buckets and day totals from the hour per-model sums. EXACT.
  BACKFILL-ERA days (pre-v0.5.17): the v0.5.17 migration synthesised hm from
      day totals, so sum(hm) == day totals and no anchor survives. Doubling is
      undetectable there; by default these days are only REPORTED. With
      --estimate-backfill they are deflated by the per-model channel share
      factor (a+w)/(a+2w) -- i.e. "halve the webui share", generalised so pure
      api buckets stay untouched. ESTIMATE: assumes every webui streaming
      request doubled and none of the api requests did; residual error is the
      token-mix difference between channels.

cost_saved_usd is scaled by the bucket's cost ratio in both modes.
recent.json needs no repair: topups never entered it (per-request records).
Days whose shapes fit neither mode (genuine partial-usage topups, phantom
hours from boundary-crossing topups, backfill contamination with excess,
anchors exceeding their buckets) are SKIPPED and reported, never guessed at.

Usage (the plugins may stay online; the script locks the sibling `.lock`
file -- the same fcntl lock the plugins take around every ledger write, so
repairs are mutually exclusive with live metering):

    python3 scripts/qk_dedup_repair.py                 # dry run, report only
    python3 scripts/qk_dedup_repair.py --apply         # repair anchored days
    python3 scripts/qk_dedup_repair.py --apply --estimate-backfill
                                                       # + deflate backfill-era days

IMPORTANT: update the two Functions in Open WebUI to >= 0.5.37 / 0.4.18
FIRST -- an old Filter keeps double-recording new webui requests while you
repair, so the dashboard drifts back within minutes.

Back up ledger.json before --apply (cp ledger.json ledger.json.bak).
"""

import argparse
import fcntl
import json
import os
import sys
import tempfile

TOK_FIELDS = ("cached", "input", "output")


def _close(a: float, b: float) -> bool:
    """fp-tolerant equality for aggregated ledger floats (per-add rounding to
    8dp and non-associative summation make exact == brittle)."""
    return abs(a - b) <= max(1e-9, 1e-6 * max(abs(a), abs(b)))


def _default_ledger_path() -> str:
    base = os.environ.get("DATA_DIR") or "/app/backend/data"
    return os.path.join(base, "quota_keeper", "ledger.json")


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


def _tok_sum(t) -> float:
    return sum(float((t or {}).get(k, 0.0) or 0.0) for k in TOK_FIELDS)


def _channel_factor(bucket: dict) -> float:
    """(a+w)/(a+2w): the deflation factor when every webui request doubled
    and no api request did. 1.0 for pure-api or unattributed buckets."""
    ch = (bucket or {}).get("channels") or {}
    a = float(ch.get("api", 0) or 0)
    w = float(ch.get("webui", 0) or 0)
    if a + w <= 0:
        return 1.0
    return (a + w) / (a + 2.0 * w)


def qk_dedup_repair(ledger_path: str, dry_run: bool = True,
                    estimate_backfill: bool = False) -> dict:
    """See module docstring. Returns a report dict; dry_run=True computes
    without writing."""
    report = {
        "dry_run": bool(dry_run),
        "estimate_backfill": bool(estimate_backfill),
        "ledger": ledger_path,
        "users_scanned": 0,
        "days_repaired": 0,
        "days_estimated": 0,
        "days_clean": 0,
        "days_skipped_legacy": 0,
        "days_skipped_ambiguous": 0,
        "days_backfill_untouched": 0,
        "tokens_removed": {k: 0.0 for k in TOK_FIELDS},
        "cost_removed_usd": 0.0,
        "est_tokens_removed": {k: 0.0 for k in TOK_FIELDS},
        "est_cost_removed_usd": 0.0,
        "skipped": [],
    }

    def _skip(uid, day, reason):
        report["days_skipped_" + reason] += 1
        if len(report["skipped"]) < 50:
            report["skipped"].append(
                {"user": uid, "day": day,
                 "reason": "legacy-hours" if reason == "legacy"
                 else "ambiguous-excess"})

    # The plugins serialize ALL ledger writes with an fcntl lock on the
    # sibling `.lock` file (not on the ledger itself) -- take the same lock
    # so a live Open WebUI cannot read-modify-write across our repair.
    lock_path = os.path.join(os.path.dirname(os.path.abspath(ledger_path)),
                             ".lock")
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            with open(ledger_path, "r", encoding="utf-8") as f:
                led = json.load(f)
            users = led.get("users") or {}
            for uid, u in users.items():
                report["users_scanned"] += 1
                for day, drec in ((u or {}).get("days") or {}).items():
                    drec = drec or {}
                    hours = drec.get("hours") or {}
                    dmodels = drec.get("models") or {}
                    dtok = drec.get("tokens") or {}
                    has_usage = _tok_sum(dtok) > 0 or bool(dmodels)
                    if not hours:
                        if has_usage:
                            _skip(uid, day, "legacy")
                        else:
                            report["days_clean"] += 1
                        continue

                    # --- build the hour per-model anchor -------------------
                    T = {}        # m -> {tokens per field, "cost"}
                    frac = False  # fractional hm tokens = synthetic backfill
                    phantom = False  # hour with usage but no per-model data
                    for hk, hrec in hours.items():
                        hrec = hrec or {}
                        hmods = hrec.get("models") or {}
                        if not hmods:
                            if _tok_sum(hrec.get("tokens")) > 0 \
                                    or (hrec.get("cost_usd") or 0.0):
                                phantom = True
                            continue
                        for m, hm in hmods.items():
                            hm = hm or {}
                            e = T.setdefault(
                                m, {k: 0.0 for k in TOK_FIELDS} | {"cost": 0.0})
                            for k in TOK_FIELDS:
                                v = float((hm.get("tokens") or {}).get(k, 0.0) or 0.0)
                                if abs(v - round(v)) > 1e-6:
                                    frac = True
                                e[k] += v
                            e["cost"] += float(hm.get("cost_usd") or 0.0)

                    if phantom:
                        # boundary-crossing topup or legacy record: the usage
                        # cannot be attributed to a model at hour level
                        _skip(uid, day, "ambiguous")
                        continue

                    covered = set(T.keys())
                    if set(dmodels.keys()) != covered:
                        _skip(uid, day, "ambiguous")
                        continue

                    # per-model anchor vs day bucket: T <= mm everywhere is
                    # required (removal-only); any excess flags the day as
                    # doubled (topups never reached hm)
                    excess = False
                    violation = False
                    for m, mm in dmodels.items():
                        mm = mm or {}
                        for k in TOK_FIELDS:
                            mv = float((mm.get("tokens") or {}).get(k, 0.0) or 0.0)
                            tv = T[m][k]
                            if tv > mv + max(1e-9, 1e-6 * mv):
                                violation = True
                            elif not _close(tv, mv):
                                excess = True
                        mc = float(mm.get("cost_usd") or 0.0)
                        tc = T[m]["cost"]
                        if tc > mc + max(1e-9, 1e-6 * mc):
                            violation = True
                        elif not _close(tc, mc):
                            excess = True
                    if violation:
                        _skip(uid, day, "ambiguous")
                        continue

                    if not excess:
                        # no topup excess: the day totals are whatever they
                        # are; fractional hm marks a backfilled day
                        if frac:
                            if estimate_backfill and _estimate_day(
                                    drec, hours, dmodels, report):
                                report["days_estimated"] += 1
                            else:
                                report["days_backfill_untouched"] += 1
                        else:
                            report["days_clean"] += 1
                        continue
                    if frac:
                        # backfill-contaminated anchor with an excess on top:
                        # not reconstructable
                        _skip(uid, day, "ambiguous")
                        continue
                    # removal-only at hour level too: an hour below its
                    # per-model sum contradicts the topup pattern
                    hour_bad = False
                    for hk, hrec in hours.items():
                        hrec = hrec or {}
                        hmods = hrec.get("models") or {}
                        shm = {k: sum(float(((hm or {}).get("tokens") or {})
                                            .get(k, 0.0) or 0.0)
                                      for hm in hmods.values())
                               for k in TOK_FIELDS}
                        scost = sum(float((hm or {}).get("cost_usd") or 0.0)
                                    for hm in hmods.values())
                        for k in TOK_FIELDS:
                            if float((hrec.get("tokens") or {}).get(k, 0.0) or 0.0) \
                                    < shm[k] - max(1e-9, 1e-6 * shm[k]):
                                hour_bad = True
                        if float(hrec.get("cost_usd") or 0.0) < scost - max(1e-9, 1e-6 * scost):
                            hour_bad = True
                    if hour_bad:
                        _skip(uid, day, "ambiguous")
                        continue

                    # --- ANCHORED repair: rebuild from the hour per-model sums
                    removed_tok = {k: 0.0 for k in TOK_FIELDS}
                    removed_cost = 0.0
                    if not dry_run:
                        for hk, hrec in hours.items():
                            hrec = hrec or {}
                            hmods = hrec.get("models") or {}
                            ht = hrec.setdefault("tokens", {})
                            for k in TOK_FIELDS:
                                ht[k] = sum(float(((hm or {}).get("tokens") or {})
                                                  .get(k, 0.0) or 0.0)
                                            for hm in hmods.values())
                            hrec["cost_usd"] = round(sum(
                                float((hm or {}).get("cost_usd") or 0.0)
                                for hm in hmods.values()), 8)
                    new_day_tok = {k: 0.0 for k in TOK_FIELDS}
                    new_day_cost = 0.0
                    for m, mm in dmodels.items():
                        mm = mm or {}
                        for k in TOK_FIELDS:
                            removed_tok[k] += float((mm.get("tokens") or {})
                                                    .get(k, 0.0) or 0.0) - T[m][k]
                            new_day_tok[k] += T[m][k]
                        old_cost = float(mm.get("cost_usd") or 0.0)
                        removed_cost += old_cost - T[m]["cost"]
                        new_day_cost += T[m]["cost"]
                        if not dry_run:
                            ratio = (T[m]["cost"] / old_cost) if old_cost > 0 else 1.0
                            mt = mm.setdefault("tokens", {})
                            for k in TOK_FIELDS:
                                mt[k] = T[m][k]
                            mm["cost_usd"] = round(T[m]["cost"], 8)
                            mm["cost_saved_usd"] = round(
                                float(mm.get("cost_saved_usd") or 0.0) * ratio, 8)
                    if not dry_run:
                        old_day_cost = float(drec.get("cost_usd") or 0.0)
                        ratio_d = (new_day_cost / old_day_cost) if old_day_cost > 0 else 1.0
                        dt = drec.setdefault("tokens", {})
                        for k in TOK_FIELDS:
                            dt[k] = new_day_tok[k]
                        drec["cost_usd"] = round(new_day_cost, 8)
                        drec["cost_saved_usd"] = round(
                            float(drec.get("cost_saved_usd") or 0.0) * ratio_d, 8)
                    for k in TOK_FIELDS:
                        report["tokens_removed"][k] = round(
                            report["tokens_removed"][k] + max(0.0, removed_tok[k]), 8)
                    report["cost_removed_usd"] = round(
                        report["cost_removed_usd"] + max(0.0, removed_cost), 8)
                    report["days_repaired"] += 1
            if not dry_run and (report["days_repaired"] or report["days_estimated"]):
                _atomic_write(ledger_path, led)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    return report


def _estimate_day(drec: dict, hours: dict, dmodels: dict, report: dict) -> None:
    """Backfill-era heuristic: deflate by the per-model channel share factor
    (a+w)/(a+2w) -- 'halve the webui share', api-only buckets stay untouched.
    Day/hour totals are rebuilt from the scaled per-model buckets so the
    buckets stay mutually consistent. Estimates accumulate under
    Estimate-mode entry point. Returns True when the day was actually
    deflated (any channel-attributed webui share), False when every bucket
    factor was 1.0 (pure-api / unattributed -- nothing to estimate)."""
    changed = False
    new_day_tok = {k: 0.0 for k in TOK_FIELDS}
    new_day_cost = 0.0
    for m, mm in dmodels.items():
        mm = mm or {}
        f = _channel_factor(mm)
        mt = mm.setdefault("tokens", {})
        for k in TOK_FIELDS:
            v = float(mt.get(k, 0.0) or 0.0)
            if not _close(f, 1.0):
                report["est_tokens_removed"][k] = round(
                    report["est_tokens_removed"][k] + v * (1.0 - f), 8)
                mt[k] = v * f
                changed = True
            new_day_tok[k] += mt[k]
        c = float(mm.get("cost_usd") or 0.0)
        if not _close(f, 1.0):
            report["est_cost_removed_usd"] = round(
                report["est_cost_removed_usd"] + c * (1.0 - f), 8)
            mm["cost_usd"] = round(c * f, 8)
            mm["cost_saved_usd"] = round(float(mm.get("cost_saved_usd") or 0.0) * f, 8)
        new_day_cost += mm["cost_usd"]
        for hrec in hours.values():
            hm = (((hrec or {}).get("models") or {}).get(m))
            if not hm:
                continue
            hmt = hm.get("tokens") or {}
            for k in TOK_FIELDS:
                if k in hmt:
                    hmt[k] = float(hmt.get(k) or 0.0) * f
            if "cost_usd" in hm:
                hm["cost_usd"] = round(float(hm.get("cost_usd") or 0.0) * f, 8)
    if not changed:
        return
    # day totals and hour totals rebuilt from the scaled per-model buckets
    old_day_cost = float(drec.get("cost_usd") or 0.0)
    ratio_d = (new_day_cost / old_day_cost) if old_day_cost > 0 else 1.0
    dt = drec.setdefault("tokens", {})
    for k in TOK_FIELDS:
        dt[k] = new_day_tok[k]
    drec["cost_usd"] = round(new_day_cost, 8)
    drec["cost_saved_usd"] = round(
        float(drec.get("cost_saved_usd") or 0.0) * ratio_d, 8)
    for hrec in hours.values():
        hrec = hrec or {}
        hmods = hrec.get("models") or {}
        if not hmods:
            continue
        ht = hrec.setdefault("tokens", {})
        for k in TOK_FIELDS:
            ht[k] = sum(float(((hm or {}).get("tokens") or {}).get(k, 0.0) or 0.0)
                        for hm in hmods.values())
        hrec["cost_usd"] = round(sum(float((hm or {}).get("cost_usd") or 0.0)
                                     for hm in hmods.values()), 8)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=_default_ledger_path(),
                    help="path to ledger.json (default: $DATA_DIR/quota_keeper/ledger.json "
                         "or /app/backend/data/quota_keeper/ledger.json)")
    ap.add_argument("--apply", action="store_true",
                    help="write the repair (default: dry run, report only)")
    ap.add_argument("--estimate-backfill", action="store_true",
                    help="also deflate backfill-era days by the per-model "
                         "channel share factor (ESTIMATE: assumes every webui "
                         "streaming request doubled, no api request did)")
    args = ap.parse_args(argv)
    if not os.path.exists(args.ledger):
        print(f"ledger not found: {args.ledger}", file=sys.stderr)
        return 2
    if args.apply:
        print("WARNING: applying repair. Back up ledger.json first if you "
              "haven't (cp ledger.json ledger.json.bak).", file=sys.stderr)
    rep = qk_dedup_repair(args.ledger, dry_run=not args.apply,
                          estimate_backfill=args.estimate_backfill)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
