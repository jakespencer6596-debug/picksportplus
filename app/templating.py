"""Jinja2 environment, filters and shared template globals."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

from app.config import settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Short forms for the common US zones, since "ET" reads better than "EDT" in a banner.
_ZONE_LABELS = {
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Los_Angeles": "PT",
    "America/Phoenix": "MST",
}


def get_zone(name: str | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(name or settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite hands datetimes back naive. Everything we store is UTC, so say so."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def to_local(value: dt.datetime | None, tz_name: str | None = None) -> dt.datetime | None:
    value = as_utc(value)
    if value is None:
        return None
    return value.astimezone(get_zone(tz_name))


def zone_label(tz_name: str | None = None) -> str:
    name = tz_name or settings.timezone
    return _ZONE_LABELS.get(name, name.split("/")[-1].replace("_", " "))


def fmt_kickoff(value: dt.datetime | None, tz_name: str | None = None) -> str:
    """For example 'Thu 8:15 PM ET'."""
    local = to_local(value, tz_name)
    if local is None:
        return "Time to be announced"
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a')} {hour}:{local.strftime('%M %p')} {zone_label(tz_name)}"


def fmt_kickoff_long(value: dt.datetime | None, tz_name: str | None = None) -> str:
    """For example 'Thursday, October 2, 8:15 PM ET'."""
    local = to_local(value, tz_name)
    if local is None:
        return "Time to be announced"
    hour = local.strftime("%I").lstrip("0") or "12"
    day = str(local.day)
    return (
        f"{local.strftime('%A')}, {local.strftime('%B')} {day}, "
        f"{hour}:{local.strftime('%M %p')} {zone_label(tz_name)}"
    )


def fmt_date(value: dt.datetime | None, tz_name: str | None = None) -> str:
    local = to_local(value, tz_name)
    if local is None:
        return ""
    return f"{local.strftime('%b')} {local.day}"


def fmt_countdown(value: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """Coarse remaining time, for example '2d 4h', '3h 12m', 'under a minute'."""
    target = as_utc(value)
    if target is None:
        return ""
    current = as_utc(now) or dt.datetime.now(dt.UTC)
    delta = target - current
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "0m"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "under a minute"


def fmt_signed(value: float | int | None) -> str:
    """'+3.5', '-3.5', 'PK'. Used for spreads."""
    if value is None:
        return ""
    if abs(value) < 0.01:
        return "PK"
    return f"{value:+g}"


def fmt_record(value: str | None) -> str:
    return value or ""


def ordinal(n: int | None) -> str:
    if n is None:
        return ""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def pluralize(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or singular + "s")


def fmt_money(value: float | int | None) -> str:
    """A plain number, no currency symbol: "480" or "27.50". Never "480.00": a whole dollar
    figure drops its trailing zeroes, since the group's own phrasing ("480 dollars collected
    of 640 dollars") reads the amount as a bare number with "dollars" spelled out beside it,
    not a currency-formatted string. None (a pool with no entry fee set yet) reads as "0"."""
    if value is None:
        return "0"
    # Round through cents first so a float artifact (639.9999999999999) never leaks into the
    # displayed figure or into the whole-vs-fractional check just below.
    cents = round(float(value) * 100)
    if cents % 100 == 0:
        return str(cents // 100)
    return f"{cents / 100:.2f}"


templates.env.filters.update(
    {
        "kickoff": fmt_kickoff,
        "kickoff_long": fmt_kickoff_long,
        "shortdate": fmt_date,
        "countdown": fmt_countdown,
        "signed": fmt_signed,
        "record": fmt_record,
        "ordinal": ordinal,
        "pluralize": pluralize,
        "localtime": to_local,
        "money": fmt_money,
    }
)

templates.env.globals.update(
    {
        "APP_NAME": "PickSportPlus",
        "zone_label": zone_label,
        "now_utc": lambda: dt.datetime.now(dt.UTC),
        "OPERATOR_NAME": settings.operator_name,
        "OPERATOR_URL": settings.operator_url,
        "SUPPORT_EMAIL": settings.support_email,
        "LEGAL_DATE": settings.legal_effective_date,
    }
)


def render(request, template_name: str, context: dict | None = None, **base):
    """Render a page with the context every template expects.

    base accepts current_user, pool, is_commissioner and active_nav. Flash messages are
    drained here so a page never has to remember to do it.
    """
    from app.auth import pop_flashes

    ctx = {
        "request": request,
        "current_user": base.get("current_user"),
        "pool": base.get("pool"),
        "is_commissioner": base.get("is_commissioner", False),
        "active_nav": base.get("active_nav"),
        "flashes": pop_flashes(request),
        "tz": (base.get("pool").timezone if base.get("pool") else settings.timezone),
    }
    ctx.update(context or {})
    status_code = base.get("status_code", 200)
    return templates.TemplateResponse(request, template_name, ctx, status_code=status_code)
