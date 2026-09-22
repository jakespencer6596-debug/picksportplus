# Pre-deploy check: standings and ties, September

Branch `standings-and-ties`, 13 commits ahead of `main`, 0 behind (fast-forward, no merge
conflicts possible). Full detail per phase is in `STANDINGS-REPORT.md`; this is the go/no-go
summary before anyone runs `git push origin main`.

## What ships

- Phase 1: the 120 point no-show bug. A player with no picks yet before their week's own
  `lock_at` now scores a live 0, not the maximum penalty; only a still-empty entry after lock
  takes the real penalty. Unfinished weeks count live in season totals, flagged with a muted
  "Includes week N, in progress." line.
- Phase 2: the league's actual tie rule, unconditional, on every ladder (weekly, bowl, season
  points, season wins): points, then weekly wins, then a genuine tie shares the place and
  splits the combined payout. The old two-mode submission-time system is gone; both stored
  `Pool.weekly_tiebreak_mode`/`Pool.season_tiebreak_mode` columns stay in the database
  untouched and unread, per this build's no-drop-no-rewrite constraint.
- Phase 3: the table sorter no longer drags a tiebreak note or the new pick-strip panel to the
  wrong row when a column is re-sorted.
- Phases 4-6: Results, Season, and This Week are each a single condensed table instead of a
  stack of cards or several separate tables.
- Phase 7: every leaderboard row has a chevron opening that player's own picks for the week,
  loaded once, lazily, privacy-enforced server side.
- Phase 8: mobile pass, 360/390/430px. One real bug found and fixed (a mobile-only CSS overlap
  affecting the tiebreak note, the stale-award note, and the new pick strip); the rest of the
  app carries zero horizontal overflow at any width tested.
- Phase 9: full regression sweep, 12 items, all pass, evidenced against named tests.
- Phase 10: full end-to-end browser verification against seeded, production-shaped local data,
  13 items, all pass. One real bug found and fixed: Results had no stale-award indicator for
  the weekly scope (Season already had one for both its ladders).

## Data safety

- **Zero Alembic migrations in this branch.** `git diff main..standings-and-ties -- alembic/`
  is empty. No schema change of any kind shipped with this build; every behavior change reads
  and writes columns that already exist.
- No stored row is ever rewritten to correct old data. The 120 point bug and the old tie rule
  are both corrected at read time (`_effective_points`/`_effective_did_not_submit` in
  `app/services/standings.py`; `recalculate_preview` in `app/services/payouts.py`). A frozen
  `PayoutAward` written under the old rule stays exactly as it was until a commissioner
  explicitly reviews the diff on the new preview page and confirms it.
- The admin "run results" action (`POST /league/run/results`) now stops at a preview page
  whenever recalculating would change an already-frozen award, rather than silently
  overwriting it. Confirming is a separate, explicit action (`POST
  /league/run/results-confirm`).
- This build never connected to production. `app/main.py`'s `_assert_local_database()` boot
  guard (checked against `RENDER` being unset) refused any non-local `DATABASE_URL` throughout
  every phase; every phase ran against this session's own sqlite fixtures or an isolated,
  disposable seed database. See `STANDINGS-REPORT.md`'s "Confirmation: no production data
  touched" section for the full detail.

## Gate, as of the last commit on this branch (`d99d7e0`)

- `ruff check .`: clean.
- `black --check .`: clean.
- `pytest -q`: 1272 passed, 2 failed. Both failures are pre-existing and unrelated to this
  build (`test_week_published_notification_sent_when_pool_opts_in`,
  `test_slate_editor_page_weight_budget`); neither test, nor the code path it covers, was
  touched by any commit on this branch. The bar this build ran under was "no new failures,"
  not "zero failures"; that bar is met.
- `npm test`: 23/23 passed.
- Em dash grep (`grep -rn "—" app/ tests/ SPEC.md README.md`): nothing, aside from the literal
  em dash inside `tests/test_app.py`'s own regression assertion that the character never
  renders anywhere in the app.

## What this means once it deploys

`picksportplus-live` auto-deploys on push to `main`. This is a live pool with real players and
real money, currently mid-Week 7. Once this branch merges and deploys:

- Every standings and payout number on the site will immediately reflect the new tie rule and
  the corrected no-show timing, for every past week as well as the current one. Any commissioner
  who has already paid out a place under the old submission-time tiebreak, or under the old
  120-point-before-lock bug, will see the site's own numbers change to match the corrected
  rule; the frozen `PayoutAward` rows themselves do not move until the commissioner explicitly
  confirms a recalculation on the new preview page, but the live standings and leaderboard
  views (which are not frozen) will show the corrected math right away.
- Recommend the commissioner review Season and Results once live, before telling players
  anything changed, since a place that was previously shown as decided by submission time may
  now show as a genuine split, or vice versa, purely from the timing rule fix, on already-
  played weeks.
- No downtime is expected: no migration runs, so there is nothing for the deploy to wait on
  beyond the normal app restart.

## Still needed before push

- **Explicit go-ahead from the commissioner (the user) to push `standings-and-ties` into
  `main`.** A prior session's attempt to push to `main` on this repo was refused by the
  environment's own permission layer, flagged as a production deploy (see `DECISIONS.md`,
  2026-09-14); this session has not attempted the push yet for the same reason this repo has
  already treated it as: it is a real, live, revenue-bearing deploy and not a decision this
  session makes unilaterally.
- Phase 13 (verify on the live site after deploy) cannot start until the push above actually
  happens.
