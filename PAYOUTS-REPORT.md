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
- [x] Phase 9: document, local merge to main (merge commit 8bf5c54; no push, no live deploy,
  by design, see below)

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
    (checked live and by `tests/test_payout_routes.py::test_duplicate_scope_place_on_create_is_rejected`).
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

## Phase 9: documentation and merge

`SPEC.md` section 10b rewritten for the current design (four scopes, dollar/percent modes,
per-week weekly semantics, pot/override, ties and rounding, frozen snapshots, over-allocation
warns not blocks), section 11 extended with the four new CLI commands, and a new note in
section 4 correcting the earlier inaccurate claim that the Decimal-money rule already lived in
section 3h (it did not; see DECISIONS.md). `README.md` got a "Payouts" walkthrough under "The
commissioner", the four new CLI commands documented under "Running a week end to end", and the
Render ephemeral-storage note extended to cover hand-entered payout rules specifically.
`DECISIONS.md` has a full "Payout system" heading covering every ambiguity resolved across all
nine phases, in order, with reasoning.

Full gate run one final time on the `payout-settings` branch (939 passed, ruff/black/em-dash
all clean), then `git checkout main && git pull --ff-only` (already up to date),
`git merge --no-ff payout-settings` (clean merge, no conflicts), full gate re-run on `main`
itself (939 passed, ruff/black/em-dash all clean). `main` now sits at merge commit `8bf5c54`,
one commit ahead of `origin/main` (`f2edba1`). Per explicit direction, this agent does not have
`git push` permission (a hard block from the harness's own permission system, confirmed early
in this build, not worked around) and does not have Render dashboard/shell access, so this is
where the work stops: `main` is merged locally with a green gate, not pushed. The product owner
pushes `origin/main` and verifies the live Render deploy directly afterward, the same way every
other push in this project has been handled.

## Every phase, with its commit SHA

- Phase 0 (orientation, baseline, branch): `d09b83d`
- Phase 1 (models and migration): `2634138`
- Phase 2 (pure engine, `app/payouts.py`): `68fb3e7`
- Phase 3 (service layer, snapshots, scoring hook): `231a9ac`
- Phase 4 (Set Payouts admin screen): `02f45df`
- Phase 5 (player-facing payout display): `d5a0360`
- Phase 6 (commissioner summary and export): `3447d6f`, follow-up fix `6c3bcb3`
- Phase 7 (CLI, demo data, cron safety): `a29640d`, `9498d80`
- Phase 8 (full verification sweep): `2d1dabc`
- Phase 9 (SPEC.md/README.md docs, merge): `3b27724`, `32b7f10`, merge commit `8bf5c54`
- Unrelated, requested alongside this build, pushed by the product owner directly before this
  branch existed: the provider budget visibility fix, `f2edba1`

No phase gate failed a third consecutive time; the Failure Protocol's revert-and-continue path
was never needed. Every gate passed on the first or second attempt of the phase it belonged to.

## Test count

861 passing before this build (baseline). 939 passing after Phase 9's merge to `main`. Net
+78 (the spec's own bar was +60 over baseline).

## Live deploy

Not pushed, not deployed, by design (see above: this agent has no push permission and no
Render access, and was explicitly told not to attempt to route around that). The product owner
will push `origin/main` and confirm `/admin/payouts` renders on the live site directly.

## Plain-English walkthrough: setting up your four ladders from scratch

Written for the commissioner, no code required.

1. Sign in and go to Admin, then Payouts (`/admin/payouts`).
2. At the top, "Pot" is your money box. Type in your entry fee (how much each player pays to
   join) and it multiplies by however many members are marked paid on the Members page. If you
   would rather just type in the real total by hand (a reserve, a carryover from last year,
   whatever the real number is), use "Pot override" instead, it always wins over the computed
   figure. You will also see "Weekly payout weeks" (how many regular weeks you pay out, 15 by
   default), "Rounding" (round shares down to the cent, the whole dollar, or the nearest five
   dollars), and "Tiebreak" (how a split cent or dollar gets handed out when two people tie,
   today this is always "earliest submitted").
3. Below the pot panel are four sections: Weekly, Bowl Week, Season: Points, Season: Wins. The
   fastest way to start is the "Load preset" button, which fills in a known, real payout
   ladder (the one this build was built around: weekly 105/55/25 a week, bowl 250/100/50,
   season points 600/405/150, season wins 325/185/110) so you can see the shape of the screen
   working with real numbers, then edit from there. It asks you to confirm since it replaces
   anything already configured.
4. In each section, "Add place" adds a row: pick the place (1st, 2nd, 3rd, and so on, no
   limit), a mode (dollars or percent of the pot), a value, and an optional label. Whichever
   mode you did not pick shows up next to it in muted text, so you always see both the dollar
   figure and the percent of the pot at the same time, never having to guess one from the
   other.
5. If your player count changes and you want every payout to automatically rescale instead of
   re-typing every number, click "Scale to pot". It converts every rule to a percentage of the
   pot at whatever it is currently worth, so the dollar amounts stay exactly the same today,
   but next season, if the pot is bigger or smaller, every payout grows or shrinks with it
   automatically.
6. Underneath the four sections is a live summary, in the same shape as a spreadsheet: each
   category's breakdown, its total, then the grand total, the pot, and what is left over
   (unallocated). If your payouts add up to more or less than the pot, a banner tells you
   exactly how far off you are. It still lets you save either way. Reserves and rounding are
   normal.
7. That is the whole setup. From here, payouts show up automatically: a Payout column appears
   on Weekly Results once a week finishes scoring, and once you save your Season: Points and
   Season: Wins rules, two award panels show up on Season Standings once the bowl week
   finishes scoring (that is this app's own signal that the season is over).
8. To see what everyone is owed and pay them, go to the "View payout summary" link from the
   Payouts screen. It lists every player, their total owed, and a checkbox that marks (or
   unmarks) everything they are still owed as paid. There is a running "Paid X of Y, N of M
   players settled" line at the top so you can track your progress. "Copy as text" puts a
   clean, aligned table on your clipboard, ready to paste into a group text. "Download CSV"
   gives you the same numbers in a spreadsheet file.

One thing worth knowing up front: once a week finishes scoring, its payout numbers are frozen.
If someone pays their entry fee late and the pot grows afterward, that already-scored week's
payout does not change. This is deliberate, so a number you already sent someone over Venmo
never quietly changes on you later. If you genuinely need to fix a past week's numbers by hand
(a rule was wrong, for example), that is what "Recalculate" is for, a separate, explicit,
confirmed action, never something that happens automatically.

## Deliberately not built, and what it would take

- A second, more granular "mark this one specific award paid" UI. The route exists
  (`POST /admin/payouts/award/{id}/paid`) but nothing links to it; the payout summary page
  only offers one checkbox per player (marks or unmarks every one of their awards at once).
  Building a per-scope checkbox instead would mean a small template change to the existing
  summary table partial, no new backend work.
- Live, client-side recalculation of the dollar/percent display as a commissioner types. The
  "both representations visible" requirement is satisfied by recomputing both figures on the
  server on every save, not by JavaScript math updating instantly as a value is typed. This
  would need either a small amount of client-side arithmetic duplicating
  `app.payouts.resolve_rule` (a real risk of the two drifting apart) or an HTMX round trip on
  every keystroke, debounced. Neither was worth the added complexity for a screen that is
  edited occasionally, not continuously.
- An admin-triggered "recalculate" button in the UI. `recalculate_awards` exists and is fully
  tested in the service layer, but no route or template button calls it yet; today the only
  way to invoke it is `python -m app.cli payouts-snapshot` combined with a direct service
  call, or a future small addition to `app/routers/payouts.py`. Wiring an actual confirmed
  button to it is a small, self contained follow-up.
- Genuine visual, responsive, and keyboard verification. See Phase 8, items 25 and 26: this
  agent verified every payout screen through a scripted HTTP client, not a browser, so
  360px/768px/1280px legibility and full keyboard/focus-ring operation were reasoned about
  from the CSS and component reuse, not watched by eye. This needs a human, or a browser
  driving agent, to close out for real.
- A second, narrower payout tiebreak rule. `Pool.payout_tiebreak` and `PAYOUT_TIEBREAKS` exist
  as a real, stored, validated setting, but only one value (`earliest_submit`) is implemented;
  the column and the UI control exist specifically so a second rule can be added later without
  a migration.

## Top three risks to this feature in live use, and a recommended mitigation for each

1. The season-complete signal is a guess, not a fact. Season-scope awards (`season_points`,
   `season_wins`) only snapshot automatically when a week marked `is_bowl_week=True` finishes
   scoring. If a commissioner's pool never marks any week as the bowl week, the season awards
   never freeze on their own, and the two panels on Season Standings simply never appear.
   Mitigation: the CLI (`payouts-snapshot --scope season_points`) already exists as an explicit
   fallback exactly for this case; the real fix is making sure this gap is visible rather than
   silent, for example a small banner on Season Standings once the regular season's last week
   is scored but neither season scope has snapshotted yet, prompting the commissioner to run
   it by hand. That banner was not built in this pass and is a good first follow-up.
2. A commissioner can genuinely lose track of which numbers are frozen versus live. The
   "Projected" label only appears on Weekly Results for the current, still-scoring week; every
   other payout figure in the app (the summary page, the season panels, CSV exports) only ever
   reads frozen `PayoutAward` rows. The risk is not incorrect math, it is a commissioner
   assuming the live Set Payouts editor's numbers ("what does 1st place pay today") are the
   same thing as what a specific past week actually paid out, when a rule has since changed.
   Mitigation: the editor screen could show a small note when a rule's current value differs
   from what the most recent frozen award for that scope actually paid, surfacing the drift
   explicitly rather than leaving a commissioner to notice it by comparing two separate pages.
   Not built in this pass, worth adding before this ships to a group that pays real money on
   real numbers.
3. Money type discipline is enforced by convention and tests, not by the database itself.
   `Decimal` end to end is a real, tested, code-review-level guarantee inside
   `app/payouts.py`/`app/services/payouts.py` (the gate's own `grep -rn "float("` check, plus
   every test asserting `isinstance(x, Decimal)`), but nothing at the SQLAlchemy/SQLite layer
   stops a future edit from quietly reintroducing a `float` somewhere in this module. Mitigation:
   the gate's `float(` grep check on these two files should stay a permanent, standing part of
   this project's CI or pre-commit hooks, not just something run once during this build; it is
   cheap, fast, and is the single test most likely to catch a real future regression here
   before it reaches production.
