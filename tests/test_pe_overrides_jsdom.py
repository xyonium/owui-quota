# tests/test_pe_overrides_jsdom.py
"""Fix 1 regression: the pricing-overrides editor must save DOM-independently.

Root cause of the data loss: `collectOverrides()` read only the inputs that
are currently in the DOM. The editor paginates 50 rows/page and has a search
box, so a manual override on a non-current page (or filtered out) had its
`row.cur` state overwritten/never re-read and was silently dropped from the
POSTed overrides -- and `pricing.overrides` is replaced wholesale by the
save's deep merge, so the key disappeared from disk.

This runs the page's real JS (extracted from QK_PAGE) in jsdom, drives the
editor through its public DOM (fetch stub + event listeners), and asserts
the /config POST body preserves hidden rows.

Requires node + the repo's jsdom (node_modules/). Skipped when unavailable.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ADMIN = REPO / "quota_keeper_admin.py"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not NODE or not (REPO / "node_modules" / "jsdom").is_dir(),
    reason="node + jsdom required for the pricing-editor page test",
)


def _extract_page_js() -> str:
    """The page JS as PYTHON EVALUATES the QK_PAGE literal (escape processing
    applied) -- raw file slicing would miss that a source-level \\n inside a
    non-raw triple-quoted string becomes a real newline in the served HTML,
    which is a SyntaxError for JS (the v0.2.6 blank-page bug)."""
    import ast

    tree = ast.parse(ADMIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "QK_PAGE":
            page = node.value.value
            break
    else:
        raise AssertionError("QK_PAGE assignment not found")
    return page.split("<script>", 1)[1].split("</script>", 1)[0]


def test_served_page_js_parses():
    """node --check on the served page script. Any Python-eaten escape
    (\\n -> raw newline inside a JS regex/string) blanks the whole page in
    real browsers while raw-slicing harnesses stay green -- pin it here."""
    import subprocess
    import tempfile

    js = _extract_page_js()
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    r = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_overrides_save_preserves_non_visible_rows():
    # qk_build_page substitutes the API prefix at mount; mirror it so the page
    # JS fetches real-looking URLs in the harness.
    page_js = _extract_page_js().replace("__QK_API_PREFIX__", "/api/v1/quota-keeper")

    # Fixture mirrors GET /models (v0.3.0 editor data source): 3 used models.
    # The search filter hides bbb (existing override on disk, wrapped form)
    # and ccc (baseline row) while aaa is edited -- the save must preserve
    # hidden rows and not drop the hidden override.
    data = {
        "models": {
            "pricing_fetched": True,
            "items": [
                {"model": "zebra/model-aaa", "used": True, "available": True,
                 "requests": 3, "unpriced_requests": 0, "cost_usd": 0.1,
                 "matched": True, "how": "exact:zebra/model-aaa",
                 "price": {"input": 1.0, "output": 2.0}, "override": None},
                {"model": "zebra/model-bbb", "used": True, "available": True,
                 "requests": 5, "unpriced_requests": 0, "cost_usd": 0.2,
                 "matched": True, "how": "override:zebra/model-bbb",
                 "price": {"input": 9.0, "output": 9.0},
                 "override": {"prices": {"input": 9.0, "output": 9.0}}},
                {"model": "zebra/model-ccc", "used": True, "available": True,
                 "requests": 1, "unpriced_requests": 0, "cost_usd": 0.0,
                 "matched": True, "how": "exact:zebra/model-ccc",
                 "price": {"input": 1.0, "output": 2.0}, "override": None},
            ],
        },
        "cfg": {
            "credits_per_usd": 1000.0,
            "quota_period": "daily",
            "default_quota_credits": None,
            "user_quotas": {},
            "group_quotas": {},
            "schedule": {"timezone": None, "night_start_hour": 22, "night_end_hour": 8,
                         "night_multiplier": 1.0, "weekend_multiplier": 1.0},
            "pricing": {"url": "http://x", "refresh_hours": 24, "default_pricing": None,
                        "overrides": {"zebra/model-bbb": {"prices": {"input": 9.0, "output": 9.0}}}},
            "tou": {"enabled": False, "timezone": None, "tiers": {}, "holidays": [],
                    "default_policy": "off", "providers": {}, "models": {}},
        },
        "stats": {"kpi": {"requests": 0, "cost_usd": 0, "tokens": {},
                          "unpriced_requests": 0},
                  "series": [], "users": [], "models": []},
    }

    fake_src = (
        "window.FAKE = {models:" + json.dumps(data["models"])
        + ",cfg:" + json.dumps(data["cfg"])
        + ",stats:" + json.dumps(data["stats"]) + ",posted:null};\n"
        + "window.__qk_fake_fetch__ = (path, opts) => {\n"
        + "  opts = opts || {};\n"
        + "  const url = String(path);\n"
        + "  const m = (opts.method || 'GET').toUpperCase();\n"
        + "  const j = v => ({json: () => v, ok: true});\n"
        + "  if (m === 'POST' && url.endsWith('/config')) { window.FAKE.posted = JSON.parse(opts.body); return Promise.resolve(j({ok:true})); }\n"
        + "  if (url.endsWith('/me')) return Promise.resolve(j({user:{id:'u1',name:'Admin',email:'a@x',role:'admin'}}));\n"
        + "  if (url.endsWith('/models')) return Promise.resolve(j(window.FAKE.models));\n"
        + "  if (url.endsWith('/config')) return Promise.resolve(j(window.FAKE.cfg));\n"
        + "  if (url.endsWith('/users')) return Promise.resolve(j([]));\n"
        + "  if (url.endsWith('/groups')) return Promise.resolve(j([]));\n"
        + "  if (url.endsWith('/pricing')) return Promise.resolve(j({url:'http://x',models:3,fetched_at_iso:'2026-08-17T00:00:00+00:00'}));\n"
        + "  if (url.indexOf('/stats') >= 0) return Promise.resolve(j(window.FAKE.stats));\n"
        + "  throw new Error('unexpected fetch: ' + m + ' ' + url);\n"
        + "};\n"
    )
    # Page JS runs inside ONE window eval together with the fixture and the
    # driver: the page's top-level `const STATE` is a lexical binding of that
    # eval scope, invisible to later evals or the node realm.
    driver = r'''
(async () => {
  try {
    await new Promise(r => setTimeout(r, 40)); // let init() + loadAdmin() finish
    // open the pricing editor -> the toggle listener triggers loadPricingFull()
    $('secPricingEditor').open = true;
    $('secPricingEditor').dispatchEvent(new window.Event('toggle'));
    await new Promise(r => setTimeout(r, 40));
    // filter the table down to ONE visible row: the other two rows exist in
    // STATE.pe.orig but are absent from the DOM (the 50/page pagination and
    // the search box both produce exactly this shape)
    $('peSearch').value = 'aaa';
    peSearch();
    const inputs = document.querySelectorAll('input[data-pk]');
    if (!inputs.length) throw new Error('no editor rows rendered');
    const pk = [...inputs].map(i => i.dataset.pk);
    if (!(pk.length === 6 && pk.every(p => p === 'zebra/model-aaa')))
      throw new Error('expected only the visible zebra/model-aaa row, got ' + pk.join(','));
    // edit the visible row through its input. jsdom in outside-only mode does
    // not execute inline attribute handlers (oninput="peEdit(this)"), so the
    // harness calls the handler directly — in a real browser the attribute
    // does the same thing.
    const aaa = [...inputs].find(i => i.dataset.f === 'input');
    aaa.value = '5.5';
    peEdit(aaa);
    // bbb: existing override on disk (9.0/9.0), hidden by the search filter,
    // never re-edited this session -> must be preserved unchanged.
    // ccc: baseline row (no override on disk), hidden, untouched; also give
    // it an explicit all-empty cur (simulating the user clearing the fields)
    // -> must NOT be emitted.
    const rc = STATE.pe.orig['zebra/model-ccc'];
    rc.cur = {prices:{input:null,cached:null,cache_write:null,output:null},alias:'',mult:''};
    // alias scenario: give bbb an alias, save, then (fresh load, untouched
    // rows) save again -- the second save must NOT wipe the alias with a
    // direct-prices override (the v0.3.0 bug: upstream-priced rows were
    // re-emitted because undefined !== 1.0 on missing fields)
    STATE.pe.orig['zebra/model-bbb'].cur = {prices:{input:null,cached:null,cache_write:null,output:null},alias:'kimi-k3',mult:0.5};
    saveConfig();
    await new Promise(r2 => setTimeout(r2, 60));
    const ov1 = (window.FAKE.posted.pricing || {}).overrides || {};
    if (!(ov1['zebra/model-bbb'] && ov1['zebra/model-bbb'].alias === 'kimi-k3'))
      throw new Error('alias not emitted: ' + JSON.stringify(ov1));
    // simulate the post-save state: cfg now holds the alias AND /models
    // reports it (the live round-trip the editor sees after a real save),
    // then re-save WITHOUT touching anything
    window.FAKE.cfg.pricing.overrides = ov1;
    window.FAKE.models.items.find(it => it.model === 'zebra/model-bbb').override = ov1['zebra/model-bbb'];
    STATE.cfg = window.FAKE.cfg;
    STATE.pe.data = window.FAKE.models;
    rebuildPeOrig();
    saveConfig();
    await new Promise(r2 => setTimeout(r2, 60));
    const ov2 = (window.FAKE.posted.pricing || {}).overrides || {};
    if (!(ov2['zebra/model-bbb'] && ov2['zebra/model-bbb'].alias === 'kimi-k3'))
      throw new Error('alias wiped on second save: ' + JSON.stringify(ov2));
    if (ov2['zebra/model-ccc'] || ov2['zebra/model-aaa'] && ov2['zebra/model-aaa'].prices === undefined)
      throw new Error('untouched rows emitted: ' + JSON.stringify(ov2));
    window.__result = 'OK overrides=' + JSON.stringify(ov2);
    return;
    await new Promise(r2 => setTimeout(r2, 60));
    const body = window.FAKE.posted;
    if (!body) throw new Error('no /config POST recorded');
    const ov = (body.pricing || {}).overrides || {};
    // assertions (v0.3.0 emission shapes):
    //  aaa: edited while visible -> {prices:{input:5.5, output:2.0, ...}}
    //  bbb: hidden, untouched, existing override -> preserved unchanged
    //  ccc: all-empty, no existing override -> dropped
    const pa = ov['zebra/model-aaa'] && ov['zebra/model-aaa'].prices || {};
    const pb = ov['zebra/model-bbb'] && ov['zebra/model-bbb'].prices || {};
    const aaaOk = pa.input === 5.5 && pa.output === 2.0;
    const bbbOk = pb.input === 9.0 && pb.output === 9.0;
    const cccOk = !('zebra/model-ccc' in ov);
    if (!(aaaOk && bbbOk && cccOk))
      throw new Error('bad overrides: ' + JSON.stringify(ov));
    window.__result = 'OK overrides=' + JSON.stringify(ov);
  } catch (e) {
    window.__result = 'FAIL ' + (e && e.stack || e);
  }
})();
'''
    main_js = """'use strict';
const fs = require('fs');
const { JSDOM } = require('jsdom');
const pageJs = fs.readFileSync(process.argv[2], 'utf8');
const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/quota', runScripts: 'outside-only',
  beforeParse(window) {
    window.fetch = (p, o) => window.__qk_fake_fetch__(p, o);
    window.confirm = () => true;
  },
});
const w = dom.window;
const doc = w.document;
const add = (id, tag, inner) => { const el = doc.createElement(tag); el.id = id; if (inner) el.innerHTML = inner; doc.body.appendChild(el); return el; };
// sections / details the init path touches
add('meta', 'span');
for (const id of ['secDash','secUsers','secModels','secRecent','secGeneral','secSchedule',
                  'secPricing','secGroups','secUserq','secPricingEditor','secTou','secPersonal'])
  add(id, 'div');
add('userq', 'table', '<tbody></tbody>');
add('kpis', 'div'); add('trend', 'div'); add('trendLegend', 'div');
add('uRank', 'table', '<tbody></tbody>'); add('modelsT', 'table', '<tbody></tbody>');
add('recentT', 'table', '<tbody></tbody>'); add('qUser', 'input');
add('groups', 'table', '<tbody></tbody>');
add('toast', 'div');
add('peSearch', 'input');
add('peOnlyUnpriced', 'input');
add('peRows', 'table', '<tbody></tbody>');
add('pePage', 'span');
add('btnSave', 'button');
add('btnRefresh', 'button');
add('pricing_url', 'input'); add('refresh_hours', 'input');
add('default_pricing', 'input'); add('matchTest', 'input'); add('matchResult', 'span');
add('credits_per_usd', 'input'); add('quota_period', 'input'); add('default_quota_credits', 'input');
add('schedule_timezone', 'input'); add('night_start_hour', 'input'); add('night_end_hour', 'input');
add('night_multiplier', 'input'); add('weekend_multiplier', 'input');
add('touEnabled', 'input'); add('touTz', 'input'); add('touPolicy', 'select');
add('trate_peak', 'input'); add('trate_offpeak', 'input'); add('trate_normal', 'input');
add('wins_peak', 'div'); add('wins_offpeak', 'div'); add('wins_normal', 'div');
add('provs', 'div'); add('toumodels', 'div'); add('holidays', 'div');
add('holYear', 'input'); add('holCountry', 'input');
add('fUser', 'input'); add('fModel', 'select'); add('customDates', 'div');
add('spanFrom', 'input'); add('spanTo', 'input');
doc.body.insertAdjacentHTML('beforeend',
  '<div class="spans"><button data-span="7d"></button></div>');
// ONE window eval: fixture + fake fetch + page script + driver share the
// same lexical scope (const STATE etc. are invisible to later evals)
w.eval(__PAGE_SRC__);
const t0 = Date.now();
(async () => {
  while (!w.__result) {
    if (Date.now() - t0 > 15000) { console.error('TIMEOUT'); process.exit(1); }
    await new Promise(r => setTimeout(r, 25));
  }
  console.log(w.__result);
  if (String(w.__result).startsWith('FAIL')) process.exit(1);
  process.exit(0);
})();
"""
    main_js = main_js.replace("__PAGE_SRC__", json.dumps(fake_src + "\n" + page_js + "\n" + driver))

    node_src = REPO / "tests" / "_pe_jsdom_main.js"
    node_src.write_text(main_js, encoding="utf-8")
    try:
        page_js_file = REPO / "tests" / "_pe_page.js"
        page_js_file.write_text(page_js, encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(node_src), str(page_js_file)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(
                "jsdom overrides harness failed:\n"
                + proc.stdout
                + "\n"
                + proc.stderr
            )
        assert "OK overrides=" in proc.stdout
    finally:
        node_src.unlink(missing_ok=True)
        page_js_file.unlink(missing_ok=True)
