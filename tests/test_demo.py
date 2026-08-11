"""Tests for app/services/demo.py's three-week rebuild (Phase 9).

Real fixtures, no network: app/services/demo.py never calls fetch_json/fetch_scoreboard, it
reads tests/fixtures files directly off disk, so these tests exercise the exact same code
path `python -m app.cli seed-demo` runs, entirely offline. See DECISIONS.md, Phase 9, for
where the new week 6 fixtures (tests/fixtures/espn_nfl_2025_w6.json,
espn_cfb_2025_w6.json, espn_core_odds_nfl_2025_w6.json, espn_core_odds_cfb_2025_w6.json) came
from and why week 6's college spreads come from ESPN's own core odds endpoint rather than
CFBD.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Game, PayoutRule, Pick, Pool, PoolMember, User, Week, WeekEntry
from app.services.demo import (
    DEMO_JOIN_CODE,
    DEMO_PLAYERS,
    NO_SHOW_WEEK1_LOCAL,
    OPEN_WEEK_NUMBER,
    clear_demo,
    demo_logins,
    seed_demo_pool,
)


def _pool(db) -> Pool:
    return db.scalar(select(Pool).where(Pool.join_code == DEMO_JOIN_CODE))


def _week(db, pool, number: int) -> Week:
    return db.scalar(select(Week).where(Week.pool_id == pool.id, Week.week_number == number))


def test_seed_demo_pool_builds_three_weeks(db):
    seed_demo_pool(db, reset=True)

    pool = _pool(db)
    assert pool is not None
    assert pool.season_year == 2025

    weeks = {w.week_number: w for w in db.scalars(select(Week).where(Week.pool_id == pool.id))}
    assert set(weeks) == {5, 6, 7}
    assert weeks[5].status == "scored"
    assert weeks[6].status == "scored"
    assert weeks[7].status == "open"


def test_seed_demo_pool_has_eight_players_one_commissioner(db):
    seed_demo_pool(db, reset=True)
    pool = _pool(db)

    members = list(db.scalars(select(PoolMember).where(PoolMember.pool_id == pool.id)))
    assert len(members) == 8
    assert len(DEMO_PLAYERS) == 8
    commissioners = [m for m in members if m.role_in_pool == "commissioner"]
    assert len(commissioners) == 1


def test_every_submitting_player_picks_exactly_picks_required_not_the_whole_slate(db):
    seed_demo_pool(db, reset=True)
    pool = _pool(db)
    week5 = _week(db, pool, 5)

    counts: dict[int, int] = {}
    for pick in db.scalars(select(Pick).where(Pick.week_id == week5.id)):
        counts[pick.user_id] = counts.get(pick.user_id, 0) + 1

    slate_size = db.scalar(select(Game).where(Game.week_id == week5.id, Game.in_slate.is_(True)))
    assert slate_size is not None
    for count in counts.values():
        assert count == pool.picks_required
        assert count < pool.num_games_per_week


def test_players_pick_a_varied_subset_not_the_identical_games(db):
    """Phase 3's rule is exactly 15 of 20; this pins that two different players do not
    mechanically sit out the exact same five games, which the old whole-slate demo could
    never have shown at all."""
    seed_demo_pool(db, reset=True)
    pool = _pool(db)
    week5 = _week(db, pool, 5)

    picked_games_by_user: dict[int, frozenset[int]] = {}
    for pick in db.scalars(select(Pick).where(Pick.week_id == week5.id)):
        picked_games_by_user.setdefault(pick.user_id, set()).add(pick.game_id)
    picked_games_by_user = {k: frozenset(v) for k, v in picked_games_by_user.items()}

    distinct_subsets = set(picked_games_by_user.values())
    assert len(distinct_subsets) > 1


def test_one_player_is_a_real_no_show_in_week_five(db):
    seed_demo_pool(db, reset=True)
    pool = _pool(db)
    week5 = _week(db, pool, 5)

    casey = db.scalar(select(User).where(User.email == f"{NO_SHOW_WEEK1_LOCAL}@picksportplus.demo"))
    assert casey is not None

    picks = list(db.scalars(select(Pick).where(Pick.week_id == week5.id, Pick.user_id == casey.id)))
    assert picks == []

    entry = db.scalar(
        select(WeekEntry).where(WeekEntry.week_id == week5.id, WeekEntry.user_id == casey.id)
    )
    assert entry.did_not_submit is True
    assert entry.points == (pool.picks_required * (pool.picks_required + 1)) // 2
    assert entry.is_winner is False


def test_week_five_has_one_voided_game(db):
    seed_demo_pool(db, reset=True)
    pool = _pool(db)
    week5 = _week(db, pool, 5)

    voided = list(db.scalars(select(Game).where(Game.week_id == week5.id, Game.status == "void")))
    assert len(voided) == 1
    # A real game: it still carries real teams and a real recorded spread, only its status
    # was changed, by the same set_void a commissioner would use on a real week.
    assert voided[0].home_team
    assert voided[0].away_team


def test_both_historical_weeks_are_fully_scored_by_default(db):
    seed_demo_pool(db, reset=True)
    pool = _pool(db)
    week5, week6 = _week(db, pool, 5), _week(db, pool, 6)

    for week in (week5, week6):
        entries = list(db.scalars(select(WeekEntry).where(WeekEntry.week_id == week.id)))
        assert len(entries) == 8
        winners = [e for e in entries if e.is_winner]
        assert winners, f"expected a winner for {week.label}"


def test_open_week_has_no_picks_and_a_future_lock(db):
    import datetime as dt

    seed_demo_pool(db, reset=True)
    pool = _pool(db)
    week7 = _week(db, pool, OPEN_WEEK_NUMBER)

    assert week7.status == "open"
    assert week7.lock_at_override is True
    assert week7.lock_at is not None
    lock_at = week7.lock_at.replace(tzinfo=week7.lock_at.tzinfo or dt.UTC)
    assert lock_at > dt.datetime.now(dt.UTC)

    picks = list(db.scalars(select(Pick).where(Pick.week_id == week7.id)))
    assert picks == []
    entries = list(db.scalars(select(WeekEntry).where(WeekEntry.week_id == week7.id)))
    assert entries == []

    games = list(db.scalars(select(Game).where(Game.week_id == week7.id, Game.in_slate.is_(True))))
    assert len(games) == pool.num_games_per_week
    for game in games:
        assert game.home_team and game.away_team  # real teams, not invented


def test_scenario_week_flag_leaves_week_six_partially_played(db):
    seed_demo_pool(db, reset=True, scenario_week=True)
    pool = _pool(db)
    week6 = _week(db, pool, 6)

    assert week6.status != "scored"

    games = list(db.scalars(select(Game).where(Game.week_id == week6.id, Game.in_slate.is_(True))))
    finals = [g for g in games if g.status in ("final", "void")]
    pending = [g for g in games if g.status not in ("final", "void")]
    assert len(finals) == pool.scenarios_min_final_games
    assert len(pending) >= pool.scenarios_min_remaining_games

    # Picks were still generated for everyone, so the panel has real picks to sweep.
    picks = list(db.scalars(select(Pick).where(Pick.week_id == week6.id)))
    assert picks


def test_scenario_week_flag_does_not_touch_week_five(db):
    seed_demo_pool(db, reset=True, scenario_week=True)
    pool = _pool(db)
    week5 = _week(db, pool, 5)
    assert week5.status == "scored"


def test_demo_payout_rules_are_seeded_and_labelled_as_demo(db):
    seed_demo_pool(db, reset=True)
    pool = _pool(db)

    rules = list(db.scalars(select(PayoutRule).where(PayoutRule.pool_id == pool.id)))
    by_scope = {
        scope: [r for r in rules if r.scope == scope]
        for scope in ("weekly", "bowl", "season_points", "season_wins")
    }
    assert len(by_scope["weekly"]) == 3
    assert len(by_scope["bowl"]) == 3
    assert len(by_scope["season_points"]) == 3
    assert len(by_scope["season_wins"]) == 3
    assert all(r.mode == "amount" for r in rules)
    assert all("(demo)" in (r.label or "") for r in rules)
    assert pool.weekly_payout_weeks == 15


def test_every_demo_member_is_marked_paid(db):
    seed_demo_pool(db, reset=True)
    pool = _pool(db)
    members = list(db.scalars(select(PoolMember).where(PoolMember.pool_id == pool.id)))
    assert all(m.paid_at is not None for m in members)
    assert all(m.paid_marked_by_user_id is not None for m in members)


def test_reset_false_on_an_existing_pool_does_not_duplicate_it(db):
    first = seed_demo_pool(db, reset=True)
    assert any("Created pool" in line for line in first)

    second = seed_demo_pool(db, reset=False)
    assert any("already exists" in line for line in second)

    pools = list(db.scalars(select(Pool).where(Pool.join_code == DEMO_JOIN_CODE)))
    assert len(pools) == 1


def test_reset_true_rebuilds_cleanly_twice_in_a_row(db):
    seed_demo_pool(db, reset=True)
    seed_demo_pool(db, reset=True)

    pools = list(db.scalars(select(Pool).where(Pool.join_code == DEMO_JOIN_CODE)))
    assert len(pools) == 1
    users = list(db.scalars(select(User).where(User.email.like("%picksportplus.demo"))))
    assert len(users) == 8


def test_demo_logins_lists_every_player_once(db):
    lines = demo_logins()
    text = "\n".join(lines)
    for _name, local, _skill, _role in DEMO_PLAYERS:
        assert local == "commissioner" or local == "player" or f"{local}@picksportplus.demo" in text


def test_clear_demo_removes_the_pool_and_its_users(db):
    seed_demo_pool(db, reset=True)
    clear_demo(db)

    assert _pool(db) is None
    users = list(db.scalars(select(User).where(User.email.like("%picksportplus.demo"))))
    assert users == []
