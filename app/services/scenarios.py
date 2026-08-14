"""Database-touching caller for app/scenarios.py (Phase 8).

app/scenarios.py is the pure engine: no database, no network, no imports from app.models
(see its own module docstring for why). This module is the other half, the one allowed to
touch the database: it pulls a week's Game and Pick rows, builds the plain dataclasses the
pure engine expects, calls into it, and hands back something a route can render. Keeping
the two in separate modules is what lets the engine stay unit tested without a database
session anywhere near it, exactly like app/services/results.py already does for
app/scoring.py.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, Pick, Pool, PoolMember, User, Week
from app.scenarios import (
    RemainingGame,
    ScenarioReport,
    StandingsRow,
    build_custom_scenario,
    compute_scenario_report,
)
from app.scoring import GameOutcome, PickInput

# In-process cache -------------------------------------------------------------
#
# Keyed on (week_id, a frozenset of every one of the week's countable games' (id, status,
# winner), the pool's scoring_mode and picks_required, the probability model, and which
# players' leverage/representative scenarios were requested), so a repeated request for the
# same week in the same "final outcomes" state never recomputes, and a genuinely different
# state (a score just went final, a commissioner voided a game) always gets a fresh key and
# so can never be served a stale answer.
#
# A plain module level dict, exactly the pattern the brief calls out as sufficient: this is
# a single pool sized product (one process, a season of at most a few dozen weeks), not a
# multi tenant cache that needs bounding. Eviction story: it never evicts within a process
# lifetime. That is a deliberately simple, honest answer rather than an LRU or a TTL,
# because staleness is already structurally impossible here (the key changes the instant
# the outcomes it depends on change), so the only cost of never evicting is a few stale,
# unreachable dict entries sitting in memory across a season, not a correctness risk.
_CACHE: dict[tuple, ScenarioReport] = {}


def _outcomes_key(games: Sequence[Game]) -> frozenset[tuple[int, str, str | None]]:
    return frozenset((g.id, g.status, g.winner) for g in games)


@dataclass(frozen=True)
class ScenarioPanelData:
    """Everything the Weekly Results route needs to render the scenarios panel."""

    visible: bool
    """False before the week has reached both configured thresholds; the route renders the
    pending state instead of the panel in that case, and report is always None."""

    final_count: int
    remaining_count: int
    min_final_games: int
    min_remaining_games: int

    report: ScenarioReport | None
    remaining_games: list[Game]
    """The Game rows behind report.remaining_game_ids, in the same order, so a template can
    render a matchup label next to each leverage row without a second query."""


def _week_games(db: Session, week: Week) -> list[Game]:
    return list(
        db.scalars(
            select(Game)
            .where(Game.week_id == week.id, Game.in_slate.is_(True))
            .order_by(Game.slate_rank)
        )
    )


def _member_picks(db: Session, pool: Pool, week: Week) -> dict[int, list[PickInput]]:
    """Every member's picks for the week, keyed by user id. A member with no picks at all
    (a no-show) still gets an entry, an empty list, so app.scenarios sees them and applies
    the no-show penalty rather than silently omitting them from the report."""
    member_ids = list(db.scalars(select(PoolMember.user_id).where(PoolMember.pool_id == pool.id)))
    by_user: dict[int, list[PickInput]] = {uid: [] for uid in member_ids}
    for p in db.scalars(select(Pick).where(Pick.week_id == week.id)):
        by_user.setdefault(p.user_id, []).append(
            PickInput(game_id=p.game_id, picked_team=p.picked_team, confidence=p.confidence)
        )
    return by_user


def _split_outcomes_and_remaining(
    games: Sequence[Game],
) -> tuple[list[GameOutcome], list[Game]]:
    """Final and void games become GameOutcomes (score_week's own is_countable already
    treats void as uncountable, so a void game folds in without special casing here,
    exactly how app/services/results.py's score_week_for_pool already builds its own
    outcome list). Anything still scheduled or in progress is a remaining game."""
    outcomes: list[GameOutcome] = []
    remaining: list[Game] = []
    for g in games:
        if g.status in ("final", "void"):
            outcomes.append(GameOutcome(game_id=g.id, status=g.status, winner=g.winner))
        else:
            remaining.append(g)
    return outcomes, remaining


def panel_thresholds_met(pool: Pool, games: Sequence[Game]) -> tuple[bool, int, int]:
    """(visible, final_count, remaining_count), the cheap check the route runs before ever
    building a full scenario report. final_count counts final AND void games (both have
    stopped being playable, exactly what score_week_for_pool's own "week is complete" check
    already means by a game that is done); remaining_count is everything still to play."""
    final_count = sum(1 for g in games if g.status in ("final", "void"))
    remaining_count = len(games) - final_count
    visible = (
        final_count >= pool.scenarios_min_final_games
        and remaining_count >= pool.scenarios_min_remaining_games
    )
    return visible, final_count, remaining_count


def week_scenario_panel(
    db: Session,
    pool: Pool,
    week: Week,
    *,
    probability_model: str = "even",
    representative_for: Sequence[int] = (),
    seed: int = 0,
) -> ScenarioPanelData:
    """The scenarios panel's full data for one week, cached per the module docstring above.

    Returns visible=False (report=None) without doing any of the real work when the week
    has not yet reached both of the pool's configured thresholds; the sweep only ever runs
    once the panel is actually going to be shown.

    A test week (Phase 3, preseason and test week support) always returns visible=False,
    regardless of how many of its games have gone final: the scenarios engine is a season-wide
    feature (placement odds feed into weekly-win counts and payout scopes, both of which a
    test week is quarantined from), so it is skipped outright rather than computed and then
    hidden only by the template.
    """
    games = _week_games(db, week)
    if week.is_test_week:
        final_count = sum(1 for g in games if g.status in ("final", "void"))
        return ScenarioPanelData(
            visible=False,
            final_count=final_count,
            remaining_count=len(games) - final_count,
            min_final_games=pool.scenarios_min_final_games,
            min_remaining_games=pool.scenarios_min_remaining_games,
            report=None,
            remaining_games=[],
        )
    visible, final_count, remaining_count = panel_thresholds_met(pool, games)
    if not visible:
        return ScenarioPanelData(
            visible=False,
            final_count=final_count,
            remaining_count=remaining_count,
            min_final_games=pool.scenarios_min_final_games,
            min_remaining_games=pool.scenarios_min_remaining_games,
            report=None,
            remaining_games=[],
        )

    final_outcomes, remaining_rows = _split_outcomes_and_remaining(games)
    remaining_games = [
        RemainingGame(
            game_id=g.id,
            home_moneyline=g.home_moneyline,
            away_moneyline=g.away_moneyline,
            spread_home=g.spread_home,
            league=g.league,
        )
        for g in remaining_rows
    ]
    picks_by_player = _member_picks(db, pool, week)

    key = (
        week.id,
        _outcomes_key(games),
        pool.scoring_mode,
        pool.picks_required,
        probability_model,
        tuple(sorted(representative_for)),
        seed,
    )
    report = _CACHE.get(key)
    if report is None:
        report = compute_scenario_report(
            picks_by_player,
            final_outcomes,
            remaining_games,
            mode=pool.scoring_mode,
            picks_required=pool.picks_required,
            probability_model=probability_model,
            seed=seed,
            representative_for=representative_for,
        )
        _CACHE[key] = report

    return ScenarioPanelData(
        visible=True,
        final_count=final_count,
        remaining_count=remaining_count,
        min_final_games=pool.scenarios_min_final_games,
        min_remaining_games=pool.scenarios_min_remaining_games,
        report=report,
        remaining_games=remaining_rows,
    )


@dataclass(frozen=True)
class CustomScenarioRow:
    """One row of a build-your-own-scenario standings table, with a display name attached
    (StandingsRow itself only carries user_id, since app/scenarios.py never touches User)."""

    user_id: int
    display_name: str
    row: StandingsRow


def custom_scenario_standings(
    db: Session,
    pool: Pool,
    week: Week,
    fixed_remaining: Mapping[int, str | None],
) -> list[CustomScenarioRow]:
    """Live standings under a caller supplied assumption for some or all remaining games.

    One pass, not cached: this is a "what if" the commissioner or a player is actively
    building interactively (the three state control per remaining game), so it is expected
    to be called with different fixed_remaining values in quick succession, and
    build_custom_scenario is already cheap (a single score_week per player, see its own
    docstring), unlike the full scenario sweep above.
    """
    games = _week_games(db, week)
    final_outcomes, _remaining_rows = _split_outcomes_and_remaining(games)
    picks_by_player = _member_picks(db, pool, week)
    rows = build_custom_scenario(
        picks_by_player,
        final_outcomes,
        fixed_remaining,
        mode=pool.scoring_mode,
        picks_required=pool.picks_required,
    )

    members = {
        u.id: u
        for u in db.scalars(
            select(User)
            .join(PoolMember, PoolMember.user_id == User.id)
            .where(PoolMember.pool_id == pool.id)
        )
    }
    return [
        CustomScenarioRow(
            user_id=row.user_id,
            display_name=members[row.user_id].display_name if row.user_id in members else "Unknown",
            row=row,
        )
        for row in rows
    ]


__all__ = [
    "ScenarioPanelData",
    "CustomScenarioRow",
    "panel_thresholds_met",
    "week_scenario_panel",
    "custom_scenario_standings",
]
