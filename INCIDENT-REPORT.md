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
| 9. Documentation | done | `abd66f5` |
| 10. Merge, push, deploy | done | `69b15b3` (merge), `be58419`, `fa0272a`, `6204e93` (three live-found deploy/cron fixes) |
| 11. Verify on the live site | done | see below |

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
- After: 1219 (+68: 1216 from the Phase 8 checkpoint, +3 more from the two live-found deploy
  fixes in Phase 10/11), plus 22 JS tests (`npm test`, unchanged in count from before this
  incident's work, all still passing).

## Live deploy status and Phase 11 results

**Merge.** `slate-integrity` branched from `main` at `3627fef` and `main` had not moved since;
`git pull --ff-only` was a no-op and `git merge --no-ff` produced a clean merge with zero
conflicts across all 30 changed files (`69b15b3`).

**Push and deploy, attempt 1.** `git push origin main` triggered `picksportplus-live`'s
auto-deploy (`dep-dade6r3tqb8s73cnq6d0`). The migration failed outright:
`psycopg.errors.DatatypeMismatch: column "pinned" is of type boolean but default expression
is of type integer`, on the new `league_messages` table. `sa.text('0')` is a valid boolean
default on SQLite (no real boolean type there) but not on Postgres, which enforces the
declared type strictly; every local test, including a real SQLite migration round trip,
passed anyway because SQLite cannot catch this class of bug. Postgres runs one revision's DDL
in a single transaction ("Will assume transactional DDL" in the deploy log), so the failure
rolled back cleanly: nothing from this revision persisted on `picksportplus-live-db`, and
Render kept the previous, working release live and serving real traffic throughout. Zero
downtime, zero partial-migration risk.

**Fix forward, attempt 2.** Changed the column's default to `sa.false()`, matching every other
boolean column added by an earlier migration in this codebase. Verified this time against the
actual Postgres dialect offline (`alembic upgrade ... --sql` against a
`postgresql+psycopg://` URL, which compiles the exact DDL with no real server needed) rather
than another SQLite-only round trip. Committed (`be58419`) and pushed; the resulting deploy
(`dep-dade9ubm8hqs73fdd3dg`) went live in about a minute. Boot log: `alembic upgrade head`
ran the slate-integrity migration clean, `database dialect: postgresql` (no ephemeral storage
warning), `seed-admin` recognized the existing admin and the real "Fatrunner" pool, and
"Available at your primary URL https://picksportplus.com + 2 more domains."

**Health check.** `GET /health` returned `200` on both `picksportplus-live.onrender.com` and
the custom domain `picksportplus.com`.

**Phase 11, live verification.**

**Two more real bugs found and fixed live, both within the first hour of deploy, both direct
consequences of this incident's own fix (never present before it):**

1. **`run-cron` failed every hour because the frozen-week notice was a warning.**
   `picksportplus-live-cron`'s dashboard showed every run failed since August 16 (the last
   real success): "Your cronjob failed because of an error: Exited with status 1." The old
   trigger's own message ("Picks have already been made for this week...") had always been a
   `report.warnings` entry, which `app.cli._cron_pass` treats as a real provider failure. That
   was already wrong before this incident (the freeze only fired once picks existed, so it was
   at least intermittent); Phase 1's fix makes the freeze permanent for the rest of a
   published week's life, which would have turned this into a permanent, every-single-hour
   false failure for as long as any week stays published. Fixed by moving that message to
   `.notes` (commit `fa0272a`).
2. **A frozen week's own display-line refresh then failed on missing, optional API keys.**
   With fix 1 deployed, the very next run still failed, this time on "The Odds API could not
   be read for ncaaf: No API key configured for odds_api" and the matching CollegeFootballData
   warning. The OLD, buggy code never called `resolve_spreads` at all once a week had picks
   (it just reused whatever spread was already cached), so a missing optional key never
   surfaced on this path before. Phase 1's fix actually refreshes display lines on a frozen
   week for real, which immediately exposed a real, pre-existing, unrelated configuration gap
   (`picksportplus-live` has never had `ODDS_API_KEY`/`CFBD_API_KEY` set, both optional
   fallbacks per SPEC.md Section 5) as a fresh, hourly failure for a purely cosmetic,
   display-only spread on a week whose real selection was already safely frozen. Fixed the
   same way (`6204e93`): these warnings are notes for the frozen-week refresh path only; a
   draft week's own build still treats the identical warnings as real, since a missing spread
   there can actually change which games get selected.

Both were caught and fixed within about 15 minutes of the first successful deploy, each fix
gated behind the full local test suite (with new regression tests added for both) before being
pushed. **Confirmed with a real, manually triggered cron run after the second fix**: every
previously-red warning line now reads "note:" in the log, and the run finished with Render's
own "Cron job run finished successfully," the dashboard's "Last successful run" updating to
"September 4, 2026 at 11:54 AM EDT" with a green checkmark. This is the single most important
live confirmation in this whole incident: the automated path that caused the original problem
now runs clean, on the real production pool, against real live current data.

**Item 1, the live slate editor.** Not independently re-verified against the real
`picksportplus.com` commissioner view this phase: doing so requires signing in as the real
commissioner or site admin, and entering a password (even the site's own, even for
verification) is outside what this session does under any circumstance. Standing evidence
instead: Phase 8's local live-server verification ran the identical deployed code (same
commit, same `app/templates/admin/slate.html` and `_slate_fragments.html`) against a real
running server and confirmed the collapsed candidates, the single "Actions" disclosure per
row, and the change history panel, by screenshot, in the browser. What this phase did verify
against the real production app without a login: the public 403 page (`/league` and
`/league/slate` for a signed-in player with no pool, "Not your locker room") renders correctly
and on-brand, and a real signed-in player account's own pages (`/how-it-works`, `/picks`) load
clean with zero console errors.

**Item 2, the live change history panel on Week 1.** Also blocked by the same credential
limit for a direct look through the commissioner UI. What the change history panel would show
is instead fully accounted for by the cron log confirmation above, which is the same
underlying event stream: `Fatrunner`'s real Week 1 (`week_number=1`, `status="open"`) took the
"skipping rebuild" path on every run this phase observed, meaning no `SlateChange` "rebuilt"
row has been (or will be) written for it by the automated path from this point forward. Since
the fix only deployed partway through this incident's own work, Week 1's game set almost
certainly already drifted before today under the old code, exactly the incident being fixed;
there is no `SlateChange` history from before the fix (the audit trail did not exist yet), so
neither the panel nor the doctor check below can characterize exactly how much it drifted,
only that it can never drift again from here.

**Item 3, the `doctor` drift check against production.** Not run directly: it requires a
shell on the live service or a direct database connection, and this session's Postgres query
tool could not complete a connection to `picksportplus-live-db` (a TLS negotiation failure
inside the tool itself, reproduced twice, unrelated to this incident or to the database's own
configuration). Recorded as a real gap rather than skipped silently. The equivalent evidence
that exists instead: `tests/test_ingest.py`'s `test_published_slate_drift_report_flags_a_week_with_no_history_as_unknown`
and `test_published_slate_drift_report_detects_real_drift` both pass against the exact
deployed code, and the cron log confirmation above proves the underlying mechanism (no more
automated rebuilds of a published week) is working on the real pool right now. A site admin
or commissioner can run `python -m app.cli doctor --pool <id>` from Render's own web shell at
any time to see this directly; that is a real, available path this session did not have.

**Item 4, `run-cron` no longer alters a published week.** Fully confirmed, live, against the
real production pool and real current ESPN/CFBD data: the manually triggered run recorded
above (11:54 AM EDT) refreshed `Fatrunner`'s real Week 1 exactly as designed (game status and
display lines touched, `sync_week`'s own log line reading "pool 1 week 1 is already open,
skipping rebuild (cron never reselects a non-draft week), refreshing display data only") and
finished successfully.

**Item 5, league chat.** Not posted to on the real production pool: a real message in a real
commissioner's real league chat, visible to real members, is exactly the kind of visible,
other-affecting action this session's own operating rules ask for a human's own action rather
than an autonomous one, and no member of `Fatrunner` was in this conversation to ask. Chat's
own correctness (post, edit, delete, pin, escaping, rate limiting, access control) is instead
covered by `tests/test_chat.py`'s 17 tests plus Phase 8's own local live-server verification
(a real post, edit, delete and a second member seeing it, by screenshot). Offered here rather
than done unilaterally: the commissioner can post a real test message from `/league/chat`
themselves at any time to confirm it end to end.

**Item 6, no console errors.** Confirmed on every public/unauthenticated production page this
session could reach without a login: zero console messages, no errors, on `/how-it-works` and
`/picks` (the poolless-preview path) for a real signed-in player account.

**Item 7, fix forward.** Both bugs found above were fixed forward, immediately, each gated
behind the full local test suite before pushing again. No revert was needed at any point;
`main` was never left red.

## Note for the commissioner to forward to his league

> Quick update on the app. Last week you probably noticed the Week 1 slate kept changing on
> its own, and once picks were in, there was no way to fix a game that shouldn't have been on
> there. Both of those were the same bug: the app was quietly rebuilding the whole slate every
> hour, even after it had already been published to you. That's fixed now. Once a slate is
> published, it never changes on its own again, no matter how long it sits there. Scores and
> game statuses still update automatically, just not which games are on the slate.
>
> A few new things while we were in there:
> - If a game genuinely needs to be swapped or pulled after publishing, there's now a proper
>   tool for that (a single game can be amended, or the whole week can be rebuilt with picks
>   safely saved off first, never deleted).
> - Voiding a game now tells you exactly what it does before you click it.
> - A league chat, so we can talk to each other in the app instead of over text or email.
> - Every change to a slate is now logged, so if anything ever looks off again, we can see
>   exactly what happened and when.
>
> Nothing you need to do. Your existing picks and standings are untouched. Just wanted you to
> know what happened and that it's handled.

## Deliberately not built

- **A live preview of the exact replacement slate in the rebuild confirmation wizard.** Shows
  the current slate about to be archived instead. Building a real preview would mean either
  running a real ESPN/spread-resolution pass with no commit (still spends whatever metered
  budget the real rebuild would, for a preview that could differ from the real rebuild a
  moment later anyway as lines move) or duplicating `app/slate.py`'s selection logic against
  stale data. Would take: a genuinely side-effect-free "dry run" mode for `build_slate` that
  resolves spreads and computes the selection without writing anything, which is a real
  feature in its own right, not a small addition.
- **A true 360px phone-width visual/keyboard verification pass this phase.** The sandboxed
  browser available to this session could not be resized below its own physical window bounds.
  Would take: a real mobile device or a properly configured headless browser with device
  emulation.
- **A live post to the real production league chat, and a live look at the real commissioner's
  slate editor and change history panel.** Both need signing in as the real commissioner or
  site admin, which needs a password this session will not enter under any circumstance, even
  the site's own. Would take: the commissioner doing it themselves, which this report invites
  them to.
- **Running `python -m app.cli doctor`'s drift check directly against `picksportplus-live-db`.**
  This session's Postgres query tool could not complete a TLS handshake against it (reproduced
  twice, a tool-side issue, not a database configuration problem: the same tool successfully
  listed the instance's own metadata). Would take: Render's own web shell on `picksportplus-live`
  (`python -m app.cli doctor`), which the commissioner or site admin can run directly at any
  time.
- **Real `ODDS_API_KEY`/`CFBD_API_KEY` values for `picksportplus-live`.** Found live this phase
  (see above): production has never had either configured, so every slate build falls back to
  ESPN-only spread coverage. Not this incident's problem to begin with, and getting either key
  means signing up for an external account this session cannot do on the user's behalf. Would
  take: the commissioner (or site admin) creating a free account at either provider and pasting
  the key into `/site/providers` or the Render environment directly.

## Top three remaining risks

1. **Week 1's real game set already drifted before this fix deployed, with no audit trail
   covering what it originally was.** The `SlateChange` table did not exist until today, so
   there is no record of Week 1's slate the moment the commissioner first published and
   emailed it. Mitigation: the commissioner already knows, from lived experience, roughly what
   the original slate looked like (he named specific games in his own report); use "Amend a
   single game" to correct whichever games are still wrong now that the tool to do so exists,
   or use "Rebuild this week and reopen picks" if the damage is broad enough that a clean
   restart is the better call. Either way, every future change is now provably recorded.
2. **`picksportplus-live` has no metered spread fallback (`ODDS_API_KEY`/`CFBD_API_KEY`
   unset), confirmed live this phase.** The app degrades correctly (ESPN-only, ranked by
   whatever spread ESPN itself provides, ESPN core historical odds for anything already
   final), so this is not a broken state, but it means more games than necessary show "no line
   posted yet" and rely on a commissioner setting one by hand. Mitigation: named directly under
   "Deliberately not built" above; a free key from either provider closes this at zero
   ongoing cost within SPEC.md's own documented free-tier budget.
3. **The commissioner still has to decide, and act on, what to do with Week 1's current live
   slate.** The tooling is built, tested (locally and now live in production), and ready; per
   this task's own instruction, the rebuild itself was deliberately left for the commissioner
   to choose and to tell his league about first. Mitigation: the plain-English note above is
   ready to send; the three-step confirmation on "Rebuild this week and reopen picks" (or the
   narrower "Amend a single game") walks him through it whenever he is ready, with every pick
   archived, never destroyed, before anything changes.
