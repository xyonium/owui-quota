# Sticky table headers — design

Date: 2026-08-22 · Status: approved · Scope: `quota_keeper_admin.py` `QK_PAGE` CSS only

## Problem

The dashboard has 7 tables wrapped in `.scroll{overflow:auto;max-height:440px}`
containers. Their `<thead>` rows are plain `th` cells with no sticky rule, so the
header scrolls away with the body. On long tables (users ranking, models, recent,
pricing editor) the column meaning is lost once you scroll.

## Current state

- `.scroll{overflow:auto;max-height:440px}` (QK_PAGE ~line 1978).
- 7 scrollable tables, all `.scroll > table > thead > tr > th`:
  `uRank` (users), `modelsT` (models), `pricePoolT` (price pool), `recentT`
  (recent), `groups`, `userq` (user quotas), `peRows` (pricing editor).
- `th{...}` base style exists; no `position:sticky` on any `th`.
- The drill-down detail table (JS-generated, in a `tr.detail` row) is NOT inside
  `.scroll` and does not scroll — out of scope.

## Decision (user-confirmed)

- Apply to **the 7 `.scroll` tables only**.
- Header background: **opaque `--card`**, so scrolled rows are fully covered (no
  text bleed-through).

## Implementation

Add one rule next to `.scroll`:

```css
/* sticky column headers inside every scrollable table */
.scroll thead th{position:sticky;top:0;z-index:2;background:var(--card);
  box-shadow:0 1px 0 var(--line)}
```

Rationale / edge cases:

- `position:sticky;top:0` on `th` (not `thead`) — the reliable cross-browser
  pattern for tables; `thead` itself is not a positioned box.
- `background:var(--card)` (opaque) covers rows scrolling underneath.
- `box-shadow:0 1px 0 var(--line)` re-creates the header separator. Under
  `border-collapse:collapse` (the table default here) a `th` `border-bottom` does
  NOT stick with the cell when sticky — it scrolls away, gluing the header to the
  first row. `box-shadow` stays with the painted cell.
- `z-index:2` keeps the header above body rows (incl. the `tr.detail` drill rows
  that carry a translucent background) but below the page `header` (z-index 9) and
  toast (99).

No HTML/JS changes. No sticky first column (tables are narrow; horizontal scroll
need is low). Out of scope per YAGNI.

## Testing

Browser rendering can't be asserted in the headless harness, so pin the CSS
declaratively in `tests/test_smoke.py`: AST-extract the real `QK_PAGE` literal
(same technique as `test_pe_overrides_jsdom._extract_page_js`) and assert the
served page contains a `.scroll thead th` rule with `position:sticky`, `top:0`,
an opaque background, and `z-index`. Guards against a future edit silently
dropping the rule.

## Rollback

Pure additive CSS; removing the rule restores prior behavior. No data/config
impact.
