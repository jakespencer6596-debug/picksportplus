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
| 6. Full verification | In progress | |
| 7. Documentation | Pending | |
| 8. Merge, push, deploy | Pending | |
| 9. Verify and repair in production | Pending | |

## Baseline (Phase 0)

Before any change on this branch: `git status` clean on `main`, `git log -1` at `6612d52`.
`pytest -q`: **1217 passed, 2 failed.** Both failures are pre-existing and unrelated to picks,
scoring, or money (a page-weight budget test off by 490 bytes, and a notification-on-publish
test); see DECISIONS.md, "Orphaned picks", for why they are left alone rather than folded into
this incident's own fix. Every gate run on this branch's own commits is green except these
same two, called out explicitly at every step rather than silently accepted.

`doctor-picks` (the read only audit command built in Phase 0) was run against the local dev
database and, separately, against production read-only via the Render Postgres connector; see
"Production impact assessment" below for the real numbers once Phase 9 runs it for real.

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

## Test count

Baseline (Phase 0, before this branch): **1217 passed, 2 pre-existing failed.**
After Phase 5: **1238 passed, the same 2 pre-existing failed.** Net new: **21 tests**, all
passing (`tests/test_orphaned_picks.py`: 8, `tests/test_pick_repair.py`: 8,
`tests/test_pick_integrity_wiring.py`: 4, `tests/test_cli.py`: 1 new cron test).

(Phase 6 will add more; this count is updated again there.)
