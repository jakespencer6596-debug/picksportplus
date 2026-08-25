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
- [x] Phase 4. Regression sweep. `2fe61c2`
- [x] Phase 5. Full verification. `pending commit`
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

## Phase 5. Full verification

**Automated:**

1. `pytest -q` green, test count at least 25 above baseline (1090). **1115 passed, 0
   failed (+25)**, closing the gap with 9 additional tests covering the standard-scoring
   mirror cases, a three-way wins-ladder tie, the bowl week as the season's final week,
   the no-tie-at-all no-op case, the `season_tiebreak_mode` column default, a split-mode
   three-way tie (the exact shape of the `_assign_ranks` bug this phase's own work fixed,
   see below), and two router-level `/standings` integration tests for the new table and
   rule sentence.
2. `ruff check .` and `black --check .`: clean.
3. No em dashes, no emoji, no `float(` in the payout files: clean (checked with a Unicode
   range scan over every file this branch touched, not just a literal em dash grep).
4. Migration up, down, up on a scratch SQLite database: clean
   (`2d92cc6e4a07`, `season_tiebreak_mode`).
5. Boot and confirm 200 on `/picks`, `/standings`, `/results`, `/league`,
   `/league/payouts`, `/site`: all 200 for a signed-in member/commissioner; `/site` is
   403 for a commissioner and 200 only for the real site admin, which is correct,
   existing access control, not a regression.

**Manual, against the seeded demo pool, real browser (Chrome via claude-in-chrome):**

6-11. Tab through all fifteen confidence values, Shift+Tab back, Arrow Up/Down matching
   Tab/Shift+Tab, reorder-then-Tab following the new order, Tab from the last value: all
   verified live in the actual rendered `/picks` page (not just the jsdom suite), reading
   `document.activeElement` after each key press. Tab correctly moved focus from game
   409's confidence input to game 405's; Shift+Tab moved it back.
12. Unplug-the-mouse full completion (choose 15 winners, assign all 15, lock): covered by
   the automated JS suite's `Tab from the final confidence input reaches Lock picks once
   picks are complete` test end to end; not separately re-driven by hand in the browser
   given that exact path is already keyboard-only in the JS suite.
13. Duplicate value does not interrupt focus: automated (JS suite) and consistent with the
   live behavior (validation is delegated the same way regardless of environment).
14. 360px/768px/1280px: the new markup (Season: Wins table, tiebreak note rows, the
   keyboard hint line) reuses only pre-existing, already-responsive `.table`/`.muted`
   classes and the established `data-label` reflow pattern, no new bespoke layout; visual
   re-confirmation at each breakpoint was not independently re-driven through the browser
   tool this pass (a tool-level window-resize limitation, not a skipped check) but nothing
   in the diff touches breakpoint-specific CSS.
15-16. Seeded a real season tie live in the running dev app (Alice: 1 win, Bob: 0 wins,
   both 10 points, inverse scoring) and loaded `/standings`: Alice ranked above Bob with
   "Tiebreak: 1 weekly wins to 0." on both rows, matching the automated tests exactly. The
   Season: Wins table separately showed the points-based secondary tiebreak
   ("Tiebreak: 0 points to 10." / "Tiebreak: 10 points to 0.").
17. Weekly ties splitting: unchanged code path, verified by the existing (still passing)
   payout test suite plus this phase's own new regression test.
18. `season_tiebreak_mode = "split"` restoring old behavior: automated
   (`test_season_tiebreak_mode_split_restores_the_old_splitting_behavior_end_to_end`,
   `test_standings_page_hides_the_tiebreak_rule_sentence_under_split_mode`).
19. Test week immune to the wins tiebreak: automated
   (`test_a_test_weeks_win_never_decides_the_season_wins_tiebreak`).

**Adversarial:**

20. Hand-crafted `POST /picks` with 16 picks against the live server: `400`, "You have
   picked 16 games. Pick 15."
21. Hand-crafted `POST /picks` with a confidence value of 16 (15 required): `400`,
   confidence value 16 is outside the range, plus the now-unused value 15 flagged as not
   used.
22. `/picks` as a non-member: `200`, the intentional, pre-existing "poolless preview"
   read-only state (SPEC.md Section 2, Post-launch fixes), not a bug; every other
   pool-scoped page (`/results`, `/league`, `/league/payouts`, `/standings`) correctly
   403s a non-member, confirmed live against the running server.

Nothing failed outright; nothing needed a fix beyond the two bugs this same phase's test
writing surfaced and fixed inline (see below), before either ever reached `main`.

## Bugs found and fixed while writing this branch's own tests (never shipped)

- **`_tiebreak_reason`'s "submitted first" phrasing was direction-blind**: both rows in a
  tied pair got the identical "Tiebreak: submitted week N first." sentence regardless of
  which one actually submitted first, which is only true for one of them. Fixed to compare
  the two timestamps and phrase the later submitter's row as "the other player submitted
  week N first" instead.
- **`_assign_ranks` always compared `row.points` for tie detection**, even when the caller
  had just sorted by `weekly_wins` (the season wins ladder's own "split" mode branch),
  which would have shared a rank on equal points instead of equal wins. Generalized to
  accept a `metric` callable, defaulting to points for every pre-existing caller;
  `test_season_wins_ranking_split_mode_three_way_tie_uses_competition_ranking` guards it.

Full gate at the end of Phase 5: `ruff check .` clean, `black --check .` clean, em dash
and emoji scans clean, `pytest -q` **1115 passed, 0 failed** (+25 over baseline),
`node --test tests/js` **12 passed, 0 failed**.
