"""Payout allocation from PayoutRule rows (Phase 7).

"There was a payout column that determined the amount due each user. In settings we
determined what #1, 2, 3, 4 received each week, for the special Bowl Week, and end of season
awards." Two layers here:

- allocate_payouts is the pure tie-splitting arithmetic: rank-ordered (user_id, rank) pairs
  plus a place-to-amount mapping in, a user_id-to-dollars mapping out. No database, no
  knowledge of scoring or standings, mirroring app/slate.py's own "the pure math lives on its
  own" shape.
- Everything else here reads PayoutRule rows and the existing standings/leaderboard services
  (never recomputing rank itself, per the brief) to answer the three real questions this
  phase needs: which scope a given week's payout draws from, whether a week is done enough to
  show a payout column at all, and the season-long summary table.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, PayoutRule, Pool, Week, WeekEntry
from app.services.standings import WeeklyRow, season_standings, weekly_leaderboard

__all__ = [
    "PayoutSummaryRow",
    "allocate_payouts",
    "rules_by_place",
    "week_payout_scope",
    "week_is_complete",
    "weekly_payouts",
    "season_payouts",
    "payout_summary",
]


def allocate_payouts(
    ranked_user_ids: Sequence[tuple[int, int]],
    amounts_by_place: Mapping[int, float],
) -> dict[int, float]:
    """Match ranked rows against a place -> dollar amount mapping.

    ranked_user_ids: (user_id, rank) pairs, already sorted best rank first, and already in
    tie break order *within* a shared rank: the earliest submitter of a tied group must come
    first. Callers build this order (see weekly_payouts and payout_summary below); this
    function only ever groups consecutive equal-rank entries, it never re-sorts.

    rank must use the same competition ranking season_standings/weekly_leaderboard already
    assign (ties share a rank, the next rank skips ahead, see
    app/services/standings.py._assign_ranks): that is exactly what makes a tied group's
    combined amount the sum of amounts_by_place[rank] through
    amounts_by_place[rank + group_size - 1], the precise places a competition-ranked tied
    group occupies. For example a two way tie for 1st occupies places 1 and 2; the next
    player is rank 3, not rank 2.

    amounts_by_place: 1 based place -> dollar amount, sparse (a place with no configured rule
    counts as 0 toward a tied group's combined amount, exactly like an ordinary, unconfigured
    place would). A user_id whose rank sits entirely past every configured place is omitted,
    not given a $0 entry, since this pool wrote no rule for that finish at all.

    Every dollar amount is converted to integer cents before the split (round(amount * 100)),
    divided with divmod, and any remainder cent(s) go to the first entries in ranked_user_ids
    within the tied group, one cent each, until the remainder is used up. That is the exact
    tie split rule this phase implements: "split the combined amount for the tied places
    evenly, rounded to the cent, with any remainder cent(s) going to whichever tied player
    submitted earliest." Working in integer cents rather than repeated float division is what
    makes every split reconcile to the total exactly, with no float rounding surprise.

    Returns {user_id: dollars}, omitting anyone whose computed share rounds to exactly 0.
    """
    if not amounts_by_place or not ranked_user_ids:
        return {}

    max_place = max(amounts_by_place)
    result: dict[int, float] = {}
    i = 0
    n = len(ranked_user_ids)
    while i < n:
        rank = ranked_user_ids[i][1]
        group = [ranked_user_ids[i]]
        j = i + 1
        while j < n and ranked_user_ids[j][1] == rank:
            group.append(ranked_user_ids[j])
            j += 1

        if rank <= max_place:
            places = range(rank, rank + len(group))
            total_cents = sum(round(amounts_by_place.get(place, 0.0) * 100) for place in places)
            share_cents, remainder_cents = divmod(total_cents, len(group))
            for index, (user_id, _rank) in enumerate(group):
                cents = share_cents + (1 if index < remainder_cents else 0)
                if cents:
                    result[user_id] = cents / 100

        i = j
    return result


def rules_by_place(db: Session, pool: Pool, scope: str) -> dict[int, float]:
    """place -> amount for one pool's payout rules of one scope. Empty when none are set,
    which is what tells a caller to render no payout column/panel at all rather than one full
    of zeroes (the pool.payout_rules relationship, cascade deleted with the pool, is never
    seeded; every row is a real, hand entered commissioner decision)."""
    rows = db.scalars(
        select(PayoutRule).where(PayoutRule.pool_id == pool.id, PayoutRule.scope == scope)
    )
    return {row.place: row.amount for row in rows}


def week_payout_scope(week: Week) -> str:
    """Which PayoutRule scope a week's payout column draws from.

    Week.is_bowl_week (Phase 1: true when any enabled league needed the postseason to resolve
    that week's anchor date) always routes to "bowl", even when "weekly" rules also exist for
    the pool, per the brief's own named test case.
    """
    return "bowl" if week.is_bowl_week else "weekly"


def week_is_complete(games: Sequence[Game]) -> bool:
    """Every countable slate game has stopped being playable: nothing left scheduled or
    in_progress. Mirrors app.services.results.score_week_for_pool's own "the week is
    complete" check (the same status values, the same "nothing left playable" shape) rather
    than reinventing a second notion of done, per the brief."""
    return bool(games) and all(g.status in ("final", "void") for g in games)


_NEVER = dt.datetime.max.replace(tzinfo=dt.UTC)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def weekly_payouts(
    db: Session,
    week: Week,
    weekly_rows: Sequence[WeeklyRow],
    rules: Mapping[int, float],
) -> dict[int, float]:
    """Payout amounts for one week's leaderboard, tie broken by earliest WeekEntry.submitted_at.

    weekly_rows is expected to be weekly_leaderboard's own output for this exact week: its
    rank is trusted as is, never recomputed here. A player with no WeekEntry (should not
    happen for anyone weekly_leaderboard lists, but handled defensively) sorts last within
    their tied group, after every real submission time.
    """
    if not rules:
        return {}
    submitted_at = {
        e.user_id: _aware(e.submitted_at)
        for e in db.scalars(select(WeekEntry).where(WeekEntry.week_id == week.id))
    }
    ordered = sorted(
        ((row.user_id, row.rank) for row in weekly_rows),
        key=lambda pair: (pair[1], submitted_at.get(pair[0]) or _NEVER),
    )
    return allocate_payouts(ordered, rules)


def season_payouts(season_rows, rules: Mapping[int, float]) -> dict[int, float]:
    """Payout amounts for the season awards panel.

    season_rows is season_standings' own output, already in its final, fully tie-broken order
    (points, then correct, then weekly wins, then name, see
    app/services/standings.py.season_standings). That existing order is reused directly as the
    tie break for a remainder cent, since nothing at season granularity plays the role
    WeekEntry.submitted_at plays for a single week (there is no single "the season started at
    this instant" per player to compare).
    """
    if not rules:
        return {}
    return allocate_payouts([(row.user_id, row.rank) for row in season_rows], rules)


@dataclass
class PayoutSummaryRow:
    user_id: int
    display_name: str
    weekly_total: float
    bowl_total: float
    season_award: float
    total_owed: float


def payout_summary(db: Session, pool: Pool) -> list[PayoutSummaryRow]:
    """Per player: weekly payouts total (every scored week that had weekly rules), the bowl
    payout, the season award, and the total owed. A plain, copy pasteable table, not a new
    export format: see app/routers/admin.py's /admin/payouts and admin/payouts.html.
    """
    weeks = list(
        db.scalars(
            select(Week).where(
                Week.pool_id == pool.id,
                Week.season_year == pool.season_year,
                Week.status == "scored",
            )
        )
    )
    weekly_rules = rules_by_place(db, pool, "weekly")
    bowl_rules = rules_by_place(db, pool, "bowl")
    season_rules = rules_by_place(db, pool, "season")

    weekly_total: dict[int, float] = {}
    bowl_total: dict[int, float] = {}
    for week in weeks:
        rules = bowl_rules if week.is_bowl_week else weekly_rules
        if not rules:
            continue
        games = list(
            db.scalars(select(Game).where(Game.week_id == week.id, Game.in_slate.is_(True)))
        )
        if not week_is_complete(games):
            continue
        rows, _ = weekly_leaderboard(db, pool, week=week)
        payouts = weekly_payouts(db, week, rows, rules)
        target = bowl_total if week.is_bowl_week else weekly_total
        for user_id, amount in payouts.items():
            target[user_id] = target.get(user_id, 0.0) + amount

    season_rows = season_standings(db, pool)
    season_awards = season_payouts(season_rows, season_rules)

    summary: list[PayoutSummaryRow] = []
    for row in season_rows:
        weekly_amt = round(weekly_total.get(row.user_id, 0.0), 2)
        bowl_amt = round(bowl_total.get(row.user_id, 0.0), 2)
        season_amt = round(season_awards.get(row.user_id, 0.0), 2)
        summary.append(
            PayoutSummaryRow(
                user_id=row.user_id,
                display_name=row.display_name,
                weekly_total=weekly_amt,
                bowl_total=bowl_amt,
                season_award=season_amt,
                total_owed=round(weekly_amt + bowl_amt + season_amt, 2),
            )
        )
    summary.sort(key=lambda r: r.display_name.lower())
    return summary
