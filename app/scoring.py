"""Scoring rules for a confidence pick'em week (Spec Section 9).

Pure logic only. No database, no network, no imports from app.models. Everything here
takes plain dataclasses in and returns plain values out, so it can be unit tested
without any application context.

Every pool runs in one of two scoring modes, set per pool on Pool.scoring_mode:

- "standard": a pick earns its staked confidence points when the player picked the team
  that won, otherwise it earns nothing. Highest total wins. This is the old behavior,
  kept working for any pool that wants it, and it is what every function here defaults
  to when a caller does not pass a mode.
- "inverse": the real rule this pool runs under, and the default at the Pool level
  (Pool.scoring_mode defaults to "inverse"). A pick earns its staked confidence points
  AGAINST the player when the pick is wrong, and nothing when it is right. Lowest total
  wins. You are penalized for being wrong, so staking your highest confidence on your
  surest pick is a real risk: if that pick loses, you eat all of those points.

Rules that hold in both modes:

- A tie game and a voided game are not countable. Nobody scores them and they drop out
  of both the correct count and the possible count.
- A game that is still scheduled or in progress is not countable yet, so it is not part
  of the possible count until it goes final.
- "correct" always counts picks that matched the winner, in both modes. It measures how
  well someone actually picked, independent of which direction points run.
- A player who never submitted a pick for the week is flagged with did_not_submit=True.
  Under "standard" they score 0 points and 0 correct, unchanged from before this phase.
  Under "inverse" a no-show is the trap this rule is built to catch: they take the
  maximum possible penalty, sum(1..picks_required), since a player who dodges the risk
  of a wrong pick cannot be allowed to end up better off than someone who played and
  lost. Either way the possible count for the week is still the size of the countable
  slate, and a no-show is never eligible to win the week (see weekly_winner_ids).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "TEAM_SIDES",
    "GameOutcome",
    "PickInput",
    "WeekResult",
    "SeasonEntryInput",
    "SeasonTotals",
    "is_countable",
    "score_pick",
    "score_week",
    "validate_picks",
    "weekly_winner_ids",
    "season_totals",
]

# The only two values a player may pick. A tie is an outcome, never a pick.
TEAM_SIDES = ("home", "away")


@dataclass(frozen=True)
class GameOutcome:
    """The state of one slate game as far as scoring is concerned."""

    game_id: int
    status: str  # "scheduled" | "in_progress" | "final" | "void"
    winner: str | None  # "home" | "away" | "tie" | None


@dataclass(frozen=True)
class PickInput:
    """One submitted pick: the side taken and the points staked on it."""

    game_id: int
    picked_team: str  # "home" | "away"
    confidence: int


@dataclass(frozen=True)
class WeekResult:
    """What one player earned in one week.

    did_not_submit is True when the player had no picks at all for the week. The UI reads
    this directly to show "no picks submitted" instead of a bare score, and
    weekly_winner_ids reads it to exclude a no-show from ever winning the week.
    """

    points: int
    correct: int
    possible: int
    did_not_submit: bool = False


@dataclass(frozen=True)
class SeasonEntryInput:
    """One stored week entry for one player, as input to a season total."""

    points: int
    correct: int
    possible: int
    is_winner: bool


@dataclass(frozen=True)
class SeasonTotals:
    """Season standings row for one player, aggregated from week entries."""

    points: int
    correct: int
    possible: int
    weeks_played: int
    weekly_wins: int


def is_countable(outcome: GameOutcome) -> bool:
    """True only when status == "final" and winner in ("home", "away").

    A tie or a voided game is NOT countable: nobody scores and it leaves the possible count.
    """
    return outcome.status == "final" and outcome.winner in TEAM_SIDES


def score_pick(pick: PickInput, outcome: GameOutcome, mode: str = "standard") -> int:
    """Points earned on one pick. 0 for any non countable outcome, in either mode.

    "standard": confidence when correct, 0 when wrong.
    "inverse": confidence AGAINST the player when wrong, 0 when correct.
    """
    if not is_countable(outcome):
        return 0
    right = pick.picked_team == outcome.winner
    if mode == "inverse":
        return 0 if right else pick.confidence
    return pick.confidence if right else 0


def score_week(
    picks: Sequence[PickInput],
    outcomes: Sequence[GameOutcome],
    *,
    mode: str = "standard",
    picks_required: int | None = None,
) -> WeekResult:
    """Score one player's week against the slate outcomes.

    possible = number of countable outcomes on the slate, regardless of whether the player
    submitted a pick for them. correct = number of countable games the player picked right,
    counted the same way in both modes. points = sum of score_pick over the picks, which is
    the wrong ones under "inverse" and the right ones under "standard".

    picks_required is how many picks a full submission needs. When not given it defaults to
    len(outcomes), which is correct as long as every player picks the whole slate (true
    today; a future phase that lets players pick a subset of the slate will pass this
    explicitly instead of relying on the default).

    A player with no picks at all is a no-show: did_not_submit=True, correct=0, and points
    is 0 under "standard" (unchanged prior behavior) or the maximum possible penalty,
    sum(1..picks_required), under "inverse". possible is still the size of the countable
    slate either way. Picks referring to a game_id not in outcomes are ignored (the game
    left the slate).
    """
    by_game: dict[int, GameOutcome] = {o.game_id: o for o in outcomes}
    possible = sum(1 for outcome in by_game.values() if is_countable(outcome))

    if not picks:
        if picks_required is None:
            picks_required = len(outcomes)
        penalty = sum(range(1, picks_required + 1)) if mode == "inverse" else 0
        return WeekResult(points=penalty, correct=0, possible=possible, did_not_submit=True)

    points = 0
    correct = 0
    for pick in picks:
        outcome = by_game.get(pick.game_id)
        if outcome is None or not is_countable(outcome):
            continue
        # correct comes from the pick being right, never from the earned points being
        # non zero, so a stored confidence of 0 cannot hide a correct pick, and it counts
        # the same way regardless of mode.
        if pick.picked_team == outcome.winner:
            correct += 1
        points += score_pick(pick, outcome, mode=mode)

    return WeekResult(points=points, correct=correct, possible=possible)


def validate_picks(
    picks: Sequence[PickInput],
    slate_game_ids: Sequence[int],
) -> list[str]:
    """Return a list of human readable error strings, empty when the submission is valid.

    Checks: exactly one pick per slate game and no extras, picked_team in ("home","away"),
    and the confidence values are exactly a permutation of 1..N where N is the number of
    distinct game ids on the slate. Messages are sentence case, plainspoken, and name the
    problem concretely, for example "Confidence value 7 is used twice." or
    "Two games are missing a winner."
    """
    errors: list[str] = []
    # A repeated id in the slate list is still one game, so N counts distinct ids.
    slate_ids = list(dict.fromkeys(slate_game_ids))
    n = len(slate_ids)

    if n == 0:
        for game_id in sorted({p.game_id for p in picks}):
            errors.append(f"Game {game_id} is not on this week's slate.")
        return errors

    if not picks:
        return ["No picks were submitted."]

    slate_set = set(slate_ids)
    picks_per_game = Counter(p.game_id for p in picks)

    # Games picked that are not on the slate.
    for game_id in sorted(gid for gid in picks_per_game if gid not in slate_set):
        errors.append(f"Game {game_id} is not on this week's slate.")

    # The same game picked more than once.
    for game_id in sorted(gid for gid, count in picks_per_game.items() if count > 1):
        errors.append(f"Game {game_id} has more than one pick.")

    # Slate games with no pick at all.
    missing = sum(1 for gid in slate_ids if gid not in picks_per_game)
    if missing == 1:
        errors.append("One game is missing a winner.")
    elif missing > 1:
        errors.append(f"{_number_word(missing)} games are missing a winner.")

    # Picks that name something other than home or away.
    for pick in picks:
        if pick.picked_team not in TEAM_SIDES:
            errors.append(
                f"Game {pick.game_id} has an invalid pick of "
                f'"{pick.picked_team}". Pick home or away.'
            )

    errors.extend(_confidence_errors(picks, slate_set, n))
    return errors


def _confidence_errors(
    picks: Sequence[PickInput],
    slate_set: set[int],
    n: int,
) -> list[str]:
    """Confidence must be a permutation of 1..N across the slate games."""
    errors: list[str] = []
    values = [p.confidence for p in picks if p.game_id in slate_set]
    used = Counter(values)

    for value in sorted(v for v, count in used.items() if count > 1):
        errors.append(f"Confidence value {value} is used {_times_phrase(used[value])}.")

    for value in sorted(v for v in used if not 1 <= v <= n):
        errors.append(f"Confidence value {value} is outside the range 1 to {n}.")

    # Only call out unused values when the pick count lines up with the slate. Otherwise
    # the missing pick message already explains the gap and this would just repeat it.
    if len(values) == n:
        for value in range(1, n + 1):
            if value not in used:
                errors.append(f"Confidence value {value} is not used.")

    return errors


def weekly_winner_ids(results: Mapping[int, WeekResult], mode: str = "standard") -> set[int]:
    """User ids who won the week. Ties share the win.

    A no-show (did_not_submit=True) is excluded from winning in both modes, before either
    the highest or lowest score is worked out: sitting the week out must never be a route
    to a weekly win, whether that is because the arithmetic makes 0 look like a win under
    "inverse" or because it never was under "standard".

    "standard": highest points among the remaining eligible results wins. If the eligible
    set is empty, or the best remaining score is 0 or less, returns an empty set: nobody
    wins a week nobody scored in.

    "inverse": lowest points among the remaining eligible results wins. 0 is a perfect
    score here, so the 0-or-less guard from "standard" does not apply. Returns an empty
    set only when the eligible set itself is empty (everyone was a no-show).
    """
    eligible = {user_id: result for user_id, result in results.items() if not result.did_not_submit}
    if not eligible:
        return set()
    if mode == "inverse":
        best = min(result.points for result in eligible.values())
        return {user_id for user_id, result in eligible.items() if result.points == best}
    best = max(result.points for result in eligible.values())
    if best <= 0:
        return set()
    return {user_id for user_id, result in eligible.items() if result.points == best}


def season_totals(entries: Iterable[SeasonEntryInput]) -> SeasonTotals:
    """Aggregate week entries for one player."""
    points = 0
    correct = 0
    possible = 0
    weeks_played = 0
    weekly_wins = 0

    for entry in entries:
        points += entry.points
        correct += entry.correct
        possible += entry.possible
        weeks_played += 1
        if entry.is_winner:
            weekly_wins += 1

    return SeasonTotals(
        points=points,
        correct=correct,
        possible=possible,
        weeks_played=weeks_played,
        weekly_wins=weekly_wins,
    )


_NUMBER_WORDS = {
    1: "One",
    2: "Two",
    3: "Three",
    4: "Four",
    5: "Five",
    6: "Six",
    7: "Seven",
    8: "Eight",
    9: "Nine",
    10: "Ten",
    11: "Eleven",
    12: "Twelve",
    13: "Thirteen",
    14: "Fourteen",
    15: "Fifteen",
    16: "Sixteen",
}

_TIMES_PHRASES = {
    2: "twice",
    3: "three times",
    4: "four times",
    5: "five times",
    6: "six times",
}


def _number_word(count: int) -> str:
    """Sentence leading number word for small counts, digits beyond the slate range."""
    return _NUMBER_WORDS.get(count, str(count))


def _times_phrase(count: int) -> str:
    """How many times a confidence value was reused, in plain words."""
    return _TIMES_PHRASES.get(count, f"{count} times")
