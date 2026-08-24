#!/usr/bin/env python3
"""One-off repair for ledger buckets double-counted by the pre-0.4.18 topup bug.

Background: a repeated usage-bearing event for an already-recorded response id
(OWUI 0.11 stream-end outlet echoing messages[-1].usage, or a duplicate usage
chunk sharing the response id) was added IN FULL a second time into the
day/hour/day-model buckets, doubling tokens/cost for affected responses
(request counters were guarded and stayed correct). The hour per-model buckets
(count_request=True only) therefore hold the TRUE usage; everything above them
absorbed the repeat.

A day is repaired ONLY when the doubling is exactly verifiable:
  - every hour of the day carries per-model data (v0.5.17+ buckets), and
  - for every token field AND cost, each hour's total is either equal to its
    per-model sum (clean hour) or exactly twice it (pure full-repeat doubling),
  - and the day totals agree with their per-model sums.
The repair rebuilds hour totals, day per-model buckets and day totals from the
hour per-model sums (exact under pure doubling; cost_saved_usd is scaled by
the bucket's cost ratio, exact when the doubling was uniform). recent.json
needs no repair: topups never entered it (per-request records only).

Days with any other excess shape (genuine Anthropic partial-usage topups whose
deltas are real usage, mixed patterns, legacy hours without per-model data)
are SKIPPED and reported -- never guessed at.

Usage (run on the server, Open WebUI stopped or quiet is safest; the script
takes the same fcntl lock the plugins use):

    python3 scripts/qk_dedup_repair.py                 # dry run, report only
    python3 scripts/qk_dedup_repair.py --apply         # write the repair
    python3 scripts/qk_dedup_repair.py --ledger /app/backend/data/quota_keeper/ledger.json --apply

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


def qk_dedup_repair(ledger_path: str, dry_run: bool = True) -> dict:
    """See module docstring. Returns a report dict; dry_run=True computes
    without writing."""
    report = {
        "dry_run": bool(dry_run),
        "ledger": ledger_path,
        "users_scanned": 0,
        "days_repaired": 0,
        "days_clean": 0,
        "days_skipped_legacy": 0,
        "days_skipped_ambiguous": 0,
        "tokens_removed": {k: 0.0 for k in TOK_FIELDS},
        "cost_removed_usd": 0.0,
        "skipped": [],
    }

    def _skip(uid, day, reason):
        report["days_skipped_" + reason] += 1
        if len(report["skipped"]) < 50:
            report["skipped"].append(
                {"user": uid, "day": day,
                 "reason": "legacy-hours" if reason == "legacy"
                 else "ambiguous-excess"})

    with open(ledger_path, "r+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            led = json.load(lf)
            users = led.get("users") or {}
            for uid, u in users.items():
                report["users_scanned"] += 1
                for day, drec in ((u or {}).get("days") or {}).items():
                    drec = drec or {}
                    hours = drec.get("hours") or {}
                    dmodels = drec.get("models") or {}
                    # classify hours: clean | doubled | anything-else
                    kinds = {}
                    for hk, hrec in hours.items():
                        hrec = hrec or {}
                        hmods = hrec.get("models") or {}
                        htok = hrec.get("tokens") or {}
                        if not hmods:
                            # legacy hour (pre-v0.5.17): no per-model data.
                            # Only ignorable if it recorded nothing at all.
                            if any((htok.get(k) or 0.0) for k in TOK_FIELDS) \
                                    or (hrec.get("cost_usd") or 0.0):
                                kinds[hk] = "legacy"
                            else:
                                kinds[hk] = "clean"
                            continue
                        mtok = {k: 0.0 for k in TOK_FIELDS}
                        mcost = 0.0
                        for hm in hmods.values():
                            for k in TOK_FIELDS:
                                mtok[k] += ((hm or {}).get("tokens") or {}).get(k, 0.0) or 0.0
                            mcost += float((hm or {}).get("cost_usd") or 0.0)
                        hcost = float(hrec.get("cost_usd") or 0.0)
                        if all(_close(float(htok.get(k) or 0.0), mtok[k])
                               for k in TOK_FIELDS) and _close(hcost, mcost):
                            kinds[hk] = "clean"
                        elif all(_close(float(htok.get(k) or 0.0), 2.0 * mtok[k])
                                 for k in TOK_FIELDS) and _close(hcost, 2.0 * mcost):
                            kinds[hk] = "doubled"
                        else:
                            kinds[hk] = "ambiguous"
                    has_usage = any((drec.get("tokens") or {}).get(k, 0.0)
                                    for k in TOK_FIELDS) or dmodels
                    if "legacy" in kinds.values() or (has_usage and not hours):
                        _skip(uid, day, "legacy")
                        continue
                    if "ambiguous" in kinds.values():
                        _skip(uid, day, "ambiguous")
                        continue
                    doubled_hours = [hk for hk, k in kinds.items() if k == "doubled"]
                    if not doubled_hours:
                        report["days_clean"] += 1
                        continue
                    # every model recorded at day level must be covered by the
                    # hour per-model data, and vice versa -- otherwise the day
                    # cannot be rebuilt exactly from it
                    covered = set()
                    for hrec in hours.values():
                        covered |= set(((hrec or {}).get("models") or {}).keys())
                    if set(dmodels.keys()) != covered:
                        _skip(uid, day, "ambiguous")
                        continue
                    # day totals and their per-model sums must agree with each
                    # other (both received every record); otherwise the excess
                    # shape is not the pure-doubling pattern this repair targets
                    msum_tok = {k: sum(((mm or {}).get("tokens") or {}).get(k, 0.0) or 0.0
                                       for mm in dmodels.values()) for k in TOK_FIELDS}
                    msum_cost = sum(float((mm or {}).get("cost_usd") or 0.0)
                                    for mm in dmodels.values())
                    dtok = drec.get("tokens") or {}
                    dcost = float(drec.get("cost_usd") or 0.0)
                    if not (all(_close(float(dtok.get(k) or 0.0), msum_tok[k])
                                for k in TOK_FIELDS)
                            and _close(dcost, msum_cost)):
                        _skip(uid, day, "ambiguous")
                        continue
                    # --- repair: rebuild from the hour per-model sums -------
                    # true usage = the hour per-model sums (count_request=True
                    # records only). The removed amount is counted ONCE, at
                    # the day level (the hour/day-model excesses are the same
                    # double-count viewed at different aggregation levels).
                    true_model = {}  # m -> {"tok": {...}, "cost": float}
                    true_tok = {k: 0.0 for k in TOK_FIELDS}
                    true_cost = 0.0
                    for hrec in hours.values():
                        for m, hm in ((hrec or {}).get("models") or {}).items():
                            ent = true_model.setdefault(
                                m, {"tok": {k: 0.0 for k in TOK_FIELDS}, "cost": 0.0})
                            for k in TOK_FIELDS:
                                v = ((hm or {}).get("tokens") or {}).get(k, 0.0) or 0.0
                                ent["tok"][k] += v
                                true_tok[k] += v
                            c = float((hm or {}).get("cost_usd") or 0.0)
                            ent["cost"] += c
                            true_cost += c
                    removed_tok = {k: max(0.0, float(dtok.get(k, 0.0) or 0.0) - true_tok[k])
                                   for k in TOK_FIELDS}
                    removed_cost = max(0.0, dcost - true_cost)
                    if not dry_run:
                        for hk in doubled_hours:
                            hrec = hours[hk]
                            hmods = hrec.get("models") or {}
                            for k in TOK_FIELDS:
                                hrec.setdefault("tokens", {})[k] = sum(
                                    ((hm or {}).get("tokens") or {}).get(k, 0.0) or 0.0
                                    for hm in hmods.values())
                            hrec["cost_usd"] = round(sum(
                                float((hm or {}).get("cost_usd") or 0.0)
                                for hm in hmods.values()), 8)
                        for m, mm in dmodels.items():
                            mm = mm or {}
                            ent = true_model[m]
                            old_cost = float(mm.get("cost_usd") or 0.0)
                            ratio = (ent["cost"] / old_cost) if old_cost > 0 else 1.0
                            mt = mm.setdefault("tokens", {})
                            for k in TOK_FIELDS:
                                mt[k] = ent["tok"][k]
                            mm["cost_usd"] = round(ent["cost"], 8)
                            mm["cost_saved_usd"] = round(
                                float(mm.get("cost_saved_usd") or 0.0) * ratio, 8)
                        old_day_cost = float(drec.get("cost_usd") or 0.0)
                        ratio_d = (true_cost / old_day_cost) if old_day_cost > 0 else 1.0
                        dt = drec.setdefault("tokens", {})
                        for k in TOK_FIELDS:
                            dt[k] = true_tok[k]
                        drec["cost_usd"] = round(true_cost, 8)
                        drec["cost_saved_usd"] = round(
                            float(drec.get("cost_saved_usd") or 0.0) * ratio_d, 8)
                    for k in TOK_FIELDS:
                        report["tokens_removed"][k] = round(
                            report["tokens_removed"][k] + removed_tok[k], 8)
                    report["cost_removed_usd"] = round(
                        report["cost_removed_usd"] + removed_cost, 8)
                    report["days_repaired"] += 1
            if not dry_run and report["days_repaired"]:
                _atomic_write(ledger_path, led)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=_default_ledger_path(),
                    help="path to ledger.json (default: $DATA_DIR/quota_keeper/ledger.json "
                         "or /app/backend/data/quota_keeper/ledger.json)")
    ap.add_argument("--apply", action="store_true",
                    help="write the repair (default: dry run, report only)")
    args = ap.parse_args(argv)
    if not os.path.exists(args.ledger):
        print(f"ledger not found: {args.ledger}", file=sys.stderr)
        return 2
    if args.apply:
        print("WARNING: applying repair. Back up ledger.json first if you "
              "haven't (cp ledger.json ledger.json.bak).", file=sys.stderr)
    rep = qk_dedup_repair(args.ledger, dry_run=not args.apply)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
