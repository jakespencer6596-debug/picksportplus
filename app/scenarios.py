"""Scenario engine: placement odds, leverage, and build-your-own scenarios.

This is the feature the group asked for by name: "Once 5 games were completed, you could
see how many different scenarios got you placed for the week. And what those scenarios
were... As well as your percent chance at 1, 2, 3."

Pure logic only, exactly like app.scoring and app.slate: no database, no network, no
imports from app.models or any app.services/app.routers module. That is what lets this
reuse app.scoring.score_week directly, so the scenario sweep can never drift from how a
real week is actually scored, and it is what keeps a single computation cheap enough to
run on every page request without a database session in the hot loop.

Performance shape. A naive implementation that calls score_week once per player per
scenario is much too slow: benchmarked on the build machine, 15 remaining games (2**15 =
32768 scenarios) times 16 players is already 5+ seconds just in score_week overhead, and
the brief's hard cap is 2 seconds total. Scoring a confidence pick'em week is a SUM over
independent picks (score_pick has no cross terms between games), so this module still
computes every scenario's real point total through score_week and score_pick, it just
avoids calling them redundantly:

1. For each player, one score_week call over the already-final outcomes gives a constant
   "base" point total (and the no-show penalty, unchanged by any scenario, since a no-show
   is decided by whether the player submitted any picks at all, not by any one outcome).
2. For each remaining game the player actually picked, two score_week calls (one per side)
   on that single pick in isolation give the exact marginal point delta that game
   contributes, independent of every other remaining game's outcome.
3. Every scenario's real total is then base + the sum of the deltas for whichever side
   each remaining game lands on in that scenario. This is exact, not an approximation:
   it is the same sum score_week would compute, computed once per (player, game, side)
   instead of once per (player, scenario). Cross-checked directly against the brute force
   per-scenario score_week call in tests/test_scenarios.py.

The sweep over 2**R scenarios itself (exhaustive) or N samples (Monte Carlo) still costs
real time proportional to R and the player count, so both paths check the wall clock
periodically (never only once at the end) against TIME_BUDGET_SECONDS and degrade rather
than blow the budget. See compute_scenario_report's docstring for the exact fallback
order. See DECISIONS.md, Phase 8, for the benchmark numbers behind these choices.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.scoring import GameOutcome, PickInput, score_week

__all__ = [
    "MAX_EXHAUSTIVE_REMAINING",
    "MONTE_CARLO_SAMPLES",
    "TIME_BUDGET_SECONDS",
    "SIGMA_BY_LEAGUE",
    "DEFAULT_SIGMA",
    "PLACES",
    "RemainingGame",
    "RepresentativeScenario",
    "PlayerScenarioOutlook",
    "ScenarioReport",
    "StandingsRow",
    "implied_probability_from_moneyline",
    "win_probability",
    "rank_players",
    "compute_scenario_report",
    "build_custom_scenario",
]

# A remaining game is a binary home/away outcome, so R remaining games have 2**R
# scenarios. 2**20 is a little over a million, which is the largest count this module
# will attempt to enumerate exactly; past that it goes straight to Monte Carlo.
MAX_EXHAUSTIVE_REMAINING = 20

# The target Monte Carlo sample count for R > 20 (Spec: "Monte Carlo with 200,000
# samples"). The actual number of samples used on a given call can be lower than this: see
# compute_scenario_report's docstring, "Monte Carlo sample count is a target, not a
# guarantee". It is never higher.
MONTE_CARLO_SAMPLES = 200_000

# The hard cap for one compute_scenario_report call, exhaustive attempt plus any Monte
# Carlo fallback combined.
TIME_BUDGET_SECONDS = 2.0

# Once the exhaustive attempt has burned this fraction of the total budget without
# finishing (build plus rank), it aborts and falls back to Monte Carlo with whatever
# budget remains. Chosen so a doomed exhaustive attempt (R too large for this player
# count to finish in time) cannot eat the entire budget and starve the fallback, while
# still giving a genuinely fast case (R = 15, 16 players measures well under half a second
# on the build machine, see DECISIONS.md) real headroom against background system load
# rather than a hair trigger fallback to an estimate it never needed.
_EXHAUSTIVE_ABORT_FRACTION = 0.75

# Monte Carlo stops drawing new samples once it has used this fraction of the total
# budget, leaving the remainder for building the returned dataclasses.
_MONTE_CARLO_STOP_FRACTION = 0.75

# Points spread by a normal CDF when no moneyline is present (Phi(-spread_home / sigma)).
# NFL games cluster tighter around the spread than college does, so college gets a wider
# sigma. An unlisted league (a future addition, or a bad value) falls back to the NFL
# number, which is the more conservative (narrower) of the two.
SIGMA_BY_LEAGUE: dict[str, float] = {"nfl": 13.5, "ncaaf": 16.0}
DEFAULT_SIGMA = SIGMA_BY_LEAGUE["nfl"]

PLACES: tuple[int, ...] = (1, 2, 3)

# How many candidate winning scenarios are retained per requested player while sweeping,
# for the representative-scenario selection to choose from afterward. Capped so a player
# who wins in the overwhelming majority of scenarios (R large) does not force this module
# to hold every single one of them in memory; a few hundred already gives the greedy
# diversity selection below plenty to work with.
_REPRESENTATIVE_CANDIDATE_CAP = 400

# Monte Carlo batch size: how many samples are drawn before the wall clock is checked
# again. Small enough to keep the time budget honest, large enough that the time.monotonic
# call itself is not the bottleneck.
_MONTE_CARLO_BATCH = 4_000

# Per-sample cost model, calibrated by hand against this module's own chunked lookup
# table on the build machine (see DECISIONS.md, Phase 8, for the benchmark numbers). Used
# only to pick a sample COUNT up front, in closed form, from problem size alone (remaining
# game count and player count) rather than by watching the clock while sampling:
# reproducibility ("same seed, same output") needs the number of samples drawn to be a
# deterministic function of the inputs, not of how fast the machine happened to be running
# at the moment, and a live "stop when the clock says so" loop cannot promise that
# (verified empirically to be flaky run to run on this machine). Two terms, since
# benchmarking showed most of the per-sample cost is drawing and packing R random bits
# (roughly constant regardless of player count) rather than the chunk-table lookup and
# rank (which does scale with players, just by much less): a formula with only a
# per-player term badly underestimated the true cost for a small player count, which is
# what first made this flaky. The generous safety multiplier on top of both terms is what
# keeps the estimate on the slow side, so the real, still-present periodic wall clock
# check in the sampling loop is a rare safety net for a machine slower than this one, not
# the everyday mechanism.
_MONTE_CARLO_SECONDS_PER_REMAINING_GAME = 8e-7
_MONTE_CARLO_SECONDS_PER_CHUNK_PLAYER = 1.5e-7
_MONTE_CARLO_SAFETY_MULTIPLIER = 3.0

# Games are grouped into chunks of this many bits for the Monte Carlo lookup table (see
# _build_chunks). 12 bits is a table of 4096 rows per chunk, built once per call; this is
# the knee of the build-cost/lookup-cost tradeoff measured on the build machine (see
# DECISIONS.md).
_MONTE_CARLO_CHUNK_BITS = 12


@dataclass(frozen=True)
class RemainingGame:
    """One not-yet-final countable game still to be decided, as far as scenarios go.

    Carries whatever probability inputs are available. Both moneyline fields and spread_home
    are optional and independent: a caller passes whichever it has, win_probability below
    falls back through moneyline, then spread, then a flat 50/50 (see its own docstring).
    league selects which sigma the spread-derived model uses (SIGMA_BY_LEAGUE); an
    unrecognised or missing league falls back to DEFAULT_SIGMA.
    """

    game_id: int
    home_moneyline: int | None = None
    away_moneyline: int | None = None
    spread_home: float | None = None
    league: str | None = None


@dataclass(frozen=True)
class RepresentativeScenario:
    """One full scenario worth showing a player: which side of each remaining game.

    assignment is ordered the same as the remaining_games list the report was computed
    from. weight is that scenario's own probability weight under whichever probability
    model produced it (1.0 for every scenario under "even"; the product of the per-game
    win probabilities under "moneyline"; under Monte Carlo every sampled scenario carries
    weight 1.0, since the sampling itself already reflects the probability model, see
    compute_scenario_report).
    """

    assignment: tuple[tuple[int, str], ...]
    weight: float


@dataclass(frozen=True)
class PlayerScenarioOutlook:
    """One player's slice of a ScenarioReport (spec: "your percent chance at 1, 2, 3").

    scenarios_at_place / pct_at_place are keyed 1, 2, 3. A scenario where two players tie
    for a place credits BOTH of them at that place (competition ranking, see rank_players),
    so pct_at_place[1] summed across every player in the report is not the same as
    "fraction of scenarios with a unique 1st place winner"; see the module tests for the
    exact identity that does hold.

    clinched[place] is True only when the player is at that place in literally every
    enumerated or sampled scenario. eliminated[place] is True when they are at that place
    in none of them. Under Monte Carlo both are necessarily an approximation: a placement
    that is real but rare enough to have gone unsampled reads as eliminated, and one that
    fails only in unsampled scenarios reads as clinched. This is inherent to sampling, not
    a bug, and is why the report labels itself is_estimate.

    leverage and representative_scenarios are computed only for players named in the
    caller's representative_for argument (see compute_scenario_report): both require
    decoding which side of each remaining game a scenario landed on, which the placement
    sweep does not otherwise need, so paying for it by default for all 16 players would
    slow down every request for no benefit to 15 of them. A player not requested gets an
    empty dict / empty list here, not an error.

    leverage maps game_id -> {"home": share, "away": share}, the share of THIS PLAYER'S
    OWN first place scenarios (not all scenarios) in which each side won. The two numbers
    for a game sum to 1.0. A game close to 50/50 within the player's own winning scenarios
    is one that genuinely does not matter to them; a game near 100/0 is one they need.
    """

    user_id: int
    scenarios_at_place: dict[int, float]
    pct_at_place: dict[int, float]
    clinched: dict[int, bool]
    eliminated: dict[int, bool]
    leverage: dict[int, dict[str, float]] = field(default_factory=dict)
    representative_scenarios: list[RepresentativeScenario] = field(default_factory=list)


@dataclass(frozen=True)
class ScenarioReport:
    """The full sweep result: every player's outlook, plus how it was computed."""

    remaining_game_ids: list[int]
    total_weight: float
    scenario_count: int
    """How many scenarios were actually enumerated or sampled. 2**R for an exhaustive
    report; the number of Monte Carlo draws actually completed within budget otherwise,
    which can be less than the requested sample count, see compute_scenario_report."""

    is_estimate: bool
    method: str  # "exhaustive" | "monte_carlo"
    probability_model: str  # "even" | "moneyline"
    probability_notes: list[str]
    elapsed_seconds: float
    players: dict[int, PlayerScenarioOutlook]


@dataclass(frozen=True)
class StandingsRow:
    """One player's row in a build-your-own-scenario standings table."""

    user_id: int
    points: int
    correct: int
    possible: int
    did_not_submit: bool
    place: int


# Probability model -----------------------------------------------------------


def implied_probability_from_moneyline(odds: int) -> float:
    """American odds to implied (vig-included) win probability.

    Negative odds (favorite): abs(odds) / (abs(odds) + 100).
    Positive odds (underdog): 100 / (odds + 100).
    """
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def win_probability(game: RemainingGame) -> tuple[float, bool]:
    """Home team win probability for one remaining game, and whether it had to fall back.

    Order: a real moneyline on both sides first, devigged so the two sides sum to exactly
    1.0 (implied_home / (implied_home + implied_away)). Else a spread, run through a normal
    CDF with a league-specific sigma. Else a flat 50/50, with the bool flagging that this
    game had neither and is a fallback (the caller collects these into probability_notes).
    """
    if game.home_moneyline is not None and game.away_moneyline is not None:
        implied_home = implied_probability_from_moneyline(game.home_moneyline)
        implied_away = implied_probability_from_moneyline(game.away_moneyline)
        total = implied_home + implied_away
        if total > 0:
            return implied_home / total, False
    if game.spread_home is not None:
        sigma = SIGMA_BY_LEAGUE.get(game.league or "", DEFAULT_SIGMA)
        p_home = 0.5 * (1 + math.erf((-game.spread_home / sigma) / math.sqrt(2)))
        return p_home, False
    return 0.5, True


# Local ranking (spec: do not import app/services/standings.py) ---------------


def _rank_values(values: Sequence[float], mode: str) -> list[int]:
    """Competition ranking (1, 2, 2, 4, ...), parallel to values. Ties share a place.

    Lowest value wins under "inverse", highest under "standard", exactly the direction
    app.scoring.weekly_winner_ids already uses. This is the one place that rule is
    encoded in this module; every caller below (the scenario sweep and
    build_custom_scenario) goes through this function so the two can never disagree.
    """
    sign = 1 if mode == "inverse" else -1
    order = sorted(range(len(values)), key=lambda i: sign * values[i])
    ranks = [0] * len(values)
    place = 1
    i = 0
    n = len(order)
    while i < n:
        j = i
        while j < n and values[order[j]] == values[order[i]]:
            j += 1
        for k in range(i, j):
            ranks[order[k]] = place
        place += j - i
        i = j
    return ranks


def rank_players(scores: Mapping[Any, float], mode: str) -> dict[Any, int]:
    """Competition ranking over a user_id -> points mapping. Ties share a place.

    The public, dict-keyed form of _rank_values, for any caller (a route, a test) that
    just wants "who is in what place" without going through a full scenario sweep. This is
    also exactly what build_custom_scenario below uses to rank one fixed scenario.
    """
    ids = list(scores.keys())
    ranks = _rank_values([scores[i] for i in ids], mode)
    return dict(zip(ids, ranks, strict=False))


# Per-player linear decomposition (see the module docstring) ------------------


@dataclass
class _PlayerLinear:
    user_id: int
    is_no_show: bool
    constant_points: float
    """The player's whole result when is_no_show is True (never varies by scenario).
    Otherwise the base points earned from already-final outcomes alone."""

    home_delta: list[float]
    away_delta: list[float]
    """Parallel to remaining_games. The exact marginal point contribution of that single
    remaining game landing home or away, from score_week on that one pick in isolation."""


def _linearize_players(
    picks_by_player: Mapping[int, Sequence[PickInput]],
    final_outcomes: Sequence[GameOutcome],
    remaining_games: Sequence[RemainingGame],
    mode: str,
    picks_required: int | None,
) -> dict[int, _PlayerLinear]:
    out: dict[int, _PlayerLinear] = {}
    for user_id, picks in picks_by_player.items():
        picks = list(picks)
        base = score_week(picks, final_outcomes, mode=mode, picks_required=picks_required)
        if base.did_not_submit:
            # A no-show's penalty depends only on picks_required, never on any outcome, so
            # it is a true constant across every scenario. No deltas needed: score_week on
            # an empty picks list would re-trigger the same no-show branch, so the per-game
            # marginal calls below are skipped entirely for a no-show.
            out[user_id] = _PlayerLinear(user_id, True, base.points, [], [])
            continue

        by_game = {p.game_id: p for p in picks}
        home_delta: list[float] = []
        away_delta: list[float] = []
        for game in remaining_games:
            pick = by_game.get(game.game_id)
            if pick is None:
                # This player never picked this game, so it cannot move their score either
                # way, exactly as score_week already treats an outcome nobody picked.
                home_delta.append(0)
                away_delta.append(0)
                continue
            home_result = score_week(
                [pick], [GameOutcome(game.game_id, "final", "home")], mode=mode
            )
            away_result = score_week(
                [pick], [GameOutcome(game.game_id, "final", "away")], mode=mode
            )
            home_delta.append(home_result.points)
            away_delta.append(away_result.points)
        out[user_id] = _PlayerLinear(user_id, False, base.points, home_delta, away_delta)
    return out


def _points_for_assignment(linear: _PlayerLinear, sides: Sequence[str]) -> float:
    """A player's total points under one concrete assignment of every remaining game.

    Used by the R == 0 trivial path and by tests cross-checking the sweep; the sweep
    itself uses the faster doubling-array / chunk-table forms below, not this function,
    but both compute the identical sum.
    """
    if linear.is_no_show:
        return linear.constant_points
    total = linear.constant_points
    for i, side in enumerate(sides):
        total += linear.home_delta[i] if side == "home" else linear.away_delta[i]
    return total


# Exhaustive sweep --------------------------------------------------------------


def _deadline_left(deadline: float) -> float:
    return deadline - time.monotonic()


def _build_points_array(linear: _PlayerLinear, remaining_count: int) -> list[float]:
    """Every scenario's point total for one player, via repeated doubling.

    Scenario index s encodes remaining_games[j]'s side in bit j of s (1 = home), built by
    extending the array away-branch-then-home-branch at each step; see DECISIONS.md for
    why this is equivalent to, but far faster than, computing each of the 2**R scenarios'
    score_week call directly. If the player is a no-show, every entry is the same constant
    (still an explicit list, so the caller can treat every player identically).
    """
    if linear.is_no_show:
        return [linear.constant_points] * (1 << remaining_count)
    arr = [linear.constant_points]
    for j in range(remaining_count):
        h = linear.home_delta[j]
        a = linear.away_delta[j]
        arr = [x + a for x in arr] + [x + h for x in arr]
    return arr


def _build_weight_array(p_home: Sequence[float], remaining_count: int) -> list[float]:
    """Every scenario's probability weight, same bit convention as _build_points_array."""
    arr = [1.0]
    for j in range(remaining_count):
        ph = p_home[j]
        arr = [x * (1 - ph) for x in arr] + [x * ph for x in arr]
    return arr


def _try_exhaustive(
    linear_by_player: dict[int, _PlayerLinear],
    p_home: list[float],
    remaining_games: Sequence[RemainingGame],
    mode: str,
    representative_for: Sequence[int],
    max_representative: int,
    deadline: float,
) -> tuple[dict[int, PlayerScenarioOutlook], float] | None:
    """Full exhaustive sweep, or None if it had to abort (caller falls back to Monte Carlo).

    Checked periodically (after each player's points array, and again every slice of the
    ranking pass), never only at the end, so a doomed attempt is caught mid-run rather than
    after it has already spent the whole budget.
    """
    R = len(remaining_games)
    user_ids = list(linear_by_player.keys())

    points_arrays: dict[int, list[float]] = {}
    for user_id in user_ids:
        if _deadline_left(deadline) <= 0:
            return None
        points_arrays[user_id] = _build_points_array(linear_by_player[user_id], R)

    weight_array = _build_weight_array(p_home, R)
    if _deadline_left(deadline) <= 0:
        return None

    n = 1 << R
    total_weight = sum(weight_array)
    game_ids = [g.game_id for g in remaining_games]
    target_set = set(representative_for)

    weighted_place: dict[int, dict[int, float]] = {u: {p: 0.0 for p in PLACES} for u in user_ids}
    seen_at: dict[int, dict[int, bool]] = {u: {p: False for p in PLACES} for u in user_ids}
    seen_not_at: dict[int, dict[int, bool]] = {u: {p: False for p in PLACES} for u in user_ids}
    leverage_home: dict[int, dict[int, float]] = {
        u: dict.fromkeys(game_ids, 0.0) for u in target_set
    }
    leverage_total: dict[int, float] = dict.fromkeys(target_set, 0.0)
    candidates: dict[int, list[tuple[int, float]]] = {u: [] for u in target_set}

    check_every = max(1, n // 20)
    for s in range(n):
        if s % check_every == 0 and _deadline_left(deadline) <= 0:
            return None
        weight = weight_array[s]
        values = [points_arrays[u][s] for u in user_ids]
        ranks = _rank_values(values, mode)
        for idx, user_id in enumerate(user_ids):
            place = ranks[idx]
            wp = weighted_place[user_id]
            sa = seen_at[user_id]
            sn = seen_not_at[user_id]
            for p in PLACES:
                if p == place:
                    wp[p] += weight
                    sa[p] = True
                else:
                    sn[p] = True
            if place == 1 and user_id in target_set:
                leverage_total[user_id] += weight
                home_map = leverage_home[user_id]
                for j, game_id in enumerate(game_ids):
                    if (s >> j) & 1:
                        home_map[game_id] += weight
                bucket = candidates[user_id]
                if len(bucket) < _REPRESENTATIVE_CANDIDATE_CAP:
                    bucket.append((s, weight))

    outlooks = _assemble_outlooks(
        user_ids=user_ids,
        game_ids=game_ids,
        total_weight=total_weight,
        weighted_place=weighted_place,
        seen_at=seen_at,
        seen_not_at=seen_not_at,
        leverage_home=leverage_home,
        leverage_total=leverage_total,
        candidates=candidates,
        decode=lambda s, j: "home" if (s >> j) & 1 else "away",
        max_representative=max_representative,
    )
    return outlooks, total_weight


# Monte Carlo sweep ---------------------------------------------------------------


def _build_chunks(
    linear_by_player: dict[int, _PlayerLinear], remaining_count: int, user_ids: list[int]
) -> list[tuple[int, int, dict[int, list[tuple[float, ...]]]]]:
    """Split the remaining games into fixed-size bit chunks, each with a lookup table.

    Returns a list of (start, end, table) where table[user_id] is a list indexed by that
    chunk's local bit pattern (same bit convention as _build_points_array, scoped to just
    that chunk's games), each entry the tuple of every player's point contribution from
    just that chunk. This is what lets one Monte Carlo sample's point totals be computed
    in O(chunks) additions per player instead of O(remaining games): the expensive part of
    the naive approach is not drawing the random bits (cheap) but summing R deltas per
    player per sample, so this precomputes those sums a chunk at a time. See DECISIONS.md
    for the benchmark this size (2**12 rows per chunk) was chosen against.
    """
    chunks: list[tuple[int, int, dict[int, list[tuple[float, ...]]]]] = []
    start = 0
    while start < remaining_count:
        end = min(start + _MONTE_CARLO_CHUNK_BITS, remaining_count)
        length = end - start
        size = 1 << length
        per_player: dict[int, list[float]] = {}
        for user_id in user_ids:
            linear = linear_by_player[user_id]
            if linear.is_no_show:
                per_player[user_id] = [0.0] * size
                continue
            arr = [0.0]
            for j in range(start, end):
                h = linear.home_delta[j]
                a = linear.away_delta[j]
                arr = [x + a for x in arr] + [x + h for x in arr]
            per_player[user_id] = arr
        table_by_index = [tuple(per_player[u][idx] for u in user_ids) for idx in range(size)]
        chunks.append((start, end, table_by_index))
        start = end
    return chunks


def _run_monte_carlo(
    linear_by_player: dict[int, _PlayerLinear],
    p_home: list[float],
    remaining_games: Sequence[RemainingGame],
    mode: str,
    representative_for: Sequence[int],
    max_representative: int,
    seed: int,
    sample_target: int,
    mc_budget_seconds: float,
    hard_deadline: float,
) -> tuple[dict[int, PlayerScenarioOutlook], int]:
    """Sampled sweep. Returns (outlooks, samples actually drawn).

    Sampling itself already reflects the probability model (each remaining game is drawn
    bernoulli(p_home), 0.5 under "even"), so unlike the exhaustive path every drawn sample
    carries equal weight; the model's influence is in which scenarios get drawn, not in a
    post hoc weight.

    How many samples: sample_target (from compute_scenario_report, at most
    MONTE_CARLO_SAMPLES) is reduced, in closed form from R and the player count alone via
    _MONTE_CARLO_SECONDS_PER_UNIT and a generous _MONTE_CARLO_SAFETY_MULTIPLIER, to
    whatever is expected to comfortably fit mc_budget_seconds. This is a deterministic
    function of the inputs, not of the live clock, which is what makes two calls with the
    same seed draw the identical sequence of samples and produce identical output (an
    earlier version of this function instead watched the clock and stopped a running batch
    loop once mc_budget_seconds was nearly spent; that measured a genuinely different
    sample count from one call to the next on this machine under ordinary background load,
    sometimes even for two calls issued back to back, which made "same seed, same output"
    flaky). The batch loop below still checks the wall clock periodically and can still
    stop early with fewer samples than the deterministic target, but only against
    hard_deadline, the caller's absolute, full time_budget_seconds cutoff (never the
    smaller mc_budget_seconds used for sizing): a true last-resort safety net for a
    machine meaningfully slower than the one _MONTE_CARLO_SECONDS_PER_UNIT was calibrated
    against, not the everyday mechanism, so it does not turn ordinary jitter into a
    reproducibility problem the way checking against the tighter budget did. See the
    module docstring and compute_scenario_report.
    """
    R = len(remaining_games)
    user_ids = list(linear_by_player.keys())
    game_ids = [g.game_id for g in remaining_games]
    target_set = set(representative_for)
    # Each player's constant already carries their real base (from already-final outcomes)
    # or, for a no-show, their whole scenario-invariant penalty. The chunk tables below
    # return exactly 0.0 contribution for a no-show (see _build_chunks), so starting every
    # sample's running total from this row and adding chunk contributions on top is correct
    # for both kinds of player without a separate branch here.
    base_row = tuple(linear_by_player[u].constant_points for u in user_ids)

    chunks = _build_chunks(linear_by_player, R, user_ids)
    seconds_per_sample = (
        _MONTE_CARLO_SECONDS_PER_REMAINING_GAME * R
        + _MONTE_CARLO_SECONDS_PER_CHUNK_PLAYER * len(chunks) * len(user_ids)
    ) * _MONTE_CARLO_SAFETY_MULTIPLIER
    affordable = (
        int(mc_budget_seconds / seconds_per_sample) if seconds_per_sample > 0 else sample_target
    )
    sample_target = max(1, min(sample_target, affordable))
    stop_at = hard_deadline

    rng = random.Random(seed)

    weighted_place: dict[int, dict[int, float]] = {u: {p: 0.0 for p in PLACES} for u in user_ids}
    seen_at: dict[int, dict[int, bool]] = {u: {p: False for p in PLACES} for u in user_ids}
    seen_not_at: dict[int, dict[int, bool]] = {u: {p: False for p in PLACES} for u in user_ids}
    leverage_home: dict[int, dict[int, float]] = {
        u: dict.fromkeys(game_ids, 0.0) for u in target_set
    }
    leverage_total: dict[int, float] = dict.fromkeys(target_set, 0.0)
    candidates: dict[int, list[tuple[tuple[bool, ...], float]]] = {u: [] for u in target_set}

    drawn = 0
    while drawn < sample_target:
        batch = min(_MONTE_CARLO_BATCH, sample_target - drawn)
        for _ in range(batch):
            bits = [rng.random() < p_home[j] for j in range(R)]
            values = list(base_row)
            for start, end, table in chunks:
                idx = 0
                for k, j in enumerate(range(start, end)):
                    if bits[j]:
                        idx |= 1 << k
                row = table[idx]
                values = [v + row[i] for i, v in enumerate(values)]
            ranks = _rank_values(values, mode)
            for i, user_id in enumerate(user_ids):
                place = ranks[i]
                wp = weighted_place[user_id]
                sa = seen_at[user_id]
                sn = seen_not_at[user_id]
                for p in PLACES:
                    if p == place:
                        wp[p] += 1.0
                        sa[p] = True
                    else:
                        sn[p] = True
                if place == 1 and user_id in target_set:
                    leverage_total[user_id] += 1.0
                    home_map = leverage_home[user_id]
                    for j, game_id in enumerate(game_ids):
                        if bits[j]:
                            home_map[game_id] += 1.0
                    bucket = candidates[user_id]
                    if len(bucket) < _REPRESENTATIVE_CANDIDATE_CAP:
                        bucket.append((tuple(bits), 1.0))
        drawn += batch
        if time.monotonic() >= stop_at:
            break

    total_weight = float(drawn)
    outlooks = _assemble_outlooks(
        user_ids=user_ids,
        game_ids=game_ids,
        total_weight=total_weight,
        weighted_place=weighted_place,
        seen_at=seen_at,
        seen_not_at=seen_not_at,
        leverage_home=leverage_home,
        leverage_total=leverage_total,
        candidates={u: list(v) for u, v in candidates.items()},
        decode=lambda bits, j: "home" if bits[j] else "away",
        max_representative=max_representative,
    )
    return outlooks, drawn


# Shared assembly --------------------------------------------------------------


def _select_representative(
    game_ids: list[int],
    leverage_home: dict[int, float],
    leverage_total: float,
    candidates: list[tuple[Any, float]],
    decode,
    max_representative: int,
) -> list[RepresentativeScenario]:
    """Up to max_representative scenarios chosen to span the leverage table.

    Heuristic: rank remaining games by how decisive they are to this player (how far their
    leverage share sits from 50/50), most decisive first. Then greedily farthest-first pick
    from the candidate pool: start with the first candidate, and repeatedly add whichever
    remaining candidate has the largest weighted Hamming distance (weighted toward the high
    leverage games) from the ones already picked. This is the concrete form of "prioritize
    scenarios that differ from each other on the games with the most leverage" (spec).
    """
    if not candidates:
        return []
    if leverage_total <= 0:
        weight_by_game = dict.fromkeys(game_ids, 0.0)
    else:
        weight_by_game = {gid: abs(leverage_home[gid] / leverage_total - 0.5) for gid in game_ids}
    ranked_games = sorted(game_ids, key=lambda g: -weight_by_game[g])
    game_weight = {g: len(game_ids) - i for i, g in enumerate(ranked_games)}

    decoded = [
        (tuple(decode(key, j) for j in range(len(game_ids))), weight) for key, weight in candidates
    ]

    def distance(a: tuple[str, ...], b: tuple[str, ...]) -> int:
        return sum(game_weight[game_ids[i]] for i in range(len(game_ids)) if a[i] != b[i])

    chosen_idx = [0]
    while len(chosen_idx) < max_representative and len(chosen_idx) < len(decoded):
        best_idx = None
        best_dist = -1
        for i, (sides, _w) in enumerate(decoded):
            if i in chosen_idx:
                continue
            min_dist = min(distance(sides, decoded[c][0]) for c in chosen_idx)
            if min_dist > best_dist:
                best_dist = min_dist
                best_idx = i
        if best_idx is None:
            break
        chosen_idx.append(best_idx)

    return [
        RepresentativeScenario(
            assignment=tuple(zip(game_ids, decoded[i][0], strict=False)),
            weight=decoded[i][1],
        )
        for i in chosen_idx
    ]


def _assemble_outlooks(
    *,
    user_ids: list[int],
    game_ids: list[int],
    total_weight: float,
    weighted_place: dict[int, dict[int, float]],
    seen_at: dict[int, dict[int, bool]],
    seen_not_at: dict[int, dict[int, bool]],
    leverage_home: dict[int, dict[int, float]],
    leverage_total: dict[int, float],
    candidates: dict[int, list[tuple[Any, float]]],
    decode,
    max_representative: int,
) -> dict[int, PlayerScenarioOutlook]:
    outlooks: dict[int, PlayerScenarioOutlook] = {}
    for user_id in user_ids:
        scenarios_at_place = dict(weighted_place[user_id])
        pct_at_place = {
            p: (scenarios_at_place[p] / total_weight if total_weight else 0.0) for p in PLACES
        }
        clinched = {p: seen_at[user_id][p] and not seen_not_at[user_id][p] for p in PLACES}
        eliminated = {p: not seen_at[user_id][p] for p in PLACES}

        leverage: dict[int, dict[str, float]] = {}
        representative: list[RepresentativeScenario] = []
        if user_id in leverage_total:
            total = leverage_total[user_id]
            home_map = leverage_home[user_id]
            for game_id in game_ids:
                home_share = (home_map[game_id] / total) if total > 0 else 0.5
                leverage[game_id] = {"home": home_share, "away": 1.0 - home_share}
            representative = _select_representative(
                game_ids,
                home_map,
                total,
                candidates[user_id],
                decode,
                max_representative,
            )

        outlooks[user_id] = PlayerScenarioOutlook(
            user_id=user_id,
            scenarios_at_place=scenarios_at_place,
            pct_at_place=pct_at_place,
            clinched=clinched,
            eliminated=eliminated,
            leverage=leverage,
            representative_scenarios=representative,
        )
    return outlooks


# Entry point --------------------------------------------------------------------


def compute_scenario_report(
    picks_by_player: Mapping[int, Sequence[PickInput]],
    final_outcomes: Sequence[GameOutcome],
    remaining_games: Sequence[RemainingGame],
    *,
    mode: str = "standard",
    picks_required: int | None = None,
    probability_model: str = "even",
    seed: int = 0,
    representative_for: Sequence[int] = (),
    max_representative: int = 5,
    time_budget_seconds: float = TIME_BUDGET_SECONDS,
    monte_carlo_samples: int = MONTE_CARLO_SAMPLES,
) -> ScenarioReport:
    """Every player's placement odds and leverage over every remaining game.

    Method selection, in order:

    1. Zero remaining games: one certain "scenario", no enumeration at all. Every player's
       clinched place is whichever place their already-final total actually lands them at
       (weight 1.0), every other place is eliminated (weight 0.0). Never an estimate.
    2. len(remaining_games) <= MAX_EXHAUSTIVE_REMAINING (20): attempt an exact exhaustive
       sweep of every 2**R scenario, weighted by probability_model. The wall clock is
       checked periodically during the attempt (after each player's points array, and
       again every slice of the ranking pass), not only at the end; if it is on pace to
       exceed _EXHAUSTIVE_ABORT_FRACTION of time_budget_seconds it aborts mid-run and
       falls through to step 3, using whichever time is left.
    3. Otherwise (R > 20, or the exhaustive attempt above aborted): Monte Carlo. Draws up
       to monte_carlo_samples scenarios seeded from seed, in fixed size batches, stopping
       early (with whatever has been drawn) once time is nearly spent. is_estimate is
       always True here, and the sample count actually used (ScenarioReport.scenario_count)
       can be less than monte_carlo_samples; see the module docstring for why 200,000 is a
       target on this hardware for a large R and many players, not a guarantee. Two calls
       with the same seed draw the identical sequence of samples and produce identical
       output, since the sample count itself is decided by elapsed wall clock time, not by
       the RNG, and re-running the identical computation back to back takes very nearly the
       identical amount of time.

    probability_model is "even" (every remaining game 50/50, every scenario equally likely)
    or "moneyline" (weight scenarios by win_probability per remaining game; games missing
    both a moneyline and a spread fall back to 50/50 for just that game, noted in
    probability_notes). This is read from the parameter alone, never a database.

    representative_for names which players get a computed leverage table and up to
    max_representative representative scenarios (spec: this is opt-in, not computed for
    every player by default, since both require decoding the actual per-game assignment of
    every one of a player's own winning scenarios, real work the placement-odds numbers do
    not otherwise need). Every player, requested or not, still gets scenarios_at_place,
    pct_at_place, clinched and eliminated.
    """
    if mode not in ("standard", "inverse"):
        raise ValueError(f"mode must be 'standard' or 'inverse', got {mode!r}.")
    if probability_model not in ("even", "moneyline"):
        raise ValueError(
            f"probability_model must be 'even' or 'moneyline', got {probability_model!r}."
        )

    start = time.monotonic()

    user_ids = list(picks_by_player.keys())
    game_ids = [g.game_id for g in remaining_games]
    R = len(remaining_games)

    probability_notes: list[str] = []
    if probability_model == "moneyline":
        p_home: list[float] = []
        for game in remaining_games:
            p, fell_back = win_probability(game)
            p_home.append(p)
            if fell_back:
                probability_notes.append(
                    f"Game {game.game_id} has no moneyline or spread; treated as 50/50."
                )
    else:
        p_home = [0.5] * R

    linear_by_player = _linearize_players(
        picks_by_player, final_outcomes, remaining_games, mode, picks_required
    )

    if R == 0:
        totals = {u: linear_by_player[u].constant_points for u in user_ids}
        ranks = rank_players(totals, mode)
        players = {}
        for user_id in user_ids:
            place = ranks[user_id]
            scenarios_at_place = {p: (1.0 if p == place else 0.0) for p in PLACES}
            players[user_id] = PlayerScenarioOutlook(
                user_id=user_id,
                scenarios_at_place=scenarios_at_place,
                pct_at_place=dict(scenarios_at_place),
                clinched={p: (p == place) for p in PLACES},
                eliminated={p: (p != place) for p in PLACES},
                leverage={},
                representative_scenarios=[],
            )
        return ScenarioReport(
            remaining_game_ids=game_ids,
            total_weight=1.0,
            scenario_count=1,
            is_estimate=False,
            method="exhaustive",
            probability_model=probability_model,
            probability_notes=probability_notes,
            elapsed_seconds=time.monotonic() - start,
            players=players,
        )

    method = "exhaustive"
    is_estimate = False
    scenario_count = 1 << R
    players: dict[int, PlayerScenarioOutlook] | None = None
    total_weight = 0.0

    if R <= MAX_EXHAUSTIVE_REMAINING:
        exhaustive_deadline = start + time_budget_seconds * _EXHAUSTIVE_ABORT_FRACTION
        exhaustive_result = _try_exhaustive(
            linear_by_player,
            p_home,
            remaining_games,
            mode,
            representative_for,
            max_representative,
            exhaustive_deadline,
        )
        if exhaustive_result is not None:
            players, total_weight = exhaustive_result

    if players is None:
        method = "monte_carlo"
        is_estimate = True
        # The Monte Carlo sample count is sized against this fraction of the ORIGINAL
        # budget regardless of how much the exhaustive attempt above already spent (it
        # always aborts, if it aborts at all, at _EXHAUSTIVE_ABORT_FRACTION, leaving
        # meaningfully more than this behind), so the target is a fixed function of the
        # inputs alone, not of how long the doomed attempt happened to run. See
        # _run_monte_carlo's docstring for why that determinism matters.
        mc_budget_seconds = time_budget_seconds * _MONTE_CARLO_STOP_FRACTION
        players, drawn = _run_monte_carlo(
            linear_by_player,
            p_home,
            remaining_games,
            mode,
            representative_for,
            max_representative,
            seed,
            monte_carlo_samples,
            mc_budget_seconds,
            start + time_budget_seconds,
        )
        scenario_count = drawn
        total_weight = float(drawn)

    return ScenarioReport(
        remaining_game_ids=game_ids,
        total_weight=total_weight,
        scenario_count=scenario_count,
        is_estimate=is_estimate,
        method=method,
        probability_model=probability_model,
        probability_notes=probability_notes,
        elapsed_seconds=time.monotonic() - start,
        players=players,
    )


# Build your own scenario --------------------------------------------------------


def build_custom_scenario(
    picks_by_player: Mapping[int, Sequence[PickInput]],
    final_outcomes: Sequence[GameOutcome],
    fixed_remaining: Mapping[int, str | None],
    *,
    mode: str = "standard",
    picks_required: int | None = None,
) -> list[StandingsRow]:
    """Live standings under one commissioner or player chosen assumption.

    fixed_remaining maps a remaining game's id to "home", "away", or None. A three state
    choice: home wins, away wins, or left undecided. An undecided game is simply omitted
    from the combined outcome list, exactly how score_week already treats a game that has
    not gone final yet (it stays out of everyone's possible count), so this function needs
    no special casing for "undecided", the omission alone is the whole behavior.

    This is one pass, not a scenario sweep: every player is scored once with score_week
    against the one combined outcome list, then ranked with the same rank_players this
    module's sweep uses, so a commissioner's "what if" table can never drift from either
    real scoring or the sweep's own notion of place.
    """
    combined = list(final_outcomes) + [
        GameOutcome(game_id=game_id, status="final", winner=side)
        for game_id, side in fixed_remaining.items()
        if side in ("home", "away")
    ]
    results = {
        user_id: score_week(list(picks), combined, mode=mode, picks_required=picks_required)
        for user_id, picks in picks_by_player.items()
    }
    ranks = rank_players({uid: r.points for uid, r in results.items()}, mode)
    rows = [
        StandingsRow(
            user_id=user_id,
            points=result.points,
            correct=result.correct,
            possible=result.possible,
            did_not_submit=result.did_not_submit,
            place=ranks[user_id],
        )
        for user_id, result in results.items()
    ]
    rows.sort(key=lambda row: (row.place, row.user_id))
    return rows
