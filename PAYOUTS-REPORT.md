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
- [ ] Phase 5: player-facing payout display
- [ ] Phase 6: commissioner payout summary and export
- [ ] Phase 7: CLI, demo data, cron safety
- [ ] Phase 8: full verification sweep
- [ ] Phase 9: document, local merge to main (no push, no live deploy: see above)

Commit SHAs and per-phase notes are filled in as each phase completes below.

## Phase 1 notes

Test count after Phase 1: 848 passed (861 baseline, minus 25 tests removed for the old payout
shape, plus 12 new: 11 in tests/test_payout_models.py, and test_demo_payout_rules... updated in
place rather than added). Migration da27c5ef4f4f verified upgrade/downgrade/upgrade on a scratch
SQLite database. Full details and adaptation notes (why the old routes/templates were removed in
this same phase rather than left broken) are in DECISIONS.md, "Payout system".
