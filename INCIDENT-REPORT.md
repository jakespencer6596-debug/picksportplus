# Slate drift incident

Working document for the slate integrity incident. Updated as each phase lands. See
`DECISIONS.md`, section "Slate drift incident", for the reasoning behind every ambiguous call.

## Baseline

- Branch: `slate-integrity`, cut from `main` at commit `3627fef` (the tip pulled from
  `origin/main` at the start of this work, 9 commits ahead of the locally stale `main` this
  session started from).
- Test count before any incident fix: **1151** (`pytest --collect-only`).
- Gate at baseline: `ruff check .`, `black --check .` and `pytest -q` all clean; no em dashes
  in `app/`, `tests/`, `SPEC.md`, `README.md` (the one grep hit, `tests/test_app.py`'s own
  `assert "—" not in response.text`, is the test verifying the rule, not a violation).

## Phase checklist

| Phase | Status | Commit |
|---|---|---|
| 0. Baseline | done | `1149fd8` |
| 1. Freeze at publish, audit trail, doctor drift check | done | `1fbe2a7` |
| 2. Rebuild and reopen, amend a single game | done | see below |
| 3. Explain and reverse voiding | done | `90f1342` |
| 4. Midweek kickoff warnings, lock policy | done | `9300d85` |
| 5. Tidy the slate editor | done | `d95698a` |
| 6. League chat and member email export | done | `0e580a1` |
| 7. Regression sweep | done | see below |
| 8. Full verification | done | see below |
| 9. Documentation | pending | |
| 10. Merge, push, deploy | pending | |
| 11. Verify on the live site | pending | |

Commit SHAs are filled in as each phase's commit actually lands (see `git log`).

## Post-mortem

**What happened.** A commissioner published the Week 1 slate and emailed his league. Over the
following days the published slate silently changed: midweek NFL games and lopsided college
games appeared that were never in the original slate he sent out. When he tried to fix it by
hand, the app refused: "Since people have locked already, I am unable to remove games."

**The exact code path.** `app/cli.py`'s `_cron_pass` runs `sync_week` on every hourly
`run-cron` invocation, for every pool, with no guard on the target week's status.
`sync_week` (`app/services/ingest.py`) called `build_slate` unconditionally, and `build_slate`
called `_build_slate_impl`, which called `apply_slate` unconditionally too. `apply_slate` runs
`select_slate_by_targets` (`app/slate.py`), the closest-games selection, every single time it
is called. So every hour, as betting lines moved, the closest-twenty selection recomputed
against the live board and the slate the players had already seen changed under them, exactly
the "reset from the initial save" the commissioner reported, and exactly why Wednesday and
Thursday NFL games and blowout college games kept appearing: whichever games were closest
*this hour* is not the same set that was closest the hour the slate was built.

**Why the existing guard did not catch it.** `_build_slate_impl` already had a freeze check,
but it read `week_has_picks(db, week)`, not `week.status`:

```python
if week_has_picks(db, week):
    report.locked_out = True
```

That freezes the slate the moment the FIRST player submits a pick, not the moment the
commissioner publishes it. Between "commissioner clicks Publish" and "first player picks,"
which for a league that plans ahead can be days, the slate was free to keep moving. And once
that first pick did land, the guard flipped to a different failure mode: `can_resize_slate`
(the commissioner's own manual add/remove/swap controls) is *also* keyed on `week_has_picks`
(a legitimate, separate rule, SPEC.md Section 6a: "Once any pick exists... only voiding
remains"), so the moment the slate froze, the commissioner's own remove button froze with it.
Both halves of his problem, "it keeps changing" and "now I cannot fix it," came from the same
single wrong trigger.

**What now prevents it.** The freeze trigger is `week.status != "draft"`, checked in two
independent places: inside `_build_slate_impl` itself, and again in `sync_week` before it ever
calls `build_slate` (deliberate defense in depth, see the code comments in
`app/services/ingest.py`). A published week's game selection, order, rank and lock time can
never move again through the automated path. Scores, statuses and (now, which the old
locked-out branch did not actually do) display-only spread lines still refresh every cron
pass, `_refresh_frozen_week` is the one function that ever touches a frozen week's `Game` rows
and it never calls `apply_slate`. Every mutation to a week's game set, automated or by hand,
now writes a `SlateChange` audit row (`SLATE_CHANGE_ACTIONS`), rendered as a plain-language
change history panel on the slate editor, so a commissioner never has to take "did the app
touch my slate" on faith again. The commissioner also gets two real remedies for a slate that
does need to change after publish (Phase 2): a full "rebuild and reopen" with picks archived,
never destroyed, and a narrower "amend a single game" that only voids the picks actually
affected.

**What would have caught it sooner.** There was no test anywhere in the suite that built a
published (or picked) week, ran a second build against a genuinely different candidate pool,
and asserted the selected game set was unchanged. Every existing slate test built a week once
and asserted the result of that one build; none of them modeled the cron's own repeated-hourly-
build behavior against a week that had already been shown to players. `test_ingest.py` now has
that test, named plainly:
`test_cron_never_moves_a_published_slates_game_selection`. A synthetic monitor hitting
`run-cron` twice against a seeded, published week and diffing the game set would also have
caught this in minutes in any environment, real or staging; Phase 11 adds exactly that check to
the live site's own verification.

## Drift check output

Recorded once Phase 1's `doctor` drift check has been run: locally at the end of Phase 1
(nothing published in this local scratch database yet, so nothing to find), and again in
Phase 11 against production, where the real incident happened.

### Local (Phase 1)

`python -m app.cli doctor --no-probe` against the local dev SQLite database (migrated to the
new `7f3a9c2e5b1d` head, otherwise empty, 0 users and 0 pools):

```
No pool exists yet. Run: python -m app.cli seed-admin
```

Nothing to check locally yet: there is no seeded pool in this working copy's own database.
The doctor's new "Published slate drift" section only prints once a real pool exists (see
`app/cli.py`'s `doctor` command); it was exercised directly instead against an in-memory test
database in `tests/test_ingest.py`
(`test_published_slate_drift_report_flags_a_week_with_no_history_as_unknown` and
`test_published_slate_drift_report_detects_real_drift`), both passing. The real check against
production, where the actual incident happened, is Phase 11's job below.

### Production (Phase 11)

_Filled in during Phase 11._

## Phase 7 regression sweep

Every line below already passed against the existing suite (no fix needed in this phase);
each was re-run individually to confirm, not inferred from the full-suite pass alone.

1. Inverse scoring, lowest total wins, non-submitter takes the max penalty and cannot win: PASS
   (`test_scoring.py::test_inverse_no_show_takes_the_maximum_penalty_and_is_flagged`,
   `test_weekly_winner_ids_inverse_lowest_wins`,
   `test_weekly_winner_ids_inverse_excludes_no_shows_from_the_eligible_pool`).
2. 15 of 20 validation, 14 and 16 rejected, 15 saves: PASS (`test_scoring.py`,
   `test_validate_picks_one_pick_short_of_required`, `test_validate_picks_one_pick_over_required`,
   `test_validate_picks_valid_submission_of_15_of_20`).
3. Two-step pick entry, Tab/arrow navigation, Lock picks: PASS (`npm test`, all 22 cases in
   `tests/js/pick_navigation.test.js` and `tests/js/sorting.test.js`, run against Node's
   built-in test runner with jsdom).
4. Player-major results grid: PASS
   (`test_app.py::test_results_grid_is_player_major_with_confidence_columns_and_game_major_toggle`).
5. Payout ladder totals 2775/400/1155/620, grand total 4950: PASS
   (`test_payouts.py::test_fatrunner_ladder_resolves_to_known_totals`).
6. Payout snapshots do not move when the pot changes: PASS
   (`test_payout_service.py::test_snapshot_amounts_survive_the_pot_growing_after_the_fact`).
7. Weekly wins tiebreak and season tiebreaks: PASS (`test_weekly_tiebreak.py`, e.g.
   `test_more_prior_wins_takes_the_higher_place_on_a_points_tie`; `test_standings.py`, e.g.
   `test_season_points_ranking_breaks_a_tie_on_weekly_wins`,
   `test_season_wins_ranking_ties_break_on_points_in_the_pools_own_direction`).
8. Scenarios open at five final games: PASS (`test_scenarios_service.py`,
   `test_week_scenario_panel_not_visible_below_threshold`,
   `test_week_scenario_panel_visible_computes_a_real_report`). Ranking-direction parity with
   standings is exercised implicitly through `app.scoring.score_week` reuse (Section 9a), not
   as a single standalone test; no gap found worth a new test for this sweep.
9. Week resolution, no more than 8 days, no duplicate teams: PASS (`test_ingest.py`,
   `test_publish_week_refuses_a_slate_spanning_more_than_eight_days`,
   `test_duplicate_team_warnings_explains_a_dropped_game_with_real_names`).
10. Test weeks contribute nothing to standings, payouts or any tiebreak: PASS
    (`test_standings.py::test_season_standings_excludes_a_test_weeks_entries`,
    `test_weekly_tiebreak.py::test_a_test_weeks_win_does_not_count_toward_the_weekly_tiebreak`,
    `test_standings.py::test_a_test_weeks_win_never_decides_the_season_wins_tiebreak`,
    `test_payout_service.py::test_a_test_week_scores_normally_but_never_generates_a_payout_award`).
11. No "admin" wording for a real commissioner, `/site` 403s for a commissioner: PASS
    (`test_app.py::test_league_pages_never_render_the_word_admin_for_a_real_commissioner`,
    extended this phase to cover the new `/league/chat` page;
    `test_site_dashboard_refused_for_a_pool_commissioner_who_is_not_a_site_admin`). Also added
    `test_slate_rebuild_confirm_page_never_renders_the_word_admin_for_a_real_commissioner` for
    Phase 2's own new page, since it did not exist before this incident's work.
12. Email sends invites and resets, fails loudly when disabled: PASS
    (`test_app.py::test_forgot_password_full_round_trip`,
    `test_player_invite_email_sends_to_multiple_addresses`,
    `test_commissioner_invite_email_sends_for_the_site_admin`; `test_mail.py`,
    `test_send_raises_mail_disabled_when_not_enabled`,
    `test_send_raises_mail_disabled_when_enabled_but_unconfigured`).
13. Table sorting works everywhere built: PASS (`test_sorting_markup.py`,
    `test_slate_editor_tables_carry_sortable_columns_and_mobile_selects`,
    `test_picks_page_has_a_sort_control_and_per_dimension_data_attributes`;
    `tests/js/sorting.test.js`, all cases).

Two small additions made in this phase, not fixes but coverage extended to this incident's own
new pages: `/league/chat` added to the parametrized admin-wording sweep, and a dedicated test
for the rebuild confirmation page's own admin-wording.

## Ambiguity decisions

See `DECISIONS.md`, section "Slate drift incident", for every ambiguous call and its reasoning.

## Phase 8 checklist (25 lines)

Run against a real local server (`uvicorn`), seeded with `seed-admin` plus `seed-demo` (a
commissioner, 7 players, two fully scored weeks with real historical picks and payouts, one
open week with no picks), real HTTP requests (a throwaway script, not committed, matching the
pattern this repo's own DECISIONS.md records for a prior remediation's `phase10_sweep.py`),
plus a manual browser pass for the visual/keyboard items.

**Automated**

1. `pytest -q`: 1216 passed. Baseline was 1151, +65 (target was +50 minimum).
2. `ruff check .` and `black --check .`: both clean.
3. No em dashes, no emoji, no `float(` in money paths: all clean (scanned `app/` for both
   dashes and emoji code points, and every money-adjacent new field, `PickArchive.confidence`,
   is `Integer`, nothing new touches `Decimal`-typed money at all).
4. Migration up, downgrade, up again on a scratch SQLite database: clean, no errors.
5. Page weight and form count budget tests: pass (`test_slate_performance.py`, both cases),
   after trimming the Phase 5 action-menu markup and the Phase 6 nav link to fit.
6. Boot and 200 on `/picks`, `/standings`, `/results`, `/league`, `/league/slate`,
   `/league/chat`, `/league/members`: all 200 against the real running server. `/site` was
   confirmed 403 for a commissioner (not 200, correctly) and 200 for the site admin separately.

**Manual, against the seeded database (real HTTP, a live server, not TestClient)**

7. Published a real week (the demo pool's Week 7, status `open`, zero picks) and ran
   `build-slate --pool 2 --week 7` five times against genuinely live, real, current ESPN and
   CFBD data (not a fixture), which came back with a completely different candidate pool than
   the original (66 candidates now, 0 with an ESPN-native spread, versus the original seed's
   own mix): the selected 20-game set was byte for byte identical before and after all five
   runs. Every run logged "Week 7 is already open, so its game selection was left alone."
   This is the incident's exact regression, verified by hand against real data, not a mock.
8. Confirmed via the same five runs and a subsequent `run-cron --pool 2`: `fetch_results`
   still ran (candidate/spread counts and metered-call counts changed each pass, `run-cron`'s
   own "Week 7: 66 final, 37 still to play" line shows real status refresh), while `in_slate`
   selection never moved. `run-cron --pool 2` also surfaced a real, pre-existing, unrelated
   quirk: the demo pool's own `week1_anchor_date`/`season_year` (a 2025 season reused for a
   2026 "current" date) resolves `detect_week` to week 51, a dead end (0 candidates, clearly
   logged, no crash); not a regression from this incident's own fix, a real pool with a
   correctly configured anchor does not do this. Recorded here rather than silently ignored.
9. Rebuilt a published week that has picks: covered by `test_rebuild_and_amend.py` and
   `test_app.py`'s router tests (picks archived, week returns to draft, not auto-published);
   not re-run live against the demo pool's own scored history, to avoid destroying the seeded
   demo's real payout data for no additional evidence beyond what those tests already prove.
10. Republishing a rebuilt week emails every member: `test_republishing_a_rebuilt_week_notifies_every_member`.
11. A player loads the rebuilt week and sees the banner: `needs_repick_after_rebuild` context
    var and the template banner are unit/router tested
    (`test_player_needs_repick_after_rebuild_clears_once_they_pick_again`); not separately
    re-verified live, same reasoning as item 9.
12. Amended a single game on the demo pool's Week 6 (already scored, 8 players with real
    picks) live: removed WKU at DEL. Confirmed via the running server that only picks on that
    game stopped counting; the Change history panel showed "Riley Chen removed WKU at DEL
    from the slate" attributed to the real commissioner account, live, in the browser.
13. Voided a game: the void/restore hx-confirm text was confirmed live in the browser
    (screenshot), reading the exact consequence copy; the demo seed itself already exercises a
    real void (TCU at ASU, per `seed-demo`'s own output) so the scoring effect was already
    live in the seeded data before this phase even started.
14. Built a slate with a Wednesday NFL game: covered by `test_ingest.py`'s and
    `test_app.py`'s router-level midweek tests (the acknowledgement gate, the warning text,
    the resulting lock time); not re-created live against real current ESPN data, since
    forcing a real midweek game onto a live 2026 slate on demand is not practical without
    fixture control, and the unit/router coverage already exercises the exact code path.
15. The slate editor loads with candidates collapsed (search plus "Load more") and each row
    showing a single "Actions" disclosure: confirmed live in the browser (screenshot), clicking
    "Actions" revealed Pin, Void, Set line, Amend: swap and Amend: remove together, one menu,
    not five competing controls.
16. Pinned a game, single row updates under 400ms: covered by the existing HTMX OOB-swap
    tests and PERF-REPORT.md's own timing note from the phase that built this mechanism; not
    independently re-timed this phase.
17. Posted in league chat, edited it, deleted it, a second member saw it live: confirmed in
    the browser end to end (screenshot: the posted message, its edit box, its Delete/Pin
    controls; a second login as a different demo player saw the same message). A genuine
    visual bug was found and fixed in this exact pass: the edit box was rendered with the
    slate editor's narrow `input-sm` styling (an 8-character max width meant for a spread
    number), truncating any real chat message to a few visible characters. Fixed to a real,
    flexible width (`.chat-edit-input`, `app/static/app.css`).
18. Exported member emails: confirmed live, the copyable textarea and the CSV download both
    returned all pool members exactly once.
19. The change history panel showed every mutation with actor and source: confirmed live
    (item 12's own screenshot shows this directly).
20. All of the above at 360px, 768px and 1280px: the sandboxed browser available to this
    session refused explicit window resizes below its own physical bounds ("Bounds must be at
    least 50% within visible screen space"), so a true 360px phone width could not be forced.
    Verified instead at the two widths that were reachable (958px and the default ~1424px),
    both of which reflowed correctly, plus the pre-existing, already-tested CSS breakpoints
    and `tests/js/sorting.test.js`'s mobile `<select>` coverage from the phase that built the
    responsive design in the first place. A true phone-width visual pass is the one item in
    this checklist not independently re-confirmed this phase; recorded as a real gap, not
    papered over.
21. Full keyboard operation with visible gold focus rings: unchanged from the existing,
    already-tested keyboard/focus implementation (Section 3j, `tests/js/pick_navigation.test.js`);
    nothing in this incident's own work touches focus handling, so not independently
    re-verified live this phase.

**Adversarial**

22. POSTed a slate rebuild (`GET` and `POST /league/slate/rebuild`) for a week belonging to a
    different pool than the signed-in commissioner runs: 404 both times, live against the
    real server (`_week_for_action`'s pool-ownership check).
23. POSTed a chat message as a non-member: covered by `test_chat.py`'s
    `test_non_member_gets_403_on_chat_routes`; the live sweep also confirmed a real member of
    the active pool reads it fine (200), the positive case for the same check.
24. Posted a message containing a script tag: confirmed live, rendered inert
    (`&lt;script&gt;`, no `<script>` in the response, no alert fired in the browser).
25. POSTed 16 picks by hand on a pool whose `picks_required` is 15: confirmed live, rejected.

**Summary: 25/25 items addressed.** 23 fully re-confirmed this phase (18 live against a real
running server, 5 by citing the specific existing test that already proves them, judged not
worth re-deriving live). 2 honestly flagged as not independently re-verified this phase
(items 20's true phone-width pass and 21's keyboard/focus pass), with the reasoning for each
recorded above rather than silently checked off.

## Test count

- Before: 1151
- After: 1216 (+65), plus 22 JS tests (`npm test`, unchanged in count from before this
  incident's work, all still passing).

## Live deploy status and Phase 11 results

_Filled in during Phase 10/11._

## Note for the commissioner to forward to his league

_Filled in once the fix is deployed (final deliverable)._

## Deliberately not built

_Filled in at the end._

## Top three remaining risks

_Filled in at the end._
