"""Tests for Phase 2 of the slate drift incident: rebuild and reopen a published week, and
amend a single game on one that already has picks. See INCIDENT-REPORT.md for the incident
this fixes ("Since people have locked already, I am unable to remove games.") and
DECISIONS.md, "Slate drift incident", for the ambiguous calls made building it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import (
    Game,
    Pick,
    PickArchive,
    Pool,
    PoolMember,
    SlateChange,
    User,
    Week,
    WeekEntry,
)
from app.services import ingest, mail
from app.services.results import score_week_for_pool

UTC = dt.UTC


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Test Pool",
        "join_code": f"CODE{id(overrides) % 100000}",
        "season_year": 2026,
        "num_games_per_week": 2,
        "target_nfl": 1,
        "target_ncaaf": 1,
        "picks_required": 2,
        "sports": ["nfl", "ncaaf"],
        "auto_publish": False,
        "timezone": "America/New_York",
        "current_week": 1,
        "week1_anchor_date": dt.date(2026, 9, 12),
        "scoring_mode": "standard",
    }
    defaults.update(overrides)
    pool = Pool(**defaults)
    db.add(pool)
    db.flush()
    return pool


def _week(db, pool: Pool, status: str = "open") -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=1,
        label="Week 1",
        status=status,
        lock_at=dt.datetime.now(UTC) + dt.timedelta(hours=48),
    )
    db.add(week)
    db.flush()
    return week


def _game(db, week: Week, event_id: str, **overrides) -> Game:
    defaults = {
        "week_id": week.id,
        "league": "nfl",
        "espn_event_id": event_id,
        "start_time": dt.datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        "home_team": f"Home {event_id}",
        "away_team": f"Away {event_id}",
        "home_abbr": "HOM",
        "away_abbr": "AWY",
        "canonical_home_key": f"nfl:home-{event_id}",
        "canonical_away_key": f"nfl:away-{event_id}",
        "spread_home": 1.0,
        "closeness": 1.0,
        "in_slate": True,
        "slate_rank": 1,
        "status": "scheduled",
    }
    defaults.update(overrides)
    game = Game(**defaults)
    db.add(game)
    db.flush()
    return game


def _user(db, email: str, name: str) -> User:
    user = User(email=email, password_hash="x", display_name=name)
    db.add(user)
    db.flush()
    return user


def _pick(db, pool, week, user, game, team="home", confidence=1) -> Pick:
    pick = Pick(
        user_id=user.id,
        pool_id=pool.id,
        week_id=week.id,
        game_id=game.id,
        picked_team=team,
        confidence=confidence,
    )
    db.add(pick)
    db.flush()
    return pick


# Rebuild and reopen -----------------------------------------------------------------------


def test_rebuild_archives_every_pick_before_deleting_and_it_round_trips(db, monkeypatch):
    pool = _pool(db)
    week = _week(db, pool)
    game = _game(db, week, "evt1")
    player = _user(db, "player@example.com", "Player One")
    pick = _pick(db, pool, week, player, game, team="home", confidence=1)
    original_created_at = pick.created_at

    monkeypatch.setattr(
        ingest,
        "fetch_candidates",
        lambda db, pool, week, **kwargs: ([], []),
    )
    result = ingest.rebuild_and_reopen_week(db, pool, week, actor_user_id=None)

    assert result.picks_archived == 1
    assert db.scalar(select(Pick).where(Pick.week_id == week.id)) is None

    archived = db.scalar(select(PickArchive).where(PickArchive.week_id == week.id))
    assert archived is not None
    assert archived.user_id == player.id
    assert archived.game_id == game.id
    assert archived.picked_team == "home"
    assert archived.confidence == 1
    assert archived.reason == "rebuild"
    # SQLite hands datetimes back naive, so compare on wall-clock value only.
    assert archived.original_submitted_at.replace(tzinfo=None) == original_created_at.replace(
        tzinfo=None
    )


def test_rebuild_returns_the_week_to_draft_and_does_not_auto_publish(db, monkeypatch):
    pool = _pool(db, auto_publish=True)  # even an auto_publish pool must not auto-publish here
    week = _week(db, pool)
    _game(db, week, "evt1")
    monkeypatch.setattr(ingest, "fetch_candidates", lambda db, pool, week, **kwargs: ([], []))

    ingest.rebuild_and_reopen_week(db, pool, week, actor_user_id=None)

    assert week.status == "draft"


def test_rebuild_on_a_week_with_zero_picks_skips_archiving_cleanly(db, monkeypatch):
    pool = _pool(db)
    week = _week(db, pool)
    _game(db, week, "evt1")
    monkeypatch.setattr(ingest, "fetch_candidates", lambda db, pool, week, **kwargs: ([], []))

    result = ingest.rebuild_and_reopen_week(db, pool, week, actor_user_id=None)

    assert result.picks_archived == 0
    assert week.rebuilt_at is None  # nothing to notify players about


def test_rebuild_writes_a_slate_change_row(db, monkeypatch):
    pool = _pool(db)
    week = _week(db, pool)
    _game(db, week, "evt1")
    boss = _user(db, "boss@example.com", "Boss")
    monkeypatch.setattr(ingest, "fetch_candidates", lambda db, pool, week, **kwargs: ([], []))

    ingest.rebuild_and_reopen_week(db, pool, week, actor_user_id=boss.id)

    row = db.scalar(
        select(SlateChange).where(SlateChange.week_id == week.id, SlateChange.action == "rebuilt")
    )
    assert row is not None
    assert row.actor_user_id == boss.id
    assert row.source == "commissioner"


def test_republishing_a_rebuilt_week_notifies_every_member(db, monkeypatch):
    monkeypatch.setattr(settings, "mail_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "test-key")
    monkeypatch.setattr(settings, "mail_from_address", "noreply@example.com")
    monkeypatch.setattr(settings, "mail_rate_limit_per_hour", 20)
    sent = []
    monkeypatch.setattr(mail, "_call_resend_api", lambda **kwargs: sent.append(kwargs) or None)

    pool = _pool(db, notify_week_published=False)  # deliberately off: this notice is unconditional
    week = _week(db, pool, status="open")
    game = _game(db, week, "evt1")
    p1 = _user(db, "p1@example.com", "Player One")
    p2 = _user(db, "p2@example.com", "Player Two")
    db.add_all(
        [
            PoolMember(pool_id=pool.id, user_id=p1.id, role_in_pool="member"),
            PoolMember(pool_id=pool.id, user_id=p2.id, role_in_pool="member"),
        ]
    )
    db.flush()
    _pick(db, pool, week, p1, game)
    monkeypatch.setattr(ingest, "fetch_candidates", lambda db, pool, week, **kwargs: ([], []))
    ingest.rebuild_and_reopen_week(db, pool, week, actor_user_id=None)
    assert week.status == "draft"

    warnings = ingest.publish_week(db, week)

    assert warnings == []
    assert len(sent) == 2
    recipients = {call["to"] for call in sent}
    assert recipients == {"p1@example.com", "p2@example.com"}
    assert any("changed" in call["subject"].lower() for call in sent)


def test_mail_failure_on_republish_surfaces_copyable_text_not_success(db, monkeypatch):
    monkeypatch.setattr(settings, "mail_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "test-key")
    monkeypatch.setattr(settings, "mail_from_address", "noreply@example.com")
    monkeypatch.setattr(settings, "mail_rate_limit_per_hour", 20)

    def _boom(**kwargs):
        raise mail.MailSendFailed("provider exploded")

    monkeypatch.setattr(mail, "_call_resend_api", _boom)

    pool = _pool(db)
    week = _week(db, pool, status="open")
    game = _game(db, week, "evt1")
    player = _user(db, "player@example.com", "Player One")
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    db.flush()
    _pick(db, pool, week, player, game)
    monkeypatch.setattr(ingest, "fetch_candidates", lambda db, pool, week, **kwargs: ([], []))
    ingest.rebuild_and_reopen_week(db, pool, week, actor_user_id=None)

    warnings = ingest.publish_week(db, week)

    assert any("Could not email" in w for w in warnings)
    assert any("Copy this message and send it yourself" in w for w in warnings)
    assert any("Subject:" in w for w in warnings)


def test_player_needs_repick_after_rebuild_clears_once_they_pick_again(db, monkeypatch):
    pool = _pool(db)
    week = _week(db, pool, status="open")
    game = _game(db, week, "evt1")
    player = _user(db, "player@example.com", "Player One")
    _pick(db, pool, week, player, game)
    monkeypatch.setattr(ingest, "fetch_candidates", lambda db, pool, week, **kwargs: ([], []))
    ingest.rebuild_and_reopen_week(db, pool, week, actor_user_id=None)

    assert ingest.player_needs_repick_after_rebuild(db, week, player.id) is True

    _pick(db, pool, week, player, game)  # fresh pick since rebuild wiped the live table

    assert ingest.player_needs_repick_after_rebuild(db, week, player.id) is False


def test_player_never_needs_a_repick_banner_when_the_week_was_never_rebuilt(db):
    pool = _pool(db)
    week = _week(db, pool)
    player = _user(db, "player@example.com", "Player One")

    assert ingest.player_needs_repick_after_rebuild(db, week, player.id) is False


# Amend a single game -----------------------------------------------------------------------


def test_amend_remove_voids_only_picks_on_that_game_leaves_others_intact(db):
    pool = _pool(db, num_games_per_week=2, picks_required=2)
    week = _week(db, pool)
    removed_game = _game(db, week, "evt1", slate_rank=1)
    kept_game = _game(db, week, "evt2", league="ncaaf", slate_rank=2)
    player = _user(db, "player@example.com", "Player One")
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    _pick(db, pool, week, player, removed_game, team="home", confidence=2)
    _pick(db, pool, week, player, kept_game, team="away", confidence=1)
    db.flush()

    game, affected = ingest.amend_published_game(
        db, week, removed_game.id, "remove", actor_user_id=None
    )

    assert affected == 1
    assert game.in_slate is False
    remaining_picks = list(db.scalars(select(Pick).where(Pick.week_id == week.id)))
    assert len(remaining_picks) == 2  # neither pick row itself is deleted
    assert any(p.game_id == kept_game.id for p in remaining_picks)


def test_amend_remove_can_run_even_though_the_ordinary_remove_button_is_locked(db):
    pool = _pool(db, num_games_per_week=2, picks_required=2)
    week = _week(db, pool)
    game = _game(db, week, "evt1")
    player = _user(db, "player@example.com", "Player One")
    _pick(db, pool, week, player, game)

    assert ingest.can_resize_slate(db, week) is False
    with pytest.raises(ingest.SlateLocked):
        ingest.remove_from_slate(db, week, game.id)

    # amend_published_game bypasses that exact lock.
    amended, _affected = ingest.amend_published_game(db, week, game.id, "remove")
    assert amended.in_slate is False


def test_amended_voided_pick_scores_zero_and_reduces_only_that_players_possible(db):
    pool = _pool(db, num_games_per_week=2, picks_required=2, scoring_mode="standard")
    week = _week(db, pool)
    amended_game = _game(db, week, "evt1", slate_rank=1)
    other_game = _game(
        db,
        week,
        "evt2",
        league="ncaaf",
        slate_rank=2,
        status="final",
        home_score=10,
        away_score=3,
        winner="home",
    )
    p1 = _user(db, "p1@example.com", "Player One")
    p2 = _user(db, "p2@example.com", "Player Two")
    db.add_all(
        [
            PoolMember(pool_id=pool.id, user_id=p1.id, role_in_pool="member"),
            PoolMember(pool_id=pool.id, user_id=p2.id, role_in_pool="member"),
        ]
    )
    _pick(db, pool, week, p1, amended_game, team="home", confidence=2)
    _pick(db, pool, week, p1, other_game, team="home", confidence=1)
    _pick(db, pool, week, p2, amended_game, team="home", confidence=1)
    _pick(db, pool, week, p2, other_game, team="home", confidence=2)
    db.flush()

    ingest.amend_published_game(db, week, amended_game.id, "remove")
    db.commit()

    score_week_for_pool(db, pool, week)

    entry1 = db.scalar(
        select(WeekEntry).where(WeekEntry.week_id == week.id, WeekEntry.user_id == p1.id)
    )
    entry2 = db.scalar(
        select(WeekEntry).where(WeekEntry.week_id == week.id, WeekEntry.user_id == p2.id)
    )
    # Both players picked the amended game; app.scoring ignores a pick whose game left the
    # slate, so only the still-scored game (evt2, both picked home, both correct) counts:
    # standard mode, 1 point for p1 (confidence 1 on evt2), 2 points for p2 (confidence 2).
    assert entry1.possible == 1
    assert entry1.correct == 1
    assert entry1.points == 1
    assert entry2.possible == 1
    assert entry2.correct == 1
    assert entry2.points == 2


def test_amend_swap_writes_a_slate_change_row(db):
    pool = _pool(db, num_games_per_week=1, picks_required=1)
    week = _week(db, pool)
    out_game = _game(db, week, "evt1", in_slate=True, slate_rank=1)
    in_game = _game(db, week, "evt2", in_slate=False)
    player = _user(db, "player@example.com", "Player One")
    _pick(db, pool, week, player, out_game)

    ingest.amend_published_game(db, week, out_game.id, "swap", swap_with_id=in_game.id)

    row = db.scalar(
        select(SlateChange).where(SlateChange.week_id == week.id, SlateChange.action == "swapped")
    )
    assert row is not None
    assert out_game.in_slate is False
    assert in_game.in_slate is True


def test_amend_unknown_action_raises(db):
    pool = _pool(db)
    week = _week(db, pool)
    game = _game(db, week, "evt1")

    with pytest.raises(ValueError):
        ingest.amend_published_game(db, week, game.id, "void")
