"""SQLAlchemy models. Kept database agnostic so SQLite and Postgres both work."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.providers.teams import canonical_key


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
# Which leaderboard a PayoutRule's place/value is matched against (Payout system rebuild):
# "weekly" for an ordinary week's WeeklyRow.rank (every regular week, not summed across the
# season), "bowl" for a week where Week.is_bowl_week is true (routes there instead of
# "weekly" even when weekly rules also exist), "season_points" for the season standings
# panel ranked by total points, "season_wins" for the same panel ranked by weekly win count
# instead. See app/payouts.py and app/services/payouts.py.
PAYOUT_SCOPES = ("weekly", "bowl", "season_points", "season_wins")
# "amount": PayoutRule.value is a flat dollar figure. "percent": PayoutRule.value is a
# percentage of the pot (0-100), resolved at read/snapshot time against the pool's
# effective_pot. See app/payouts.py.resolve_rule.
PAYOUT_MODES = ("amount", "percent")
# The unit each resolved share is rounded down to before the leftover remainder is handed
# out one unit at a time in tiebreak order. See app/payouts.py.allocate.
PAYOUT_ROUNDINGS = ("cent", "dollar", "five")
# How a remainder unit (and a tied group's internal order) is broken. Only one rule exists
# today (earliest WeekEntry.submitted_at first, None sorts last, then user_id), kept as a
# named, stored setting rather than a hard coded constant so a future tiebreak rule needs no
# migration to switch a pool onto it.
PAYOUT_TIEBREAKS = ("earliest_submit",)

# The rivalry pairs (Phase 5) that auto-pin themselves onto every rebuilt slate no matter
# how wide the spread runs: the two the commissioner group named directly (Ohio State vs
# Michigan, Auburn vs Alabama) plus the rest of the obviously-same-shape rivalries. Every
# key comes from a real canonical_key(...) call, never a hand typed slug, so a change to
# app/providers/teams.py's alias tables cannot silently drift this list out of sync with
# what upsert_games actually matches against. See DECISIONS.md, Phase 5.
DEFAULT_RIVALRIES: tuple[tuple[str, str], ...] = (
    (canonical_key("Ohio State", "ncaaf"), canonical_key("Michigan", "ncaaf")),
    (canonical_key("Auburn", "ncaaf"), canonical_key("Alabama", "ncaaf")),
    (canonical_key("Army", "ncaaf"), canonical_key("Navy", "ncaaf")),
    (canonical_key("Michigan", "ncaaf"), canonical_key("Michigan State", "ncaaf")),
    (canonical_key("Florida", "ncaaf"), canonical_key("Georgia", "ncaaf")),
    (canonical_key("Texas", "ncaaf"), canonical_key("Oklahoma", "ncaaf")),
    (canonical_key("USC", "ncaaf"), canonical_key("Notre Dame", "ncaaf")),
)


def _default_rivalries() -> list[list[str]]:
    """A fresh, mutable copy, so no two Pool rows ever share the same list object."""
    return [list(pair) for pair in DEFAULT_RIVALRIES]


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
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="PoolMember.user_id",
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
    # A second, independent code that gates creating a COMMISSIONER account for this pool
    # (POST /register?commissioner_code=..., app/routers/auth.py), never the plain player
    # join_code above. Kept as its own nullable column, never a flag or alias on join_code,
    # because it gates a materially more powerful action (creating a commissioner, not a
    # member) and the product owner was explicit that rotating one must never affect the
    # other (Post-launch fixes: commissioner invite links). Every existing pool is backfilled
    # with a fresh one in the migration that adds this column; every new pool gets one at
    # creation time in POST /site/leagues/new, so in practice this is never left null, but
    # the column stays nullable at the type level since nothing here structurally requires
    # every pool to always have one (matching Pool.venmo_handle and Pool.entry_fee's own
    # nullable, "no value set yet" convention rather than forcing a NOT NULL with a synthetic
    # default). Reusing app.auth.generate_join_code, never a second code generator.
    commissioner_invite_code: Mapped[str | None] = mapped_column(
        String(40), unique=True, index=True, nullable=True
    )
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
    # Off by default (Phase 5): the tool always proposes a slate, but a fresh pool waits for
    # the commissioner to review and publish it by hand rather than opening automatically.
    # See DECISIONS.md, Phase 5, for why the default flipped.
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Rivalry pairs that auto-pin themselves onto every rebuilt slate, regardless of spread
    # width (Phase 5: "certain games with wider spreads are almost always included"). A list
    # of two element lists of canonical team keys, either order, for example
    # [["ncaaf:ohio-state", "ncaaf:michigan"]]. See app/providers/teams.canonical_key for the
    # key format and app/services/ingest.py for where a match is applied to a new Game row.
    rivalries: Mapped[list[list[str]]] = mapped_column(
        JSON, default=_default_rivalries, nullable=False
    )
    # Per pool override of the OPEN_REGISTRATION env default, owned by the commissioner.
    open_registration: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_week: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # True for exactly one hidden, non-customer pool in a given deployment (Post-launch
    # fixes): it holds a real, live game slate through the ordinary build_slate pipeline, for
    # a signed in visitor who has not joined a league yet to preview, read only. It is its own
    # third thing, never a customer league and never the marketing demo: no commissioner, no
    # PoolMember rows, ever, and excluded from every league listing and switcher. See
    # app/services/preview.py and app/cli.py's seed-preview command, the only place its slate
    # is ever built or refreshed.
    is_preview: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
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

    # Venmo entry gate (Phase 7: "Will be Venmo only this year. No Venmo, no participation.").
    # Numeric(10, 2), not Float: the payout system rebuild (see DECISIONS.md, "Payout system")
    # requires every payout-facing money field to be Decimal end to end, and entry_fee feeds
    # directly into the payout pot, so it moved off this codebase's older Float-for-money
    # convention along with it. Nullable/None until the commissioner sets a real number by
    # hand; the house rule is that no dollar figure is ever hard coded, including a fallback
    # default here.
    entry_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    # Overrides the computed pot (entry_fee times paid member count) when set, for a
    # commissioner who wants to hold a reserve, carry over a prior season's balance, or
    # otherwise declare the real number by hand rather than trusting the count. See
    # app/services/payouts.py.effective_pot.
    pot_override: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    # How many regular season weeks the "weekly" payout scope applies to (his real structure:
    # "Weekly (weeks 1-15)"). A weekly rule resolves to this many separate per-week payouts,
    # never a single season-long sum. See app/payouts.py.category_total.
    weekly_payout_weeks: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    # PAYOUT_ROUNDINGS: the unit each resolved payout share rounds down to before the leftover
    # remainder is distributed one unit at a time. See app/payouts.py.allocate.
    payout_rounding: Mapped[str] = mapped_column(String(8), default="dollar", nullable=False)
    # PAYOUT_TIEBREAKS: how a remainder unit and a tied group's internal order are broken.
    payout_tiebreak: Mapped[str] = mapped_column(
        String(16), default="earliest_submit", nullable=False
    )
    # The single collector's Venmo handle (no @), "1 person to pay, no multiple accounts" per
    # the group. PoolMember.member_venmo_handle (below) is a different thing: an optional note
    # for the commissioner's own reconciliation, never a second place to pay.
    venmo_handle: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # True (the default) means POST /picks and POST /picks/lock both refuse a member whose
    # PoolMember.paid_at is null, and GET /picks shows the blocking Venmo panel. A commissioner
    # who wants a free pool, or wants picks open before payment is settled, switches this off.
    payment_required_to_pick: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Free text shown to players above the Venmo links, for example how to word the Venmo note
    # or a Zelle fallback for anyone without Venmo. Never seeded with real instructions.
    payment_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Scenarios panel visibility (Phase 8: "Once 5 games were completed, you could see how
    # many different scenarios got you placed for the week"). The panel on Weekly Results
    # only renders once a week has at least this many final countable games AND at least
    # this many still-remaining countable games; below that it shows a pending state
    # instead. Both are commissioner settings, read at render time, never hard coded, so a
    # smaller or larger pool can tune when the panel is worth showing.
    scenarios_min_final_games: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    scenarios_min_remaining_games: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Opt in, off by default (Phase 7 remediation, see DECISIONS.md): when True, every pool
    # member with a real account gets a short email the moment a week opens for picks (wired
    # into app/services/ingest.py's publish_week, both the commissioner's manual "Publish this
    # week" and build_slate's own auto_publish path). Off by default so a pool that never
    # configures mail, or a commissioner who does not want the noise, sees no change in
    # behavior. Exposed on /league/settings.
    notify_week_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )

    members: Mapped[list[PoolMember]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )
    weeks: Mapped[list[Week]] = relationship(back_populates="pool", cascade="all, delete-orphan")
    payout_rules: Mapped[list[PayoutRule]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )
    # ORM-level cascade, matching payout_rules above: needed so deleting a Pool through the
    # session removes its awards even on a SQLite connection without PRAGMA foreign_keys=ON
    # (the DB-level ON DELETE CASCADE on payout_awards.pool_id is a second, independent
    # safety net, not the only mechanism this relies on).
    payout_awards: Mapped[list[PayoutAward]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )

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
    # "member" (default), "commissioner" (full power, including managing other
    # commissioners), or "co_commissioner" (Post-launch fixes: everything a commissioner can
    # do operationally, slate, settings, members, marking paid, but never promote, demote,
    # remove a commissioner or co-commissioner, or see/share/rotate
    # Pool.commissioner_invite_code). No DB level enum, see the module note above, so this
    # third value needed no migration of its own for the column itself. See is_commissioner
    # below and app.auth.require_full_commissioner for the operational-versus-roster-
    # management split.
    role_in_pool: Mapped[str] = mapped_column(String(16), default="member", nullable=False)
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    # Set the moment a full commissioner invites this still-plain member to become a
    # co-commissioner (POST /league/members/{id}/co-commissioner/invite). role_in_pool stays
    # "member" until the invited member accepts it themselves (POST
    # /league/co-commissioner/accept), which sets role_in_pool = "co_commissioner" and clears
    # this back to null; declining (POST /league/co-commissioner/decline) also clears it to
    # null with no other change. Unlike the site admin's existing instant member_role toggle,
    # a full commissioner's promotion never takes effect on its own, exactly the confirmation
    # step the product owner asked for. Null the rest of the time: no invite pending. See
    # DECISIONS.md, Post-launch fixes.
    co_commissioner_invited_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set the moment a commissioner marks this member paid (POST /league/members/{id}/paid or
    # the bulk action), cleared to unmark. Null means unpaid, the only state the Venmo gate
    # (Pool.payment_required_to_pick) checks; there is no separate "confirmed by whom" table,
    # paid_marked_by_user_id below is enough for accountability.
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Who clicked the toggle, for accountability if a payment is disputed later. Set alongside
    # paid_at and cleared alongside it; never set on its own. SET NULL rather than CASCADE: a
    # site admin account being deleted years later must not silently un-mark every payment
    # they ever confirmed.
    paid_marked_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Optional, for the commissioner's own bookkeeping only (spotting two members who share one
    # Venmo account, which the group explicitly banned: "1 person to pay, no multiple
    # accounts"). Never itself a place to pay; Pool.venmo_handle is the single collector.
    member_venmo_handle: Mapped[str | None] = mapped_column(String(80), nullable=True)

    pool: Mapped[Pool] = relationship(back_populates="members")
    # Two FKs onto users.id now (user_id, paid_marked_by_user_id), so both relationships name
    # their own foreign_keys explicitly; SQLAlchemy cannot infer which FK belongs to which
    # relationship once there is more than one path to the same target table.
    user: Mapped[User] = relationship(back_populates="memberships", foreign_keys=[user_id])
    paid_marked_by: Mapped[User | None] = relationship(foreign_keys=[paid_marked_by_user_id])

    @property
    def is_commissioner(self) -> bool:
        """True for both a full commissioner and a co-commissioner: both can operate the pool
        equally (slate editor, settings, members, marking paid). Every existing route gated on
        this property, or on app.auth.is_commissioner/require_commissioner, needed no change
        to treat a co-commissioner as a working commissioner. The narrower power a
        co-commissioner does not get, managing anyone's commissioner status, is its own check,
        app.auth.is_full_commissioner/require_full_commissioner, which tests role_in_pool ==
        "commissioner" exactly rather than this property."""
        return self.role_in_pool in ("commissioner", "co_commissioner")


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
    # True for a commissioner-built low-stakes test week (Phase 3: preseason and test week
    # support), built from whatever is live right now (NFL preseason, college week 0)
    # rather than the pool's real season. Scores normally within itself (WeekEntry rows are
    # real and correct) but is fully quarantined from everything season-wide: excluded from
    # season standings and weekly-win counts (app/services/standings.py), never generates a
    # PayoutAward of any scope (the freeze hook in app/services/results.py.score_week_for_pool
    # skips it outright), and the scenarios panel treats it as never visible
    # (app/services/scenarios.py.week_scenario_panel). See DECISIONS.md, Phase 3.
    is_test_week: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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

    # American odds (Phase 8, the scenario engine's moneyline probability model). Populated
    # opportunistically wherever app/providers/espn.py already parses odds, when a moneyline
    # shaped key is present in the payload. None of this codebase's recorded ESPN fixtures
    # carry one (checked directly against tests/fixtures, see DECISIONS.md, Phase 8), so on
    # live traffic these are commonly null and app.scenarios.win_probability falls back to
    # the spread derived normal CDF model, which is expected and documented, not a bug.
    home_moneyline: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_moneyline: Mapped[int | None] = mapped_column(Integer, nullable=True)

    in_slate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    slate_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True means this game always makes the slate on the next rebuild, regardless of its
    # spread (Phase 5). Set either by a commissioner action or by a rivalry auto-pin on
    # first creation (see upsert_games in app/services/ingest.py). No separate "why pinned"
    # column: whether it is a rivalry match against pool.rivalries or a manual pin is worked
    # out at render/report time from canonical_home_key/canonical_away_key, not stored here.
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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
    # Set when the player deliberately locks their own picks for the week, ahead of the
    # pool-wide lock_at (Phase 4). Distinct from submitted_at: a save touches submitted_at
    # and never this column, only POST /picks/lock does. Cleared by POST /picks/unlock, which
    # is only permitted while week_is_locked(week) is still False; once the real lock_at
    # passes, the player can never unlock again and this column is frozen at whatever it was.
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class PayoutRule(Base):
    """One place's dollar-or-percent value for one payout scope (Payout system rebuild).

    "This will be done manually, so if there's a way in settings to Set Payouts for weekly,
    bowl week, season points, and season wins... it's a function of total $$ pool." Ships with
    zero rows for every pool, always: the commissioner enters every real number by hand from
    /league/payouts, never a seeded or hard coded figure (see DECISIONS.md, "Payout system").
    Matched against rank at read time (app/payouts.py, app/services/payouts.py), never stored
    against a specific week or player itself, so the same structure applies to every week
    without re-entering it; PayoutAward below is the frozen, per-week/per-player result.

    This replaces the earlier, simpler payout_rules table (float amount, three scopes, no
    percent mode). Production had zero real rows in the old shape, so this is a clean rebuild,
    not a migration of old rows into the new shape; see DECISIONS.md, "Payout system", Phase 0.
    """

    __tablename__ = "payout_rules"
    __table_args__ = (
        UniqueConstraint("pool_id", "scope", "place", name="uq_payout_rule_pool_scope_place"),
        CheckConstraint("place >= 1", name="ck_payout_rule_place_positive"),
        CheckConstraint("value >= 0", name="ck_payout_rule_value_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("pools.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # PAYOUT_SCOPES
    place: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 based, 1 is first, no cap
    mode: Mapped[str] = mapped_column(String(8), nullable=False)  # PAYOUT_MODES
    # Dollars when mode == "amount", a percentage (0-100) of the pot when mode == "percent".
    # Numeric(12, 4): headroom for a percent value like 2.1234 without ever needing float math.
    value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    label: Mapped[str | None] = mapped_column(String(60), nullable=True)  # "1st place", optional
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
        nullable=False,
    )

    pool: Mapped[Pool] = relationship(back_populates="payout_rules")


class PayoutAward(Base):
    """A frozen, resolved payout, written once a week (or the season) is fully scored (Payout
    system rebuild). Percent-mode payouts resolve against the pot, and the pot can grow after
    the fact (a member pays late), so re-resolving a past week live would silently change a
    figure the commissioner may have already paid out over Venmo. This table is the fix: it
    freezes the resolved dollar amount, the pot it was computed against, and the rule in force
    at that moment, the instant a week (or the season) finishes scoring. All display of a past
    payout reads this table; only the current, still-unfinished week/season may show a live,
    unsaved projection, clearly labeled "Projected". See app/services/payouts.py.
    """

    __tablename__ = "payout_awards"
    __table_args__ = (
        UniqueConstraint(
            "pool_id", "scope", "week_id", "user_id", name="uq_payout_award_pool_scope_week_user"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("pools.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # PAYOUT_SCOPES
    # Null for the two season scopes; set for weekly/bowl. Cascade deletes the award if the
    # week itself is ever deleted (a rebuild-from-scratch scenario), rather than orphaning it.
    week_id: Mapped[int | None] = mapped_column(
        ForeignKey("weeks.id", ondelete="CASCADE"), index=True, nullable=True
    )
    place: Mapped[int] = mapped_column(Integer, nullable=False)
    # How many players shared this exact place (a tie). 1 when this player finished alone.
    tied_with: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    pot_at_award: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    rule_mode: Mapped[str] = mapped_column(String(8), nullable=False)  # PAYOUT_MODES
    rule_value: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    awarded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set only when recalculate_awards overwrites a prior snapshot by hand, an explicit,
    # confirmed admin action, never an automatic side effect of scoring running again.
    recalculated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recalculated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_marked_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    pool: Mapped[Pool] = relationship(back_populates="payout_awards")
    user: Mapped[User] = relationship(foreign_keys=[user_id])


class ContactSubmission(Base):
    """One submission from the public /contact page (Post-launch fixes: a real contact form,
    replacing the old bare mailto link). A lead capture form, not a support ticket system: no
    status, no assignment, no reply-from-here feature. The site admin reads these from
    GET /site/contacts and replies to the submitter directly, over their own normal email
    client, within 24 hours. No email is ever sent by this model or by POST /contact.
    """

    __tablename__ = "contact_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )


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


class PlatformSetting(Base):
    """The one platform-wide, site-admin-controlled settings row (Phase 5 remediation,
    provider controls move to site admin).

    Exactly one row ever exists in practice: app.providers.http.get_platform_settings creates
    it on first read if the table is still empty, the same get-or-create shape
    app.providers.http.get_usage already uses for ProviderUsage. Deliberately not a generic
    key-value settings table: espn_only is the only platform-wide toggle this codebase needs
    today, and a generic table would be over-engineering for one boolean (see DECISIONS.md,
    Phase 5). Deliberately not a Pool column either: this switch is explicitly global, read by
    app.services.ingest.build_slate for every pool's build at once, not a per-league
    preference a commissioner could set.
    """

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # When True, build_slate skips The Odds API and CollegeFootballData entirely for every
    # pool's build, ESPN only, regardless of any per-call allow_metered a caller passes (see
    # ingest.build_slate's own docstring). Off by default: the full provider path with
    # existing spend limits intact. Read fresh from the database on every build, never cached
    # in process memory, so a toggle from POST /site/providers/espn-only takes effect on the
    # very next build with no redeploy and no restart.
    espn_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PasswordResetToken(Base):
    """A single use, one hour password reset token (Phase 7 remediation, see DECISIONS.md).

    token_hash is a SHA-256 hex digest of the raw token mailed to the user, never the raw
    token itself, deliberately NOT run through app.auth.hash_password's bcrypt (bcrypt's slow,
    salted hash exists to resist offline brute forcing of a low entropy human-chosen secret;
    this token is a 256 bit value from secrets.token_urlsafe(32), already far past brute
    forceable, so a fast, deterministic hash is used instead so GET/POST /reset-password can
    look the row up by an exact match rather than a full table scan verifying every pending
    token by hand). See app/routers/auth.py's _hash_token.

    used_at enforces single use: set the moment the token is spent, checked by every reset
    attempt, never cleared. expires_at is created_at + one hour, checked on every attempt
    regardless of used_at, so an old, unused token cannot be spent late either.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()


class MailLog(Base):
    """One row per attempted send, successful or not (Phase 7 remediation, see DECISIONS.md).

    This is the site admin's only way to prove whether an email actually went out
    (/site/mail): app.services.mail.send writes exactly one row here for every call, before
    returning on success or raising on failure, so there is no code path that sends (or tries
    to send) without a matching row here. actor_key is the rate limiting bucket
    app.services.mail._rate_limited counts against, "user:{id}" for a signed in sender
    (a site admin or commissioner) or "email:{address}" for the one anonymous sender, a
    password reset request.
    """

    __tablename__ = "mail_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # sent, disabled, failed, rate_limited
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_key: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        index=True,
        nullable=False,
    )
