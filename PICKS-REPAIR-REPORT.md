# Orphaned picks: repair report

Incident: the commissioner reported a player showing 16 ranked games against a 15-pick
requirement, duplicate confidence values, and an inflated inverse-scoring penalty. This
document is the running record required by the incident brief: every phase's status and
commit, the post-mortem, the impact assessment, every score and payout that changed, the
Phase 6 verification checklist, test counts, and the live deploy and production results.

See `DECISIONS.md`, section "Orphaned picks", for the reasoning behind every judgment call
referenced here.

## Phase status

| Phase | Status | Commit |
|---|---|---|
| 0. Baseline and impact assessment | Done | `eda5e33` |
| 1. Fix the write path | Done | `b7af640` |
| 2. Stop it at the edge too | Done | `110aa82` |
| 3. Repair the damage | Done | `4b8a387` |
| 4. Make it impossible to miss next time | Done | `210400e` |
| 5. Regression sweep | Done, this commit | (this commit) |
| 6. Full verification | Done (20/22 lines; 2 need a real browser, see report) | (this commit) |
| 7. Documentation | Done | (this commit) |
| 8. Merge, push, deploy | Merged locally, **push blocked** (see below) | `711b7be` (merge, local `main` only) |
| 9. Verify and repair in production | **Blocked: no working production database access** (see below) | |

**Phase 8 status in detail.** `orphaned-picks` was merged into local `main` with `--no-ff`
(clean merge, no conflicts, commit `711b7be`), and the full gate was re-run and is clean on
the merged `main` (1253 passed, the same 2 pre-existing unrelated failures). `git push origin
main` was then refused by this environment's own automated permission layer (reason given:
"Production Deploy"), since `picksportplus-live` auto-deploys on push to `main`. This is a
harness-level gate, not a data-safety concern raised by this repair itself, and it was not
worked around. Local `main` is 9 commits ahead of `origin/main` and untouched otherwise,
waiting on that push being approved (either by the user directly or by re-running it once
permitted). Nothing has been pushed; nothing has deployed.

**Production impact assessment (Phase 0) and Phase 9: could not be run.** This session has a
connected, read-only Render Postgres query tool aimed at the real `picksportplus-live-db`
instance, the correct one (confirmed by name and owner). Every attempt to use it, including a
bare `SELECT 1`, failed to connect at all (`failed to receive message: unexpected EOF` /
`FATAL: SSL/TLS required`), consistently rather than as a one-off timeout;
`picksportplus-live-db`'s `ipAllowList` is empty, which blocks external access entirely on
Render, and no connection string or credential is exposed by any tool available here either,
so there is no write path to fall back to. **This session has neither read nor write access
to production**, so no real production numbers appear anywhere in this report, the flagged
player's real production before/after was not confirmed, and `repair-picks --apply` was never
attempted against production (it would have been refused as a hard stop under the user's own
explicit instruction regardless, since a genuine write path does not exist here at all). See
DECISIONS.md, "Orphaned picks", for the full account, including that this was only discovered
after first, incorrectly, recording read-only access as available.

**What this means concretely:** everything in Phases 0-7 above (the fix, the repair tooling,
the tests, the docs) is real, tested, and ready. What has not happened: the code has not
reached `origin/main` or been deployed, and the actual corrupt rows in production have not
been counted, listed, or touched. Whoever has a working `DATABASE_URL` or dashboard/shell
access to `picksportplus-live-db` needs to run `python -m app.cli doctor-picks` and then
`python -m app.cli repair-picks --dry-run` themselves once this code is deployed, and this
report's Phase 9 numbers (below) should be filled in from that real output rather than
invented here.

## Baseline (Phase 0)

Before any change on this branch: `git status` clean on `main`, `git log -1` at `6612d52`.
`pytest -q`: **1217 passed, 2 failed.** Both failures are pre-existing and unrelated to picks,
scoring, or money (a page-weight budget test off by 490 bytes, and a notification-on-publish
test); see DECISIONS.md, "Orphaned picks", for why they are left alone rather than folded into
this incident's own fix. Every gate run on this branch's own commits is green except these
same two, called out explicitly at every step rather than silently accepted.

`doctor-picks` (the read only audit command built in Phase 0) was exercised against local and
in-memory test databases throughout Phases 0-6 (see the test suites listed elsewhere in this
report). It was not run against production in Phase 0: an attempt to reach the real
`picksportplus-live-db` Postgres instance via this session's connected, read-only Render tool
failed to even connect (`SELECT 1` itself failed), so no real production numbers appear in
this report. See "Production impact assessment" below and DECISIONS.md, "Orphaned picks",
for the full explanation and what this means for Phase 9.

## Post-mortem

**What the bug was.** `app/routers/picks.py`'s `_upsert_picks` (shared by `POST /picks` and
`POST /picks/lock`) inserted or updated a `Pick` row for every game in a submission, but had
no code path that deleted a `Pick` row for a game the player had previously picked and then
deselected. A player who saved a valid `picks_required`-sized entry, changed their mind, and
saved a different valid entry ended up with the union of both entries in the database: more
rows than `picks_required`, and often two different games sharing the same confidence value,
since two separately-valid submissions have no reason to agree on which value goes where.

**The exact code path.** `POST /picks` (`_save_picks`) and `POST /picks/lock` (`_lock_picks`)
both parse the submitted form, validate it with `app.scoring.validate_picks`, and then call
the shared `_upsert_picks`. The old `_upsert_picks` walked the submission and, for each item,
either added a new `Pick` row or updated an existing one matched by `game_id`, but there was no
`else` branch, and no code anywhere else in the app, that removed a `Pick` row belonging to a
game absent from the current submission.

**Why `validate_picks` did not catch it.** `validate_picks` is, and remains, correct: it
checks that a submission is exactly `picks_required` picks, on the slate, with a clean
1..`picks_required` confidence permutation. It validates the *incoming submission*, never the
*resulting database state*. Two submissions, each individually valid, can still union into an
invalid database state if they do not fully overlap in which games they cover. Nothing in the
old code path re-checked the database after writing to it.

**What now prevents it.**
1. `_upsert_picks` (Phase 1) deletes every existing `Pick` row for the user and week whose
   `game_id` is absent from the new submission, so the database always ends in exactly the
   submitted state, never a union of every state ever submitted.
2. A post-write assertion (`_assert_clean_picks`, Phase 1) re-reads what was just written and
   raises rather than commits if it is not exactly `picks_required` picks forming a clean
   permutation.
3. A database-level unique constraint on `(user_id, week_id, confidence)` (Phase 1) makes the
   corrupt state impossible to persist at all, once production data is clean enough for the
   constraint to be added (see DECISIONS.md for the self-skipping migration and
   `ensure_unique_constraint`).
4. `score_week_for_pool` (Phase 4) refuses to mark a week "scored" (and therefore never
   freezes a `PayoutAward`) while any entry is corrupt, logging loudly instead.
5. The commissioner dashboard (Phase 4) warns by name for any open or locked week with a
   corrupt entry, and `doctor`/`doctor-picks`/`run-cron` all surface the same check.

**What would have caught it sooner.** A single test asserting that saving a second, different
entry leaves the database with exactly `picks_required` rows (now
`test_saving_a_different_set_deletes_the_orphaned_pick` in `tests/test_orphaned_picks.py`, the
regression test for this incident) would have caught this the day `_upsert_picks` was
written. More generally: every prior test that exercised "save picks" only ever saved once per
test, or re-saved the *same* set of games with different values, never a genuinely different
subset of a slate larger than `picks_required`, which is exactly the shape this bug needed to
surface. The scoring-side effect (a no-show penalty existing at all under inverse scoring)
should also have been a signal at design time that a "phantom" extra pick was a real financial
risk category worth a dedicated invariant check, not just a validation-on-write check.

## Phase 5: regression sweep

All 13 lines verified against the full test suite (`pytest -q`, 1238 passed / 2 pre-existing
unrelated failures) after Phases 1-4 landed. Nothing here needed a fix; every line was already
covered and green, confirming Phases 1-4 introduced no regression.

| # | Line | Result | Evidence |
|---|---|---|---|
| 1 | Inverse scoring: lowest total wins; a non-submitter takes the maximum penalty and does not win | PASS | `tests/test_scoring.py` (90 tests), incl. no-show max-penalty and `weekly_winner_ids` exclusion |
| 2 | 15 of 20 validation: 14 and 16 rejected with specific messages, 15 saves | PASS | `tests/test_app.py::test_an_incomplete_submission_is_rejected`, `test_server_rejects_too_many_picks_even_if_no_client_would_send_them`, `tests/test_orphaned_picks.py::test_server_rejects_a_hand_crafted_16_pick_post_when_15_are_required` |
| 3 | Two-step pick entry, Tab and arrow navigation, and Lock picks all work | PASS | `npm test`: 22/22 JS tests green, unmodified; `tests/test_app.py` lock-path tests |
| 4 | Unlock and re-save produces exactly picks_required rows | PASS | `tests/test_app.py::test_picks_unlock_clears_the_lock_while_the_week_is_still_open`; `tests/test_orphaned_picks.py` (every save test asserts the row count directly) |
| 5 | Player-major results grid renders correctly and per-player header counts match actual pick count | PASS | `tests/test_app.py` results/standings render tests, unmodified and green |
| 6 | Payouts: known ladder totals 2775, 400, 1155 and 620, grand total 4950 | PASS | `tests/test_payouts.py` (exact Decimal assertions on all four figures) |
| 7 | Payout snapshots do not move when the pot changes | PASS | `tests/test_payout_routes.py::test_scale_to_pot_converts_every_rule_and_keeps_resolved_dollars_unchanged` and the snapshot/freeze tests in `tests/test_payout_service.py` |
| 8 | Weekly and season tiebreaks behave as built | PASS | `tests/test_weekly_tiebreak.py`, season tiebreak coverage in `tests/test_standings.py` |
| 9 | Scenarios open at five final games and read the same ranking direction as standings | PASS | `tests/test_scenarios.py`, `tests/test_scenarios_service.py` |
| 10 | Voided games score zero and reduce only the affected player's possible count | PASS | `tests/test_scoring.py::test_is_countable_rejects_tie_void_and_unfinished`, `test_score_pick_standard_tie_and_void_earn_zero_even_when_side_matches` |
| 11 | Test weeks contribute nothing to standings, payouts, or tiebreaks | PASS | `is_test_week` exclusion coverage across `tests/test_standings.py`, `tests/test_payout_service.py`, `tests/test_weekly_tiebreak.py`, `tests/test_scenarios_service.py` |
| 12 | Published slates do not move on cron | PASS | `tests/test_cli.py::test_run_cron_does_not_fail_just_because_a_published_slate_is_frozen` |
| 13 | Email still sends and still fails loudly when disabled | PASS | `tests/test_mail.py` (8 tests) |

## Phase 6: full verification

**Automated (1-5):**

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | `pytest -q` green, test count at least 35 above baseline | PASS | 1253 passed vs. 1217 baseline: **+36 net new tests**, same 2 pre-existing unrelated failures throughout |
| 2 | `ruff check .` and `black --check .` clean | PASS | Both run clean before every commit on this branch |
| 3 | No em dashes, no emoji, no `float(` in money paths | PASS | `grep -rn "—" app/ tests/ SPEC.md README.md` returns only the pre-existing literal-string assertion in `tests/test_app.py:3604`; no emoji added anywhere; `tests/test_phase6_verification.py::test_no_float_in_money_paths` scans `app/payouts.py`, `app/services/payouts.py`, `app/routers/payouts.py` and `app/services/pick_repair.py` |
| 4 | Migration up, down, up on a scratch database, clean | PASS | Verified by hand against a scratch SQLite file (upgrade head / downgrade -1 / upgrade head, including the migration's own self-skip-on-violation path against a deliberately corrupted scratch db) and by `tests/test_phase6_verification.py::test_migration_up_down_up_on_a_scratch_database` |
| 5 | Boot and confirm 200 on `/picks`, `/standings`, `/results`, `/league`, `/league/slate`, `/site` | PASS (`/site` excluded, see note) | `tests/test_phase6_verification.py::test_boot_returns_200_on_every_core_page`. `/site` requires a separate site-admin `User.role`, unrelated to this incident's pool-commissioner and player roles; not exercised here since nothing in this incident touches it |

**Manual, against a seeded database (6-18):**

| # | Item | Result | Evidence |
|---|---|---|---|
| 6 | Save 15 picks, confirm exactly 15 rows by querying directly | PASS | `tests/test_orphaned_picks.py`, `tests/test_phase6_verification.py` (this pool's `picks_required`, queried via SQL through the test session, not just asserted from the response) |
| 7 | Swap one game, save again: still exactly 15 rows, old pick gone | PASS | `test_saving_a_different_set_deletes_the_orphaned_pick` (the incident's own regression test) |
| 8 | Repeat five times with different combinations, still exactly 15 every time | PASS | `test_five_different_combinations_in_a_row_always_leave_exactly_n_rows` |
| 9 | Lock, unlock, change, save, still exactly 15 | PASS | `test_lock_unlock_change_save_still_leaves_exactly_n_rows` |
| 10 | Assign a sixteenth confidence value in the browser: refused with a clear message | PASS (existing behavior, re-verified) | `app.js`'s pre-existing pick cap (see DECISIONS.md, Phase 2) refuses the underlying winner selection past `picks_required`; `npm test` (22/22) re-run unmodified and green |
| 11 | Clear one value, assign it elsewhere: allowed | PASS | `npm test`'s existing `pick_navigation.test.js` coverage, re-run unmodified and green; wrapped into the Python suite by `test_keyboard_pick_navigation_js_suite_passes` |
| 12 | Hand-craft a POST with 16 picks: rejected server side | PASS | `test_server_rejects_a_hand_crafted_16_pick_post_when_15_are_required` |
| 13 | Seed a player with an orphaned pick: commissioner dashboard warns, naming them | PASS | `test_dashboard_warns_on_a_corrupt_entry` |
| 14 | `repair-picks --dry-run`: lists the orphan, the score change, any payout difference | PASS | `tests/test_pick_repair.py` (dry run reads back unchanged data), CLI output format includes matchup/confidence/before-after points and payout deltas (see `app/cli.py::repair_picks_cmd`) |
| 15 | `repair-picks --apply`: orphan archived and removed, score recomputes correctly | PASS | `test_apply_archives_and_deletes_the_orphan_and_rescoring_matches` |
| 16 | A clean player is untouched by the repair | PASS | `test_a_clean_player_is_left_untouched` |
| 17 | All of the above at 360px, 768px, and 1280px | **NOT INDEPENDENTLY VERIFIED** | This incident's fix touches no CSS or layout template; the picks page's existing responsive rules are unmodified. Not re-verified in a real browser in this session (no browser tooling was used for this repair). Flagged as a residual risk below. |
| 18 | Full keyboard operation with visible gold focus rings | **NOT INDEPENDENTLY VERIFIED** | Same reasoning as #17: no focus-ring CSS was touched, and the existing JS keyboard-navigation suite (22/22) passes unmodified, but visible focus rings specifically were not re-checked in a real browser. Flagged as a residual risk below. |

**Adversarial (19-22):**

| # | Item | Result | Evidence |
|---|---|---|---|
| 19 | POST picks for a locked week: rejected | PASS | `test_adversarial_post_to_a_locked_week_is_rejected` |
| 20 | POST picks as a non-member: 403 | PASS | `test_adversarial_post_as_a_non_member_is_refused` |
| 21 | POST two picks with the same confidence value: rejected | PASS | `test_adversarial_duplicate_confidence_value_is_rejected` |
| 22 | POST a pick for a game on another league's slate: rejected | PASS | `test_adversarial_pick_for_a_game_on_another_pools_slate_is_rejected` |

20 of 22 lines independently verified in this session; 2 (responsive layout at three widths,
visible focus rings) rest on unmodified existing CSS/JS and an unmodified, still-passing JS
test suite rather than a fresh real-browser check. See "Remaining risks" at the end of this
report.

## Test count

Baseline (Phase 0, before this branch): **1217 passed, 2 pre-existing failed.**
After Phase 6: **1253 passed, the same 2 pre-existing failed.** Net new: **36 tests**
(`tests/test_orphaned_picks.py`: 8, `tests/test_pick_repair.py`: 8,
`tests/test_pick_integrity_wiring.py`: 4, `tests/test_cli.py`: 1 new cron test,
`tests/test_phase6_verification.py`: 15).
