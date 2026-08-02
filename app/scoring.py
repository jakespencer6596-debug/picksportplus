"""Scoring rules for a confidence pick'em week (Spec Section 9).

Pure logic only. No database, no network, no imports from app.models. Everything here
takes plain dataclasses in and returns plain values out, so it can be unit tested
without any application context.

The rules, in one place:

- A pick earns its staked confidence points when the player picked the team that won,
  otherwise it earns nothing.
- A tie game and a voided game are not countable. Nobody scores them and they drop out
  of both the correct count and the possible count.
- A game that is still scheduled or in progress is not countable yet, so it is not part
  of the possible count until it goes final.
- A player who never submitted scores 0 points and 0 correct, but the possible count for
  the week is still the size of the countable slate.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

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
    """What one player earned in one week."""

    points: int
    correct: int
    possible: int


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


def score_pick(pick: PickInput, outcome: GameOutcome) -> int:
    """Points earned. confidence when countable and correct, otherwise 0."""
    if not is_countable(outcome):
        return 0
    if pick.picked_team != outcome.winner:
        return 0
    return pick.confidence


def score_week(
    picks: Sequence[PickInput],
    outcomes: Sequence[GameOutcome],
) -> WeekResult:
    """Score one player's week against the slate outcomes.

    possible = number of countable outcomes on the slate, regardless of whether the player
    submitted a pick for them. correct = number of countable games the player picked right.
    points = sum of earned points. A player with no picks scores 0 / 0 / possible.
    Picks referring to a game_id not in outcomes are ignored (the game left the slate).
    """
    by_game: dict[int, GameOutcome] = {o.game_id: o for o in outcomes}
    possible = sum(1 for outcome in by_game.values() if is_countable(outcome))

    points = 0
    correct = 0
    for pick in picks:
        outcome = by_game.get(pick.game_id)
        if outcome is None or not is_countable(outcome):
            continue
        if pick.picked_team != outcome.winner:
            continue
        # correct comes from the pick being right, never from the earned points being
        # non zero, so a stored confidence of 0 cannot hide a correct pick.
        points += score_pick(pick, outcome)
        correct += 1

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


def weekly_winner_ids(results: Mapping[int, WeekResult]) -> set[int]:
    """User ids with the highest points. Ties share the win.

    Returns an empty set when there are no results or when the highest score is 0, because
    nobody wins a week nobody scored in.
    """
    if not results:
        return set()
    best = max(result.points for result in results.values())
    if best <= 0:
        return set()
    return {user_id for user_id, result in results.items() if result.points == best}


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
