#!/usr/bin/env python3
"""One-off repair v4: re-split 2026-08-25 gemini-3.7-flash cache for
user 71477b94 (谢邃韬) hour-11 and hour-15 buckets.

Reason: the aistudio/gemini channel of cli-proxy-api translated upstream
Gemini usageMetadata to Claude format WITHOUT the cache split (input_tokens
= full prompt incl. cache, no cache_read_input_tokens) — the antigravity
translator already did this correctly. Quota Keeper therefore recorded
cached=0 and billed cached reads at full input price. Fixed upstream
(translator + unit tests, deployed 2026-08-25 ~18:30); this script repairs
the already-recorded rows.

Scope (verified against cpa-usage-keeper usage_events, 2026-08-25):
- hour 11: QK 134 reqs input 12,623,112 == CPA 133 /v1/messages rows
  (96 aistudio gemini-flash-latest + 37 gemini gemini-3.7-flash-high),
  cached 11,221,549 -> rate 0.91165. Other gemini rows that hour
  (周鹏翔 etc.) were OpenAI-format and already carry correct cache.
- hour 15: QK 27 reqs input 3,974,791 EXACTLY == CPA gemini-3.7-flash-high
  /v1/messages (cached 3,583,709, rate 0.90162). The gemini-flash alias
  row (158,883) was genuinely uncached (CPA cached=0) — untouched.

All other hours' zero-cache rows were verified genuinely uncached in CPA
(e.g. h10/h13/h14/h17/h18 cached=0 there too) — untouched.

Prices (pricing_cache.json): gemini-3.7-flash input 0.75, cached 0.075,
output 3.75 per 1M. TOU tier at both hours was peak rate=1.0, so base cost
equals recorded cost (no multiplier to undo).

Run inside the open-webui container:
    docker cp scripts/qk_gemini_cache_backfill_0825.py open-webui:/tmp/
    docker exec open-webui python3 /tmp/qk_gemini_cache_backfill_0825.py          # dry run
    docker exec open-webui python3 /tmp/qk_gemini_cache_backfill_0825.py --apply
"""
import json, os, shutil, sys, tempfile, fcntl

DATA = "/app/backend/data/quota_keeper"
LEDGER = os.path.join(DATA, "ledger.json")
RECENT = os.path.join(DATA, "recent.json")
DAY = "2026-08-25"
UID = "71477b94-a0c5-4ecb-ae19-8d4d940f024f"  # 谢邃韬
PRICE = {"input": 0.75, "cached": 0.075, "output": 3.75}

# hour -> {model: new (cached, input)}; output and request counts stay.
# h11: CPA exact sums for the 132 known cache-session rows (95 aistudio
#   gemini-flash-latest + 37 gemini gemini-3.7-flash-high /v1/messages):
#   cached 11,221,549 / input 12,309,104. QK bucket input is 12,623,112
#   (2 requests / 314k tokens more than CPA — likely a CPA-failed row and
#   stream-aborted retry); the unattributable residual stays in input
#   (conservative: billed at the higher input rate).
# h15: exact CPA sums (27 rows match QK 1:1 incl. input total).
HOUR_MODELS = {
    "11": {"gemini-3.7-flash": {"cached": 11221549.0, "input": None}},
    "15": {"gemini-3.7-flash": {"cached": 3583709.0, "input": 391082.0}},
}

# h15 per-request CPA rows (ts, input, output, cached) for recent.json repair.
# Matched on (input, output) — inputs strictly increase within the session.
RECENT_CPA_ROWS = [
    (140829, 281, 0), (140918, 85, 138176), (141035, 65, 138171),
    (141123, 58, 138166), (141204, 36, 138161), (141261, 41, 138155),
    (142465, 41, 138159), (143587, 39, 138162), (144394, 39, 142226),
    (145090, 39, 142225), (145982, 41, 142218), (146797, 41, 142218),
    (147382, 35, 142217), (147438, 39, 142212), (147495, 1659, 142206),
    (149203, 32, 146277), (149256, 43, 146272), (149320, 41, 146266),
    (149392, 41, 146261), (149464, 4261, 146256), (152242, 35, 146236),
    (152261, 40, 146262), (152319, 28, 146257), (153326, 38, 150322),
    (153385, 33, 150317), (153439, 681, 150311), (154184, 704, 0),
]
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
        u = (led.get("users") or {}).get(UID)
        if not u:
            print(f"user {UID} not found")
            return
        d = (u.get("days") or {}).get(DAY)
        if not d:
            print(f"day {DAY} not found for user")
            return

        day_cost_delta = day_cached_delta = day_input_delta = 0.0
        for hh, specs in HOUR_MODELS.items():
            hrec = (d.get("hours") or {}).get(hh)
            if not hrec:
                print(f"hour {hh} missing for user")
                continue
            for model, new in specs.items():
                mm = (hrec.get("models") or {}).get(model)
                if not mm:
                    print(f"hour {hh} model {model} missing")
                    continue
                t = mm.get("tokens") or {}
                prompt = (t.get("cached", 0) or 0) + (t.get("input", 0) or 0)
                if prompt <= 0:
                    continue
                new_cached = round(new["cached"], 1)
                if new.get("input") is not None:
                    new_input = round(new["input"], 1)
                else:
                    # h11: leave the unattributable residual in input
                    new_input = round(prompt - new_cached, 1)
                old_c, old_i = t.get("cached", 0), t.get("input", 0)
                if abs(new_cached - old_c) < 1 and abs(new_input - old_i) < 1:
                    print(f"hour {hh} [{model}] already correct, skip")
                    continue
                old_hc = mm.get("cost_usd", 0) or 0
                new_hc = cost({"cached": new_cached, "input": new_input,
                               "output": t.get("output", 0)})
                delta = new_hc - old_hc
                print(f"hour {hh} [{model}]: cached {old_c:.0f}->{new_cached:.0f} "
                      f"input {old_i:.0f}->{new_input:.0f} "
                      f"cost ${old_hc:.4f}->${new_hc:.4f} (delta ${delta:+.4f})")
                if not DRY:
                    t["cached"], t["input"] = new_cached, new_input
                    mm["cost_usd"] = new_hc
                    hrec["cost_usd"] = (hrec.get("cost_usd", 0) or 0) + delta
                    ht = hrec.get("tokens") or {}
                    ht["cached"] = (ht.get("cached", 0) or 0) + (new_cached - old_c)
                    ht["input"] = (ht.get("input", 0) or 0) + (new_input - old_i)
                    hrec["tokens"] = ht
                day_cost_delta += delta
                day_cached_delta += (new_cached - old_c)
                day_input_delta += (new_input - old_i)

        # day-model bucket (gemini-3.7-flash only; gemini-flash alias untouched)
        dm = (d.get("models") or {}).get("gemini-3.7-flash")
        if dm:
            dt_ = dm.get("tokens") or {}
            nc = dt_.get("cached", 0) + day_cached_delta
            ni = dt_.get("input", 0) + day_input_delta
            old_dc = dm.get("cost_usd", 0) or 0
            new_dc = old_dc + day_cost_delta
            print(f"day-model gemini-3.7-flash: cached {dt_.get('cached',0):.0f}->{nc:.0f} "
                  f"cost ${old_dc:.4f}->${new_dc:.4f} (delta ${day_cost_delta:+.4f})")
            if not DRY:
                dt_["cached"], dt_["input"] = nc, ni
                dm["cost_usd"] = new_dc

        # day bucket
        dt2 = d.get("tokens") or {}
        ndc = dt2.get("cached", 0) + day_cached_delta
        ndi = dt2.get("input", 0) + day_input_delta
        old_d2 = d.get("cost_usd", 0) or 0
        new_d2 = old_d2 + day_cost_delta
        print(f"day totals: cost ${old_d2:.4f}->${new_d2:.4f} "
              f"cached {dt2.get('cached',0):.0f}->{ndc:.0f} input {dt2.get('input',0):.0f}->{ndi:.0f}")
        if not DRY:
            dt2["cached"], dt2["input"] = ndc, ndi
            d["cost_usd"] = new_d2

        print(f"\nledger: total delta ${day_cost_delta:+.4f}, "
              f"cached +{day_cached_delta:.0f}, input {day_input_delta:+.0f}")

        # recent.json: per-request repair for the h15 session (27 entries
        # still in the ring buffer), matched to CPA rows on (input, output).
        # h11 entries have rolled off the buffer. Entry cost recomputed at
        # the same prices; TOU tier for h15 was peak rate=1.0 (no multiplier).
        rec = json.load(open(RECENT, encoding="utf-8"))
        items = rec.get("items") if isinstance(rec, dict) else rec
        lookup = {}
        for inp, out, cached in RECENT_CPA_ROWS:
            lookup[(inp, out)] = cached
        n_rec = 0
        rec_cost_old = rec_cost_new = 0.0
        for it in items or []:
            if it.get("user_id") != UID or it.get("model") != "gemini-3.7-flash":
                continue
            t = it.get("tokens") or {}
            key = (t.get("input"), t.get("output"))
            if key not in lookup:
                continue
            cached_new = float(lookup[key])
            if cached_new == t.get("cached", 0):
                continue
            new_input = max(0.0, t.get("input", 0) - cached_new)
            old_cost = it.get("cost_usd", 0) or 0
            new_cost = cost({"cached": cached_new, "input": new_input,
                             "output": t.get("output", 0)})
            print(f"recent ts={it.get('ts'):.0f}: cached {t.get('cached',0):.0f}->{cached_new:.0f} "
                  f"input {t.get('input',0):.0f}->{new_input:.0f} "
                  f"cost ${old_cost:.5f}->${new_cost:.5f}")
            if not DRY:
                t["cached"], t["input"] = cached_new, new_input
                it["cost_usd"] = new_cost
            n_rec += 1
            rec_cost_old += old_cost
            rec_cost_new += new_cost
        if n_rec:
            print(f"recent: {n_rec} entries repaired, cost ${rec_cost_old:.4f} -> ${rec_cost_new:.4f}")
        else:
            print("recent: no affected entries found")

        if not DRY:
            shutil.copy2(LEDGER, LEDGER + ".bak-cachefix4-20260825")
            shutil.copy2(RECENT, RECENT + ".bak-cachefix4-20260825")
            atomic_write(LEDGER, led)
            atomic_write(RECENT, rec)
            print("APPLIED (backups: *.bak-cachefix4-20260825)")
        else:
            print("DRY RUN — rerun with --apply to write")


if __name__ == "__main__":
    main()
