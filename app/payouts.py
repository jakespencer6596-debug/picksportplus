"""Payout allocation engine for the four payout scopes (Payout system rebuild, Phase 2).

Pure logic only. No database, no network, no imports from app.models. Everything here takes
plain dataclasses in and returns plain values out, so it can be unit tested without any
application context, exactly like app/scoring.py. This property is what makes the engine fast
and testable, and it is why nothing in this file ever queries anything: a caller in
app/services/payouts.py (Phase 3) is responsible for pulling real PayoutRule rows and real
standings out of the database and handing them to these functions as plain values.

Money is Decimal end to end, never float, in every function and every return value. A payout
engine that drifted a cent through float math would be silently paying someone the wrong
amount of real money, so this is a hard rule, not a style preference.

Four scopes, matching PAYOUT_SCOPES in app/models.py exactly:

- "weekly": paid every regular week. A weekly rule's value is a PER WEEK figure, so the
  category total for the season multiplies the per-week total by however many weeks the
  pool actually pays out (Pool.weekly_payout_weeks), not by counting configured rules.
- "bowl": paid once, for the bowl week. Same ranking direction rule as weekly.
- "season_points": paid once, at season end, ranked by season point total. Same ranking
  direction rule as weekly and bowl.
- "season_wins": paid once, at season end, ranked by weekly win count. ALWAYS descending
  (most wins first), regardless of the pool's scoring mode. This is the one scope whose
  direction is not a pass-through of the pool's own scoring mode, and it is the single
  easiest wiring mistake a future caller could make (see rank_standings below), so it gets
  its own explicit callout here and its own test that proves the direction is not assumed.

Ranking direction is never hard coded inside this module. rank_standings takes descending as
a required keyword argument, and every call site (including the ones in this module's own
tests) must pass it explicitly, sourced from the pool's scoring mode for weekly/bowl/
season_points and hard coded True only at the call site for season_wins. Baking a direction
into allocate() or rank_standings() itself would make it possible for a future caller to wire
season_wins to the pool's scoring direction by accident, which is exactly the bug this design
exists to make impossible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

__all__ = [
    "SCOPES",
    "MODES",
    "ROUNDING",
    "Rule",
    "StandingInput",
    "Standing",
    "Award",
    "ScopeSummary",
    "AllocationSummary",
    "resolve_rule",
    "rank_standings",
    "allocate",
    "category_total",
    "allocation_summary",
]

# Kept identical to app.models.PAYOUT_SCOPES/PAYOUT_MODES/PAYOUT_ROUNDINGS on purpose: this
# module never imports app.models (no database imports allowed here), so the two lists have
# to be hand kept in sync instead. If they drift, a rule with a scope or mode this module does
# not recognize will surface as a plain KeyError/ValueError rather than a silent misread.
SCOPES = ("weekly", "bowl", "season_points", "season_wins")
MODES = ("amount", "percent")
# The unit each resolved share is rounded down to before the leftover remainder is handed out
# one unit at a time. Decimal values, not strings, so allocate() can hand them straight to
# _floor_to_unit below without a second lookup or parse.
ROUNDING = {"cent": Decimal("0.01"), "dollar": Decimal("1"), "five": Decimal("5")}


@dataclass(frozen=True)
class Rule:
    """One configured payout place: pool_id and id are the database's concern, not this
    module's, so a Rule here is only ever the four fields that matter to resolving money."""

    scope: str
    place: int  # 1 based, 1 is first, no upper bound
    mode: str  # "amount" | "percent"
    value: Decimal
    label: str | None = None


@dataclass(frozen=True)
class StandingInput:
    """One player's raw position for a scope, before ranking. metric is season points,
    season wins, or a single week's points, whatever this scope ranks by; rank_standings is
    what turns a list of these into ranked Standing rows."""

    user_id: int
    metric: Decimal
    submitted_at: datetime | None = None


@dataclass(frozen=True)
class Standing:
    """One player's ranked position, ready to feed into allocate(). rank is already 1 based
    with ties sharing a rank (competition ranking: two tied for 1st are both rank 1, and the
    next distinct player is rank 3, not rank 2), matching how allocate() expects to find
    consecutive places to split among a tied group."""

    user_id: int
    rank: int
    metric: Decimal  # carried through for reference/display only, allocate() never reads it
    submitted_at: datetime | None  # used only for remainder tiebreak


@dataclass(frozen=True)
class Award:
    """One resolved payout for one player. place/tied_with describe the group this player
    was part of (place is the group's starting rank, tied_with is how many players shared
    it), not a claim that this player alone occupied that exact spot. rule_mode/rule_value
    are carried through from whichever configured rule governed this player's group, purely
    so a caller (or a stored PayoutAward row) can show what rule produced this number without
    a second lookup."""

    user_id: int
    place: int
    tied_with: int
    amount: Decimal
    rule_mode: str
    rule_value: Decimal


@dataclass(frozen=True)
class ScopeSummary:
    """The resolved shape of one scope's configured rules against a pot, independent of any
    actual standings. places is the ordered place-to-dollar breakdown for a single occurrence
    of this scope (a single week for "weekly", the one-time payout for the other three), NOT
    multiplied by weeks; category_total is the full-season figure for this scope and IS
    multiplied by weeks for "weekly". This asymmetry (places = per-occurrence,
    category_total = per-season) is deliberate: places is what a commissioner editing the
    payout table wants to see (what does 1st place pay, this week), while category_total is
    what a pot reconciliation view wants (what does this scope cost across the whole season).
    """

    scope: str
    places: dict[int, Decimal]
    category_total: Decimal
    percent_of_pot: Decimal  # category_total as a percent (0-100 scale) of pot, 0 if pot is 0


@dataclass(frozen=True)
class AllocationSummary:
    """The full picture of what a pool's payout configuration resolves to against one pot.

    scopes always has all four SCOPES as keys, even a scope with zero rules configured
    (category_total Decimal(0), empty places), so a template can iterate SCOPES and always
    find a row rather than guarding against a missing key. unallocated = pot - grand_total is
    intentionally allowed to go negative: over-allocation (rules that promise more than the
    pot holds) is legal, it is the commissioner's call to make and fix, not something this
    engine blocks or clamps.
    """

    pot: Decimal
    scopes: dict[str, ScopeSummary]
    grand_total: Decimal
    unallocated: Decimal


def resolve_rule(rule: Rule, pot: Decimal) -> Decimal:
    """One rule's dollar value against a pot.

    "amount": the value already is dollars, passed through unchanged. "percent": the value is
    a percentage (0-100 scale) of pot, so pot * value / 100. This only ever divides by the
    constant 100, never by pot itself, so a pot of 0 resolves cleanly to 0 for a percent rule
    rather than raising a ZeroDivisionError.
    """
    if rule.mode == "amount":
        return rule.value
    if rule.mode == "percent":
        return pot * rule.value / 100
    raise ValueError(f"Unknown payout mode: {rule.mode!r}")


def rank_standings(rows: Sequence[StandingInput], *, descending: bool) -> list[Standing]:
    """Turn raw per-player metrics into ranked Standing rows.

    descending is required and is never defaulted or inferred: the caller must always state,
    explicitly, which direction wins for this call. Pass the pool's effective scoring
    direction for weekly/bowl/season_points, and pass True, hard coded at the call site, for
    season_wins, regardless of what the pool's scoring mode is elsewhere. There is
    deliberately no scope parameter here for this function to branch on: a scope-aware
    default would be exactly the kind of implicit wiring this module exists to avoid.

    Ranking is competition ranking: equal metrics share a rank, and the next distinct metric
    resumes at the 1 based position it would have held in the fully sorted list (two tied for
    1st, next player is rank 3). This is what lets allocate() treat a tied group as occupying
    a contiguous block of places starting at its rank.
    """
    ordered = sorted(rows, key=lambda row: row.metric, reverse=descending)

    standings: list[Standing] = []
    last_metric: Decimal | None = None
    last_rank = 0
    for index, row in enumerate(ordered, start=1):
        if last_metric is not None and row.metric == last_metric:
            rank = last_rank
        else:
            rank = index
            last_rank = index
            last_metric = row.metric
        standings.append(
            Standing(
                user_id=row.user_id,
                rank=rank,
                metric=row.metric,
                submitted_at=row.submitted_at,
            )
        )
    return standings


def _floor_to_unit(value: Decimal, unit: Decimal) -> Decimal:
    """Round value down to the nearest whole multiple of unit.

    Decimal.quantize rounds to a number of FRACTIONAL DIGITS, not to an arbitrary unit: since
    Decimal("5") and Decimal("1") both have zero fractional digits, value.quantize(Decimal("5"),
    rounding=ROUND_DOWN) would silently round to the nearest whole dollar, not the nearest
    five dollars, which is exactly wrong for ROUNDING["five"]. Dividing by the unit, flooring
    to a whole number of units, then multiplying back is what actually rounds down to the
    requested unit for "cent", "dollar", and "five" alike.
    """
    return (value / unit).to_integral_value(rounding=ROUND_DOWN) * unit


def _tiebreak_sort_key(tiebreak: str):
    """The sort key that decides both a tied group's internal order and, therefore, who
    picks up each leftover rounding unit. Only "earliest_submit" exists today (matching
    PAYOUT_TIEBREAKS in app/models.py), kept as a named, checked setting rather than a bare
    assumption so a future second tiebreak rule has somewhere to plug in without changing
    allocate()'s signature.

    A missing submitted_at sorts last (True > False as the first tuple element), never first:
    "we don't know when they submitted" must not look like "they submitted first" when a real
    remainder cent is on the line. user_id is the final tiebreaker so two players with equal
    or equally missing timestamps still resolve deterministically rather than depending on
    incidental list order.
    """
    if tiebreak != "earliest_submit":
        raise ValueError(f"Unknown payout tiebreak: {tiebreak!r}")
    return lambda standing: (standing.submitted_at is None, standing.submitted_at, standing.user_id)


def _governing_rule(rank: int, size: int, place_rule: dict[int, Rule]) -> Rule:
    """Which configured Rule a tied group's Award rows point back to, for rule_mode/
    rule_value. Prefers the rule at the group's own starting place (the normal case: real
    payout tables are configured contiguously, 1st/2nd/3rd/...). Falls back to the first
    configured place within the group's span (the "3 way tie for 2nd, 4th has no rule" case),
    and finally to a zero amount placeholder if literally nothing in the span is configured,
    so a caller never has to guard against getting None back.
    """
    for place in range(rank, rank + size):
        rule = place_rule.get(place)
        if rule is not None:
            return rule
    return Rule(scope="", place=rank, mode="amount", value=Decimal(0))


def allocate(
    rules: Sequence[Rule],
    standings: Sequence[Standing],
    *,
    pot: Decimal,
    rounding: str,
    tiebreak: str = "earliest_submit",
) -> list[Award]:
    """Resolve one scope's rules against one scope's ranked standings into real Awards.

    rules and standings must both already belong to a single scope (and, for "weekly", a
    single week): this function has no scope of its own to filter by, it just splits whatever
    place table and whatever ranking it is handed. Empty rules or empty standings both return
    an empty list without error, there is nothing to split either way.

    Tied players split the combined pool of the consecutive places they occupy: two tied for
    1st split the 1st and 2nd amounts and the next player takes 3rd; a place with no rule
    configured always contributes zero to that split rather than raising. A rank that starts
    entirely past the highest configured place is skipped outright (not awarded a zero-dollar
    row), which is also what makes "no rules for this scope" resolve to an empty list rather
    than a list of zeroes: with no rules, max_place is 0 and every real rank is skipped.
    """
    unit = ROUNDING[rounding]
    order_key = _tiebreak_sort_key(tiebreak)

    place_rule: dict[int, Rule] = {rule.place: rule for rule in rules}
    if not place_rule:
        return []
    max_place = max(place_rule)

    groups: dict[int, list[Standing]] = {}
    for standing in standings:
        groups.setdefault(standing.rank, []).append(standing)

    awards: list[Award] = []
    for rank in sorted(groups):
        if rank > max_place:
            # Nothing configured anywhere in or past this rank: unfilled/unconfigured places
            # pay nothing and are not awarded to anyone, rather than manufacturing a
            # zero-dollar row for every player past the last real place.
            continue

        members = sorted(groups[rank], key=order_key)
        size = len(members)
        span = range(rank, rank + size)
        raw_total = sum(
            (resolve_rule(place_rule[p], pot) for p in span if p in place_rule),
            start=Decimal(0),
        )

        # Round the group's combined total down to the payout unit before splitting it, not
        # after. A percent-mode rule can resolve to more decimal precision than the rounding
        # unit (a 0.05 percent rule against an odd pot, for example), and there is no way to
        # hand out a fraction of a cent, so that sub-unit dust has to be dropped somewhere.
        # Dropping it once, here, on the group total, is what lets the reconciliation assert
        # below hold exactly for every group, every time, instead of chasing an un-splittable
        # remainder through the per-member loop.
        allocated_total = _floor_to_unit(raw_total, unit)

        floor_share = _floor_to_unit(allocated_total / size, unit)
        remainder = allocated_total - floor_share * size
        # remainder is guaranteed to be a non negative, exact multiple of unit smaller than
        # size * unit, because both allocated_total and floor_share * size already are.
        remainder_units = int((remainder / unit).to_integral_value(rounding=ROUND_DOWN))

        amounts = [floor_share] * size
        for i in range(remainder_units):
            amounts[i] += unit

        # This is the money-safety line for the whole module: if a future edit to the split
        # math above ever lets this fail, it means the group's payout no longer sums to what
        # was actually allocated to it, and someone is being shorted or overpaid. Asserted
        # here, in the engine itself, not only in the test suite.
        assert sum(amounts, Decimal(0)) == allocated_total

        governing = _governing_rule(rank, size, place_rule)
        for member, amount in zip(members, amounts, strict=True):
            awards.append(
                Award(
                    user_id=member.user_id,
                    place=rank,
                    tied_with=size,
                    amount=amount,
                    rule_mode=governing.mode,
                    rule_value=governing.value,
                )
            )

    return awards


def category_total(rules: Sequence[Rule], *, pot: Decimal, weeks: int = 1) -> Decimal:
    """The full total a set of same-scope rules costs, resolved against pot.

    weeks defaults to 1, correct for every scope except "weekly" (bowl and both season scopes
    are one-time payouts). A caller resolving the weekly scope must pass
    weeks=pool.weekly_payout_weeks explicitly: this function has no scope of its own to branch
    on, it only multiplies whatever it is handed. weeks=0 is a legitimate input (a pool that
    has not started paying weeklies yet) and returns Decimal(0), not an error.
    """
    per_occurrence = sum((resolve_rule(rule, pot) for rule in rules), start=Decimal(0))
    return per_occurrence * weeks


def allocation_summary(
    all_rules: Sequence[Rule],
    *,
    pot: Decimal,
    weekly_weeks: int,
) -> AllocationSummary:
    """The whole-pool payout picture: every scope's place breakdown and category total,
    resolved against one pot, plus a grand total and the unallocated remainder.

    all_rules may span every scope at once (unlike allocate()/category_total(), which each
    work on one scope's rules at a time); this function does the grouping by scope itself so
    a caller can hand it a pool's entire PayoutRule set in one call.
    """
    by_scope: dict[str, list[Rule]] = {scope: [] for scope in SCOPES}
    for rule in all_rules:
        # Tolerate a rule with a scope this module does not recognize by just dropping it
        # from the summary rather than raising: validating that every rule's scope is one of
        # PAYOUT_SCOPES is the model/service layer's job, not this pure engine's.
        if rule.scope in by_scope:
            by_scope[rule.scope].append(rule)

    scopes: dict[str, ScopeSummary] = {}
    grand_total = Decimal(0)
    for scope in SCOPES:
        rules = sorted(by_scope[scope], key=lambda rule: rule.place)
        weeks = weekly_weeks if scope == "weekly" else 1
        places = {rule.place: resolve_rule(rule, pot) for rule in rules}
        total = category_total(rules, pot=pot, weeks=weeks)
        percent_of_pot = (total / pot * 100) if pot else Decimal(0)
        scopes[scope] = ScopeSummary(
            scope=scope,
            places=places,
            category_total=total,
            percent_of_pot=percent_of_pot,
        )
        grand_total += total

    return AllocationSummary(
        pot=pot,
        scopes=scopes,
        grand_total=grand_total,
        unallocated=pot - grand_total,
    )
