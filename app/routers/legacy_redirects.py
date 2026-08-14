"""301 redirects from the pre-Phase-4 /admin/... URLs to their new homes.

Phase 4 remediation (see DECISIONS.md) split /admin into /league (commissioner tools) and
/site (site admin tools). Every GET path here is one a human could plausibly have bookmarked
or linked to from an email; each gets a permanent redirect so an old link still works.

POST-only legacy paths (form submissions: settings saves, slate actions, member actions, and
so on) are deliberately NOT redirected here. A 301 does not reliably preserve a POST's method
and body across every client, and nothing in this app itself will ever issue one of those old
POST paths again once the Phase 4 sweep is done, since every template and every internal
redirect was updated to the new URLs. See DECISIONS.md, Phase 4, for the fuller reasoning.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["legacy-redirects"])

_REDIRECTS = {
    "/admin": "/league",
    "/admin/settings": "/league/settings",
    "/admin/members": "/league/members",
    "/admin/slate": "/league/slate",
    "/admin/payouts": "/league/payouts",
    "/admin/payouts/summary": "/league/payouts/summary",
    "/admin/payouts/summary.csv": "/league/payouts/summary.csv",
    "/admin/leagues": "/site/leagues",
    "/admin/leagues/new": "/site/leagues/new",
    "/admin/contacts": "/site/contacts",
}


def _make_redirect(target: str):
    def _redirect() -> RedirectResponse:
        return RedirectResponse(target, status_code=301)

    return _redirect


for _old_path, _new_path in _REDIRECTS.items():
    router.add_api_route(
        _old_path,
        _make_redirect(_new_path),
        methods=["GET"],
        include_in_schema=False,
    )


__all__ = ["router"]
