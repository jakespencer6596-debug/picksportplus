"""Application configuration, loaded from the environment or a .env file."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import ClassVar

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def is_ephemeral_sqlite_path(database_url: str) -> bool:
    """True for a sqlite URL whose file lives under /tmp, the one directory Render's free
    instance disk guarantees is writable and the one it wipes on every sleep or redeploy
    (Phase 1 remediation, see DECISIONS.md). A non-sqlite URL (Postgres, MySQL) is never
    ephemeral by this check; a sqlite file anywhere other than /tmp is treated as durable
    enough for local development, even though no path on a Render free instance actually
    survives a restart outside a persistent disk or an external database."""
    if not database_url.startswith("sqlite"):
        return False
    path = database_url.split("///", 1)[-1]
    return path == "/tmp" or path.startswith("/tmp/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    secret_key: str = "dev-only-insecure-key-change-me"
    database_url: str = "sqlite:///./picksportplus.db"

    odds_api_key: str = ""
    cfbd_api_key: str = ""

    # Metered provider governor. ESPN is keyless and unmetered so it is not budgeted.
    # The Odds API free plan allows 500 credits per month, CFBD 1000 calls. We stay under.
    odds_api_monthly_budget: int = 400
    cfbd_monthly_budget: int = 800
    # Metered spread pulls allowed per pool week, across the window before lock.
    max_spread_refreshes_per_week: int = 4
    # CFBD is the last-resort college-only fallback and is capped harder.
    max_cfbd_calls_per_week: int = 1
    # A cached spread pull younger than this is reused instead of respending credits.
    spread_cache_minutes: int = 360

    season_year: int = 2026
    # The Saturday pool week 1 anchors to (app/services/calendar.py resolves each enabled
    # league's own ESPN week from this date, see DECISIONS.md, Phase 1). Real, confirmed
    # value for the 2026 season: seed_admin threads this straight onto a freshly created
    # pool's Pool.week1_anchor_date, the same field a commissioner can also edit later from
    # /league/settings. Nullable so a pool can be seeded without one and configured by hand
    # instead, matching Pool.week1_anchor_date's own nullability.
    week1_anchor_date: dt.date | None = dt.date(2026, 9, 12)
    open_registration: bool = False

    # Blank by default so a public demo does not ship a placeholder account or publish a
    # placeholder contact address. seed-admin skips cleanly when either of these is unset.
    admin_email: str = ""
    admin_password: str = ""
    admin_display_name: str = "Commissioner"

    # Render sets these automatically on every service. Their presence is how the app knows
    # it is running behind Render's proxy rather than on a laptop.
    render_external_hostname: str = ""
    render_external_url: str = ""
    # Comma separated. For a custom domain pointed at this service (picksportplus.com, say),
    # since Render's own auto-injected hostname above only ever covers the *.onrender.com
    # address, never a domain bought elsewhere and pointed here after the fact.
    extra_allowed_hosts: str = ""

    default_pool_name: str = "PickSportPlus"
    default_join_code: str = "make-one-up"

    # Who operates the app, shown in the footer and on the legal pages.
    operator_name: str = "Spencer Innovations"
    operator_url: str = "https://spencerinv.com"
    # Contact address on the legal pages. Falls back to ADMIN_EMAIL when unset.
    contact_email: str = ""
    # The effective date printed on the terms and the privacy policy. Bump it by hand when
    # the wording changes, so the date always means something.
    legal_effective_date: str = "August 2, 2026"

    timezone: str = "America/New_York"
    # Seed defaults for a new pool. The commissioner owns these once the pool exists.
    # 20 total, split 8 NFL and 12 college.
    num_games_per_week: int = 20
    nfl_games_per_week: int = 8
    ncaaf_games_per_week: int = 12
    publish_lead_days: int = 6

    offline_mode: bool = False

    # Transactional email (Phase 7 remediation, see DECISIONS.md). Off by default so a fresh
    # deploy or a local dev environment never accidentally tries to send: every call site
    # (invites, password reset, week-published notification) must keep working through its
    # existing copy-and-paste fallback when this is False. Blank api key/from address is a
    # valid answer too, matching ODDS_API_KEY/CFBD_API_KEY's own "optional, blank is fine"
    # convention; app.services.mail.send treats "disabled" and "not configured" as the same
    # loud failure rather than two different silent no-ops.
    mail_enabled: bool = False
    resend_api_key: str = ""
    mail_from_address: str = ""
    mail_from_name: str = "PickSportPlus"
    # Simple per-actor cap, counted from real MailLog rows rather than an in-process counter
    # (see app/services/mail.py), since this app can restart at will (Phase 1 remediation) and
    # an in-process counter would silently reset on every sleep or redeploy.
    mail_rate_limit_per_hour: int = 20

    # Network behaviour for the three feeds. Kept here so tests can tighten them.
    http_timeout_seconds: float = 15.0
    http_retries: int = 2

    # Phase 6 remediation (see DECISIONS.md): a whole build_slate call has no wall-clock
    # budget of its own, only the per-HTTP-call timeout above, so a slow or hanging provider
    # could otherwise hold a build open indefinitely with the commissioner staring at a blank
    # page. 90 seconds comfortably covers a real build (ESPN for the schedule, then up to a
    # few Odds API/CFBD calls, one per league, each bounded by http_timeout_seconds) while
    # still failing loud well before a commissioner gives up and reloads. Tests that want a
    # deterministic, fast timeout pass ingest.build_slate(..., time_budget_seconds=...) directly
    # rather than lowering this setting.
    slate_build_timeout_seconds: float = 90.0

    # The commissioner sets the real numbers per pool. These bounds only stop nonsense.
    # ClassVar keeps pydantic from treating them as settings fields.
    MIN_SLATE: ClassVar[int] = 2
    MAX_SLATE: ClassVar[int] = 40

    @field_validator("num_games_per_week")
    @classmethod
    def _clamp_slate_size(cls, v: int) -> int:
        if not Settings.MIN_SLATE <= v <= Settings.MAX_SLATE:
            raise ValueError(
                f"NUM_GAMES_PER_WEEK must be between {Settings.MIN_SLATE} and {Settings.MAX_SLATE}"
            )
        return v

    @field_validator("nfl_games_per_week", "ncaaf_games_per_week")
    @classmethod
    def _clamp_league_target(cls, v: int) -> int:
        if not 0 <= v <= Settings.MAX_SLATE:
            raise ValueError(f"League targets must be between 0 and {Settings.MAX_SLATE}")
        return v

    @field_validator("spread_cache_minutes")
    @classmethod
    def _floor_spread_cache(cls, v: int) -> int:
        # A zero here would disable caching on the metered providers, so every build would
        # respend credits. Nobody ever wants that, and it reads like "no limit" by mistake.
        return max(1, v)

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Render hands out postgres:// URLs. SQLAlchemy 2 wants an explicit driver.
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+psycopg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_ephemeral_storage(self) -> bool:
        """True when database_url is a sqlite file under /tmp, which Render's free instance
        disk wipes on every restart, sleep or redeploy (Phase 1 remediation, see
        DECISIONS.md). A Postgres URL, or a sqlite file anywhere other than /tmp, is not
        flagged: a developer's local sqlite:///./picksportplus.db survives fine between
        runs on the same machine."""
        return is_ephemeral_sqlite_path(self.database_url)

    @property
    def support_email(self) -> str:
        """Empty is a valid answer. The legal pages fall back to plain text when it is."""
        return (self.contact_email or self.admin_email or "").strip()

    @property
    def is_render(self) -> bool:
        return bool(self.render_external_hostname)

    @property
    def secure_cookies(self) -> bool:
        """HTTPS only cookies everywhere except a local http dev server."""
        return self.is_render or not self.is_sqlite

    @property
    def base_url(self) -> str:
        """Absolute origin, for links that have to work outside a request context."""
        if self.render_external_url:
            return self.render_external_url.rstrip("/")
        if self.render_external_hostname:
            return f"https://{self.render_external_hostname}"
        return "http://localhost:8000"

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = ["localhost", "127.0.0.1", "testserver"]
        if self.render_external_hostname:
            hosts.append(self.render_external_hostname)
        if self.render_external_url:
            host = self.render_external_url.split("://")[-1].split("/")[0]
            if host:
                hosts.append(host)
        hosts.extend(h.strip() for h in self.extra_allowed_hosts.split(",") if h.strip())
        return hosts

    @property
    def has_admin_credentials(self) -> bool:
        """seed-admin needs both, and neither may be a leftover placeholder."""
        email = self.admin_email.strip().lower()
        password = self.admin_password
        if not email or not password:
            return False
        return email != "you@example.com" and password != "change-me"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
