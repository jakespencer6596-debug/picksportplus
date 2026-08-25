# Tab entry and season tiebreak. Report

Tracking document for the "Tab entry and season wins tiebreak" work (branch
`tab-and-tiebreak`). Filled in as each phase lands; the final version replaces this
scaffold before the branch merges to `main`.

## Baseline (Phase 0)

- Test count before this branch's own new tests: **1090 passed, 0 failed** (`pytest -q`,
  full suite, `C:\dev\PickSportPlus`, 2026-08-24).
- `ruff check .`: clean.
- `black --check .`: clean.
- Em dash check (`grep -rn "—" app/ tests/ SPEC.md README.md`): only
  `tests/test_app.py`'s own assertion that the string never appears in rendered output
  (the check target itself), nothing real.

## Phase checklist

- [x] Phase 0. Baseline, branch, this report's scaffold. `25f8c2c`
- [x] Phase 1. Tab and arrow keys move between confidence values. `9b9ddd3`
- [x] Phase 2. Season ranking breaks ties on total wins. `bd70095`
- [x] Phase 3. Define season submission time. `bd70095` (landed together with Phase 2,
      one coherent ranking implementation; Phase 3's own tests and DECISIONS.md entry are
      both present and separately identifiable, see below)
- [x] Phase 4. Regression sweep. `pending commit`
- [ ] Phase 5. Full verification.
- [ ] Phase 6. Documentation.
- [ ] Phase 7. Merge, push, deploy, verify.

## Ambiguities resolved (full detail in `DECISIONS.md`, "Tab entry and season tiebreak")

1. **Phase 1 front end test harness.** No JS tests existed before this branch. Added the
   minimum: `package.json` + `jsdom` as the sole dev dependency, run via Node's own
   `node --test`, no bundler, no framework, `node_modules/` gitignored.
2. **Phase 1, Tab off the last (disabled) confidence input.** Redirect to Lock picks only
   when it is not disabled; otherwise fall through to normal document order rather than
   stranding focus on an unusable control.
3. **Phase 2, Season Wins ladder's points tiebreak direction.** Mirrors the pool's own
   scoring direction (matching the points ladder), not a hard coded "fewer always wins":
   the spec's own worked example cannot distinguish the two readings since it is written
   against this pool's default `inverse` mode, and a hard coded reading would silently
   invert under `standard` mode.
4. **Phase 2, the rule statement's literal text only renders when
   `season_tiebreak_mode == "wins"`,** since it would otherwise state a rule that is false
   for a pool switched to `"split"`.
5. **Phase 3, season submission time = the final scored week's timestamp,** with mean
   submission time and count-of-weeks-first considered and rejected (both documented with
   reasoning in `DECISIONS.md`).

## Phase 4. Regression sweep

Every item below that touches code this branch did not change is verified by the full
suite (1106/1106) staying green with no test weakened, skipped, or deleted; item 11 also
got a new, dedicated test (`test_a_test_weeks_win_never_decides_the_season_wins_tiebreak`,
`tests/test_standings.py`) since it is the one regression risk this branch's own change
could plausibly introduce.

1.  Inverse scoring lowest-total-wins and no-show max penalty. **Pass**, `app/scoring.py`
    untouched, existing suite green.
2.  15 of 20 pick-count validation (14 and 16 rejected, 15 saves). **Pass**,
    `validate_picks` untouched, existing suite green.
3.  Two-step pick entry (numeric input, Reorder to inputs, drag, Lock) still works with Tab
    layered on. **Pass**, new JS suite (12/12) plus the existing `test_app.py` picks-route
    tests (21/21) green; full interactive/keyboard verification is Phase 5.
4.  Player-major results grid. **Pass**, `app/routers/results.py`/`results.html` untouched.
5.  Payout ladder totals 2775/400/1155/620/4950. **Pass**, `app/payouts.py`'s pure engine
    untouched, `tests/test_payouts.py` green.
6.  Payout snapshots do not move when the pot changes. **Pass**, `snapshot_awards`
    untouched.
7.  Weekly ties still split and reconcile. **Pass**, plus a new dedicated regression test,
    `test_weekly_ties_still_split_under_the_default_season_tiebreak_mode`
    (`tests/test_payout_service.py`).
8.  Scenarios: panel gating, percentages, leverage, same ranking direction as standings.
    **Pass**, `app/scenarios.py` is a pure module with no import of
    `app/services/standings.py` at all, untouched; existing scenario suite green.
9.  Season and Weekly tabs stay separate, every table still sorts. **Pass** for the
    existing tables (untouched markup/JS); the new Season: Wins table reuses the identical,
    already-generic `data-sortable` machinery. Live check in Phase 5.
10. Week resolution span and duplicate-team guard. **Pass**, `app/slate.py`/
    `app/services/ingest.py` untouched.
11. Test weeks contribute nothing to season standings/payouts, **including the wins
    tiebreak**. **Pass**, verified by the pre-existing test plus the new dedicated one
    above: a test week win cannot leak into `weekly_wins` (the season aggregate already
    excludes `is_test_week` rows) so it can never decide a tiebreak either.
12. No "admin" wording on commissioner pages, `/site` 403s for a commissioner.
    **Pass**, `app/auth.py`/`app/routers/site*.py` untouched.
13. Email sends and fails loudly when disabled. **Pass**, `app/services/mail.py`
    untouched.

Nothing failed. Nothing needed a fix in this phase.

Full gate at the end of Phase 4: `ruff check .` clean, `black --check .` clean, em dash
scan clean, `pytest -q` **1106 passed, 0 failed** (+16 over baseline), `node --test
tests/js` **12 passed, 0 failed**.
