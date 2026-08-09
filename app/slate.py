"""Slate selection: the closest spread games for a week (spec Section 6).

This module is deliberately pure. No database, no network, no imports from
app.models. The ingest service resolves a home relative spread per game, wraps
each game in a Candidate, and hands the list to select_slate_by_targets. The
result is a deterministic function of the arguments alone (pass an explicit now
to keep it that way, otherwise the started game filter reads the wall clock), so
it is cheap to unit test and safe to re run as often as the cron fires.

Selection is per league, not a single global top N. The week takes the closest
target_nfl NFL games and the closest target_ncaaf college games, 8 and 12 by
default for a total of 20. A resolvable spread ranks a game, closest first, but
it is never required for a game to be eligible: a real, scheduled game with no
posted line yet is still a legitimate candidate, it simply sorts after every
game whose spread is known. When a league has fewer eligible games than its
target, the gap is filled with the next closest games from the other league so
the total still lands, and the shortfall is reported so the commissioner can
see why the mix came out the way it did.

Vocabulary used throughout:

- spread_home is home relative. Negative means the home team is favoured.
- closeness is abs(spread_home), or None when the spread is not yet known.
  A pick em at 0.0 is the closest possible game.
- slate_rank counts from 1, where 1 is the closest game on the slate.

Pinned games (spec Section 6a, the rivalry feedback): a candidate can carry
pinned=True, which guarantees it survives selection regardless of how wide its
spread is, or whether it has a spread at all. Pins are seeded into the first
pass before the ordinary per league fill runs, so a pin already counts against
its own league's target; the existing over/under total balancing (drop the
farthest, fill from the other league) does the rest exactly as it already did
for an ordinary shortfall. A pin only needs to not have already kicked off,
when exclude_started is true, the same as any other candidate; a missing
spread never disqualifies it. Pinning itself (by a commissioner or by rivalry
auto-pin) happens upstream, in app.services.ingest; this module only ever
guarantees a pin already set on the input survives.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "LEAGUES",
    "LEAGUE_LABELS",
    "Candidate",
    "Selected",
    "SlateResult",
    "closeness_of",
    "select_slate_by_targets",
    "compute_lock_at",
]

# The leagues the product covers. A candidate in any other league is still
# selectable, it simply has no target of its own and can only arrive via the
# cross league fill.
LEAGUES: tuple[str, ...] = ("nfl", "ncaaf")

# How a league is named in the notes the admin screen and the log show.
LEAGUE_LABELS: dict[str, str] = {"nfl": "NFL", "ncaaf": "college"}


@dataclass(frozen=True)
class Candidate:
    """One game that could go on the slate."""

    key: str
    """Stable id, the ESPN event id."""

    league: str
    """Either "nfl" or "ncaaf"."""

    kickoff: datetime
    """Timezone aware, UTC."""

    spread_home: float | None
    """Home relative. Negative means home favoured. None means unresolved."""

    pinned: bool = False
    """True to force this game onto the slate regardless of closeness.

    Set upstream (a commissioner's manual pin, or a rivalry auto-pin), never
    invented here. A default of False keeps every caller that pre-dates pins,
    including every existing test, constructing a Candidate unchanged.
    """


@dataclass(frozen=True)
class Selected:
    """One game that made the slate."""

    key: str
    slate_rank: int
    """1 is the closest game."""

    closeness: float | None
    """abs(spread_home), or None when the game made the slate with no line yet."""

    pinned: bool = False
    """True when this game made the slate because it was pinned, not (only)
    because it was among the closest. See Candidate.pinned."""


@dataclass(frozen=True)
class SlateResult:
    """The chosen slate plus everything the commissioner needs to explain it."""

    selected: list[Selected]
    """Ordered by slate_rank, 1 is the closest game."""

    per_league: dict[str, int]
    """Final count per league. Only leagues that actually appear are listed."""

    shortfalls: dict[str, int]
    """League to how many games short of its target the supply came up.

    Measured on the first pass, so it stays a supply signal that the fill cannot
    erase. A league can also finish under its target because the targets added
    up to more than the total, which is a separate note and not a shortfall.
    Zeroes are omitted.
    """

    filled_from_other: int
    """Games in the final set beyond their own league's target."""

    notes: list[str]
    """Plain sentences for the admin screen and the log."""


def closeness_of(spread_home: float | None) -> float | None:
    """abs(spread), or None when the spread is unresolved.

    A spread that is not a finite number (NaN or an infinity, which is what a
    median over an empty bookmaker list or a NaN literal in a provider payload
    produces) counts as unresolved rather than as a real line. NaN compares
    false against everything, so letting one through would make the sort order,
    and therefore the whole slate, depend on the order of the input list.
    """
    if spread_home is None:
        return None
    value = abs(float(spread_home))
    if not math.isfinite(value):
        return None
    return value


def _require_aware(value: object, label: str) -> datetime:
    """Return value as an aware datetime, or raise ValueError."""
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime, got {type(value).__name__}.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone aware. Naive datetimes are ambiguous, use UTC.")
    return value


def _sort_key(candidate: Candidate) -> tuple[float, datetime, str]:
    """Closest first, then earliest kickoff, then key.

    A candidate with no resolvable spread sorts after every candidate with a real
    one: float("inf") stands in for closeness here so "closest known spread
    first" stays the default preference, but that stand-in is sort-only and never
    leaks into Selected.closeness, which stores the honest closeness_of value
    (None when there is nothing to rank the game by).

    The key tiebreak is what makes the whole selection stable: two games with the
    same closeness (including two spread-less games, which tie at infinity) and
    the same kickoff still have exactly one correct order, so shuffling the input
    cannot change the output.
    """
    closeness = closeness_of(candidate.spread_home)
    sort_closeness = float("inf") if closeness is None else closeness
    return (sort_closeness, candidate.kickoff, candidate.key)


def _parse_targets(targets: Mapping[str, int] | None) -> dict[str, int]:
    """Validate the per league targets and return them as a plain dict.

    Order is preserved, so the notes and the reported shortfalls come out in the
    order the commissioner's settings named the leagues.
    """
    if not targets:
        return {}
    parsed: dict[str, int] = {}
    for league, raw in targets.items():
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"targets[{league!r}] must be an integer, got {raw!r}.")
        if raw < 0:
            raise ValueError(f"targets[{league!r}] must not be negative, got {raw}.")
        parsed[league] = raw
    return parsed


def _label(league: str) -> str:
    """The league name as it reads in a sentence."""
    return LEAGUE_LABELS.get(league, league)


def _games(count: int) -> str:
    """Format a count as 1 game or 4 games."""
    return f"{count} game" if count == 1 else f"{count} games"


def _league_games(count: int, league: str) -> str:
    """Format a count as 1 NFL game or 5 NFL games."""
    noun = "game" if count == 1 else "games"
    return f"{count} {_label(league)} {noun}"


def _reason(started: int, count: int) -> str:
    """Why a league's supply came up short of its target.

    A resolvable spread is never required for eligibility, so a shortfall has
    only two possible causes left: the started game filter removed enough of
    that league's games, or the league genuinely does not have enough games
    scheduled for the window at all. count is the supply the sentence's count
    refers to, so "was"/"were" agrees with it; "had" needs no such split.
    """
    if started:
        return "had not kicked off yet"
    return f"{'was' if count == 1 else 'were'} scheduled this week"


def _join(labels: Sequence[str]) -> str:
    """Join labels as college, or college and NFL, or college, NFL and XFL."""
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _build_notes(
    targets: Mapping[str, int],
    shortfalls: Mapping[str, int],
    first_pass: Mapping[str, int],
    per_league: Mapping[str, int],
    started: Mapping[str, int],
    started_total: int,
    filled_from_other: int,
    filled: int,
    dropped: int,
    eligible_count: int,
    final_count: int,
    total: int,
) -> list[str]:
    """One plain sentence per notable condition, in reading order."""
    notes: list[str] = []

    for league, short in shortfalls.items():
        supply = first_pass.get(league, 0)
        notes.append(
            f"Only {_league_games(supply, league)} "
            f"{_reason(started.get(league, 0), supply)}, {short} short of the target of "
            f"{targets[league]}."
        )

    if shortfalls and filled_from_other:
        over = [
            _label(league) for league, count in per_league.items() if count > targets.get(league, 0)
        ]
        noun = "game" if filled_from_other == 1 else "games"
        notes.append(f"The gap was filled with the next closest {_join(over)} {noun}.")

    targets_total = sum(targets.values())
    if dropped:
        verb = "was" if dropped == 1 else "were"
        noun = "game" if dropped == 1 else "games"
        notes.append(
            f"The league targets add up to {targets_total}, which is more than the "
            f"total of {total}, so the {dropped} farthest {noun} {verb} dropped."
        )
    elif targets and targets_total < total and filled:
        verb = "was" if filled == 1 else "were"
        noun = "game" if filled == 1 else "games"
        notes.append(
            f"The league targets add up to {targets_total}, which is less than the "
            f"total of {total}, so the {filled} next closest {noun} {verb} added."
        )

    if final_count < total:
        notes.append(
            f"The slate came up {_games(total - final_count)} short of {total} "
            f"because only {_games(eligible_count)} {_reason(started_total, eligible_count)}."
        )

    return notes


def select_slate_by_targets(
    candidates: Iterable[Candidate],
    targets: Mapping[str, int] | None,
    total: int,
    now: datetime | None = None,
    exclude_started: bool = True,
) -> SlateResult:
    """Take the closest games per league, then fill across leagues to the total.

    The first pass gives every league named in targets its own closest games, up
    to its target. If that leaves the slate short of total, the remaining
    eligible games fill it in global closeness order, which is what lets a thin
    NFL week still publish a full slate. If the targets add up to more than the
    total, the farthest games are dropped until exactly total remain. An empty
    targets mapping, or None, means no per league targets at all, so the whole
    slate comes from that global fill.

    Returns at most total games. Fewer is valid and expected when supply is
    genuinely short, for example a light week where a league simply does not
    have enough games scheduled. Candidates are deduplicated on key, keeping the
    closest entry, so a provider that reports one event twice cannot put it on
    the slate twice.

    Raises ValueError for a total below 1, for a target that is not a
    non negative integer, and for any naive datetime.
    """
    if isinstance(total, bool) or not isinstance(total, int):
        raise ValueError(f"total must be an integer, got {total!r}.")
    if total < 1:
        raise ValueError(f"total must be at least 1, got {total}.")

    parsed_targets = _parse_targets(targets)

    # Materialize before the passes below. A caller who hands in a generator
    # would otherwise have it drained by the awareness check and get a silently
    # empty slate, which the cron would then publish.
    candidates = list(candidates)

    if now is not None:
        _require_aware(now, "now")
    for candidate in candidates:
        _require_aware(candidate.kickoff, f"kickoff for candidate {candidate.key!r}")

    if exclude_started:
        cutoff = now if now is not None else datetime.now(UTC)
    else:
        cutoff = None

    # A resolvable spread is never a gate, only a rank. The only thing that can
    # drop a candidate here is the started game filter, so count those by league
    # for the notes.
    eligible: list[Candidate] = []
    started: dict[str, int] = {}
    started_total = 0
    for candidate in candidates:
        if cutoff is not None and candidate.kickoff <= cutoff:
            started[candidate.league] = started.get(candidate.league, 0) + 1
            started_total += 1
            continue
        eligible.append(candidate)

    # One entry per key, keeping the closest, in global order.
    ordered: list[Candidate] = []
    seen: set[str] = set()
    for candidate in sorted(eligible, key=_sort_key):
        if candidate.key in seen:
            continue
        seen.add(candidate.key)
        ordered.append(candidate)

    by_league: dict[str, list[Candidate]] = {}
    for candidate in ordered:
        by_league.setdefault(candidate.league, []).append(candidate)

    # Pins are seeded into the first pass before the ordinary per league fill
    # runs, so a pin already counts against its own league's target. A pin
    # that has already kicked off was already dropped by the eligibility
    # filter above and so never reaches here; a missing spread does not drop
    # it, see the module docstring.
    pinned_candidates = [candidate for candidate in ordered if candidate.pinned]
    if len(pinned_candidates) > total:
        raise ValueError(
            f"{len(pinned_candidates)} games are pinned, more than the slate " f"total of {total}."
        )

    chosen_keys: set[str] = {candidate.key for candidate in pinned_candidates}
    first_pass: dict[str, int] = {}
    for candidate in pinned_candidates:
        first_pass[candidate.league] = first_pass.get(candidate.league, 0) + 1

    # First pass: each targeted league takes its own closest games, on top of
    # whatever pins already claimed a spot. A league not named in targets
    # takes nothing here but stays eligible for the fill. A league whose pins
    # already meet or exceed its target contributes nothing more here, which
    # is what lets its pins expand that league's effective count: the total
    # is then brought back to total by the ordinary over/under balancing
    # below, exactly as it already handles any other shortfall or overflow.
    for league, target in parsed_targets.items():
        remaining = max(0, target - first_pass.get(league, 0))
        taken = [
            candidate for candidate in by_league.get(league, []) if candidate.key not in chosen_keys
        ][:remaining]
        chosen_keys.update(candidate.key for candidate in taken)
        first_pass[league] = first_pass.get(league, 0) + len(taken)

    chosen = [candidate for candidate in ordered if candidate.key in chosen_keys]

    dropped = 0
    filled = 0
    if len(chosen) > total:
        # chosen is already closest first, so the tail is the farthest games,
        # but a pinned game must survive no matter how far it sits. Pins are
        # never more than total (checked above), so there is always enough
        # unpinned tail to drop.
        excess = len(chosen) - total
        drop_keys = {candidate.key for candidate in [c for c in chosen if not c.pinned][-excess:]}
        chosen = [candidate for candidate in chosen if candidate.key not in drop_keys]
        dropped = excess
    elif len(chosen) < total:
        for candidate in ordered:
            if len(chosen) >= total:
                break
            if candidate.key in chosen_keys:
                continue
            chosen_keys.add(candidate.key)
            chosen.append(candidate)
            filled += 1
        chosen.sort(key=_sort_key)

    selected = [
        Selected(
            key=candidate.key,
            slate_rank=rank,
            closeness=closeness_of(candidate.spread_home),
            pinned=candidate.pinned,
        )
        for rank, candidate in enumerate(chosen, start=1)
    ]

    counts: dict[str, int] = {}
    for candidate in chosen:
        counts[candidate.league] = counts.get(candidate.league, 0) + 1

    # Targeted leagues first, in the order the settings named them, then any
    # league that only arrived through the fill. Fixed order keeps the whole
    # result reproducible, not just equal.
    per_league: dict[str, int] = {
        league: counts[league] for league in parsed_targets if league in counts
    }
    for league in sorted(counts):
        if league not in per_league:
            per_league[league] = counts[league]

    shortfalls: dict[str, int] = {}
    for league, target in parsed_targets.items():
        short = max(0, target - first_pass.get(league, 0))
        if short:
            shortfalls[league] = short

    filled_from_other = sum(
        max(0, count - parsed_targets.get(league, 0)) for league, count in per_league.items()
    )

    notes = _build_notes(
        targets=parsed_targets,
        shortfalls=shortfalls,
        first_pass=first_pass,
        per_league=per_league,
        started=started,
        started_total=started_total,
        filled_from_other=filled_from_other,
        filled=filled,
        dropped=dropped,
        eligible_count=len(ordered),
        final_count=len(selected),
        total=total,
    )

    return SlateResult(
        selected=selected,
        per_league=per_league,
        shortfalls=shortfalls,
        filled_from_other=filled_from_other,
        notes=notes,
    )


def compute_lock_at(kickoffs: Sequence[datetime]) -> datetime | None:
    """Earliest kickoff, or None for an empty sequence."""
    values = list(kickoffs)
    if not values:
        return None
    for value in values:
        if not isinstance(value, datetime):
            raise ValueError(f"kickoffs must all be datetimes, got {type(value).__name__}.")
    try:
        return min(values)
    except TypeError as exc:  # naive and aware datetimes mixed together
        raise ValueError("kickoffs must all be timezone aware or all naive, not a mix.") from exc
