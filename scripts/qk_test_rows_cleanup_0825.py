#!/usr/bin/env python3
"""One-off cleanup: remove the two test requests sent from the 3705809b
API key to prx.gemini-flash-search on 2026-08-25 18:40/18:41 (live
verification of the gateway cache fix). They were recorded unpriced
(cost 0) but pollute request counts / unpriced stats for hour 18.

Run inside the open-webui container:
    docker cp scripts/qk_test_rows_cleanup_0825.py open-webui:/tmp/
    docker exec open-webui python3 /tmp/qk_test_rows_cleanup_0825.py          # dry run
    docker exec open-webui python3 /tmp/qk_test_rows_cleanup_0825.py --apply
"""
import json, os, shutil, sys, tempfile, fcntl

DATA = "/app/backend/data/quota_keeper"
LEDGER = os.path.join(DATA, "ledger.json")
RECENT = os.path.join(DATA, "recent.json")
DAY = "2026-08-25"
UID = "3705809b-2508-413e-acec-8f42176cee07"
MODEL = "gemini-flash-search"
HOUR = "18"
# recent.json entries: ts 1787639950.6506674? no — those were webui. The test
# rows were ts 1787640053 (18:40:53) and 1787640078 (18:41:18), model
# gemini-flash-search, channel api. Remove by (ts, model) match.
RECENT_TS = {1787640053.0, 1787640078.0}  # filled below from the file itself
DRY = "--apply" not in sys.argv


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
        d = (u.get("days") or {}).get(DAY)
        hrec = (d.get("hours") or {}).get(HOUR)
        mm = (hrec.get("models") or {}).get(MODEL) if hrec else None
        dm = (d.get("models") or {}).get(MODEL)
        if not mm:
            print("nothing to clean (hour bucket already gone)")
            return

        t = mm.get("tokens") or {}
        reqs = mm.get("requests", 0)
        cost = mm.get("cost_usd", 0) or 0
        unpriced = dm.get("unpriced_requests", 0) if dm else 0
        print(f"removing: {reqs} reqs {MODEL} h{HOUR}, tokens {t}, cost {cost}, "
              f"unpriced {unpriced}")

        if not DRY:
            # hour bucket: this hour held only these two requests
            if set(hrec.get("models", {}).keys()) == {MODEL}:
                d["hours"].pop(HOUR, None)
            else:
                hrec["models"].pop(MODEL, None)
                hrec["requests"] = (hrec.get("requests", 0) or 0) - reqs
                hrec["cost_usd"] = (hrec.get("cost_usd", 0) or 0) - cost
                ht = hrec.get("tokens") or {}
                for k in ("cached", "input", "output"):
                    ht[k] = (ht.get(k, 0) or 0) - t.get(k, 0)
                ch = hrec.get("channels") or {}
                ch["api"] = max(0, ch.get("api", 0) - reqs)
            # day-model
            d["models"].pop(MODEL, None)
            # day totals
            d["requests"] = max(0, (d.get("requests", 0) or 0) - reqs)
            d["cost_usd"] = (d.get("cost_usd", 0) or 0) - cost
            dt = d.get("tokens") or {}
            for k in ("cached", "input", "output"):
                dt[k] = (dt.get(k, 0) or 0) - t.get(k, 0)
            dch = d.get("channels") or {}
            dch["api"] = max(0, dch.get("api", 0) - reqs)

        # recent.json: drop the two test entries (18:40/18:41 gemini-flash-search)
        rec = json.load(open(RECENT, encoding="utf-8"))
        items = rec.get("items") if isinstance(rec, dict) else rec
        removed = [it for it in items or [] if
                   it.get("model") == MODEL and it.get("user_id") == UID
                   and it.get("channel") == "api"
                   and float(it.get("ts") or 0) >= 1787640000.0]
        print(f"recent entries to remove: {len(removed)}")
        for it in removed:
            print(f"  ts={it.get('ts')} tokens={it.get('tokens')}")
        if not DRY and removed:
            keep = [it for it in items if it not in removed]
            if isinstance(rec, dict):
                rec["items"] = keep
            else:
                rec = keep

        if not DRY:
            shutil.copy2(LEDGER, LEDGER + ".bak-testcleanup-20260825")
            shutil.copy2(RECENT, RECENT + ".bak-testcleanup-20260825")
            atomic_write(LEDGER, led)
            atomic_write(RECENT, rec)
            print("APPLIED (backups: *.bak-testcleanup-20260825)")
        else:
            print("DRY RUN — rerun with --apply to write")


if __name__ == "__main__":
    main()
