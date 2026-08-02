"""Application configuration, loaded from the environment or a .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import ClassVar

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    open_registration: bool = False

    admin_email: str = "you@example.com"
    admin_password: str = "change-me"
    admin_display_name: str = "Commissioner"

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

    # Network behaviour for the three feeds. Kept here so tests can tighten them.
    http_timeout_seconds: float = 15.0
    http_retries: int = 2

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
    def support_email(self) -> str:
        return self.contact_email or self.admin_email


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
