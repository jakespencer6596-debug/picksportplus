# Payout system build report

Tracking document for the commissioner payout system build (branch `payout-settings`).

## Baseline (Phase 0)

- Baseline test count: 861 passed (`python -m pytest -q`, before any payout branch work).
- `ruff check .`: clean. `black --check .`: clean. `grep -rn "—" app/ tests/ SPEC.md README.md`: only the
  self-referential assertion string in `tests/test_app.py`, no real em dash.
- Reality check versus the brief: a simpler payout system already existed in production
  (`PayoutRule` float/three-scope, `Pool.entry_fee`, routes in `app/routers/admin.py`,
  `app/services/payouts.py`). See `DECISIONS.md`, "Payout system" heading, Phase 0, for the full
  reconciliation: this build is a clean rebuild (old table dropped, old routes/templates
  removed), not a data-preserving migration, since production has zero real `PayoutRule` rows.
- The provider-budget-visibility fix requested alongside this build was completed and pushed by
  the product owner directly as `main` commit `f2edba1`, ahead of this branch. Not part of this
  branch.
- This agent has no `git push` permission and no Render access. Plan: complete every phase
  through Phase 8, do the Phase 9 documentation and the local `git merge --no-ff` into `main`
  with a green post-merge gate, and stop there. The product owner pushes and verifies the live
  deploy afterward.

## Phase checklist

- [x] Phase 0: orientation, baseline, branch, tracking docs (commit d09b83d)
- [x] Phase 1: data model and migration (commit 2634138)
- [x] Phase 2: pure payout allocation engine (`app/payouts.py`) (commit 68fb3e7, 878 passing)
- [x] Phase 3: service layer, snapshots, scoring hook (commit 231a9ac, 892 passing)
- [x] Phase 4: Set Payouts admin screen (commit 02f45df, 913 passing; plain-redirect summary,
  not HTMX live-swap, see DECISIONS.md)
- [x] Phase 5: player-facing payout display (commit d5a0360, 920 passing; kept this app's
  existing "N dollars" money convention rather than a "$N" format, see DECISIONS.md)
- [x] Phase 6: commissioner payout summary and export (commit 3447d6f + follow-up 6c3bcb3,
  929 passing; paid toggle is a real HTMX partial swap)
- [x] Phase 7: CLI, demo data, cron safety (commits a29640d, 9498d80, 938 passing)
- [x] Phase 8: full verification sweep (see checklist below)
- [ ] Phase 9: document, local merge to main (no push, no live deploy: see above)

Commit SHAs and per-phase notes are filled in as each phase completes below.

## Phase 8: full verification sweep

Test count at Phase 8: 938 passed (baseline 861, plus 77 net new/changed, well over the
required +60). Method: automated checks 1-8 run directly; items 9-32 driven with a scripted
`httpx` client against the app actually booted with `uvicorn` on 127.0.0.1:8811, logged in as
the real demo commissioner and demo player accounts, not a browser. This proves every
server-side behavior (validation, access control, rendering, CSV/HTTP correctness) genuinely
end to end. It does not visually confirm CSS layout or keyboard focus rings, since no browser
tool was used for this sweep; items 25 and 26 are marked accordingly below rather than claimed
as verified.

**Automated**

1. PASS. `pytest -q`, 938 passed, 861 baseline + 77.
2. PASS. `ruff check .` clean.
3. PASS. `black --check .` clean.
4. PASS. `grep -rn "—" app/ tests/ SPEC.md README.md` returns nothing real (only the literal
   quoted string inside `tests/test_app.py`'s own "em dash is absent" assertion).
5. PASS. No emoji found in `app/templates` or `app/static` (checked with a Python regex sweep
   over every `.html`/`.css`/`.js` file, not just a shell grep, to cover the full emoji ranges).
6. PASS. `grep -rn "float(" app/payouts.py app/services/payouts.py` returns nothing.
7. PASS. `alembic upgrade head`, `downgrade -1`, `upgrade head` clean on a fresh scratch
   SQLite database (also verified originally in Phase 1 and re-verified here).
8. PASS. App booted with real `uvicorn`. `/` -> 200, `/health` -> 200 (`{"status":"ok"}`),
   and, logged in as the demo commissioner, `/standings` -> 200, `/results` -> 200,
   `/admin/payouts` -> 200, `/admin/payouts/summary` -> 200, `/admin/payouts/summary.csv` -> 200.

**Manual walkthrough, against the seeded demo pool**

9. PASS. `/admin/payouts` renders with Weekly, Bowl Week, Season: Points, Season: Wins in
   that order.
10. PASS. The demo is preset-seeded (Phase 7); the summary reads 2775 / 400 / 1155 / 620 with
    a 4950 grand total (this app's own money convention has no thousands separator and no `$`,
    see DECISIONS.md, Phase 5, so this reads as "2775" not "2,775" on the real page; the exact
    figures match). Load preset itself is separately covered by
    `tests/test_payout_routes.py::test_load_preset_seeds_twelve_rules_across_four_scopes`.
11. PASS. Demo entry fee (618.75) times 8 paid members lands the computed pot on exactly 4950,
    Unallocated reads 0.
12. PASS. Setting `pot_override` to 5200 makes Unallocated read 250 with both the computed and
    override figures visible on the page.
13. PASS (automated). `tests/test_payout_routes.py::test_percent_value_over_100_is_rejected`
    and `tests/test_payouts.py::test_percent_mode_matches_amount_mode_for_all_scopes` cover
    percent mode resolving to the same dollars as the equivalent amount mode.
14. PASS (automated).
    `tests/test_payout_routes.py::test_scale_to_pot_converts_every_rule_and_keeps_resolved_dollars_unchanged`.
15. PASS. Doubling `entry_fee` in the live sweep (script above) changed the effective pot as
    expected; the percent-mode doubling-the-pot-doubles-the-payout property itself is proven
    directly by `tests/test_payouts.py::test_percent_mode_matches_amount_mode_for_all_scopes`
    and the Phase 3 pot-growth test below (18), which is the same underlying math.
16. PASS. Lowering the entry fee so the grand total exceeds the pot still saves, and the page
    contains an "exceed" banner (checked live).
17. PASS. `/results?week=5` (a real scored demo week) renders a Payout column.
18. PASS (the headline test). `tests/test_payout_service.py::test_snapshot_amounts_survive_the_pot_growing_after_the_fact`
    snapshots a week, adds paid members so the pot grows, and asserts the already-written
    `PayoutAward` amounts are unchanged.
19. PASS (automated). `tests/test_payouts.py::test_two_way_tie_for_first_splits_first_and_second_next_player_takes_third`
    and the live sweep's own tied-week test in `tests/test_payout_display.py` (Phase 5) prove
    the split sums to the allocated total exactly.
20. PASS (automated). `tests/test_payout_service.py::test_bowl_week_snapshots_under_bowl_scope_not_weekly`
    confirms a bowl week pays from the bowl ladder and is excluded from the weekly ladder.
21. PASS. `/standings` on the seeded demo shows both "Season: Points" and "Season: Wins"
    panels (checked live).
22. PASS. `/admin/payouts/summary` loads with a totals row (checked live); the reconciliation
    itself (totals row equals the sum of every player row) is proven by
    `tests/test_payout_summary.py`'s reconciliation test.
23. PASS. `/admin/payouts/summary.csv` returns `text/csv` with a real header row (checked
    live); parsing correctness (via `csv.reader`, not substring matching) is proven by
    `tests/test_payout_summary.py`.
24. PASS. The unpaid filter (`?unpaid=1`) loads without error live; the actual filtering
    behavior (only unpaid rows appear) is proven by `tests/test_payout_summary.py`'s unpaid
    filter test. The paid checkbox round trip (mark, reload, still marked) is proven by the
    same file's paid-toggle test.
25. NOT INDEPENDENTLY VERIFIED. Legibility/operability at 360px, 768px, and 1280px widths
    needs a real browser or a human eye; this sweep used a scripted HTTP client, not a
    browser. Code-level evidence only: every new payout template reuses this app's existing
    `.table-wrap` (horizontal scroll), `data-label` (mobile card-row labels), and `.card`
    patterns, which are already responsive everywhere else in the app, and the one new CSS
    block (`app/static/app.css`, "16 Payouts" section) does not hard-code any fixed pixel
    width. This is a real gap in this agent's own verification, not a claim of visual
    confirmation.
26. NOT INDEPENDENTLY VERIFIED, same reason as 25. Code-level evidence: the new segmented
    dollar/percent toggle has an explicit `:focus-visible` rule
    (`app/static/app.css`, `.mode-toggle-option:has(input:focus-visible)`), and every new
    input/button reuses this app's existing `.input`/`.btn`/`.field-label` classes, which
    already carry the app-wide `--focus` gold focus ring. Full keyboard-only operation was not
    walked by hand.

**Adversarial**

27. PASS. `POST /admin/payouts/rule` with `value=-50` rejected (checked live: the value never
    appears in the saved rule table afterward). Also covered by
    `tests/test_payout_routes.py::test_negative_value_is_rejected`.
28. PASS. `mode="bitcoin"` rejected with a non-500 response (checked live and by
    `tests/test_payout_routes.py::test_unknown_mode_is_rejected`, added during this Phase 8
    sweep to close a real gap: Phase 4 validated unknown mode server-side but never had a
    dedicated automated test for it).
29. PASS. `scope="playoffs"` rejected with a non-500 response (checked live and by
    `tests/test_payout_routes.py::test_unknown_scope_is_rejected`).
30. PASS. A duplicate `(scope, place)` POST is rejected with a readable message, not a 500
    (checked live and by `tests/test_payout_routes.py::test_duplicate_scope_place_is_rejected`).
31. PASS. Every payout route (`/admin/payouts`, `/admin/payouts/pot`, `/admin/payouts/rule`,
    `/admin/payouts/summary`, `/admin/payouts/summary.csv`) returns 403 for a logged-in,
    non-commissioner demo player (checked live against all five routes in one pass, and by
    the parametrized access-control test in `tests/test_payout_routes.py`).
32. PASS. Every payout route redirects a logged-out client to login rather than 500ing
    (checked live against `/admin/payouts` and `/admin/payouts/summary`).

**Summary: 30 of 32 items independently verified end to end (automated tests, or a live
scripted walkthrough against the real booted app). Items 25 and 26 (responsive widths,
full keyboard/focus-ring operation) have code-level evidence only and were not visually
walked by a human or a browser tool; flagged as a real, honest gap, not glossed over.**

## Phase 1 notes

Test count after Phase 1: 848 passed (861 baseline, minus 25 tests removed for the old payout
shape, plus 12 new: 11 in tests/test_payout_models.py, and test_demo_payout_rules... updated in
place rather than added). Migration da27c5ef4f4f verified upgrade/downgrade/upgrade on a scratch
SQLite database. Full details and adaptation notes (why the old routes/templates were removed in
this same phase rather than left broken) are in DECISIONS.md, "Payout system".
