"""The 120 point bug (standings and ties, September).

_cron_pass scored every week whose status was "open" or "locked". score_week_for_pool wrote a
WeekEntry for every member, and app.scoring.score_week gave any member with no picks the
maximum inverse-mode penalty (120 at 15 picks) unconditionally, even on a week whose own
lock_at had not passed yet. Week 2 being open with nobody's picks final meant every member was
written as a no-show, and _season_base_rows summed every WeekEntry regardless of the week's
own state, so everyone's season total jumped by 120 the moment week 2 opened.

The fix: before a week's own lock_at passes, a member with no picks scores 0, not a no-show
(app/services/results.py.score_week_for_pool). Existing bad rows already written under the old
behavior are corrected at read time, never by rewriting the stored row
(app/services/standings.py._effective_points/_effective_did_not_submit).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.models import Game, PayoutAward, PayoutRule, Pick, Pool, PoolMember, User, Week, WeekEntry
from app.services.results import score_week_for_pool
from app.services.standings import season_live_weeks, season_standings

UTC = dt.UTC


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "No Show Timing Pool",
        "join_code": f"NOSHOW{id(overrides) % 100000}",
        "season_year": 2026,
        "num_games_per_week": 4,
        "target_nfl": 4,
        "target_ncaaf": 0,
        "picks_required": 4,
        "sports": ["nfl"],
        "auto_publish": True,
        "open_registration": False,
        "timezone": "America/New_York",
        "current_week": 1,
    }
    defaults.update(overrides)
    pool = Pool(**defaults)
    db.add(pool)
    db.flush()
    return pool


def _user(db, email, name) -> User:
    from app.auth import hash_password

    user = User(email=email, password_hash=hash_password("hunter2hunter2"), display_name=name)
    db.add(user)
    db.flush()
    return user


def _week(db, pool, *, week_number, status="open", lock_in_hours) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=week_number,
        label=f"Week {week_number}",
        status=status,
        lock_at=dt.datetime.now(UTC) + dt.timedelta(hours=lock_in_hours),
    )
    db.add(week)
    db.flush()
    return week


def _game(db, week, abbr, *, rank, status="scheduled", winner=None) -> Game:
    game = Game(
        week_id=week.id,
        league="nfl",
        espn_event_id=f"noshow-{abbr}-{week.id}",
        start_time=dt.datetime.now(UTC) + dt.timedelta(hours=1),
        home_team=abbr,
        away_team=f"Opp{abbr}",
        home_abbr=abbr,
        away_abbr=f"O{abbr}",
        canonical_home_key=f"nfl:{abbr.lower()}",
        canonical_away_key=f"nfl:o{abbr.lower()}",
        spread_home=-3.0,
        closeness=3.0,
        spread_source="espn",
        in_slate=True,
        slate_rank=rank,
        status=status,
        winner=winner,
    )
    db.add(game)
    db.flush()
    return game


def _pick(db, pool, week, user, game, *, confidence, picked_team="home"):
    now = dt.datetime.now(UTC)
    db.add(
        Pick(
            user_id=user.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=game.id,
            picked_team=picked_team,
            confidence=confidence,
            created_at=now,
            updated_at=now,
        )
    )


def _members(db, pool, *users):
    for i, user in enumerate(users):
        role = "commissioner" if i == 0 else "member"
        db.add(PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool=role))
    db.flush()


def test_open_week_before_lock_no_picks_scores_zero_not_a_no_show(db):
    """The bug, exactly: a week that is open, lock_at still in the future, and a member with
    no picks at all must score 0, not the maximum inverse penalty, and must not be flagged a
    no-show. This is the case that, before the fix, wrote everyone a phantom 120."""
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    quiet = _user(db, "quiet@example.com", "Quiet Player")
    _members(db, pool, boss, quiet)
    week = _week(db, pool, week_number=2, status="open", lock_in_hours=48)
    for i in range(4):
        _game(db, week, f"G{i}", rank=i + 1)

    report = score_week_for_pool(db, pool, week)
    db.commit()

    assert report.players == 2
    entry = db.scalar(
        select(WeekEntry).where(WeekEntry.week_id == week.id, WeekEntry.user_id == quiet.id)
    )
    assert entry is not None
    assert entry.points == 0
    assert entry.did_not_submit is False

    rows = {r.user_id: r for r in season_standings(db, pool)}
    assert rows[quiet.id].points == 0


def test_locked_week_no_picks_takes_the_max_penalty(db):
    """The same week, but lock_at has already passed: now a no-show really is a no-show and
    takes the full inverse-mode penalty, sum(1..picks_required) = 10 at 4 picks."""
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    quiet = _user(db, "quiet@example.com", "Quiet Player")
    _members(db, pool, boss, quiet)
    week = _week(db, pool, week_number=2, status="locked", lock_in_hours=-1)
    for i in range(4):
        _game(db, week, f"G{i}", rank=i + 1)

    report = score_week_for_pool(db, pool, week)
    db.commit()

    assert report.players == 2
    entry = db.scalar(
        select(WeekEntry).where(WeekEntry.week_id == week.id, WeekEntry.user_id == quiet.id)
    )
    assert entry.points == 10
    assert entry.did_not_submit is True


def test_locked_week_submitters_score_only_final_games(db):
    """A locked week with some games final and some still pending: a submitter's points
    reflect only the final games so far, the ordinary live-scoring rule, unaffected by the
    no-show timing fix."""
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    player = _user(db, "player@example.com", "Player")
    _members(db, pool, boss, player)
    week = _week(db, pool, week_number=2, status="locked", lock_in_hours=-1)
    g1 = _game(db, week, "G0", rank=1, status="final", winner="away")  # player picked home, wrong
    g2 = _game(db, week, "G1", rank=2, status="final", winner="home")  # player picked home, right
    g3 = _game(db, week, "G2", rank=3, status="scheduled")
    g4 = _game(db, week, "G3", rank=4, status="scheduled")
    _pick(db, pool, week, player, g1, confidence=4)
    _pick(db, pool, week, player, g2, confidence=3)
    _pick(db, pool, week, player, g3, confidence=2)
    _pick(db, pool, week, player, g4, confidence=1)

    report = score_week_for_pool(db, pool, week)
    db.commit()

    entry = db.scalar(
        select(WeekEntry).where(WeekEntry.week_id == week.id, WeekEntry.user_id == player.id)
    )
    # inverse mode: wrong pick (g1, confidence 4) charges 4 against; correct pick (g2) charges
    # nothing; the two still-pending games are not countable yet.
    assert entry.points == 4
    assert entry.possible == 2
    assert entry.correct == 1
    assert report.week_complete is False


def test_season_points_week1_final_week2_in_progress_no_phantom_penalty(db):
    """Season points equal week 1 (final) plus the live week 2 figure, never plus 120 per
    member just because week 2 is open. This is the season-level version of the bug."""
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    player = _user(db, "player@example.com", "Player")
    quiet = _user(db, "quiet@example.com", "Quiet Player")
    _members(db, pool, boss, player, quiet)

    week1 = _week(db, pool, week_number=1, status="locked", lock_in_hours=-100)
    w1_games = [
        _game(db, week1, f"W1G{i}", rank=i + 1, status="final", winner="home") for i in range(4)
    ]
    for i, game in enumerate(w1_games):
        _pick(db, pool, week1, player, game, confidence=i + 1)
        _pick(db, pool, week1, quiet, game, confidence=i + 1)
    score_week_for_pool(db, pool, week1)
    db.commit()
    week1_points = {
        e.user_id: e.points
        for e in db.scalars(select(WeekEntry).where(WeekEntry.week_id == week1.id))
    }

    week2 = _week(db, pool, week_number=2, status="open", lock_in_hours=48)
    w2_games = [_game(db, week2, f"W2G{i}", rank=i + 1) for i in range(4)]
    for i, game in enumerate(w2_games):
        _pick(db, pool, week2, player, game, confidence=i + 1)
    # quiet submits nothing for week 2, on purpose: this is the exact shape that produced the
    # phantom 120.
    score_week_for_pool(db, pool, week2)
    db.commit()

    rows = {r.user_id: r for r in season_standings(db, pool)}
    assert rows[player.id].points == week1_points[player.id] + 0
    assert rows[quiet.id].points == week1_points[quiet.id] + 0, (
        "quiet's season total must equal week 1 alone, never week 1 plus a 120 point penalty "
        "for a week that has not locked yet"
    )

    assert season_live_weeks(db, pool) == [2]


def test_no_weekly_winner_or_payout_award_on_an_unfinished_week(db):
    """No weekly winner flag and no PayoutAward snapshot is ever written for an open week or
    a locked week with games still pending, even once some games have gone final."""
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    player = _user(db, "player@example.com", "Player")
    _members(db, pool, boss, player)
    week = _week(db, pool, week_number=2, status="locked", lock_in_hours=-1)
    g1 = _game(db, week, "G0", rank=1, status="final", winner="home")
    g2 = _game(db, week, "G1", rank=2, status="scheduled")
    g3 = _game(db, week, "G2", rank=3, status="scheduled")
    g4 = _game(db, week, "G3", rank=4, status="scheduled")
    for i, game in enumerate([g1, g2, g3, g4]):
        _pick(db, pool, week, player, game, confidence=i + 1)
    db.add(
        PayoutRule(pool_id=pool.id, scope="weekly", place=1, mode="amount", value=Decimal("105"))
    )
    db.commit()

    report = score_week_for_pool(db, pool, week)
    db.commit()

    assert report.week_complete is False
    entry = db.scalar(
        select(WeekEntry).where(WeekEntry.week_id == week.id, WeekEntry.user_id == player.id)
    )
    assert entry.is_winner is False
    awards = list(db.scalars(select(PayoutAward).where(PayoutAward.pool_id == pool.id)))
    assert awards == []


def test_season_live_weeks_empty_once_every_started_week_is_fully_scored(db):
    """The "in progress" label must not render once every week that has entries has actually
    finished scoring: season_live_weeks returns empty in the ordinary settled case."""
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    player = _user(db, "player@example.com", "Player")
    _members(db, pool, boss, player)
    week = _week(db, pool, week_number=1, status="locked", lock_in_hours=-100)
    for i in range(4):
        game = _game(db, week, f"G{i}", rank=i + 1, status="final", winner="home")
        _pick(db, pool, week, player, game, confidence=i + 1)
    score_week_for_pool(db, pool, week)
    db.commit()

    assert week.status == "scored"
    assert season_live_weeks(db, pool) == []


def test_stale_phantom_120_row_reads_as_zero_without_the_stored_row_changing(db):
    """A WeekEntry already holding the old, incorrect penalty (written before this fix
    deployed, on a week whose lock had not passed at the time) is read back as 0 and not a
    no-show, but the stored row itself is never rewritten by the read path. Corrections happen
    at read time, not by rewriting rows.
    """
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    quiet = _user(db, "quiet@example.com", "Quiet Player")
    _members(db, pool, boss, quiet)
    week = _week(db, pool, week_number=2, status="open", lock_in_hours=48)
    for i in range(4):
        _game(db, week, f"G{i}", rank=i + 1)

    # Simulate the pre-fix bug directly: write the phantom penalty by hand, bypassing
    # score_week_for_pool entirely, exactly what the old code path used to leave behind.
    stale = WeekEntry(
        user_id=quiet.id,
        pool_id=pool.id,
        week_id=week.id,
        points=10,
        correct=0,
        possible=0,
        did_not_submit=True,
    )
    db.add(stale)
    db.commit()

    rows = {r.user_id: r for r in season_standings(db, pool)}
    assert rows[quiet.id].points == 0

    # The stored row itself is untouched: still 10, still flagged did_not_submit, exactly as
    # a real repair-at-read approach requires.
    db.refresh(stale)
    assert stale.points == 10
    assert stale.did_not_submit is True
