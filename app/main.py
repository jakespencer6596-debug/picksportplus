"""FastAPI application: middleware, routers, error pages."""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import event
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import settings
from app.templating import APP_CSS_URL, APP_JS_URL, STATIC_DIR, render, templates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("picksportplus")

app = FastAPI(title="PickSportPlus", docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="psp_session",
    same_site="lax",
    # Secure behind Render's TLS, relaxed for a local http dev server. Starlette always
    # sets HttpOnly on this cookie, so it is never readable from JavaScript.
    https_only=settings.secure_cookies,
    max_age=60 * 60 * 24 * 30,
)

# Only answer to hosts we expect. Render publishes the real hostname in the environment.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

# Render terminates TLS at its edge and forwards X-Forwarded-Proto. Without this the app
# would believe every request is plain http and would refuse to set a Secure cookie.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Far-future cached, content-hashed URLs for the two hand authored assets the design system
# calls out as large (Phase 2, weekly tiebreak/sorting/performance work, see PERF-REPORT.md).
# Registered BEFORE the generic /static mount below: a Starlette Mount claims its whole path
# prefix outright once it matches, so these two literal routes would 404 from inside the mount
# itself (never falling through to a route defined later) if they were not declared first. The
# plain /static/app.css and /static/app.js the mount serves still work too, unversioned, for
# any old bookmark or cached reference; every page just links the hashed URL below instead, so
# a browser that has ever fetched one can cache it for a year. The URL itself changes the
# moment app.css or app.js does, on the next deploy's fresh hash (app/templating.py), so
# "immutable" here really does mean this exact URL's bytes can never change under a client,
# not just "we don't expect them to."
#
# methods=["GET", "HEAD"], not the bare @app.get shorthand: found live against production
# (Phase 9, see DECISIONS.md) that a plain @app.get route only answers GET, so a HEAD request
# for the exact same URL fell through to the generic /static mount below, which then 404s
# because no file is actually named app.<hash>.css on disk. Never broke a real page load
# (every browser fetches a <link>/<script> with GET, never HEAD), but any tool that does use
# HEAD to check a resource, a monitor or a cache warmer, saw a false 404.
_LONG_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


@app.api_route(APP_CSS_URL, methods=["GET", "HEAD"], include_in_schema=False)
def _versioned_app_css():
    return FileResponse(STATIC_DIR / "app.css", media_type="text/css", headers=_LONG_CACHE_HEADERS)


@app.api_route(APP_JS_URL, methods=["GET", "HEAD"], include_in_schema=False)
def _versioned_app_js():
    return FileResponse(
        STATIC_DIR / "app.js", media_type="application/javascript", headers=_LONG_CACHE_HEADERS
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Performance instrumentation (Phase 0, weekly tiebreak/sorting/performance work, see
# PERF-REPORT.md). Off in production: settings.debug_timing defaults False, and the listener
# below is a no-op read of that flag on every query, cheap enough to leave wired at all times
# rather than attaching and detaching it as a request-scoped concern. Listens on the Engine
# class, not one instance, so it counts queries against whichever engine is actually bound to
# the request (the real app.db.engine in production, a throwaway per-test engine in the test
# suite's own TestClient fixture), matching how app/db.py's own SQLite pragma listener works.
#
# A plain dict, not a contextvars.ContextVar: FastAPI runs a sync route handler in a worker
# thread via anyio's threadpool, which hands that thread a COPY of the calling coroutine's
# context, so a value the query listener sets from inside that thread never propagates back to
# this middleware's own context. A process-wide counter sidesteps that entirely. This makes the
# count exact for one request in flight at a time (true for local dev, the measurement script,
# and a single commissioner clicking around) but not safe against two concurrent requests
# interleaving their counts, an acceptable limitation for a diagnostic that is always off in
# production.
_query_state = {"count": 0}


@event.listens_for(Engine, "before_cursor_execute")
def _count_query(conn, cursor, statement, parameters, context, executemany):  # pragma: no cover
    if not settings.debug_timing:
        return
    _query_state["count"] += 1


@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    if not settings.debug_timing:
        return await call_next(request)
    _query_state["count"] = 0
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        count = _query_state["count"]
    response.headers["X-Render-Time-Ms"] = f"{elapsed_ms:.1f}"
    response.headers["X-Query-Count"] = str(count)
    log.info(
        "DEBUG_TIMING %s %s: %.1fms, %d queries",
        request.method,
        request.url.path,
        elapsed_ms,
        count,
    )
    return response


@app.on_event("startup")
def _log_storage_status() -> None:
    """Loud, impossible-to-miss log line on boot so an operator reading the Render deploy
    log sees data-loss risk immediately, not just from a banner someone has to click into
    (Phase 1 remediation, see DECISIONS.md)."""
    from app.db import engine

    log.info("database dialect: %s", engine.dialect.name)
    if settings.is_ephemeral_storage:
        log.warning(
            "DATABASE_URL points at ephemeral storage (%s). Every account, league, pick, "
            "payout rule and award will be LOST the next time this service sleeps or "
            "redeploys. Set DATABASE_URL to a persistent Postgres database (for example "
            "Neon) before inviting real players.",
            settings.database_url,
        )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Redirects for signed-out users, in-theme pages for everything else."""
    location = (exc.headers or {}).get("Location")
    if exc.status_code in (302, 303, 307) and location:
        target = location
        if exc.status_code == 303 and request.url.path not in ("/", "/login"):
            target = f"{location}?next={request.url.path}"
        return RedirectResponse(target, status_code=303)

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "components/error_inline.html",
            {"request": request, "detail": exc.detail},
            status_code=exc.status_code,
        )

    titles = {
        403: "Not your locker room",
        404: "That page is not on the schedule",
        500: "Something went wrong on our end",
    }
    return render(
        request,
        "error.html",
        {
            "code": exc.status_code,
            "title": titles.get(exc.status_code, "Something went wrong"),
            "detail": exc.detail if isinstance(exc.detail, str) else "",
        },
        status_code=exc.status_code,
    )


@app.exception_handler(FastAPIHTTPException)
async def fastapi_http_exception_handler(request: Request, exc: FastAPIHTTPException):
    return await http_exception_handler(request, exc)


@app.get("/health", include_in_schema=False)
@app.get("/healthz", include_in_schema=False)
def health():
    """Render's health check. Deliberately touches nothing, so a feed or database hiccup
    cannot make the platform think the service is down."""
    return {"status": "ok"}


# Routers are imported after app creation so they can import from app.main if needed.
from app.routers import (  # noqa: E402
    admin,
    admin_contacts,
    auth,
    chat,
    leaderboard,
    leagues,
    legacy_redirects,
    legal,
    payouts,
    picks,
    public,
    results,
    site,
)

app.include_router(auth.router)
app.include_router(picks.router)
app.include_router(leaderboard.router)
app.include_router(results.router)
app.include_router(admin.router)
app.include_router(chat.router)
app.include_router(payouts.router)
app.include_router(leagues.router)
app.include_router(site.router)
app.include_router(admin_contacts.router)
app.include_router(legal.router)
app.include_router(public.router)
# Registered last: every real /admin/... route from before Phase 4 is gone, so these bare
# 301s only ever catch an old bookmark or an old link, never shadow a live route.
app.include_router(legacy_redirects.router)


@app.get("/", include_in_schema=False)
def index(request: Request):
    """A signed in visitor still lands on this week's picks, unchanged. A signed out
    visitor gets the public landing page instead of being bounced straight to /login."""
    if request.session.get("uid"):
        return RedirectResponse("/picks", status_code=303)
    return render(request, "home.html", {}, current_user=None, pool=None, is_commissioner=False)
