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
| 3. Explain and reverse voiding | pending | |
| 4. Midweek kickoff warnings, lock policy | pending | |
| 5. Tidy the slate editor | pending | |
| 6. League chat and member email export | pending | |
| 7. Regression sweep | pending | |
| 8. Full verification | pending | |
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

## Ambiguity decisions

See `DECISIONS.md`, section "Slate drift incident", for every ambiguous call and its reasoning.

## Phase 8 checklist (25 lines)

_Filled in during Phase 8._

## Test count

- Before: 1151
- After: _filled in at Phase 8_

## Live deploy status and Phase 11 results

_Filled in during Phase 10/11._

## Note for the commissioner to forward to his league

_Filled in once the fix is deployed (final deliverable)._

## Deliberately not built

_Filled in at the end._

## Top three remaining risks

_Filled in at the end._
