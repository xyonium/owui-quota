#!/usr/bin/env python3
"""One-off repair: re-split 2026-08-24 gemini-3.7-flash buckets by the true
cache rate observed in cpa-usage-keeper (CPA-side per-request data) and
recompute cost. Backs up ledger.json / recent.json before writing.

Reason: before filter 0.4.19 the flat usage.cached_tokens field was dropped,
so cached reads were recorded as full-price input (cache_rate 3.9% vs true
45.7%). Token totals (prompt = cached+input, output) are correct and stay
untouched; only the cached/input split and cost are corrected.

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
MODELS = {"gemini-3.7-flash"}
CACHE_RATE = 0.457292  # CPA 2026-08-24 gemini-3.7-flash*: cache_read/(total prompt), 393 reqs
PRICE = {"input": 0.75, "cached": 0.075, "output": 3.75, "cache_write": 0.038}
DRY = "--apply" not in sys.argv


def cost(tok):
    return (tok.get("cached", 0) * PRICE["cached"]
            + tok.get("input", 0) * PRICE["input"]
            + tok.get("output", 0) * PRICE["output"]) / 1e6


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
            mm = (d.get("models") or {}).get("gemini-3.7-flash")
            if not mm:
                continue
            t = mm.get("tokens") or {}
            prompt = (t.get("cached", 0) or 0) + (t.get("input", 0) or 0)
            if prompt <= 0:
                continue
            new_cached = round(prompt * CACHE_RATE, 1)
            new_input = round(prompt - new_cached, 1)
            old_c, old_i = t.get("cached", 0), t.get("input", 0)
            if abs(new_cached - old_c) < 1:
                continue
            old_bucket_cost = mm.get("cost_usd", 0) or 0
            new_bucket_cost = cost({"cached": new_cached, "input": new_input,
                                    "output": t.get("output", 0)})
            delta = new_bucket_cost - old_bucket_cost
            print(f"user {uid[:8]} {u.get('name')}: cached {old_c:.0f}->{new_cached:.0f} "
                  f"input {old_i:.0f}->{new_input:.0f} cost ${old_bucket_cost:.4f}->${new_bucket_cost:.4f}")
            if not DRY:
                t["cached"], t["input"] = new_cached, new_input
                mm["cost_usd"] = new_bucket_cost
                # roll the delta up to the day bucket and user totals
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
        print(f"\nledger: {changed_d} buckets, cost ${old_cost_sum:.4f} -> ${new_cost_sum:.4f}"
              f" (delta ${new_cost_sum - old_cost_sum:+.4f})")

        # recent.json: per-request gemini-3.7-flash entries, same re-split
        rec = json.load(open(RECENT, encoding="utf-8"))
        items = rec.get("items") if isinstance(rec, dict) else rec
        n_rec = 0
        rec_old = rec_new = 0.0
        for it in items or []:
            if it.get("model") not in MODELS:
                continue
            t = it.get("tokens") or {}
            prompt = (t.get("cached", 0) or 0) + (t.get("input", 0) or 0)
            if prompt <= 0:
                continue
            new_cached = round(prompt * CACHE_RATE, 1)
            new_input = round(prompt - new_cached, 1)
            oc = it.get("cost_usd", 0) or 0
            nc = cost({"cached": new_cached, "input": new_input, "output": t.get("output", 0)})
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
            shutil.copy2(LEDGER, LEDGER + ".bak-cachefix-20260824")
            shutil.copy2(RECENT, RECENT + ".bak-cachefix-20260824")
            atomic_write(LEDGER, led)
            atomic_write(RECENT, rec)
            print("APPLIED (backups: *.bak-cachefix-20260824)")
        else:
            print("DRY RUN — rerun with --apply to write")


if __name__ == "__main__":
    main()
