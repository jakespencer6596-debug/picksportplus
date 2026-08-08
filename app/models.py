"""SQLAlchemy models. Kept database agnostic so SQLite and Postgres both work."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# Status and role values are plain strings rather than SQL enums so that adding a
# value later does not need a Postgres type migration.
WEEK_STATUSES = ("draft", "open", "locked", "scored")
GAME_STATUSES = ("scheduled", "in_progress", "final", "void")
LEAGUES = ("nfl", "ncaaf")
SPREAD_SOURCES = ("espn", "espn_core", "odds_api", "cfbd", "manual")
# "inverse": wrong picks count against the player, lowest total wins. This is the pool's
# real rule and the default. "standard": correct picks earn points, highest total wins,
# kept switchable per pool. See app/scoring.py.
SCORING_MODES = ("standard", "inverse")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Always stored lowercase. See auth.normalize_email.
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="player", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )

    memberships: Mapped[list[PoolMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    picks: Mapped[list[Pick]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Pool(Base):
    __tablename__ = "pools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    join_code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)

    # Slate size. All three are commissioner settings. The per league numbers are targets:
    # the closest target_nfl NFL games and the closest target_ncaaf college games. When one
    # league is short of games with a resolvable spread, the shortfall is filled from the
    # other league so num_games_per_week is still met.
    num_games_per_week: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    target_nfl: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    target_ncaaf: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    # How many of the num_games_per_week slate games a player must pick, exactly, confidence
    # 1..picks_required. Must stay between 1 and num_games_per_week (settings_save enforces
    # this); a pool that wants to require the whole slate just sets this equal to
    # num_games_per_week. See app/scoring.py for how this feeds validate_picks and score_week.
    picks_required: Mapped[int] = mapped_column(Integer, default=15, nullable=False)

    sports: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["nfl", "ncaaf"], nullable=False
    )
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Per pool override of the OPEN_REGISTRATION env default, owned by the commissioner.
    open_registration: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_week: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # The commissioner-set Saturday that pool week 1 anchors to for the season. Pool week N's
    # own anchor is week1_anchor_date + (N - 1) weeks. Nullable because a pool created before
    # this feature, or one nobody has configured yet, has none: see app/services/ingest.py
    # detect_week for the fallback that keeps such a pool working without it.
    week1_anchor_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # "inverse" (default): wrong picks count their staked confidence against the player,
    # lowest total wins. "standard": correct picks earn their staked confidence, highest
    # total wins. See app/scoring.py for the scoring functions both modes run through.
    scoring_mode: Mapped[str] = mapped_column(String(16), default="inverse", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )

    members: Mapped[list[PoolMember]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )
    weeks: Mapped[list[Week]] = relationship(back_populates="pool", cascade="all, delete-orphan")

    @property
    def league_targets(self) -> dict[str, int]:
        """Per league target counts, limited to the leagues this pool actually plays."""
        raw = {"nfl": self.target_nfl, "ncaaf": self.target_ncaaf}
        enabled = set(self.sports or ["nfl", "ncaaf"])
        return {k: v for k, v in raw.items() if k in enabled}

    @property
    def targets_match_total(self) -> bool:
        return sum(self.league_targets.values()) == self.num_games_per_week


class PoolMember(Base):
    __tablename__ = "pool_members"
    __table_args__ = (UniqueConstraint("pool_id", "user_id", name="uq_pool_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("pools.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role_in_pool: Mapped[str] = mapped_column(String(16), default="member", nullable=False)
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )

    pool: Mapped[Pool] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")

    @property
    def is_commissioner(self) -> bool:
        return self.role_in_pool == "commissioner"


class Week(Base):
    __tablename__ = "weeks"
    __table_args__ = (
        UniqueConstraint("pool_id", "season_year", "week_number", name="uq_pool_season_week"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("pools.id", ondelete="CASCADE"), index=True, nullable=False
    )
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    # The pool's own 1, 2, 3... sequence number. Never sent to ESPN directly: each enabled
    # league resolves its own ESPN week number and season type from anchor_date, see
    # app/services/calendar.py. resolved_weeks records what each league actually resolved to.
    week_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # The Saturday this pool week is anchored to: pool.week1_anchor_date + (week_number - 1)
    # weeks. Nullable because a week created while the pool has no week1_anchor_date configured
    # gets none, and fetch_candidates falls back to sending week_number to ESPN directly.
    anchor_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # What each enabled league actually resolved to when the candidate pool was last fetched,
    # for example {"nfl": {"week": 1, "season_type": 2}, "ncaaf": {"week": 3, "season_type": 2}}.
    # A league that had no games for anchor_date (regular season or postseason) maps to None.
    resolved_weeks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # True when any enabled league needed season_type=3 (postseason/bowl season) to find a
    # calendar window containing anchor_date.
    is_bowl_week: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="draft", nullable=False)
    lock_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when a commissioner overrides the computed lock, so a rebuild will not stomp it.
    lock_at_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scored_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Metered spread pulls already spent on this week. Capped by
    # settings.max_spread_refreshes_per_week so a season stays inside the free tiers.
    spread_refreshes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cfbd_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )

    pool: Mapped[Pool] = relationship(back_populates="weeks")
    games: Mapped[list[Game]] = relationship(
        back_populates="week", cascade="all, delete-orphan", order_by="Game.slate_rank"
    )
    entries: Mapped[list[WeekEntry]] = relationship(
        back_populates="week", cascade="all, delete-orphan"
    )


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (UniqueConstraint("week_id", "espn_event_id", name="uq_week_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_id: Mapped[int] = mapped_column(
        ForeignKey("weeks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    league: Mapped[str] = mapped_column(String(8), nullable=False)
    espn_event_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    odds_api_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    start_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    home_team: Mapped[str] = mapped_column(String(120), nullable=False)
    away_team: Mapped[str] = mapped_column(String(120), nullable=False)
    home_abbr: Mapped[str] = mapped_column(String(12), nullable=False)
    away_abbr: Mapped[str] = mapped_column(String(12), nullable=False)
    home_record: Mapped[str | None] = mapped_column(String(20), nullable=True)
    away_record: Mapped[str | None] = mapped_column(String(20), nullable=True)
    canonical_home_key: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    canonical_away_key: Mapped[str] = mapped_column(String(120), index=True, nullable=False)

    # Home relative. Negative means the home team is favoured.
    spread_home: Mapped[float | None] = mapped_column(Float, nullable=True)
    closeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    in_slate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    slate_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="scheduled", nullable=False)
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner: Mapped[str | None] = mapped_column(String(8), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )

    week: Mapped[Week] = relationship(back_populates="games")
    picks: Mapped[list[Pick]] = relationship(back_populates="game", cascade="all, delete-orphan")

    @property
    def is_final(self) -> bool:
        return self.status == "final"

    @property
    def is_void(self) -> bool:
        return self.status == "void"

    @property
    def matchup(self) -> str:
        return f"{self.away_abbr} at {self.home_abbr}"

    @property
    def line_text(self) -> str:
        """Human readable line, for example 'Line GB -2.5' or 'Line pick em'."""
        if self.spread_home is None:
            return "Line not available"
        if abs(self.spread_home) < 0.01:
            return "Line pick em"
        if self.spread_home < 0:
            return f"Line {self.home_abbr} {self.spread_home:g}"
        return f"Line {self.away_abbr} {-self.spread_home:g}"


class Pick(Base):
    __tablename__ = "picks"
    __table_args__ = (UniqueConstraint("user_id", "game_id", name="uq_user_game"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("pools.id", ondelete="CASCADE"), index=True, nullable=False
    )
    week_id: Mapped[int] = mapped_column(
        ForeignKey("weeks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), index=True, nullable=False
    )
    picked_team: Mapped[str] = mapped_column(String(8), nullable=False)  # home or away
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="picks")
    game: Mapped[Game] = relationship(back_populates="picks")


class WeekEntry(Base):
    __tablename__ = "week_entries"
    __table_args__ = (UniqueConstraint("user_id", "week_id", name="uq_user_week"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("pools.id", ondelete="CASCADE"), index=True, nullable=False
    )
    week_id: Mapped[int] = mapped_column(
        ForeignKey("weeks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    possible: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Set from app.scoring.WeekResult.did_not_submit. The explicit flag the UI checks to
    # show "no picks submitted" instead of a bare score, rather than inferring it from
    # submitted_at is None. Under scoring_mode "inverse" a no-show still carries a real,
    # nonzero points value (the maximum penalty), so this flag is what tells the UI and
    # weekly_winner_ids that the number on the row is a penalty, not a real result.
    did_not_submit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship()
    week: Mapped[Week] = relationship(back_populates="entries")


class FeedCache(Base):
    """Last good payload per feed call, so a provider outage degrades instead of crashing.

    Also the credit saver: a cached metered response younger than
    settings.spread_cache_minutes is reused rather than re-fetched.
    """

    __tablename__ = "feed_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ProviderUsage(Base):
    """Monthly call and credit accounting for the metered providers.

    ESPN is deliberately absent. It needs no key, publishes no quota, and carries all of
    the high frequency work (schedules, live status, final scores, and odds when present).
    """

    __tablename__ = "provider_usage"
    __table_args__ = (UniqueConstraint("provider", "period", name="uq_provider_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # odds_api or cfbd
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # calendar month, "2026-08"
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credits_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Straight from the provider, currently only The Odds API x-requests-remaining header.
    remaining_reported: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_called_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
