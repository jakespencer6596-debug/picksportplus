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

- [ ] Phase 0. Baseline, branch, this report's scaffold.
- [ ] Phase 1. Tab and arrow keys move between confidence values.
- [ ] Phase 2. Season ranking breaks ties on total wins.
- [ ] Phase 3. Define season submission time.
- [ ] Phase 4. Regression sweep.
- [ ] Phase 5. Full verification.
- [ ] Phase 6. Documentation.
- [ ] Phase 7. Merge, push, deploy, verify.

Filled in with commit SHAs, pass/fail detail, and the rest of the required content
(ambiguity decisions, the Phase 5 22-line checklist, the payout worked example, live
deploy confirmation) as each phase actually completes.
