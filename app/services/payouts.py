"""Database-backed payout service (Payout system rebuild).

This module is being rebuilt on top of app/payouts.py, the new pure dollar-or-percent
allocation engine. The old float, three-scope implementation (allocate_payouts, rules_by_place,
week_payout_scope, week_is_complete, weekly_payouts, season_payouts, payout_summary) is gone
along with the old payout_rules table shape it read; see DECISIONS.md, "Payout system", Phase 0.
The new service functions land in the next phase of this build.
"""

from __future__ import annotations
