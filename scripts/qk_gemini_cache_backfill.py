#!/usr/bin/env python3
"""One-off repair v2: re-split 2026-08-24 gemini day buckets by the true
cache rate observed in cpa-usage-keeper (CPA-side per-request data) and
recompute cost. Backs up ledger.json / recent.json before writing.

Reason: before filter 0.4.19 the flat usage.cached_tokens field was dropped,
so cached reads were recorded as full-price input (cache_rate ~4% vs true
84%). Token totals (prompt = cached+input, output) are correct and stay
untouched; only the cached/input split and cost are corrected.

v2 fixes the rate semantics: CPA input_tokens INCLUDES the cached part
(verified by a request both sides recorded), so rate = cache_read/input,
not cache_read/(input+cache_read) as v1 wrongly used (45.7% vs 84.3%).

Run inside the open-webui container:
    docker cp scripts/qk_gemini_cache_backfill.py open-webui:/tmp/
    docker exec open-webui python3 /tmp/qk_gemini_cache_backfill.py          # dry run
    docker exec open-webui python3 /tmp/qk_gemini_cache_backfill.py --apply
"""
import json, os, shutil, sys, tempfile, fcntl

DATA = "/app/backend/data/quota_keeper"
LEDGER = os.path.join(DATA, "ledger.json")
RECENT = os.path.join(DATA, "recent.json")
TODAY = "2026-08-24"
# rate = CPA cache_read_tokens / input_tokens for the same day's traffic.
# CPA's input_tokens is the TOTAL prompt (cache included) — verified against
# a request both sides recorded: QK input 53023 (cached 0) == CPA input
# 53023 + cache_read 48783. (v1 of this script wrongly used
# cache_read/(input+cache_read)=45.7%, under-correcting the split.)
# gemini-3.7-flash := CPA gemini-3.7-flash-high (89.0%) + gemini-flash-latest
# (66.8%) blended = 46930707/55696680; gemini-3.1-pro-preview := CPA
# gemini-pro-agent (60.35%; token totals match exactly: 363331).
MODELS = {
    "gemini-3.7-flash": {
        "rate": 0.84262,
        "price": {"input": 0.75, "cached": 0.075, "output": 3.75},
    },
    "gemini-3.1-pro-preview": {
        "rate": 0.60353,
        "price": {"input": 2.0, "cached": 0.2, "output": 12.0},
    },
}
DRY = "--apply" not in sys.argv


def cost(tok, price):
    return (tok.get("cached", 0) * price["cached"]
            + tok.get("input", 0) * price["input"]
            + tok.get("output", 0) * price["output"]) / 1e6


def atomic_write(path, obj):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    lock_path = LEDGER + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        led = json.load(open(LEDGER, encoding="utf-8"))
        changed_d = 0
        old_cost_sum = new_cost_sum = 0.0
        for uid, u in (led.get("users") or {}).items():
            d = (u.get("days") or {}).get(TODAY)
            if not d:
                continue
            for model, spec in MODELS.items():
                mm = (d.get("models") or {}).get(model)
                if not mm:
                    continue
                rate, price = spec["rate"], spec["price"]
                t = mm.get("tokens") or {}
                prompt = (t.get("cached", 0) or 0) + (t.get("input", 0) or 0)
                if prompt <= 0:
                    continue
                new_cached = round(prompt * rate, 1)
                new_input = round(prompt - new_cached, 1)
                old_c, old_i = t.get("cached", 0), t.get("input", 0)
                if abs(new_cached - old_c) < 1:
                    continue
                old_bucket_cost = mm.get("cost_usd", 0) or 0
                new_bucket_cost = cost({"cached": new_cached, "input": new_input,
                                        "output": t.get("output", 0)}, price)
                delta = new_bucket_cost - old_bucket_cost
                print(f"user {uid[:8]} {u.get('name')} [{model}]: cached {old_c:.0f}->{new_cached:.0f} "
                      f"input {old_i:.0f}->{new_input:.0f} cost ${old_bucket_cost:.4f}->${new_bucket_cost:.4f}")
                if not DRY:
                    t["cached"], t["input"] = new_cached, new_input
                    mm["cost_usd"] = new_bucket_cost
                    # day/user totals were already corrected by v2; hour
                    # buckets are fixed below. Only roll up when they differ
                    # (i.e. this run changed the day-model bucket).
                    d["cost_usd"] = (d.get("cost_usd", 0) or 0) + delta
                    dt = d.get("tokens") or {}
                    dt["cached"] = (dt.get("cached", 0) or 0) + (new_cached - old_c)
                    dt["input"] = (dt.get("input", 0) or 0) + (new_input - old_i)
                    d["tokens"] = dt
                    tot = u.get("total") or {}
                    if tot:
                        tot["cost_usd"] = (tot.get("cost_usd", 0) or 0) + delta
                        tt = tot.get("tokens") or {}
                        tt["cached"] = (tt.get("cached", 0) or 0) + (new_cached - old_c)
                        tt["input"] = (tt.get("input", 0) or 0) + (new_input - old_i)
                        tot["tokens"] = tt
                changed_d += 1
                old_cost_sum += old_bucket_cost
                new_cost_sum += new_bucket_cost

        # v3: hour buckets (days[day].hours[hour].models[model]) carry their
        # own tokens/cost and back the 24h view — re-split them too. Delta
        # rolls into the hour record's own totals only; day/user totals are
        # already correct from the day-model pass above.
        changed_h = 0
        for uid, u in (led.get("users") or {}).items():
            d = (u.get("days") or {}).get(TODAY)
            if not d:
                continue
            for hh, hrec in ((d.get("hours") or {}).items()):
                hmodels = (hrec or {}).get("models") or {}
                for model, spec in MODELS.items():
                    mm = hmodels.get(model)
                    if not mm:
                        continue
                    rate, price = spec["rate"], spec["price"]
                    t = mm.get("tokens") or {}
                    prompt = (t.get("cached", 0) or 0) + (t.get("input", 0) or 0)
                    if prompt <= 0:
                        continue
                    new_cached = round(prompt * rate, 1)
                    new_input = round(prompt - new_cached, 1)
                    old_c, old_i = t.get("cached", 0), t.get("input", 0)
                    if abs(new_cached - old_c) < 1:
                        continue
                    old_hc = mm.get("cost_usd", 0) or 0
                    new_hc = cost({"cached": new_cached, "input": new_input,
                                   "output": t.get("output", 0)}, price)
                    delta = new_hc - old_hc
                    print(f"hour {hh} user {uid[:8]} [{model}]: cached {old_c:.0f}->{new_cached:.0f} "
                          f"cost ${old_hc:.4f}->${new_hc:.4f}")
                    if not DRY:
                        t["cached"], t["input"] = new_cached, new_input
                        mm["cost_usd"] = new_hc
                        hrec["cost_usd"] = (hrec.get("cost_usd", 0) or 0) + delta
                        ht = hrec.get("tokens") or {}
                        ht["cached"] = (ht.get("cached", 0) or 0) + (new_cached - old_c)
                        ht["input"] = (ht.get("input", 0) or 0) + (new_input - old_i)
                        hrec["tokens"] = ht
                    changed_h += 1
        print(f"hours: {changed_h} hour-model buckets fixed")
        print(f"\nledger: {changed_d} buckets, cost ${old_cost_sum:.4f} -> ${new_cost_sum:.4f}"
              f" (delta ${new_cost_sum - old_cost_sum:+.4f})")

        # recent.json: per-request entries for the affected models, same re-split
        rec = json.load(open(RECENT, encoding="utf-8"))
        items = rec.get("items") if isinstance(rec, dict) else rec
        n_rec = 0
        rec_old = rec_new = 0.0
        for it in items or []:
            spec = MODELS.get(it.get("model"))
            if not spec:
                continue
            rate, price = spec["rate"], spec["price"]
            t = it.get("tokens") or {}
            prompt = (t.get("cached", 0) or 0) + (t.get("input", 0) or 0)
            if prompt <= 0:
                continue
            new_cached = round(prompt * rate, 1)
            new_input = round(prompt - new_cached, 1)
            oc = it.get("cost_usd", 0) or 0
            nc = cost({"cached": new_cached, "input": new_input, "output": t.get("output", 0)}, price)
            print(f"recent ts={it.get('ts')}: cached {t.get('cached', 0):.0f}->{new_cached:.0f} "
                  f"cost ${oc:.5f}->${nc:.5f}")
            if not DRY:
                t["cached"], t["input"] = new_cached, new_input
                it["cost_usd"] = nc
            n_rec += 1
            rec_old += oc
            rec_new += nc
        print(f"recent: {n_rec} entries, cost ${rec_old:.4f} -> ${rec_new:.4f}")

        if not DRY:
            # keep earlier backups intact; each version backs up separately
            shutil.copy2(LEDGER, LEDGER + ".bak-cachefix3-20260824")
            shutil.copy2(RECENT, RECENT + ".bak-cachefix3-20260824")
            atomic_write(LEDGER, led)
            atomic_write(RECENT, rec)
            print("APPLIED (backups: *.bak-cachefix3-20260824)")
        else:
            print("DRY RUN — rerun with --apply to write")


if __name__ == "__main__":
    main()
