"""Shared HTTP layer: timeouts, light retry, last-good caching, and the credit governor.

The whole point of this module is that a season of PickSportPlus stays inside the free tiers.

  ESPN      keyless, unmetered. Carries all the high frequency work: schedules, live status,
            final scores, and odds when the game has not finished yet. Never budgeted.
  Odds API  500 credits per calendar month on the free plan. One spreads request covers a whole
            league for 1 credit, and it returns every upcoming game, so a single call serves
            many weeks. Budgeted, capped per week, and cached hard.
  CFBD      1000 calls per month. Last resort, college only, capped to one lines call per week.

Two independent guards apply to the metered providers:

  1. A monthly counter per provider, checked against ODDS_API_MONTHLY_BUDGET and
     CFBD_MONTHLY_BUDGET, both set below the true free limits to leave headroom.
  2. A per week cap enforced by the caller (see app/services/ingest.py), so one noisy week
     cannot drain the month.

Every response is cached. A cached metered response younger than SPREAD_CACHE_MINUTES is
reused instead of respending a credit, and a stale cache is still served when a provider is
down or the budget is spent, so a page never crashes on a feed failure.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import FeedCache, ProviderUsage, utcnow

log = logging.getLogger("picksportplus.http")

METERED = ("odds_api", "cfbd")
USER_AGENT = "PickSportPlus/1.0 (weekly pick em pool)"


class ProviderError(RuntimeError):
    """A feed could not be read and no cached copy was available."""


class BudgetExceeded(ProviderError):
    """A metered provider has spent its monthly budget. Fall back to ESPN."""


# Usage accounting -----------------------------------------------------------


def month_key(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.UTC)
    return f"{now.year:04d}-{now.month:02d}"


def budget_for(provider: str) -> int:
    return {
        "odds_api": settings.odds_api_monthly_budget,
        "cfbd": settings.cfbd_monthly_budget,
    }.get(provider, 0)


def get_usage(db: Session, provider: str, period: str | None = None) -> ProviderUsage:
    period = period or month_key()
    row = db.scalar(
        select(ProviderUsage).where(
            ProviderUsage.provider == provider, ProviderUsage.period == period
        )
    )
    if row is None:
        row = ProviderUsage(provider=provider, period=period, calls=0, credits_used=0)
        db.add(row)
        db.flush()
    return row


def spend_remaining(db: Session, provider: str) -> int:
    """Credits left this month according to our own counter."""
    row = get_usage(db, provider)
    return max(0, budget_for(provider) - row.credits_used)


def has_budget(db: Session, provider: str, need: int = 1) -> bool:
    if provider not in METERED:
        return True
    if not _api_key_for(provider):
        return False
    row = get_usage(db, provider)
    # The provider's own count wins when it reports one, because it is the real quota.
    if row.remaining_reported is not None and row.remaining_reported < need:
        return False
    return row.credits_used + need <= budget_for(provider)


def _record(
    db: Session,
    provider: str,
    *,
    credits: int = 1,
    remaining: int | None = None,
    error: str | None = None,
) -> None:
    row = get_usage(db, provider)
    row.calls += 1
    row.credits_used += credits
    if remaining is not None:
        row.remaining_reported = remaining
    row.last_called_at = utcnow()
    row.last_error = error
    db.flush()


def _api_key_for(provider: str) -> str:
    return {"odds_api": settings.odds_api_key, "cfbd": settings.cfbd_api_key}.get(provider, "")


def usage_report(db: Session) -> list[dict[str, Any]]:
    """Admin facing view of where the month stands."""
    out: list[dict[str, Any]] = []
    for provider in METERED:
        row = get_usage(db, provider)
        budget = budget_for(provider)
        out.append(
            {
                "provider": provider,
                "label": "The Odds API" if provider == "odds_api" else "CollegeFootballData",
                "configured": bool(_api_key_for(provider)),
                "period": row.period,
                "used": row.credits_used,
                "budget": budget,
                "remaining": max(0, budget - row.credits_used),
                "provider_remaining": row.remaining_reported,
                "pct": round(100 * row.credits_used / budget) if budget else 0,
                "last_called_at": row.last_called_at,
                "last_error": row.last_error,
                "exhausted": not has_budget(db, provider),
            }
        )
    return out


def provider_warnings(db: Session) -> list[str]:
    """Plain warnings for the admin page. Empty when everything is healthy."""
    warnings: list[str] = []
    for report in usage_report(db):
        if not report["configured"]:
            continue
        if report["exhausted"]:
            warnings.append(
                f"{report['label']} has spent its monthly budget "
                f"({report['used']} of {report['budget']}). Spreads fall back to ESPN "
                f"until {report['period']} rolls over."
            )
        elif report["pct"] >= 80:
            warnings.append(
                f"{report['label']} is at {report['pct']}% of its monthly budget "
                f"({report['used']} of {report['budget']})."
            )
        if report["last_error"]:
            warnings.append(f"{report['label']} last call reported: {report['last_error']}")
    return warnings


# Cache ----------------------------------------------------------------------


def cache_get(db: Session, key: str) -> tuple[Any, dt.datetime] | None:
    row = db.scalar(select(FeedCache).where(FeedCache.cache_key == key))
    if row is None:
        return None
    fetched = row.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=dt.UTC)
    return row.payload, fetched


def cache_put(db: Session, key: str, payload: Any) -> None:
    row = db.scalar(select(FeedCache).where(FeedCache.cache_key == key))
    if row is None:
        db.add(FeedCache(cache_key=key, payload=payload, fetched_at=utcnow()))
    else:
        row.payload = payload
        row.fetched_at = utcnow()
    db.flush()


def _is_fresh(fetched_at: dt.datetime, ttl_minutes: int) -> bool:
    age = dt.datetime.now(dt.UTC) - fetched_at
    return age.total_seconds() < ttl_minutes * 60


# The request --------------------------------------------------------------------


def fetch_json(
    db: Session,
    *,
    cache_key: str,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    provider: str | None = None,
    credits: int = 1,
    ttl_minutes: int | None = None,
    allow_stale: bool = True,
) -> tuple[Any, str]:
    """Fetch JSON with caching and budget enforcement.

    Returns (payload, source) where source is one of "live", "cache" or "stale".
    Raises ProviderError or BudgetExceeded when nothing usable can be produced.

    provider is None for ESPN (unmetered) or one of METERED for the budgeted feeds.
    """
    cached = cache_get(db, cache_key)

    # A fresh cache short circuits everything, which is what keeps credit spend near zero.
    if cached and ttl_minutes and _is_fresh(cached[1], ttl_minutes):
        log.debug("cache hit (fresh) %s", cache_key)
        return cached[0], "cache"

    if settings.offline_mode:
        if cached:
            return cached[0], "stale"
        raise ProviderError(f"Offline mode is on and {cache_key} is not cached.")

    if provider in METERED:
        if not _api_key_for(provider):
            if cached:
                return cached[0], "stale"
            raise ProviderError(f"No API key configured for {provider}.")
        if not has_budget(db, provider, credits):
            log.warning("budget exhausted for %s, not calling", provider)
            if cached and allow_stale:
                return cached[0], "stale"
            raise BudgetExceeded(
                f"{provider} has spent its monthly budget of {budget_for(provider)}."
            )

    # Metered feeds get a single attempt plus one retry only for transport level failures,
    # so a flaky network cannot quietly burn the month.
    attempts = 2 if provider in METERED else max(1, settings.http_retries + 1)
    last_error: str | None = None

    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(
                timeout=settings.http_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, **(headers or {})},
            ) as client:
                response = client.get(url, params=params)

            remaining = _parse_remaining(response)

            if response.status_code == 200:
                payload = response.json()
                cache_put(db, cache_key, payload)
                if provider in METERED:
                    _record(db, provider, credits=credits, remaining=remaining)
                    log.info(
                        "%s call ok, %s credit(s), provider reports %s remaining",
                        provider,
                        credits,
                        remaining if remaining is not None else "unknown",
                    )
                return payload, "live"

            last_error = f"HTTP {response.status_code}"
            # 401/403 is a bad key and 422 is a bad request. Retrying wastes credits.
            if response.status_code in (401, 403, 422):
                if provider in METERED:
                    _record(db, provider, credits=0, remaining=remaining, error=last_error)
                break
            if response.status_code == 429:
                last_error = "HTTP 429 rate limited or quota exhausted"
                if provider in METERED:
                    _record(db, provider, credits=0, remaining=0, error=last_error)
                break

        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        except ValueError as exc:  # malformed JSON
            last_error = f"Invalid JSON: {exc}"

        if attempt < attempts:
            time.sleep(min(2.0 * attempt, 4.0))

    log.warning("fetch failed for %s: %s", cache_key, last_error)
    if provider in METERED and last_error:
        _record(db, provider, credits=0, error=last_error)

    if cached and allow_stale:
        log.info("serving stale cache for %s", cache_key)
        return cached[0], "stale"
    raise ProviderError(f"Could not read {cache_key}: {last_error}")


def _parse_remaining(response: httpx.Response) -> int | None:
    """The Odds API reports the true quota in x-requests-remaining on every call."""
    raw = response.headers.get("x-requests-remaining")
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None
